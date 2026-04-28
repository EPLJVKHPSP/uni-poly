"""Bulk-download Probalytics orderbook snapshots into per-(market, day) Parquet.

Each download call asks for at most 24h of one market. This keeps the
server-side LOCF interpolation bounded, sidesteps the request-timeout cliff
we hit when asking for a market's full lifetime, and produces a storage
layout that matches our fills partitioning::

    data/probalytics/orderbooks/<YYYY-MM-DD>/<market_platform_id>.parquet

Resumability is file-based: a non-empty file at that path skips the
download. Empty payloads (the ~534 byte header-only Parquet that the API
returns when no snapshots exist in the window) are detected and removed,
then recorded in ``_meta/orderbook_empty.json`` so we don't keep retrying
known-empty (market, day) pairs.
"""

from __future__ import annotations

import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, List, Optional, Sequence, Set, Tuple

import pandas as pd

from .client import ProbalyticsRest

logger = logging.getLogger(__name__)


# A Parquet with only the header (no row groups) consistently weighs ~534 bytes
# from this endpoint. Treat anything at or below this as "no rows".
_EMPTY_PARQUET_BYTES = 768


def _to_iso_z(ts) -> str:
    """RFC3339 in UTC with trailing ``Z`` (Probalytics' expected format)."""
    if isinstance(ts, str):
        return ts
    if isinstance(ts, pd.Timestamp):
        ts = ts.to_pydatetime()
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _coerce_ts(x) -> datetime:
    if isinstance(x, datetime):
        return x if x.tzinfo else x.replace(tzinfo=timezone.utc)
    if isinstance(x, pd.Timestamp):
        ts = x.to_pydatetime()
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    raise TypeError(f"cannot coerce {type(x)} to datetime")


def _day_window(day: date) -> Tuple[datetime, datetime]:
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    return start, start + timedelta(days=1)


def _intersect_day_with_market(
    day: date, opened: datetime, closed: datetime,
    floor: Optional[datetime], ceiling: Optional[datetime],
) -> Optional[Tuple[datetime, datetime]]:
    win_start, win_end = _day_window(day)
    lo = max(win_start, opened)
    hi = min(win_end, closed)
    if floor is not None:
        lo = max(lo, floor)
    if ceiling is not None:
        hi = min(hi, ceiling)
    if hi <= lo:
        return None
    return lo, hi


@dataclass
class OrderBookSyncResult:
    market_platform_id: str
    day: date
    out_path: str
    bytes_written: int
    skipped: bool = False
    empty: bool = False
    error: Optional[str] = None


@dataclass
class OrderBookSyncStats:
    attempted: int = 0
    skipped_existing: int = 0
    skipped_known_empty: int = 0
    succeeded: int = 0
    empty: int = 0
    errored: int = 0
    bytes_total: int = 0
    errors: List[str] = field(default_factory=list)

    def add(self, r: OrderBookSyncResult) -> None:
        self.attempted += 1
        if r.skipped:
            self.skipped_existing += 1
            return
        if r.error:
            self.errored += 1
            self.errors.append(f"{r.market_platform_id} {r.day}: {r.error}")
            return
        if r.empty:
            self.empty += 1
            return
        self.succeeded += 1
        self.bytes_total += r.bytes_written


def _load_known_empty(meta_path: str) -> Set[Tuple[str, str]]:
    if not os.path.exists(meta_path):
        return set()
    try:
        with open(meta_path) as fp:
            return {tuple(x) for x in json.load(fp)}
    except Exception:
        return set()


def _save_known_empty(meta_path: str, items: Set[Tuple[str, str]]) -> None:
    os.makedirs(os.path.dirname(meta_path), exist_ok=True)
    with open(meta_path, "w") as fp:
        json.dump(sorted(items), fp)


def _download_one(
    rest: ProbalyticsRest,
    job: Tuple[str, date, str, str, str],
    timeout: float,
) -> OrderBookSyncResult:
    platform_id, day, start, end, out_path = job
    try:
        n = rest.download_orderbook(platform_id, start, end, out_path, timeout=timeout)
        if n is None:
            return OrderBookSyncResult(platform_id, day, out_path, 0,
                                       error="non-200 response (see warning log)")
        if n <= _EMPTY_PARQUET_BYTES:
            try:
                os.remove(out_path)
            except OSError:
                pass
            return OrderBookSyncResult(platform_id, day, out_path, 0, empty=True)
        return OrderBookSyncResult(platform_id, day, out_path, n)
    except Exception as exc:  # noqa: BLE001
        return OrderBookSyncResult(platform_id, day, out_path, 0, error=str(exc))


def sync_orderbooks(
    rest: ProbalyticsRest,
    universe: pd.DataFrame,
    *,
    root: str = "data/probalytics",
    start_floor: Optional[datetime] = None,
    end_ceiling: Optional[datetime] = None,
    workers: int = 4,
    force: bool = False,
    progress_every: int = 25,
    request_timeout: float = 120.0,
) -> OrderBookSyncStats:
    """Download per-(market, day) orderbook snapshots for every market in ``universe``.

    ``universe`` must include columns ``market_platform_id``, ``opened_at``,
    and ``closes_at`` (the values returned by ``fetch_market_universe``).
    """
    out_root = os.path.join(root, "orderbooks")
    meta_path = os.path.join(root, "_meta", "orderbook_empty.json")
    known_empty = _load_known_empty(meta_path)
    new_empty: Set[Tuple[str, str]] = set()

    jobs: List[Tuple[str, date, str, str, str]] = []
    skipped_existing = 0
    skipped_known_empty = 0
    for _, row in universe.iterrows():
        platform_id = str(row["market_platform_id"])
        opened = row.get("opened_at")
        closes = row.get("closes_at") or row.get("end_date") or row.get("resolves_at")
        if opened is None or pd.isna(opened):
            continue
        if closes is None or pd.isna(closes):
            closes = end_ceiling or datetime.now(timezone.utc)

        opened_dt = _coerce_ts(opened)
        closes_dt = _coerce_ts(closes)

        lo_day = max(opened_dt.date(), start_floor.date() if start_floor else opened_dt.date())
        hi_day = min(closes_dt.date(), end_ceiling.date() if end_ceiling else closes_dt.date())
        if hi_day < lo_day:
            continue

        d = lo_day
        while d <= hi_day:
            day_str = d.isoformat()
            out_path = os.path.join(out_root, day_str, f"{platform_id}.parquet")
            if (platform_id, day_str) in known_empty:
                skipped_known_empty += 1
                d += timedelta(days=1)
                continue
            if not force and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                skipped_existing += 1
                d += timedelta(days=1)
                continue
            window = _intersect_day_with_market(d, opened_dt, closes_dt, start_floor, end_ceiling)
            if window is None:
                d += timedelta(days=1)
                continue
            jobs.append((platform_id, d, _to_iso_z(window[0]), _to_iso_z(window[1]), out_path))
            d += timedelta(days=1)

    stats = OrderBookSyncStats(
        skipped_existing=skipped_existing,
        skipped_known_empty=skipped_known_empty,
    )
    logger.info(
        "orderbook sync: %d (market, day) jobs to fetch, %d already on disk, %d known-empty",
        len(jobs), skipped_existing, skipped_known_empty,
    )
    if not jobs:
        return stats

    with ThreadPoolExecutor(max_workers=max(workers, 1)) as pool:
        futures = {pool.submit(_download_one, rest, job, request_timeout): job for job in jobs}
        done = 0
        for fut in as_completed(futures):
            res = fut.result()
            stats.add(res)
            if res.empty:
                new_empty.add((res.market_platform_id, res.day.isoformat()))
            done += 1
            if done % progress_every == 0 or done == len(jobs):
                logger.info(
                    "  progress: %d/%d  ok=%d empty=%d err=%d  bytes=%.1fMB",
                    done, len(jobs), stats.succeeded, stats.empty, stats.errored,
                    stats.bytes_total / 1e6,
                )
                # Flush known-empty incrementally so a kill mid-run keeps progress.
                if new_empty:
                    _save_known_empty(meta_path, known_empty | new_empty)

    if new_empty:
        _save_known_empty(meta_path, known_empty | new_empty)
    return stats


def write_sync_state(
    root: str,
    *,
    universe_size: int,
    fills_paths: Sequence[str],
    book_stats: OrderBookSyncStats,
) -> str:
    """Persist a small JSON describing the most recent sync run."""
    meta_dir = os.path.join(root, "_meta")
    os.makedirs(meta_dir, exist_ok=True)
    payload = {
        "synced_at": datetime.now(timezone.utc).isoformat(),
        "universe_size": int(universe_size),
        "fills_files": [os.path.relpath(p, root) for p in fills_paths],
        "orderbooks": {
            "attempted": book_stats.attempted,
            "skipped_existing": book_stats.skipped_existing,
            "skipped_known_empty": book_stats.skipped_known_empty,
            "succeeded": book_stats.succeeded,
            "empty": book_stats.empty,
            "errored": book_stats.errored,
            "bytes_total": book_stats.bytes_total,
            "errors_sample": book_stats.errors[:25],
        },
    }
    out = os.path.join(meta_dir, "sync_state.json")
    with open(out, "w") as fp:
        json.dump(payload, fp, indent=2, default=str)
    return out

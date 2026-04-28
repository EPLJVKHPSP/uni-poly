"""Sync the BTC/ETH strike-market universe and historical fills from Probalytics.

We hit ClickHouse rather than the REST list endpoints because the SQL backend
gives us full historical scans without paginating millions of rows over HTTP.

Outputs (all under ``data/probalytics/``):

  - ``markets.parquet``           — universe metadata, one row per market
  - ``fills/<YYYY-MM-DD>.parquet`` — fills partitioned by UTC day

The two functions are independently runnable from the CLI in
``scripts/probalytics_sync.py``.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Iterator, List, Optional, Sequence, Tuple

import pandas as pd

from .client import ProbalyticsCreds, open_clickhouse, stream_clickhouse

logger = logging.getLogger(__name__)


DEFAULT_DATA_ROOT = "data/probalytics"


# ---------------------------------------------------------------------------
# Market-universe selection
# ---------------------------------------------------------------------------

def _slug_predicate(
    asset_filters: Sequence[str],
    include_hourly: bool,
    include_daily: bool,
    *,
    escape_pct: bool = False,
) -> str:
    """Return a SQL fragment matching our BTC/ETH strike slug families.

    Three families are matched (when enabled):

      hourly: ``<asset>-above-<STRIKE>-on-<DATE>-<HHMM>am-et`` (binary touch).
      daily/weekly multi-strike: ``will-<asset>-reach-<STRIKE>-<DATE>`` —
        Probalytics' name for the per-strike children of the
        ``what-price-will-<asset>-hit-<MONTH>`` event group.
      monthly multi-strike root: ``what-price-will-<asset>-hit-<MONTH>`` —
        rare but kept for completeness.

    When ``escape_pct=True`` the ``%`` LIKE wildcards are doubled so the result
    is safe to drop into a SQL string that goes through ``str.__mod__`` for
    parameter substitution (which is what clickhouse-driver does).
    """
    pct = "%%" if escape_pct else "%"
    assets = [a.lower() for a in asset_filters]
    asset_short = {"bitcoin": "btc", "ethereum": "eth"}
    if include_hourly:
        asset_alts = "(" + " OR ".join(
            f"m.slug LIKE '{a}-above-{pct}'" for a in assets
        ) + ")"
    else:
        asset_alts = "FALSE"
    if include_daily:
        # Long-form full-name slug: will-ethereum-reach-3000-on-april-21
        long_alts = " OR ".join(
            f"m.slug LIKE 'will-{a}-reach-{pct}'" for a in assets
        )
        # Short-form ticker slug: will-eth-reach-3000-on-april-21
        short_alts = " OR ".join(
            f"m.slug LIKE 'will-{asset_short[a]}-reach-{pct}'"
            for a in assets if a in asset_short
        )
        # Event/root slug: what-price-will-ethereum-hit-in-april-2026
        root_alts = " OR ".join(
            f"m.slug LIKE 'what-price-will-{a}-hit-{pct}'" for a in assets
        )
        parts = [p for p in (long_alts, short_alts, root_alts) if p]
        daily_alts = "(" + " OR ".join(parts) + ")" if parts else "FALSE"
    else:
        daily_alts = "FALSE"
    return f"({asset_alts} OR {daily_alts})"


def fetch_market_universe(
    creds: ProbalyticsCreds,
    *,
    assets: Sequence[str] = ("bitcoin", "ethereum"),
    include_hourly: bool = True,
    include_daily: bool = True,
) -> pd.DataFrame:
    """Pull metadata for every BTC/ETH strike market Probalytics knows about."""
    client = open_clickhouse(creds)
    where = _slug_predicate(assets, include_hourly=include_hourly, include_daily=include_daily)
    # Bare execute() doesn't go through %-formatting, so we use the un-escaped form.
    sql = f"""
    SELECT
        m.id              AS probalytics_id,
        m.platform_id     AS market_platform_id,
        m.slug,
        m.title,
        m.category,
        m.market_type,
        m.opened_at,
        m.closes_at,
        m.resolves_at,
        m.end_date,
        m.status,
        m.outcomes
    FROM markets m
    WHERE m.platform = 'POLYMARKET'
      AND {where}
    ORDER BY m.opened_at ASC
    """
    rows = client.execute(sql, with_column_types=True)
    data, cols = rows
    df = pd.DataFrame(data, columns=[c[0] for c in cols])
    # `outcomes` is a list of (id, platform_id, name, index) tuples; keep it raw.
    df["asset"] = df["slug"].apply(_classify_asset)
    df["kind"] = df["slug"].apply(_classify_kind)
    return df


def _classify_asset(slug: str) -> Optional[str]:
    s = slug or ""
    if s.startswith("bitcoin-above-") or s.startswith("what-price-will-bitcoin-hit-"):
        return "BTC"
    if s.startswith("ethereum-above-") or s.startswith("what-price-will-ethereum-hit-"):
        return "ETH"
    return None


def _classify_kind(slug: str) -> Optional[str]:
    s = slug or ""
    if "-above-" in s:
        return "hourly_above"
    if s.startswith("what-price-will-"):
        return "daily_touch"
    return None


def write_markets_parquet(df: pd.DataFrame, root: str = DEFAULT_DATA_ROOT) -> str:
    out = os.path.join(root, "markets.parquet")
    os.makedirs(root, exist_ok=True)
    # Outcomes is a list of dataclass-like tuples; convert to plain dicts so
    # parquet round-trips cleanly across versions.
    df = df.copy()
    df["outcomes"] = df["outcomes"].apply(_outcomes_to_records)
    df.to_parquet(out, index=False)
    logger.info("wrote %d markets -> %s", len(df), out)
    return out


def _outcomes_to_records(items) -> list[dict]:
    if items is None:
        return []
    out = []
    for it in items:
        if isinstance(it, dict):
            out.append({k: it[k] for k in ("id", "platform_id", "name", "index") if k in it})
        else:
            # tuple form: (id, platform_id, name, index)
            id_, plat, name, idx = it
            out.append({"id": str(id_), "platform_id": plat, "name": name, "index": int(idx)})
    return out


# ---------------------------------------------------------------------------
# Fills sync (per-day Parquet partitions)
# ---------------------------------------------------------------------------

def sync_fills_for_universe(
    creds: ProbalyticsCreds,
    market_platform_ids: Optional[Sequence[str]] = None,
    *,
    assets: Sequence[str] = ("bitcoin", "ethereum"),
    include_hourly: bool = True,
    include_daily: bool = True,
    start_day: Optional[date] = None,
    end_day: Optional[date] = None,
    root: str = DEFAULT_DATA_ROOT,
    chunk_size: int = 100_000,
) -> List[str]:
    """Download every fill matching the BTC/ETH strike universe, partitioned by UTC day.

    Selection happens *server-side* via a join against the markets table — we
    never have to inline thousands of platform_ids (ClickHouse caps query
    size at ~256 KB by default). Pass ``market_platform_ids`` only if you
    want a tighter slice; otherwise leave it ``None`` and rely on the slug
    filter.

    Returns the list of Parquet files written.
    """
    client = open_clickhouse(creds)
    where_slug_bare = _slug_predicate(assets, include_hourly=include_hourly, include_daily=include_daily)
    where_slug_esc = _slug_predicate(assets, include_hourly=include_hourly, include_daily=include_daily, escape_pct=True)

    if start_day is None or end_day is None:
        rng = client.execute(
            f"""
            SELECT toDate(min(f.timestamp)), toDate(max(f.timestamp))
            FROM fills f INNER JOIN markets m ON f.market_id = m.id
            WHERE m.platform = 'POLYMARKET' AND {where_slug_bare}
            """
        )[0]
        start_day = start_day or rng[0]
        end_day = end_day or rng[1]
    if start_day is None or end_day is None:
        logger.warning("No fills found in universe")
        return []

    written: List[str] = []
    for d in _daterange(start_day, end_day):
        out = _sync_fills_for_day(client, where_slug_esc, d, root=root, chunk_size=chunk_size)
        if out:
            written.append(out)
    return written


def _sync_fills_for_day(
    client,
    where_slug: str,
    day: date,
    *,
    root: str,
    chunk_size: int,
) -> Optional[str]:
    out_path = os.path.join(root, "fills", f"{day.isoformat()}.parquet")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    # NOTE: ``where_slug`` must be the escape_pct=True variant — clickhouse-driver
    # runs the SQL through Python ``%`` formatting before sending it.
    sql = f"""
    SELECT
        f.market_platform_id,
        f.platform,
        f.outcome.platform_id           AS outcome_platform_id,
        f.outcome.name                  AS outcome_name,
        f.outcome.index                 AS outcome_index,
        toFloat64(f.size)               AS size,
        toFloat64(f.price)              AS price,
        toFloat64(f.normalized_price)   AS normalized_price,
        f.taker_side,
        toFloat64(f.taker_cash_flow)    AS taker_cash_flow,
        toFloat64(f.maker_cash_flow)    AS maker_cash_flow,
        f.taker_id,
        f.maker_id,
        toFloat64(f.fee)                AS fee,
        f.timestamp
    FROM fills f INNER JOIN markets m ON f.market_id = m.id
    WHERE m.platform = 'POLYMARKET'
      AND {where_slug}
      AND toDate(f.timestamp) = %(day)s
    ORDER BY f.timestamp ASC
    """

    columns = [
        "market_platform_id", "platform",
        "outcome_platform_id", "outcome_name", "outcome_index",
        "size", "price", "normalized_price",
        "taker_side", "taker_cash_flow", "maker_cash_flow",
        "taker_id", "maker_id", "fee", "timestamp",
    ]
    frames: list[pd.DataFrame] = []
    total = 0
    for batch in stream_clickhouse(client, sql, {"day": day}, chunk_size=chunk_size):
        frames.append(pd.DataFrame(batch, columns=columns))
        total += len(batch)
    if not frames:
        logger.info("fills %s: 0 rows (skipped)", day.isoformat())
        return None
    df = pd.concat(frames, ignore_index=True)
    df.to_parquet(out_path, index=False)
    logger.info("fills %s: %d rows -> %s", day.isoformat(), total, out_path)
    return out_path


def _daterange(start: date, end: date) -> Iterator[date]:
    cur = start
    while cur <= end:
        yield cur
        cur = cur + timedelta(days=1)


def fills_date_window(
    creds: ProbalyticsCreds,
    *,
    assets: Sequence[str] = ("bitcoin", "ethereum"),
    include_hourly: bool = True,
    include_daily: bool = True,
) -> Tuple[Optional[datetime], Optional[datetime]]:
    """Return (min_ts, max_ts) of fills available for the universe."""
    client = open_clickhouse(creds)
    # No params -> bare execute, no need to escape %.
    where_slug = _slug_predicate(assets, include_hourly=include_hourly, include_daily=include_daily)
    res = client.execute(
        f"""
        SELECT min(f.timestamp), max(f.timestamp)
        FROM fills f INNER JOIN markets m ON f.market_id = m.id
        WHERE m.platform = 'POLYMARKET' AND {where_slug}
        """
    )[0]
    return tuple(res)

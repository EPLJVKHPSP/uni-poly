"""On-demand orderbook fetcher with disk cache.

A backtest only really needs L2 depth at the **timestamps where it actually
trades** — for our hedge that's open + close per LP repositioning, ~100
quotes total over a 7-day window. Sequentially fetching a tight window
around each trade timestamp is fast (each request is ~1-3s for a small
window) and trivially cacheable.

Cache layout (keyed by ``(market_platform_id, window_start_floor)``)::

    data/probalytics/orderbooks_ondemand/
      <market_platform_id>/<YYYYMMDDTHHMMSSZ>_<duration_s>.parquet

The cache key is the **floor of the trade timestamp to the configured
window step** so multiple trades inside the same window reuse one file.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd

from .client import ProbalyticsRest
from .replay import OrderBookReplay, Snapshot, _to_aware_utc

logger = logging.getLogger(__name__)


CACHE_SUBDIR = "orderbooks_ondemand"
DEFAULT_WINDOW_BEFORE = 300   # seconds before trade ts
DEFAULT_WINDOW_AFTER = 300    # seconds after trade ts
EMPTY_PARQUET_BYTES = 768


def _floor_to_window(ts: datetime, window_seconds: int) -> datetime:
    epoch = int(ts.timestamp())
    floored = (epoch // window_seconds) * window_seconds
    return datetime.fromtimestamp(floored, tz=timezone.utc)


def _to_iso_z(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _cache_path(root: str, market_platform_id: str, start: datetime, duration_s: int) -> str:
    fname = start.strftime("%Y%m%dT%H%M%SZ") + f"_{duration_s}.parquet"
    return os.path.join(root, CACHE_SUBDIR, market_platform_id, fname)


class OrderBookFetcher:
    """Pulls and caches small orderbook windows around backtest trade timestamps."""

    def __init__(
        self,
        rest: Optional[ProbalyticsRest],
        root: str,
        *,
        window_before_seconds: int = DEFAULT_WINDOW_BEFORE,
        window_after_seconds: int = DEFAULT_WINDOW_AFTER,
        request_timeout: float = 90.0,
    ):
        self.rest = rest
        self.root = root
        self.before = int(window_before_seconds)
        self.after = int(window_after_seconds)
        self.timeout = float(request_timeout)
        self._duration = self.before + self.after
        self._mem_cache: dict[tuple[str, datetime], Optional[OrderBookReplay]] = {}
        # Stats (visible in build_summary)
        self.cache_hits = 0
        self.cache_misses = 0
        self.empty_responses = 0
        self.errors = 0

    def get(self, market_platform_id: str, ts: datetime) -> Optional[OrderBookReplay]:
        """Return an OrderBookReplay covering ``ts``; None if Probalytics has no book."""
        ts_a = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
        win_start = _floor_to_window(ts_a - timedelta(seconds=self.before), self._duration)
        key = (market_platform_id, win_start)
        if key in self._mem_cache:
            self.cache_hits += 1
            return self._mem_cache[key]

        path = _cache_path(self.root, market_platform_id, win_start, self._duration)
        if os.path.exists(path) and os.path.getsize(path) > 0:
            replay = OrderBookReplay.from_parquet(path) if os.path.getsize(path) > EMPTY_PARQUET_BYTES else None
            self._mem_cache[key] = replay
            self.cache_hits += 1
            return replay

        if self.rest is None:
            # No fetcher configured (e.g. offline mode); record miss and bail.
            self.cache_misses += 1
            self._mem_cache[key] = None
            return None

        # Cache miss: fetch and persist.
        win_end = win_start + timedelta(seconds=self._duration)
        try:
            n = self.rest.download_orderbook(
                market_platform_id,
                _to_iso_z(win_start),
                _to_iso_z(win_end),
                path,
                timeout=self.timeout,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("ondemand book fetch %s @ %s failed: %s", market_platform_id, ts_a, exc)
            self.errors += 1
            self._mem_cache[key] = None
            return None

        self.cache_misses += 1
        if n is None:
            self.errors += 1
            self._mem_cache[key] = None
            return None
        if n <= EMPTY_PARQUET_BYTES:
            self.empty_responses += 1
            try:
                os.remove(path)
            except OSError:
                pass
            self._mem_cache[key] = None
            return None
        replay = OrderBookReplay.from_parquet(path)
        self._mem_cache[key] = replay
        return replay

    def stats(self) -> dict:
        return {
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "empty_responses": self.empty_responses,
            "errors": self.errors,
        }

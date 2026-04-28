"""Load Probalytics orderbook Parquets and serve LOCF lookups + VWAP fills."""

from __future__ import annotations

import bisect
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import pandas as pd

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Per-snapshot helpers
# ---------------------------------------------------------------------------

def _level(level_obj) -> Tuple[float, float]:
    """Normalise a Probalytics level (dict or 2-tuple) to (price, size)."""
    if isinstance(level_obj, dict):
        return float(level_obj["price"]), float(level_obj["size"])
    p, s = level_obj
    return float(p), float(s)


def vwap_on_levels(levels: list, contracts: float) -> Tuple[float, float]:
    """Walk a sorted level list filling ``contracts`` and return (vwap, filled).

    Levels are expected sorted from best to worst (asks ascending, bids
    descending). Returns ``(0.0, 0.0)`` if there's no liquidity.
    """
    n = max(float(contracts or 0.0), 0.0)
    if n <= 0.0 or not levels:
        return 0.0, 0.0
    remaining = n
    notional = 0.0
    filled = 0.0
    for lev in levels:
        p, s = _level(lev)
        if s <= 0.0:
            continue
        take = min(s, remaining)
        notional += p * take
        filled += take
        remaining -= take
        if remaining <= 1e-12:
            break
    if filled <= 0.0:
        return 0.0, 0.0
    return notional / filled, filled


# ---------------------------------------------------------------------------
# Per-market replay
# ---------------------------------------------------------------------------

@dataclass
class Snapshot:
    ts: datetime
    bids: list
    asks: list

    def best_bid(self) -> Optional[float]:
        for lev in self.bids:
            p, s = _level(lev)
            if s > 0.0:
                return p
        return None

    def best_ask(self) -> Optional[float]:
        for lev in self.asks:
            p, s = _level(lev)
            if s > 0.0:
                return p
        return None

    def mid(self) -> Optional[float]:
        b = self.best_bid()
        a = self.best_ask()
        if b is None or a is None:
            return None
        return (a + b) / 2.0


class OrderBookReplay:
    """In-memory replay for one (market, outcome) book.

    Probalytics returns one snapshot per (market, outcome). For our hedge we
    only quote on the YES leg of each touch market, so the helper exposes a
    separate replay per outcome name.
    """

    def __init__(self, snapshots_by_outcome: Dict[str, List[Snapshot]]):
        self._by_outcome: Dict[str, List[Snapshot]] = {
            o: sorted(snaps, key=lambda s: s.ts) for o, snaps in snapshots_by_outcome.items()
        }
        self._ts_index: Dict[str, List[datetime]] = {
            o: [s.ts for s in snaps] for o, snaps in self._by_outcome.items()
        }

    @classmethod
    def from_parquet(cls, path: str) -> "OrderBookReplay":
        df = pd.read_parquet(path)
        if df.empty:
            return cls({})
        df["outcome_name"] = df["outcome"].apply(lambda o: o["name"] if isinstance(o, dict) else o[2])
        by: Dict[str, List[Snapshot]] = {}
        for name, sub in df.groupby("outcome_name", sort=False):
            sub = sub.sort_values("timestamp")
            snaps = [
                Snapshot(ts=_to_aware_utc(r.timestamp), bids=list(r.bids), asks=list(r.asks))
                for r in sub.itertuples(index=False)
            ]
            by[name] = snaps
        return cls(by)

    def outcomes(self) -> List[str]:
        return list(self._by_outcome.keys())

    def snapshot_at(self, ts: datetime, outcome: str = "Yes") -> Optional[Snapshot]:
        """LOCF lookup: returns the most recent snapshot at or before ``ts``."""
        if outcome not in self._by_outcome:
            return None
        snaps = self._by_outcome[outcome]
        index = self._ts_index[outcome]
        ts_a = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
        i = bisect.bisect_right(index, ts_a) - 1
        if i < 0:
            return None
        return snaps[i]

    def best_bid_ask(self, ts: datetime, outcome: str = "Yes") -> Tuple[Optional[float], Optional[float]]:
        snap = self.snapshot_at(ts, outcome)
        if snap is None:
            return (None, None)
        return (snap.best_bid(), snap.best_ask())

    def vwap_buy(self, ts: datetime, contracts: float, outcome: str = "Yes") -> Tuple[Optional[float], float]:
        """Walk the ask side at ``ts`` and return (vwap, filled_contracts)."""
        snap = self.snapshot_at(ts, outcome)
        if snap is None:
            return (None, 0.0)
        vwap, filled = vwap_on_levels(snap.asks, contracts)
        return (vwap if filled > 0.0 else None, filled)

    def vwap_sell(self, ts: datetime, contracts: float, outcome: str = "Yes") -> Tuple[Optional[float], float]:
        snap = self.snapshot_at(ts, outcome)
        if snap is None:
            return (None, 0.0)
        vwap, filled = vwap_on_levels(snap.bids, contracts)
        return (vwap if filled > 0.0 else None, filled)


# ---------------------------------------------------------------------------
# Universe-level loader
# ---------------------------------------------------------------------------

class OrderBookUniverse:
    """Lazy registry: ``platform_id -> OrderBookReplay`` loaded on demand.

    Reads the day-partitioned layout produced by ``books_sync``::

        <root>/orderbooks/<YYYY-MM-DD>/<market_platform_id>.parquet

    For each market we glob across all per-day Parquets and concatenate the
    snapshots. A market with no parquets at all returns ``None``.
    """

    def __init__(self, root: str):
        import glob
        self.root = root
        self.dir = os.path.join(root, "orderbooks")
        self._cache: Dict[str, OrderBookReplay] = {}
        # Pre-build an index: platform_id -> [parquet paths]
        self._index: Dict[str, List[str]] = {}
        if os.path.isdir(self.dir):
            for path in glob.glob(os.path.join(self.dir, "*", "*.parquet")):
                pid = os.path.splitext(os.path.basename(path))[0]
                if pid.startswith("0x"):
                    self._index.setdefault(pid, []).append(path)
            for pid in self._index:
                self._index[pid].sort()

    def has(self, platform_id: str) -> bool:
        return platform_id in self._index

    def get(self, platform_id: str) -> Optional[OrderBookReplay]:
        if platform_id in self._cache:
            return self._cache[platform_id]
        paths = self._index.get(platform_id)
        if not paths:
            return None
        frames = []
        for p in paths:
            if os.path.getsize(p) == 0:
                continue
            frames.append(pd.read_parquet(p))
        if not frames:
            return None
        df = pd.concat(frames, ignore_index=True)
        df["outcome_name"] = df["outcome"].apply(lambda o: o["name"] if isinstance(o, dict) else o[2])
        by: Dict[str, List[Snapshot]] = {}
        for name, sub in df.groupby("outcome_name", sort=False):
            sub = sub.sort_values("timestamp")
            by[name] = [
                Snapshot(ts=_to_aware_utc(r.timestamp), bids=list(r.bids), asks=list(r.asks))
                for r in sub.itertuples(index=False)
            ]
        replay = OrderBookReplay(by)
        self._cache[platform_id] = replay
        return replay

    def loaded_count(self) -> int:
        return len(self._cache)

    def universe_size(self) -> int:
        return len(self._index)


def _to_aware_utc(ts) -> datetime:
    if isinstance(ts, pd.Timestamp):
        ts = ts.to_pydatetime()
    if not isinstance(ts, datetime):
        raise TypeError(f"unexpected ts {type(ts)}")
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)

"""Probalytics sync: ClickHouse + REST clients, Parquet writers, replay helpers.

We pull three things from Probalytics for our BTC/ETH strike universe and
persist them locally so the backtest can run against deterministic data:

1. ``markets.parquet``   — one row per Polymarket market we care about (slug,
   condition_id, outcomes, opened_at, closes_at, resolution).
2. ``fills/{date}.parquet`` — every fill on those markets, one Parquet per UTC
   day. Used for empirical slippage fitting and as a sanity check on books.
3. ``orderbooks/{platform_id}.parquet`` — per-market L2 snapshots interpolated
   to 1ms (LOCF) by Probalytics. The replay helper turns these into a function
   ``best_bid_ask(ts) -> (bid, ask, depth)`` and a VWAP-on-book walk used by
   ``apply_execution_costs``.

Storage layout::

    data/probalytics/
      markets.parquet
      fills/<YYYY-MM-DD>.parquet
      orderbooks/<market_platform_id>.parquet
      _meta/sync_state.json
"""

from __future__ import annotations

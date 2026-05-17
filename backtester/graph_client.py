"""The Graph API client — pool metadata and hourly candle fetching."""

import json
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional

import requests as _requests_mod

# Default = Uniswap V3 on Ethereum mainnet. Override via ``BACKTEST_SUBGRAPH_ID``
# environment variable (set per-process by the harness) when fetching pools on
# Arbitrum, Base, or any other Uniswap V3 deployment.
DEFAULT_SUBGRAPH_ID = "5zvR82QoaXYFyDEKLZ9t6v9adgnptxYpKpSbxtgVENFV"
SUBGRAPH_ID = DEFAULT_SUBGRAPH_ID  # backward-compat name used in tests/imports

# Known Uniswap V3 subgraph IDs on The Graph (queried with the same API key).
KNOWN_SUBGRAPHS: Dict[str, str] = {
    "ethereum": "5zvR82QoaXYFyDEKLZ9t6v9adgnptxYpKpSbxtgVENFV",
    "arbitrum": "FbCGRftH4a3yZugY7TnbYgPJVEv2LvMT6oF1fxPe9aJM",
    "base":     "HMuAwufqZ1YCRmzL2SfHTVkzZovC9VL2UAKhjvRqKiR1",
}


def _requests():
    """Resolve ``requests`` through the shim module so that
    @patch("active_backtester.requests.post") intercepts calls correctly."""
    shim = sys.modules.get("active_backtester")
    if shim and hasattr(shim, "requests"):
        return getattr(shim, "requests")
    return _requests_mod


def _resolve_subgraph_id() -> str:
    # Per-process override: harness sets BACKTEST_SUBGRAPH_ID when the active
    # pool lives on Arbitrum/Base/etc. Falls back to mainnet when unset.
    override = os.environ.get("BACKTEST_SUBGRAPH_ID")
    if override:
        # Support both raw subgraph IDs and chain shortcuts.
        return KNOWN_SUBGRAPHS.get(override.lower(), override)
    return DEFAULT_SUBGRAPH_ID


def _graph_url() -> str:
    api_key = os.getenv("THEGRAPH_API_KEY")
    if not api_key:
        raise RuntimeError("THEGRAPH_API_KEY not set")
    return f"https://gateway.thegraph.com/api/{api_key}/subgraphs/id/{_resolve_subgraph_id()}"


def fetch_pool_metadata(pool_id: str) -> Dict:
    override = os.environ.get("BACKTEST_POOL_METADATA_JSON")
    if override:
        path = Path(override)
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and "data" not in raw:
            return raw
        pools = raw.get("data", {}).get("id", [])
        if pools:
            return pools[0]
        raise RuntimeError(f"Invalid pool metadata JSON: {path}")

    # Single-pool lookup: omit ``orderBy`` so the query is portable across
    # chain-specific Uniswap V3 subgraphs (the Base subgraph rejects the
    # mainnet-only ``totalValueLockedETH`` field). The where-clause-by-id
    # already returns at most one row.
    query = """query Pools($id: ID!) {
        id: pools(where: { id: $id }) {
            id feeTier
            token0 { id symbol name decimals }
            token1 { id symbol name decimals }
        }
    }"""
    resp = _requests().post(
        _graph_url(),
        json={"query": query, "variables": {"id": pool_id}},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    pools = data.get("data", {}).get("id", [])
    if not pools:
        raise RuntimeError(f"Pool {pool_id} not found on The Graph")
    return pools[0]


def fetch_hourly_candles(pool_id: str, start_ts: int, end_ts: int) -> List[Dict]:
    """Fetch all hourly candles, paginating in 30-day windows (max 1000 rows)."""
    override = os.environ.get("BACKTEST_CANDLES_JSON")
    if override:
        path = Path(override)
        candles = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(candles, list):
            raise RuntimeError(f"BACKTEST_CANDLES_JSON must be a JSON array: {path}")
        return candles

    query = """query PoolHourDatas($pool: ID!, $fromdate: Int!, $todate: Int!) {
        poolHourDatas(
            where: { pool: $pool, periodStartUnix_gt: $fromdate, periodStartUnix_lt: $todate, close_gt: 0 }
            orderBy: periodStartUnix, orderDirection: asc, first: 1000
        ) {
            periodStartUnix liquidity high low close
            feeGrowthGlobal0X128 feeGrowthGlobal1X128
            pool {
                totalValueLockedUSD totalValueLockedToken0 totalValueLockedToken1
                token0 { decimals } token1 { decimals }
            }
        }
    }"""
    url = _graph_url()
    all_candles: List[Dict] = []
    batch_start = start_ts

    while batch_start < end_ts:
        batch_end = min(batch_start + 86400 * 30, end_ts)
        resp = _requests().post(
            url,
            json={
                "query": query,
                "variables": {"pool": pool_id, "fromdate": batch_start, "todate": batch_end},
            },
            timeout=30,
        )
        resp.raise_for_status()
        candles = resp.json().get("data", {}).get("poolHourDatas", [])
        if candles:
            all_candles.extend(candles)
            last_ts = int(candles[-1]["periodStartUnix"])
            batch_start = max(last_ts + 1, batch_end)
        else:
            batch_start = batch_end

    all_candles.sort(key=lambda c: int(c["periodStartUnix"]))
    return all_candles

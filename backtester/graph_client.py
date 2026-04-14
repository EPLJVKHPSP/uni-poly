"""The Graph API client — pool metadata and hourly candle fetching."""

import os
import sys
from typing import Dict, List

import requests as _requests_mod

SUBGRAPH_ID = "5zvR82QoaXYFyDEKLZ9t6v9adgnptxYpKpSbxtgVENFV"


def _requests():
    """Resolve ``requests`` through the shim module so that
    @patch("active_backtester.requests.post") intercepts calls correctly."""
    shim = sys.modules.get("active_backtester")
    if shim and hasattr(shim, "requests"):
        return getattr(shim, "requests")
    return _requests_mod


def _graph_url() -> str:
    api_key = os.getenv("THEGRAPH_API_KEY")
    if not api_key:
        raise RuntimeError("THEGRAPH_API_KEY not set")
    return f"https://gateway.thegraph.com/api/{api_key}/subgraphs/id/{SUBGRAPH_ID}"


def fetch_pool_metadata(pool_id: str) -> Dict:
    query = """query Pools($id: ID!) {
        id: pools(where: { id: $id } orderBy: totalValueLockedETH, orderDirection: desc) {
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

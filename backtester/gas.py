"""Historical gas prices via free public Ethereum RPC and gas cost helpers."""

import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Dict

import requests as _requests_mod

logger = logging.getLogger(__name__)

GAS_MINT = 430_000
GAS_BURN_COLLECT = 250_000
GAS_SWAP = 150_000

_DEFAULT_RPC = "https://eth.llamarpc.com"
_BLOCK_TIME_SECS = 12
_SAMPLES_PER_DAY = 4


def _rpc_call(method: str, params: list, rpc_url: str = _DEFAULT_RPC) -> dict:
    resp = _requests_mod.post(
        rpc_url,
        json={"jsonrpc": "2.0", "method": method, "params": params, "id": 1},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(data["error"])
    return data.get("result", {})


def fetch_daily_gas_prices(start_date: str, end_date: str) -> Dict[str, int]:
    """Sample baseFeePerGas from Ethereum blocks to build a daily gas price map.

    Fetches a few blocks per day via a free public RPC and averages the
    baseFeePerGas.  No API key required.

    Args:
        start_date: YYYY-MM-DD
        end_date:   YYYY-MM-DD

    Returns:
        Mapping of ``YYYY-MM-DD`` -> average ``baseFeePerGas`` in Wei (int).
        Empty dict on failure.
    """
    try:
        head = _rpc_call("eth_getBlockByNumber", ["latest", False])
        head_number = int(head["number"], 16)
        head_ts = int(head["timestamp"], 16)
    except Exception as exc:
        logger.warning("RPC head block fetch failed: %s — gas fees will be 0", exc)
        return {}

    start_dt = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    total_days = (end_dt - start_dt).days + 1
    if total_days <= 0:
        return {}

    gas_map: Dict[str, int] = {}
    fetched = 0
    errors = 0

    for day_offset in range(total_days):
        day_dt = start_dt + timedelta(days=day_offset)
        date_str = day_dt.strftime("%Y-%m-%d")

        day_fees = []
        for sample in range(_SAMPLES_PER_DAY):
            sample_ts = int(day_dt.timestamp()) + sample * (86400 // _SAMPLES_PER_DAY)
            est_block = head_number - (head_ts - sample_ts) // _BLOCK_TIME_SECS
            if est_block < 1:
                continue

            block_hex = hex(est_block)
            try:
                blk = _rpc_call("eth_getBlockByNumber", [block_hex, False])
                base_fee = blk.get("baseFeePerGas")
                if base_fee is not None:
                    day_fees.append(int(base_fee, 16))
            except Exception:
                errors += 1
                continue
            time.sleep(0.05)

        if day_fees:
            gas_map[date_str] = sum(day_fees) // len(day_fees)
            fetched += 1

    logger.info(
        "Loaded %d/%d days of gas prices via RPC sampling (%d block errors)",
        fetched, total_days, errors,
    )
    return gas_map


def gas_cost_usd(
    gas_units: int,
    ts: int,
    eth_price: float,
    gas_prices: Dict[str, int],
) -> float:
    """Compute the USD cost of a transaction given its gas units.

    Looks up the daily average gas price (in Wei) for the date of *ts*.
    Returns 0.0 when no gas data is available for that date.
    """
    if not gas_prices:
        return 0.0

    date_str = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
    avg_wei = gas_prices.get(date_str)
    if avg_wei is None:
        return 0.0

    gas_cost_eth = gas_units * avg_wei * 1e-18
    return gas_cost_eth * eth_price

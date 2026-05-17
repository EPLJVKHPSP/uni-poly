"""Historical gas prices via free public Ethereum RPC and gas cost helpers."""

import json
import logging
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict

import requests as _requests_mod

logger = logging.getLogger(__name__)

GAS_MINT = 430_000
GAS_BURN_COLLECT = 250_000
GAS_SWAP = 150_000

_DEFAULT_RPC = os.getenv("ETH_RPC_URL", "https://ethereum.publicnode.com")
_BLOCK_TIME_SECS = 12
_SAMPLES_PER_DAY = 4


def _rpc_call(method: str, params: list, rpc_url: str = _DEFAULT_RPC) -> dict:
    payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": 1}
    last_exc: Exception = RuntimeError("RPC call failed")
    for attempt in range(5):
        try:
            resp = _requests_mod.post(rpc_url, json=payload, timeout=15)
            if resp.status_code in (429, 500, 502, 503, 504):
                # brief exponential backoff for overloaded public RPCs
                time.sleep(0.3 * (2 ** attempt))
                continue
            resp.raise_for_status()
            data = resp.json()
            if "error" in data:
                raise RuntimeError(data["error"])
            return data.get("result", {})
        except Exception as exc:
            last_exc = exc
            time.sleep(0.2 * (2 ** attempt))
            continue
    raise last_exc


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
    override = os.environ.get("BACKTEST_GAS_PRICES_JSON")
    if override:
        path = Path(override)
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise RuntimeError(f"BACKTEST_GAS_PRICES_JSON must be a JSON object: {path}")
        return {str(k): int(v) for k, v in raw.items() if not str(k).startswith("__")}

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
    priority_fee_gwei: float = 0.0,
    strict: bool = False,
) -> float:
    """Compute the USD cost of a transaction given its gas units.

    - Looks up the daily average ``baseFeePerGas`` (in Wei) for the date of ``ts``.
    - Adds ``priority_fee_gwei`` as a constant tip on top of base fee
      (default 0 to preserve legacy unit-test behaviour).
    - When data is missing:
        * ``strict=False``  → return 0.0 (legacy, optimistic)
        * ``strict=True``   → raise RuntimeError (no silent under-counting)
    """
    date_str = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")

    # Allow callers to configure tip + strict-mode by stashing them in the
    # gas_prices mapping under reserved keys (so we don't have to thread two
    # extra params through every call site).
    if isinstance(gas_prices, dict):
        if priority_fee_gwei == 0.0 and "__priority_fee_gwei__" in gas_prices:
            try:
                priority_fee_gwei = float(gas_prices["__priority_fee_gwei__"])
            except (TypeError, ValueError):
                priority_fee_gwei = 0.0
        if not strict and gas_prices.get("__strict__") is True:
            strict = True

    if not gas_prices or all(str(k).startswith("__") for k in gas_prices):
        if strict:
            raise RuntimeError(
                f"gas_cost_usd: no gas-price data available for {date_str}; "
                "set ETH_RPC_URL or relax `gas_strict` to allow $0 fallback."
            )
        return 0.0

    avg_wei = gas_prices.get(date_str)
    if avg_wei is None:
        if strict:
            raise RuntimeError(
                f"gas_cost_usd: missing gas-price data for {date_str}; "
                "fix the RPC source or relax `gas_strict` to allow $0 fallback."
            )
        return 0.0

    tip_wei = int(priority_fee_gwei * 1_000_000_000)
    gas_cost_eth = gas_units * (avg_wei + tip_wei) * 1e-18
    return gas_cost_eth * eth_price

"""Position open/close lifecycle."""

import sys
from typing import Dict, Optional, Tuple

from il import calculate_il_at_price

from .fee_math import (
    _tokens_for_strategy_human,
    _tokens_for_strategy_scaled,
    _liquidity_for_strategy,
)
from .gas import GAS_MINT, GAS_SWAP, GAS_BURN_COLLECT, gas_cost_usd


def _get_db_func(name):
    """Resolve db_utils function via the shim module so that
    @patch("active_backtester.<name>") intercepts calls correctly."""
    shim = sys.modules.get("active_backtester")
    if shim and hasattr(shim, name):
        return getattr(shim, name)
    import db_utils
    return getattr(db_utils, name)


def open_position(
    candle: Dict,
    pool_data: Dict,
    min_range: float,
    max_range: float,
    wallet: Dict,
    insurance_info: Dict,
    price_token: int = 0,
    gas_prices: Optional[Dict[str, int]] = None,
    spread: float = 0.0,
) -> Dict:
    """Open a position using the full wallet. Returns position record."""
    close_price = float(candle["close"])
    entry_price = close_price if price_token == 0 else 1.0 / close_price
    ts = int(candle["periodStartUnix"])

    dec0 = int(pool_data["token0"]["decimals"])
    dec1 = int(pool_data["token1"]["decimals"])
    decimal_diff = dec1 - dec0

    wallet_value = wallet["usdc"] + wallet["eth"] * entry_price
    swap_fee_rate = int(pool_data.get("feeTier", 0)) / 1_000_000
    gas_open = gas_cost_usd(GAS_MINT + GAS_SWAP, ts, entry_price, gas_prices or {})

    deposit_value = wallet_value
    lower_mid = insurance_info["lower_bet_price"]
    upper_mid = insurance_info["upper_bet_price"]
    lower_ask = min(lower_mid + spread / 2, 1.0)
    upper_ask = min(upper_mid + spread / 2, 1.0)

    for _ in range(5):
        t0, t1 = _tokens_for_strategy_human(min_range, max_range, deposit_value, entry_price)
        il_lower = calculate_il_at_price(entry_price, t0, t1, min_range, min_range, max_range)
        il_upper = calculate_il_at_price(entry_price, t0, t1, max_range, min_range, max_range)

        lower_contracts = abs(min(0, il_lower["IL"]))
        upper_contracts = abs(min(0, il_upper["IL"]))

        lower_cost = lower_contracts * lower_ask
        upper_cost = upper_contracts * upper_ask
        total_insurance_cost = lower_cost + upper_cost

        remaining = wallet_value - total_insurance_cost
        if remaining <= 0:
            return None

        needed_usdc, _ = _tokens_for_strategy_human(min_range, max_range, remaining, entry_price)
        wallet_usdc_after_ins = wallet["usdc"] - total_insurance_cost
        swap_amount_usd = abs(wallet_usdc_after_ins - needed_usdc)
        swap_fee = swap_amount_usd * swap_fee_rate

        deposit_value = remaining - swap_fee - gas_open
        if deposit_value <= 0:
            return None

    spread_cost_buy = (lower_ask - lower_mid) * lower_contracts + (upper_ask - upper_mid) * upper_contracts

    token0_dep, token1_dep = _tokens_for_strategy_human(
        min_range, max_range, deposit_value, entry_price,
    )

    amt0_scaled, amt1_scaled = _tokens_for_strategy_scaled(
        min_range, max_range, deposit_value, entry_price, decimal_diff,
    )
    liquidity = _liquidity_for_strategy(
        entry_price, min_range, max_range, amt0_scaled, amt1_scaled, dec0, dec1,
    )

    return {
        "open_ts": ts,
        "entry_price": entry_price,
        "min_range": min_range,
        "max_range": max_range,
        "wallet_before": {"usdc": wallet["usdc"], "eth": wallet["eth"], "value_usd": wallet_value},
        "deposit_value": deposit_value,
        "token0_dep": token0_dep,
        "token1_dep": token1_dep,
        "liquidity": liquidity,
        "dec0": dec0,
        "dec1": dec1,
        "lower_bet_price": insurance_info["lower_bet_price"],
        "upper_bet_price": insurance_info["upper_bet_price"],
        "lower_contracts": lower_contracts,
        "upper_contracts": upper_contracts,
        "lower_insurance_cost_usdc": lower_cost,
        "upper_insurance_cost_usdc": upper_cost,
        "insurance_cost": total_insurance_cost,
        "swap_fee": swap_fee,
        "swap_amount": swap_amount_usd,
        "gas_fee_open": gas_open,
        "spread_cost_buy": spread_cost_buy,
        "accumulated_fees_usdc": 0.0,
        "accumulated_fees_eth": 0.0,
        "candle_count": 0,
    }


def close_position(
    pos: Dict,
    close_candle: Dict,
    touched_lower: bool,
    touched_upper: bool,
    price_token: int = 0,
    token_symbol: str = "ETH",
    conn=None,
    gas_prices: Optional[Dict[str, int]] = None,
    spread: float = 0.0,
) -> Tuple[Dict, Dict]:
    """Settle a position. Returns (pos, new_wallet)."""
    if touched_lower:
        touch_price = pos["min_range"]
    elif touched_upper:
        touch_price = pos["max_range"]
    else:
        cp = float(close_candle["close"])
        touch_price = cp if price_token == 0 else 1.0 / cp

    il = calculate_il_at_price(
        pos["entry_price"], pos["token0_dep"], pos["token1_dep"],
        touch_price, pos["min_range"], pos["max_range"],
    )

    close_ts = int(close_candle["periodStartUnix"])

    payout = 0.0
    insurance_sellback = 0.0
    if touched_lower:
        payout += pos["lower_contracts"]
    if touched_upper:
        payout += pos["upper_contracts"]

    get_clob_token_id = _get_db_func("get_clob_token_id")
    get_historical_bet_price = _get_db_func("get_historical_bet_price")

    spread_cost_sell = 0.0

    if not touched_lower and pos["lower_contracts"] > 0 and conn is not None:
        lower_clob = get_clob_token_id(token_symbol, pos["min_range"], "down", "Yes", conn)
        if lower_clob:
            mid_price = get_historical_bet_price(lower_clob, close_ts, conn)
            if mid_price is not None:
                bid_price = max(mid_price - spread / 2, 0.0)
                insurance_sellback += pos["lower_contracts"] * bid_price
                spread_cost_sell += pos["lower_contracts"] * (mid_price - bid_price)

    if not touched_upper and pos["upper_contracts"] > 0 and conn is not None:
        upper_clob = get_clob_token_id(token_symbol, pos["max_range"], "up", "Yes", conn)
        if upper_clob:
            mid_price = get_historical_bet_price(upper_clob, close_ts, conn)
            if mid_price is not None:
                bid_price = max(mid_price - spread / 2, 0.0)
                insurance_sellback += pos["upper_contracts"] * bid_price
                spread_cost_sell += pos["upper_contracts"] * (mid_price - bid_price)

    if touch_price <= pos["min_range"]:
        wd_usdc = 0.0
        wd_eth = pos["deposit_value"] / pos["min_range"] if pos["min_range"] else 0.0
    elif touch_price >= pos["max_range"]:
        wd_usdc = pos["deposit_value"]
        wd_eth = 0.0
    else:
        wd_usdc, wd_eth = _tokens_for_strategy_human(
            pos["min_range"], pos["max_range"], pos["deposit_value"], touch_price,
        )

    fees_usdc = pos["accumulated_fees_usdc"]
    fees_eth = pos["accumulated_fees_eth"]
    fees_total_usd = fees_usdc + fees_eth * touch_price

    gas_close = gas_cost_usd(GAS_BURN_COLLECT, close_ts, touch_price, gas_prices or {})

    new_wallet_usdc = wd_usdc + fees_usdc + payout + insurance_sellback - gas_close
    new_wallet_eth = wd_eth + fees_eth
    new_wallet_value = new_wallet_usdc + new_wallet_eth * touch_price

    pos["close_ts"] = close_ts
    pos["close_price"] = touch_price
    pos["touched_lower"] = touched_lower
    pos["touched_upper"] = touched_upper
    pos["wd_usdc"] = wd_usdc
    pos["wd_eth"] = wd_eth
    pos["il"] = il["IL"]
    pos["il_pct"] = il["IL_pct"]
    pos["insurance_payout"] = payout
    pos["insurance_sellback"] = insurance_sellback
    pos["insurance_net"] = payout + insurance_sellback - pos["insurance_cost"]
    pos["fees_earned_usdc"] = fees_usdc
    pos["fees_earned_eth"] = fees_eth
    pos["fees_earned_usd"] = fees_total_usd
    pos["gas_fee_close"] = gas_close
    pos["spread_cost_sell"] = spread_cost_sell
    pos["wallet_after"] = {"usdc": new_wallet_usdc, "eth": new_wallet_eth, "value_usd": new_wallet_value}
    pos["duration_hours"] = (pos["close_ts"] - pos["open_ts"]) / 3600

    return pos, {"usdc": new_wallet_usdc, "eth": new_wallet_eth}

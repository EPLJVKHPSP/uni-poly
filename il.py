"""Impermanent loss calculation for Uniswap V3 concentrated liquidity positions."""

import math


def tokens_from_liquidity(price, price_low, price_high, liquidity):
    """Compute LP token amounts at any price given liquidity and range bounds."""
    sp = math.sqrt(price)
    sl = math.sqrt(price_low)
    sh = math.sqrt(price_high)

    if sp <= sl:
        amount1 = liquidity * (sh - sl) / (sl * sh)
        return 0, amount1

    if sp < sh:
        amount0 = liquidity * (sp - sl)
        amount1 = liquidity * (sh - sp) / (sp * sh)
        return amount0, amount1

    amount0 = liquidity * (sh - sl)
    return amount0, 0


def calculate_il_at_price(
    entry_price,
    token0_initial,
    token1_initial,
    target_price,
    min_range,
    max_range,
):
    """
    Calculate impermanent loss at a specific price compared to HODL.

    Returns dict with LP_value, HODL_value, IL (negative = loss), IL_pct.
    """
    sp0 = math.sqrt(entry_price)
    sl = math.sqrt(min_range)
    sh = math.sqrt(max_range)

    L0 = token0_initial / (sp0 - sl) if (sp0 - sl) > 0 else 0
    L1 = token1_initial * (sp0 * sh) / (sh - sp0) if (sh - sp0) > 0 else 0
    L = (L0 + L1) / 2

    t0_LP, t1_LP = tokens_from_liquidity(target_price, min_range, max_range, L)

    lp_value = t0_LP + t1_LP * target_price
    hodl_value = token0_initial + token1_initial * target_price

    il = lp_value - hodl_value
    il_pct = (il / hodl_value * 100.0) if hodl_value > 0 else 0.0

    return {
        "LP_value": lp_value,
        "HODL_value": hodl_value,
        "IL": il,
        "IL_pct": il_pct,
    }

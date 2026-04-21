"""Impermanent loss calculation for Uniswap V3 concentrated liquidity positions.

All math in this module is in *human units* (USDC as float USDC, ETH as float
ETH) using the project's convention ``price = USD per ETH`` (so the amount
formulas look "inverted" vs canonical Uniswap docs, which use
``price = token1 / token0``). That asymmetry is intentional and preserved
across ``fee_math._tokens_for_strategy_human`` and this module.
"""

import math


def tokens_from_liquidity(price, price_low, price_high, liquidity):
    """LP token amounts at ``price`` given ``liquidity`` and range bounds.

    Returns ``(amount0_USDC, amount1_ETH)`` under the module's USDC/ETH
    convention (``price`` in USD per ETH).
    """
    sp = math.sqrt(price)
    sl = math.sqrt(price_low)
    sh = math.sqrt(price_high)

    if sp <= sl:
        amount1 = liquidity * (sh - sl) / (sl * sh)
        return 0.0, amount1

    if sp < sh:
        amount0 = liquidity * (sp - sl)
        amount1 = liquidity * (sh - sp) / (sp * sh)
        return amount0, amount1

    amount0 = liquidity * (sh - sl)
    return amount0, 0.0


def liquidity_from_tokens(entry_price, token0, token1, min_range, max_range):
    """Recover ``L`` (human-unit liquidity) from a token split at ``entry_price``.

    Both formulas ``L = token0 / (sp - sl)`` and ``L = token1 * sp*sh / (sh - sp)``
    should agree when ``(token0, token1)`` comes from a consistent V3 split at
    ``entry_price``. If only one side is nonzero (boundary entry), use it. If
    both are nonzero and they disagree materially (>1%), we take ``min(L0, L1)``
    to match Uniswap's own ``liquidityForStrategy`` (the extra tokens would sit
    idle on-chain).

    Returns the scalar ``L`` in human units, or 0.0 when both sides are zero
    (degenerate position).
    """
    sp = math.sqrt(entry_price)
    sl = math.sqrt(min_range)
    sh = math.sqrt(max_range)

    l0 = token0 / (sp - sl) if (sp - sl) > 0 and token0 > 0 else 0.0
    l1 = (token1 * sp * sh) / (sh - sp) if (sh - sp) > 0 and token1 > 0 else 0.0

    if l0 > 0 and l1 > 0:
        return min(l0, l1)
    return l0 + l1


def calculate_il_at_price(
    entry_price,
    token0_initial,
    token1_initial,
    target_price,
    min_range,
    max_range,
):
    """Impermanent loss at ``target_price`` vs HODL of the entry split.

    Returns a dict with ``LP_value``, ``HODL_value``, ``IL`` (negative = loss)
    and ``IL_pct``.
    """
    liquidity = liquidity_from_tokens(
        entry_price, token0_initial, token1_initial, min_range, max_range,
    )

    t0_lp, t1_lp = tokens_from_liquidity(target_price, min_range, max_range, liquidity)

    lp_value = t0_lp + t1_lp * target_price
    hodl_value = token0_initial + token1_initial * target_price

    il = lp_value - hodl_value
    il_pct = (il / hodl_value * 100.0) if hodl_value > 0 else 0.0

    return {
        "LP_value": lp_value,
        "HODL_value": hodl_value,
        "IL": il,
        "IL_pct": il_pct,
    }

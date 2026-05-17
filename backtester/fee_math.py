"""Uniswap V3 fee calculations — pure math, zero I/O."""

import math
from typing import Dict, Optional, Tuple


def _log_base(y: float, x: float) -> float:
    return math.log(y) / math.log(x)


def _get_tick_from_price(price: float, dec0: int, dec1: int, base_selected: int = 0) -> int:
    d0 = dec1 if base_selected == 1 else dec0
    d1 = dec0 if base_selected == 1 else dec1
    val = float(price) * (10 ** (d0 - d1))
    if val <= 0:
        return 0
    return round(_log_base(val, 1.0001))


def _active_liquidity_for_candle(min_tick: int, max_tick: int, low_tick: int, high_tick: int) -> float:
    divider = (high_tick - low_tick) if (high_tick - low_tick) != 0 else 1
    ratio_true = (min(max_tick, high_tick) - max(min_tick, low_tick)) / divider if (high_tick - low_tick) != 0 else 1.0
    ratio = ratio_true * 100.0 if high_tick > min_tick and low_tick < max_tick else 0.0
    return ratio if ratio and not math.isnan(ratio) else 0.0


def _calc_unbounded_fees(
    fg0: str, prev_fg0: str, fg1: str, prev_fg1: str, dec0: int, dec1: int
) -> Tuple[float, float]:
    f0 = (int(fg0) / (2**128)) / (10**dec0) - (int(prev_fg0) / (2**128)) / (10**dec0)
    f1 = (int(fg1) / (2**128)) / (10**dec1) - (int(prev_fg1) / (2**128)) / (10**dec1)
    return f0, f1


def _tokens_for_strategy_scaled(min_r: float, max_r: float, investment: float, price: float, decimal_diff: int) -> Tuple[float, float]:
    """Token split at entry with decimal scaling. Matches JS tokensForStrategy exactly.
    Used for computing *liquidity* (fee math needs scaled values)."""
    sqrt_price = math.sqrt(price * (10**decimal_diff))
    sqrt_low = math.sqrt(min_r * (10**decimal_diff))
    sqrt_high = math.sqrt(max_r * (10**decimal_diff))

    if sqrt_price > sqrt_low and sqrt_price < sqrt_high:
        delta = investment / ((sqrt_price - sqrt_low) + ((1 / sqrt_price - 1 / sqrt_high) * price * (10**decimal_diff)))
        amount1 = delta * (sqrt_price - sqrt_low)
        amount0 = delta * (1 / sqrt_price - 1 / sqrt_high) * (10**decimal_diff)
    elif sqrt_price <= sqrt_low:
        delta = investment / (((1 / sqrt_low - 1 / sqrt_high) * price))
        amount1 = 0.0
        amount0 = delta * (1 / sqrt_low - 1 / sqrt_high)
    else:
        delta = investment / (sqrt_high - sqrt_low)
        amount1 = delta * (sqrt_high - sqrt_low)
        amount0 = 0.0
    return amount0, amount1


def _tokens_for_strategy_human(min_r: float, max_r: float, investment: float, price: float) -> Tuple[float, float]:
    """Token split in human-readable units (no decimal scaling).
    token0 = stablecoin amount (e.g. USDC), token1 = volatile asset (e.g. ETH).
    Used for IL calculation and insurance sizing."""
    sp = math.sqrt(price)
    sl = math.sqrt(min_r)
    sh = math.sqrt(max_r)

    if sp > sl and sp < sh:
        delta = investment / ((sp - sl) + ((1 / sp - 1 / sh) * price))
        amount0 = delta * (sp - sl)
        amount1 = delta * (1 / sp - 1 / sh)
    elif sp <= sl:
        delta = investment / (((1 / sl - 1 / sh) * price))
        amount0 = 0.0
        amount1 = delta * (1 / sl - 1 / sh)
    else:
        delta = investment / (sh - sl)
        amount0 = delta * (sh - sl)
        amount1 = 0.0
    return amount0, amount1


def _liquidity_for_strategy(
    price: float, low: float, high: float,
    tokens0: float, tokens1: float,
    dec0: int, dec1: int,
) -> float:
    """Matches JS liquidityForStrategy."""
    decimal = dec1 - dec0
    s_low_raw = math.sqrt(low * (10**decimal)) * (2**96)
    s_high_raw = math.sqrt(high * (10**decimal)) * (2**96)
    s_low = min(s_low_raw, s_high_raw)
    s_high = max(s_low_raw, s_high_raw)
    s_price = math.sqrt(price * (10**decimal)) * (2**96)

    if s_price <= s_low:
        denom = ((2**96) * (s_high - s_low) / s_high / s_low) / (10**dec0)
        return tokens0 / denom if denom else 0.0
    elif s_price <= s_high:
        denom0 = ((2**96) * (s_high - s_price) / s_high / s_price) / (10**dec0)
        denom1 = (s_price - s_low) / (2**96) / (10**dec1)
        liq0 = tokens0 / denom0 if denom0 else 0.0
        liq1 = tokens1 / denom1 if denom1 else 0.0
        return min(liq0, liq1)
    else:
        denom = (s_high - s_low) / (2**96) / (10**dec1)
        return tokens1 / denom if denom else 0.0


def _tokens_from_liquidity_v3(
    price: float, low: float, high: float, liquidity: float, dec0: int, dec1: int,
) -> Tuple[float, float]:
    """Matches JS tokensFromLiquidity (with decimal scaling)."""
    decimal = dec1 - dec0
    s_low_raw = math.sqrt(low * (10**decimal)) * (2**96)
    s_high_raw = math.sqrt(high * (10**decimal)) * (2**96)
    s_low = min(s_low_raw, s_high_raw)
    s_high = max(s_low_raw, s_high_raw)
    s_price = math.sqrt(price * (10**decimal)) * (2**96)

    if s_price <= s_low:
        amount1 = (liquidity * (2**96) * (s_high - s_low) / s_high / s_low) / (10**dec0)
        return 0.0, amount1
    elif s_price < s_high:
        amount0 = liquidity * (s_price - s_low) / (2**96) / (10**dec1)
        amount1 = (liquidity * (2**96) * (s_high - s_price) / s_high / s_price) / (10**dec0)
        return amount0, amount1
    else:
        amount0 = liquidity * (s_high - s_low) / (2**96) / (10**dec1)
        return amount0, 0.0


def compute_hourly_fee_split(
    candle: Dict,
    prev_candle: Dict,
    liquidity: float,
    min_range: float,
    max_range: float,
    dec0: int,
    dec1: int,
    price_token: int = 0,
    pool_active_liquidity: Optional[float] = None,
) -> Tuple[float, float]:
    """
    Compute fees earned in a single hourly candle, returned as
    (fee_usdc, fee_eth) — in-kind token quantities, not collapsed to USD.

    Pool-dilution adjustment
    ------------------------
    The Graph's ``feeGrowthGlobal`` deltas reflect the *historical* per-unit-
    of-liquidity fee growth — i.e. they were generated by the pool's actual
    liquidity ``L_pool`` at the time, which did NOT include our hypothetical
    deposit. If we actually joined the pool with ``L_us``, the per-LP fee rate
    would shrink because the same swap volume is split across more L:

        realistic_fg = total_pool_fees / (L_pool + L_us)
                     = fg_historical * L_pool / (L_pool + L_us)

    so our realistic fees become

        our_fees = L_us * realistic_fg
                 = L_us * fg_historical * L_pool / (L_pool + L_us)

    Pass ``pool_active_liquidity`` (the candle's current-tick L from the V3
    contract, available as ``candle["liquidity"]``) to apply this correction.
    When ``None`` or non-positive the function falls back to the legacy
    "infinitesimal LP" assumption (no dilution); existing tests/calls keep
    their old behaviour.
    """
    fg0, fg1 = _calc_unbounded_fees(
        candle["feeGrowthGlobal0X128"], prev_candle["feeGrowthGlobal0X128"],
        candle["feeGrowthGlobal1X128"], prev_candle["feeGrowthGlobal1X128"],
        dec0, dec1,
    )

    c_low = float(candle["low"]) if price_token == 0 else (1.0 / float(candle["low"]) if float(candle["low"]) else 1.0)
    c_high = float(candle["high"]) if price_token == 0 else (1.0 / float(candle["high"]) if float(candle["high"]) else 1.0)

    low_tick = _get_tick_from_price(c_low, dec0, dec1, price_token)
    high_tick = _get_tick_from_price(c_high, dec0, dec1, price_token)
    min_tick = _get_tick_from_price(min_range, dec0, dec1, price_token)
    max_tick = _get_tick_from_price(max_range, dec0, dec1, price_token)

    active_liq = _active_liquidity_for_candle(min_tick, max_tick, low_tick, high_tick)

    if (
        pool_active_liquidity is not None
        and pool_active_liquidity > 0
        and liquidity > 0
    ):
        dilution = pool_active_liquidity / (pool_active_liquidity + liquidity)
    else:
        dilution = 1.0

    fee_token0_raw = fg0 * liquidity * active_liq / 100.0 * dilution
    fee_token1_raw = fg1 * liquidity * active_liq / 100.0 * dilution

    close_price = float(candle["close"])
    pool_info = candle["pool"]
    tvl_usd = float(pool_info["totalValueLockedUSD"])
    tvl_t0 = float(pool_info["totalValueLockedToken0"])
    tvl_t1 = float(pool_info["totalValueLockedToken1"])

    if price_token == 0:
        combined_raw = fee_token0_raw + fee_token1_raw * close_price
        denom = tvl_t1 * close_price + tvl_t0
    else:
        combined_raw = (fee_token0_raw / close_price + fee_token1_raw) if close_price else 0.0
        denom = (tvl_t1 + tvl_t0 / close_price) if close_price else 1.0

    scale = tvl_usd / denom if denom else 0.0
    fee_usd_total = combined_raw * scale
    if fee_usd_total <= 0 or close_price <= 0:
        return 0.0, 0.0

    if price_token == 0:
        t0_usd = fee_token0_raw * scale
        t1_usd = fee_token1_raw * close_price * scale
    else:
        t0_usd = (fee_token0_raw / close_price) * scale if close_price else 0.0
        t1_usd = fee_token1_raw * scale

    total_raw_usd = t0_usd + t1_usd
    if total_raw_usd <= 0:
        return 0.0, 0.0

    # The simulator's wallet bookkeeping assumes the first return value is
    # the *stable* (USDC-side) leg and the second is the *volatile* leg in
    # native units. Which on-chain token is the stable side depends on the
    # pool's token order:
    #   price_token == 0 -> token0 is the stable / token1 is the volatile;
    #                        volatile native price = close_price.
    #   price_token == 1 -> token1 is the stable / token0 is the volatile;
    #                        volatile native price = 1 / close_price.
    if price_token == 0:
        stable_share, volatile_share = t0_usd, t1_usd
        volatile_native_price = close_price
    else:
        stable_share, volatile_share = t1_usd, t0_usd
        volatile_native_price = (1.0 / close_price) if close_price else 0.0

    fee_usdc = fee_usd_total * (stable_share / total_raw_usd)
    if volatile_native_price > 0:
        fee_eth = (fee_usd_total * (volatile_share / total_raw_usd)) / volatile_native_price
    else:
        fee_eth = 0.0

    return fee_usdc, fee_eth

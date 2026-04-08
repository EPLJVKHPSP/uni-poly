import json
import math

# === Helper: compute LP token amounts at any price ===
def tokens_from_liquidity(price, price_low, price_high, liquidity):
    sp = math.sqrt(price)
    sl = math.sqrt(price_low)
    sh = math.sqrt(price_high)

    # Case 1 — price below range: all WETH
    if sp <= sl:
        amount1 = liquidity * (sh - sl) / (sl * sh)
        return 0, amount1

    # Case 2 — price inside range: mix of token0/token1
    if sp < sh:
        amount0 = liquidity * (sp - sl)
        amount1 = liquidity * (sh - sp) / (sp * sh)
        return amount0, amount1

    # Case 3 — above range: all USDC
    amount0 = liquidity * (sh - sl)
    return amount0, 0


def calculate_il_at_price(
    entry_price,
    token0_initial,
    token1_initial,
    target_price,
    min_range,
    max_range
):
    """
    Calculate impermanent loss at a specific price compared to HODL.
    
    Args:
        entry_price: Entry price when position was opened
        token0_initial: Initial amount of token0
        token1_initial: Initial amount of token1
        target_price: Price to calculate IL at
        min_range: Lower bound of LP range
        max_range: Upper bound of LP range
        
    Returns:
        dict: {
            "LP_value": float,
            "HODL_value": float,
            "IL": float,  # LP_value - HODL_value (negative = loss)
            "IL_pct": float
        }
    """
    sp0 = math.sqrt(entry_price)
    sl = math.sqrt(min_range)
    sh = math.sqrt(max_range)
    
    # Calculate liquidity from initial token amounts
    L0 = token0_initial / (sp0 - sl) if (sp0 - sl) > 0 else 0
    L1 = token1_initial * (sp0 * sh) / (sh - sp0) if (sh - sp0) > 0 else 0
    L = (L0 + L1) / 2
    
    # Get LP tokens at target price
    t0_LP, t1_LP = tokens_from_liquidity(target_price, min_range, max_range, L)
    
    # Calculate values
    lp_value = t0_LP + t1_LP * target_price
    hodl_value = token0_initial + token1_initial * target_price
    
    # Calculate IL
    il = lp_value - hodl_value
    il_pct = (il / hodl_value * 100.0) if hodl_value > 0 else 0.0
    
    return {
        "LP_value": lp_value,
        "HODL_value": hodl_value,
        "IL": il,
        "IL_pct": il_pct,
    }


def calculate_il_extended(path="backtest_results.json"):
    """
    Calculate IL from backtest_results.json file.
    Kept for backwards compatibility.
    """
    with open(path, "r") as f:
        data = json.load(f)

    token0_initial = float(data["initialInvestment"]["token0Amount"])
    token1_initial = float(data["initialInvestment"]["token1Amount"])
    P0 = float(data["initialInvestment"]["entryPrice"])
    P_final = float(data["hodlStrategy"]["finalPrice"])

    P_low = float(data["config"]["minRange"])
    P_high = float(data["config"]["maxRange"])

    # Use the new function for consistency
    return {
        "IL_final": calculate_il_at_price(P0, token0_initial, token1_initial, P_final, P_low, P_high),
        "IL_lower_boundary": calculate_il_at_price(P0, token0_initial, token1_initial, P_low, P_low, P_high),
        "IL_upper_boundary": calculate_il_at_price(P0, token0_initial, token1_initial, P_high, P_low, P_high),
    }


if __name__ == "__main__":
    out = calculate_il_extended("backtest_results.json")

    print("\n=== IL at Final Price ===")
    print(out["IL_final"])

    print("\n=== IL if Price Hits LOWER Boundary ===")
    print(out["IL_lower_boundary"])

    print("\n=== IL if Price Hits UPPER Boundary ===")
    print(out["IL_upper_boundary"])
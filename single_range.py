#!/usr/bin/env python3
"""
Run a single range from config (no combinations), using the same logic as range_optimizer:
- Backtest the configured range
- Fetch live current price from CoinGecko
- Calculate IL at boundaries
- Calculate Polymarket insurance costs
- Compute net net APY
Outputs JSON to stdout.
"""

import json
import sys
import logging
import os
import requests

from db_utils import get_db_connection
from range_optimizer import (
    load_config,
    run_backtest_for_range,
    get_token_symbol_from_pool,
    get_current_price_from_coingecko,
    calculate_insurance_costs,
    calculate_net_apy,
)
from il import calculate_il_at_price

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


def get_uniswap_v3_pool_fee_tier(pool_address: str, rpc_url: str) -> int | None:
    """Fetch Uniswap V3 pool fee tier (uint24) from on-chain `fee()` via eth_call."""
    # function selector for fee(): keccak("fee()")[0:4] = 0xddca3f43
    data = "0xddca3f43"
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_call",
        "params": [
            {"to": pool_address, "data": data},
            "latest",
        ],
    }
    try:
        r = requests.post(rpc_url, json=payload, timeout=15)
        r.raise_for_status()
        j = r.json()
        result = j.get("result")
        if not result or result == "0x":
            return None
        # ABI-encoded uint24 is right-aligned in 32 bytes
        return int(result, 16)
    except Exception as e:
        print("[DEBUG] feeTier RPC fetch failed:", e)
        return None


def main(config_path: str = "uniswap-v3-backtest/config.json"):
    config = load_config(config_path)

    pool_id = config["poolID"]
    min_range = config["minRange"]
    max_range = config["maxRange"]
    investment = config["investmentAmount"]
    days = config.get("days", 30)
    period = config.get("period", "hourly")
    protocol = config.get("protocol", 0)
    price_token = config.get("priceToken", 0)

    # RPC URL for on-chain pool metadata checks
    rpc_url = os.environ.get("ETH_RPC_URL", "https://eth.llamarpc.com")

    logger.info(f"Running SINGLE range [{min_range}, {max_range}] for pool {pool_id}")

    backtest = run_backtest_for_range(
        pool_id,
        min_range,
        max_range,
        investment,
        days,
        period,
        protocol,
        price_token
    )

    if not backtest:
        raise RuntimeError("Backtest failed for the configured range.")

    # --- DEBUG Step 1/2: Validate what lpStrategy['apy'] represents using backtest fields ---
    lp_apy_reported = float(backtest["lpStrategy"]["apy"])
    actual_days = float(backtest["period"]["actualDays"])

    lp_total_with_fees = float(backtest["lpStrategy"].get("totalValueWithFees", 0.0))
    lp_roi_pct_period = float(backtest["lpStrategy"].get("roi", 0.0))  # percent over the backtest period

    roi_frac = lp_roi_pct_period / 100.0
    apy_from_roi = (1.0 + roi_frac) ** (365.0 / actual_days) - 1.0
    apr_linear = roi_frac * (365.0 / actual_days)

    print("\n[DEBUG] lp_apy_reported_pct:", lp_apy_reported)
    print("[DEBUG] actual_days:", actual_days)
    print("[DEBUG] lp_total_with_fees_usd:", lp_total_with_fees)
    print("[DEBUG] lp_roi_pct_period:", lp_roi_pct_period)
    print("[DEBUG] apr_linear_pct:", apr_linear * 100.0)
    print("[DEBUG] apy_from_roi_pct:", apy_from_roi * 100.0)
    # --- END DEBUG Step 1/2 ---

    # --- DEBUG Step 3: Fee-only ROI/APR for apples-to-apples comparisons ---
    _fees = backtest.get("fees", {})
    fees_total_usd = float(_fees.get("totalUSD", 0.0))
    fee_roi_frac = (fees_total_usd / float(investment)) if float(investment) > 0 else 0.0
    fee_apr_linear = fee_roi_frac * (365.0 / actual_days)

    print("[DEBUG] fees_total_usd:", fees_total_usd)
    print("[DEBUG] fee_roi_period_pct:", fee_roi_frac * 100.0)
    print("[DEBUG] fee_apr_linear_pct:", fee_apr_linear * 100.0)
    # --- END DEBUG Step 3 ---

    pool_data = backtest["pool"]

    # --- DEBUG Step 4: Inspect pool fee tier / fee rate fields (common source of ~2x fee inflation) ---
    onchain_fee_tier = None
    try:
        print("[DEBUG] backtest_top_keys:", sorted(list(backtest.keys())))
        print("[DEBUG] pool_keys:", sorted(list(pool_data.keys())))

        onchain_fee_tier = get_uniswap_v3_pool_fee_tier(pool_id, rpc_url)
        if onchain_fee_tier is not None:
            print("[DEBUG] onchain_fee_tier_raw:", onchain_fee_tier)
            print("[DEBUG] onchain_fee_rate:", onchain_fee_tier / 1_000_000.0)
        else:
            print("[DEBUG] onchain_fee_tier_raw:", None)

        # Print any plausible fee-tier fields if present
        for k in ("feeTier", "fee", "fee_tier", "feeTierBps", "feeTierBips", "feeAmount"):
            if k in pool_data:
                print(f"[DEBUG] pool.{k}:", pool_data[k])
            if k in backtest:
                print(f"[DEBUG] backtest.{k}:", backtest[k])

        # Try to interpret fee tier into a fee rate (Uniswap v3 commonly uses 500/3000/10000 with denominator 1e6)
        fee_tier_raw = pool_data.get("feeTier") or pool_data.get("fee") or backtest.get("feeTier") or backtest.get("fee")
        if fee_tier_raw is not None:
            ft = float(fee_tier_raw)
            fee_rate = (ft / 1_000_000.0) if ft > 1.0 else ft
            print("[DEBUG] interpreted_fee_tier_raw:", fee_tier_raw)
            print("[DEBUG] interpreted_fee_rate:", fee_rate)
    except Exception as _e:
        print("[DEBUG] feeTier inspection error:", _e)
    # --- END DEBUG Step 4 ---

    token_symbol = get_token_symbol_from_pool(pool_id, pool_data)

    logger.info(f"Fetching current price for {token_symbol} from CoinGecko...")
    current_price = get_current_price_from_coingecko(token_symbol)
    if current_price is None:
        raise RuntimeError(f"Failed to fetch current price for {token_symbol}.")

    # Derive decimal difference (token1 - token0) to keep IL math aligned with the pool
    try:
        decimal_diff = int(pool_data["token1"].get("decimals", 18)) - int(pool_data["token0"].get("decimals", 18))
    except Exception:
        decimal_diff = 0
    logger.info(f"Using decimal_diff={decimal_diff} for IL/insurance math")

    conn = get_db_connection()
    try:
        # Use the actual initial allocation from the backtest (not recomputed) to keep IL realistic
        initial = backtest.get("initialInvestment", {})
        token0_initial = float(initial.get("token0Amount", 0.0))
        token1_initial = float(initial.get("token1Amount", 0.0))
        entry_price = float(initial.get("entryPrice", current_price))

        il_at_boundaries = {
            "lower": calculate_il_at_price(
                entry_price,
                token0_initial,
                token1_initial,
                min_range,
                min_range,
                max_range,
            ),
            "upper": calculate_il_at_price(
                entry_price,
                token0_initial,
                token1_initial,
                max_range,
                min_range,
                max_range,
            ),
        }

        insurance = calculate_insurance_costs(
            token_symbol,
            min_range,
            max_range,
            il_at_boundaries["lower"],
            il_at_boundaries["upper"],
            investment,
            conn
        )

        lp_apy = float(backtest["lpStrategy"]["apy"])
        actual_days = float(backtest["period"]["actualDays"])

        # Old net APY (kept for comparison/debug)
        net_apy_old = calculate_net_apy(lp_apy, insurance["total_cost"], investment, actual_days)

        # Fixed net APY: subtract insurance from the period end value, then annualize once
        lp_total_with_fees = float(backtest["lpStrategy"].get("totalValueWithFees", 0.0))
        net_end_usd = lp_total_with_fees - float(insurance["total_cost"])
        net_roi = (net_end_usd / float(investment)) - 1.0 if float(investment) > 0 else 0.0
        net_apy = ((1.0 + net_roi) ** (365.0 / actual_days) - 1.0) * 100.0

        # Linear APRs for apples-to-apples comparisons (many UIs report APR, not compounded APY)
        lp_roi_pct_period = float(backtest["lpStrategy"].get("roi", 0.0))
        lp_roi_frac = lp_roi_pct_period / 100.0
        lp_apr_linear_pct = (lp_roi_frac * (365.0 / actual_days)) * 100.0

        net_roi_pct_period = net_roi * 100.0
        net_apr_linear_pct = (net_roi * (365.0 / actual_days)) * 100.0

        fees_total_usd = float(backtest.get("fees", {}).get("totalUSD", 0.0))
        fee_roi_pct_period = (fees_total_usd / float(investment)) * 100.0 if float(investment) > 0 else 0.0
        fee_apr_linear_pct = fee_roi_pct_period * (365.0 / actual_days)

        print("\n[DEBUG] net_end_usd:", net_end_usd)
        print("[DEBUG] net_roi_period_pct:", net_roi * 100.0)
        print("[DEBUG] net_apy_fixed_pct:", net_apy)
        print("[DEBUG] net_apy_old_pct:", net_apy_old)
        print("[DEBUG] lp_apr_linear_pct:", lp_apr_linear_pct)
        print("[DEBUG] net_apr_linear_pct:", net_apr_linear_pct)

        result = {
            "pool_id": pool_id,
            "token_symbol": token_symbol,
            "current_price": current_price,
            "range": {
                "min": min_range,
                "max": max_range,
            },
            "lp_apy": lp_apy,
            "lp_roi_pct_period": lp_roi_pct_period,
            "lp_apr_linear_pct": lp_apr_linear_pct,
            "onchain_fee_tier": onchain_fee_tier,
            "onchain_fee_rate": (onchain_fee_tier / 1_000_000.0) if onchain_fee_tier is not None else None,
            "fees_total_usd": fees_total_usd,
            "fee_roi_pct_period": fee_roi_pct_period,
            "fee_apr_linear_pct": fee_apr_linear_pct,
            "insurance_cost": insurance["total_cost"],
            "insurance_cost_pct": (insurance["total_cost"] / investment * 100.0) if investment > 0 else 0,
            "net_apy": net_apy,
            "net_end_usd": net_end_usd,
            "net_roi_pct_period": net_roi_pct_period,
            "net_apr_linear_pct": net_apr_linear_pct,
            "net_apy_old": net_apy_old,
            "il_lower": il_at_boundaries["lower"]["IL"],
            "il_upper": il_at_boundaries["upper"]["IL"],
            "il_lower_pct": il_at_boundaries["lower"]["IL_pct"],
            "il_upper_pct": il_at_boundaries["upper"]["IL_pct"],
            "lower_insurance_cost": insurance["lower_cost"],
            "upper_insurance_cost": insurance["upper_cost"],
            "insurance_markets": {
                "lower": insurance.get("lower_market"),
                "upper": insurance.get("upper_market"),
            },
            "backtest_data": backtest,
        }

        # Human-readable summary (mirrors range_optimizer style)
        logger.info("\n" + "=" * 60)
        logger.info("Single Range Result")
        logger.info("=" * 60)
        logger.info(f"Range: [{min_range}, {max_range}]")
        logger.info(f"LP APY: {lp_apy:.2f}%")
        logger.info(f"Insurance Cost: ${insurance['total_cost']:.2f} ({result['insurance_cost_pct']:.2f}%)")
        logger.info(f"Net APY: {net_apy:.2f}%")
        logger.info(f"IL Lower: {result['il_lower']:.2f} ({result['il_lower_pct']:.2f}%)")
        logger.info(f"IL Upper: {result['il_upper']:.2f} ({result['il_upper_pct']:.2f}%)")
        logger.info(f"Results use current price: ${current_price:.2f}")
        
        # Add insurance market information
        logger.info("\n" + "-" * 60)
        logger.info("Insurance Markets to Purchase:")
        logger.info("-" * 60)
        
        if insurance.get("lower_market"):
            lower_m = insurance["lower_market"]
            logger.info(f"Lower Boundary Insurance (${min_range:.2f}):")
            logger.info(f"  Market ID: {lower_m['market_id']}")
            logger.info(f"  Question: {lower_m['market_question']}")
            logger.info(f"  Bet Price: ${lower_m['price']:.4f}")
            logger.info(f"  Coverage Needed: ${insurance['lower_coverage']:.2f}")
            logger.info(f"  Cost: ${insurance['lower_cost']:.2f}")
        else:
            logger.info(f"Lower Boundary Insurance (${min_range:.2f}): Not needed (no IL)")
        
        if insurance.get("upper_market"):
            upper_m = insurance["upper_market"]
            logger.info(f"Upper Boundary Insurance (${max_range:.2f}):")
            logger.info(f"  Market ID: {upper_m['market_id']}")
            logger.info(f"  Question: {upper_m['market_question']}")
            logger.info(f"  Bet Price: ${upper_m['price']:.4f}")
            logger.info(f"  Coverage Needed: ${insurance['upper_coverage']:.2f}")
            logger.info(f"  Cost: ${insurance['upper_cost']:.2f}")
        else:
            logger.info(f"Upper Boundary Insurance (${max_range:.2f}): Not needed (no IL)")
        
        logger.info("\n" + "=" * 60)
        logger.info("Full JSON result below:")

        print(json.dumps(result, indent=2))

    finally:
        conn.close()


if __name__ == "__main__":
    try:
        cfg_path = sys.argv[1] if len(sys.argv) > 1 else "uniswap-v3-backtest/config.json"
        main(cfg_path)
    except Exception as e:
        logger.error(f"Error in single_range: {e}", exc_info=True)
        sys.exit(1)

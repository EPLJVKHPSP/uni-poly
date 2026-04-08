#!/usr/bin/env python3
"""
Main orchestrator script for IL hedging optimization.

Finds the best Uniswap V3 LP range by:
1. Extracting all range combinations from Polymarket database
2. Backtesting each range
3. Calculating IL at boundaries
4. Calculating insurance costs
5. Finding range with highest net APY
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional
import logging
import requests
import math

from db_utils import (
    get_db_connection,
    get_range_combinations,
    get_insurance_cost,
    get_clob_token_id,
    get_historical_bet_price,
)
from il import calculate_il_at_price

# CoinGecko IDs for tokens (matching parser.py)
COINGECKO_IDS = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "SOL": "solana",
    "BNB": "wbnb",
    "PUMP": "pump-fun",
    "LINK": "chainlink",
    "ENA": "ethena",
}

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def load_config(config_path: str = "uniswap-v3-backtest/config.json") -> Dict:
    """Load configuration from config.json file."""
    config_file = Path(config_path)
    if not config_file.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")
    
    with open(config_file, 'r') as f:
        config = json.load(f)
    
    return config


def get_token_symbol_from_pool(pool_id: str, pool_data: Dict) -> str:
    """
    Extract token symbol from pool data.
    
    For Uniswap V3 pools, token1 is typically the volatile token.
    We need to map WETH -> ETH, WBTC -> BTC, etc.
    """
    if not pool_data or 'token1' not in pool_data:
        raise ValueError(f"Cannot extract token symbol from pool {pool_id}")
    
    token1_symbol = pool_data['token1'].get('symbol', '').upper()
    
    # Map wrapped tokens to underlying symbols
    token_mapping = {
        'WETH': 'ETH',
        'WBTC': 'BTC',
        'WBNB': 'BNB',
    }
    
    # Remove 'W' prefix if present
    if token1_symbol.startswith('W') and len(token1_symbol) > 1:
        token1_symbol = token1_symbol[1:]
    
    # Apply mapping
    symbol = token_mapping.get(token1_symbol, token1_symbol)
    
    logger.info(f"Extracted token symbol: {symbol} from pool token1: {pool_data['token1'].get('symbol')}")
    return symbol


_price_cache: Dict[str, float] = {}


def get_current_price_from_coingecko(token_symbol: str) -> Optional[float]:
    """
    Fetch current price from CoinGecko API.
    
    Args:
        token_symbol: Token symbol (e.g., "ETH", "BTC")
        
    Returns:
        Current price in USD or None if failed
    """
    if token_symbol in _price_cache:
        return _price_cache[token_symbol]

    coingecko_id = COINGECKO_IDS.get(token_symbol)
    if not coingecko_id:
        logger.warning(f"No CoinGecko ID found for token symbol: {token_symbol}")
        return None
    
    try:
        url = f"https://api.coingecko.com/api/v3/simple/price"
        params = {
            "ids": coingecko_id,
            "vs_currencies": "usd"
        }
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        
        if coingecko_id in data and "usd" in data[coingecko_id]:
            price = float(data[coingecko_id]["usd"])
            logger.info(f"Fetched current price from CoinGecko: ${price:.2f} for {token_symbol}")
            _price_cache[token_symbol] = price
            return price
        else:
            logger.error(f"CoinGecko response missing price for {token_symbol}: {data}")
            return None
    except Exception as e:
        logger.error(f"Failed to fetch price from CoinGecko for {token_symbol}: {e}")
        return None


def tokens_for_strategy(min_range: float, max_range: float, investment: float, price: float, decimal_diff: int) -> (float, float):
    """
    Python port of tokensForStrategy from backtest.mjs.
    Returns tuple (amount0, amount1) at a given price for a range and investment.
    """
    sqrt_price = math.sqrt(price * (math.pow(10, decimal_diff)))
    sqrt_low = math.sqrt(min_range * (math.pow(10, decimal_diff)))
    sqrt_high = math.sqrt(max_range * (math.pow(10, decimal_diff)))

    amount0 = amount1 = 0.0

    if sqrt_price > sqrt_low and sqrt_price < sqrt_high:
        delta = investment / (((sqrt_price - sqrt_low)) + (((1 / sqrt_price) - (1 / sqrt_high)) * (price * math.pow(10, decimal_diff))))
        amount1 = delta * (sqrt_price - sqrt_low)
        amount0 = delta * ((1 / sqrt_price) - (1 / sqrt_high)) * math.pow(10, decimal_diff)
    elif sqrt_price < sqrt_low:
        delta = investment / ((((1 / sqrt_low) - (1 / sqrt_high)) * price))
        amount1 = 0.0
        amount0 = delta * ((1 / sqrt_low) - (1 / sqrt_high))
    else:
        delta = investment / ((sqrt_high - sqrt_low))
        amount1 = delta * (sqrt_high - sqrt_low)
        amount0 = 0.0

    return amount0, amount1


def run_backtest_for_range(
    pool_id: str,
    min_range: float,
    max_range: float,
    investment: float,
    days: int,
    period: str = "hourly",
    protocol: int = 0,
    price_token: int = 0,
    script_dir: str = "uniswap-v3-backtest"
) -> Optional[Dict]:
    """
    Run JS backtest script for a specific range.
    
    Returns:
        Dict with backtest results or None if failed
    """
    # Get absolute path to script
    script_dir_path = Path(script_dir).resolve()
    script_path = script_dir_path / "example.js"
    
    if not script_path.exists():
        logger.error(f"Backtest script not found: {script_path}")
        return None
    
    cmd = [
        "node",
        "example.js",  # Use relative path since we'll cd into the directory
        "--pool-id", pool_id,
        "--min-range", str(min_range),
        "--max-range", str(max_range),
        "--investment", str(investment),
        "--days", str(days),
        "--period", period,
        "--protocol", str(protocol),
        "--price-token", str(price_token),
        "--json-only"
    ]
    
    try:
        logger.debug(f"Running backtest: {' '.join(cmd)}")
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
            cwd=str(script_dir_path)
        )
        
        # Parse JSON output
        output = result.stdout.strip()
        stderr_output = result.stderr.strip()
        
        if not output:
            logger.error(f"No output from backtest for range [{min_range}, {max_range}]")
            if stderr_output:
                logger.error(f"STDERR: {stderr_output}")
            return None
        
        # Find JSON in output (might have warnings before)
        # Try multiple strategies to extract JSON
        backtest_data = None
        json_error = None
        
        # Strategy 1: Try parsing entire output
        try:
            backtest_data = json.loads(output)
        except json.JSONDecodeError as e:
            json_error = e
            # Strategy 2: Find JSON object boundaries
            lines = output.split('\n')
            json_start = -1
            brace_count = 0
            
            # Find first opening brace
            for i, line in enumerate(lines):
                if '{' in line:
                    json_start = i
                    break
            
            if json_start >= 0:
                # Find matching closing brace
                json_lines = lines[json_start:]
                json_text = '\n'.join(json_lines)
                
                # Count braces to find where JSON ends
                brace_count = 0
                json_end_pos = -1
                for i, char in enumerate(json_text):
                    if char == '{':
                        brace_count += 1
                    elif char == '}':
                        brace_count -= 1
                        if brace_count == 0:
                            json_end_pos = i + 1
                            break
                
                if json_end_pos > 0:
                    json_text = json_text[:json_end_pos]
                    try:
                        backtest_data = json.loads(json_text)
                    except json.JSONDecodeError as e2:
                        json_error = e2
        
        if backtest_data is None:
            logger.error(f"Failed to parse JSON for range [{min_range}, {max_range}]")
            logger.debug(f"Error: {json_error}")
            logger.debug(f"Output (first 1000 chars): {output[:1000]}")
            logger.debug(f"STDERR: {stderr_output[:500] if stderr_output else 'None'}")
            return None
        
        return backtest_data
        
    except subprocess.CalledProcessError as e:
        logger.error(f"Backtest failed for range [{min_range}, {max_range}]: {e.stderr}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error running backtest: {e}")
        return None


def calculate_il_at_boundaries(
    backtest_data: Dict,
    min_range: float,
    max_range: float,
    current_price: float,
    investment: float,
    decimal_diff: int
) -> Dict:
    """
    Calculate IL at minRange and maxRange boundaries.
    
    Returns:
        Dict with 'lower' and 'upper' IL values vs HODL
    """
    # Use the initial allocation captured by the JS backtester to keep IL math aligned
    # with the actual LP position that was simulated. This avoids over/under-scaling
    # from recomputing token amounts with different decimal handling.
    initial = backtest_data.get("initialInvestment", {})
    token0_initial = float(initial.get("token0Amount", 0.0))
    token1_initial = float(initial.get("token1Amount", 0.0))
    entry_price = float(initial.get("entryPrice", current_price))
    
    # Calculate IL at lower boundary
    il_lower = calculate_il_at_price(
        entry_price,
        token0_initial,
        token1_initial,
        min_range,
        min_range,
        max_range
    )
    
    # Calculate IL at upper boundary
    il_upper = calculate_il_at_price(
        entry_price,
        token0_initial,
        token1_initial,
        max_range,
        min_range,
        max_range
    )
    
    return {
        'lower': il_lower,
        'upper': il_upper
    }


def calculate_insurance_costs(
    token_symbol: str,
    min_range: float,
    max_range: float,
    il_lower: Dict,
    il_upper: Dict,
    investment: float,
    conn=None
) -> Dict:
    """
    Calculate insurance costs to cover IL at boundaries.
    
    Returns:
        Dict with insurance costs, coverage amounts, and market information
    """
    from db_utils import get_insurance_market_info
    
    # IL is negative when there's a loss
    # We only need insurance if IL is negative
    il_lower_amount = abs(min(0, il_lower['IL']))
    il_upper_amount = abs(min(0, il_upper['IL']))
    
    # Calculate insurance costs and get market info
    cost_lower = 0.0
    cost_upper = 0.0
    lower_market_info = None
    upper_market_info = None
    
    if il_lower_amount > 0:
        lower_market_info = get_insurance_market_info(
            token_symbol,
            min_range,
            'down',
            conn
        )
        if lower_market_info:
            cost_lower = il_lower_amount * lower_market_info['price']
    
    if il_upper_amount > 0:
        upper_market_info = get_insurance_market_info(
            token_symbol,
            max_range,
            'up',
            conn
        )
        if upper_market_info:
            cost_upper = il_upper_amount * upper_market_info['price']
    
    total_cost = cost_lower + cost_upper
    
    return {
        'lower_cost': cost_lower,
        'upper_cost': cost_upper,
        'total_cost': total_cost,
        'lower_coverage': il_lower_amount,
        'upper_coverage': il_upper_amount,
        'lower_market': lower_market_info,
        'upper_market': upper_market_info,
    }


def calculate_historical_insurance_costs(
    token_symbol: str,
    min_range: float,
    max_range: float,
    il_lower: Dict,
    il_upper: Dict,
    backtest_start_ts,
    conn=None,
) -> Dict:
    """
    Calculate insurance costs using the historical bet price at backtest start,
    instead of the current live price.

    Args:
        backtest_start_ts: Unix timestamp (or datetime) of backtest period start.
                           The bet price closest to this time is used.

    Returns:
        Same structure as calculate_insurance_costs, with an extra
        'price_source' key set to 'historical'.
    """
    il_lower_amount = abs(min(0, il_lower["IL"]))
    il_upper_amount = abs(min(0, il_upper["IL"]))

    cost_lower = 0.0
    cost_upper = 0.0
    lower_bet_price = None
    upper_bet_price = None

    if il_lower_amount > 0:
        clob_id = get_clob_token_id(token_symbol, min_range, "down", "Yes", conn)
        if clob_id:
            lower_bet_price = get_historical_bet_price(clob_id, backtest_start_ts, conn)
            if lower_bet_price is not None:
                cost_lower = il_lower_amount * lower_bet_price

    if il_upper_amount > 0:
        clob_id = get_clob_token_id(token_symbol, max_range, "up", "Yes", conn)
        if clob_id:
            upper_bet_price = get_historical_bet_price(clob_id, backtest_start_ts, conn)
            if upper_bet_price is not None:
                cost_upper = il_upper_amount * upper_bet_price

    return {
        "lower_cost": cost_lower,
        "upper_cost": cost_upper,
        "total_cost": cost_lower + cost_upper,
        "lower_coverage": il_lower_amount,
        "upper_coverage": il_upper_amount,
        "lower_bet_price": lower_bet_price,
        "upper_bet_price": upper_bet_price,
        "lower_market": None,
        "upper_market": None,
        "price_source": "historical",
    }


def filter_realistic_ranges(
    range_combinations: List[Dict],
    current_price: float
) -> List[Dict]:
    """
    Filter range combinations to only keep ranges within ±60% of current price.
    
    Args:
        range_combinations: List of range dictionaries
        current_price: Current price of the token
        
    Returns:
        Filtered and deduplicated list of range combinations
    """
    filtered = []
    seen_ranges = set()
    
    # Calculate price bounds: ±60% from current price
    min_allowed = current_price * 0.4  # -60%
    max_allowed = current_price * 1.6  # +60%
    
    for combo in range_combinations:
        min_r = combo['min']
        max_r = combo['max']
        
        # Create a unique key for deduplication (round to 2 decimals)
        range_key = (round(min_r, 2), round(max_r, 2))
        if range_key in seen_ranges:
            continue
        seen_ranges.add(range_key)
        
        # Only keep ranges where both boundaries are within ±60% of current price
        if min_r >= min_allowed and max_r <= max_allowed:
            filtered.append(combo)
    
    return filtered


def calculate_net_apy(
    lp_apy: float,
    insurance_cost: float,
    investment: float,
    days: float
) -> float:
    """
    Calculate net APY after subtracting insurance costs.
    
    Args:
        lp_apy: LP strategy APY (percentage)
        insurance_cost: Total insurance cost in USD
        investment: Initial investment in USD
        days: Number of days for the strategy
        
    Returns:
        Net APY (percentage)
    """
    # Convert insurance cost to APY equivalent
    if days <= 0 or investment <= 0:
        return lp_apy
    
    # Insurance cost as percentage of investment
    insurance_pct = (insurance_cost / investment) * 100.0
    
    # Annualized insurance cost
    insurance_apy = insurance_pct * (365.0 / days)
    
    # Net APY = LP APY - Insurance APY cost
    net_apy = lp_apy - insurance_apy
    
    return net_apy


def is_range_valid_for_current_price(
    min_range: float,
    max_range: float,
    current_price: float,
    min_buffer_pct: float = 4.5
) -> bool:
    """
    Validate if a range is suitable given the current price.
    
    Rules:
    1. If current price is below the range, lower bound must be at least 
       min_buffer_pct% below current price
    2. If current price is above the range, upper bound must be at least 
       min_buffer_pct% above current price
    3. If current price is inside the range, both boundaries must maintain 
       the buffer distance
    
    Args:
        min_range: Lower bound of the range
        max_range: Upper bound of the range
        current_price: Current price of the token
        min_buffer_pct: Minimum buffer percentage (default 4.5%)
        
    Returns:
        bool: True if range is valid, False otherwise
    """
    # Calculate distances from current price to boundaries
    lower_distance_pct = ((current_price - min_range) / current_price) * 100.0
    upper_distance_pct = ((max_range - current_price) / current_price) * 100.0
    
    # Case 1: Current price is below the range
    if current_price < min_range:
        # Lower bound must be at least min_buffer_pct% below current price
        if lower_distance_pct < min_buffer_pct:
            return False
    
    # Case 2: Current price is above the range
    elif current_price > max_range:
        # Upper bound must be at least min_buffer_pct% above current price
        if upper_distance_pct < min_buffer_pct:
            return False
    
    # Case 3: Current price is inside the range
    else:
        # Both boundaries must maintain buffer distance
        if abs(lower_distance_pct) < min_buffer_pct or abs(upper_distance_pct) < min_buffer_pct:
            return False
    
    return True


def optimize_ranges(
    config_path: str = "uniswap-v3-backtest/config.json",
    output_path: str = "optimization_results.json",
    use_historical: bool = False,
) -> Dict:
    """
    Main optimization function.

    Args:
        config_path: Path to backtest config JSON
        output_path: Where to write results
        use_historical: If True, insurance costs are computed using
                        historical Polymarket bet prices from the backtest
                        period (requires bet_price_history table populated
                        via polymarket_history.py).
    
    Returns:
        Dict with best range and all tested ranges
    """
    mode_label = "HISTORICAL" if use_historical else "LIVE"
    logger.info(f"Starting range optimization (insurance mode: {mode_label})...")
    
    # Load config
    config = load_config(config_path)
    pool_id = config['poolID']
    investment = config['investmentAmount']
    days = config.get('days', 30)
    period = config.get('period', 'hourly')
    protocol = config.get('protocol', 0)
    price_token = config.get('priceToken', 0)
    
    logger.info(f"Pool ID: {pool_id}")
    logger.info(f"Investment: ${investment}")
    logger.info(f"Days: {days}")
    
    # Run a quick backtest to get pool data and extract token symbol
    logger.info("Fetching pool data to extract token symbol...")
    temp_backtest = run_backtest_for_range(
        pool_id,
        config.get('minRange', 2000),
        config.get('maxRange', 4000),
        investment,
        days,
        period,
        protocol,
        price_token
    )
    
    if not temp_backtest:
        raise RuntimeError("Failed to get pool data. Check your pool ID and API key.")
    
    pool_data = temp_backtest['pool']
    token_symbol = get_token_symbol_from_pool(pool_id, pool_data)
    
    # Get actual current price from CoinGecko (not historical backtest entry price)
    logger.info(f"Fetching current price for {token_symbol} from CoinGecko...")
    current_price = get_current_price_from_coingecko(token_symbol)
    
    if current_price is None:
        raise RuntimeError(f"Failed to fetch current price for {token_symbol}. Check CoinGecko API or token symbol mapping.")

    # Decimal difference used for token computations (matches JS backtester)
    try:
        decimal_diff = int(pool_data['token1'].get('decimals', 18)) - int(pool_data['token0'].get('decimals', 18))
    except Exception:
        decimal_diff = 0
    
    logger.info(f"Optimizing ranges for token: {token_symbol}")
    logger.info(f"Current price: ${current_price:.2f}")
    
    # Get all range combinations from database
    conn = get_db_connection()
    try:
        logger.info("Fetching range combinations from database...")
        range_combinations = get_range_combinations(token_symbol, conn)
        logger.info(f"Found {len(range_combinations)} total range combinations")
        
        # Filter to only realistic ranges (within ±60% of current price)
        logger.info("Filtering to realistic ranges (within ±60% of current price)...")
        range_combinations = filter_realistic_ranges(
            range_combinations,
            current_price
        )
        logger.info(f"Filtered to {len(range_combinations)} realistic ranges to test (duplicates removed)")
        
        if not range_combinations:
            raise ValueError(f"No range combinations found for token {token_symbol} in database")
        
        # Test each range combination
        all_results = []
        best_result = None
        best_net_apy = float('-inf')
        
        for i, range_combo in enumerate(range_combinations, 1):
            min_r = range_combo['min']
            max_r = range_combo['max']
            
            logger.info(f"[{i}/{len(range_combinations)}] Testing range [{min_r}, {max_r}]...")
            
            # Run backtest
            backtest_data = run_backtest_for_range(
                pool_id,
                min_r,
                max_r,
                investment,
                days,
                period,
                protocol,
                price_token
            )
            
            if not backtest_data:
                logger.warning(f"Skipping range [{min_r}, {max_r}] - backtest failed")
                continue
            
            # Validate range suitability BEFORE expensive calculations
            if not is_range_valid_for_current_price(
                min_r, max_r, current_price, min_buffer_pct=4.5
            ):
                # Calculate distances for logging
                lower_distance_pct = ((current_price - min_r) / current_price) * 100.0
                upper_distance_pct = ((max_r - current_price) / current_price) * 100.0
                
                logger.warning(
                    f"  Range [{min_r}, {max_r}] rejected: current price ${current_price:.2f} "
                    f"too close to boundaries (lower: {lower_distance_pct:.2f}%, "
                    f"upper: {upper_distance_pct:.2f}%). Skipping IL/insurance calculations."
                )
                continue  # Skip to next range, don't calculate IL/insurance
            
            # Calculate IL at boundaries using live price and recomputed tokens
            il_at_boundaries = calculate_il_at_boundaries(
                backtest_data,
                min_r,
                max_r,
                current_price,
                investment,
                decimal_diff
            )
            
            # Calculate insurance costs (historical or live)
            if use_historical:
                backtest_start_ts = backtest_data['period']['startTimestamp']
                insurance = calculate_historical_insurance_costs(
                    token_symbol,
                    min_r,
                    max_r,
                    il_at_boundaries['lower'],
                    il_at_boundaries['upper'],
                    backtest_start_ts,
                    conn,
                )
            else:
                insurance = calculate_insurance_costs(
                    token_symbol,
                    min_r,
                    max_r,
                    il_at_boundaries['lower'],
                    il_at_boundaries['upper'],
                    investment,
                    conn,
                )
            
            # Get LP APY from backtest
            lp_apy = backtest_data['lpStrategy']['apy']
            actual_days = backtest_data['period']['actualDays']
            
            # Calculate net APY
            net_apy = calculate_net_apy(
                lp_apy,
                insurance['total_cost'],
                investment,
                actual_days
            )
            
            # Store result
            result = {
                'min_range': min_r,
                'max_range': max_r,
                'lp_apy': lp_apy,
                'insurance_cost': insurance['total_cost'],
                'insurance_cost_pct': (insurance['total_cost'] / investment * 100.0) if investment > 0 else 0,
                'net_apy': net_apy,
                'il_lower': il_at_boundaries['lower']['IL'],
                'il_upper': il_at_boundaries['upper']['IL'],
                'il_lower_pct': il_at_boundaries['lower']['IL_pct'],
                'il_upper_pct': il_at_boundaries['upper']['IL_pct'],
                'lower_insurance_cost': insurance['lower_cost'],
                'upper_insurance_cost': insurance['upper_cost'],
                'backtest_data': backtest_data,  # Store full backtest for reference
            }
            
            all_results.append(result)
            
            # Update best result (only valid ranges reach here)
            if net_apy > best_net_apy:
                best_net_apy = net_apy
                best_result = result
                logger.info(f"  New best valid range: [{min_r}, {max_r}] with Net APY: {net_apy:.2f}%")
            
            logger.info(f"  LP APY: {lp_apy:.2f}%, Insurance: ${insurance['total_cost']:.2f}, Net APY: {net_apy:.2f}%")
        
        # Check if we found any valid results
        if best_result is None:
            raise ValueError(
                f"No valid ranges found for {token_symbol} at current price ${current_price:.2f}. "
                f"All {len(all_results)} tested ranges were too close to current price boundaries."
            )
        
        # Prepare output
        output = {
            'timestamp': best_result['backtest_data'].get('timestamp', ''),
            'pool_id': pool_id,
            'token_symbol': token_symbol,
            'investment_amount': investment,
            'days': days,
            'period': period,
            'insurance_price_mode': 'historical' if use_historical else 'live',
            'best_range': {
                'min': best_result['min_range'],
                'max': best_result['max_range'],
                'lp_apy': best_result['lp_apy'],
                'insurance_cost': best_result['insurance_cost'],
                'insurance_cost_pct': best_result['insurance_cost_pct'],
                'net_apy': best_result['net_apy'],
                'il_lower': best_result['il_lower'],
                'il_upper': best_result['il_upper'],
                'il_lower_pct': best_result['il_lower_pct'],
                'il_upper_pct': best_result['il_upper_pct'],
            },
            'all_ranges': [
                {
                    'min': r['min_range'],
                    'max': r['max_range'],
                    'lp_apy': r['lp_apy'],
                    'insurance_cost': r['insurance_cost'],
                    'insurance_cost_pct': r['insurance_cost_pct'],
                    'net_apy': r['net_apy'],
                    'il_lower': r['il_lower'],
                    'il_upper': r['il_upper'],
                }
                for r in all_results
            ],
            'total_ranges_tested': len(all_results)
        }
        
        # Save results
        output_file = Path(output_path)
        with open(output_file, 'w') as f:
            json.dump(output, f, indent=2)
        
        logger.info(f"\n{'='*60}")
        logger.info("Optimization Complete!")
        logger.info(f"{'='*60}")
        logger.info(f"Best Range: [{best_result['min_range']}, {best_result['max_range']}]")
        logger.info(f"LP APY: {best_result['lp_apy']:.2f}%")
        logger.info(f"Insurance Cost: ${best_result['insurance_cost']:.2f} ({best_result['insurance_cost_pct']:.2f}%)")
        logger.info(f"Net APY: {best_result['net_apy']:.2f}%")
        logger.info(f"IL Lower: {best_result['il_lower']:.2f} ({best_result['il_lower_pct']:.2f}%)")
        logger.info(f"IL Upper: {best_result['il_upper']:.2f} ({best_result['il_upper_pct']:.2f}%)")
        logger.info(f"\nResults saved to: {output_path}")
        
        return output
        
    finally:
        conn.close()


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Uniswap V3 range optimizer with Polymarket IL hedging")
    ap.add_argument("--config", default="uniswap-v3-backtest/config.json", help="Path to backtest config")
    ap.add_argument("--output", default="optimization_results.json", help="Output JSON path")
    ap.add_argument(
        "--historical",
        action="store_true",
        help="Use historical Polymarket bet prices from the backtest period "
             "instead of live prices (requires bet_price_history table)",
    )
    args = ap.parse_args()

    try:
        optimize_ranges(args.config, args.output, use_historical=args.historical)
    except KeyboardInterrupt:
        logger.info("\nOptimization interrupted by user")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Error during optimization: {e}", exc_info=True)
        sys.exit(1)
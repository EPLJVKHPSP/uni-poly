#!/usr/bin/env python3
"""
Active Position Backtester — backward-compatibility shim.

All logic lives in the ``backtester`` package.  This file re-exports every
public symbol so that existing imports (``from active_backtester import …``)
and ``python active_backtester.py`` continue to work unchanged.
"""

import logging
import requests  # noqa: F401  — kept so @patch("active_backtester.requests.post") works

from dotenv import load_dotenv

from db_utils import (  # noqa: F401  — kept for patch targets
    get_db_connection,
    get_range_combinations,
    get_clob_token_id,
    get_historical_bet_price,
)
from il import calculate_il_at_price  # noqa: F401

from backtester import (  # noqa: F401
    SUBGRAPH_ID,
    _graph_url,
    fetch_pool_metadata,
    fetch_hourly_candles,
    _log_base,
    _get_tick_from_price,
    _active_liquidity_for_candle,
    _calc_unbounded_fees,
    _tokens_for_strategy_scaled,
    _tokens_for_strategy_human,
    _liquidity_for_strategy,
    _tokens_from_liquidity_v3,
    compute_hourly_fee_split,
    GAS_MINT,
    GAS_BURN_COLLECT,
    GAS_SWAP,
    fetch_daily_gas_prices,
    gas_cost_usd,
    _map_wrapped_symbol,
    _filter_ranges_for_price,
    _score_range,
    pick_best_range,
    pick_best_range_by_sweep,
    _get_insurance_for_range,
    open_position,
    close_position,
    simulate,
    build_summary,
    run_sweep,
    main,
)

load_dotenv(override=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

if __name__ == "__main__":
    main()

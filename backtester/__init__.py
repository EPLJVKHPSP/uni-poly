"""Backtester package — re-exports all public symbols for backward compatibility."""

from .graph_client import (  # noqa: F401
    SUBGRAPH_ID,
    _graph_url,
    fetch_pool_metadata,
    fetch_hourly_candles,
)

from .fee_math import (  # noqa: F401
    _log_base,
    _get_tick_from_price,
    _active_liquidity_for_candle,
    _calc_unbounded_fees,
    _tokens_for_strategy_scaled,
    _tokens_for_strategy_human,
    _liquidity_for_strategy,
    _tokens_from_liquidity_v3,
    compute_hourly_fee_split,
)

from .range_selection import (  # noqa: F401
    _map_wrapped_symbol,
    _filter_ranges_for_price,
    _score_range,
    pick_best_range,
    pick_best_range_by_sweep,
    _get_insurance_for_range,
)

from .gas import (  # noqa: F401
    GAS_MINT,
    GAS_BURN_COLLECT,
    GAS_SWAP,
    fetch_daily_gas_prices,
    gas_cost_usd,
)

from .positions import (  # noqa: F401
    open_position,
    close_position,
)

from .polymarket_execution import (  # noqa: F401
    ClosePolicy,
    SlippageConfig,
    slippage_per_contract_usd,
    apply_execution_costs,
    choose_close_price,
)

from .simulation import (  # noqa: F401
    simulate,
    build_summary,
    run_sweep,
    main,
)

from .data_validation import (  # noqa: F401
    CANDLE_INTERVAL_SECS,
    CandleQualityReport,
    GasCoverageReport,
    PolymarketCoverageReport,
    validate_candles,
    validate_gas_coverage,
    validate_polymarket_coverage,
)

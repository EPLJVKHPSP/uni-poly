"""Unit tests for pure math helpers in active_backtester.py."""

import math
import pytest

from active_backtester import (
    _log_base,
    _get_tick_from_price,
    _active_liquidity_for_candle,
    _calc_unbounded_fees,
    _tokens_for_strategy_scaled,
    _tokens_for_strategy_human,
    _liquidity_for_strategy,
    _tokens_from_liquidity_v3,
    compute_hourly_fee_split,
    _map_wrapped_symbol,
    _filter_ranges_for_price,
    build_summary,
    GAS_MINT,
    GAS_BURN_COLLECT,
    GAS_SWAP,
    gas_cost_usd,
)


# ---------------------------------------------------------------------------
# _log_base
# ---------------------------------------------------------------------------

class TestLogBase:

    @pytest.mark.unit
    def test_known_values(self):
        assert _log_base(8, 2) == pytest.approx(3.0, rel=1e-9)
        assert _log_base(1000, 10) == pytest.approx(3.0, rel=1e-9)
        assert _log_base(1, 10) == pytest.approx(0.0, abs=1e-12)

    @pytest.mark.unit
    def test_tick_base(self):
        assert _log_base(1.0001, 1.0001) == pytest.approx(1.0, rel=1e-9)


# ---------------------------------------------------------------------------
# _get_tick_from_price
# ---------------------------------------------------------------------------

class TestGetTickFromPrice:

    @pytest.mark.unit
    def test_returns_integer(self):
        tick = _get_tick_from_price(3000.0, dec0=6, dec1=18)
        assert isinstance(tick, int)

    @pytest.mark.unit
    def test_higher_price_higher_tick(self):
        t1 = _get_tick_from_price(2000.0, dec0=6, dec1=18)
        t2 = _get_tick_from_price(4000.0, dec0=6, dec1=18)
        assert t2 > t1

    @pytest.mark.unit
    def test_zero_price_returns_zero(self):
        assert _get_tick_from_price(0.0, dec0=6, dec1=18) == 0

    @pytest.mark.unit
    def test_base_selected_flips_decimals(self):
        t0 = _get_tick_from_price(3000.0, dec0=6, dec1=18, base_selected=0)
        t1 = _get_tick_from_price(3000.0, dec0=6, dec1=18, base_selected=1)
        assert t0 != t1


# ---------------------------------------------------------------------------
# _active_liquidity_for_candle
# ---------------------------------------------------------------------------

class TestActiveLiquidityForCandle:

    @pytest.mark.unit
    def test_full_overlap(self):
        ratio = _active_liquidity_for_candle(min_tick=100, max_tick=200, low_tick=100, high_tick=200)
        assert ratio == pytest.approx(100.0)

    @pytest.mark.unit
    def test_no_overlap(self):
        ratio = _active_liquidity_for_candle(min_tick=100, max_tick=200, low_tick=300, high_tick=400)
        assert ratio == 0.0

    @pytest.mark.unit
    def test_partial_overlap(self):
        ratio = _active_liquidity_for_candle(min_tick=100, max_tick=200, low_tick=150, high_tick=250)
        assert 0 < ratio < 100.0

    @pytest.mark.unit
    def test_candle_inside_range(self):
        ratio = _active_liquidity_for_candle(min_tick=0, max_tick=500, low_tick=100, high_tick=200)
        assert ratio == pytest.approx(100.0)

    @pytest.mark.unit
    def test_equal_low_high_returns_one(self):
        ratio = _active_liquidity_for_candle(min_tick=100, max_tick=200, low_tick=150, high_tick=150)
        assert ratio == pytest.approx(100.0) or ratio == 0.0


# ---------------------------------------------------------------------------
# _calc_unbounded_fees
# ---------------------------------------------------------------------------

class TestCalcUnboundedFees:

    @pytest.mark.unit
    def test_zero_delta(self):
        f0, f1 = _calc_unbounded_fees("1000", "1000", "2000", "2000", dec0=6, dec1=18)
        assert f0 == 0.0
        assert f1 == 0.0

    @pytest.mark.unit
    def test_positive_delta(self):
        base = str(2**128 * 10**6)
        prev = "0"
        f0, f1 = _calc_unbounded_fees(base, prev, "0", "0", dec0=6, dec1=18)
        assert f0 == pytest.approx(1.0, rel=1e-9)
        assert f1 == 0.0

    @pytest.mark.unit
    def test_both_tokens(self):
        fg0 = str(2**128 * 10**6)
        fg1 = str(2**128 * 10**18)
        f0, f1 = _calc_unbounded_fees(fg0, "0", fg1, "0", dec0=6, dec1=18)
        assert f0 == pytest.approx(1.0, rel=1e-9)
        assert f1 == pytest.approx(1.0, rel=1e-9)


# ---------------------------------------------------------------------------
# _tokens_for_strategy_human
# ---------------------------------------------------------------------------

class TestTokensForStrategyHuman:

    @pytest.mark.unit
    def test_investment_sums_correctly(self):
        """token0 + token1 * price should approximate investment."""
        t0, t1 = _tokens_for_strategy_human(2000, 4000, 100000, 3000)
        value = t0 + t1 * 3000
        assert value == pytest.approx(100000, rel=0.01)

    @pytest.mark.unit
    def test_price_below_range_all_token1(self):
        t0, t1 = _tokens_for_strategy_human(3000, 4000, 100000, 2500)
        assert t0 == 0.0
        assert t1 > 0

    @pytest.mark.unit
    def test_price_above_range_all_token0(self):
        t0, t1 = _tokens_for_strategy_human(2000, 2500, 100000, 3000)
        assert t0 > 0
        assert t1 == 0.0

    @pytest.mark.unit
    def test_wider_range_different_split(self):
        t0_narrow, t1_narrow = _tokens_for_strategy_human(2800, 3200, 100000, 3000)
        t0_wide, t1_wide = _tokens_for_strategy_human(2000, 4000, 100000, 3000)
        assert t0_narrow != t0_wide

    @pytest.mark.unit
    @pytest.mark.parametrize("mn,mx,inv,price", [
        (2000, 4000, 100_000, 3000),
        (2500, 3500, 50_000, 3000),
        (1800, 2200, 100_000, 2000),
        (1600, 2400, 80_000, 1868),
        (2000, 3400, 100_000, 3180),
        (1800, 2800, 120_000, 2468),
    ])
    def test_value_conservation_parametrized(self, mn, mx, inv, price):
        """token0 + token1 * price must equal the investment for any valid range."""
        t0, t1 = _tokens_for_strategy_human(mn, mx, inv, price)
        reconstructed = t0 + t1 * price
        assert reconstructed == pytest.approx(inv, rel=1e-6)

    @pytest.mark.unit
    @pytest.mark.parametrize("mn,mx,price", [
        (2000, 4000, 3000),
        (2500, 3500, 3000),
        (1800, 2200, 2000),
        (1600, 2400, 1868),
    ])
    def test_ratio_matches_uniswap_v3_formula(self, mn, mx, price):
        """The USDC/ETH split must match the Uniswap V3 concentrated liquidity ratio:
        token0 / token1 == (sqrt(P) - sqrt(Pa)) / (1/sqrt(P) - 1/sqrt(Pb))
        """
        t0, t1 = _tokens_for_strategy_human(mn, mx, 100_000, price)
        sp = math.sqrt(price)
        sl = math.sqrt(mn)
        sh = math.sqrt(mx)
        expected_ratio = (sp - sl) / (1/sp - 1/sh)
        actual_ratio = t0 / t1 if t1 > 0 else float('inf')
        assert actual_ratio == pytest.approx(expected_ratio, rel=1e-6)


# ---------------------------------------------------------------------------
# _tokens_for_strategy_scaled
# ---------------------------------------------------------------------------

class TestTokensForStrategyScaled:

    @pytest.mark.unit
    def test_returns_nonnegative(self):
        t0, t1 = _tokens_for_strategy_scaled(2000, 4000, 100000, 3000, 12)
        assert t0 >= 0
        assert t1 >= 0

    @pytest.mark.unit
    def test_price_inside_range(self):
        t0, t1 = _tokens_for_strategy_scaled(2000, 4000, 100000, 3000, 12)
        assert t0 > 0
        assert t1 > 0

    @pytest.mark.unit
    @pytest.mark.parametrize("mn,mx,inv,price", [
        (2000, 4000, 100_000, 3000),
        (2500, 3500, 50_000, 3000),
        (1800, 2200, 100_000, 2000),
        (1600, 2400, 80_000, 1868),
    ])
    def test_scaled_tokens_produce_positive_liquidity(self, mn, mx, inv, price):
        """Scaled tokens must yield positive liquidity when price is inside range."""
        decimal_diff = 18 - 6
        t0_s, t1_s = _tokens_for_strategy_scaled(mn, mx, inv, price, decimal_diff)
        liq = _liquidity_for_strategy(price, mn, mx, t0_s, t1_s, 6, 18)
        assert liq > 0


# ---------------------------------------------------------------------------
# _liquidity_for_strategy
# ---------------------------------------------------------------------------

class TestLiquidityForStrategy:

    @pytest.mark.unit
    def test_positive_liquidity(self):
        t0, t1 = _tokens_for_strategy_scaled(2000, 4000, 100000, 3000, 12)
        liq = _liquidity_for_strategy(3000, 2000, 4000, t0, t1, 6, 18)
        assert liq > 0

    @pytest.mark.unit
    def test_zero_tokens_zero_liquidity(self):
        liq = _liquidity_for_strategy(3000, 2000, 4000, 0, 0, 6, 18)
        assert liq == 0.0


# ---------------------------------------------------------------------------
# _tokens_from_liquidity_v3
# ---------------------------------------------------------------------------

class TestTokensFromLiquidityV3:

    @pytest.mark.unit
    def test_round_trip_consistency(self):
        """tokens -> liquidity -> tokens should be consistent."""
        t0, t1 = _tokens_for_strategy_scaled(2000, 4000, 100000, 3000, 12)
        liq = _liquidity_for_strategy(3000, 2000, 4000, t0, t1, 6, 18)
        t0_rt, t1_rt = _tokens_from_liquidity_v3(3000, 2000, 4000, liq, 6, 18)
        assert t0_rt >= 0
        assert t1_rt >= 0

    @pytest.mark.unit
    def test_zero_liquidity(self):
        t0, t1 = _tokens_from_liquidity_v3(3000, 2000, 4000, 0, 6, 18)
        assert t0 == 0.0
        assert t1 == 0.0

    @pytest.mark.unit
    @pytest.mark.parametrize("mn,mx,inv,price", [
        (2000, 4000, 100_000, 3000),
        (2500, 3500, 50_000, 3000),
        (1800, 2200, 100_000, 2000),
        (1600, 2400, 80_000, 1868),
        (2000, 3400, 100_000, 3180),
    ])
    def test_round_trip_recovers_deposit_amounts(self, mn, mx, inv, price):
        """deposit -> scaled tokens -> liquidity -> tokens should recover the same amounts.
        Note: _tokens_from_liquidity_v3 returns (ETH-side, USDC-side) which is the
        reverse of _tokens_for_strategy_scaled's (USDC-side, ETH-side)."""
        decimal_diff = 18 - 6
        t0_s, t1_s = _tokens_for_strategy_scaled(mn, mx, inv, price, decimal_diff)
        liq = _liquidity_for_strategy(price, mn, mx, t0_s, t1_s, 6, 18)
        rt_eth, rt_usdc = _tokens_from_liquidity_v3(price, mn, mx, liq, 6, 18)
        assert rt_usdc == pytest.approx(t0_s, rel=0.01)
        assert rt_eth == pytest.approx(t1_s, rel=0.01)

    @pytest.mark.unit
    @pytest.mark.parametrize("mn,mx,inv", [
        (2000, 4000, 100_000),
        (1800, 2200, 100_000),
        (2000, 3400, 100_000),
    ])
    def test_boundary_behavior_human_tokens(self, mn, mx, inv):
        """At lower boundary: all value is ETH (token1), 0 USDC (token0).
        At upper boundary: all value is USDC (token0), 0 ETH (token1).
        This tests _tokens_for_strategy_human which the backtester uses for deposits/withdrawals."""
        t0_low, t1_low = _tokens_for_strategy_human(mn, mx, inv, mn)
        assert t0_low == pytest.approx(0.0, abs=1e-6), "At lower bound, USDC should be 0"
        assert t1_low > 0, "At lower bound, all value should be in ETH"

        t0_high, t1_high = _tokens_for_strategy_human(mn, mx, inv, mx)
        assert t0_high > 0, "At upper bound, all value should be in USDC"
        assert t1_high == pytest.approx(0.0, abs=1e-6), "At upper bound, ETH should be 0"


# ---------------------------------------------------------------------------
# compute_hourly_fee_split
# ---------------------------------------------------------------------------

class TestComputeHourlyFeeSplit:

    @pytest.mark.unit
    def test_zero_fee_growth_zero_fees(self, sample_candle_pair):
        """No fee growth delta -> no fees."""
        prev, curr = sample_candle_pair
        curr["feeGrowthGlobal0X128"] = prev["feeGrowthGlobal0X128"]
        curr["feeGrowthGlobal1X128"] = prev["feeGrowthGlobal1X128"]
        f_usdc, f_eth = compute_hourly_fee_split(curr, prev, 1e12, 2000, 4000, 6, 18)
        assert f_usdc == 0.0
        assert f_eth == 0.0

    @pytest.mark.unit
    def test_positive_fees_with_growth(self, sample_candle_pair):
        prev, curr = sample_candle_pair
        f_usdc, f_eth = compute_hourly_fee_split(curr, prev, 1e12, 2000, 4000, 6, 18)
        assert f_usdc >= 0
        assert f_eth >= 0

    @pytest.mark.unit
    def test_returns_tuple_of_two(self, sample_candle_pair):
        prev, curr = sample_candle_pair
        result = compute_hourly_fee_split(curr, prev, 1e12, 2000, 4000, 6, 18)
        assert len(result) == 2


# ---------------------------------------------------------------------------
# _map_wrapped_symbol
# ---------------------------------------------------------------------------

class TestMapWrappedSymbol:

    @pytest.mark.unit
    def test_weth_to_eth(self):
        assert _map_wrapped_symbol("WETH") == "ETH"

    @pytest.mark.unit
    def test_wbtc_to_btc(self):
        assert _map_wrapped_symbol("WBTC") == "BTC"

    @pytest.mark.unit
    def test_wbnb_to_bnb(self):
        assert _map_wrapped_symbol("WBNB") == "BNB"

    @pytest.mark.unit
    def test_passthrough_non_wrapped(self):
        assert _map_wrapped_symbol("USDC") == "USDC"

    @pytest.mark.unit
    def test_lowercase_handled(self):
        result = _map_wrapped_symbol("weth")
        assert result == "ETH"


# ---------------------------------------------------------------------------
# _filter_ranges_for_price
# ---------------------------------------------------------------------------

class TestFilterRangesForPrice:

    @pytest.mark.unit
    def test_filters_out_of_range(self, sample_combos):
        filtered = _filter_ranges_for_price(sample_combos, current_price=3000.0)
        for c in filtered:
            assert c["min"] < 3000.0
            assert c["max"] > 3000.0

    @pytest.mark.unit
    def test_buffer_requirement(self, sample_combos):
        filtered = _filter_ranges_for_price(sample_combos, current_price=3000.0, buffer_pct=5.0)
        for c in filtered:
            lower_dist = (3000.0 - c["min"]) / 3000.0 * 100
            upper_dist = (c["max"] - 3000.0) / 3000.0 * 100
            assert lower_dist >= 5.0
            assert upper_dist >= 5.0

    @pytest.mark.unit
    def test_width_limit(self, sample_combos):
        filtered = _filter_ranges_for_price(sample_combos, current_price=3000.0, max_width_pct=60.0)
        for c in filtered:
            width_pct = (c["max"] - c["min"]) / 3000.0 * 100
            assert width_pct <= 60.0

    @pytest.mark.unit
    def test_price_outside_all_ranges_returns_empty(self):
        combos = [{"min": 2000.0, "max": 2500.0}]
        filtered = _filter_ranges_for_price(combos, current_price=3000.0)
        assert filtered == []

    @pytest.mark.unit
    def test_deduplication(self):
        combos = [
            {"min": 2400.0, "max": 3600.0},
            {"min": 2400.0, "max": 3600.0},
        ]
        filtered = _filter_ranges_for_price(combos, current_price=3000.0)
        assert len(filtered) <= 1


# ---------------------------------------------------------------------------
# build_summary
# ---------------------------------------------------------------------------

class TestBuildSummary:

    @pytest.mark.unit
    def test_summary_structure(self):
        candles = [
            {"periodStartUnix": "1700000000", "close": "3000.0"},
            {"periodStartUnix": "1700086400", "close": "3100.0"},
        ]
        positions = [{
            "open_ts": 1700000000, "close_ts": 1700086400,
            "entry_price": 3000.0, "close_price": 3100.0,
            "min_range": 2500.0, "max_range": 3500.0,
            "fees_earned_usdc": 100.0, "fees_earned_eth": 0.05,
            "fees_earned_usd": 250.0, "il": -50.0, "il_pct": -0.5,
            "insurance_cost": 30.0, "insurance_payout": 0.0,
            "insurance_sellback": 10.0, "insurance_net": -20.0,
            "swap_fee": 5.0, "swap_amount": 10000.0,
            "gas_fee_open": 2.50, "gas_fee_close": 1.80,
            "spread_cost_buy": 1.20, "spread_cost_sell": 0.80,
            "wallet_before": {"usdc": 50000.0, "eth": 16.67, "value_usd": 100000.0},
            "wallet_after": {"usdc": 50100.0, "eth": 16.72, "value_usd": 101932.0},
            "token0_dep": 48000.0, "token1_dep": 16.0,
            "deposit_value": 96000.0,
            "duration_hours": 24.0,
            "touched_lower": False, "touched_upper": False,
        }]
        wallet = {"usdc": 50100.0, "eth": 16.72}
        summary = build_summary(positions, candles, 100000, "0xpool", "ETH", wallet)

        assert "pool_id" in summary
        assert "active_strategy" in summary
        assert "positions" in summary
        assert summary["active_strategy"]["total_positions"] == 1
        assert "total_gas_fees_usdc" in summary["active_strategy"]
        assert "total_spread_cost_usdc" in summary["active_strategy"]
        assert summary["active_strategy"]["total_spread_cost_usdc"] == pytest.approx(2.00, rel=1e-6)

    @pytest.mark.unit
    def test_roi_calculation(self):
        candles = [
            {"periodStartUnix": "1700000000", "close": "3000.0"},
            {"periodStartUnix": "1700086400", "close": "3000.0"},
        ]
        positions = []
        wallet = {"usdc": 55000.0, "eth": 16.67}
        summary = build_summary(positions, candles, 100000, "0xpool", "ETH", wallet)
        expected_value = 55000.0 + 16.67 * 3000.0
        expected_roi = (expected_value / 100000 - 1) * 100
        assert summary["active_strategy"]["roi_pct"] == pytest.approx(expected_roi, rel=0.01)

    @pytest.mark.unit
    def test_gas_fees_aggregated(self):
        candles = [
            {"periodStartUnix": "1700000000", "close": "3000.0"},
            {"periodStartUnix": "1700086400", "close": "3100.0"},
        ]
        positions = [{
            "open_ts": 1700000000, "close_ts": 1700086400,
            "entry_price": 3000.0, "close_price": 3100.0,
            "min_range": 2500.0, "max_range": 3500.0,
            "fees_earned_usdc": 100.0, "fees_earned_eth": 0.05,
            "fees_earned_usd": 250.0, "il": -50.0, "il_pct": -0.5,
            "insurance_cost": 30.0, "insurance_payout": 0.0,
            "insurance_sellback": 10.0, "insurance_net": -20.0,
            "swap_fee": 5.0, "swap_amount": 10000.0,
            "gas_fee_open": 2.50, "gas_fee_close": 1.80,
            "spread_cost_buy": 0.0, "spread_cost_sell": 0.0,
            "wallet_before": {"usdc": 50000.0, "eth": 16.67, "value_usd": 100000.0},
            "wallet_after": {"usdc": 50100.0, "eth": 16.72, "value_usd": 101932.0},
            "token0_dep": 48000.0, "token1_dep": 16.0,
            "deposit_value": 96000.0,
            "duration_hours": 24.0,
            "touched_lower": False, "touched_upper": False,
        }]
        wallet = {"usdc": 50100.0, "eth": 16.72}
        summary = build_summary(positions, candles, 100000, "0xpool", "ETH", wallet)
        assert summary["active_strategy"]["total_gas_fees_usdc"] == pytest.approx(4.30, rel=1e-6)


# ---------------------------------------------------------------------------
# gas_cost_usd
# ---------------------------------------------------------------------------

class TestGasCostUsd:

    @pytest.mark.unit
    def test_basic_calculation(self):
        """gas_units * gas_price_wei * 1e-18 * eth_price == expected."""
        gas_prices = {"2024-01-15": 20_000_000_000}
        ts = 1705276800
        eth_price = 2500.0
        cost = gas_cost_usd(GAS_MINT, ts, eth_price, gas_prices)
        expected = GAS_MINT * 20_000_000_000 * 1e-18 * 2500.0
        assert cost == pytest.approx(expected, rel=1e-9)

    @pytest.mark.unit
    def test_empty_map_returns_zero(self):
        cost = gas_cost_usd(GAS_MINT, 1705276800, 2500.0, {})
        assert cost == 0.0

    @pytest.mark.unit
    def test_missing_date_returns_zero(self):
        gas_prices = {"2024-01-14": 20_000_000_000}
        cost = gas_cost_usd(GAS_MINT, 1705276800, 2500.0, gas_prices)
        assert cost == 0.0

    @pytest.mark.unit
    def test_scales_with_gas_units(self):
        gas_prices = {"2024-01-15": 10_000_000_000}
        ts = 1705276800
        cost_mint = gas_cost_usd(GAS_MINT, ts, 2000.0, gas_prices)
        cost_burn = gas_cost_usd(GAS_BURN_COLLECT, ts, 2000.0, gas_prices)
        assert cost_mint / cost_burn == pytest.approx(GAS_MINT / GAS_BURN_COLLECT, rel=1e-9)

    @pytest.mark.unit
    def test_scales_with_eth_price(self):
        gas_prices = {"2024-01-15": 10_000_000_000}
        ts = 1705276800
        cost_low = gas_cost_usd(GAS_MINT, ts, 1000.0, gas_prices)
        cost_high = gas_cost_usd(GAS_MINT, ts, 3000.0, gas_prices)
        assert cost_high / cost_low == pytest.approx(3.0, rel=1e-9)

    @pytest.mark.unit
    def test_constants_are_positive(self):
        assert GAS_MINT > 0
        assert GAS_BURN_COLLECT > 0
        assert GAS_SWAP > 0

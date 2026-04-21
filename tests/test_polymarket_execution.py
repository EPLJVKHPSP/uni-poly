"""Unit tests for backtester.polymarket_execution (pure math)."""

import pytest

from backtester.polymarket_execution import (
    SlippageConfig,
    slippage_per_contract_usd,
    apply_execution_costs,
    choose_close_price,
)


class TestSlippagePerContract:
    @pytest.mark.unit
    def test_disabled_when_cfg_none(self):
        assert slippage_per_contract_usd(contracts=1000, cfg=None) == pytest.approx(0.0)

    @pytest.mark.unit
    def test_linear_in_contracts(self):
        cfg = SlippageConfig(per_1k_contracts=0.02, max_per_contract=0.0)
        assert slippage_per_contract_usd(0, cfg) == pytest.approx(0.0)
        assert slippage_per_contract_usd(1000, cfg) == pytest.approx(0.02)
        assert slippage_per_contract_usd(2000, cfg) == pytest.approx(0.04)

    @pytest.mark.unit
    def test_cap_applied_when_positive(self):
        cfg = SlippageConfig(per_1k_contracts=0.10, max_per_contract=0.03)
        # 1000 -> 0.10 but capped to 0.03
        assert slippage_per_contract_usd(1000, cfg) == pytest.approx(0.03)


class TestApplyExecutionCosts:
    @pytest.mark.unit
    def test_buy_decomposes_spread_and_slippage(self):
        cfg = SlippageConfig(per_1k_contracts=0.02, max_per_contract=0.0)
        exec_px, spread_cost, slip_cost = apply_execution_costs(
            mid_price=0.50, spread=0.04, contracts=1000, side="buy", slippage_cfg=cfg
        )
        # ask = 0.52; impact per contract = 0.02; exec = 0.54
        assert exec_px == pytest.approx(0.54)
        assert spread_cost == pytest.approx(1000 * 0.02)
        assert slip_cost == pytest.approx(1000 * 0.02)

    @pytest.mark.unit
    def test_sell_decomposes_spread_and_slippage(self):
        cfg = SlippageConfig(per_1k_contracts=0.01, max_per_contract=0.0)
        exec_px, spread_cost, slip_cost = apply_execution_costs(
            mid_price=0.20, spread=0.04, contracts=2000, side="sell", slippage_cfg=cfg
        )
        # bid = 0.18; impact per contract = 0.02; exec = 0.16
        assert exec_px == pytest.approx(0.16)
        assert spread_cost == pytest.approx(2000 * 0.02)
        assert slip_cost == pytest.approx(2000 * 0.02)

    @pytest.mark.unit
    def test_exec_price_is_clamped_to_unit_interval(self):
        cfg = SlippageConfig(per_1k_contracts=10.0, max_per_contract=0.0)
        exec_px, _, _ = apply_execution_costs(
            mid_price=0.99, spread=0.10, contracts=1000, side="buy", slippage_cfg=cfg
        )
        assert 0.0 <= exec_px <= 1.0
        assert exec_px == pytest.approx(1.0)


class TestChooseClosePrice:
    @pytest.mark.unit
    def test_touch_policy_uses_boundary(self):
        px, src = choose_close_price(
            policy="touch",
            touched_lower=True,
            touched_upper=False,
            min_range=2500.0,
            max_range=3500.0,
            candle_close_price=3000.0,
        )
        assert px == pytest.approx(2500.0)
        assert src == "boundary"

    @pytest.mark.unit
    def test_pessimistic_lower_is_min_boundary_vs_close(self):
        px, src = choose_close_price(
            policy="pessimistic",
            touched_lower=True,
            touched_upper=False,
            min_range=2500.0,
            max_range=3500.0,
            candle_close_price=2400.0,
        )
        assert px == pytest.approx(2400.0)
        assert src == "worse_of_boundary_vs_close"

    @pytest.mark.unit
    def test_pessimistic_upper_is_max_boundary_vs_close(self):
        px, _ = choose_close_price(
            policy="pessimistic",
            touched_lower=False,
            touched_upper=True,
            min_range=2500.0,
            max_range=3500.0,
            candle_close_price=3600.0,
        )
        assert px == pytest.approx(3600.0)

    @pytest.mark.unit
    def test_next_candle_uses_next_close(self):
        px, src = choose_close_price(
            policy="next_candle",
            touched_lower=True,
            touched_upper=False,
            min_range=2500.0,
            max_range=3500.0,
            candle_close_price=3000.0,
            next_candle_close_price=3100.0,
        )
        assert px == pytest.approx(3100.0)
        assert src == "next_candle_close"


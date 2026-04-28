"""Unit tests for backtester.polymarket_execution (pure math)."""

import pytest

from backtester.polymarket_execution import (
    PolymarketFeeModel,
    SlippageConfig,
    apply_execution_costs,
    choose_close_price,
    polymarket_fee_per_contract,
    polymarket_taker_fee_usd,
    slippage_per_contract_usd,
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


class TestPolymarketFeeCurve:
    """Pin the published Polymarket dynamic taker-fee curve.

    Anchored to https://docs.polymarket.com/trading/fees: the Crypto category
    charges ``feeRate=0.072``, ``exponent=1``, with a published peak of $1.80
    on 100 shares at p=0.5.
    """

    @pytest.mark.unit
    def test_disabled_when_model_is_none(self):
        assert polymarket_taker_fee_usd(100, 0.5, None) == 0.0
        assert polymarket_fee_per_contract(0.5, None) == 0.0

    @pytest.mark.unit
    def test_crypto_peak_matches_published_table(self):
        model = PolymarketFeeModel.for_category("crypto")
        # 100 shares @ $0.50 -> $1.80 (the published peak).
        assert polymarket_taker_fee_usd(100, 0.50, model) == pytest.approx(1.80, rel=1e-6)
        # Symmetric around 0.5: 100 @ 0.10 == 100 @ 0.90 (per published table = $0.65).
        assert polymarket_taker_fee_usd(100, 0.10, model) == pytest.approx(0.648, rel=1e-3)
        assert polymarket_taker_fee_usd(100, 0.90, model) == pytest.approx(0.648, rel=1e-3)

    @pytest.mark.unit
    def test_geopolitics_is_fee_free(self):
        model = PolymarketFeeModel.for_category("geopolitics")
        assert not model.enabled
        assert polymarket_taker_fee_usd(1000, 0.5, model) == 0.0

    @pytest.mark.unit
    def test_zero_at_extremes(self):
        model = PolymarketFeeModel.for_category("crypto")
        assert polymarket_taker_fee_usd(100, 0.0, model) == 0.0
        assert polymarket_taker_fee_usd(100, 1.0, model) == 0.0

    @pytest.mark.unit
    def test_fee_folded_into_buy_exec_price(self):
        """A buy with fees enabled must pay strictly more per share than without."""
        cfg = SlippageConfig(per_1k_contracts=0.0, max_per_contract=0.0)
        model = PolymarketFeeModel.for_category("crypto")
        no_fee_px, _, _ = apply_execution_costs(
            mid_price=0.20, spread=0.0, contracts=1000, side="buy",
            slippage_cfg=cfg, fee_model=None,
        )
        with_fee_px, _, with_fee_slip = apply_execution_costs(
            mid_price=0.20, spread=0.0, contracts=1000, side="buy",
            slippage_cfg=cfg, fee_model=model,
        )
        assert with_fee_px > no_fee_px
        # The slippage bucket absorbs the fee delta when there's no other slip.
        assert with_fee_slip == pytest.approx(1000 * (with_fee_px - no_fee_px), rel=1e-6)

    @pytest.mark.unit
    def test_fee_folded_into_sell_exec_price(self):
        cfg = SlippageConfig(per_1k_contracts=0.0, max_per_contract=0.0)
        model = PolymarketFeeModel.for_category("crypto")
        no_fee_px, _, _ = apply_execution_costs(
            mid_price=0.80, spread=0.0, contracts=1000, side="sell",
            slippage_cfg=cfg, fee_model=None,
        )
        with_fee_px, _, _ = apply_execution_costs(
            mid_price=0.80, spread=0.0, contracts=1000, side="sell",
            slippage_cfg=cfg, fee_model=model,
        )
        assert with_fee_px < no_fee_px


class TestApplyExecutionCostsBookWalk:
    """Book-walk path: when L2 levels are supplied, fitted slippage is replaced
    with a true VWAP walk against the actual ladder."""

    @pytest.mark.unit
    def test_buy_uses_vwap_when_book_provided(self):
        cfg = SlippageConfig(per_1k_contracts=10.0, max_per_contract=1.0)  # huge fitted slip
        # Book with deep top, then a deeper level; total fill cost = 100*0.50 + 200*0.52
        bids = [{"price": 0.49, "size": 1000.0}]
        asks = [{"price": 0.50, "size": 100.0}, {"price": 0.52, "size": 1000.0}]
        exec_px, sp_cost, sl_cost = apply_execution_costs(
            mid_price=0.495, spread=0.10, contracts=300, side="buy",
            slippage_cfg=cfg, book_bids=bids, book_asks=asks, fee_model=None,
        )
        expected_vwap = (100 * 0.50 + 200 * 0.52) / 300
        assert exec_px == pytest.approx(expected_vwap, rel=1e-9)
        assert sp_cost == pytest.approx(300 * (0.50 - 0.495), rel=1e-9)
        assert sl_cost == pytest.approx(300 * (expected_vwap - 0.50), rel=1e-9)

    @pytest.mark.unit
    def test_sell_uses_vwap_when_book_provided(self):
        cfg = SlippageConfig(per_1k_contracts=10.0, max_per_contract=1.0)
        bids = [{"price": 0.50, "size": 100.0}, {"price": 0.48, "size": 1000.0}]
        asks = [{"price": 0.51, "size": 1000.0}]
        exec_px, sp_cost, sl_cost = apply_execution_costs(
            mid_price=0.505, spread=0.10, contracts=300, side="sell",
            slippage_cfg=cfg, book_bids=bids, book_asks=asks, fee_model=None,
        )
        expected_vwap = (100 * 0.50 + 200 * 0.48) / 300
        assert exec_px == pytest.approx(expected_vwap, rel=1e-9)
        assert sp_cost == pytest.approx(300 * (0.505 - 0.50), rel=1e-9)
        assert sl_cost == pytest.approx(300 * (0.50 - expected_vwap), rel=1e-9)

    @pytest.mark.unit
    def test_falls_back_when_book_too_thin(self):
        """If the side can't fill the requested size we fall back to parametric."""
        cfg = SlippageConfig(per_1k_contracts=0.02, max_per_contract=0.10)
        thin_asks = [{"price": 0.50, "size": 10.0}]  # nowhere near the 1000 ask we need
        exec_px, _sp, _sl = apply_execution_costs(
            mid_price=0.50, spread=0.04, contracts=1000, side="buy",
            slippage_cfg=cfg, book_bids=None, book_asks=thin_asks,
        )
        # Parametric path: ask = 0.52, slippage = 0.02/contract -> exec = 0.54
        assert exec_px == pytest.approx(0.54, rel=1e-9)

    @pytest.mark.unit
    def test_fee_added_after_book_walk(self):
        model = PolymarketFeeModel.for_category("crypto")
        cfg = SlippageConfig(per_1k_contracts=0.0, max_per_contract=0.0)
        bids = [{"price": 0.49, "size": 1000.0}]
        asks = [{"price": 0.50, "size": 1000.0}]
        no_fee_px, _, _ = apply_execution_costs(
            mid_price=0.495, spread=0.0, contracts=100, side="buy",
            slippage_cfg=cfg, book_bids=bids, book_asks=asks, fee_model=None,
        )
        with_fee_px, _, _ = apply_execution_costs(
            mid_price=0.495, spread=0.0, contracts=100, side="buy",
            slippage_cfg=cfg, book_bids=bids, book_asks=asks, fee_model=model,
        )
        assert with_fee_px > no_fee_px

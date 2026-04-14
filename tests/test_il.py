"""Unit tests for il.py — pure Uniswap V3 impermanent loss math."""

import math
import pytest

from il import tokens_from_liquidity, calculate_il_at_price


# ---------------------------------------------------------------------------
# tokens_from_liquidity
# ---------------------------------------------------------------------------

class TestTokensFromLiquidity:

    @pytest.mark.unit
    def test_price_below_range_all_token1(self):
        """When price is below range, LP holds only token1."""
        a0, a1 = tokens_from_liquidity(price=1800, price_low=2000, price_high=4000, liquidity=1e6)
        assert a0 == 0
        assert a1 > 0

    @pytest.mark.unit
    def test_price_above_range_all_token0(self):
        """When price is above range, LP holds only token0."""
        a0, a1 = tokens_from_liquidity(price=5000, price_low=2000, price_high=4000, liquidity=1e6)
        assert a0 > 0
        assert a1 == 0

    @pytest.mark.unit
    def test_price_inside_range_both_tokens(self):
        """When price is inside range, LP holds both tokens."""
        a0, a1 = tokens_from_liquidity(price=3000, price_low=2000, price_high=4000, liquidity=1e6)
        assert a0 > 0
        assert a1 > 0

    @pytest.mark.unit
    def test_price_at_lower_bound(self):
        """At exact lower bound, treat as below range (sp <= sl)."""
        a0, a1 = tokens_from_liquidity(price=2000, price_low=2000, price_high=4000, liquidity=1e6)
        assert a0 == 0
        assert a1 > 0

    @pytest.mark.unit
    def test_price_at_upper_bound(self):
        """At exact upper bound, sp == sh so sp < sh is False -> above-range branch."""
        a0, a1 = tokens_from_liquidity(price=4000, price_low=2000, price_high=4000, liquidity=1e6)
        assert a0 > 0
        assert a1 == 0

    @pytest.mark.unit
    def test_zero_liquidity_returns_zeros(self):
        a0, a1 = tokens_from_liquidity(price=3000, price_low=2000, price_high=4000, liquidity=0)
        assert a0 == 0
        assert a1 == 0

    @pytest.mark.unit
    def test_amounts_increase_with_liquidity(self):
        a0_lo, a1_lo = tokens_from_liquidity(price=3000, price_low=2000, price_high=4000, liquidity=1e6)
        a0_hi, a1_hi = tokens_from_liquidity(price=3000, price_low=2000, price_high=4000, liquidity=2e6)
        assert a0_hi == pytest.approx(2 * a0_lo, rel=1e-9)
        assert a1_hi == pytest.approx(2 * a1_lo, rel=1e-9)


# ---------------------------------------------------------------------------
# calculate_il_at_price
# ---------------------------------------------------------------------------

class TestCalculateIlAtPrice:

    @pytest.mark.unit
    def test_il_near_zero_at_entry(self):
        """IL should be approximately zero when target == entry."""
        t0, t1 = tokens_from_liquidity(3000, 2000, 4000, 1e6)
        result = calculate_il_at_price(
            entry_price=3000, token0_initial=t0, token1_initial=t1,
            target_price=3000, min_range=2000, max_range=4000,
        )
        assert result["IL"] == pytest.approx(0, abs=1.0)

    @pytest.mark.unit
    def test_il_negative_at_lower_bound(self):
        """IL is negative (loss vs HODL) when price moves to lower bound."""
        result = calculate_il_at_price(
            entry_price=3000, token0_initial=50000, token1_initial=16.67,
            target_price=2000, min_range=2000, max_range=4000,
        )
        assert result["IL"] < 0
        assert result["IL_pct"] < 0

    @pytest.mark.unit
    def test_il_negative_at_upper_bound(self):
        """IL is negative (loss vs HODL) when price moves to upper bound."""
        result = calculate_il_at_price(
            entry_price=3000, token0_initial=50000, token1_initial=16.67,
            target_price=4000, min_range=2000, max_range=4000,
        )
        assert result["IL"] < 0
        assert result["IL_pct"] < 0

    @pytest.mark.unit
    def test_return_keys(self):
        result = calculate_il_at_price(
            entry_price=3000, token0_initial=50000, token1_initial=16.67,
            target_price=3200, min_range=2000, max_range=4000,
        )
        assert set(result.keys()) == {"LP_value", "HODL_value", "IL", "IL_pct"}

    @pytest.mark.unit
    def test_hodl_value_independent_of_range(self):
        """HODL doesn't depend on the LP range."""
        r1 = calculate_il_at_price(3000, 50000, 16.67, 3500, 2000, 4000)
        r2 = calculate_il_at_price(3000, 50000, 16.67, 3500, 2500, 3500)
        assert r1["HODL_value"] == pytest.approx(r2["HODL_value"], rel=1e-9)

    @pytest.mark.unit
    def test_zero_initial_tokens_no_crash(self):
        result = calculate_il_at_price(
            entry_price=3000, token0_initial=0, token1_initial=0,
            target_price=3500, min_range=2000, max_range=4000,
        )
        assert result["IL_pct"] == 0.0

    @pytest.mark.unit
    def test_lp_value_positive_with_investment(self):
        result = calculate_il_at_price(
            entry_price=3000, token0_initial=50000, token1_initial=16.67,
            target_price=3000, min_range=2000, max_range=4000,
        )
        assert result["LP_value"] > 0
        assert result["HODL_value"] > 0

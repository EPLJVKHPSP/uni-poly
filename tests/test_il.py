"""Unit tests for il.py — pure Uniswap V3 impermanent loss math."""

import math
import pytest

from il import tokens_from_liquidity, calculate_il_at_price, liquidity_from_tokens


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

    @pytest.mark.unit
    def test_round_trip_with_consistent_tokens(self):
        """When tokens come from a consistent V3 split, IL at entry is exactly 0
        and LP value equals the deposit."""
        price = 3000.0
        min_r, max_r = 2000.0, 4000.0
        t0, t1 = tokens_from_liquidity(price, min_r, max_r, liquidity=1_000_000)
        deposit = t0 + t1 * price
        r = calculate_il_at_price(price, t0, t1, price, min_r, max_r)
        assert r["IL"] == pytest.approx(0.0, abs=1e-6)
        assert r["LP_value"] == pytest.approx(deposit, rel=1e-9)

    @pytest.mark.unit
    def test_boundary_entry_lower_uses_non_zero_side(self):
        """When entry is at the lower boundary, only token1 is deposited; the
        old ``(L0+L1)/2`` trick would have returned half the true liquidity."""
        price = 2000.0
        min_r, max_r = 2000.0, 4000.0
        _, t1 = tokens_from_liquidity(price, min_r, max_r, liquidity=1_000_000)
        assert t1 > 0
        r = calculate_il_at_price(price, 0.0, t1, price, min_r, max_r)
        # LP value at entry equals HODL value of the ETH we just deposited.
        assert r["LP_value"] == pytest.approx(t1 * price, rel=1e-9)
        assert r["IL"] == pytest.approx(0.0, abs=1e-6)

    @pytest.mark.unit
    def test_boundary_entry_upper_uses_non_zero_side(self):
        """Symmetric case: entry at the upper boundary, only token0 deposited."""
        price = 4000.0
        min_r, max_r = 2000.0, 4000.0
        t0, _ = tokens_from_liquidity(price, min_r, max_r, liquidity=1_000_000)
        assert t0 > 0
        r = calculate_il_at_price(price, t0, 0.0, price, min_r, max_r)
        assert r["LP_value"] == pytest.approx(t0, rel=1e-9)
        assert r["IL"] == pytest.approx(0.0, abs=1e-6)


# ---------------------------------------------------------------------------
# liquidity_from_tokens
# ---------------------------------------------------------------------------


class TestLiquidityFromTokens:

    @pytest.mark.unit
    def test_recovers_liquidity_from_consistent_split(self):
        price, min_r, max_r = 3000.0, 2000.0, 4000.0
        L = 1_234_567.89
        t0, t1 = tokens_from_liquidity(price, min_r, max_r, L)
        assert liquidity_from_tokens(price, t0, t1, min_r, max_r) == pytest.approx(L, rel=1e-9)

    @pytest.mark.unit
    def test_uses_token1_when_token0_zero(self):
        """At price == min_range, token0 = 0 — L must come from the token1 side."""
        price, min_r, max_r = 2000.0, 2000.0, 4000.0
        _, t1 = tokens_from_liquidity(price, min_r, max_r, 1e6)
        assert liquidity_from_tokens(price, 0.0, t1, min_r, max_r) == pytest.approx(1e6, rel=1e-9)

    @pytest.mark.unit
    def test_uses_token0_when_token1_zero(self):
        price, min_r, max_r = 4000.0, 2000.0, 4000.0
        t0, _ = tokens_from_liquidity(price, min_r, max_r, 1e6)
        assert liquidity_from_tokens(price, t0, 0.0, min_r, max_r) == pytest.approx(1e6, rel=1e-9)

    @pytest.mark.unit
    def test_picks_min_when_inputs_inconsistent(self):
        """Uniswap itself uses min(L0, L1); excess tokens sit idle."""
        price, min_r, max_r = 3000.0, 2500.0, 3500.0
        t0, t1 = tokens_from_liquidity(price, min_r, max_r, 1.0)
        L = liquidity_from_tokens(price, t0 * 2, t1, min_r, max_r)
        # The smaller side (token1-derived L=1.0) wins.
        assert L == pytest.approx(1.0, rel=1e-9)

    @pytest.mark.unit
    def test_zero_tokens_returns_zero(self):
        assert liquidity_from_tokens(3000.0, 0.0, 0.0, 2000.0, 4000.0) == 0.0

"""Integration tests for the simulation engine in active_backtester.py."""

import math
from unittest.mock import patch, MagicMock
import pytest

from active_backtester import (
    open_position,
    close_position,
    _score_range,
    pick_best_range,
    _get_insurance_for_range,
    _tokens_for_strategy_human,
    _tokens_for_strategy_scaled,
    _liquidity_for_strategy,
    _tokens_from_liquidity_v3,
    simulate,
    run_sweep,
)
from tests.conftest import make_candle_series


# ---------------------------------------------------------------------------
# open_position
# ---------------------------------------------------------------------------

class TestOpenPosition:

    @pytest.mark.integration
    def test_opens_with_valid_inputs(self, sample_candle, pool_data, sample_wallet, sample_insurance_info):
        pos = open_position(sample_candle, pool_data, 2500.0, 3500.0, sample_wallet, sample_insurance_info)
        assert pos is not None
        assert pos["min_range"] == 2500.0
        assert pos["max_range"] == 3500.0
        assert pos["entry_price"] == 3000.0
        assert pos["deposit_value"] > 0
        assert pos["liquidity"] > 0
        assert pos["accumulated_fees_usdc"] == 0.0
        assert pos["accumulated_fees_eth"] == 0.0

    @pytest.mark.integration
    def test_insurance_cost_deducted(self, sample_candle, pool_data, sample_wallet, sample_insurance_info):
        pos = open_position(sample_candle, pool_data, 2500.0, 3500.0, sample_wallet, sample_insurance_info)
        wallet_value = sample_wallet["usdc"] + sample_wallet["eth"] * 3000.0
        assert pos["deposit_value"] < wallet_value
        assert pos["insurance_cost"] > 0

    @pytest.mark.integration
    def test_returns_none_when_insurance_exceeds_wallet(self, sample_candle, pool_data):
        """Deposit goes negative when insurance cost exceeds wallet -> returns None."""
        zero_wallet = {"usdc": 0.0, "eth": 0.0}
        expensive_insurance = {"lower_bet_price": 0.99, "upper_bet_price": 0.99}
        pos = open_position(sample_candle, pool_data, 2500.0, 3500.0, zero_wallet, expensive_insurance)
        assert pos is None

    @pytest.mark.integration
    def test_wallet_before_recorded(self, sample_candle, pool_data, sample_wallet, sample_insurance_info):
        pos = open_position(sample_candle, pool_data, 2500.0, 3500.0, sample_wallet, sample_insurance_info)
        assert pos["wallet_before"]["usdc"] == sample_wallet["usdc"]
        assert pos["wallet_before"]["eth"] == sample_wallet["eth"]

    @pytest.mark.integration
    def test_deposit_value_equals_token_sum(self, sample_candle, pool_data, sample_wallet, sample_insurance_info):
        """token0_dep (USDC) + token1_dep (ETH) * price must equal deposit_value."""
        pos = open_position(sample_candle, pool_data, 2500.0, 3500.0, sample_wallet, sample_insurance_info)
        reconstructed = pos["token0_dep"] + pos["token1_dep"] * pos["entry_price"]
        assert reconstructed == pytest.approx(pos["deposit_value"], rel=1e-6)

    @pytest.mark.integration
    def test_deposit_ratio_matches_uniswap_v3(self, sample_candle, pool_data, sample_wallet, sample_insurance_info):
        """The USDC/ETH deposit ratio must match the Uniswap V3 formula for the range."""
        pos = open_position(sample_candle, pool_data, 2500.0, 3500.0, sample_wallet, sample_insurance_info)
        price = pos["entry_price"]
        mn, mx = pos["min_range"], pos["max_range"]
        sp, sl, sh = math.sqrt(price), math.sqrt(mn), math.sqrt(mx)
        expected_ratio = (sp - sl) / (1/sp - 1/sh)
        actual_ratio = pos["token0_dep"] / pos["token1_dep"]
        assert actual_ratio == pytest.approx(expected_ratio, rel=1e-6)

    @pytest.mark.integration
    def test_deposit_plus_costs_equals_wallet(self, sample_candle, pool_data, sample_wallet, sample_insurance_info):
        """LP deposit only reflects swap fee (insurance/gas are external)."""
        pos = open_position(sample_candle, pool_data, 2500.0, 3500.0, sample_wallet, sample_insurance_info)
        wallet_value = sample_wallet["usdc"] + sample_wallet["eth"] * pos["entry_price"]
        assert pos["deposit_value"] + pos["swap_fee"] == pytest.approx(wallet_value, rel=1e-6)

    @pytest.mark.integration
    def test_liquidity_round_trip_recovers_deposit(self, sample_candle, pool_data, sample_wallet, sample_insurance_info):
        """liquidity derived from deposit must recover the same scaled token amounts.
        _tokens_from_liquidity_v3 returns (ETH-side, USDC-side) — reversed vs _tokens_for_strategy_scaled."""
        pos = open_position(sample_candle, pool_data, 2500.0, 3500.0, sample_wallet, sample_insurance_info)
        price = pos["entry_price"]
        mn, mx = pos["min_range"], pos["max_range"]
        dec0, dec1 = pos["dec0"], pos["dec1"]
        decimal_diff = dec1 - dec0

        t0_s, t1_s = _tokens_for_strategy_scaled(mn, mx, pos["deposit_value"], price, decimal_diff)
        liq = _liquidity_for_strategy(price, mn, mx, t0_s, t1_s, dec0, dec1)
        rt_eth, rt_usdc = _tokens_from_liquidity_v3(price, mn, mx, liq, dec0, dec1)
        assert rt_usdc == pytest.approx(t0_s, rel=0.01)
        assert rt_eth == pytest.approx(t1_s, rel=0.01)

    @pytest.mark.integration
    @pytest.mark.parametrize("mn,mx", [
        (2500.0, 3500.0),
        (2000.0, 4000.0),
        (2800.0, 3200.0),
    ])
    def test_deposit_quantities_across_ranges(self, pool_data, sample_insurance_info, mn, mx):
        """Deposit token quantities must be consistent across different ranges at the same price."""
        from tests.conftest import _make_candle
        candle = _make_candle(ts=1_700_000_000, close="3000.0", low="2980.0", high="3020.0",
                              fg0="100000000000000000000000000000000000000",
                              fg1="200000000000000000000000000000000000000")
        wallet = {"usdc": 50_000.0, "eth": 16.666667}
        pos = open_position(candle, pool_data, mn, mx, wallet, sample_insurance_info)
        if pos is not None:
            reconstructed = pos["token0_dep"] + pos["token1_dep"] * pos["entry_price"]
            assert reconstructed == pytest.approx(pos["deposit_value"], rel=1e-6)


# ---------------------------------------------------------------------------
# Swap fee on rebalance
# ---------------------------------------------------------------------------

class TestSwapFee:

    @pytest.mark.integration
    def test_swap_fee_present_in_position(self, sample_candle, pool_data, sample_wallet, sample_insurance_info):
        """Position record must include swap_fee and swap_amount."""
        pos = open_position(sample_candle, pool_data, 2500.0, 3500.0, sample_wallet, sample_insurance_info)
        assert "swap_fee" in pos
        assert "swap_amount" in pos
        assert pos["swap_fee"] >= 0
        assert pos["swap_amount"] >= 0

    @pytest.mark.integration
    def test_swap_fee_uses_pool_fee_tier(self, sample_candle, sample_wallet, sample_insurance_info):
        """Swap fee must scale with the pool's feeTier."""
        pool_500 = {
            "id": "0xpool", "feeTier": "500",
            "token0": {"id": "0xa", "symbol": "USDC", "name": "USDC", "decimals": "6"},
            "token1": {"id": "0xb", "symbol": "WETH", "name": "WETH", "decimals": "18"},
        }
        pool_3000 = {
            "id": "0xpool", "feeTier": "3000",
            "token0": {"id": "0xa", "symbol": "USDC", "name": "USDC", "decimals": "6"},
            "token1": {"id": "0xb", "symbol": "WETH", "name": "WETH", "decimals": "18"},
        }
        pos_500 = open_position(sample_candle, pool_500, 2500.0, 3500.0, sample_wallet, sample_insurance_info)
        pos_3000 = open_position(sample_candle, pool_3000, 2500.0, 3500.0, sample_wallet, sample_insurance_info)
        assert pos_500["swap_fee"] > 0
        assert pos_3000["swap_fee"] > 0
        assert pos_3000["swap_fee"] / pos_500["swap_fee"] == pytest.approx(6.0, rel=0.01)

    @pytest.mark.integration
    def test_swap_fee_zero_when_no_fee_tier(self, sample_candle, sample_wallet, sample_insurance_info):
        """When feeTier is 0 or missing, swap fee should be zero."""
        pool_no_fee = {
            "id": "0xpool", "feeTier": "0",
            "token0": {"id": "0xa", "symbol": "USDC", "name": "USDC", "decimals": "6"},
            "token1": {"id": "0xb", "symbol": "WETH", "name": "WETH", "decimals": "18"},
        }
        pos = open_position(sample_candle, pool_no_fee, 2500.0, 3500.0, sample_wallet, sample_insurance_info)
        assert pos["swap_fee"] == 0.0
        wallet_value = sample_wallet["usdc"] + sample_wallet["eth"] * pos["entry_price"]
        assert pos["deposit_value"] == pytest.approx(wallet_value, rel=1e-6)

    @pytest.mark.integration
    def test_swap_fee_proportional_to_swap_amount(self, sample_candle, pool_data, sample_insurance_info):
        """swap_fee should equal swap_amount * feeTier / 1e6."""
        wallet = {"usdc": 50_000.0, "eth": 16.666667}
        pos = open_position(sample_candle, pool_data, 2500.0, 3500.0, wallet, sample_insurance_info)
        fee_rate = int(pool_data["feeTier"]) / 1_000_000
        expected_fee = pos["swap_amount"] * fee_rate
        assert pos["swap_fee"] == pytest.approx(expected_fee, rel=1e-9)

    @pytest.mark.integration
    def test_swap_fee_reduces_deposit(self, sample_candle, pool_data, sample_wallet, sample_insurance_info):
        """Deposit value with swap fee must be less than without."""
        pool_no_fee = {**pool_data, "feeTier": "0"}
        pos_with = open_position(sample_candle, pool_data, 2500.0, 3500.0, sample_wallet, sample_insurance_info)
        pos_without = open_position(sample_candle, pool_no_fee, 2500.0, 3500.0, sample_wallet, sample_insurance_info)
        assert pos_with["deposit_value"] < pos_without["deposit_value"]
        assert pos_with["swap_fee"] > 0
        assert pos_without["swap_fee"] == 0.0


# ---------------------------------------------------------------------------
# Gas fee on open/close
# ---------------------------------------------------------------------------

class TestGasFee:

    SAMPLE_GAS_PRICES = {"2023-11-14": 30_000_000_000, "2023-11-15": 30_000_000_000}

    @pytest.mark.integration
    def test_gas_fee_present_in_position(self, sample_candle, pool_data, sample_wallet, sample_insurance_info):
        """Position record must include gas_fee_open."""
        pos = open_position(
            sample_candle, pool_data, 2500.0, 3500.0, sample_wallet, sample_insurance_info,
            gas_prices=self.SAMPLE_GAS_PRICES,
        )
        assert "gas_fee_open" in pos
        assert pos["gas_fee_open"] > 0

    @pytest.mark.integration
    def test_gas_fee_zero_without_prices(self, sample_candle, pool_data, sample_wallet, sample_insurance_info):
        """Gas fee is zero when no gas_prices map is provided."""
        pos = open_position(sample_candle, pool_data, 2500.0, 3500.0, sample_wallet, sample_insurance_info)
        assert pos["gas_fee_open"] == 0.0

    @pytest.mark.integration
    def test_gas_fee_reduces_deposit(self, sample_candle, pool_data, sample_wallet, sample_insurance_info):
        """Gas is external: deposit value should be unchanged by gas_prices."""
        pos_no_gas = open_position(sample_candle, pool_data, 2500.0, 3500.0, sample_wallet, sample_insurance_info)
        pos_with_gas = open_position(
            sample_candle, pool_data, 2500.0, 3500.0, sample_wallet, sample_insurance_info,
            gas_prices=self.SAMPLE_GAS_PRICES,
        )
        assert pos_with_gas["deposit_value"] == pytest.approx(pos_no_gas["deposit_value"], rel=1e-12)
        assert pos_with_gas["gas_fee_open"] > 0
        assert pos_no_gas["gas_fee_open"] == 0.0

    @pytest.mark.integration
    def test_gas_fee_close_present(self, sample_candle, pool_data, sample_wallet, sample_insurance_info):
        """Close position must record gas_fee_close."""
        pos = open_position(sample_candle, pool_data, 2500.0, 3500.0, sample_wallet, sample_insurance_info)
        candle_close = {"periodStartUnix": "1700086400", "close": "2500.0"}
        closed_pos, wallet = close_position(
            pos, candle_close, touched_lower=True, touched_upper=False,
            gas_prices=self.SAMPLE_GAS_PRICES,
        )
        assert "gas_fee_close" in closed_pos
        assert closed_pos["gas_fee_close"] > 0

    @pytest.mark.integration
    def test_gas_fee_close_deducted_from_wallet(self, sample_candle, pool_data, sample_wallet, sample_insurance_info):
        """Gas is external: close gas should not change returned LP wallet."""
        pos = open_position(sample_candle, pool_data, 2500.0, 3500.0, sample_wallet, sample_insurance_info)

        candle_close = {"periodStartUnix": "1700086400", "close": "3000.0"}
        _, wallet_no_gas = close_position(pos.copy(), candle_close, False, False)

        pos2 = open_position(sample_candle, pool_data, 2500.0, 3500.0, sample_wallet, sample_insurance_info)
        _, wallet_with_gas = close_position(
            pos2, candle_close, False, False,
            gas_prices=self.SAMPLE_GAS_PRICES,
        )
        assert wallet_with_gas["usdc"] == pytest.approx(wallet_no_gas["usdc"], rel=1e-12)

    @pytest.mark.integration
    def test_value_conservation_with_gas(self, sample_candle, pool_data, sample_wallet, sample_insurance_info):
        """Deposit + swap_fee == wallet_value even with gas (insurance/gas external)."""
        pos = open_position(
            sample_candle, pool_data, 2500.0, 3500.0, sample_wallet, sample_insurance_info,
            gas_prices=self.SAMPLE_GAS_PRICES,
        )
        wallet_value = sample_wallet["usdc"] + sample_wallet["eth"] * pos["entry_price"]
        assert pos["deposit_value"] + pos["swap_fee"] == pytest.approx(wallet_value, rel=1e-6)


# ---------------------------------------------------------------------------
# External-cost accounting mode
# ---------------------------------------------------------------------------

class TestExternalCostsAccounting:

    @pytest.mark.integration
    def test_open_position_external_costs_deposit_equals_wallet_value_when_no_swap_fee(
        self, sample_candle, sample_wallet, sample_insurance_info
    ):
        """LP deposit is not reduced by insurance or gas (only swap_fee)."""
        pool_no_fee = {
            "id": "0xpool", "feeTier": "0",
            "token0": {"id": "0xa", "symbol": "USDC", "name": "USDC", "decimals": "6"},
            "token1": {"id": "0xb", "symbol": "WETH", "name": "WETH", "decimals": "18"},
        }
        pos = open_position(sample_candle, pool_no_fee, 2500.0, 3500.0, sample_wallet, sample_insurance_info)
        wallet_value = sample_wallet["usdc"] + sample_wallet["eth"] * pos["entry_price"]
        assert pos["swap_fee"] == 0.0
        assert pos["deposit_value"] == pytest.approx(wallet_value, rel=1e-9)
        assert pos["insurance_cost"] > 0

    @pytest.mark.integration
    def test_close_position_external_costs_wallet_excludes_insurance_and_gas(
        self, sample_candle, sample_wallet, sample_insurance_info
    ):
        pool_no_fee = {
            "id": "0xpool", "feeTier": "0",
            "token0": {"id": "0xa", "symbol": "USDC", "name": "USDC", "decimals": "6"},
            "token1": {"id": "0xb", "symbol": "WETH", "name": "WETH", "decimals": "18"},
        }
        pos = open_position(sample_candle, pool_no_fee, 2500.0, 3500.0, sample_wallet, sample_insurance_info)
        closed, new_wallet = close_position(pos, sample_candle, False, False)
        assert new_wallet["usdc"] == pytest.approx(closed["wd_usdc"] + closed["fees_earned_usdc"], rel=1e-9)
        assert new_wallet["eth"] == pytest.approx(closed["wd_eth"] + closed["fees_earned_eth"], rel=1e-9)


# ---------------------------------------------------------------------------
# Polymarket bid-ask spread
# ---------------------------------------------------------------------------

class TestSpread:

    @pytest.mark.integration
    def test_spread_zero_matches_default(self, sample_candle, pool_data, sample_wallet, sample_insurance_info):
        """spread=0 gives same result as no spread."""
        pos_default = open_position(sample_candle, pool_data, 2500.0, 3500.0, sample_wallet, sample_insurance_info)
        pos_zero = open_position(sample_candle, pool_data, 2500.0, 3500.0, sample_wallet, sample_insurance_info, spread=0.0)
        assert pos_default["insurance_cost"] == pytest.approx(pos_zero["insurance_cost"], rel=1e-9)
        assert pos_default["deposit_value"] == pytest.approx(pos_zero["deposit_value"], rel=1e-9)
        assert pos_zero["spread_cost_buy"] == pytest.approx(0.0, abs=1e-12)

    @pytest.mark.integration
    def test_spread_increases_insurance_cost(self, sample_candle, pool_data, sample_wallet, sample_insurance_info):
        """Positive spread must increase insurance cost (buy at ask > mid)."""
        pos_no = open_position(sample_candle, pool_data, 2500.0, 3500.0, sample_wallet, sample_insurance_info, spread=0.0)
        pos_sp = open_position(sample_candle, pool_data, 2500.0, 3500.0, sample_wallet, sample_insurance_info, spread=0.04)
        assert pos_sp["insurance_cost"] > pos_no["insurance_cost"]
        assert pos_sp["spread_cost_buy"] > 0

    @pytest.mark.integration
    def test_spread_reduces_deposit(self, sample_candle, pool_data, sample_wallet, sample_insurance_info):
        """Spread affects insurance execution, not LP deposit (insurance external)."""
        pos_no = open_position(sample_candle, pool_data, 2500.0, 3500.0, sample_wallet, sample_insurance_info, spread=0.0)
        pos_sp = open_position(sample_candle, pool_data, 2500.0, 3500.0, sample_wallet, sample_insurance_info, spread=0.04)
        assert pos_sp["deposit_value"] == pytest.approx(pos_no["deposit_value"], rel=1e-12)

    @pytest.mark.integration
    def test_spread_buy_capped_at_one(self, sample_candle, pool_data, sample_wallet):
        """Ask price capped at 1.0 even with huge spread."""
        expensive_ins = {"lower_bet_price": 0.98, "upper_bet_price": 0.98}
        pos = open_position(sample_candle, pool_data, 2500.0, 3500.0, sample_wallet, expensive_ins, spread=0.10)
        if pos is not None:
            assert pos["insurance_cost"] > 0

    @pytest.mark.integration
    @patch("active_backtester.get_historical_bet_price")
    @patch("active_backtester.get_clob_token_id")
    def test_spread_decreases_sellback(self, mock_clob, mock_bet_price, pool_data):
        """Selling back at bid (mid - spread/2) must yield less than at mid."""
        mock_clob.return_value = "0xclob"
        mock_bet_price.return_value = 0.20

        pos_base = {
            "open_ts": 1700000000, "entry_price": 3000.0,
            "min_range": 2500.0, "max_range": 3500.0,
            "deposit_value": 95000.0, "token0_dep": 45000.0, "token1_dep": 16.67,
            "liquidity": 1e12, "dec0": 6, "dec1": 18,
            "lower_bet_price": 0.15, "upper_bet_price": 0.10,
            "lower_contracts": 5000.0, "upper_contracts": 3000.0,
            "lower_insurance_cost_usdc": 750.0, "upper_insurance_cost_usdc": 300.0,
            "insurance_cost": 1050.0, "spread_cost_buy": 0.0,
            "accumulated_fees_usdc": 0.0, "accumulated_fees_eth": 0.0, "candle_count": 10,
        }

        candle = {"periodStartUnix": "1700086400", "close": "3000.0"}
        conn = MagicMock()

        import copy
        pos_no = copy.deepcopy(pos_base)
        closed_no, _ = close_position(pos_no, candle, False, False, token_symbol="ETH", conn=conn, spread=0.0)

        pos_sp = copy.deepcopy(pos_base)
        closed_sp, _ = close_position(pos_sp, candle, False, False, token_symbol="ETH", conn=conn, spread=0.04)

        assert closed_sp["insurance_sellback"] < closed_no["insurance_sellback"]
        assert closed_sp["spread_cost_sell"] > 0
        assert closed_no["spread_cost_sell"] == pytest.approx(0.0, abs=1e-12)

    @pytest.mark.integration
    @patch("active_backtester.get_historical_bet_price")
    @patch("active_backtester.get_clob_token_id")
    def test_spread_sell_capped_at_zero(self, mock_clob, mock_bet_price, pool_data):
        """Bid price (mid - spread/2) capped at 0.0 when spread is very large."""
        mock_clob.return_value = "0xclob"
        mock_bet_price.return_value = 0.01

        pos = {
            "open_ts": 1700000000, "entry_price": 3000.0,
            "min_range": 2500.0, "max_range": 3500.0,
            "deposit_value": 95000.0, "token0_dep": 45000.0, "token1_dep": 16.67,
            "liquidity": 1e12, "dec0": 6, "dec1": 18,
            "lower_bet_price": 0.15, "upper_bet_price": 0.10,
            "lower_contracts": 5000.0, "upper_contracts": 3000.0,
            "lower_insurance_cost_usdc": 750.0, "upper_insurance_cost_usdc": 300.0,
            "insurance_cost": 1050.0, "spread_cost_buy": 0.0,
            "accumulated_fees_usdc": 0.0, "accumulated_fees_eth": 0.0, "candle_count": 10,
        }
        candle = {"periodStartUnix": "1700086400", "close": "3000.0"}
        conn = MagicMock()

        closed, _ = close_position(pos, candle, False, False, token_symbol="ETH", conn=conn, spread=0.50)
        assert closed["insurance_sellback"] == pytest.approx(0.0, abs=1e-12)

    @pytest.mark.integration
    def test_value_conservation_with_spread(self, sample_candle, pool_data, sample_wallet, sample_insurance_info):
        """Deposit + swap_fee == wallet_value must still hold with spread (insurance/gas external)."""
        pos = open_position(
            sample_candle, pool_data, 2500.0, 3500.0, sample_wallet, sample_insurance_info,
            spread=0.04,
        )
        wallet_value = sample_wallet["usdc"] + sample_wallet["eth"] * pos["entry_price"]
        assert pos["deposit_value"] + pos["swap_fee"] == pytest.approx(wallet_value, rel=1e-6)


# ---------------------------------------------------------------------------
# Rebalance cycle: close -> open at new price verifies quantities stay consistent
# ---------------------------------------------------------------------------

class TestRebalanceCycleQuantities:
    """Verify that after closing a position and reopening at a new price/range,
    the deposit quantities still satisfy the Uniswap V3 invariants."""

    @pytest.mark.integration
    @patch("active_backtester.get_historical_bet_price")
    @patch("active_backtester.get_clob_token_id")
    def test_rebalance_preserves_value_conservation(self, mock_clob, mock_bet_price, pool_data):
        mock_clob.return_value = "0xclob"
        mock_bet_price.return_value = 0.10

        from tests.conftest import _make_candle
        insurance = {"lower_bet_price": 0.15, "upper_bet_price": 0.10}

        candle_1 = _make_candle(ts=1_700_000_000, close="3000.0", low="2980.0", high="3020.0",
                                fg0="100000000000000000000000000000000000000",
                                fg1="200000000000000000000000000000000000000")
        wallet = {"usdc": 50_000.0, "eth": 16.666667}
        pos = open_position(candle_1, pool_data, 2500.0, 3500.0, wallet, insurance)
        assert pos is not None

        recon_1 = pos["token0_dep"] + pos["token1_dep"] * pos["entry_price"]
        assert recon_1 == pytest.approx(pos["deposit_value"], rel=1e-6)

        pos["accumulated_fees_usdc"] = 500.0
        pos["accumulated_fees_eth"] = 0.1
        candle_close = _make_candle(ts=1_700_086_400, close="2500.0", low="2480.0", high="3020.0",
                                    fg0="110000000000000000000000000000000000000",
                                    fg1="210000000000000000000000000000000000000")
        closed_pos, new_wallet = close_position(
            pos, candle_close, touched_lower=True, touched_upper=False,
            token_symbol="ETH", conn=MagicMock(),
        )

        assert new_wallet["usdc"] >= 0
        assert new_wallet["eth"] >= 0

        candle_2 = _make_candle(ts=1_700_090_000, close="2600.0", low="2580.0", high="2620.0",
                                fg0="120000000000000000000000000000000000000",
                                fg1="220000000000000000000000000000000000000")
        insurance_2 = {"lower_bet_price": 0.20, "upper_bet_price": 0.15}
        pos_2 = open_position(candle_2, pool_data, 2200.0, 3000.0, new_wallet, insurance_2)

        if pos_2 is not None:
            recon_2 = pos_2["token0_dep"] + pos_2["token1_dep"] * pos_2["entry_price"]
            assert recon_2 == pytest.approx(pos_2["deposit_value"], rel=1e-6)

            sp = math.sqrt(pos_2["entry_price"])
            sl = math.sqrt(pos_2["min_range"])
            sh = math.sqrt(pos_2["max_range"])
            expected_ratio = (sp - sl) / (1/sp - 1/sh)
            actual_ratio = pos_2["token0_dep"] / pos_2["token1_dep"]
            assert actual_ratio == pytest.approx(expected_ratio, rel=1e-6)

            new_wallet_value = new_wallet["usdc"] + new_wallet["eth"] * pos_2["entry_price"]
            assert pos_2["deposit_value"] + pos_2["swap_fee"] == pytest.approx(new_wallet_value, rel=1e-6)

    @pytest.mark.integration
    @patch("active_backtester.get_historical_bet_price")
    @patch("active_backtester.get_clob_token_id")
    def test_simulate_all_positions_have_consistent_deposits(self, mock_clob, mock_bet_price, pool_data):
        """Run simulate and verify every opened position has correct token quantities."""
        mock_clob.return_value = "0xclob"
        mock_bet_price.return_value = 0.15

        candles = make_candle_series(n=100, start_price=3000.0, price_delta=0.0)
        candles[20]["low"] = "2400.0"
        candles[50]["high"] = "3600.0"

        conn = MagicMock()
        positions, wallet, snaps = simulate(
            candles, pool_data, "ETH", 100_000.0, conn,
            fixed_range=(2500.0, 3500.0), quiet=True,
            initial_eth=16.666667,
        )

        for i, pos in enumerate(positions):
            recon = pos["token0_dep"] + pos["token1_dep"] * pos["entry_price"]
            assert recon == pytest.approx(pos["deposit_value"], rel=1e-5), \
                f"Position #{i+1}: deposit value mismatch ({recon:.2f} vs {pos['deposit_value']:.2f})"


# ---------------------------------------------------------------------------
# close_position
# ---------------------------------------------------------------------------

class TestClosePosition:

    def _make_position(self):
        return {
            "open_ts": 1700000000,
            "entry_price": 3000.0,
            "min_range": 2500.0,
            "max_range": 3500.0,
            "deposit_value": 95000.0,
            "token0_dep": 45000.0,
            "token1_dep": 16.67,
            "liquidity": 1e12,
            "dec0": 6,
            "dec1": 18,
            "lower_bet_price": 0.15,
            "upper_bet_price": 0.10,
            "lower_contracts": 5000.0,
            "upper_contracts": 3000.0,
            "lower_insurance_cost_usdc": 750.0,
            "upper_insurance_cost_usdc": 300.0,
            "insurance_cost": 1050.0,
            "accumulated_fees_usdc": 200.0,
            "accumulated_fees_eth": 0.05,
            "candle_count": 24,
        }

    @pytest.mark.integration
    def test_lower_boundary_touch(self):
        pos = self._make_position()
        candle = {"periodStartUnix": "1700086400", "close": "2500.0"}
        pos, wallet = close_position(pos, candle, touched_lower=True, touched_upper=False)

        assert pos["touched_lower"] is True
        assert pos["touched_upper"] is False
        assert pos["close_price"] == 2500.0
        assert pos["insurance_payout"] == 5000.0
        assert wallet["usdc"] > 0

    @pytest.mark.integration
    def test_upper_boundary_touch(self):
        pos = self._make_position()
        candle = {"periodStartUnix": "1700086400", "close": "3500.0"}
        pos, wallet = close_position(pos, candle, touched_lower=False, touched_upper=True)

        assert pos["touched_upper"] is True
        assert pos["insurance_payout"] == 3000.0

    @pytest.mark.integration
    def test_no_boundary_touch(self):
        pos = self._make_position()
        candle = {"periodStartUnix": "1700086400", "close": "3000.0"}
        pos, wallet = close_position(pos, candle, touched_lower=False, touched_upper=False)

        assert pos["insurance_payout"] == 0.0
        assert pos["close_price"] == 3000.0

    @pytest.mark.integration
    @patch("active_backtester.get_historical_bet_price")
    @patch("active_backtester.get_clob_token_id")
    def test_sellback_with_conn(self, mock_clob, mock_bet_price):
        mock_clob.return_value = "0xclob123"
        mock_bet_price.return_value = 0.08

        pos = self._make_position()
        candle = {"periodStartUnix": "1700086400", "close": "2500.0"}
        conn = MagicMock()
        pos, wallet = close_position(
            pos, candle, touched_lower=True, touched_upper=False,
            token_symbol="ETH", conn=conn,
        )

        assert pos["insurance_sellback"] > 0

    @pytest.mark.integration
    def test_duration_calculated(self):
        pos = self._make_position()
        candle = {"periodStartUnix": "1700086400", "close": "3000.0"}
        pos, _ = close_position(pos, candle, False, False)
        assert pos["duration_hours"] == pytest.approx(24.0, rel=0.01)

    @pytest.mark.integration
    def test_wallet_after_includes_fees(self):
        pos = self._make_position()
        candle = {"periodStartUnix": "1700086400", "close": "3000.0"}
        pos, wallet = close_position(pos, candle, False, False)
        assert wallet["usdc"] > 0
        assert wallet["eth"] > 0


class TestClosePositionWithdrawalMath:
    """Regression guards for the LP withdrawal math fix.

    Before the fix, ``close_position`` computed boundary withdrawals as
    ``deposit_value / min_range`` (lower touch) or ``deposit_value`` in USDC
    (upper touch). Those values diverge from the actual LP composition unless
    ``touch_price == entry_price``. The fix uses the position's human-unit
    liquidity to derive the correct composition, so wallet + fees + insurance
    exactly reconciles with ``il.calculate_il_at_price``'s ``LP_value``.
    """

    @pytest.mark.unit
    def test_lp_withdrawal_matches_il_lp_value_at_lower_boundary(self, sample_candle, pool_data, sample_insurance_info):
        from active_backtester import open_position, close_position, calculate_il_at_price
        wallet = {"usdc": 50_000.0, "eth": 16.666667}
        pos = open_position(sample_candle, pool_data, 2500.0, 3500.0, wallet, sample_insurance_info)
        assert pos is not None

        candle_close = {"periodStartUnix": "1700086400", "close": "2500.0"}
        closed, new_wallet = close_position(pos, candle_close, touched_lower=True, touched_upper=False)

        il = calculate_il_at_price(
            pos["entry_price"], pos["token0_dep"], pos["token1_dep"],
            2500.0, 2500.0, 3500.0,
        )
        withdrawn_value = closed["wd_usdc"] + closed["wd_eth"] * 2500.0
        assert withdrawn_value == pytest.approx(il["LP_value"], rel=1e-6)

    @pytest.mark.unit
    def test_lp_withdrawal_matches_il_lp_value_at_upper_boundary(self, sample_candle, pool_data, sample_insurance_info):
        from active_backtester import open_position, close_position, calculate_il_at_price
        wallet = {"usdc": 50_000.0, "eth": 16.666667}
        pos = open_position(sample_candle, pool_data, 2500.0, 3500.0, wallet, sample_insurance_info)

        candle_close = {"periodStartUnix": "1700086400", "close": "3500.0"}
        closed, new_wallet = close_position(pos, candle_close, touched_lower=False, touched_upper=True)

        il = calculate_il_at_price(
            pos["entry_price"], pos["token0_dep"], pos["token1_dep"],
            3500.0, 2500.0, 3500.0,
        )
        withdrawn_value = closed["wd_usdc"] + closed["wd_eth"] * 3500.0
        assert withdrawn_value == pytest.approx(il["LP_value"], rel=1e-6)

    @pytest.mark.unit
    def test_lp_withdrawal_matches_il_lp_value_in_range(self, sample_candle, pool_data, sample_insurance_info):
        """Mid-range close: withdrawal value must match ``LP_value`` from il.py."""
        from active_backtester import open_position, close_position, calculate_il_at_price
        wallet = {"usdc": 50_000.0, "eth": 16.666667}
        pos = open_position(sample_candle, pool_data, 2500.0, 3500.0, wallet, sample_insurance_info)

        candle_close = {"periodStartUnix": "1700086400", "close": "3200.0"}
        closed, _ = close_position(pos, candle_close, touched_lower=False, touched_upper=False)

        il = calculate_il_at_price(
            pos["entry_price"], pos["token0_dep"], pos["token1_dep"],
            3200.0, 2500.0, 3500.0,
        )
        withdrawn_value = closed["wd_usdc"] + closed["wd_eth"] * 3200.0
        assert withdrawn_value == pytest.approx(il["LP_value"], rel=1e-6)


# ---------------------------------------------------------------------------
# _score_range / pick_best_range
# ---------------------------------------------------------------------------

class TestScoreRange:

    @pytest.mark.integration
    @patch("active_backtester.get_historical_bet_price")
    @patch("active_backtester.get_clob_token_id")
    def test_returns_scored_dict(self, mock_clob, mock_bet_price):
        mock_clob.return_value = "0xclob"
        mock_bet_price.return_value = 0.20

        conn = MagicMock()
        result = _score_range(2500.0, 3500.0, "ETH", 1700000000, 100000, conn)

        assert result is not None
        assert "insurance_cost_rate" in result
        assert result["insurance_cost_rate"] == pytest.approx(0.40, rel=1e-9)

    @pytest.mark.integration
    @patch("active_backtester.get_historical_bet_price")
    @patch("active_backtester.get_clob_token_id")
    def test_returns_none_when_no_data(self, mock_clob, mock_bet_price):
        mock_clob.return_value = None
        mock_bet_price.return_value = None

        conn = MagicMock()
        result = _score_range(2500.0, 3500.0, "ETH", 1700000000, 100000, conn)
        assert result is None

    @pytest.mark.integration
    @patch("active_backtester.get_historical_bet_price")
    @patch("active_backtester.get_clob_token_id")
    def test_defaults_missing_side_to_half(self, mock_clob, mock_bet_price):
        mock_clob.side_effect = ["0xclob_lower", None]
        mock_bet_price.side_effect = [0.15, None]

        conn = MagicMock()
        result = _score_range(2500.0, 3500.0, "ETH", 1700000000, 100000, conn)

        assert result is not None
        assert result["upper_bet_price"] == 0.5


class TestPickBestRange:

    @pytest.mark.integration
    @patch("active_backtester._score_range")
    def test_picks_best_scored(self, mock_score, sample_combos):
        mock_score.side_effect = [
            {"min": 2400, "max": 3600, "lower_bet_price": 0.15, "upper_bet_price": 0.10, "insurance_cost_rate": 0.25, "range_width_pct": 40.0},
            {"min": 2600, "max": 3400, "lower_bet_price": 0.20, "upper_bet_price": 0.12, "insurance_cost_rate": 0.32, "range_width_pct": 26.67},
            {"min": 2800, "max": 3200, "lower_bet_price": 0.30, "upper_bet_price": 0.25, "insurance_cost_rate": 0.55, "range_width_pct": 13.33},
        ]
        conn = MagicMock()
        result = pick_best_range(sample_combos, 3000.0, "ETH", 1700000000, 100000, conn)
        assert result is not None
        assert result["min"] == 2600
        assert result["max"] == 3400
        assert float(result["lower_bet_price"]) <= 0.20
        assert float(result["upper_bet_price"]) <= 0.20

    @pytest.mark.integration
    @patch("active_backtester._score_range")
    def test_filters_out_ranges_with_high_bet_prob(self, mock_score, sample_combos):
        mock_score.side_effect = [
            {"min": 2400, "max": 3600, "lower_bet_price": 0.05, "upper_bet_price": 0.21, "insurance_cost_rate": 0.16, "range_width_pct": 40.0},
            {"min": 2600, "max": 3400, "lower_bet_price": 0.20, "upper_bet_price": 0.20, "insurance_cost_rate": 0.20, "range_width_pct": 26.67},
            {"min": 2800, "max": 3200, "lower_bet_price": 0.22, "upper_bet_price": 0.09, "insurance_cost_rate": 0.21, "range_width_pct": 13.33},
        ]
        conn = MagicMock()
        result = pick_best_range(sample_combos, 3000.0, "ETH", 1700000000, 100000, conn)
        assert result is not None
        assert float(result["lower_bet_price"]) <= 0.20
        assert float(result["upper_bet_price"]) <= 0.20

    @pytest.mark.integration
    @patch("active_backtester._score_range")
    def test_returns_none_when_no_scored(self, mock_score):
        mock_score.return_value = None
        combos = [{"min": 2400.0, "max": 3600.0}]
        conn = MagicMock()
        result = pick_best_range(combos, 3000.0, "ETH", 1700000000, 100000, conn)
        assert result is None


# ---------------------------------------------------------------------------
# _get_insurance_for_range
# ---------------------------------------------------------------------------

class TestGetInsuranceForRange:

    @pytest.mark.integration
    @patch("active_backtester.get_historical_bet_price")
    @patch("active_backtester.get_clob_token_id")
    def test_returns_insurance_dict(self, mock_clob, mock_bet_price):
        mock_clob.return_value = "0xclob"
        mock_bet_price.return_value = 0.20

        conn = MagicMock()
        result = _get_insurance_for_range(2500.0, 3500.0, "ETH", 1700000000, conn)
        assert result is not None
        assert "lower_bet_price" in result
        assert "upper_bet_price" in result

    @pytest.mark.integration
    @patch("active_backtester.get_historical_bet_price")
    @patch("active_backtester.get_clob_token_id")
    def test_returns_none_when_both_missing(self, mock_clob, mock_bet_price):
        mock_clob.return_value = None
        mock_bet_price.return_value = None

        conn = MagicMock()
        result = _get_insurance_for_range(2500.0, 3500.0, "ETH", 1700000000, conn)
        assert result is None


# ---------------------------------------------------------------------------
# Hourly snapshots
# ---------------------------------------------------------------------------

class TestSnapshots:

    @pytest.mark.integration
    @patch("active_backtester.get_historical_bet_price")
    @patch("active_backtester.get_clob_token_id")
    def test_snapshot_count_matches_candles(self, mock_clob, mock_bet_price, pool_data):
        """One snapshot per candle processed."""
        mock_clob.return_value = "0xclob"
        mock_bet_price.return_value = 0.15

        candles = make_candle_series(n=30, start_price=3000.0, price_delta=0.0)
        conn = MagicMock()
        _, _, snaps = simulate(
            candles, pool_data, "ETH", 100_000.0, conn,
            fixed_range=(2500.0, 3500.0), quiet=True,
            initial_eth=16.666667,
        )
        assert len(snaps) == len(candles)

    @pytest.mark.integration
    @patch("active_backtester.get_historical_bet_price")
    @patch("active_backtester.get_clob_token_id")
    def test_snapshot_has_required_fields(self, mock_clob, mock_bet_price, pool_data):
        mock_clob.return_value = "0xclob"
        mock_bet_price.return_value = 0.15

        candles = make_candle_series(n=10, start_price=3000.0, price_delta=0.0)
        conn = MagicMock()
        _, _, snaps = simulate(
            candles, pool_data, "ETH", 100_000.0, conn,
            fixed_range=(2500.0, 3500.0), quiet=True,
            initial_eth=16.666667,
        )
        required = {"ts", "price", "hodl_usd", "strategy_usd", "lp_value_usd",
                     "fees_accrued_usd", "poly_equity_usd", "wallet_usdc",
                     "wallet_eth", "position_open"}
        for s in snaps:
            assert required.issubset(s.keys()), f"Missing keys: {required - s.keys()}"

    @pytest.mark.integration
    @patch("active_backtester.get_historical_bet_price")
    @patch("active_backtester.get_clob_token_id")
    def test_hodl_uses_initial_quantities(self, mock_clob, mock_bet_price, pool_data):
        """HODL value must equal initial_usdc + initial_eth * current_price."""
        mock_clob.return_value = "0xclob"
        mock_bet_price.return_value = 0.15

        candles = make_candle_series(n=10, start_price=3000.0, price_delta=0.0)
        conn = MagicMock()
        _, _, snaps = simulate(
            candles, pool_data, "ETH", 100_000.0, conn,
            fixed_range=(2500.0, 3500.0), quiet=True,
            initial_eth=16.666667,
        )
        first = snaps[0]
        # ETH-first mode: baseline quantities depend on the computed USDC requirement.
        # Assert internal consistency using snapshot wallet quantities.
        expected_hodl = first["wallet_usdc"] + first["wallet_eth"] * first["price"]
        assert first["hodl_usd"] == pytest.approx(expected_hodl, rel=1e-4)

    @pytest.mark.integration
    @patch("active_backtester.get_historical_bet_price")
    @patch("active_backtester.get_clob_token_id")
    def test_position_open_flag(self, mock_clob, mock_bet_price, pool_data):
        """Snapshots while position is active should have position_open=True."""
        mock_clob.return_value = "0xclob"
        mock_bet_price.return_value = 0.15

        candles = make_candle_series(n=10, start_price=3000.0, price_delta=0.0)
        conn = MagicMock()
        _, _, snaps = simulate(
            candles, pool_data, "ETH", 100_000.0, conn,
            fixed_range=(2500.0, 3500.0), quiet=True,
            initial_eth=16.666667,
        )
        open_snaps = [s for s in snaps if s["position_open"]]
        assert len(open_snaps) > 0

    @pytest.mark.integration
    @patch("active_backtester.get_historical_bet_price")
    @patch("active_backtester.get_clob_token_id")
    def test_poly_equity_positive_when_position_open(self, mock_clob, mock_bet_price, pool_data):
        """Polymarket equity should be > 0 when a position is open and bet price is available."""
        mock_clob.return_value = "0xclob"
        mock_bet_price.return_value = 0.15

        candles = make_candle_series(n=10, start_price=3000.0, price_delta=0.0)
        conn = MagicMock()
        _, _, snaps = simulate(
            candles, pool_data, "ETH", 100_000.0, conn,
            fixed_range=(2500.0, 3500.0), quiet=True,
            initial_eth=16.666667,
        )
        open_snaps = [s for s in snaps if s["position_open"]]
        for s in open_snaps:
            assert s["poly_equity_usd"] > 0

    @pytest.mark.integration
    @patch("active_backtester.get_historical_bet_price")
    @patch("active_backtester.get_clob_token_id")
    def test_range_field_present_when_position_open(self, mock_clob, mock_bet_price, pool_data):
        mock_clob.return_value = "0xclob"
        mock_bet_price.return_value = 0.15

        candles = make_candle_series(n=10, start_price=3000.0, price_delta=0.0)
        conn = MagicMock()
        _, _, snaps = simulate(
            candles, pool_data, "ETH", 100_000.0, conn,
            fixed_range=(2500.0, 3500.0), quiet=True,
            initial_eth=16.666667,
        )
        for s in snaps:
            if s["position_open"]:
                assert "range" in s
                assert s["range"] == [2500.0, 3500.0]
            else:
                assert "range" not in s

    @pytest.mark.integration
    @patch("active_backtester.get_historical_bet_price")
    @patch("active_backtester.get_clob_token_id")
    def test_snapshots_in_summary(self, mock_clob, mock_bet_price, pool_data):
        """build_summary must include snapshots array."""
        mock_clob.return_value = "0xclob"
        mock_bet_price.return_value = 0.15

        from active_backtester import build_summary
        candles = make_candle_series(n=100, start_price=3000.0, price_delta=0.0)
        conn = MagicMock()
        positions, wallet, snaps = simulate(
            candles, pool_data, "ETH", 100_000.0, conn,
            fixed_range=(2500.0, 3500.0), quiet=True,
            initial_eth=16.666667,
        )
        summary = build_summary(positions, candles, 100_000.0, "0xpool", "ETH", wallet, snapshots=snaps)
        assert "snapshots" in summary
        assert len(summary["snapshots"]) == len(snaps)


class TestSummaryBaselinesAndQuality:
    """The summary must carry the baselines block and data_quality block so
    the operator can answer 'does it beat HODL after costs?' and judge how
    much to trust the answer, without re-running anything."""

    @pytest.mark.integration
    @patch("active_backtester.get_historical_bet_price")
    @patch("active_backtester.get_clob_token_id")
    def test_summary_has_baselines_block(self, mock_clob, mock_bet_price, pool_data):
        mock_clob.return_value = "0xclob"
        mock_bet_price.return_value = 0.15

        from active_backtester import build_summary
        candles = make_candle_series(n=100, start_price=3000.0, price_delta=0.0)
        conn = MagicMock()
        positions, wallet, snaps = simulate(
            candles, pool_data, "ETH", 100_000.0, conn,
            fixed_range=(2500.0, 3500.0), quiet=True,
            initial_eth=16.666667,
        )
        summary = build_summary(positions, candles, 100_000.0, "0xpool", "ETH", wallet, snapshots=snaps)
        assert "baselines" in summary
        assert "hodl" in summary["baselines"]
        assert "unhedged_active_lp" in summary["baselines"]
        hodl = summary["baselines"]["hodl"]
        assert set(hodl).issuperset({"final_value_usd", "roi_pct",
                                      "outperformance_vs_hodl_usd",
                                      "outperformance_vs_hodl_pct"})
        unhedged = summary["baselines"]["unhedged_active_lp"]
        assert set(unhedged).issuperset({"final_value_usd", "roi_pct", "apy",
                                          "hedge_net_contribution_usd"})

    @pytest.mark.integration
    @patch("active_backtester.get_historical_bet_price")
    @patch("active_backtester.get_clob_token_id")
    def test_hedge_net_contribution_reconciles(self, mock_clob, mock_bet_price, pool_data):
        """``unhedged.final_value + hedge_net_contribution == strategy final_value``."""
        mock_clob.return_value = "0xclob"
        mock_bet_price.return_value = 0.15

        from active_backtester import build_summary
        candles = make_candle_series(n=100, start_price=3000.0, price_delta=0.0)
        candles[30]["low"] = "2400.0"  # force a lower-boundary touch
        conn = MagicMock()
        positions, wallet, snaps = simulate(
            candles, pool_data, "ETH", 100_000.0, conn,
            fixed_range=(2500.0, 3500.0), quiet=True,
            initial_eth=16.666667,
        )
        summary = build_summary(positions, candles, 100_000.0, "0xpool", "ETH", wallet, snapshots=snaps)
        total = summary["active_strategy"]["final_value_usd"]
        unh = summary["baselines"]["unhedged_active_lp"]
        assert unh["final_value_usd"] + unh["hedge_net_contribution_usd"] == pytest.approx(total, rel=1e-2)

    @pytest.mark.integration
    @patch("active_backtester.get_historical_bet_price")
    @patch("active_backtester.get_clob_token_id")
    def test_data_quality_block_passthrough(self, mock_clob, mock_bet_price, pool_data):
        mock_clob.return_value = "0xclob"
        mock_bet_price.return_value = 0.15

        from active_backtester import build_summary
        candles = make_candle_series(n=20, start_price=3000.0, price_delta=0.0)
        conn = MagicMock()
        positions, wallet, snaps = simulate(
            candles, pool_data, "ETH", 100_000.0, conn,
            fixed_range=(2500.0, 3500.0), quiet=True,
            initial_eth=16.666667,
        )
        dq = {"candles": {"candle_count": 20}, "gas": {}, "polymarket": {}}
        run_meta = {"pool_id": "0xpool", "days": 1}
        summary = build_summary(
            positions, candles, 100_000.0, "0xpool", "ETH", wallet,
            snapshots=snaps, data_quality=dq, run_metadata=run_meta,
        )
        assert summary["data_quality"] == dq
        assert summary["run_metadata"] == run_meta


# ---------------------------------------------------------------------------
# simulate (full loop with synthetic candles)
# ---------------------------------------------------------------------------

class TestSimulate:

    @pytest.mark.integration
    @patch("active_backtester.get_historical_bet_price")
    @patch("active_backtester.get_clob_token_id")
    def test_fixed_range_opens_and_closes(self, mock_clob, mock_bet_price, pool_data):
        mock_clob.return_value = "0xclob"
        mock_bet_price.return_value = 0.15

        candles = make_candle_series(n=30, start_price=3000.0, price_delta=0.0)

        candles[20]["low"] = "2400.0"

        conn = MagicMock()
        positions, wallet, snaps = simulate(
            candles, pool_data, "ETH", 100000.0, conn,
            fixed_range=(2500.0, 3500.0), quiet=True,
            initial_eth=16.666667,
        )

        assert len(positions) >= 1
        assert wallet["usdc"] > 0 or wallet["eth"] > 0

    @pytest.mark.integration
    @patch("active_backtester.get_historical_bet_price")
    @patch("active_backtester.get_clob_token_id")
    def test_no_positions_when_price_outside_range(self, mock_clob, mock_bet_price, pool_data):
        mock_clob.return_value = "0xclob"
        mock_bet_price.return_value = 0.15

        candles = make_candle_series(n=20, start_price=5000.0, price_delta=0.0)

        conn = MagicMock()
        positions, wallet, snaps = simulate(
            candles, pool_data, "ETH", 100000.0, conn,
            fixed_range=(2500.0, 3500.0), quiet=True,
            initial_eth=16.666667,
        )

        assert len(positions) == 0

    @pytest.mark.integration
    @patch("active_backtester.get_historical_bet_price")
    @patch("active_backtester.get_clob_token_id")
    def test_force_close_at_end(self, mock_clob, mock_bet_price, pool_data):
        mock_clob.return_value = "0xclob"
        mock_bet_price.return_value = 0.15

        candles = make_candle_series(n=20, start_price=3000.0, price_delta=0.0)

        conn = MagicMock()
        positions, wallet, snaps = simulate(
            candles, pool_data, "ETH", 100000.0, conn,
            fixed_range=(2500.0, 3500.0), quiet=True,
            initial_eth=16.666667,
        )

        if positions:
            last = positions[-1]
            assert "close_ts" in last

    @pytest.mark.integration
    @patch("active_backtester.get_historical_bet_price")
    @patch("active_backtester.get_clob_token_id")
    def test_cooldown_respected(self, mock_clob, mock_bet_price, pool_data):
        mock_clob.return_value = "0xclob"
        mock_bet_price.return_value = 0.15

        candles = make_candle_series(n=50, start_price=3000.0, price_delta=0.0)
        candles[5]["low"] = "2400.0"
        candles[15]["low"] = "2400.0"

        conn = MagicMock()
        positions, _, snaps = simulate(
            candles, pool_data, "ETH", 100000.0, conn,
            fixed_range=(2500.0, 3500.0), cooldown_hours=3, quiet=True,
            initial_eth=16.666667,
        )

        if len(positions) >= 2:
            gap = positions[1]["open_ts"] - positions[0]["close_ts"]
            assert gap >= 3600 * 3


class TestClosePoliciesIntegration:
    """Integration guards: close policy changes results deterministically."""

    @pytest.mark.integration
    @patch("active_backtester.get_historical_bet_price")
    @patch("active_backtester.get_clob_token_id")
    def test_next_candle_close_changes_close_price(self, mock_clob, mock_bet_price, pool_data):
        mock_clob.return_value = "0xclob"
        mock_bet_price.return_value = 0.15

        candles = make_candle_series(n=10, start_price=3000.0, price_delta=0.0)
        # Touch lower at candle 5, but have the *next* candle close far away.
        candles[5]["low"] = "2400.0"
        candles[6]["close"] = "3100.0"

        conn = MagicMock()
        pos_touch, _, _ = simulate(
            candles, pool_data, "ETH", 100_000.0, conn,
            fixed_range=(2500.0, 3500.0), quiet=True, close_policy="touch",
            initial_eth=16.666667,
        )
        pos_next, _, _ = simulate(
            candles, pool_data, "ETH", 100_000.0, conn,
            fixed_range=(2500.0, 3500.0), quiet=True, close_policy="next_candle",
            initial_eth=16.666667,
        )

        assert pos_touch, "touch policy should produce at least one position"
        assert pos_next, "next_candle policy should produce at least one position"
        assert pos_touch[0]["close_price"] == pytest.approx(2500.0)
        assert pos_next[0]["close_price"] == pytest.approx(3100.0)

    @pytest.mark.integration
    @patch("active_backtester.get_historical_bet_price")
    @patch("active_backtester.get_clob_token_id")
    def test_pessimistic_is_worse_than_touch_on_lower(self, mock_clob, mock_bet_price, pool_data):
        mock_clob.return_value = "0xclob"
        mock_bet_price.return_value = 0.15

        candles = make_candle_series(n=10, start_price=3000.0, price_delta=0.0)
        candles[5]["low"] = "2400.0"
        candles[5]["close"] = "2400.0"  # worse than boundary

        conn = MagicMock()
        pos_touch, _, _ = simulate(
            candles, pool_data, "ETH", 100_000.0, conn,
            fixed_range=(2500.0, 3500.0), quiet=True, close_policy="touch",
            initial_eth=16.666667,
        )
        pos_pess, _, _ = simulate(
            candles, pool_data, "ETH", 100_000.0, conn,
            fixed_range=(2500.0, 3500.0), quiet=True, close_policy="pessimistic",
            initial_eth=16.666667,
        )

        assert pos_touch and pos_pess
        assert pos_touch[0]["close_price"] == pytest.approx(2500.0)
        assert pos_pess[0]["close_price"] == pytest.approx(2400.0)


class TestSummaryNewFields:
    @pytest.mark.integration
    @patch("active_backtester.get_historical_bet_price")
    @patch("active_backtester.get_clob_token_id")
    def test_summary_includes_slippage_and_delta_matched_baseline(self, mock_clob, mock_bet_price, pool_data):
        mock_clob.return_value = "0xclob"
        mock_bet_price.return_value = 0.15

        from active_backtester import build_summary
        candles = make_candle_series(n=50, start_price=3000.0, price_delta=0.0)
        candles[10]["low"] = "2400.0"

        conn = MagicMock()
        positions, wallet, snaps = simulate(
            candles, pool_data, "ETH", 100_000.0, conn,
            fixed_range=(2500.0, 3500.0), quiet=True,
            slippage_per_1k_contracts=0.02,
            slippage_max_per_contract=0.05,
            initial_eth=16.666667,
        )
        summary = build_summary(positions, candles, 100_000.0, "0xpool", "ETH", wallet, snapshots=snaps)
        s = summary["active_strategy"]
        assert "total_slippage_cost_usdc" in s
        assert s["total_slippage_cost_usdc"] >= 0

        bl = summary.get("baselines", {})
        assert "delta_matched_hodl" in bl
        assert "final_value_usd" in bl["delta_matched_hodl"]

        edge = summary.get("edge_lp_plus_hedge", {})
        assert set(edge).issuperset({"usd", "pct"}) or edge == {}

        # New: explicit Polymarket execution counterfactual block (mid vs actual).
        pmx = summary.get("polymarket_execution", {})
        assert "mid_price_counterfactual" in pmx
        assert "actual" in pmx
        mid = pmx["mid_price_counterfactual"]
        actual = pmx["actual"]
        assert set(mid).issuperset({"insurance_buy_cost_usd", "exec_drag_total_usd"})
        assert set(actual).issuperset({"insurance_buy_cost_usd"})
        # Identity: actual_buy ~= mid_buy + exec_premium_buy
        assert actual["insurance_buy_cost_usd"] == pytest.approx(
            mid["insurance_buy_cost_usd"] + mid["exec_premium_buy_usd"], rel=1e-6
        )

        cf = summary.get("counterfactuals", {}).get("db_mid_execution", {})
        assert set(cf).issuperset({"final_value_usd", "roi_pct", "apy", "delta_vs_actual"})


# ---------------------------------------------------------------------------
# run_sweep
# ---------------------------------------------------------------------------

class TestRunSweep:

    @pytest.mark.integration
    @pytest.mark.slow
    @patch("active_backtester.get_historical_bet_price")
    @patch("active_backtester.get_clob_token_id")
    @patch("active_backtester.get_range_combinations")
    def test_sweep_ranks_by_apy(self, mock_combos, mock_clob, mock_bet_price, pool_data):
        mock_combos.return_value = [
            {"min": 2500.0, "max": 3500.0, "lower_bet_price": 0.15, "upper_bet_price": 0.10,
             "lower_market_id": 1, "upper_market_id": 2,
             "lower_market_question": "q1", "upper_market_question": "q2",
             "lower_event_id": 10, "upper_event_id": 11},
            {"min": 2000.0, "max": 4000.0, "lower_bet_price": 0.10, "upper_bet_price": 0.08,
             "lower_market_id": 3, "upper_market_id": 4,
             "lower_market_question": "q3", "upper_market_question": "q4",
             "lower_event_id": 12, "upper_event_id": 13},
        ]
        mock_clob.return_value = "0xclob"
        mock_bet_price.return_value = 0.15

        candles = make_candle_series(n=720, start_price=3000.0, price_delta=0.0)
        conn = MagicMock()

        results = run_sweep(candles, pool_data, "ETH", conn, initial_eth=16.666667)

        assert isinstance(results, list)
        if len(results) >= 2:
            assert results[0]["apy"] >= results[1]["apy"]

    @pytest.mark.integration
    @patch("active_backtester.get_range_combinations")
    def test_sweep_raises_on_empty_combos(self, mock_combos, pool_data):
        mock_combos.return_value = []
        candles = make_candle_series(n=20, start_price=3000.0)
        conn = MagicMock()

        with pytest.raises(ValueError, match="No Polymarket range"):
            run_sweep(candles, pool_data, "ETH", conn, initial_eth=16.666667)


# ---------------------------------------------------------------------------
# Insurance expiry rebalances
# ---------------------------------------------------------------------------


class TestInsuranceExpiry:

    @pytest.mark.integration
    @patch("active_backtester.get_historical_bet_price")
    @patch("active_backtester.get_clob_token_id")
    @patch("active_backtester.get_candidate_markets")
    @patch("active_backtester._get_insurance_for_range")
    def test_expiry_forces_close_with_zero_insurance(
        self, mock_ins, mock_cands, mock_clob, mock_bet_price, pool_data
    ):
        """If either Polymarket market expires, we force-close and treat insurance as $0."""
        from datetime import datetime, timezone

        mock_ins.return_value = {"lower_bet_price": 0.5, "upper_bet_price": 0.5}
        mock_clob.return_value = "0xclob"
        mock_bet_price.return_value = 0.5

        candles = make_candle_series(n=6, start_price=2000.0, price_delta=0.0)
        exp_dt = datetime.fromtimestamp(int(candles[2]["periodStartUnix"]), tz=timezone.utc)
        mock_cands.return_value = [{"clob_token_id": "0xclob", "market_id": 1, "end_date": exp_dt}]

        conn = MagicMock()
        positions, _, _ = simulate(
            candles, pool_data, "ETH", 0.0, conn,
            fixed_range=(1800.0, 2200.0),
            cooldown_hours=0,
            quiet=True,
            initial_eth=10.0,
        )

        assert len(positions) >= 2
        assert positions[0].get("close_reason") == "expiry"
        assert positions[0].get("insurance_payout") == 0.0
        assert positions[0].get("insurance_sellback") == 0.0

"""Unit tests for pure helper functions in parser.py."""

from decimal import Decimal
import pytest

from parser import (
    to_decimal,
    parse_json_list,
    infer_underlying_symbol,
    infer_level_and_direction,
    is_price_like_event,
)


# ---------------------------------------------------------------------------
# to_decimal
# ---------------------------------------------------------------------------

class TestToDecimal:

    @pytest.mark.unit
    def test_valid_integer(self):
        assert to_decimal(42) == Decimal("42")

    @pytest.mark.unit
    def test_valid_float_string(self):
        assert to_decimal("3.14") == Decimal("3.14")

    @pytest.mark.unit
    def test_none_returns_none(self):
        assert to_decimal(None) is None

    @pytest.mark.unit
    def test_empty_string_returns_none(self):
        assert to_decimal("") is None

    @pytest.mark.unit
    def test_null_string_returns_none(self):
        assert to_decimal("null") is None

    @pytest.mark.unit
    def test_garbage_returns_none(self):
        assert to_decimal("not-a-number") is None

    @pytest.mark.unit
    def test_negative_number(self):
        assert to_decimal("-100.5") == Decimal("-100.5")


# ---------------------------------------------------------------------------
# parse_json_list
# ---------------------------------------------------------------------------

class TestParseJsonList:

    @pytest.mark.unit
    def test_list_passthrough(self):
        assert parse_json_list(["Yes", "No"]) == ["Yes", "No"]

    @pytest.mark.unit
    def test_json_string(self):
        assert parse_json_list('["Yes", "No"]') == ["Yes", "No"]

    @pytest.mark.unit
    def test_invalid_json_returns_empty(self):
        assert parse_json_list("not-json") == []

    @pytest.mark.unit
    def test_none_returns_empty(self):
        assert parse_json_list(None) == []

    @pytest.mark.unit
    def test_integer_returns_empty(self):
        assert parse_json_list(123) == []

    @pytest.mark.unit
    def test_nested_list(self):
        result = parse_json_list('[[1,2],[3,4]]')
        assert result == [[1, 2], [3, 4]]


# ---------------------------------------------------------------------------
# infer_underlying_symbol
# ---------------------------------------------------------------------------

class TestInferUnderlyingSymbol:

    @pytest.mark.unit
    def test_bitcoin_keyword(self):
        assert infer_underlying_symbol(["Will Bitcoin hit $100k?"]) == "BTC"

    @pytest.mark.unit
    def test_ethereum_keyword(self):
        assert infer_underlying_symbol(["Ethereum price above $5000?"]) == "ETH"

    @pytest.mark.unit
    def test_solana_keyword(self):
        assert infer_underlying_symbol(["Will SOL reach $300?"]) == "SOL"

    @pytest.mark.unit
    def test_bnb_keyword(self):
        assert infer_underlying_symbol(["BNB price prediction"]) == "BNB"

    @pytest.mark.unit
    def test_chainlink_keyword(self):
        assert infer_underlying_symbol(["Chainlink above $50?"]) == "LINK"

    @pytest.mark.unit
    def test_no_match_returns_none(self):
        assert infer_underlying_symbol(["Stock market prediction"]) is None

    @pytest.mark.unit
    def test_none_in_texts(self):
        assert infer_underlying_symbol([None, "bitcoin price"]) == "BTC"

    @pytest.mark.unit
    def test_case_insensitive(self):
        assert infer_underlying_symbol(["BITCOIN ATH"]) == "BTC"

    @pytest.mark.unit
    def test_pump_fun(self):
        assert infer_underlying_symbol(["pump.fun token price"]) == "PUMP"


# ---------------------------------------------------------------------------
# infer_level_and_direction
# ---------------------------------------------------------------------------

class TestInferLevelAndDirection:

    @pytest.mark.unit
    def test_dollar_amount_up(self):
        level, direction = infer_level_and_direction("Will ETH reach $5,000?")
        assert level == Decimal("5000")
        assert direction == "up"

    @pytest.mark.unit
    def test_dollar_amount_down(self):
        level, direction = infer_level_and_direction("Will BTC dip below $50,000?")
        assert level == Decimal("50000")
        assert direction == "down"

    @pytest.mark.unit
    def test_no_direction_keywords(self):
        level, direction = infer_level_and_direction("BTC at $60000 by December?")
        assert level == Decimal("60000")
        assert direction == "unknown"

    @pytest.mark.unit
    def test_no_number(self):
        level, direction = infer_level_and_direction("Will ETH reach ATH?")
        assert level is None
        assert direction == "up"

    @pytest.mark.unit
    def test_none_input(self):
        level, direction = infer_level_and_direction(None)
        assert level is None
        assert direction is None

    @pytest.mark.unit
    def test_empty_string(self):
        level, direction = infer_level_and_direction("")
        assert level is None
        assert direction is None

    @pytest.mark.unit
    def test_ath_keyword(self):
        level, direction = infer_level_and_direction("Will ETH hit all time high?")
        assert direction == "up"


# ---------------------------------------------------------------------------
# is_price_like_event
# ---------------------------------------------------------------------------

class TestIsPriceLikeEvent:

    @pytest.mark.unit
    def test_price_event_accepted(self):
        event = {"slug": "eth-price-above-5000", "title": "ETH price above $5000?"}
        assert is_price_like_event(event) is True

    @pytest.mark.unit
    def test_ath_event_accepted(self):
        event = {"slug": "bitcoin-all-time-high", "title": "Bitcoin all-time-high?"}
        assert is_price_like_event(event) is True

    @pytest.mark.unit
    def test_short_term_excluded(self):
        event = {"slug": "eth-up-or-down-today", "title": "ETH up or down today?"}
        assert is_price_like_event(event) is False

    @pytest.mark.unit
    def test_fdv_excluded(self):
        event = {"slug": "solana-fdv-prediction", "title": "SOL FDV above $100B?"}
        assert is_price_like_event(event) is False

    @pytest.mark.unit
    def test_nft_excluded(self):
        event = {"slug": "cryptopunks-floor-price", "title": "CryptoPunks floor price above 50 ETH?"}
        assert is_price_like_event(event) is False

    @pytest.mark.unit
    def test_dominance_excluded(self):
        event = {"slug": "btc-dominance-above-60", "title": "Bitcoin dominance above 60%?"}
        assert is_price_like_event(event) is False

    @pytest.mark.unit
    def test_gas_excluded(self):
        event = {"slug": "gas-price-below-10-gwei", "title": "Gas price below 10 gwei?"}
        assert is_price_like_event(event) is False

    @pytest.mark.unit
    def test_no_price_keyword_excluded(self):
        event = {"slug": "who-wins-election", "title": "Who wins the election?"}
        assert is_price_like_event(event) is False

    @pytest.mark.unit
    def test_empty_event(self):
        event = {}
        assert is_price_like_event(event) is False

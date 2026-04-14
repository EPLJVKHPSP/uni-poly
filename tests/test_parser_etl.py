"""Tests for parser.py ETL functions — HTTP mocked."""

from decimal import Decimal
from unittest.mock import patch, MagicMock
import pytest

from parser import fetch_ath_by_symbol, infer_underlying_symbol, infer_level_and_direction, is_price_like_event


# ---------------------------------------------------------------------------
# fetch_ath_by_symbol
# ---------------------------------------------------------------------------

class TestFetchAthBySymbol:

    @pytest.mark.integration
    @patch("parser.requests.get")
    def test_returns_ath_dict(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = [
            {"id": "bitcoin", "ath": 73000},
            {"id": "ethereum", "ath": 4878},
            {"id": "solana", "ath": 260},
        ]
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        result = fetch_ath_by_symbol()

        assert "BTC" in result
        assert "ETH" in result
        assert result["BTC"] == Decimal("73000")
        assert result["ETH"] == Decimal("4878")

    @pytest.mark.integration
    @patch("parser.requests.get")
    def test_returns_empty_on_failure(self, mock_get):
        mock_get.side_effect = Exception("network error")
        result = fetch_ath_by_symbol()
        assert result == {}

    @pytest.mark.integration
    @patch("parser.requests.get")
    def test_skips_missing_ath(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = [
            {"id": "bitcoin", "ath": None},
        ]
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        result = fetch_ath_by_symbol()
        assert "BTC" not in result

    @pytest.mark.integration
    @patch("parser.requests.get")
    def test_handles_empty_response(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = []
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        result = fetch_ath_by_symbol()
        assert result == {}


# ---------------------------------------------------------------------------
# End-to-end filter pipeline (pure functions chained)
# ---------------------------------------------------------------------------

class TestFilterPipeline:

    @pytest.mark.unit
    def test_realistic_eth_price_event(self):
        event = {
            "slug": "eth-price-above-5000",
            "title": "Will Ethereum price hit $5,000?",
        }
        assert is_price_like_event(event) is True
        sym = infer_underlying_symbol([event["title"]])
        assert sym == "ETH"
        level, direction = infer_level_and_direction("Will Ethereum price hit $5,000?")
        assert level == Decimal("5000")
        assert direction == "up"

    @pytest.mark.unit
    def test_btc_dip_event(self):
        event = {
            "slug": "btc-dip-to-50000",
            "title": "Will Bitcoin dip to $50,000?",
        }
        assert is_price_like_event(event) is True
        sym = infer_underlying_symbol([event["title"]])
        assert sym == "BTC"
        level, direction = infer_level_and_direction("Will Bitcoin dip to $50,000?")
        assert level == Decimal("50000")
        assert direction == "down"

    @pytest.mark.unit
    def test_non_crypto_event_rejected(self):
        event = {
            "slug": "us-election-result",
            "title": "Who wins the 2024 election?",
        }
        assert is_price_like_event(event) is False
        sym = infer_underlying_symbol([event["title"]])
        assert sym is None

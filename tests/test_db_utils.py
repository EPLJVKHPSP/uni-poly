"""Tests for db_utils.py — all DB calls mocked via psycopg2.connect patch."""

from unittest.mock import patch, MagicMock, call
from datetime import datetime, timezone

import pytest

import db_utils


# ---------------------------------------------------------------------------
# get_db_connection
# ---------------------------------------------------------------------------

class TestGetDbConnection:

    @pytest.mark.integration
    @patch("db_utils.psycopg2.connect")
    def test_returns_connection(self, mock_connect, mock_env):
        mock_connect.return_value = MagicMock()
        conn = db_utils.get_db_connection()
        mock_connect.assert_called_once()
        assert conn is mock_connect.return_value

    @pytest.mark.integration
    @patch("db_utils.psycopg2.connect")
    def test_uses_env_vars(self, mock_connect, mock_env):
        db_utils.get_db_connection()
        kwargs = mock_connect.call_args.kwargs
        assert kwargs["dbname"] == "test_db"
        assert kwargs["user"] == "test_user"
        assert kwargs["password"] == "test_pw"


# ---------------------------------------------------------------------------
# get_range_combinations
# ---------------------------------------------------------------------------

class TestGetRangeCombinations:

    @pytest.mark.integration
    @patch("db_utils.psycopg2.connect")
    def test_builds_combinations(self, mock_connect):
        cursor = MagicMock()
        conn = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        down_rows = [
            {"level": "2000", "price": "0.15", "market_id": 1, "market_question": "ETH below $2000?", "event_id": 10},
            {"level": "2500", "price": "0.20", "market_id": 2, "market_question": "ETH below $2500?", "event_id": 11},
        ]
        up_rows = [
            {"level": "3500", "price": "0.10", "market_id": 3, "market_question": "ETH above $3500?", "event_id": 12},
            {"level": "4000", "price": "0.12", "market_id": 4, "market_question": "ETH above $4000?", "event_id": 13},
        ]
        cursor.fetchall.side_effect = [down_rows, up_rows]

        combos = db_utils.get_range_combinations("ETH", conn=conn)

        assert len(combos) == 4
        for c in combos:
            assert c["min"] < c["max"]
            assert "lower_bet_price" in c
            assert "upper_bet_price" in c

    @pytest.mark.integration
    @patch("db_utils.psycopg2.connect")
    def test_empty_levels_returns_empty(self, mock_connect):
        cursor = MagicMock()
        conn = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        cursor.fetchall.side_effect = [[], []]

        combos = db_utils.get_range_combinations("ETH", conn=conn)
        assert combos == []

    @pytest.mark.integration
    @patch("db_utils.get_db_connection")
    def test_opens_own_connection_when_none(self, mock_get_conn):
        cursor = MagicMock()
        conn = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        cursor.fetchall.side_effect = [[], []]
        mock_get_conn.return_value = conn

        db_utils.get_range_combinations("ETH", conn=None)
        mock_get_conn.assert_called_once()
        conn.close.assert_called_once()


# ---------------------------------------------------------------------------
# get_clob_token_id
# ---------------------------------------------------------------------------

class TestGetClobTokenId:

    @pytest.mark.integration
    def test_returns_token_id(self):
        cursor = MagicMock()
        conn = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        cursor.fetchone.return_value = ("0xabc123",)

        result = db_utils.get_clob_token_id("ETH", 2500.0, "down", "Yes", conn)
        assert result == "0xabc123"

    @pytest.mark.integration
    def test_returns_none_when_no_row(self):
        cursor = MagicMock()
        conn = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        cursor.fetchone.return_value = None

        result = db_utils.get_clob_token_id("ETH", 2500.0, "down", "Yes", conn)
        assert result is None


# ---------------------------------------------------------------------------
# get_historical_bet_price
# ---------------------------------------------------------------------------

class TestGetHistoricalBetPrice:

    @pytest.mark.integration
    def test_returns_price_before_target(self):
        cursor = MagicMock()
        conn = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        cursor.fetchone.return_value = ("0.35",)

        result = db_utils.get_historical_bet_price("0xabc", 1700000000, conn)
        assert result == 0.35

    @pytest.mark.integration
    def test_fallback_to_after_target(self):
        cursor = MagicMock()
        conn = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        cursor.fetchone.side_effect = [None, ("0.42",)]

        result = db_utils.get_historical_bet_price("0xabc", 1700000000, conn)
        assert result == 0.42

    @pytest.mark.integration
    def test_returns_none_when_no_data(self):
        cursor = MagicMock()
        conn = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        cursor.fetchone.side_effect = [None, None]

        result = db_utils.get_historical_bet_price("0xabc", 1700000000, conn)
        assert result is None

    @pytest.mark.integration
    def test_accepts_datetime_target(self):
        cursor = MagicMock()
        conn = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        cursor.fetchone.return_value = ("0.50",)

        ts = datetime(2023, 11, 15, tzinfo=timezone.utc)
        result = db_utils.get_historical_bet_price("0xabc", ts, conn)
        assert result == 0.50

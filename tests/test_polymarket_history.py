"""Tests for polymarket_history.py — DB and HTTP mocked."""

from unittest.mock import patch, MagicMock, call
from datetime import datetime, timezone
import pytest

from polymarket_history import (
    ensure_history_table,
    fetch_price_history,
    upsert_price_history,
    sync_all_markets,
)


# ---------------------------------------------------------------------------
# ensure_history_table
# ---------------------------------------------------------------------------

class TestEnsureHistoryTable:

    @pytest.mark.integration
    def test_executes_create_table(self):
        cur = MagicMock()
        ensure_history_table(cur)
        assert cur.execute.call_count == 2
        first_sql = cur.execute.call_args_list[0][0][0]
        assert "CREATE TABLE" in first_sql
        assert "bet_price_history" in first_sql

    @pytest.mark.integration
    def test_creates_index(self):
        cur = MagicMock()
        ensure_history_table(cur)
        second_sql = cur.execute.call_args_list[1][0][0]
        assert "CREATE INDEX" in second_sql


# ---------------------------------------------------------------------------
# fetch_price_history
# ---------------------------------------------------------------------------

class TestFetchPriceHistory:

    @pytest.mark.integration
    @patch("polymarket_history.requests.get")
    def test_returns_history_list(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "history": [
                {"t": 1700000000, "p": 0.35},
                {"t": 1700003600, "p": 0.37},
            ]
        }
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        result = fetch_price_history("0xclob123")
        assert len(result) == 2
        assert result[0]["t"] == 1700000000

    @pytest.mark.integration
    @patch("polymarket_history.requests.get")
    def test_passes_fidelity_param(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"history": []}
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        fetch_price_history("0xclob123", fidelity=30)
        call_kwargs = mock_get.call_args.kwargs
        assert call_kwargs["params"]["fidelity"] == 30

    @pytest.mark.integration
    @patch("polymarket_history.requests.get")
    def test_returns_empty_on_failure(self, mock_get):
        mock_get.side_effect = Exception("timeout")
        result = fetch_price_history("0xclob123")
        assert result == []

    @pytest.mark.integration
    @patch("polymarket_history.requests.get")
    def test_includes_start_end_ts(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"history": []}
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        fetch_price_history("0xclob", start_ts=1700000000, end_ts=1700100000)
        params = mock_get.call_args.kwargs["params"]
        assert params["startTs"] == 1700000000
        assert params["endTs"] == 1700100000


# ---------------------------------------------------------------------------
# upsert_price_history
# ---------------------------------------------------------------------------

class TestUpsertPriceHistory:

    @pytest.mark.integration
    @patch("psycopg2.extras.execute_values")
    def test_inserts_rows(self, mock_exec):
        cur = MagicMock()
        history = [
            {"t": 1700000000, "p": 0.35},
            {"t": 1700003600, "p": 0.37},
        ]

        count = upsert_price_history(cur, "0xclob", history)

        assert count == 2
        mock_exec.assert_called_once()

    @pytest.mark.integration
    @patch("psycopg2.extras.execute_values")
    def test_deduplicates_timestamps(self, mock_exec):
        cur = MagicMock()
        history = [
            {"t": 1700000000, "p": 0.35},
            {"t": 1700000000, "p": 0.36},
            {"t": 1700003600, "p": 0.37},
        ]

        count = upsert_price_history(cur, "0xclob", history)
        assert count == 2

    @pytest.mark.integration
    def test_empty_history_returns_zero(self):
        cur = MagicMock()
        count = upsert_price_history(cur, "0xclob", [])
        assert count == 0


# ---------------------------------------------------------------------------
# sync_all_markets
# ---------------------------------------------------------------------------

class TestSyncAllMarkets:

    @pytest.mark.integration
    @patch("polymarket_history.time.sleep")
    @patch("polymarket_history.fetch_price_history")
    @patch("polymarket_history.get_db_connection")
    def test_syncs_markets(self, mock_get_conn, mock_fetch, mock_sleep):
        cursor = MagicMock()
        conn = MagicMock()
        conn.cursor.return_value = cursor
        mock_get_conn.return_value = conn

        cursor.fetchall.return_value = [
            ("0xclob1", 1, "Yes", "ETH", 2500, "down"),
            ("0xclob2", 2, "Yes", "ETH", 3500, "up"),
        ]

        mock_fetch.side_effect = [
            [{"t": 1700000000, "p": 0.35}],
            [{"t": 1700000000, "p": 0.20}],
        ]

        with patch("polymarket_history.upsert_price_history", return_value=1) as mock_upsert:
            sync_all_markets(fidelity=60)

        assert mock_fetch.call_count == 2
        assert mock_upsert.call_count == 2
        assert conn.commit.call_count >= 2
        conn.close.assert_called_once()

    @pytest.mark.integration
    @patch("polymarket_history.time.sleep")
    @patch("polymarket_history.fetch_price_history")
    @patch("polymarket_history.get_db_connection")
    def test_rate_limiting(self, mock_get_conn, mock_fetch, mock_sleep):
        cursor = MagicMock()
        conn = MagicMock()
        conn.cursor.return_value = cursor
        mock_get_conn.return_value = conn

        cursor.fetchall.return_value = [
            ("0xclob1", 1, "Yes", "ETH", 2500, "down"),
            ("0xclob2", 2, "Yes", "ETH", 3500, "up"),
        ]
        mock_fetch.return_value = []

        sync_all_markets()

        assert mock_sleep.call_count == 2
        mock_sleep.assert_called_with(0.3)

    @pytest.mark.integration
    @patch("polymarket_history.time.sleep")
    @patch("polymarket_history.fetch_price_history")
    @patch("polymarket_history.get_db_connection")
    def test_closes_connection_on_error(self, mock_get_conn, mock_fetch, mock_sleep):
        conn = MagicMock()
        conn.cursor.side_effect = Exception("DB error")
        mock_get_conn.return_value = conn

        with pytest.raises(Exception):
            sync_all_markets()

        conn.close.assert_called_once()

"""Tests for HTTP/Graph API functions in active_backtester.py."""

from unittest.mock import patch, MagicMock
import pytest

from active_backtester import _graph_url, fetch_pool_metadata, fetch_hourly_candles


# ---------------------------------------------------------------------------
# _graph_url
# ---------------------------------------------------------------------------

class TestGraphUrl:

    @pytest.mark.integration
    def test_returns_url_with_key(self, mock_env):
        url = _graph_url()
        assert "test-graph-key" in url
        assert "gateway.thegraph.com" in url

    @pytest.mark.integration
    def test_raises_without_key(self, monkeypatch):
        monkeypatch.delenv("THEGRAPH_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="THEGRAPH_API_KEY"):
            _graph_url()


# ---------------------------------------------------------------------------
# fetch_pool_metadata
# ---------------------------------------------------------------------------

class TestFetchPoolMetadata:

    @pytest.mark.integration
    @patch("active_backtester.requests.post")
    def test_returns_first_pool(self, mock_post, mock_env):
        pool = {
            "id": "0xpool",
            "feeTier": "500",
            "token0": {"id": "0x1", "symbol": "USDC", "name": "USD Coin", "decimals": "6"},
            "token1": {"id": "0x2", "symbol": "WETH", "name": "Wrapped Ether", "decimals": "18"},
        }
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"data": {"id": [pool]}}
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        result = fetch_pool_metadata("0xpool")
        assert result["id"] == "0xpool"
        assert result["token0"]["symbol"] == "USDC"

    @pytest.mark.integration
    @patch("active_backtester.requests.post")
    def test_raises_on_empty_pool(self, mock_post, mock_env):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"data": {"id": []}}
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        with pytest.raises(RuntimeError, match="not found"):
            fetch_pool_metadata("0xmissing")

    @pytest.mark.integration
    @patch("active_backtester.requests.post")
    def test_calls_raise_for_status(self, mock_post, mock_env):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"data": {"id": [{"id": "0x1"}]}}
        mock_post.return_value = mock_resp

        fetch_pool_metadata("0x1")
        mock_resp.raise_for_status.assert_called_once()


# ---------------------------------------------------------------------------
# fetch_hourly_candles
# ---------------------------------------------------------------------------

class TestFetchHourlyCandles:

    @pytest.mark.integration
    @patch("active_backtester.requests.post")
    def test_single_batch(self, mock_post, mock_env):
        candles = [
            {"periodStartUnix": "1700000000", "close": "3000"},
            {"periodStartUnix": "1700003600", "close": "3010"},
        ]
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"data": {"poolHourDatas": candles}}
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        result = fetch_hourly_candles("0xpool", 1700000000, 1700003600 + 1)
        assert len(result) >= 2

    @pytest.mark.integration
    @patch("active_backtester.requests.post")
    def test_pagination_across_windows(self, mock_post, mock_env):
        batch1 = [{"periodStartUnix": str(1700000000 + i * 3600), "close": "3000"} for i in range(100)]
        batch2 = [{"periodStartUnix": str(1700000000 + 86400 * 30 + i * 3600), "close": "3100"} for i in range(50)]

        mock_resp1 = MagicMock()
        mock_resp1.json.return_value = {"data": {"poolHourDatas": batch1}}
        mock_resp1.raise_for_status = MagicMock()

        mock_resp2 = MagicMock()
        mock_resp2.json.return_value = {"data": {"poolHourDatas": batch2}}
        mock_resp2.raise_for_status = MagicMock()

        mock_resp_empty = MagicMock()
        mock_resp_empty.json.return_value = {"data": {"poolHourDatas": []}}
        mock_resp_empty.raise_for_status = MagicMock()

        mock_post.side_effect = [mock_resp1, mock_resp2, mock_resp_empty]

        start = 1700000000
        end = 1700000000 + 86400 * 60
        result = fetch_hourly_candles("0xpool", start, end)

        assert len(result) == 150

    @pytest.mark.integration
    @patch("active_backtester.requests.post")
    def test_sorted_by_timestamp(self, mock_post, mock_env):
        candles = [
            {"periodStartUnix": "1700003600", "close": "3010"},
            {"periodStartUnix": "1700000000", "close": "3000"},
        ]
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"data": {"poolHourDatas": candles}}
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        result = fetch_hourly_candles("0xpool", 1699999999, 1700003601)
        timestamps = [int(c["periodStartUnix"]) for c in result]
        assert timestamps == sorted(timestamps)

    @pytest.mark.integration
    @patch("active_backtester.requests.post")
    def test_empty_result(self, mock_post, mock_env):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"data": {"poolHourDatas": []}}
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        result = fetch_hourly_candles("0xpool", 1700000000, 1700003600)
        assert result == []

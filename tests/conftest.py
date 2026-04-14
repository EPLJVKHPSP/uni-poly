"""Shared fixtures for the Keyrock Polymarket test suite."""

from typing import Optional

import pytest
from unittest.mock import MagicMock


# ---------------------------------------------------------------------------
# Pool / candle fixtures (mirror The Graph poolHourDatas schema)
# ---------------------------------------------------------------------------

POOL_INFO = {
    "totalValueLockedUSD": "250000000",
    "totalValueLockedToken0": "125000000",
    "totalValueLockedToken1": "50000",
    "token0": {"decimals": "6"},
    "token1": {"decimals": "18"},
}


def _make_candle(
    ts: int,
    close: str,
    low: str,
    high: str,
    fg0: str = "0",
    fg1: str = "0",
    liquidity: str = "1000000000000",
    pool_override: Optional[dict] = None,
) -> dict:
    return {
        "periodStartUnix": str(ts),
        "close": close,
        "low": low,
        "high": high,
        "liquidity": liquidity,
        "feeGrowthGlobal0X128": fg0,
        "feeGrowthGlobal1X128": fg1,
        "pool": pool_override or POOL_INFO,
    }


@pytest.fixture
def sample_candle():
    """Single realistic hourly candle at ETH ~3000."""
    return _make_candle(
        ts=1_700_000_000,
        close="3000.0",
        low="2980.0",
        high="3020.0",
        fg0="100000000000000000000000000000000000000",
        fg1="200000000000000000000000000000000000000",
    )


@pytest.fixture
def sample_candle_pair():
    """Two consecutive candles with fee-growth deltas."""
    prev = _make_candle(
        ts=1_700_000_000,
        close="3000.0",
        low="2980.0",
        high="3020.0",
        fg0="100000000000000000000000000000000000000",
        fg1="200000000000000000000000000000000000000",
    )
    curr = _make_candle(
        ts=1_700_003_600,
        close="3010.0",
        low="2990.0",
        high="3030.0",
        fg0="100500000000000000000000000000000000000",
        fg1="200500000000000000000000000000000000000",
    )
    return prev, curr


# ---------------------------------------------------------------------------
# Pool metadata fixture (from fetch_pool_metadata)
# ---------------------------------------------------------------------------

@pytest.fixture
def pool_data():
    return {
        "id": "0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640",
        "feeTier": "500",
        "token0": {"id": "0xa0b8", "symbol": "USDC", "name": "USD Coin", "decimals": "6"},
        "token1": {"id": "0xc02a", "symbol": "WETH", "name": "Wrapped Ether", "decimals": "18"},
    }


# ---------------------------------------------------------------------------
# Wallet & insurance fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_wallet():
    return {"usdc": 50_000.0, "eth": 16.666667}


@pytest.fixture
def sample_insurance_info():
    return {
        "lower_bet_price": 0.15,
        "upper_bet_price": 0.10,
    }


# ---------------------------------------------------------------------------
# Mock DB connection
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_db_conn():
    """MagicMock psycopg2 connection with cursor context manager."""
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return conn, cursor


# ---------------------------------------------------------------------------
# Environment variable fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_env(monkeypatch):
    monkeypatch.setenv("DB_NAME", "test_db")
    monkeypatch.setenv("DB_USER", "test_user")
    monkeypatch.setenv("DB_PASSWORD", "test_pw")
    monkeypatch.setenv("DB_HOST", "localhost")
    monkeypatch.setenv("DB_PORT", "5432")
    monkeypatch.setenv("THEGRAPH_API_KEY", "test-graph-key")


# ---------------------------------------------------------------------------
# Range combo fixtures (mirrors db_utils.get_range_combinations output)
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_combos():
    return [
        {"min": 2400.0, "max": 3600.0, "lower_bet_price": 0.15, "upper_bet_price": 0.10,
         "lower_market_id": 1, "upper_market_id": 2,
         "lower_market_question": "ETH below $2400?", "upper_market_question": "ETH above $3600?",
         "lower_event_id": 100, "upper_event_id": 101},
        {"min": 2600.0, "max": 3400.0, "lower_bet_price": 0.20, "upper_bet_price": 0.12,
         "lower_market_id": 3, "upper_market_id": 4,
         "lower_market_question": "ETH below $2600?", "upper_market_question": "ETH above $3400?",
         "lower_event_id": 102, "upper_event_id": 103},
        {"min": 2800.0, "max": 3200.0, "lower_bet_price": 0.30, "upper_bet_price": 0.25,
         "lower_market_id": 5, "upper_market_id": 6,
         "lower_market_question": "ETH below $2800?", "upper_market_question": "ETH above $3200?",
         "lower_event_id": 104, "upper_event_id": 105},
    ]


# ---------------------------------------------------------------------------
# Helper to build N candles for simulation tests
# ---------------------------------------------------------------------------

def make_candle_series(
    n: int,
    start_ts: int = 1_700_000_000,
    start_price: float = 3000.0,
    price_delta: float = 0.0,
    fg0_base: int = 100_000_000_000_000_000_000_000_000_000_000_000_000,
    fg0_step: int = 500_000_000_000_000_000_000_000_000_000_000_000,
    fg1_base: int = 200_000_000_000_000_000_000_000_000_000_000_000_000,
    fg1_step: int = 500_000_000_000_000_000_000_000_000_000_000_000,
) -> list:
    """Generate a series of N candles with linearly drifting price and fee growth."""
    candles = []
    for i in range(n):
        price = start_price + i * price_delta
        candles.append(_make_candle(
            ts=start_ts + i * 3600,
            close=str(price),
            low=str(price - 20),
            high=str(price + 20),
            fg0=str(fg0_base + i * fg0_step),
            fg1=str(fg1_base + i * fg1_step),
        ))
    return candles

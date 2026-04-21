"""Tests for backtester.data_validation."""

from typing import Dict, List

import pytest

from backtester.data_validation import (
    validate_candles,
    validate_gas_coverage,
    validate_polymarket_coverage,
)


def _candle(ts: int, fg0: str = "100", fg1: str = "200",
            close: str = "3000.0", low: str = "2980.0", high: str = "3020.0") -> Dict:
    return {
        "periodStartUnix": str(ts),
        "close": close, "low": low, "high": high,
        "feeGrowthGlobal0X128": fg0,
        "feeGrowthGlobal1X128": fg1,
    }


class TestValidateCandles:

    @pytest.mark.unit
    def test_empty_input_is_clean(self):
        r = validate_candles([])
        assert r.candle_count == 0
        assert r.is_clean

    @pytest.mark.unit
    def test_clean_series_is_clean(self):
        series = [
            _candle(1000, fg0=str(100 + i), fg1=str(200 + i))
            for i in range(5)
        ]
        # rewrite ts to be hourly
        series = [_candle(1_700_000_000 + 3600 * i,
                          fg0=str(100 + i * 10),
                          fg1=str(200 + i * 10)) for i in range(5)]
        r = validate_candles(series)
        assert r.is_clean
        assert r.candle_count == 5
        assert r.missing_hours == 0

    @pytest.mark.unit
    def test_detects_hourly_gap(self):
        series = [
            _candle(1_700_000_000),
            _candle(1_700_000_000 + 3600),
            # skip one hour (1_700_000_000 + 7200)
            _candle(1_700_000_000 + 10_800),
        ]
        r = validate_candles(series)
        assert r.missing_hours == 1
        assert len(r.gap_segments) == 1
        assert r.gap_segments[0]["missing_hours"] == 1

    @pytest.mark.unit
    def test_detects_multi_hour_gap(self):
        series = [
            _candle(1_700_000_000),
            _candle(1_700_000_000 + 3 * 3600),
        ]
        r = validate_candles(series)
        assert r.missing_hours == 2

    @pytest.mark.unit
    def test_detects_duplicate_ts(self):
        series = [
            _candle(1_700_000_000),
            _candle(1_700_000_000),
            _candle(1_700_000_000 + 3600),
        ]
        r = validate_candles(series)
        assert r.duplicate_ts == 1

    @pytest.mark.unit
    def test_detects_non_monotonic_fee_growth(self):
        series = [
            _candle(1_700_000_000, fg0="500", fg1="600"),
            _candle(1_700_003_600, fg0="400", fg1="600"),  # fg0 decreased
        ]
        r = validate_candles(series)
        assert r.fee_growth_non_monotonic >= 1

    @pytest.mark.unit
    def test_detects_non_positive_close(self):
        series = [
            _candle(1_700_000_000, close="3000.0"),
            _candle(1_700_003_600, close="0.0"),
        ]
        r = validate_candles(series)
        assert r.non_positive_close == 1

    @pytest.mark.unit
    def test_detects_high_lower_than_low(self):
        series = [
            _candle(1_700_000_000, low="3000.0", high="2900.0"),
        ]
        r = validate_candles(series)
        assert r.non_positive_hl == 1


class TestValidateGasCoverage:

    @pytest.mark.unit
    def test_full_coverage(self):
        prices = {"2026-01-01": 30, "2026-01-02": 35, "2026-01-03": 40}
        r = validate_gas_coverage("2026-01-01", "2026-01-03", prices)
        assert r.coverage_pct == 100.0
        assert r.missing_dates == []

    @pytest.mark.unit
    def test_partial_coverage(self):
        prices = {"2026-01-01": 30, "2026-01-03": 40}
        r = validate_gas_coverage("2026-01-01", "2026-01-03", prices)
        assert r.covered_days == 2
        assert r.requested_days == 3
        assert r.missing_dates == ["2026-01-02"]

    @pytest.mark.unit
    def test_empty_gas_prices_means_zero_coverage(self):
        r = validate_gas_coverage("2026-01-01", "2026-01-02", {})
        assert r.coverage_pct == 0.0
        assert r.missing_dates == ["2026-01-01", "2026-01-02"]


class TestValidatePolymarketCoverage:

    @pytest.mark.unit
    def test_full_coverage(self):
        snaps = [
            {"position_open": True, "lower_bid": 0.15, "upper_bid": 0.10},
            {"position_open": True, "lower_bid": 0.16, "upper_bid": 0.09},
        ]
        r = validate_polymarket_coverage(snaps)
        assert r.position_hours == 2
        assert r.lower_bid_coverage_pct == 100.0
        assert r.upper_bid_coverage_pct == 100.0

    @pytest.mark.unit
    def test_partial_coverage(self):
        snaps = [
            {"position_open": True, "lower_bid": 0.15},  # upper missing
            {"position_open": True, "upper_bid": 0.09},  # lower missing
            {"position_open": True},                     # both missing
        ]
        r = validate_polymarket_coverage(snaps)
        assert r.position_hours == 3
        assert r.hours_with_lower_bid == 1
        assert r.hours_with_upper_bid == 1
        assert r.hours_with_any_bid == 2

    @pytest.mark.unit
    def test_ignores_idle_snapshots(self):
        snaps = [
            {"position_open": False},
            {"position_open": True, "lower_bid": 0.2, "upper_bid": 0.2},
        ]
        r = validate_polymarket_coverage(snaps)
        assert r.position_hours == 1

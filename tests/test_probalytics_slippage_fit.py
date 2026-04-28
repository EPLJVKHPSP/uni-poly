"""Unit tests for the Probalytics slippage fitter."""

from __future__ import annotations

import pandas as pd
import pytest

from probalytics_pkg.slippage_fit import fit_per_asset_slippage, slippage_summary


def _ts(x):
    if isinstance(x, pd.Timestamp):
        return x if x.tz is not None else x.tz_localize("UTC")
    return pd.Timestamp(x).tz_localize("UTC")


def _mkfill(market, outcome, ts, side, size, price):
    return {
        "market_platform_id": market,
        "outcome_platform_id": outcome,
        "timestamp": _ts(ts),
        "taker_side": side,
        "size": float(size),
        "price": float(price),
        "normalized_price": float(price) if outcome.endswith("_yes") else 1.0 - float(price),
    }


def test_returns_empty_dict_for_empty_fills():
    assert fit_per_asset_slippage(pd.DataFrame()) == {}


def test_falls_back_when_too_few_clusters():
    rows = [
        _mkfill("m1", "m1_yes", "2026-04-15 12:00:00", "BUY", 100, 0.40),
        _mkfill("m1", "m1_yes", "2026-04-15 12:00:01", "BUY", 100, 0.40),
    ]
    fit = fit_per_asset_slippage(pd.DataFrame(rows), fallback_per_1k=0.05)
    assert fit["m1_yes"] == pytest.approx(0.05)


def test_dispersion_is_used_when_enough_clusters():
    """Engineer 6 clusters of 3 fills each, spaced > window apart so the
    sliding window picks up exactly one cluster at a time.

    Each cluster: 3 fills at prices [0.40, 0.41, 0.42], 200 contracts each.
    range = 0.02; total size = 600 (= 0.6 KC); per_1k = 0.02 / 0.6 ≈ 0.0333.
    """
    rows = []
    for c in range(6):
        # Two-minute spacing >> 60s window so clusters never overlap.
        base_ts = pd.Timestamp("2026-04-15 12:00:00") + pd.Timedelta(minutes=c * 2)
        for i, px in enumerate([0.40, 0.41, 0.42]):
            rows.append(_mkfill(
                "mX", "mX_yes",
                base_ts + pd.Timedelta(seconds=i),
                "BUY", 200, px,
            ))
    fit = fit_per_asset_slippage(pd.DataFrame(rows), min_clusters_per_asset=5)
    expected = 0.02 / 0.6  # ≈ 0.0333
    assert fit["mX_yes"] == pytest.approx(expected, abs=0.005)


def test_replicates_market_coefficient_to_all_outcomes():
    rows = []
    base_ts = pd.Timestamp("2026-04-15 12:00:00", tz="UTC")
    for c in range(6):
        for i, (out, px) in enumerate([("m_yes", 0.30), ("m_no", 0.70), ("m_yes", 0.31)]):
            rows.append(_mkfill(
                "m", out,
                base_ts + pd.Timedelta(minutes=c, seconds=i),
                "BUY", 100, px,
            ))
    fit = fit_per_asset_slippage(pd.DataFrame(rows), min_clusters_per_asset=3)
    assert "m_yes" in fit and "m_no" in fit
    assert fit["m_yes"] == fit["m_no"]


def test_clips_to_max_per_1k():
    """A market with extreme dispersion should clip at max_per_1k (default 0.20)."""
    rows = []
    base_ts = pd.Timestamp("2026-04-15 12:00:00", tz="UTC")
    for c in range(6):
        for i, px in enumerate([0.10, 0.50, 0.90]):
            rows.append(_mkfill(
                "wild", "wild_yes",
                base_ts + pd.Timedelta(minutes=c, seconds=i),
                "BUY", 50, px,  # only 150 total = 0.15 KC -> raw 0.8/0.15 ≈ 5.3
            ))
    fit = fit_per_asset_slippage(pd.DataFrame(rows), min_clusters_per_asset=5, max_per_1k=0.20)
    assert fit["wild_yes"] == pytest.approx(0.20)


def test_summary_handles_empty():
    assert slippage_summary({}) == {"assets": 0}

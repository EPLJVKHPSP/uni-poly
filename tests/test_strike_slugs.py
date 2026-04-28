"""Unit tests for the targeted Polymarket strike-slug generator."""

from datetime import date

import pytest

from polymarket_history_pkg.strike_slugs import (
    DAILY_KIND,
    HOURLY_KIND,
    all_strike_slugs,
    daily_slug,
    daily_slugs,
    hourly_slug,
    hourly_slugs,
)


@pytest.mark.unit
def test_daily_slug_matches_polymarket_format():
    # User-provided canonical example for the daily touch event.
    assert daily_slug("BTC", date(2026, 4, 22)) == "what-price-will-bitcoin-hit-on-april-22"
    assert daily_slug("ETH", date(2026, 4, 1)) == "what-price-will-ethereum-hit-on-april-1"


@pytest.mark.unit
@pytest.mark.parametrize(
    "asset, dt, hour, expected",
    [
        ("BTC", date(2026, 4, 22), 6, "bitcoin-above-on-april-22-2026-6am-et"),
        ("BTC", date(2026, 4, 22), 0, "bitcoin-above-on-april-22-2026-12am-et"),
        ("BTC", date(2026, 4, 22), 12, "bitcoin-above-on-april-22-2026-12pm-et"),
        ("BTC", date(2026, 4, 22), 13, "bitcoin-above-on-april-22-2026-1pm-et"),
        ("BTC", date(2026, 4, 22), 23, "bitcoin-above-on-april-22-2026-11pm-et"),
        ("ETH", date(2026, 1, 5), 1, "ethereum-above-on-january-5-2026-1am-et"),
    ],
)
def test_hourly_slug_matches_polymarket_format(asset, dt, hour, expected):
    assert hourly_slug(asset, dt, hour) == expected


@pytest.mark.unit
def test_daily_slugs_yields_one_per_asset_per_day():
    days = list(daily_slugs(date(2026, 4, 1), date(2026, 4, 3), assets=("BTC", "ETH")))
    assert len(days) == 6  # 3 days * 2 assets
    assert all(s.kind == DAILY_KIND for s in days)
    assert {s.asset for s in days} == {"bitcoin", "ethereum"}


@pytest.mark.unit
def test_hourly_slugs_walks_full_day():
    rows = list(hourly_slugs(date(2026, 4, 22), date(2026, 4, 22), assets=("BTC",)))
    assert len(rows) == 24
    assert all(r.kind == HOURLY_KIND for r in rows)
    assert {r.target_hour for r in rows} == set(range(24))


@pytest.mark.unit
def test_all_strike_slugs_combines_modes():
    rows = all_strike_slugs(
        date(2026, 4, 22), date(2026, 4, 22),
        assets=("BTC",),
        include_daily=True, include_hourly=True, hours=(6, 12, 18),
    )
    kinds = [r.kind for r in rows]
    # 1 daily + 3 hourly = 4
    assert kinds.count(DAILY_KIND) == 1
    assert kinds.count(HOURLY_KIND) == 3


@pytest.mark.unit
def test_invalid_asset_rejected():
    with pytest.raises(ValueError):
        list(daily_slugs(date(2026, 4, 22), date(2026, 4, 22), assets=("DOGE",)))

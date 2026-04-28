"""Construct expected Polymarket event slugs for ETH/BTC strike markets.

Polymarket deploys two predictable families of binary markets that are exactly
the instruments we need to hedge an LP:

1. **Daily touch** (``what-price-will-{bitcoin,ethereum}-hit-on-{month}-{day}``)
   Each event holds N binary children of the form
   "Will <ASSET> reach $X on <DATE>?" / "Will <ASSET> dip to $X on <DATE>?".
   These are *touch* markets — they pay $1 the first time the underlying
   crosses the level intra-day, which is exactly the IL-hedge payoff.

2. **Hourly close-on-date** (``{bitcoin,ethereum}-above-on-{month}-{day}-{year}-{H}{ampm}-et``)
   Each event holds N binary children of the form
   "<ASSET> above $X on <DATE>, <H>AM/PM ET?" — they resolve on the *close*
   of the matching Binance 1h candle. Useful when the LP redeploys on an
   hourly cadence.

Reconstructing slugs is dramatically faster than paginating Gamma's
``/events`` endpoint (which returns 16k+ crypto events, most of which are
irrelevant to a WETH/USDC LP).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable, Iterator, List, Sequence, Tuple


_MONTHS = (
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
)

# Polymarket asset slug stems (the part of the slug between protocol prefix
# and the date). Aliases included so callers can pass tickers or full names.
ASSET_TO_SLUG = {
    "BTC": "bitcoin",
    "BITCOIN": "bitcoin",
    "ETH": "ethereum",
    "ETHEREUM": "ethereum",
}

DAILY_KIND = "daily"
HOURLY_KIND = "hourly"


@dataclass(frozen=True)
class StrikeSlug:
    kind: str        # "daily" | "hourly"
    asset: str       # canonical "bitcoin" | "ethereum"
    target_date: date
    target_hour: int  # 0..23, ignored for daily slugs (-1)
    slug: str


def _normalise_assets(assets: Sequence[str]) -> List[str]:
    out: List[str] = []
    seen: set[str] = set()
    for a in assets:
        key = ASSET_TO_SLUG.get(str(a).upper().strip())
        if key and key not in seen:
            out.append(key)
            seen.add(key)
    if not out:
        raise ValueError(f"No supported assets in {assets!r}; supported: BTC, ETH")
    return out


def _hour_token(h: int) -> str:
    """Convert 0..23 to Polymarket's '12am','1am',...,'12pm','1pm',...,'11pm'."""
    if not 0 <= h <= 23:
        raise ValueError(f"hour {h!r} out of range")
    if h == 0:
        return "12am"
    if h == 12:
        return "12pm"
    if h < 12:
        return f"{h}am"
    return f"{h - 12}pm"


def _date_range(start: date, end: date) -> Iterator[date]:
    """Inclusive on both ends. Days walked forward."""
    if end < start:
        raise ValueError("end date precedes start date")
    cur = start
    while cur <= end:
        yield cur
        cur = cur + timedelta(days=1)


def daily_slug(asset: str, target: date) -> str:
    """``what-price-will-{asset}-hit-on-{month}-{day}`` (no year, no padding)."""
    canon = ASSET_TO_SLUG[asset.upper()]
    month = _MONTHS[target.month - 1]
    return f"what-price-will-{canon}-hit-on-{month}-{target.day}"


def hourly_slug(asset: str, target: date, hour: int) -> str:
    """``{asset}-above-on-{month}-{day}-{year}-{H}{ampm}-et`` (no padding)."""
    canon = ASSET_TO_SLUG[asset.upper()]
    month = _MONTHS[target.month - 1]
    return f"{canon}-above-on-{month}-{target.day}-{target.year}-{_hour_token(hour)}-et"


def daily_slugs(
    start: date,
    end: date,
    assets: Sequence[str] = ("BTC", "ETH"),
) -> Iterator[StrikeSlug]:
    canon_assets = _normalise_assets(assets)
    for d in _date_range(start, end):
        for a in canon_assets:
            yield StrikeSlug(DAILY_KIND, a, d, -1, daily_slug(a, d))


def hourly_slugs(
    start: date,
    end: date,
    assets: Sequence[str] = ("BTC", "ETH"),
    hours: Iterable[int] = range(24),
) -> Iterator[StrikeSlug]:
    canon_assets = _normalise_assets(assets)
    hours_list = sorted({int(h) for h in hours})
    for d in _date_range(start, end):
        for a in canon_assets:
            for h in hours_list:
                yield StrikeSlug(HOURLY_KIND, a, d, h, hourly_slug(a, d, h))


def all_strike_slugs(
    start: date,
    end: date,
    assets: Sequence[str] = ("BTC", "ETH"),
    include_daily: bool = True,
    include_hourly: bool = True,
    hours: Iterable[int] = range(24),
) -> List[StrikeSlug]:
    """Return all candidate slugs for ``[start, end]`` (inclusive)."""
    out: List[StrikeSlug] = []
    if include_daily:
        out.extend(daily_slugs(start, end, assets))
    if include_hourly:
        out.extend(hourly_slugs(start, end, assets, hours))
    return out


def parse_iso_date(s: str | date | datetime) -> date:
    if isinstance(s, datetime):
        return s.date()
    if isinstance(s, date):
        return s
    return date.fromisoformat(str(s)[:10])

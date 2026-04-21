"""Data-quality layer for the backtester.

The guide is explicit: "No silent fallbacks that hide data gaps." This module
inspects the two data feeds the backtest depends on (Uniswap hourly candles
from The Graph, and the gas-price map from the Ethereum RPC sampler) and
returns a *structured* report — never raises on merely suspicious data, but
surfaces everything so downstream code / the summary / the operator can decide
what to do with it.

Pure I/O-free functions only (take already-fetched data as input). That keeps
them cheap to run in tests and gives us deterministic checks.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence

CANDLE_INTERVAL_SECS = 3600


@dataclass
class CandleQualityReport:
    """Structured summary of a candle series. Counts + concrete bad indices.

    Fields are intentionally JSON-safe so the report can be embedded verbatim
    in ``build_summary`` output.
    """

    candle_count: int = 0
    first_ts: Optional[int] = None
    last_ts: Optional[int] = None
    expected_count: Optional[int] = None
    missing_hours: int = 0
    gap_segments: List[Dict[str, int]] = field(default_factory=list)
    duplicate_ts: int = 0
    out_of_order: int = 0
    non_positive_close: int = 0
    non_positive_hl: int = 0
    fee_growth_non_monotonic: int = 0
    fee_growth_jumps_gt_1pct: int = 0

    @property
    def is_clean(self) -> bool:
        return (
            self.missing_hours == 0
            and self.duplicate_ts == 0
            and self.out_of_order == 0
            and self.non_positive_close == 0
            and self.non_positive_hl == 0
            and self.fee_growth_non_monotonic == 0
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def validate_candles(
    candles: Sequence[Dict[str, Any]],
    interval_secs: int = CANDLE_INTERVAL_SECS,
    jump_threshold_pct: float = 1.0,
) -> CandleQualityReport:
    """Inspect a sequence of poolHourData candles.

    Checks:
      * ``periodStartUnix`` monotonicity and uniqueness
      * hourly gaps vs the expected stride ``interval_secs``
      * non-positive ``close`` / ``low`` / ``high``
      * ``feeGrowthGlobal[01]X128`` non-decreasing (they accumulate on-chain)
      * optional flag for suspiciously large feeGrowth jumps
    """
    report = CandleQualityReport(candle_count=len(candles))
    if not candles:
        return report

    prev_ts: Optional[int] = None
    prev_fg0: Optional[int] = None
    prev_fg1: Optional[int] = None
    first_fg0: Optional[int] = None
    first_fg1: Optional[int] = None

    for idx, c in enumerate(candles):
        try:
            ts = int(c["periodStartUnix"])
        except (KeyError, TypeError, ValueError):
            report.out_of_order += 1
            continue

        if report.first_ts is None:
            report.first_ts = ts
        report.last_ts = ts

        if prev_ts is not None:
            if ts == prev_ts:
                report.duplicate_ts += 1
            elif ts < prev_ts:
                report.out_of_order += 1
            else:
                stride = ts - prev_ts
                if stride > interval_secs:
                    missing = stride // interval_secs - 1
                    if missing > 0:
                        report.missing_hours += missing
                        report.gap_segments.append({
                            "start_ts": prev_ts + interval_secs,
                            "end_ts": ts - interval_secs,
                            "missing_hours": int(missing),
                        })

        try:
            close = float(c.get("close", 0))
            low = float(c.get("low", 0))
            high = float(c.get("high", 0))
        except (TypeError, ValueError):
            report.non_positive_close += 1
            close = low = high = 0.0

        if close <= 0:
            report.non_positive_close += 1
        if low <= 0 or high <= 0 or high < low:
            report.non_positive_hl += 1

        fg0_raw = c.get("feeGrowthGlobal0X128")
        fg1_raw = c.get("feeGrowthGlobal1X128")
        try:
            fg0 = int(fg0_raw) if fg0_raw is not None else None
            fg1 = int(fg1_raw) if fg1_raw is not None else None
        except (TypeError, ValueError):
            fg0 = fg1 = None

        if fg0 is not None and prev_fg0 is not None:
            if fg0 < prev_fg0:
                report.fee_growth_non_monotonic += 1
            elif first_fg0 is not None and first_fg0 > 0:
                jump = (fg0 - prev_fg0) / first_fg0 * 100.0
                if jump > jump_threshold_pct:
                    report.fee_growth_jumps_gt_1pct += 1

        if fg1 is not None and prev_fg1 is not None:
            if fg1 < prev_fg1:
                report.fee_growth_non_monotonic += 1

        prev_ts = ts
        if fg0 is not None:
            prev_fg0 = fg0
            if first_fg0 is None:
                first_fg0 = fg0
        if fg1 is not None:
            prev_fg1 = fg1
            if first_fg1 is None:
                first_fg1 = fg1

    if report.first_ts is not None and report.last_ts is not None:
        span = report.last_ts - report.first_ts
        report.expected_count = span // interval_secs + 1

    return report


@dataclass
class GasCoverageReport:
    requested_days: int = 0
    covered_days: int = 0
    coverage_pct: float = 0.0
    missing_dates: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def validate_gas_coverage(
    start_date: str,
    end_date: str,
    gas_prices: Dict[str, int],
) -> GasCoverageReport:
    """Check how many days between ``start_date`` and ``end_date`` have gas data."""
    from datetime import timedelta

    start = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end = datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if end < start:
        return GasCoverageReport()

    total_days = (end - start).days + 1
    covered = 0
    missing: List[str] = []
    for i in range(total_days):
        d = (start + timedelta(days=i)).strftime("%Y-%m-%d")
        if gas_prices.get(d):
            covered += 1
        else:
            missing.append(d)

    return GasCoverageReport(
        requested_days=total_days,
        covered_days=covered,
        coverage_pct=(covered / total_days * 100.0) if total_days else 0.0,
        missing_dates=missing,
    )


@dataclass
class PolymarketCoverageReport:
    position_hours: int = 0
    hours_with_lower_bid: int = 0
    hours_with_upper_bid: int = 0
    hours_with_any_bid: int = 0
    lower_bid_coverage_pct: float = 0.0
    upper_bid_coverage_pct: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def validate_polymarket_coverage(snapshots: Iterable[Dict[str, Any]]) -> PolymarketCoverageReport:
    """Measure how often we actually had a historical bid for each side while
    a position was open. Low coverage means we're pricing insurance off a
    sparse feed — the operator should see this before trusting the equity
    curve's Polymarket leg.
    """
    position_hours = 0
    lower = 0
    upper = 0
    any_hours = 0
    for s in snapshots:
        if not s.get("position_open"):
            continue
        position_hours += 1
        has_lower = s.get("lower_bid") is not None
        has_upper = s.get("upper_bid") is not None
        if has_lower:
            lower += 1
        if has_upper:
            upper += 1
        if has_lower or has_upper:
            any_hours += 1

    return PolymarketCoverageReport(
        position_hours=position_hours,
        hours_with_lower_bid=lower,
        hours_with_upper_bid=upper,
        hours_with_any_bid=any_hours,
        lower_bid_coverage_pct=(lower / position_hours * 100.0) if position_hours else 0.0,
        upper_bid_coverage_pct=(upper / position_hours * 100.0) if position_hours else 0.0,
    )

#!/usr/bin/env python3
"""
Generate an interactive Plotly HTML report from active_backtest_results.json.

The report uses:
- summary["snapshots"] for hourly telemetry (strategy vs HODL, decomposition, Polymarket bids)
- summary["positions"]  for per-position bars and the position timeline
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import plotly.graph_objects as go
from plotly.subplots import make_subplots


def _dt_utc(ts: int) -> datetime:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc)


def _dt_from_iso(s: str) -> datetime:
    # input is like "2026-01-13T18:00:00+00:00"
    return datetime.fromisoformat(s)


def _as_float(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default


@dataclass(frozen=True)
class PositionEvent:
    idx: int
    open_dt: datetime
    close_dt: datetime
    mn: float
    mx: float
    touched_lower: bool
    touched_upper: bool
    il_usdc: float
    ins_cost: float
    ins_payout: float
    ins_sellback: float
    spread_buy: float
    spread_sell: float
    swap_fee: float
    gas_open: float
    gas_close: float

    @property
    def reason(self) -> str:
        if self.touched_lower:
            return "LOWER"
        if self.touched_upper:
            return "UPPER"
        return "END"


def load_summary(path: Path) -> Dict[str, Any]:
    with path.open("r") as f:
        return json.load(f)


def parse_positions(summary: Dict[str, Any]) -> List[PositionEvent]:
    out: List[PositionEvent] = []
    for i, p in enumerate(summary.get("positions", []), start=1):
        rng = p.get("range") or [None, None]
        out.append(
            PositionEvent(
                idx=i,
                open_dt=_dt_from_iso(p["open"]),
                close_dt=_dt_from_iso(p["close"]),
                mn=_as_float(rng[0]),
                mx=_as_float(rng[1]),
                touched_lower=bool(p.get("touched_lower", False)),
                touched_upper=bool(p.get("touched_upper", False)),
                il_usdc=_as_float(p.get("il_usdc", 0.0)),
                ins_cost=_as_float(p.get("insurance_cost_usdc", 0.0)),
                ins_payout=_as_float(p.get("insurance_payout_usdc", 0.0)),
                ins_sellback=_as_float(p.get("insurance_sellback_usdc", 0.0)),
                spread_buy=_as_float(p.get("spread_cost_buy_usdc", 0.0)),
                spread_sell=_as_float(p.get("spread_cost_sell_usdc", 0.0)),
                swap_fee=_as_float(p.get("swap_fee_usdc", 0.0)),
                gas_open=_as_float(p.get("gas_fee_open_usdc", 0.0)),
                gas_close=_as_float(p.get("gas_fee_close_usdc", 0.0)),
            )
        )
    return out


def parse_snapshots(summary: Dict[str, Any]) -> Dict[str, List[Any]]:
    snaps = summary.get("snapshots", [])
    xs = [_dt_utc(s["ts"]) for s in snaps]

    def col(k: str) -> List[float]:
        return [_as_float(s.get(k, 0.0)) for s in snaps]

    # sparse bids: use None to create gaps
    lower_bid = [s.get("lower_bid", None) for s in snaps]
    upper_bid = [s.get("upper_bid", None) for s in snaps]

    return {
        "x": xs,
        "price": col("price"),
        "hodl_usd": col("hodl_usd"),
        "strategy_usd": col("strategy_usd"),
        "lp_value_usd": col("lp_value_usd"),
        "fees_accrued_usd": col("fees_accrued_usd"),
        "poly_equity_usd": col("poly_equity_usd"),
        "lower_bid": lower_bid,
        "upper_bid": upper_bid,
    }


def _tick_daily_layout(fig: go.Figure) -> None:
    fig.update_xaxes(
        tickformat="%b %d",
        dtick="D1",
        showgrid=True,
    )


def make_equity_figure(
    title: str,
    snaps: Dict[str, List[Any]],
    positions: List[PositionEvent],
    final_value_usd: Optional[float],
) -> go.Figure:
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=snaps["x"],
            y=snaps["strategy_usd"],
            name="Strategy (LP+Poly)",
            mode="lines",
        )
    )
    fig.add_trace(
        go.Scatter(
            x=snaps["x"],
            y=snaps["hodl_usd"],
            name="HODL (initial quantities)",
            mode="lines",
        )
    )

    # Position close markers (vertical lines) with reason color.
    for p in positions:
        color = {"LOWER": "royalblue", "UPPER": "firebrick", "END": "gray"}[p.reason]
        fig.add_vline(
            x=p.close_dt,
            line_width=1,
            line_dash="dot",
            line_color=color,
        )

    if final_value_usd is not None:
        fig.add_hline(y=final_value_usd, line_width=1, line_dash="dash", line_color="gray")

    fig.update_layout(
        title=title,
        legend_orientation="h",
        margin=dict(l=50, r=20, t=60, b=40),
        height=420,
        yaxis_title="USD",
    )
    _tick_daily_layout(fig)
    return fig


def make_decomposition_figure(snaps: Dict[str, List[Any]]) -> go.Figure:
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=snaps["x"],
            y=snaps["lp_value_usd"],
            name="LP value (mark-to-market)",
            mode="lines",
            stackgroup="one",
        )
    )
    fig.add_trace(
        go.Scatter(
            x=snaps["x"],
            y=snaps["fees_accrued_usd"],
            name="Accrued fees (in-kind, valued)",
            mode="lines",
            stackgroup="one",
        )
    )
    fig.add_trace(
        go.Scatter(
            x=snaps["x"],
            y=snaps["poly_equity_usd"],
            name="Polymarket equity (bid)",
            mode="lines",
            stackgroup="one",
        )
    )
    # Overlay total
    fig.add_trace(
        go.Scatter(
            x=snaps["x"],
            y=snaps["strategy_usd"],
            name="Strategy total",
            mode="lines",
            line=dict(width=2),
        )
    )
    fig.update_layout(
        title="Equity decomposition (stacked components)",
        legend_orientation="h",
        margin=dict(l=50, r=20, t=60, b=40),
        height=420,
        yaxis_title="USD",
    )
    _tick_daily_layout(fig)
    return fig


def make_poly_bids_figure(snaps: Dict[str, List[Any]]) -> go.Figure:
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=snaps["x"],
            y=snaps["upper_bid"],
            name="Upper bid",
            mode="lines",
            connectgaps=False,
        )
    )
    fig.add_trace(
        go.Scatter(
            x=snaps["x"],
            y=snaps["lower_bid"],
            name="Lower bid",
            mode="lines",
            connectgaps=False,
        )
    )
    fig.update_layout(
        title="Polymarket bid telemetry (held contracts)",
        legend_orientation="h",
        margin=dict(l=50, r=20, t=60, b=40),
        height=360,
        yaxis_title="$ per contract",
        yaxis_range=[0, 1],
    )
    _tick_daily_layout(fig)
    return fig


def make_positions_timeline_figure(positions: List[PositionEvent]) -> go.Figure:
    fig = go.Figure()
    for p in positions:
        color = {"LOWER": "royalblue", "UPPER": "firebrick", "END": "gray"}[p.reason]
        fig.add_trace(
            go.Bar(
                x=[(p.close_dt - p.open_dt).total_seconds() / 3600.0],
                y=[f"#{p.idx} [{p.mn:.0f}-{p.mx:.0f}]"],
                orientation="h",
                base=p.open_dt,
                name=p.reason,
                marker_color=color,
                hovertemplate=(
                    "Pos %{y}<br>"
                    "Open: %{base|%Y-%m-%d %H:%M}<br>"
                    "Hours: %{x:.0f}<br>"
                    "Reason: " + p.reason + "<extra></extra>"
                ),
                showlegend=False,
            )
        )

    fig.update_layout(
        title="Position timeline",
        margin=dict(l=80, r=20, t=60, b=40),
        height=max(320, 60 + 35 * max(1, len(positions))),
        xaxis_title="Time",
        yaxis_title="Positions",
    )
    fig.update_xaxes(
        tickformat="%b %d",
        dtick="D1",
    )
    return fig


def make_position_bars_figure(positions: List[PositionEvent]) -> go.Figure:
    xs = [f"#{p.idx}" for p in positions]
    il = [p.il_usdc for p in positions]
    payout = [p.ins_payout for p in positions]
    sellback = [p.ins_sellback for p in positions]
    cost = [p.ins_cost for p in positions]
    spread = [p.spread_buy + p.spread_sell for p in positions]
    swap = [p.swap_fee for p in positions]
    gas = [p.gas_open + p.gas_close for p in positions]

    fig = go.Figure()
    fig.add_trace(go.Bar(x=xs, y=il, name="IL (USDC)", offsetgroup="il"))
    fig.add_trace(go.Bar(x=xs, y=payout, name="Insurance payout", offsetgroup="ins"))
    fig.add_trace(go.Bar(x=xs, y=sellback, name="Insurance sellback", offsetgroup="ins"))
    fig.add_trace(go.Bar(x=xs, y=[-c for c in cost], name="Insurance cost (paid)", offsetgroup="cost"))
    fig.add_trace(go.Bar(x=xs, y=[-c for c in spread], name="Spread cost", offsetgroup="cost"))
    fig.add_trace(go.Bar(x=xs, y=[-c for c in swap], name="Swap fee", offsetgroup="cost"))
    fig.add_trace(go.Bar(x=xs, y=[-c for c in gas], name="Gas fees", offsetgroup="cost"))

    fig.update_layout(
        title="Per-position IL vs insurance and costs",
        barmode="relative",
        legend_orientation="h",
        margin=dict(l=50, r=20, t=60, b=40),
        height=420,
        yaxis_title="USDC (signed)",
    )
    return fig


def build_report(summary: Dict[str, Any], title: str) -> go.Figure:
    positions = parse_positions(summary)
    snaps = parse_snapshots(summary)
    final_value_usd = _as_float(summary.get("final_wallet", {}).get("value_usd", None), default=None)  # type: ignore[arg-type]

    # 5 stacked rows, each a full-width subplot.
    fig = make_subplots(
        rows=5,
        cols=1,
        shared_xaxes=False,
        vertical_spacing=0.08,
        subplot_titles=(
            "Equity curve (Strategy vs HODL)",
            "Equity decomposition",
            "Position timeline",
            "Per-position IL / insurance / costs",
            "Polymarket bid telemetry",
        ),
        specs=[
            [{"type": "xy"}],
            [{"type": "xy"}],
            [{"type": "xy"}],
            [{"type": "xy"}],
            [{"type": "xy"}],
        ],
    )

    eq = make_equity_figure("Equity curve", snaps, positions, final_value_usd)
    for tr in eq.data:
        fig.add_trace(tr, row=1, col=1)
    for shape in getattr(eq.layout, "shapes", []) or []:
        fig.add_shape(shape, row=1, col=1)

    dec = make_decomposition_figure(snaps)
    for tr in dec.data:
        fig.add_trace(tr, row=2, col=1)

    tl = make_positions_timeline_figure(positions)
    for tr in tl.data:
        fig.add_trace(tr, row=3, col=1)

    bars = make_position_bars_figure(positions)
    for tr in bars.data:
        fig.add_trace(tr, row=4, col=1)

    bids = make_poly_bids_figure(snaps)
    for tr in bids.data:
        fig.add_trace(tr, row=5, col=1)

    # Global layout tuning
    fig.update_layout(
        title=title,
        height=5 * 420,
        margin=dict(l=50, r=20, t=80, b=40),
        legend_orientation="h",
    )

    # Daily ticks on time-series rows
    for r in (1, 2, 5):
        fig.update_xaxes(tickformat="%b %d", dtick="D1", row=r, col=1)

    return fig


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate Plotly HTML report from backtest JSON")
    ap.add_argument("--input", default="active_backtest_results.json", help="Input JSON path")
    ap.add_argument("--output", default="report.html", help="Output HTML path")
    ap.add_argument("--title", default="Backtest Report (LP + Polymarket)", help="Report title")
    args = ap.parse_args()

    in_path = Path(args.input)
    out_path = Path(args.output)
    summary = load_summary(in_path)
    fig = build_report(summary, args.title)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.write_html(str(out_path), include_plotlyjs="cdn", full_html=True)
    print(f"Wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


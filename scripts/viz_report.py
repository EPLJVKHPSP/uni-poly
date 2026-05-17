#!/usr/bin/env python3
"""
Generate an interactive Plotly HTML report from active_backtest_results.json.

The report uses:
- summary["snapshots"] for hourly telemetry (strategy vs HODL, decomposition, Polymarket bids)
- summary["positions"]  for per-position bars and the position timeline
"""

from __future__ import annotations
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

PricingMode = Literal["exec", "mid"]

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


def _fmt_money(x: Any, places: int = 2) -> str:
    try:
        v = float(x)
    except Exception:
        return "—"
    if v != v:  # NaN
        return "—"
    return f"${v:,.{places}f}"


def _fmt_num(x: Any, places: int = 6) -> str:
    try:
        v = float(x)
    except Exception:
        return "—"
    if v != v:
        return "—"
    return f"{v:,.{places}f}"


def _fmt_pct(x: Any, places: int = 2) -> str:
    try:
        v = float(x)
    except Exception:
        return "—"
    if v == float("inf"):
        return "inf"
    if v == float("-inf"):
        return "-inf"
    if v != v:
        return "—"
    return f"{v:,.{places}f}%"


def _compute_apy(final_value_usd: Any, investment_usd: Any, total_days: Any) -> Optional[float]:
    try:
        fv = float(final_value_usd)
        inv = float(investment_usd)
        days = float(total_days)
    except Exception:
        return None
    if inv <= 0 or days <= 0 or fv <= 0:
        return 0.0
    try:
        return ((fv / inv) ** (365.0 / days) - 1.0) * 100.0
    except OverflowError:
        return float("inf") if fv > inv else float("-inf")


def _run_config_block(summary: Dict[str, Any]) -> str:
    rm = summary.get("run_metadata") or {}
    warnings = rm.get("warnings") or []
    incomplete = bool(rm.get("incomplete", False))

    keys = [
        "pool_id",
        "days",
        "lookback_days",
        "cooldown_hours",
        "price_token",
        "fixed_range",
        "spread",
        "slippage_per_1k_contracts",
        "slippage_max_per_contract",
        "close_policy",
        "selection_mode",
        "capital_model",
        "initial_eth",
        "initial_usdc",
        "incomplete",
    ]
    lines = ["<b>Run configuration</b>"]
    for k in keys:
        if k in rm:
            lines.append(f"<b>{k}</b>: {rm.get(k)}")
    if incomplete and warnings:
        lines.append("<br><b>Warnings</b>:")
        for w in warnings:
            msg = w.get("message") or w.get("type") or "warning"
            sev = w.get("severity", "warning")
            lines.append(f"- [{sev}] {msg}")
    return "<br>".join(lines)


def _exec_mid_roi_apy(summary: Dict[str, Any]) -> Dict[str, Optional[float]]:
    """Resolve EXEC and MID ROI/APY (with auditable fallback for MID)."""
    a = summary.get("active_strategy") or {}
    investment = summary.get("investment_usd")
    total_days = (summary.get("period") or {}).get("total_days")

    exec_roi = a.get("roi_pct")
    exec_apy = a.get("apy")

    mid_cf = (summary.get("counterfactuals") or {}).get("db_mid_execution") or {}
    mid_roi = mid_cf.get("roi_pct")
    mid_apy = mid_cf.get("apy")
    if mid_roi is None or mid_apy is None:
        fv = a.get("final_value_usd")
        spread_cost = a.get("total_spread_cost_usdc", 0.0)
        slip_cost = a.get("total_slippage_cost_usdc", 0.0)
        try:
            mid_fv = float(fv) + float(spread_cost) + float(slip_cost)
        except Exception:
            mid_fv = None
        if mid_fv is not None:
            try:
                mid_roi = (mid_fv / float(investment) - 1.0) * 100.0 if investment else None
            except Exception:
                mid_roi = None
            mid_apy = _compute_apy(mid_fv, investment, total_days)

    hodl = (summary.get("baselines") or {}).get("hodl") or {}
    hodl_roi = hodl.get("roi_pct")
    hodl_apy = hodl.get("apy")
    if hodl_apy is None:
        hodl_apy = _compute_apy(hodl.get("final_value_usd"), investment, total_days)

    def _diff(a_: Optional[float], b_: Optional[float]) -> Optional[float]:
        if a_ is None or b_ is None:
            return None
        return float(a_) - float(b_)

    return {
        "exec_roi": exec_roi,
        "exec_apy": exec_apy,
        "mid_roi": mid_roi,
        "mid_apy": mid_apy,
        "hodl_roi": hodl_roi,
        "hodl_apy": hodl_apy,
        "exec_vs_hodl_roi": _diff(exec_roi, hodl_roi),
        "exec_vs_hodl_apy": _diff(exec_apy, hodl_apy),
        "mid_vs_hodl_roi": _diff(mid_roi, hodl_roi),
        "mid_vs_hodl_apy": _diff(mid_apy, hodl_apy),
    }


def _fmt_pp(v: Optional[float], places: int = 2) -> str:
    """Format an ROI/APY *delta* in percentage points."""
    if v is None:
        return "—"
    try:
        return f"{float(v):+,.{places}f} pp"
    except Exception:
        return "—"


def make_headline_roi_table(summary: Dict[str, Any]) -> go.Figure:
    """Compact headline ROI/APY summary — the most important numbers, on top.

    Two columns (EXEC / MID). The first row shows the *outperformance vs
    HODL* (the bottom-line answer), then the strategy and HODL legs are
    listed underneath for context."""
    m = _exec_mid_roi_apy(summary)

    def _cell(roi: Optional[float], apy: Optional[float]) -> str:
        return f"{_fmt_pct(roi)} ROI · {_fmt_pct(apy)} APY"

    def _cell_pp(roi: Optional[float], apy: Optional[float]) -> str:
        return f"{_fmt_pp(roi)} ROI · {_fmt_pp(apy)} APY"

    rows: List[Tuple[str, str, str]] = [
        (
            "<b>Strategy vs HODL</b>",
            _cell_pp(m["exec_vs_hodl_roi"], m["exec_vs_hodl_apy"]),
            _cell_pp(m["mid_vs_hodl_roi"], m["mid_vs_hodl_apy"]),
        ),
        (
            "Strategy",
            _cell(m["exec_roi"], m["exec_apy"]),
            _cell(m["mid_roi"], m["mid_apy"]),
        ),
        (
            "HODL baseline (initial 50/50)",
            _cell(m["hodl_roi"], m["hodl_apy"]),
            _cell(m["hodl_roi"], m["hodl_apy"]),
        ),
    ]

    col0 = [r[0] for r in rows]
    col1 = [r[1] for r in rows]
    col2 = [r[2] for r in rows]

    headline_color = ["rgb(248,250,252)"] * len(rows)
    headline_color[0] = "rgb(232,243,232)"

    fig = go.Figure(
        data=[
            go.Table(
                header=dict(
                    values=["", "<b>EXEC (with premium)</b>", "<b>MID (no premium)</b>"],
                    fill_color="rgb(238,242,247)",
                    line_color="rgb(220,220,220)",
                    font=dict(size=13, color="rgb(20,30,40)"),
                    height=32,
                    align="left",
                ),
                cells=dict(
                    values=[col0, col1, col2],
                    align=["left", "right", "right"],
                    fill_color=[headline_color, headline_color, headline_color],
                    line_color="rgb(232,232,232)",
                    font=dict(size=13, family="Inter, ui-sans-serif, system-ui"),
                    height=30,
                ),
                columnwidth=[0.34, 0.33, 0.33],
            )
        ]
    )
    fig.update_layout(margin=dict(l=10, r=10, t=4, b=4), height=140)
    return fig


def make_summary_table(summary: Dict[str, Any]) -> go.Figure:
    token = summary.get("token_symbol", "ETH")
    a = summary.get("active_strategy") or {}
    baselines = summary.get("baselines") or {}
    hodl = baselines.get("hodl") or {}
    dm = baselines.get("delta_matched_hodl") or {}
    total_days = (summary.get("period") or {}).get("total_days")
    investment = summary.get("investment_usd")

    exec_roi = a.get("roi_pct")
    exec_apy = a.get("apy")

    mid_cf = (summary.get("counterfactuals") or {}).get("db_mid_execution") or {}
    mid_roi = mid_cf.get("roi_pct")
    mid_apy = mid_cf.get("apy")
    if mid_roi is None or mid_apy is None:
        # Fallback (auditable): derive MID counterfactual by adding back execution drag.
        # This matches the simulation-side approximation when position-level decomposition
        # is unavailable in the JSON schema.
        fv = a.get("final_value_usd")
        spread_cost = a.get("total_spread_cost_usdc", 0.0)
        slip_cost = a.get("total_slippage_cost_usdc", 0.0)
        try:
            mid_fv = float(fv) + float(spread_cost) + float(slip_cost)
        except Exception:
            mid_fv = None
        if mid_fv is not None:
            try:
                mid_roi = (mid_fv / float(investment) - 1.0) * 100.0 if investment else None
            except Exception:
                mid_roi = None
            mid_apy = _compute_apy(mid_fv, investment, total_days)

    hodl_roi = hodl.get("roi_pct")
    hodl_apy = hodl.get("apy")
    if hodl_apy is None:
        hodl_apy = _compute_apy(hodl.get("final_value_usd"), investment, total_days)

    dm_roi = dm.get("roi_pct", None)
    dm_apy = dm.get("apy", None)
    if dm and dm_apy is None:
        dm_apy = _compute_apy(dm.get("final_value_usd"), investment, total_days)

    iw = summary.get("initial_wallet") or {}
    fw = summary.get("final_wallet") or {}
    td = summary.get("token_delta") or {}

    exec = summary.get("polymarket_execution") or {}
    exec_actual = exec.get("actual") or {}
    exec_mid = exec.get("mid_price_counterfactual") or {}

    strat = summary.get("active_strategy") or {}
    fees_usdc = strat.get("total_fees_usdc")
    fees_eth = strat.get("total_fees_eth")
    fees_usd = strat.get("total_fees_usd")

    rows: List[List[str]] = [
        ["<b>ROI / APY comparison</b>", "", ""],
        ["Variant", "ROI", "APY"],
        ["Standard-bets-prices (MID)", _fmt_pct(mid_roi), _fmt_pct(mid_apy)],
        ["Prices-of-bets-with-premium (EXEC)", _fmt_pct(exec_roi), _fmt_pct(exec_apy)],
        ["HOLD baseline (50/50 at entry)", _fmt_pct(hodl_roi), _fmt_pct(hodl_apy)],
    ]
    if dm:
        rows.append(["HOLD baseline (delta-matched)", _fmt_pct(dm_roi), _fmt_pct(dm_apy)])

    rows += [
        ["", "", ""],
        ["<b>Token accounting</b>", "", ""],
        ["Initial wallet", f"{_fmt_money(iw.get('USDC'))} USDC", f"{_fmt_num(iw.get(token), 6)} {token}"],
        ["Final wallet", f"{_fmt_money(fw.get('USDC'))} USDC", f"{_fmt_num(fw.get(token), 6)} {token}"],
        ["Token delta", f"{_fmt_money(td.get('USDC'))} USDC", f"{_fmt_num(td.get(token), 6)} {token}"],
        ["Wallet value (USD)", _fmt_money(iw.get("value_usd")), _fmt_money(fw.get("value_usd"))],
        ["Cost basis (USD)", _fmt_money(strat.get("cost_basis_usd")), ""],
        ["Strategy total value (USD)", _fmt_money(strat.get("final_value_usd")), ""],
        ["LP-only final value (USD)", _fmt_money(strat.get("lp_final_value_usd")), ""],
        ["", "", ""],
        ["<b>Coverage (insurance) spend & outcomes</b>", "", ""],
        ["Spent on coverage", _fmt_money(strat.get("total_insurance_cost_usdc")), ""],
        ["Premium: spread cost", _fmt_money(strat.get("total_spread_cost_usdc")), ""],
        ["Premium: slippage cost", _fmt_money(strat.get("total_slippage_cost_usdc")), ""],
        ["Got back: payout", _fmt_money(strat.get("total_insurance_payout_usdc")), ""],
        ["Got back: sellback", _fmt_money(strat.get("total_insurance_sellback_usdc")), ""],
        ["Net insurance", _fmt_money(strat.get("total_insurance_net_usdc")), ""],
        ["", "", ""],
        ["<b>Fees</b>", "", ""],
        ["Uniswap fees", f"{_fmt_money(fees_usdc)} USDC", f"{_fmt_num(fees_eth, 6)} {token} (≈ {_fmt_money(fees_usd)})"],
        ["", "", ""],
        ["<b>Polymarket pricing audit</b>", "", ""],
        ["Insurance buy cost (EXEC)", _fmt_money(exec_actual.get("insurance_buy_cost_usd")), ""],
        ["Insurance buy cost (MID, derived)", _fmt_money(exec_mid.get("insurance_buy_cost_usd")), ""],
        ["Insurance sellback (EXEC)", _fmt_money(exec_actual.get("insurance_sellback_usd")), ""],
        ["Insurance sellback (MID, derived)", _fmt_money(exec_mid.get("insurance_sellback_usd")), ""],
        ["Total execution drag (spread+slip)", _fmt_money(exec_mid.get("exec_drag_total_usd")), ""],
    ]

    col0 = [r[0] for r in rows]
    col1 = [r[1] for r in rows]
    col2 = [r[2] for r in rows]

    fig = go.Figure(
        data=[
            go.Table(
                header=dict(
                    values=["", "", ""],
                    fill_color="rgb(245,245,245)",
                    line_color="rgb(220,220,220)",
                    font=dict(size=12),
                    height=26,
                    align="center",
                ),
                cells=dict(
                    values=[col0, col1, col2],
                    align=["center", "center", "center"],
                    fill_color="white",
                    line_color="rgb(235,235,235)",
                    font=dict(size=12),
                    height=24,
                ),
                columnwidth=[0.52, 0.24, 0.24],
            )
        ]
    )
    fig.update_layout(margin=dict(l=10, r=10, t=10, b=10), height=560)
    return fig


def make_balances_table(summary: Dict[str, Any]) -> go.Figure:
    """Compact balance/cashflow table comparing EXEC vs MID."""
    token = summary.get("token_symbol", "ETH")
    a = summary.get("active_strategy") or {}
    baselines = summary.get("baselines") or {}
    hodl = baselines.get("hodl") or {}
    exec_mid = (summary.get("polymarket_execution") or {}).get("mid_price_counterfactual") or {}
    mid_cf = (summary.get("counterfactuals") or {}).get("db_mid_execution") or {}

    iw = summary.get("initial_wallet") or {}
    fw = summary.get("final_wallet") or {}

    init_usdc = _as_float(iw.get("USDC"))
    init_tok = _as_float(iw.get(token))
    init_usd = _as_float(iw.get("value_usd"))

    hodl_final_usd = _as_float(hodl.get("final_value_usd"), default=float("nan"))

    final_usdc = _as_float(fw.get("USDC"))
    final_tok = _as_float(fw.get(token))
    final_lp_usd = _as_float(fw.get("value_usd"))

    gas_usd = _as_float(a.get("total_gas_fees_usdc"))
    uni_fees_usd = _as_float(a.get("total_fees_usd"))

    poly_buy_exec = _as_float(a.get("total_insurance_cost_usdc"))
    poly_buy_mid = _as_float(exec_mid.get("insurance_buy_cost_usd"))

    poly_payout = _as_float(a.get("total_insurance_payout_usdc"))
    poly_sell_exec = _as_float(a.get("total_insurance_sellback_usdc"))
    poly_sell_mid = _as_float(exec_mid.get("insurance_sellback_usd"))

    last_sell_exec = 0.0
    last_sell_mid = None
    try:
        ps = summary.get("positions") or []
        if ps:
            last = ps[-1]
            last_sell_exec = _as_float(last.get("insurance_sellback_usdc", 0.0))
            # MID last-position sellback can be reconstructed from EXEC sellback
            # plus execution drag on sell (spread+slippage), which is recorded per position.
            # This is consistent with build_summary's aggregate mid_sellback derivation.
            last_sp = _as_float(last.get("spread_cost_sell_usdc", 0.0))
            last_sl = _as_float(last.get("slippage_cost_sell_usdc", 0.0))
            last_sell_mid = last_sell_exec + last_sp + last_sl
    except Exception:
        last_sell_exec = 0.0
        last_sell_mid = None

    exec_final_total = _as_float(a.get("final_value_usd"))
    mid_final_total = _as_float(mid_cf.get("final_value_usd"))

    exec_cost_basis = _as_float(a.get("cost_basis_usd"))
    mid_cost_basis = exec_cost_basis
    buy_premium = _as_float(exec_mid.get("exec_premium_buy_usd"), default=0.0)
    if buy_premium:
        mid_cost_basis = exec_cost_basis - buy_premium

    exec_roi = a.get("roi_pct")
    exec_apy = a.get("apy")
    mid_roi = mid_cf.get("roi_pct")
    mid_apy = mid_cf.get("apy")

    rows: List[Tuple[str, Any, Any]] = [
        ("<b>Performance (EXEC vs MID)</b>", "", ""),
        ("Cost basis (USD)", _fmt_money(exec_cost_basis), _fmt_money(mid_cost_basis)),
        ("Strategy total value (USD)", _fmt_money(exec_final_total), _fmt_money(mid_final_total)),
        ("ROI / APY", f"{_fmt_pct(exec_roi)} / {_fmt_pct(exec_apy)}", f"{_fmt_pct(mid_roi)} / {_fmt_pct(mid_apy)}"),
        ("", "", ""),
        ("<b>Deposited to LP at entry</b>", "", ""),
        (f"Initial {token}", f"{_fmt_num(init_tok, 6)} {token}", f"{_fmt_num(init_tok, 6)} {token}"),
        ("Initial USDC", f"{_fmt_num(init_usdc, 2)} USDC", f"{_fmt_num(init_usdc, 2)} USDC"),
        ("Initial notional (USD)", _fmt_money(init_usd), _fmt_money(init_usd)),
        ("", "", ""),
        ("<b>HODL repriced at end (same quantities)</b>", "", ""),
        ("Same initial quantities @ final price (USD)", _fmt_money(hodl_final_usd), _fmt_money(hodl_final_usd)),
        ("", "", ""),
        ("<b>LP wallet after backtest (after closing)</b>", "", ""),
        (f"Final {token}", f"{_fmt_num(final_tok, 6)} {token}", f"{_fmt_num(final_tok, 6)} {token}"),
        ("Final USDC", f"{_fmt_num(final_usdc, 2)} USDC", f"{_fmt_num(final_usdc, 2)} USDC"),
        ("Final LP value (USD)", _fmt_money(final_lp_usd), _fmt_money(final_lp_usd)),
        ("", "", ""),
        ("<b>Cashflows</b>", "", ""),
        ("Gas spent (USD)", _fmt_money(gas_usd), _fmt_money(gas_usd)),
        ("Polymarket deposited (buy cost)", _fmt_money(poly_buy_exec), _fmt_money(poly_buy_mid)),
        ("Polymarket payout (total)", _fmt_money(poly_payout), _fmt_money(poly_payout)),
        ("Polymarket sellback (total)", _fmt_money(poly_sell_exec), _fmt_money(poly_sell_mid)),
        ("Polymarket sellback (last position)", _fmt_money(last_sell_exec), _fmt_money(last_sell_mid)),
        ("Uniswap fees earned (USD)", _fmt_money(uni_fees_usd), _fmt_money(uni_fees_usd)),
    ]

    col0 = [r[0] for r in rows]
    col1 = [r[1] for r in rows]
    col2 = [r[2] for r in rows]

    fig = go.Figure(
        data=[
            go.Table(
                header=dict(
                    values=["", "<b>EXEC (with premium)</b>", "<b>MID (no premium)</b>"],
                    fill_color="rgb(245,245,245)",
                    line_color="rgb(220,220,220)",
                    font=dict(size=12),
                    height=28,
                    align="left",
                ),
                cells=dict(
                    values=[col0, col1, col2],
                    align=["left", "right", "right"],
                    fill_color="white",
                    line_color="rgb(235,235,235)",
                    font=dict(size=12),
                    height=24,
                ),
                columnwidth=[0.56, 0.22, 0.22],
            )
        ]
    )
    fig.update_layout(margin=dict(l=10, r=10, t=10, b=10), height=640)
    return fig


def _position_duration_days(p: Dict[str, Any]) -> str:
    """Hold period from open to close as a compact day count."""
    try:
        o = _dt_from_iso(str(p["open"]))
        c = _dt_from_iso(str(p["close"]))
        days = (c - o).total_seconds() / 86400.0
        if days < 0.05:
            return "<1d"
        if days < 10:
            return f"{days:.1f}d"
        return f"{days:.0f}d"
    except Exception:
        return "—"


def _insurance_amounts(p: Dict[str, Any], pricing: PricingMode) -> Dict[str, float]:
    """Insurance cashflows at EXEC or reconstructed MID prices."""
    payout = _as_float(p.get("insurance_payout_usdc"))
    buy_exec = _as_float(p.get("insurance_cost_usdc"))
    sell_exec = _as_float(p.get("insurance_sellback_usdc"))
    if pricing == "exec":
        buy, sell = buy_exec, sell_exec
    else:
        buy = max(
            0.0,
            buy_exec
            - _as_float(p.get("spread_cost_buy_usdc"))
            - _as_float(p.get("slippage_cost_buy_usdc")),
        )
        sell = (
            sell_exec
            + _as_float(p.get("spread_cost_sell_usdc"))
            + _as_float(p.get("slippage_cost_sell_usdc"))
        )
    return {"buy": buy, "sell": sell, "payout": payout, "net": payout + sell - buy}


def _ins_pct_of_deposit(p: Dict[str, Any], ins_buy: float) -> Optional[float]:
    deposit_usd = _as_float((p.get("deposit") or {}).get("value_usd"), default=None)
    if deposit_usd and deposit_usd > 0:
        return ins_buy / deposit_usd * 100.0
    return None


def _rebalance_table_headers(pricing: PricingMode) -> List[str]:
    """Shared column layout for EXEC and MID trade lists (only insurance labels differ)."""
    tag = "EXEC" if pricing == "exec" else "MID"
    return [
        "<b>#</b>",
        "<b>Days</b>",
        "<b>Why entered</b>",
        "<b>Why closed</b>",
        "<b>Range</b>",
        "<b>Width</b>",
        "<b>Buf ↓</b>",
        "<b>Buf ↑</b>",
        f"<b>Ins buy ({tag})</b>",
        "<b>Ins / Dep</b>",
        "<b>Ins payout</b>",
        f"<b>Ins sell ({tag})</b>",
        f"<b>Ins net ({tag})</b>",
        "<b>Fees</b>",
        "<b>IL</b>",
        "<b>Δ Wallet</b>",
    ]


# Relative widths for the 15-column trade table (must sum ≈ 1.0).
_REBALANCE_COL_WIDTHS: List[float] = [
    0.03, 0.04, 0.08, 0.08, 0.09, 0.045, 0.045, 0.045,
    0.075, 0.055, 0.065, 0.075, 0.075, 0.055, 0.055, 0.075,
]


def _rebalance_table_rows(summary: Dict[str, Any], pricing: PricingMode) -> List[Tuple[Any, ...]]:
    rows: List[Tuple[Any, ...]] = []
    prev_reason: Optional[str] = None
    for i, p in enumerate(summary.get("positions") or [], start=1):
        rng = p.get("range") or [None, None]
        mn = _as_float(rng[0], default=None)
        mx = _as_float(rng[1], default=None)
        reason = str(p.get("close_reason") or "")
        trigger_short, _ = _entry_trigger(prev_reason, i)
        prev_reason = reason
        m = _position_entry_metrics(p)
        ins = _insurance_amounts(p, pricing)
        ins_pct = _ins_pct_of_deposit(p, ins["buy"])

        wb = p.get("wallet_before") or {}
        wa = p.get("wallet_after") or {}
        d_wallet = _as_float(wa.get("value_usd")) - _as_float(wb.get("value_usd"))

        rows.append(
            (
                i,
                _position_duration_days(p),
                trigger_short,
                reason,
                f"[{_fmt_num(mn, 0)}–{_fmt_num(mx, 0)}]" if mn is not None and mx is not None else "",
                _fmt_pct(m["width_pct"], 1),
                _fmt_pct(m["lower_buf_pct"], 1),
                _fmt_pct(m["upper_buf_pct"], 1),
                _fmt_money(ins["buy"]),
                _fmt_pct(ins_pct, 2),
                _fmt_money(ins["payout"]),
                _fmt_money(ins["sell"]),
                _fmt_money(ins["net"]),
                _fmt_money(p.get("fees_earned_usd")),
                _fmt_money(p.get("il_usdc")),
                _fmt_money(d_wallet),
            )
        )
    return rows


def make_rebalances_table(summary: Dict[str, Any], pricing: PricingMode = "exec") -> go.Figure:
    """Per-position ledger; identical layout for EXEC and MID (insurance columns tagged)."""
    headers = _rebalance_table_headers(pricing)
    rows = _rebalance_table_rows(summary, pricing)
    cols = list(zip(*rows)) if rows else [tuple() for _ in headers]
    n_rows = max(len(rows), 1)

    fig = go.Figure(
        data=[
            go.Table(
                header=dict(
                    values=headers,
                    fill_color="rgb(245,245,245)",
                    line_color="rgb(220,220,220)",
                    font=dict(size=11),
                    height=28,
                    align="left",
                ),
                cells=dict(
                    values=[list(c) for c in cols],
                    fill_color="white",
                    line_color="rgb(235,235,235)",
                    font=dict(size=10, family="Inter, ui-sans-serif, system-ui"),
                    height=22,
                    align="left",
                ),
                columnwidth=_REBALANCE_COL_WIDTHS,
            )
        ]
    )
    fig.update_layout(
        margin=dict(l=4, r=4, t=6, b=6),
        height=min(90 + 24 * n_rows, 680),
    )
    return fig


def _entry_trigger(prev_close_reason: Optional[str], idx: int) -> Tuple[str, str]:
    """Why did we open position ``idx``?

    Returns ``(short_label, long_label)``. The short label is meant for the
    trade table, the long label is used in tooltips / explainers.

    Trigger logic mirrors ``backtester/simulation.py``:

      - Position #1 always opens at the very first usable candle.
      - If the previous position closed because the LP range was breached
        (``lower`` / ``upper``), we rebalance into a fresh range immediately
        after the cooldown — this is a *price-driven* re-entry.
      - If the previous position closed because the hedge market expired
        (``expiry`` / ``period_end``), we open a new position because the
        old insurance legs no longer exist; this is a *time-driven*
        rollover, not a market signal.
    """
    if idx == 1 or not prev_close_reason:
        return ("first open", "First open of the backtest")
    pr = (prev_close_reason or "").strip().lower()
    if pr == "lower":
        return ("rebalance ↓", "Previous LP range was breached on the LOWER bound — repositioned")
    if pr == "upper":
        return ("rebalance ↑", "Previous LP range was breached on the UPPER bound — repositioned")
    if pr == "expiry":
        return ("hedge expired", "Insurance market expired (no breach) — rolled to a fresh market")
    if pr == "period_end":
        return ("period end", "End of backtest window (forced close)")
    return (pr or "—", f"Re-opened after previous close ({pr or 'unknown'})")


def _position_entry_metrics(p: Dict[str, Any]) -> Dict[str, Any]:
    """Geometry & cost ratios that explain *why* this range was attractive."""
    rng = p.get("range") or [None, None]
    mn = _as_float(rng[0], default=None)
    mx = _as_float(rng[1], default=None)
    price = _as_float(p.get("entry_price"), default=None)
    width_pct = lower_buf = upper_buf = None
    if mn is not None and mx is not None and price and price > 0:
        width_pct = (mx - mn) / price * 100.0
        lower_buf = (price - mn) / price * 100.0
        upper_buf = (mx - price) / price * 100.0
    deposit_usd = _as_float((p.get("deposit") or {}).get("value_usd"), default=None)
    ins_cost = _as_float(p.get("insurance_cost_usdc"), default=0.0)
    ins_pct = (ins_cost / deposit_usd * 100.0) if (deposit_usd and deposit_usd > 0) else None
    return {
        "width_pct": width_pct,
        "lower_buf_pct": lower_buf,
        "upper_buf_pct": upper_buf,
        "ins_pct_of_deposit": ins_pct,
    }


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

    def rng_col(ix: int) -> List[Optional[float]]:
        out: List[Optional[float]] = []
        for s in snaps:
            r = s.get("range")
            if not isinstance(r, (list, tuple)) or len(r) != 2:
                out.append(None)
                continue
            out.append(_as_float(r[ix], default=None))  # type: ignore[arg-type]
        return out

    # sparse bids: use None to create gaps
    lower_bid = [s.get("lower_bid", None) for s in snaps]
    upper_bid = [s.get("upper_bid", None) for s in snaps]

    return {
        "x": xs,
        "price": col("price"),
        "range_lower": rng_col(0),
        "range_upper": rng_col(1),
        "hodl_usd": col("hodl_usd"),
        "strategy_usd": col("strategy_usd"),
        "lp_value_usd": col("lp_value_usd"),
        "fees_accrued_usd": col("fees_accrued_usd"),
        "poly_equity_usd": col("poly_equity_usd"),
        "lower_bid": lower_bid,
        "upper_bid": upper_bid,
    }


def _tick_daily_layout(fig: go.Figure) -> None:
    # Weekly ticks: daily labels quickly become unreadable for 60-90d windows.
    # Plotly accepts milliseconds for date axes: 7 days = 604800000 ms.
    fig.update_xaxes(tickformat="%b %d", dtick=7 * 24 * 60 * 60 * 1000, showgrid=True, tickangle=-45)


def make_price_and_range_figure(snaps: Dict[str, List[Any]], positions: Optional[List[Dict[str, Any]]] = None) -> go.Figure:
    """Beefy-style price chart with the strategy's active range band."""
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=snaps["x"],
            y=snaps["price"],
            name="Pool price (USD/ETH)",
            mode="lines",
            line=dict(color="black", width=2),
        )
    )

    if positions:
        entry_x: List[Any] = []
        entry_y: List[float] = []
        entry_text: List[str] = []
        close_x: List[Any] = []
        close_y: List[float] = []
        close_text: List[str] = []
        close_color: List[str] = []
        prev_reason: Optional[str] = None
        for idx, p in enumerate(positions, start=1):
            try:
                mn, mx = (p.get("range") or [None, None])[:2]
                cr = str(p.get("close_reason") or "")
                trig_short, trig_long = _entry_trigger(prev_reason, idx)
                prev_reason = cr
                m = _position_entry_metrics(p)
                entry_x.append(_dt_from_iso(str(p["open"])))
                entry_y.append(float(p.get("entry_price")))
                entry_text.append(
                    f"<b>Open #{idx}</b> — {trig_short}<br>"
                    f"{trig_long}<br>"
                    f"Range: [{mn:.0f}, {mx:.0f}]  (width {m['width_pct']:.1f}% of price)<br>"
                    f"Buffer: ↓{m['lower_buf_pct']:.1f}%   ↑{m['upper_buf_pct']:.1f}%<br>"
                    f"Entry price: ${float(p.get('entry_price')):,.2f}<br>"
                    f"Insurance buy: ${_as_float(p.get('insurance_cost_usdc')):,.0f}"
                    f"  ({(m['ins_pct_of_deposit'] or 0):.2f}% of deposit)"
                    f"<extra></extra>"
                )

                close_x.append(_dt_from_iso(str(p["close"])))
                close_y.append(float(p.get("close_price")))
                close_text.append(
                    f"Close ({cr})<br>Range: [{mn:.0f}, {mx:.0f}]<br>Price: ${float(p.get('close_price')):,.2f}<br><extra></extra>"
                )
                close_color.append(
                    {"expiry": "rgba(245,158,11,0.95)", "lower": "rgba(59,130,246,0.95)", "upper": "rgba(239,68,68,0.95)"}.get(
                        cr, "rgba(107,114,128,0.95)"
                    )
                )
            except Exception:
                continue
        if entry_x and entry_y:
            fig.add_trace(
                go.Scatter(
                    x=entry_x,
                    y=entry_y,
                    name="Rebalance / Enter",
                    mode="markers",
                    marker=dict(size=8, color="rgba(16,185,129,0.95)", line=dict(width=1, color="white")),
                    text=entry_text,
                    hovertemplate="%{text}",
                )
            )
        if close_x and close_y:
            fig.add_trace(
                go.Scatter(
                    x=close_x,
                    y=close_y,
                    name="Rebalance / Close",
                    mode="markers",
                    marker=dict(size=8, color=close_color, symbol="x", line=dict(width=1, color="white")),
                    text=close_text,
                    hovertemplate="%{text}",
                )
            )

    # Range overlay: build separate filled polygons per contiguous "in-position"
    # segment so Plotly doesn't fill diagonally across gaps.
    xs = snaps["x"]
    lo = snaps["range_lower"]
    hi = snaps["range_upper"]

    def _iter_segments() -> List[Tuple[List[Any], List[float], List[float]]]:
        seg_x: List[Any] = []
        seg_lo: List[float] = []
        seg_hi: List[float] = []
        out: List[Tuple[List[Any], List[float], List[float]]] = []
        for x, l, h in zip(xs, lo, hi):
            if l is None or h is None:
                if seg_x:
                    out.append((seg_x, seg_lo, seg_hi))
                    seg_x, seg_lo, seg_hi = [], [], []
                continue
            seg_x.append(x)
            seg_lo.append(float(l))
            seg_hi.append(float(h))
        if seg_x:
            out.append((seg_x, seg_lo, seg_hi))
        return out

    segments = _iter_segments()

    # Lines (step-like) for bounds, without fill.
    fig.add_trace(
        go.Scatter(
            x=xs,
            y=lo,
            name="Range lower",
            mode="lines",
            connectgaps=False,
            line=dict(color="rgba(239,68,68,0.90)", width=1.5, shape="hv"),
        )
    )
    fig.add_trace(
        go.Scatter(
            x=xs,
            y=hi,
            name="Range upper",
            mode="lines",
            connectgaps=False,
            line=dict(color="rgba(59,130,246,0.85)", width=1.5, shape="hv"),
        )
    )

    # Filled band per segment, kept out of legend.
    for seg_x, seg_lo, seg_hi in segments:
        poly_x = list(seg_x) + list(reversed(seg_x))
        poly_y = list(seg_hi) + list(reversed(seg_lo))
        fig.add_trace(
            go.Scatter(
                x=poly_x,
                y=poly_y,
                mode="lines",
                line=dict(width=0),
                fill="toself",
                fillcolor="rgba(59,130,246,0.15)",
                name="Range",
                showlegend=False,
                hoverinfo="skip",
            )
        )

    fig.update_layout(
        title="Pool price with active strategy range",
        legend_orientation="h",
        margin=dict(l=50, r=20, t=60, b=40),
        height=380,
        yaxis_title="USD per ETH",
    )
    _tick_daily_layout(fig)
    return fig


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
        dtick=7 * 24 * 60 * 60 * 1000,
        tickangle=-45,
    )
    return fig


def _mid_strategy_curve(
    xs: List[Any],
    exec_strat: List[Optional[float]],
    positions: List[Dict[str, Any]],
) -> List[Optional[float]]:
    """Reconstruct the MID-pricing strategy USD curve from the EXEC curve.

    For each position, the spread + slippage drag on the *buy* leg is paid at
    open-time and the drag on the *sell* leg is paid at close-time.  Adding the
    cumulative drag back into the EXEC curve produces what the wallet would
    have looked like if Polymarket execution were free."""
    if not xs:
        return list(exec_strat)

    events: List[Tuple[datetime, float]] = []
    for p in positions:
        sp_buy = _as_float(p.get("spread_cost_buy_usdc"))
        sl_buy = _as_float(p.get("slippage_cost_buy_usdc"))
        sp_sell = _as_float(p.get("spread_cost_sell_usdc"))
        sl_sell = _as_float(p.get("slippage_cost_sell_usdc"))
        try:
            o = _dt_from_iso(str(p["open"]))
            events.append((o, sp_buy + sl_buy))
        except Exception:
            pass
        try:
            c = _dt_from_iso(str(p["close"]))
            events.append((c, sp_sell + sl_sell))
        except Exception:
            pass
    events.sort(key=lambda e: e[0])

    out: List[Optional[float]] = []
    j = 0
    drag = 0.0
    for x, exec_v in zip(xs, exec_strat):
        while j < len(events) and events[j][0] <= x:
            drag += events[j][1]
            j += 1
        if exec_v is None:
            out.append(None)
        else:
            out.append(exec_v + drag)
    return out


def make_cumulative_pnl_figure(
    snaps: Dict[str, List[Any]],
    summary: Dict[str, Any],
    pricing: PricingMode = "exec",
) -> go.Figure:
    """Cumulative USD PnL of the strategy vs HODL, decomposed into LP fees
    and insurance net.

    All curves are zeroed at the first snapshot so the chart shows *change*
    from initial capital.  ``pricing="exec"`` (default) uses the realised
    EXEC strategy and EXEC insurance cashflows; ``pricing="mid"`` rebuilds
    the strategy curve and the cumulative insurance net from MID prices by
    adding back the recorded execution drag."""
    xs = snaps["x"]
    hodl = snaps["hodl_usd"]
    exec_strat = snaps["strategy_usd"]
    positions = summary.get("positions") or []

    if pricing == "exec":
        strat = exec_strat
    else:
        strat = _mid_strategy_curve(xs, exec_strat, positions)

    inv_usd = _as_float(summary.get("investment_usd"))
    if not inv_usd:
        for v in strat:
            if v:
                inv_usd = float(v)
                break

    strat_pnl = [(v - inv_usd) if v is not None else None for v in strat]
    hodl_pnl = [(v - inv_usd) if v is not None else None for v in hodl]

    tag = "EXEC" if pricing == "exec" else "MID"
    strat_color = "rgb(31,119,180)" if pricing == "exec" else "rgb(148,103,189)"

    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=xs, y=strat_pnl,
            name=f"Strategy PnL ({tag})",
            mode="lines",
            line=dict(color=strat_color, width=2.5),
            hovertemplate="%{x|%Y-%m-%d %H:%M}<br>"
                          f"Strategy ({tag}): " "$%{y:,.0f}<extra></extra>",
        )
    )
    fig.add_trace(
        go.Scatter(
            x=xs, y=hodl_pnl,
            name="HODL PnL (initial 50/50)",
            mode="lines",
            line=dict(color="rgb(127,127,127)", width=2, dash="dash"),
            hovertemplate="%{x|%Y-%m-%d %H:%M}<br>HODL: $%{y:,.0f}<extra></extra>",
        )
    )

    if positions:
        ins_x: List[Any] = []
        ins_y: List[float] = []
        fee_x: List[Any] = []
        fee_y: List[float] = []
        ins_cum = 0.0
        fee_cum = 0.0
        for p in positions:
            try:
                close_dt = _dt_from_iso(str(p["close"]))
            except Exception:
                continue
            if pricing == "exec":
                ins_cum += _as_float(p.get("insurance_net_usdc"))
            else:
                amt = _insurance_amounts(p, "mid")
                ins_cum += amt["net"]
            fee_cum += _as_float(p.get("fees_earned_usd"))
            ins_x.append(close_dt)
            ins_y.append(ins_cum)
            fee_x.append(close_dt)
            fee_y.append(fee_cum)

        fig.add_trace(
            go.Scatter(
                x=ins_x, y=ins_y,
                name=f"Cumulative insurance net ({tag})",
                mode="lines+markers",
                line=dict(color="rgb(214,39,40)", width=1.5),
                marker=dict(size=4),
                hovertemplate="%{x|%Y-%m-%d}<br>"
                              f"Cum insurance net ({tag}): " "$%{y:,.0f}<extra></extra>",
            )
        )
        fig.add_trace(
            go.Scatter(
                x=fee_x, y=fee_y,
                name="Cumulative LP fees earned",
                mode="lines+markers",
                line=dict(color="rgb(44,160,44)", width=1.5),
                marker=dict(size=4),
                hovertemplate="%{x|%Y-%m-%d}<br>Cum LP fees: $%{y:,.0f}<extra></extra>",
            )
        )

    fig.add_hline(y=0, line_width=1, line_dash="dot", line_color="rgba(0,0,0,0.45)")
    fig.update_layout(
        title=f"Cumulative PnL vs HODL — {tag} pricing",
        legend_orientation="h",
        margin=dict(l=50, r=20, t=60, b=40),
        height=420,
        yaxis_title="USD (vs initial capital)",
    )
    _tick_daily_layout(fig)
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
    snaps = parse_snapshots(summary)
    token = summary.get("token_symbol", "ETH")

    # Rows:
    #   1 — Headline ROI/APY (EXEC vs MID, with vs-HODL outperformance on top)
    #   2 — Balances + Cashflow
    #   3 — Strategy Ranges Over Time
    #   4 — Cumulative PnL Decomposition (EXEC)
    #   5 — Cumulative PnL Decomposition (MID)
    #   6 — Trade List (EXEC / with premium)
    #   7 — Trade List (MID / no premium)
    fig = make_subplots(
        rows=7,
        cols=1,
        shared_xaxes=False,
        vertical_spacing=0.045,
        row_heights=[0.06, 0.16, 0.13, 0.13, 0.13, 0.195, 0.195],
        subplot_titles=(
            "ROI / APY — Strategy vs HODL (EXEC and MID)",
            "Balances + Cashflow",
            "Strategy Ranges Over Time",
            "Cumulative PnL Decomposition — EXEC (with premium)",
            "Cumulative PnL Decomposition — MID (no premium)",
            "Trade List — EXEC (with premium)",
            "Trade List — MID (no premium)",
        ),
        specs=[
            [{"type": "table"}],
            [{"type": "table"}],
            [{"type": "xy"}],
            [{"type": "xy"}],
            [{"type": "xy"}],
            [{"type": "table"}],
            [{"type": "table"}],
        ],
    )

    headline = make_headline_roi_table(summary)
    for tr in headline.data:
        fig.add_trace(tr, row=1, col=1)

    tbl = make_balances_table(summary)
    for tr in tbl.data:
        fig.add_trace(tr, row=2, col=1)

    pr = make_price_and_range_figure(snaps, positions=summary.get("positions") or [])
    for tr in pr.data:
        fig.add_trace(tr, row=3, col=1)

    pnl_exec = make_cumulative_pnl_figure(snaps, summary, pricing="exec")
    for tr in pnl_exec.data:
        fig.add_trace(tr, row=4, col=1)

    pnl_mid = make_cumulative_pnl_figure(snaps, summary, pricing="mid")
    for tr in pnl_mid.data:
        fig.add_trace(tr, row=5, col=1)

    n_pos = len(summary.get("positions") or [])
    table_panel_h = min(90 + 24 * max(n_pos, 1), 680)

    rtbl = make_rebalances_table(summary, pricing="exec")
    for tr in rtbl.data:
        fig.add_trace(tr, row=6, col=1)

    rtbl_mid = make_rebalances_table(summary, pricing="mid")
    for tr in rtbl_mid.data:
        fig.add_trace(tr, row=7, col=1)

    fig.update_layout(
        title=None,
        height=2160 + 2 * table_panel_h,
        margin=dict(l=55, r=25, t=100, b=120),
        showlegend=True,
        legend=dict(
            orientation="h",
            x=0.0,
            xanchor="left",
            y=0.0,
            yanchor="top",
            entrywidthmode="fraction",
            entrywidth=0.22,
        ),
    )
    fig.update_annotations(yshift=18)

    fig.update_xaxes(tickformat="%b %d", dtick=7 * 24 * 60 * 60 * 1000, tickangle=-45, row=3, col=1)
    fig.update_xaxes(tickformat="%b %d", dtick=7 * 24 * 60 * 60 * 1000, tickangle=-45, row=4, col=1)
    fig.update_xaxes(tickformat="%b %d", dtick=7 * 24 * 60 * 60 * 1000, tickangle=-45, row=5, col=1)
    fig.update_yaxes(title_text=f"USD per {token}", row=3, col=1)
    fig.update_yaxes(title_text="USD (vs initial capital)", row=4, col=1)
    fig.update_yaxes(title_text="USD (vs initial capital)", row=5, col=1)

    return fig


def write_report_html(
    *,
    summary: Dict[str, Any],
    fig: go.Figure,
    out_path: Path,
    title: str,
) -> None:
    """Write a self-contained HTML report with a stable header section.

    Using a normal HTML header block (instead of Plotly annotations) guarantees
    it will never overlap the Plotly table/plots, regardless of viewport size.
    """
    plot_html = fig.to_html(include_plotlyjs="cdn", full_html=False)

    # Counts for the entry-trigger summary
    positions = summary.get("positions") or []
    n_pos = len(positions)
    triggers = {"first open": 0, "rebalance ↓": 0, "rebalance ↑": 0,
                "hedge expired": 0, "period end": 0, "other": 0}
    prev_reason = None
    for i, p in enumerate(positions, start=1):
        short, _ = _entry_trigger(prev_reason, i)
        triggers[short if short in triggers else "other"] += 1
        prev_reason = str(p.get("close_reason") or "")
    n_first = triggers["first open"]
    n_rebal_dn = triggers["rebalance ↓"]
    n_rebal_up = triggers["rebalance ↑"]
    n_expired = triggers["hedge expired"]
    n_other = triggers["period end"] + triggers["other"]
    rm = summary.get("run_metadata") or {}
    cooldown = rm.get("cooldown_hours", "?")

    explainer = f"""
      <div class="explainer">
        <h3 style="margin:0 0 8px 0">How positions are opened</h3>
        <p style="margin:0 0 8px 0">
          The simulator runs hourly. With <b>no</b> open position it scans the
          range universe at the current candle and selects the range with the
          highest score:
          <code>narrowness_bonus − insurance_cost_rate</code>, after filtering
          out (a) ranges that don't contain the current price with at least a
          5% buffer on each side, and (b) ranges where either Polymarket leg's
          YES probability is above 20% (a hard cap that biases entries toward
          out-of-the-money wings). Ties broken by lowest insurance spend.
        </p>
        <p style="margin:0 0 8px 0">A new position is opened when one of three things happens:</p>
        <ul style="margin:0 0 8px 18px; padding:0">
          <li><b>First open</b> — at the very first usable candle of the backtest window.</li>
          <li><b>Rebalance ↓ / ↑</b> — the previous LP range was breached on the lower / upper bound; the position is force-closed (next-candle policy) and immediately re-entered after a {cooldown}h cooldown.</li>
          <li><b>Hedge expired</b> — the Polymarket touch market underwriting the previous position rolled off (its <code>end_date</code> passed); the LP is closed and re-opened against a freshly-listed monthly market with the same (asset, strike) family.</li>
        </ul>
        <p style="margin:0">
          Across the {n_pos} positions in this run:
          <b>{n_first}</b> first open · <b>{n_rebal_dn}</b> price-driven rebalance ↓ ·
          <b>{n_rebal_up}</b> price-driven rebalance ↑ · <b>{n_expired}</b> hedge-expiry rollovers ·
          <b>{n_other}</b> other.
          See the <i>Why entered</i> and <i>Days</i> columns in the trade lists
          (EXEC and MID share the same layout; only insurance prices differ).
          Hover any green entry marker on the price chart for the per-trade rationale.
        </p>
      </div>"""

    html = f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>{title}</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
      body {{
        font-family: Inter, ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
        margin: 0;
        padding: 28px 18px 52px 18px;
        background: #F6F7FB;
        color: #111827;
      }}
      .container {{
        max-width: min(1440px, 96vw);
        margin: 0 auto;
      }}
      .page-title {{
        font-size: 20px;
        font-weight: 700;
        letter-spacing: -0.01em;
        margin: 0 0 16px 0;
      }}
      .card {{
        background: #FFFFFF;
        border: 1px solid rgba(17,24,39,0.10);
        border-radius: 14px;
        padding: 16px;
        box-shadow: 0 10px 24px rgba(17,24,39,0.08);
      }}
      .card .js-plotly-plot {{
        border-radius: 12px;
      }}
      .explainer {{
        background: #FFFFFF;
        border: 1px solid rgba(17,24,39,0.10);
        border-radius: 14px;
        padding: 16px 18px;
        box-shadow: 0 6px 18px rgba(17,24,39,0.06);
        margin: 0 0 16px 0;
        line-height: 1.45;
        font-size: 13.5px;
      }}
      .explainer h3 {{ font-size: 14.5px; }}
      .explainer code {{
        background: #F3F4F6;
        padding: 1px 5px;
        border-radius: 4px;
        font-size: 12.5px;
      }}
    </style>
  </head>
  <body>
    <div class="container">
      <div class="page-title">{title}</div>
      {explainer}
      <div class="card">
        {plot_html}
      </div>
    </div>
  </body>
</html>
"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")


def main() -> int:
    # Config-driven entrypoint (no CLI flags).
    # Ensure repo root is on sys.path when executed as `python3 scripts/viz_report.py`.
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    from config_loader import load_config, get_section

    cfg = load_config()
    rpt = get_section(cfg, "report")

    in_path = Path(str(rpt.get("input_json", "active_backtest_results.json")))
    out_path = Path(str(rpt.get("output_html", "report.html")))
    title = str(rpt.get("title", "Backtest Report (LP + Polymarket)"))

    summary = load_summary(in_path)
    fig = build_report(summary, title)

    write_report_html(summary=summary, fig=fig, out_path=out_path, title=title)
    print(f"Wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


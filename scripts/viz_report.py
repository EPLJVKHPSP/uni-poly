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


def make_rebalances_table(summary: Dict[str, Any]) -> go.Figure:
    """Detailed per-position (rebalance) ledger."""
    def _date_only(s: Any) -> str:
        try:
            return _dt_from_iso(str(s)).date().isoformat()
        except Exception:
            return ""

    rows = []
    for i, p in enumerate(summary.get("positions") or [], start=1):
        rng = p.get("range") or [None, None]
        mn = _as_float(rng[0], default=None)
        mx = _as_float(rng[1], default=None)
        reason = str(p.get("close_reason") or "")

        wb = p.get("wallet_before") or {}
        wa = p.get("wallet_after") or {}
        d_wallet = _as_float(wa.get("value_usd")) - _as_float(wb.get("value_usd"))

        rows.append(
            (
                i,
                _date_only(p.get("open", "")),
                _date_only(p.get("close", "")),
                reason,
                f"[{_fmt_num(mn, 0)}–{_fmt_num(mx, 0)}]" if mn is not None and mx is not None else "",
                _fmt_money(p.get("entry_price")),
                _fmt_money(p.get("close_price")),
                _fmt_money(p.get("insurance_cost_usdc")),
                _fmt_money(p.get("insurance_payout_usdc")),
                _fmt_money(p.get("insurance_sellback_usdc")),
                _fmt_money(p.get("fees_earned_usd")),
                _fmt_money(p.get("il_usdc")),
                _fmt_money(d_wallet),
            )
        )

    headers = [
        "<b>#</b>",
        "<b>Open</b>",
        "<b>Close</b>",
        "<b>Reason</b>",
        "<b>Range</b>",
        "<b>Entry</b>",
        "<b>Close</b>",
        "<b>Ins buy (EXEC)</b>",
        "<b>Ins payout</b>",
        "<b>Ins sell (EXEC)</b>",
        "<b>Fees</b>",
        "<b>IL</b>",
        "<b>Δ Wallet (USD)</b>",
    ]

    cols = list(zip(*rows)) if rows else [tuple() for _ in headers]

    fig = go.Figure(
        data=[
            go.Table(
                header=dict(
                    values=headers,
                    fill_color="rgb(245,245,245)",
                    line_color="rgb(220,220,220)",
                    font=dict(size=12),
                    height=28,
                    align="center",
                ),
                cells=dict(
                    values=[list(c) for c in cols],
                    fill_color="white",
                    line_color="rgb(235,235,235)",
                    font=dict(size=10),
                    height=20,
                    align="center",
                ),
                columnwidth=[0.04, 0.08, 0.08, 0.08, 0.10, 0.06, 0.06, 0.09, 0.09, 0.09, 0.06, 0.06, 0.12],
            )
        ]
    )
    fig.update_layout(margin=dict(l=10, r=10, t=10, b=10), height=460)
    return fig


def make_rebalances_table_mid(summary: Dict[str, Any]) -> go.Figure:
    """Per-position ledger reconstructed at MID prices (no premium/discount)."""
    def _date_only(s: Any) -> str:
        try:
            return _dt_from_iso(str(s)).date().isoformat()
        except Exception:
            return ""

    rows = []
    for i, p in enumerate(summary.get("positions") or [], start=1):
        rng = p.get("range") or [None, None]
        mn = _as_float(rng[0], default=None)
        mx = _as_float(rng[1], default=None)
        reason = str(p.get("close_reason") or "")

        # EXEC values stored in JSON
        buy_exec = _as_float(p.get("insurance_cost_usdc"))
        sell_exec = _as_float(p.get("insurance_sellback_usdc"))
        payout = _as_float(p.get("insurance_payout_usdc"))

        # Execution drag recorded separately; use it to reconstruct MID.
        sp_buy = _as_float(p.get("spread_cost_buy_usdc"))
        sl_buy = _as_float(p.get("slippage_cost_buy_usdc"))
        sp_sell = _as_float(p.get("spread_cost_sell_usdc"))
        sl_sell = _as_float(p.get("slippage_cost_sell_usdc"))

        buy_mid = max(0.0, buy_exec - sp_buy - sl_buy)
        sell_mid = sell_exec + sp_sell + sl_sell
        net_mid = payout + sell_mid - buy_mid

        wb = p.get("wallet_before") or {}
        wa = p.get("wallet_after") or {}
        d_wallet = _as_float(wa.get("value_usd")) - _as_float(wb.get("value_usd"))

        rows.append(
            (
                i,
                _date_only(p.get("open", "")),
                _date_only(p.get("close", "")),
                reason,
                f"[{_fmt_num(mn, 0)}–{_fmt_num(mx, 0)}]" if mn is not None and mx is not None else "",
                _fmt_money(p.get("entry_price")),
                _fmt_money(p.get("close_price")),
                _fmt_money(buy_mid),
                _fmt_money(payout),
                _fmt_money(sell_mid),
                _fmt_money(net_mid),
                _fmt_money(p.get("fees_earned_usd")),
                _fmt_money(p.get("il_usdc")),
                _fmt_money(d_wallet),
            )
        )

    headers = [
        "<b>#</b>",
        "<b>Open</b>",
        "<b>Close</b>",
        "<b>Reason</b>",
        "<b>Range</b>",
        "<b>Entry</b>",
        "<b>Close</b>",
        "<b>Ins buy (MID)</b>",
        "<b>Ins payout</b>",
        "<b>Ins sell (MID)</b>",
        "<b>Ins net (MID)</b>",
        "<b>Fees</b>",
        "<b>IL</b>",
        "<b>Δ Wallet (USD)</b>",
    ]

    cols = list(zip(*rows)) if rows else [tuple() for _ in headers]

    fig = go.Figure(
        data=[
            go.Table(
                header=dict(
                    values=headers,
                    fill_color="rgb(245,245,245)",
                    line_color="rgb(220,220,220)",
                    font=dict(size=12),
                    height=28,
                    align="center",
                ),
                cells=dict(
                    values=[list(c) for c in cols],
                    fill_color="white",
                    line_color="rgb(235,235,235)",
                    font=dict(size=10),
                    height=20,
                    align="center",
                ),
                columnwidth=[0.04, 0.08, 0.08, 0.08, 0.08, 0.055, 0.055, 0.095, 0.075, 0.095, 0.095, 0.055, 0.055, 0.12],
            )
        ]
    )
    fig.update_layout(margin=dict(l=10, r=10, t=10, b=10), height=460)
    return fig


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
        for p in positions:
            try:
                mn, mx = (p.get("range") or [None, None])[:2]
                cr = str(p.get("close_reason") or "")
                entry_x.append(_dt_from_iso(str(p["open"])))
                entry_y.append(float(p.get("entry_price")))
                entry_text.append(
                    f"Enter<br>Range: [{mn:.0f}, {mx:.0f}]<br>Price: ${float(p.get('entry_price')):,.2f}<br><extra></extra>"
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
    # 4 rows: balances/cashflows table + price+range chart + rebalance history (EXEC) + rebalance history (MID).
    fig = make_subplots(
        rows=4,
        cols=1,
        shared_xaxes=False,
        vertical_spacing=0.09,
        subplot_titles=(
            "Balances + Cashflow",
            "Strategy Ranges Over Time",
            "Rebalance History (EXEC / with premium)",
            "Rebalance History (MID / no premium)",
        ),
        specs=[
            [{"type": "table"}],
            [{"type": "xy"}],
            [{"type": "table"}],
            [{"type": "table"}],
        ],
    )

    tbl = make_balances_table(summary)
    for tr in tbl.data:
        fig.add_trace(tr, row=1, col=1)

    pr = make_price_and_range_figure(snaps, positions=summary.get("positions") or [])
    for tr in pr.data:
        fig.add_trace(tr, row=2, col=1)

    rtbl = make_rebalances_table(summary)
    for tr in rtbl.data:
        fig.add_trace(tr, row=3, col=1)

    rtbl_mid = make_rebalances_table_mid(summary)
    for tr in rtbl_mid.data:
        fig.add_trace(tr, row=4, col=1)

    # Global layout tuning
    fig.update_layout(
        title=None,
        height=2050,
        margin=dict(l=55, r=25, t=100, b=120),
        legend=dict(
            orientation="h",
            x=0.0,
            xanchor="left",
            # Will be positioned just below the chart row once domains exist.
            y=0.0,
            yanchor="top",
            entrywidthmode="fraction",
            entrywidth=0.20,
        ),
    )
    # Subplot title spacing (keep tight; layout already has card padding).
    fig.update_annotations(yshift=18)

    # Daily ticks on time-series rows
    fig.update_xaxes(tickformat="%b %d", dtick=7 * 24 * 60 * 60 * 1000, tickangle=-45, row=2, col=1)

    # Place the legend directly under the chart (row=2), not at the end of the page.
    # For this report there's a single cartesian y-axis (the chart); tables don't create y-axes.
    try:
        y0 = float(fig.layout.yaxis.domain[0])
        fig.update_layout(legend=dict(y=y0 - 0.03))
    except Exception:
        pass

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
        max-width: 1180px;
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
    </style>
  </head>
  <body>
    <div class="container">
      <div class="page-title">{title}</div>
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


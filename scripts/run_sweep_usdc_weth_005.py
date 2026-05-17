"""Focused refinement sweep around the USDC/WETH 0.05% Mainnet anchor winner.

This is the harness that produced the headline WETH config
(`hedge_lp_fee_credit_pct = 0.70`, `fixed_range_pct = 30`, `range_yes_cap =
0.10`) recorded in ``best_weth_anchor.config.json``.

Grid:
    range_yes_cap            ∈ {0.06, 0.08, 0.10}
    fixed_range_pct          ∈ {25, 30, 35}
    hedge_lp_fee_credit_pct  ∈ {0.30, 0.50, 0.70}
  → 3 × 3 × 3 = 27 cells, sequential, ~150 s each ≈ 70 min wall time.

The grid was trimmed from the original 4 × 4 × 3 = 48 cells after observing
that ``yc=0.04`` cells grind through Postgres for >10 minutes without
opening a position (tight YES-cap × narrow width finds almost no qualifying
Polymarket touch markets, but still hits the DB on every candle). A
per-cell timeout (``SWEEP_CELL_TIMEOUT_SEC``, default 480 s) guards against
the same pathology recurring.

Held constant (matches the current winner):
    pool                     = 0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640
    initial_eth              = 50.0
    cooldown_hours           = 1
    min_hedge_tte_hours      = 336
    selection_objective      = "narrowness_minus_premium"
    restore_to_anchor        = True
    hedge_sizing_mode        = "full_restore"
    final_restore_at_end     = True
    require_full_insurance   = True
    max_idle_hours           = 24
    relax_filters_when_empty = False

Outputs (created on demand, not committed):
    runs/sweep_uw005/<id>.json                 per-cell simulator output
    runs/sweep_uw005/configs/<id>.config.json  each cell's config
    runs/sweep_uw005/logs/<id>.log             per-cell stdout
    runs/sweep_uw005/leaderboard.md            ranked table, written incrementally
    runs/sweep_uw005/leaderboard.json          machine-readable

Re-running:
    set -a && source .env && set +a
    source venv/bin/activate
    python3 scripts/run_sweep_usdc_weth_005.py            # full sweep
    python3 scripts/run_sweep_usdc_weth_005.py --only "yc10_w30_cr70"  # one cell
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SWEEP_DIR = REPO_ROOT / "runs" / "sweep_uw005"
CONFIGS_DIR = SWEEP_DIR / "configs"
LOGS_DIR = SWEEP_DIR / "logs"
LEADERBOARD_MD = SWEEP_DIR / "leaderboard.md"
LEADERBOARD_JSON = SWEEP_DIR / "leaderboard.json"

POOL = "0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640"
SUBGRAPH = "5zvR82QoaXYFyDEKLZ9t6v9adgnptxYpKpSbxtgVENFV"

# Grid trimmed after observing yc=0.04 cells grinding through Postgres for >10
# minutes without opening a position (tight YES-cap × narrow width finds almost
# no qualifying Polymarket touch markets, but still hits the DB on every candle).
# yc=0.04 was outside the original Phase 26 winner anyway.
YC_VALUES = [0.06, 0.08, 0.10]
WIDTH_VALUES = [25.0, 30.0, 35.0]
CREDIT_VALUES = [0.30, 0.50, 0.70]
# 3 × 3 × 3 = 27 cells; expected wall time ≈ 70 min at ~150 s/cell.


def _cell_id(yc: float, w: float, cr: float) -> str:
    yc_s = f"{int(round(yc * 100)):02d}"
    w_s = f"{int(round(w)):02d}"
    cr_s = f"{int(round(cr * 100)):02d}"
    return f"yc{yc_s}_w{w_s}_cr{cr_s}"


def _build_config(cell_id: str, yc: float, w: float, cr: float) -> dict:
    return {
        "_id": cell_id,
        "_label": f"USDC/WETH 0.05% anchor — yc={yc} w={int(w)}% cr={cr}",
        "_chain": "ethereum",
        "_subgraph_id": SUBGRAPH,
        "backtest": {
            "pool": POOL,
            "days": 180,
            "lookback_days": 0,
            "initial_eth": 50.0,
            "initial_usdc": None,
            "cooldown_hours": 1,
            "price_token": None,
            "fixed_range": None,
            "sweep": False,
            "spread": 0.04,
            "slippage_per_1k_contracts": 0.02,
            "slippage_max_per_contract": 0.10,
            "close_policy": "pessimistic",
            "touch_settlement_haircut": 0.03,
            "sell_touched_at_market": True,
            "risk_free_rate_apy": 0.045,
            "perp_funding_rate_apy": 0.10,
            "gas_strict": False,
            "priority_fee_gwei": 2.0,
            "restrict_to_touch_markets": True,
            "polymarket_fee_category": "crypto",
            "polymarket_fees_enabled": True,
            "min_hedge_tte_hours": 336,
            "range_yes_cap": yc,
            "selection_objective": "narrowness_minus_premium",
            "fixed_range_pct": w,
            "range_buffer_pct": None,
            "range_max_width_pct": None,
            "bypass_insurance": False,
            "relax_filters_when_empty": False,
            "restore_to_anchor": True,
            "hedge_sizing_mode": "full_restore",
            "hedge_lp_fee_credit_pct": cr,
            "final_restore_at_end": True,
            "telemetry": {"enabled": False, "path": None},
            "max_idle_hours": 24,
            "min_market_volume_usd": 1000.0,
            "output_json": f"runs/sweep_uw005/{cell_id}.json",
            "require_full_insurance": True,
        },
        "report": {
            "input_json": f"runs/sweep_uw005/{cell_id}.json",
            "output_html": f"runs/sweep_uw005/{cell_id}.report.html",
            "title": f"USDC/WETH 0.05% sweep cell {cell_id}",
        },
    }


def _read_summary(json_path: Path) -> dict:
    if not json_path.exists():
        return {"error": f"output missing: {json_path}"}
    try:
        data = json.loads(json_path.read_text())
    except Exception as e:
        return {"error": f"parse error: {e}"}

    baselines = data.get("baselines") or {}
    real_cash = baselines.get("real_cash_terms") or {}
    hodl = baselines.get("hodl") or {}
    final_wallet = data.get("final_wallet") or {}

    posns = data.get("positions") or []
    insured = sum(1 for p in posns if float(p.get("insurance_cost_usdc") or 0) > 0)

    times = []
    for p in posns:
        try:
            o = datetime.fromisoformat(str(p.get("open")).replace("Z", "+00:00"))
            c = datetime.fromisoformat(str(p.get("close")).replace("Z", "+00:00"))
            times.append((o, c))
        except Exception:
            continue
    times.sort(key=lambda x: x[0])
    max_idle_h = 0.0
    for i in range(1, len(times)):
        gap_h = (times[i][0] - times[i - 1][1]).total_seconds() / 3600.0
        if gap_h > max_idle_h:
            max_idle_h = gap_h

    return {
        "cycles": len(posns),
        "insured": insured,
        "premium": real_cash.get("gross_premium_paid_usd"),
        "real_cash_final": real_cash.get("real_cash_final_value_usd"),
        "real_cash_vs_hodl_usd": real_cash.get("real_cash_vs_hodl_usd"),
        "real_cash_vs_hodl_pct": real_cash.get("real_cash_vs_hodl_pct"),
        "real_cash_roi_pct": real_cash.get("real_cash_roi_pct"),
        "hodl_final": hodl.get("final_value_usd"),
        "hodl_roi_pct": hodl.get("roi_pct"),
        "max_idle_hours": round(max_idle_h, 1),
        "investment_usd": data.get("investment_usd"),
    }


def _write_leaderboard(results: list[dict]) -> None:
    SWEEP_DIR.mkdir(parents=True, exist_ok=True)
    LEADERBOARD_JSON.write_text(json.dumps(results, indent=2))

    finished = [r for r in results if r.get("rc") == 0 and r.get("real_cash_vs_hodl_usd") is not None]
    compliant = [r for r in finished if (r.get("max_idle_hours") or 0) <= 24]
    non_compliant = [r for r in finished if (r.get("max_idle_hours") or 0) > 24]
    compliant_sorted = sorted(compliant, key=lambda r: -float(r["real_cash_vs_hodl_usd"]))
    non_compliant_sorted = sorted(non_compliant, key=lambda r: -float(r["real_cash_vs_hodl_usd"]))

    lines = [
        "# USDC/WETH 0.05% Mainnet — anchor sweep (yc × width × credit)",
        "",
        f"Pool: `{POOL}` · 180-day window · always insured · max_idle 24 h enforced · selection=`narrowness_minus_premium`",
        f"Run: {time.strftime('%Y-%m-%d %H:%M:%S %Z')} · "
        f"{len(finished)}/{len(results)} cells finished "
        f"({len(compliant)} 24 h-compliant, {len(non_compliant)} rule-violating)",
        "",
        "## Strict-24h-compliant leaderboard (best vs HODL first)",
        "",
        "*Configs whose worst gap between consecutive insured positions is ≤ 24 hours. "
        "These are the only legitimate answers to your stated rule.*",
        "",
        "| Rank | Cell | yc | w% | cr | Cycles | Insured | Premium | vs HODL | ROI − HODL ROI | Max idle (h) |",
        "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for i, r in enumerate(compliant_sorted, 1):
        if r.get("rc") != 0:
            lines.append(
                f"| {i} | `{r['id']}` | — | — | — | — | — | — | **FAILED** | — | — |"
            )
            continue
        prem = r.get("premium") or 0
        vs_hodl = r.get("real_cash_vs_hodl_usd") or 0
        roi = r.get("real_cash_roi_pct")
        roi_hodl = r.get("hodl_roi_pct")
        roi_str = (
            f"{roi - roi_hodl:+.2f} pp" if (roi is not None and roi_hodl is not None) else "—"
        )
        lines.append(
            f"| {i} | `{r['id']}` | {r['yc']} | {int(r['width'])} | {r['credit']:.2f} | "
            f"{r.get('cycles')} | {r.get('insured')}/{r.get('cycles')} | "
            f"${prem:,.0f} | **${vs_hodl:,.0f}** ({r.get('real_cash_vs_hodl_pct')}%) | {roi_str} | "
            f"{r.get('max_idle_hours')} |"
        )

    if non_compliant_sorted:
        lines.append("")
        lines.append("## Rule-violating cells (max idle > 24 h) — DO NOT QUOTE AS WINNERS")
        lines.append("")
        lines.append(
            "*These cells skipped many cycles because no qualifying Polymarket market was "
            "available (`relax_filters_when_empty=False`). The portfolio sat unhedged for "
            "the listed idle gaps, which lowered premium spend artificially. Listed for "
            "diagnostic purposes only.*"
        )
        lines.append("")
        lines.append(
            "| Cell | yc | w% | cr | Cycles | Premium | vs HODL | Max idle (h) |"
        )
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")
        for r in non_compliant_sorted:
            prem = r.get("premium") or 0
            vs_hodl = r.get("real_cash_vs_hodl_usd") or 0
            lines.append(
                f"| `{r['id']}` | {r['yc']} | {int(r['width'])} | {r['credit']:.2f} | "
                f"{r.get('cycles')} | ${prem:,.0f} | "
                f"${vs_hodl:,.0f} ({r.get('real_cash_vs_hodl_pct')}%) | "
                f"**{r.get('max_idle_hours')}** |"
            )

    failed = [r for r in results if r.get("rc") not in (0, None)]
    if failed:
        lines.append("")
        lines.append("## Failed cells (timeout / error)")
        lines.append("")
        for r in failed:
            lines.append(f"- `{r['id']}` rc={r['rc']} log=`{r.get('log')}`")

    LEADERBOARD_MD.write_text("\n".join(lines) + "\n")


def _run_one(cfg_path: Path, cell_id: str, yc: float, w: float, cr: float) -> dict:
    out_json = REPO_ROOT / json.loads(cfg_path.read_text())["backtest"]["output_json"]
    log_path = LOGS_DIR / f"{cell_id}.log"

    env = os.environ.copy()
    env["BACKTEST_CONFIG_PATH"] = str(cfg_path)
    env["BACKTEST_SUBGRAPH_ID"] = SUBGRAPH

    print(f"  → {cell_id}: yc={yc} w={int(w)}% cr={cr} ... ", end="", flush=True)
    t0 = time.time()
    if out_json.exists():
        out_json.unlink()
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    timeout_s = int(os.environ.get("SWEEP_CELL_TIMEOUT_SEC", "480"))
    with log_path.open("w") as logf:
        try:
            rc = subprocess.call(
                [sys.executable, "-c", "from backtester import main; main()"],
                env=env,
                cwd=str(REPO_ROOT),
                stdout=logf,
                stderr=subprocess.STDOUT,
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired:
            rc = 124
            logf.write(f"\n[orchestrator] TIMEOUT after {timeout_s}s, killed\n")
    dt = time.time() - t0

    if rc != 0:
        print(f"FAILED rc={rc} ({dt:.0f}s)", flush=True)
        return {
            "id": cell_id,
            "yc": yc, "width": w, "credit": cr,
            "rc": rc, "elapsed_sec": round(dt, 1),
            "log": str(log_path.relative_to(REPO_ROOT)),
        }

    summary = _read_summary(out_json)
    vs = summary.get("real_cash_vs_hodl_usd") or 0
    print(f"vs HODL ${vs:,.0f} ({summary.get('real_cash_vs_hodl_pct')}%) ({dt:.0f}s)", flush=True)
    return {
        "id": cell_id,
        "yc": yc, "width": w, "credit": cr,
        "rc": 0, "elapsed_sec": round(dt, 1),
        "config_path": str(cfg_path.relative_to(REPO_ROOT)),
        "output_json": str(out_json.relative_to(REPO_ROOT)),
        "log": str(log_path.relative_to(REPO_ROOT)),
        **summary,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", nargs="*", default=None,
                        help="Run only the listed cell IDs (e.g. yc06_w25_cr50)")
    args = parser.parse_args()

    if "BACKTEST_CONFIG_PATH" in os.environ:
        del os.environ["BACKTEST_CONFIG_PATH"]

    SWEEP_DIR.mkdir(parents=True, exist_ok=True)
    CONFIGS_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    cells: list[tuple[str, float, float, float]] = []
    for yc in YC_VALUES:
        for w in WIDTH_VALUES:
            for cr in CREDIT_VALUES:
                cid = _cell_id(yc, w, cr)
                if args.only and cid not in set(args.only):
                    continue
                cells.append((cid, yc, w, cr))

    print(f"Sweep: {len(cells)} cells, sequential, ~150 s each → ~{len(cells)*150/60:.0f} min")
    print(f"Pool: USDC/WETH 0.05% Mainnet ({POOL})")
    print(f"Outputs: {SWEEP_DIR.relative_to(REPO_ROOT)}/")

    results: list[dict] = []
    for i, (cid, yc, w, cr) in enumerate(cells, 1):
        cfg = _build_config(cid, yc, w, cr)
        cfg_path = CONFIGS_DIR / f"{cid}.config.json"
        cfg_path.write_text(json.dumps(cfg, indent=2))
        print(f"[{i}/{len(cells)}]", end=" ", flush=True)
        results.append(_run_one(cfg_path, cid, yc, w, cr))
        _write_leaderboard(results)

    finished = [r for r in results if r.get("rc") == 0]
    if finished:
        winner = max(finished, key=lambda r: float(r.get("real_cash_vs_hodl_usd") or -1e18))
        print(f"\nWINNER: {winner['id']} → ${winner['real_cash_vs_hodl_usd']:,.0f} vs HODL "
              f"({winner['real_cash_vs_hodl_pct']}%)")
    print(f"\nLeaderboard: {LEADERBOARD_MD.relative_to(REPO_ROOT)}")
    return 0 if all(r.get("rc") == 0 for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

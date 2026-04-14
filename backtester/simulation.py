"""Simulation loop, sweep mode, summary builder, and CLI entrypoint."""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

from .graph_client import fetch_pool_metadata, fetch_hourly_candles
from .fee_math import compute_hourly_fee_split, _tokens_for_strategy_human
from .gas import fetch_daily_gas_prices
from .range_selection import (
    _map_wrapped_symbol,
    _filter_ranges_for_price,
    pick_best_range,
    pick_best_range_by_sweep,
    _get_insurance_for_range,
)
from .positions import open_position, close_position
from .telemetry import TelemetrySink, new_run_id

logger = logging.getLogger(__name__)


def _get_db_func(name):
    """Resolve db_utils function via the shim module so that
    @patch("active_backtester.<name>") intercepts calls correctly."""
    shim = sys.modules.get("active_backtester")
    if shim and hasattr(shim, name):
        return getattr(shim, name)
    import db_utils
    return getattr(db_utils, name)


def simulate(
    candles: List[Dict],
    pool_data: Dict,
    token_symbol: str,
    investment: float,
    conn,
    price_token: int = 0,
    cooldown_hours: int = 1,
    fixed_range: Optional[Tuple[float, float]] = None,
    quiet: bool = False,
    warmup_candles: Optional[List[Dict]] = None,
    all_candles: Optional[List[Dict]] = None,
    gas_prices: Optional[Dict[str, int]] = None,
    spread: float = 0.0,
    telemetry: Optional[TelemetrySink] = None,
) -> Tuple[List[Dict], Dict]:
    """
    Walk through hourly candles with realistic wallet tracking.

    When *warmup_candles* and *all_candles* are provided, range selection uses
    a lookback sweep (insurance efficiency) instead of the heuristic scorer.
    Returns (positions, final_wallet, snapshots).
    """
    get_range_combinations = _get_db_func("get_range_combinations")
    get_clob_token_id = _get_db_func("get_clob_token_id")
    get_historical_bet_price = _get_db_func("get_historical_bet_price")
    use_lookback = warmup_candles is not None and all_candles is not None and fixed_range is None
    all_combos = get_range_combinations(token_symbol, conn) if fixed_range is None else None

    warmup_len = len(warmup_candles) if warmup_candles else 0

    dec0 = int(pool_data["token0"]["decimals"])
    dec1 = int(pool_data["token1"]["decimals"])

    first_price = float(candles[0]["close"]) if price_token == 0 else 1.0 / float(candles[0]["close"])
    wallet = {"usdc": investment / 2.0, "eth": (investment / 2.0) / first_price}
    initial_usdc = wallet["usdc"]
    initial_eth = wallet["eth"]

    positions: List[Dict] = []
    snapshots: List[Dict] = []
    current_pos: Optional[Dict] = None
    lower_clob_id: Optional[str] = None
    upper_clob_id: Optional[str] = None
    i = 0
    log = logger.info if not quiet else logger.debug

    if telemetry is not None:
        telemetry.emit(
            "run_start",
            int(candles[0]["periodStartUnix"]),
            payload={
                "token_symbol": token_symbol,
                "investment_usd": investment,
                "price_token": price_token,
                "cooldown_hours": cooldown_hours,
                "fixed_range": list(fixed_range) if fixed_range else None,
                "selection_mode": "lookback" if use_lookback else ("fixed" if fixed_range else "heuristic"),
                "spread": spread,
                "baseline_initial_usdc": initial_usdc,
                "baseline_initial_eth": initial_eth,
            },
        )

    def _snap(ts, price, pos, wlt):
        """Build an hourly snapshot dict."""
        hodl_usd = initial_usdc + initial_eth * price

        if pos is not None:
            clamped = max(pos["min_range"], min(price, pos["max_range"]))
            lp_usdc, lp_eth = _tokens_for_strategy_human(
                pos["min_range"], pos["max_range"], pos["deposit_value"], clamped,
            )
            lp_value = lp_usdc + lp_eth * price
            fees_value = pos["accumulated_fees_usdc"] + pos["accumulated_fees_eth"] * price

            poly_equity = 0.0
            lower_bid = None
            upper_bid = None
            if lower_clob_id and conn:
                lp_ = get_historical_bet_price(lower_clob_id, ts, conn)
                if lp_ is not None:
                    lower_bid = max(lp_ - spread / 2, 0.0)
                    poly_equity += pos["lower_contracts"] * lower_bid
            if upper_clob_id and conn:
                up_ = get_historical_bet_price(upper_clob_id, ts, conn)
                if up_ is not None:
                    upper_bid = max(up_ - spread / 2, 0.0)
                    poly_equity += pos["upper_contracts"] * upper_bid

            strategy_usd = lp_value + fees_value + poly_equity
        else:
            lp_value = 0.0
            fees_value = 0.0
            poly_equity = 0.0
            lower_bid = None
            upper_bid = None
            strategy_usd = wlt["usdc"] + wlt["eth"] * price

        snap = {
            "ts": ts,
            "price": round(price, 2),
            "hodl_usd": round(hodl_usd, 2),
            "strategy_usd": round(strategy_usd, 2),
            "lp_value_usd": round(lp_value, 2),
            "fees_accrued_usd": round(fees_value, 2),
            "poly_equity_usd": round(poly_equity, 2),
            "wallet_usdc": round(wlt["usdc"], 2),
            "wallet_eth": round(wlt["eth"], 6),
            "position_open": pos is not None,
        }
        if lower_bid is not None:
            snap["lower_bid"] = round(lower_bid, 4)
        if upper_bid is not None:
            snap["upper_bid"] = round(upper_bid, 4)
        if pos is not None:
            snap["range"] = [pos["min_range"], pos["max_range"]]
        snapshots.append(snap)
        if telemetry is not None:
            telemetry.emit(
                "candle",
                ts,
                payload={
                    "price": snap["price"],
                    "price_token": price_token,
                    "baseline_hodl_value_usd": snap["hodl_usd"],
                    "strategy_total_value_usd": snap["strategy_usd"],
                    "wallet_idle": {"usdc": snap["wallet_usdc"], "eth": snap["wallet_eth"]},
                    "wallet_idle_value_usd": round(wlt["usdc"] + wlt["eth"] * price, 2),
                    "in_position": bool(pos is not None),
                    "lp_value_usd": snap["lp_value_usd"],
                    "fees_accrued_usd": snap["fees_accrued_usd"],
                    "poly_equity_usd": snap["poly_equity_usd"],
                    "range": snap.get("range"),
                    "lower_bid": snap.get("lower_bid"),
                    "upper_bid": snap.get("upper_bid"),
                },
            )

    while i < len(candles):
        candle = candles[i]
        close_price = float(candle["close"])
        current_price = close_price if price_token == 0 else 1.0 / close_price
        ts = int(candle["periodStartUnix"])

        if current_pos is None:
            range_method = None
            if fixed_range is not None:
                mn, mx = fixed_range
                if mn >= current_price or mx <= current_price:
                    _snap(ts, current_price, current_pos, wallet)
                    i += 1
                    continue
                insurance_info = _get_insurance_for_range(mn, mx, token_symbol, ts, conn)
                if insurance_info is None:
                    _snap(ts, current_price, current_pos, wallet)
                    i += 1
                    continue
                range_method = "fixed"
            elif use_lookback:
                lookback_slice = all_candles[: warmup_len + i]
                range_info = pick_best_range_by_sweep(
                    lookback_slice, pool_data, all_combos, current_price,
                    token_symbol, investment, conn,
                    price_token=price_token, cooldown_hours=cooldown_hours,
                    candle_ts=ts, simulate_fn=simulate,
                )
                if range_info is None:
                    _snap(ts, current_price, current_pos, wallet)
                    i += 1
                    continue
                mn, mx = range_info["min"], range_info["max"]
                insurance_info = {"lower_bet_price": range_info["lower_bet_price"], "upper_bet_price": range_info["upper_bet_price"]}
                range_method = "lookback"
                sweep_score = range_info.get("sweep_score")
            else:
                range_info = pick_best_range(all_combos, current_price, token_symbol, ts, investment, conn)
                if range_info is None:
                    _snap(ts, current_price, current_pos, wallet)
                    i += 1
                    continue
                mn, mx = range_info["min"], range_info["max"]
                insurance_info = {"lower_bet_price": range_info["lower_bet_price"], "upper_bet_price": range_info["upper_bet_price"]}
                range_method = "heuristic"

            current_pos = open_position(candle, pool_data, mn, mx, wallet, insurance_info, price_token, gas_prices=gas_prices, spread=spread)
            if current_pos is None:
                _snap(ts, current_price, current_pos, wallet)
                i += 1
                continue

            lower_clob_id = get_clob_token_id(token_symbol, mn, "down", "Yes", conn) if conn else None
            upper_clob_id = get_clob_token_id(token_symbol, mx, "up", "Yes", conn) if conn else None

            wb = current_pos['wallet_before']
            method_tag = f" ({range_method})" if range_method else ""
            if range_method == "lookback" and sweep_score is not None:
                method_tag = f" (lookback sweep, efficiency={sweep_score:+,.0f})"
            log(f"\n--- OPEN #{len(positions)+1} [{mn:.0f}, {mx:.0f}]{method_tag} @ ${current_price:.0f} | {datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')} ---")
            log(f"  Wallet before:  {wb['usdc']:>12,.2f} USDC  {wb['eth']:>12.6f} ETH  (=${wb['value_usd']:,.0f})")
            log(f"  Insurance cost: {current_pos['insurance_cost']:>12,.2f} USDC  (lower=${current_pos['lower_insurance_cost_usdc']:,.2f} upper=${current_pos['upper_insurance_cost_usdc']:,.2f})")
            log(f"  Swap fee:       {current_pos['swap_fee']:>12,.2f} USDC  (swapped ${current_pos['swap_amount']:,.0f})")
            log(f"  Gas fee (open): {current_pos['gas_fee_open']:>12,.2f} USDC")
            if current_pos.get('spread_cost_buy', 0) > 0:
                log(f"  Spread (buy):   {current_pos['spread_cost_buy']:>12,.2f} USDC")
            log(f"  LP deposit:     {current_pos['token0_dep']:>12,.2f} USDC  {current_pos['token1_dep']:>12.6f} ETH  (=${current_pos['deposit_value']:,.0f})")
            if telemetry is not None:
                telemetry.emit(
                    "position_open",
                    ts,
                    payload={
                        "min_range": current_pos["min_range"],
                        "max_range": current_pos["max_range"],
                        "entry_price": current_pos["entry_price"],
                        "insurance_cost_usdc": current_pos["insurance_cost"],
                        "lower_bet_price": current_pos["lower_bet_price"],
                        "upper_bet_price": current_pos["upper_bet_price"],
                        "lower_contracts": current_pos["lower_contracts"],
                        "upper_contracts": current_pos["upper_contracts"],
                        "deposit_value_usd": current_pos["deposit_value"],
                        "deposit_tokens": {"usdc": current_pos["token0_dep"], "eth": current_pos["token1_dep"]},
                        "wallet_before": current_pos["wallet_before"],
                        "swap_fee_usdc": current_pos.get("swap_fee", 0.0),
                        "gas_fee_open_usdc": current_pos.get("gas_fee_open", 0.0),
                        "spread_cost_buy_usdc": current_pos.get("spread_cost_buy", 0.0),
                    },
                )
            _snap(ts, current_price, current_pos, wallet)
            i += 1
            continue

        low = float(candle["low"])
        high = float(candle["high"])
        price_low = low if price_token == 0 else (1.0 / low if low else 0)
        price_high = high if price_token == 0 else (1.0 / high if high else 0)
        if price_token == 1:
            price_low, price_high = price_high, price_low

        if i > 0:
            f_usdc, f_eth = compute_hourly_fee_split(
                candle, candles[i - 1],
                current_pos["liquidity"],
                current_pos["min_range"], current_pos["max_range"],
                dec0, dec1, price_token,
            )
            current_pos["accumulated_fees_usdc"] += f_usdc
            current_pos["accumulated_fees_eth"] += f_eth
            current_pos["candle_count"] += 1

        touched_lower = price_low <= current_pos["min_range"]
        touched_upper = price_high >= current_pos["max_range"]

        if not (touched_lower or touched_upper):
            _snap(ts, current_price, current_pos, wallet)
            i += 1
            continue

        if touched_lower or touched_upper:
            current_pos, wallet = close_position(
                current_pos, candle, touched_lower, touched_upper,
                price_token, token_symbol, conn, gas_prices=gas_prices, spread=spread,
            )
            positions.append(current_pos)

            side = "LOWER" if touched_lower else "UPPER"
            sb = current_pos.get('insurance_sellback', 0)
            gc = current_pos.get('gas_fee_close', 0)
            sc_sell = current_pos.get('spread_cost_sell', 0)
            wa = current_pos['wallet_after']
            log(f"  >> CLOSE ({side}) @ ${current_pos['close_price']:.0f} | {datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')} | {current_pos['duration_hours']:.0f}h")
            log(f"  LP withdraw:    {current_pos['wd_usdc']:>12,.2f} USDC  {current_pos['wd_eth']:>12.6f} ETH")
            log(f"  Fees earned:    {current_pos['fees_earned_usdc']:>12,.2f} USDC  {current_pos['fees_earned_eth']:>12.6f} ETH  (=${current_pos['fees_earned_usd']:,.0f})")
            log(f"  IL:             {current_pos['il']:>12,.2f} USDC  ({current_pos['il_pct']:.2f}%)")
            log(f"  Ins payout:     {current_pos['insurance_payout']:>12,.2f} USDC")
            if sb > 0:
                log(f"  Ins sellback:   {sb:>12,.2f} USDC")
            if sc_sell > 0:
                log(f"  Spread (sell):  {sc_sell:>12,.2f} USDC")
            log(f"  Ins net:        {current_pos['insurance_net']:>12,.2f} USDC  (cost={current_pos['insurance_cost']:,.2f} pay={current_pos['insurance_payout']:,.2f} sell={sb:,.2f})")
            log(f"  Gas fee (close):{gc:>12,.2f} USDC")
            log(f"  Wallet after:   {wa['usdc']:>12,.2f} USDC  {wa['eth']:>12.6f} ETH  (=${wa['value_usd']:,.0f})")
            if telemetry is not None:
                close_reason = "lower" if touched_lower else "upper"
                telemetry.emit(
                    "position_close",
                    ts,
                    payload={
                        "reason": close_reason,
                        "close_price": current_pos.get("close_price"),
                        "duration_hours": current_pos.get("duration_hours"),
                        "fees_earned": {
                            "usdc": current_pos.get("fees_earned_usdc", 0.0),
                            "eth": current_pos.get("fees_earned_eth", 0.0),
                            "usd": current_pos.get("fees_earned_usd", 0.0),
                        },
                        "il_usdc": current_pos.get("il"),
                        "insurance": {
                            "cost_usdc": current_pos.get("insurance_cost"),
                            "payout_usdc": current_pos.get("insurance_payout"),
                            "sellback_usdc": current_pos.get("insurance_sellback", 0.0),
                            "net_usdc": current_pos.get("insurance_net"),
                        },
                        "swap_fee_usdc": current_pos.get("swap_fee", 0.0),
                        "gas_fee_close_usdc": current_pos.get("gas_fee_close", 0.0),
                        "spread_cost_sell_usdc": current_pos.get("spread_cost_sell", 0.0),
                        "wallet_after": current_pos.get("wallet_after"),
                    },
                )
            current_pos = None
            lower_clob_id = None
            upper_clob_id = None
            _snap(ts, current_price, current_pos, wallet)
            if telemetry is not None and cooldown_hours > 0:
                cooldown_start_ts = ts
                cooldown_end_ts = ts + cooldown_hours * 3600
                telemetry.emit(
                    "cooldown_start",
                    cooldown_start_ts,
                    payload={"cooldown_hours": cooldown_hours, "until_ts": cooldown_end_ts},
                )
                telemetry.emit(
                    "cooldown_end",
                    cooldown_end_ts,
                    payload={"cooldown_hours": cooldown_hours},
                )
            i += 1 + cooldown_hours
            continue

    if current_pos is not None:
        current_pos, wallet = close_position(
            current_pos, candles[-1], False, False,
            price_token, token_symbol, conn, gas_prices=gas_prices, spread=spread,
        )
        positions.append(current_pos)
        sb = current_pos.get('insurance_sellback', 0)
        gc = current_pos.get('gas_fee_close', 0)
        sc_sell = current_pos.get('spread_cost_sell', 0)
        wa = current_pos['wallet_after']
        log(f"  >> CLOSE (PERIOD END) @ ${current_pos['close_price']:.0f} | {current_pos['duration_hours']:.0f}h")
        log(f"  LP withdraw:    {current_pos['wd_usdc']:>12,.2f} USDC  {current_pos['wd_eth']:>12.6f} ETH")
        log(f"  Fees earned:    {current_pos['fees_earned_usdc']:>12,.2f} USDC  {current_pos['fees_earned_eth']:>12.6f} ETH  (=${current_pos['fees_earned_usd']:,.0f})")
        log(f"  IL:             {current_pos['il']:>12,.2f} USDC  ({current_pos['il_pct']:.2f}%)")
        log(f"  Ins sellback:   {sb:>12,.2f} USDC")
        if sc_sell > 0:
            log(f"  Spread (sell):  {sc_sell:>12,.2f} USDC")
        log(f"  Ins net:        {current_pos['insurance_net']:>12,.2f} USDC  (cost={current_pos['insurance_cost']:,.2f} pay=0 sell={sb:,.2f})")
        log(f"  Gas fee (close):{gc:>12,.2f} USDC")
        log(f"  Wallet after:   {wa['usdc']:>12,.2f} USDC  {wa['eth']:>12.6f} ETH  (=${wa['value_usd']:,.0f})")
        if telemetry is not None:
            telemetry.emit(
                "position_close",
                int(candles[-1]["periodStartUnix"]),
                payload={
                    "reason": "period_end",
                    "close_price": current_pos.get("close_price"),
                    "duration_hours": current_pos.get("duration_hours"),
                    "fees_earned": {
                        "usdc": current_pos.get("fees_earned_usdc", 0.0),
                        "eth": current_pos.get("fees_earned_eth", 0.0),
                        "usd": current_pos.get("fees_earned_usd", 0.0),
                    },
                    "il_usdc": current_pos.get("il"),
                    "insurance": {
                        "cost_usdc": current_pos.get("insurance_cost"),
                        "payout_usdc": 0.0,
                        "sellback_usdc": current_pos.get("insurance_sellback", 0.0),
                        "net_usdc": current_pos.get("insurance_net"),
                    },
                    "swap_fee_usdc": current_pos.get("swap_fee", 0.0),
                    "gas_fee_close_usdc": current_pos.get("gas_fee_close", 0.0),
                    "spread_cost_sell_usdc": current_pos.get("spread_cost_sell", 0.0),
                    "wallet_after": current_pos.get("wallet_after"),
                },
            )

    if telemetry is not None:
        final_ts = int(candles[-1]["periodStartUnix"])
        final_price = float(candles[-1]["close"]) if price_token == 0 else 1.0 / float(candles[-1]["close"])
        telemetry.emit(
            "run_end",
            final_ts,
            payload={
                "final_price": round(final_price, 6),
                "baseline_hodl_value_usd": round(initial_usdc + initial_eth * final_price, 2),
                "strategy_total_value_usd": round(wallet["usdc"] + wallet["eth"] * final_price, 2),
                "final_wallet": {"usdc": round(wallet["usdc"], 2), "eth": round(wallet["eth"], 6)},
                "positions": len(positions),
            },
        )

    return positions, wallet, snapshots


def build_summary(
    positions: List[Dict],
    candles: List[Dict],
    investment: float,
    pool_id: str,
    token_symbol: str,
    final_wallet: Dict,
    price_token: int = 0,
    snapshots: Optional[List[Dict]] = None,
) -> Dict:
    start_ts = int(candles[0]["periodStartUnix"])
    end_ts = int(candles[-1]["periodStartUnix"])
    total_hours = (end_ts - start_ts) / 3600
    total_days = total_hours / 24

    entry_price = float(candles[0]["close"]) if price_token == 0 else 1.0 / float(candles[0]["close"])
    final_price = float(candles[-1]["close"]) if price_token == 0 else 1.0 / float(candles[-1]["close"])

    total_fees_usdc = sum(p["fees_earned_usdc"] for p in positions)
    total_fees_eth = sum(p["fees_earned_eth"] for p in positions)
    total_fees_usd = sum(p["fees_earned_usd"] for p in positions)
    total_il = sum(p["il"] for p in positions)
    total_ins_cost = sum(p["insurance_cost"] for p in positions)
    total_ins_payout = sum(p["insurance_payout"] for p in positions)
    total_ins_sellback = sum(p.get("insurance_sellback", 0) for p in positions)
    total_ins_net = total_ins_payout + total_ins_sellback - total_ins_cost
    total_swap_fees = sum(p.get("swap_fee", 0) for p in positions)
    total_gas_fees = sum(p.get("gas_fee_open", 0) + p.get("gas_fee_close", 0) for p in positions)
    total_spread_cost = sum(p.get("spread_cost_buy", 0) + p.get("spread_cost_sell", 0) for p in positions)

    boundary_closes = sum(1 for p in positions if p.get("touched_lower") or p.get("touched_upper"))
    lower_touches = sum(1 for p in positions if p.get("touched_lower"))
    upper_touches = sum(1 for p in positions if p.get("touched_upper"))
    hours_in_position = sum(p["duration_hours"] for p in positions)
    utilization_pct = (hours_in_position / total_hours * 100) if total_hours > 0 else 0

    initial_usdc = investment / 2.0
    initial_eth = (investment / 2.0) / entry_price

    final_value = final_wallet["usdc"] + final_wallet["eth"] * final_price
    roi_pct = (final_value / investment - 1) * 100
    try:
        apy = ((final_value / investment) ** (365 / total_days) - 1) * 100 if total_days > 0 and final_value > 0 else 0
    except OverflowError:
        apy = float("inf") if final_value > investment else float("-inf")

    delta_usdc = final_wallet["usdc"] - initial_usdc
    delta_eth = final_wallet["eth"] - initial_eth
    delta_usdc_pct = (delta_usdc / initial_usdc * 100) if initial_usdc else 0
    delta_eth_pct = (delta_eth / initial_eth * 100) if initial_eth else 0

    t0_sym, t1_sym = "USDC", token_symbol

    pos_records = []
    for p in positions:
        wb = p["wallet_before"]
        wa = p["wallet_after"]
        pos_records.append({
            "open": datetime.fromtimestamp(p["open_ts"], tz=timezone.utc).isoformat(),
            "close": datetime.fromtimestamp(p["close_ts"], tz=timezone.utc).isoformat(),
            "range": [p["min_range"], p["max_range"]],
            "entry_price": round(p["entry_price"], 2),
            "close_price": round(p["close_price"], 2),
            "duration_hours": round(p["duration_hours"], 1),
            "wallet_before": {t0_sym: round(wb["usdc"], 2), t1_sym: round(wb["eth"], 6), "value_usd": round(wb["value_usd"], 2)},
            "deposit": {t0_sym: round(p["token0_dep"], 2), t1_sym: round(p["token1_dep"], 6), "value_usd": round(p["deposit_value"], 2)},
            "wallet_after": {t0_sym: round(wa["usdc"], 2), t1_sym: round(wa["eth"], 6), "value_usd": round(wa["value_usd"], 2)},
            "fees_earned_usdc": round(p["fees_earned_usdc"], 2),
            "fees_earned_eth": round(p["fees_earned_eth"], 6),
            "fees_earned_usd": round(p["fees_earned_usd"], 2),
            "il_usdc": round(p["il"], 2),
            "swap_fee_usdc": round(p.get("swap_fee", 0), 2),
            "swap_amount_usdc": round(p.get("swap_amount", 0), 2),
            "gas_fee_open_usdc": round(p.get("gas_fee_open", 0), 2),
            "gas_fee_close_usdc": round(p.get("gas_fee_close", 0), 2),
            "spread_cost_buy_usdc": round(p.get("spread_cost_buy", 0), 2),
            "spread_cost_sell_usdc": round(p.get("spread_cost_sell", 0), 2),
            "insurance_cost_usdc": round(p["insurance_cost"], 2),
            "insurance_payout_usdc": round(p["insurance_payout"], 2),
            "insurance_sellback_usdc": round(p.get("insurance_sellback", 0), 2),
            "insurance_net_usdc": round(p["insurance_net"], 2),
            "touched_lower": p.get("touched_lower", False),
            "touched_upper": p.get("touched_upper", False),
        })

    return {
        "pool_id": pool_id,
        "token_symbol": token_symbol,
        "investment_usd": investment,
        "initial_wallet": {t0_sym: round(initial_usdc, 2), t1_sym: round(initial_eth, 6), "value_usd": round(investment, 2)},
        "final_wallet": {t0_sym: round(final_wallet["usdc"], 2), t1_sym: round(final_wallet["eth"], 6), "value_usd": round(final_value, 2)},
        "token_delta": {
            t0_sym: round(delta_usdc, 2),
            t1_sym: round(delta_eth, 6),
            f"{t0_sym}_pct": round(delta_usdc_pct, 2),
            f"{t1_sym}_pct": round(delta_eth_pct, 2),
        },
        "period": {
            "start": datetime.fromtimestamp(start_ts, tz=timezone.utc).isoformat(),
            "end": datetime.fromtimestamp(end_ts, tz=timezone.utc).isoformat(),
            "total_days": round(total_days, 1),
            "total_hours": round(total_hours),
        },
        "prices": {"entry": round(entry_price, 2), "final": round(final_price, 2), "change_pct": round((final_price / entry_price - 1) * 100, 2) if entry_price else 0},
        "active_strategy": {
            "total_positions": len(positions),
            "boundary_closes": boundary_closes, "lower_touches": lower_touches, "upper_touches": upper_touches,
            "hours_in_position": round(hours_in_position, 1), "utilization_pct": round(utilization_pct, 1),
            "total_fees_usdc": round(total_fees_usdc, 2), "total_fees_eth": round(total_fees_eth, 6),
            "total_fees_usd": round(total_fees_usd, 2), "total_il_usdc": round(total_il, 2),
            "total_swap_fees_usdc": round(total_swap_fees, 2),
            "total_gas_fees_usdc": round(total_gas_fees, 2),
            "total_spread_cost_usdc": round(total_spread_cost, 2),
            "total_insurance_cost_usdc": round(total_ins_cost, 2), "total_insurance_payout_usdc": round(total_ins_payout, 2),
            "total_insurance_sellback_usdc": round(total_ins_sellback, 2), "total_insurance_net_usdc": round(total_ins_net, 2),
            "final_value_usd": round(final_value, 2), "roi_pct": round(roi_pct, 2), "apy": round(apy, 2),
        },
        "positions": pos_records,
        "snapshots": snapshots or [],
    }


def run_sweep(
    candles: List[Dict],
    pool_data: Dict,
    token_symbol: str,
    investment: float,
    conn,
    price_token: int = 0,
    cooldown_hours: int = 1,
    gas_prices: Optional[Dict[str, int]] = None,
    spread: float = 0.0,
) -> List[Dict]:
    """Run simulation for every valid Polymarket range combo, return ranked results."""
    get_range_combinations = _get_db_func("get_range_combinations")
    all_combos = get_range_combinations(token_symbol, conn)
    if not all_combos:
        raise ValueError(f"No Polymarket range combinations found for {token_symbol}")

    first_price = float(candles[0]["close"]) if price_token == 0 else 1.0 / float(candles[0]["close"])
    final_price = float(candles[-1]["close"]) if price_token == 0 else 1.0 / float(candles[-1]["close"])
    start_ts = int(candles[0]["periodStartUnix"])
    end_ts = int(candles[-1]["periodStartUnix"])
    total_days = (end_ts - start_ts) / 3600 / 24

    seen = set()
    unique_ranges = []
    for c in all_combos:
        mn, mx = float(c["min"]), float(c["max"])
        key = (round(mn, 2), round(mx, 2))
        if key in seen:
            continue
        seen.add(key)
        if mn >= first_price and mx <= first_price:
            continue
        if mx <= mn:
            continue
        unique_ranges.append((mn, mx))

    logger.info(f"Sweeping {len(unique_ranges)} range combinations...")
    results = []

    for idx, (mn, mx) in enumerate(unique_ranges):
        try:
            positions, final_wallet, _snaps = simulate(
                candles, pool_data, token_symbol, investment, conn,
                price_token=price_token, cooldown_hours=cooldown_hours,
                fixed_range=(mn, mx), quiet=True,
                gas_prices=gas_prices, spread=spread,
            )
        except Exception:
            continue

        if not positions:
            continue

        initial_usdc = investment / 2.0
        initial_eth = (investment / 2.0) / first_price

        final_value = final_wallet["usdc"] + final_wallet["eth"] * final_price
        roi_pct = (final_value / investment - 1) * 100
        apy = ((final_value / investment) ** (365 / total_days) - 1) * 100 if total_days > 0 and final_value > 0 else 0
        total_fees_usdc = sum(p["fees_earned_usdc"] for p in positions)
        total_fees_eth = sum(p["fees_earned_eth"] for p in positions)
        total_fees_usd = sum(p["fees_earned_usd"] for p in positions)
        total_il = sum(p["il"] for p in positions)
        total_ins_cost = sum(p["insurance_cost"] for p in positions)
        total_ins_payout = sum(p["insurance_payout"] for p in positions)
        total_ins_sellback = sum(p.get("insurance_sellback", 0) for p in positions)
        boundary_closes = sum(1 for p in positions if p.get("touched_lower") or p.get("touched_upper"))

        delta_usdc = final_wallet["usdc"] - initial_usdc
        delta_eth = final_wallet["eth"] - initial_eth

        results.append({
            "range": [mn, mx],
            "positions": len(positions),
            "boundary_closes": boundary_closes,
            "total_fees_usdc": round(total_fees_usdc, 2),
            "total_fees_eth": round(total_fees_eth, 6),
            "total_fees_usd": round(total_fees_usd, 2),
            "total_il_usdc": round(total_il, 2),
            "insurance_cost_usdc": round(total_ins_cost, 2),
            "insurance_payout_usdc": round(total_ins_payout, 2),
            "insurance_sellback_usdc": round(total_ins_sellback, 2),
            "insurance_net_usdc": round(total_ins_payout + total_ins_sellback - total_ins_cost, 2),
            "final_wallet": {
                "USDC": round(final_wallet["usdc"], 2),
                token_symbol: round(final_wallet["eth"], 6),
                "value_usd": round(final_value, 2),
            },
            "token_delta": {
                "USDC": round(delta_usdc, 2),
                token_symbol: round(delta_eth, 6),
            },
            "roi_pct": round(roi_pct, 2),
            "apy": round(apy, 2),
        })

        if (idx + 1) % 50 == 0:
            logger.info(f"  ... {idx + 1}/{len(unique_ranges)} ranges done")

    results.sort(key=lambda r: r["apy"], reverse=True)
    return results


def main():
    ap = argparse.ArgumentParser(description="Active Position Backtester")
    ap.add_argument("--pool", default="0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640", help="Uniswap V3 pool address")
    ap.add_argument("--days", type=int, default=90, help="Backtest window in days")
    ap.add_argument("--investment", type=float, default=100000.0, help="Investment amount in USD")
    ap.add_argument("--cooldown", type=int, default=1, help="Hours to wait after closing before reopening")
    ap.add_argument("--price-token", type=int, default=0, help="Which token is the price base (0 or 1)")
    ap.add_argument("--fixed-range", type=str, default=None, help="Force a specific range, e.g. '2000,2400'")
    ap.add_argument("--sweep", action="store_true", help="Sweep all Polymarket ranges and rank by APY")
    ap.add_argument("--lookback", type=int, default=0, help="Days of warm-up data for lookback sweep range selection (0 = heuristic)")
    ap.add_argument("--spread", type=float, default=0.04, help="Polymarket bid-ask spread in $ per contract (default 0.04)")
    ap.add_argument("--telemetry-path", type=str, default=None, help="Write structured JSONL telemetry to this path (disabled if omitted)")
    ap.add_argument("--no-telemetry", action="store_true", help="Disable telemetry even if --telemetry-path is set")
    ap.add_argument("--output", default="active_backtest_results.json", help="Output JSON path")
    args = ap.parse_args()

    get_db_connection = _get_db_func("get_db_connection")

    logger.info(f"Fetching pool metadata for {args.pool}...")
    pool_data = fetch_pool_metadata(args.pool)
    token_symbol = _map_wrapped_symbol(pool_data["token1"]["symbol"])
    logger.info(f"Pool: {pool_data['token0']['symbol']}/{pool_data['token1']['symbol']} -> token={token_symbol}")

    now = datetime.now(timezone.utc)
    end_ts = int(now.timestamp())
    total_fetch_days = args.days + args.lookback
    start_ts = int((now - timedelta(days=total_fetch_days)).timestamp())

    start_date_str = (now - timedelta(days=total_fetch_days)).strftime("%Y-%m-%d")
    end_date_str = now.strftime("%Y-%m-%d")
    logger.info("Fetching historical gas prices via RPC block sampling...")
    gas_prices = fetch_daily_gas_prices(start_date_str, end_date_str)

    logger.info(f"Fetching {total_fetch_days} days of hourly candles ({args.days}d backtest + {args.lookback}d lookback)...")
    all_candles = fetch_hourly_candles(args.pool, start_ts, end_ts)
    logger.info(f"Fetched {len(all_candles)} hourly candles")

    if len(all_candles) < 10:
        logger.error("Too few candles fetched, aborting")
        sys.exit(1)

    lookback_hours = args.lookback * 24
    if args.lookback > 0 and len(all_candles) > lookback_hours:
        candles = all_candles[lookback_hours:]
        warmup_candles = all_candles[:lookback_hours]
        logger.info(f"Split: {len(warmup_candles)} warmup candles + {len(candles)} backtest candles")
    else:
        candles = all_candles
        warmup_candles = None

    fixed_range = None
    if args.fixed_range:
        parts = args.fixed_range.split(",")
        fixed_range = (float(parts[0]), float(parts[1]))

    conn = get_db_connection()
    try:
        if args.sweep:
            results = run_sweep(
                candles, pool_data, token_symbol, args.investment, conn,
                price_token=args.price_token, cooldown_hours=args.cooldown,
                gas_prices=gas_prices, spread=args.spread,
            )
            with open(args.output, "w") as f:
                json.dump(results, f, indent=2)
            init_usdc = args.investment / 2.0
            first_p = float(candles[0]["close"]) if args.price_token == 0 else 1.0 / float(candles[0]["close"])
            init_eth = (args.investment / 2.0) / first_p
            logger.info(f"\n{'='*120}")
            logger.info(f"SWEEP RESULTS — {len(results)} ranges tested ({args.days}d, ${args.investment:,.0f})")
            logger.info(f"Initial: {init_usdc:,.0f} USDC + {init_eth:.4f} {token_symbol}")
            logger.info(f"{'='*120}")
            logger.info(f"{'Range':>20s} {'Pos':>4s} {'Hits':>4s} {'dUSDC':>12s} {'d'+token_symbol:>10s} {'Fees':>10s} {'InsCost':>10s} {'InsPay':>10s} {'APY%':>8s}")
            logger.info("-" * 120)
            for r in results[:30]:
                rng = f"[{r['range'][0]:.0f}-{r['range'][1]:.0f}]"
                td = r["token_delta"]
                logger.info(
                    f"{rng:>20s} {r['positions']:>4d} {r['boundary_closes']:>4d} "
                    f"{td['USDC']:>+12,.0f} {td[token_symbol]:>+10.4f} "
                    f"${r['total_fees_usdc']:>9,.0f} "
                    f"${r['insurance_cost_usdc']:>9,.0f} ${r['insurance_payout_usdc']:>9,.0f} "
                    f"{r['apy']:>7.2f}%"
                )
            logger.info(f"\nFull results saved to {args.output}")
        else:
            mode = "lookback sweep" if warmup_candles else ("fixed" if fixed_range else "heuristic")
            logger.info(f"Starting active strategy simulation (range selection: {mode})...")
            telemetry = None
            if args.telemetry_path and not args.no_telemetry:
                rid = new_run_id()
                default_path = args.telemetry_path
                # If a directory is provided, write a file inside it.
                if default_path.endswith("/") or os.path.isdir(default_path):
                    default_path = os.path.join(default_path, f"active_backtester_{rid}.jsonl")
                telemetry = TelemetrySink(path=default_path, run_id=rid, enabled=True)
            positions, final_wallet, hourly_snapshots = simulate(
                candles, pool_data, token_symbol, args.investment, conn,
                price_token=args.price_token, cooldown_hours=args.cooldown,
                fixed_range=fixed_range,
                warmup_candles=warmup_candles,
                all_candles=all_candles if warmup_candles else None,
                gas_prices=gas_prices, spread=args.spread,
                telemetry=telemetry,
            )
            summary = build_summary(
                positions, candles, args.investment, args.pool, token_symbol, final_wallet, args.price_token,
                snapshots=hourly_snapshots,
            )
            with open(args.output, "w") as f:
                json.dump(summary, f, indent=2)

            s = summary["active_strategy"]
            iw = summary["initial_wallet"]
            fw = summary["final_wallet"]
            td = summary["token_delta"]
            t1 = token_symbol

            logger.info(f"\n{'='*60}")
            if warmup_candles:
                logger.info(f"RANGE SELECTION: lookback sweep ({args.lookback}d window)")
            elif fixed_range:
                logger.info(f"RANGE SELECTION: fixed [{fixed_range[0]:.0f}, {fixed_range[1]:.0f}]")
            else:
                logger.info("RANGE SELECTION: heuristic (narrowness - cost)")
            logger.info(f"INITIAL DEPOSIT ({summary['prices']['entry']:.0f} $/ETH)")
            logger.info(f"  {iw['USDC']:>12,.2f} USDC")
            logger.info(f"  {iw[t1]:>12.6f} {t1}")
            logger.info(f"{'='*60}")
            logger.info(f"FINAL WALLET ({summary['prices']['final']:.0f} $/ETH)")
            logger.info(f"  {fw['USDC']:>12,.2f} USDC  ({td['USDC']:>+,.2f}  {td['USDC_pct']:>+.2f}%)")
            logger.info(f"  {fw[t1]:>12.6f} {t1}  ({td[t1]:>+.6f}  {td[f'{t1}_pct']:>+.2f}%)")
            logger.info(f"{'='*60}")
            logger.info(f"Positions: {s['total_positions']}  (boundary: {s['boundary_closes']}, lower: {s['lower_touches']}, upper: {s['upper_touches']})")
            logger.info(f"Fees:      {s['total_fees_usdc']:,.2f} USDC + {s['total_fees_eth']:.6f} {t1}  (=${s['total_fees_usd']:,.2f})")
            logger.info(f"IL:        ${s['total_il_usdc']:,.2f}")
            logger.info(f"Swap fees: ${s['total_swap_fees_usdc']:,.2f}")
            logger.info(f"Gas fees:  ${s['total_gas_fees_usdc']:,.2f}")
            logger.info(f"Spread:    ${s['total_spread_cost_usdc']:,.2f}")
            logger.info(f"Insurance: cost=${s['total_insurance_cost_usdc']:,.2f}  payout=${s['total_insurance_payout_usdc']:,.2f}  sellback=${s['total_insurance_sellback_usdc']:,.2f}  net=${s['total_insurance_net_usdc']:,.2f}")
            logger.info(f"ROI (USD): {s['roi_pct']:.2f}%    APY: {s['apy']:.2f}%")
            logger.info(f"ETH:       ${summary['prices']['entry']:.0f} -> ${summary['prices']['final']:.0f} ({summary['prices']['change_pct']:.1f}%)")
            logger.info(f"\nResults saved to {args.output}")

    finally:
        conn.close()

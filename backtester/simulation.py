"""Simulation loop, sweep mode, summary builder, and config-driven entrypoint."""

import json
import logging
import os
import sys
import math
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

from il import tokens_from_liquidity, liquidity_from_tokens

from .graph_client import fetch_pool_metadata, fetch_hourly_candles
from .fee_math import compute_hourly_fee_split, _tokens_for_strategy_human
from .gas import fetch_daily_gas_prices
from .data_validation import (
    validate_candles,
    validate_gas_coverage,
    validate_polymarket_coverage,
)
from .range_selection import (
    _map_wrapped_symbol,
    _filter_ranges_for_price,
    pick_best_range,
    pick_best_range_by_sweep,
    _get_insurance_for_range,
)
from .positions import open_position, close_position
from .polymarket_execution import ClosePolicy, SlippageConfig, choose_close_price
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
    slippage_per_1k_contracts: float = 0.0,
    slippage_max_per_contract: float = 0.0,
    close_policy: ClosePolicy = "touch",
    telemetry: Optional[TelemetrySink] = None,
    initial_eth: Optional[float] = None,
    initial_usdc: Optional[float] = None,
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
    get_candidate_markets = _get_db_func("get_candidate_markets")
    use_lookback = warmup_candles is not None and all_candles is not None and fixed_range is None
    # Range combinations depend on which Polymarket markets were active at a given time.
    # For historical backtests we should query combinations "as-of" the candle timestamp.
    # We keep an initial snapshot for early fallbacks, but selection will refresh per-open.
    all_combos = get_range_combinations(token_symbol, conn) if conn is not None else []

    warmup_len = len(warmup_candles) if warmup_candles else 0

    dec0 = int(pool_data["token0"]["decimals"])
    dec1 = int(pool_data["token1"]["decimals"])

    first_price = float(candles[0]["close"]) if price_token == 0 else 1.0 / float(candles[0]["close"])
    # Capital model: ETH-first (no USD 50/50 fallback).
    if initial_eth is None:
        raise ValueError("initial_eth must be provided (ETH-first mode only).")
    wallet = {"usdc": float(initial_usdc or 0.0), "eth": float(initial_eth)}
    baseline_ready = False
    baseline_initial_usdc = 0.0
    baseline_initial_eth = 0.0
    initial_notional_usd: Optional[float] = None

    slippage_cfg = SlippageConfig(
        per_1k_contracts=float(slippage_per_1k_contracts or 0.0),
        max_per_contract=float(slippage_max_per_contract or 0.0),
    )

    positions: List[Dict] = []
    snapshots: List[Dict] = []
    current_pos: Optional[Dict] = None
    lower_clob_id: Optional[str] = None
    upper_clob_id: Optional[str] = None
    i = 0
    log = logger.info if not quiet else logger.debug
    pending_close: Optional[Dict] = None
    delta_matched_qty: Optional[Dict[str, float]] = None

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
                "slippage_per_1k_contracts": slippage_cfg.per_1k_contracts,
                "slippage_max_per_contract": slippage_cfg.max_per_contract,
                "close_policy": close_policy,
                "baseline_initial_usdc": baseline_initial_usdc,
                "baseline_initial_eth": baseline_initial_eth,
                "capital_model": "eth_first",
                "external_costs": True,
            },
        )

    def _required_usdc_for_eth(*, mn: float, mx: float, price: float, eth_amt: float) -> float:
        """Compute required token0 (USDC) to pair with `eth_amt` token1 (ETH) at `price` within [mn, mx]."""
        sp = math.sqrt(float(price))
        sl = math.sqrt(float(mn))
        sh = math.sqrt(float(mx))
        # Price is expected to be inside the range for a valid open.
        if not (sl < sp < sh) or eth_amt <= 0:
            return 0.0
        ratio = (sp - sl) * sp * sh / (sh - sp)  # token0/token1
        return float(eth_amt) * ratio

    def _snap(ts, price, pos, wlt):
        """Build an hourly snapshot dict."""
        hodl_usd = baseline_initial_usdc + baseline_initial_eth * price
        dm_hodl_usd = None
        if delta_matched_qty is not None:
            dm_hodl_usd = delta_matched_qty["usdc"] + delta_matched_qty["eth"] * price

        if pos is not None:
            # Use the position's stored human-unit liquidity for mark-to-market.
            # Re-splitting ``deposit_value`` at a drifted price would conflate
            # "what if we rebuilt the LP now" with "what the existing LP holds
            # now" — the latter is what the equity curve needs.
            l_human = pos.get("L_human") or liquidity_from_tokens(
                pos["entry_price"], pos["token0_dep"], pos["token1_dep"],
                pos["min_range"], pos["max_range"],
            )
            clamped = max(pos["min_range"], min(price, pos["max_range"]))
            lp_usdc, lp_eth = tokens_from_liquidity(
                clamped, pos["min_range"], pos["max_range"], l_human,
            )
            # Valuation is at the *current* price (unclamped): when price is
            # outside the range the LP is 100% in the favoured token at the
            # boundary amount, and that amount is valued at the market price.
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
        if dm_hodl_usd is not None:
            snap["delta_matched_hodl_usd"] = round(dm_hodl_usd, 2)
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

        # If a previous candle triggered a next-candle close, settle now using
        # *this* candle's close. This is the only allowed lookahead for that policy.
        if pending_close is not None and current_pos is not None:
            touched_lower = bool(pending_close["touched_lower"])
            touched_upper = bool(pending_close["touched_upper"])
            close_px, close_src = choose_close_price(
                policy="next_candle",
                touched_lower=touched_lower,
                touched_upper=touched_upper,
                min_range=current_pos["min_range"],
                max_range=current_pos["max_range"],
                candle_close_price=current_price,
                next_candle_close_price=current_price,
            )
            current_pos["close_policy"] = "next_candle"
            current_pos["close_price_source"] = close_src
            current_pos, wallet = close_position(
                current_pos,
                candle,
                touched_lower,
                touched_upper,
                price_token,
                token_symbol,
                conn,
                gas_prices=gas_prices,
                spread=spread,
                slippage_cfg=slippage_cfg,
                close_price_override=close_px,
            )
            positions.append(current_pos)
            pending_close = None

            # Logging / telemetry mirrors the touch-based close branch below.
            side = "LOWER" if touched_lower else "UPPER"
            sb = current_pos.get("insurance_sellback", 0)
            gc = current_pos.get("gas_fee_close", 0)
            sc_sell = current_pos.get("spread_cost_sell", 0)
            sl_sell = current_pos.get("slippage_cost_sell", 0)
            wa = current_pos["wallet_after"]
            log(
                f"  >> CLOSE ({side}, next candle) @ ${current_pos['close_price']:.0f} | "
                f"{datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')} | {current_pos['duration_hours']:.0f}h"
            )
            log(f"  LP withdraw:    {current_pos['wd_usdc']:>12,.2f} USDC  {current_pos['wd_eth']:>12.6f} ETH")
            log(f"  Fees earned:    {current_pos['fees_earned_usdc']:>12,.2f} USDC  {current_pos['fees_earned_eth']:>12.6f} ETH  (=${current_pos['fees_earned_usd']:,.0f})")
            log(f"  IL:             {current_pos['il']:>12,.2f} USDC  ({current_pos['il_pct']:.2f}%)")
            log(f"  Ins payout:     {current_pos['insurance_payout']:>12,.2f} USDC")
            if sb > 0:
                log(f"  Ins sellback:   {sb:>12,.2f} USDC")
            if sc_sell > 0:
                log(f"  Spread (sell):  {sc_sell:>12,.2f} USDC")
            if sl_sell > 0:
                log(f"  Slippage (sell):{sl_sell:>12,.2f} USDC")
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
                        "close_policy": "next_candle",
                        "close_price": current_pos.get("close_price"),
                        "close_price_source": current_pos.get("close_price_source"),
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
                        "slippage_cost_sell_usdc": current_pos.get("slippage_cost_sell", 0.0),
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
                    # Fallback: use the static bet prices from the range combinations table
                    # (same source used by heuristic selection), when historical mids are missing.
                    # Important: query combinations "as-of" this candle timestamp (markets rotate).
                    combos_at_ts = get_range_combinations(token_symbol, conn, candle_ts=ts) if conn is not None else []
                    match = None
                    mn_key = round(float(mn), 2)
                    mx_key = round(float(mx), 2)
                    for c in (combos_at_ts or []):
                        try:
                            if round(float(c.get("min")), 2) == mn_key and round(float(c.get("max")), 2) == mx_key:
                                match = c
                                break
                        except Exception:
                            continue
                    if match is not None and (match.get("lower_bet_price") is not None or match.get("upper_bet_price") is not None):
                        insurance_info = {
                            "lower_bet_price": float(match.get("lower_bet_price") or 0.5),
                            "upper_bet_price": float(match.get("upper_bet_price") or 0.5),
                        }
                    else:
                        insurance_info = None
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
                    candle_ts=ts,
                    simulate_fn=(lambda *a, **kw: simulate(*a, **kw, initial_eth=initial_eth, initial_usdc=initial_usdc)),
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
                combos_at_ts = get_range_combinations(token_symbol, conn, candle_ts=ts) if conn is not None else all_combos
                range_info = pick_best_range(combos_at_ts, current_price, token_symbol, ts, investment, conn)
                if range_info is None:
                    _snap(ts, current_price, current_pos, wallet)
                    i += 1
                    continue
                mn, mx = range_info["min"], range_info["max"]
                insurance_info = {"lower_bet_price": range_info["lower_bet_price"], "upper_bet_price": range_info["upper_bet_price"]}
                range_method = "heuristic"

            # ETH-first capital model: compute required USDC (Y) once we know the opening range.
            if (initial_eth is not None) and (not baseline_ready):
                if initial_usdc is not None and float(initial_usdc) > 0:
                    wallet["usdc"] = float(initial_usdc)
                else:
                    wallet["usdc"] = _required_usdc_for_eth(
                        mn=float(mn),
                        mx=float(mx),
                        price=float(current_price),
                        eth_amt=float(wallet["eth"]),
                    )
                baseline_initial_usdc = float(wallet["usdc"])
                baseline_initial_eth = float(wallet["eth"])
                baseline_ready = True
                initial_notional_usd = baseline_initial_usdc + baseline_initial_eth * float(current_price)

            current_pos = open_position(
                candle,
                pool_data,
                mn,
                mx,
                wallet,
                insurance_info,
                price_token,
                gas_prices=gas_prices,
                spread=spread,
                slippage_cfg=slippage_cfg,
            )
            if current_pos is None:
                _snap(ts, current_price, current_pos, wallet)
                i += 1
                continue

            # Pick Polymarket markets (and expiries) for this position.
            lower_meta = None
            upper_meta = None
            if conn is not None:
                try:
                    lower_cands = get_candidate_markets(token_symbol, mn, "down", "Yes", conn, candle_ts=ts)
                    upper_cands = get_candidate_markets(token_symbol, mx, "up", "Yes", conn, candle_ts=ts)
                    lower_meta = lower_cands[0] if lower_cands else None
                    upper_meta = upper_cands[0] if upper_cands else None
                except Exception:
                    lower_meta = None
                    upper_meta = None

            def _end_ts(meta):
                if not meta:
                    return None
                ed = meta.get("end_date")
                try:
                    return int(ed.timestamp())
                except Exception:
                    return None

            lower_clob_id = (lower_meta or {}).get("clob_token_id") or (
                get_clob_token_id(token_symbol, mn, "down", "Yes", conn, candle_ts=ts) if conn else None
            )
            upper_clob_id = (upper_meta or {}).get("clob_token_id") or (
                get_clob_token_id(token_symbol, mx, "up", "Yes", conn, candle_ts=ts) if conn else None
            )

            current_pos["lower_clob_token_id"] = lower_clob_id
            current_pos["upper_clob_token_id"] = upper_clob_id
            current_pos["lower_end_ts"] = _end_ts(lower_meta)
            current_pos["upper_end_ts"] = _end_ts(upper_meta)

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
            if current_pos.get('slippage_cost_buy', 0) > 0:
                log(f"  Slippage (buy): {current_pos['slippage_cost_buy']:>12,.2f} USDC")
            log(f"  LP deposit:     {current_pos['token0_dep']:>12,.2f} USDC  {current_pos['token1_dep']:>12.6f} ETH  (=${current_pos['deposit_value']:,.0f})")

            # Delta-matched HODL baseline: lock in the first position's deposit split.
            if delta_matched_qty is None:
                delta_matched_qty = {"usdc": float(current_pos["token0_dep"]), "eth": float(current_pos["token1_dep"])}

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
                        "slippage_cost_buy_usdc": current_pos.get("slippage_cost_buy", 0.0),
                        "close_policy": close_policy,
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

        # Insurance expiry: if either market has reached end_date, force-close the position
        # and treat remaining insurance value as $0 (per project assumption).
        le = current_pos.get("lower_end_ts")
        ue = current_pos.get("upper_end_ts")
        expired = (le is not None and ts >= int(le)) or (ue is not None and ts >= int(ue))
        if expired:
            current_pos["close_policy"] = "expiry"
            current_pos["close_price_source"] = "candle_close"
            current_pos, wallet = close_position(
                current_pos,
                candle,
                False,
                False,
                price_token,
                token_symbol,
                conn,
                gas_prices=gas_prices,
                spread=spread,
                slippage_cfg=slippage_cfg,
                close_price_override=current_price,
                expired=True,
            )
            positions.append(current_pos)
            wa = current_pos["wallet_after"]
            log(
                f"  >> CLOSE (EXPIRY) @ ${current_pos['close_price']:.0f} | "
                f"{datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')} | {current_pos['duration_hours']:.0f}h"
            )
            log(f"  Wallet after:   {wa['usdc']:>12,.2f} USDC  {wa['eth']:>12.6f} ETH  (=${wa['value_usd']:,.0f})")
            current_pos = None
            lower_clob_id = None
            upper_clob_id = None
            _snap(ts, current_price, current_pos, wallet)
            i += 1 + cooldown_hours
            continue

        touched_lower = price_low <= current_pos["min_range"]
        touched_upper = price_high >= current_pos["max_range"]

        if not (touched_lower or touched_upper):
            _snap(ts, current_price, current_pos, wallet)
            i += 1
            continue

        if touched_lower or touched_upper:
            if close_policy == "next_candle":
                pending_close = {"touched_lower": touched_lower, "touched_upper": touched_upper, "ts": ts}
                _snap(ts, current_price, current_pos, wallet)
                i += 1
                continue

            close_px, close_src = choose_close_price(
                policy=close_policy,
                touched_lower=touched_lower,
                touched_upper=touched_upper,
                min_range=current_pos["min_range"],
                max_range=current_pos["max_range"],
                candle_close_price=current_price,
            )
            current_pos["close_policy"] = close_policy
            current_pos["close_price_source"] = close_src
            current_pos, wallet = close_position(
                current_pos,
                candle,
                touched_lower,
                touched_upper,
                price_token,
                token_symbol,
                conn,
                gas_prices=gas_prices,
                spread=spread,
                slippage_cfg=slippage_cfg,
                close_price_override=close_px,
            )
            positions.append(current_pos)

            side = "LOWER" if touched_lower else "UPPER"
            sb = current_pos.get('insurance_sellback', 0)
            gc = current_pos.get('gas_fee_close', 0)
            sc_sell = current_pos.get('spread_cost_sell', 0)
            sl_sell = current_pos.get('slippage_cost_sell', 0)
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
            if sl_sell > 0:
                log(f"  Slippage (sell):{sl_sell:>12,.2f} USDC")
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
                        "close_policy": close_policy,
                        "close_price": current_pos.get("close_price"),
                        "close_price_source": current_pos.get("close_price_source"),
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
                        "slippage_cost_sell_usdc": current_pos.get("slippage_cost_sell", 0.0),
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
        if pending_close is not None and close_policy == "next_candle":
            # Cannot look ahead beyond the dataset; close at the final candle close.
            current_pos["close_policy"] = "next_candle"
            current_pos["close_price_source"] = "candle_close_fallback_no_next"
        current_pos, wallet = close_position(
            current_pos, candles[-1], False, False,
            price_token, token_symbol, conn, gas_prices=gas_prices, spread=spread,
            slippage_cfg=slippage_cfg,
        )
        positions.append(current_pos)
        sb = current_pos.get('insurance_sellback', 0)
        gc = current_pos.get('gas_fee_close', 0)
        sc_sell = current_pos.get('spread_cost_sell', 0)
        sl_sell = current_pos.get('slippage_cost_sell', 0)
        wa = current_pos['wallet_after']
        log(f"  >> CLOSE (PERIOD END) @ ${current_pos['close_price']:.0f} | {current_pos['duration_hours']:.0f}h")
        log(f"  LP withdraw:    {current_pos['wd_usdc']:>12,.2f} USDC  {current_pos['wd_eth']:>12.6f} ETH")
        log(f"  Fees earned:    {current_pos['fees_earned_usdc']:>12,.2f} USDC  {current_pos['fees_earned_eth']:>12.6f} ETH  (=${current_pos['fees_earned_usd']:,.0f})")
        log(f"  IL:             {current_pos['il']:>12,.2f} USDC  ({current_pos['il_pct']:.2f}%)")
        log(f"  Ins sellback:   {sb:>12,.2f} USDC")
        if sc_sell > 0:
            log(f"  Spread (sell):  {sc_sell:>12,.2f} USDC")
        if sl_sell > 0:
            log(f"  Slippage (sell):{sl_sell:>12,.2f} USDC")
        log(f"  Ins net:        {current_pos['insurance_net']:>12,.2f} USDC  (cost={current_pos['insurance_cost']:,.2f} pay=0 sell={sb:,.2f})")
        log(f"  Gas fee (close):{gc:>12,.2f} USDC")
        log(f"  Wallet after:   {wa['usdc']:>12,.2f} USDC  {wa['eth']:>12.6f} ETH  (=${wa['value_usd']:,.0f})")
        if telemetry is not None:
            telemetry.emit(
                "position_close",
                int(candles[-1]["periodStartUnix"]),
                payload={
                    "reason": "period_end",
                    "close_policy": current_pos.get("close_policy", "touch"),
                    "close_price": current_pos.get("close_price"),
                    "close_price_source": current_pos.get("close_price_source"),
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
                    "slippage_cost_sell_usdc": current_pos.get("slippage_cost_sell", 0.0),
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
                "baseline_hodl_value_usd": round(baseline_initial_usdc + baseline_initial_eth * final_price, 2),
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
    data_quality: Optional[Dict] = None,
    run_metadata: Optional[Dict] = None,
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
    total_slippage_cost = sum(p.get("slippage_cost_buy", 0) + p.get("slippage_cost_sell", 0) for p in positions)

    boundary_closes = sum(1 for p in positions if p.get("touched_lower") or p.get("touched_upper"))
    lower_touches = sum(1 for p in positions if p.get("touched_lower"))
    upper_touches = sum(1 for p in positions if p.get("touched_upper"))
    hours_in_position = sum(p["duration_hours"] for p in positions)
    utilization_pct = (hours_in_position / total_hours * 100) if total_hours > 0 else 0

    # Initial token quantities: derive from the first position's wallet_before.
    if positions:
        first_wb = positions[0].get("wallet_before", {})
        initial_usdc = float(first_wb.get("usdc", 0.0))
        initial_eth = float(first_wb.get("eth", 0.0))
        investment = float(first_wb.get("value_usd", investment))
    else:
        # No positions opened -> cannot derive initial notional; keep zeros.
        initial_usdc = 0.0
        initial_eth = 0.0
        investment = float(investment or 0.0)

    final_value = final_wallet["usdc"] + final_wallet["eth"] * final_price

    polymarket_proceeds = total_ins_payout + total_ins_sellback
    cost_basis = investment + total_gas_fees + total_ins_cost
    final_total_value = final_value + polymarket_proceeds

    roi_pct = (final_total_value / cost_basis - 1) * 100 if cost_basis else 0.0
    try:
        apy = ((final_total_value / cost_basis) ** (365 / total_days) - 1) * 100 if total_days > 0 and final_total_value > 0 and cost_basis > 0 else 0
    except OverflowError:
        apy = float("inf") if final_total_value > cost_basis else float("-inf")

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
            "close_reason": p.get("close_reason"),
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
            "slippage_cost_buy_usdc": round(p.get("slippage_cost_buy", 0), 2),
            "slippage_cost_sell_usdc": round(p.get("slippage_cost_sell", 0), 2),
            "insurance_cost_usdc": round(p["insurance_cost"], 2),
            "insurance_payout_usdc": round(p["insurance_payout"], 2),
            "insurance_sellback_usdc": round(p.get("insurance_sellback", 0), 2),
            "insurance_net_usdc": round(p["insurance_net"], 2),
            "touched_lower": p.get("touched_lower", False),
            "touched_upper": p.get("touched_upper", False),
        })

    # Reconstruct an unhedged variant PnL from the existing position records.
    # Every position carries the idiosyncratic insurance cost/payout/sellback
    # and spread_cost_buy/sell — subtract them out to show what the same series
    # of LP positions would have earned with *no* Polymarket hedge. This is the
    # "does the hedge pay for itself?" baseline the guide asks for.
    unhedged_strategy_value = final_value
    hedge_net_contribution_usd = 0.0
    if positions:
        # Hedge is tracked outside the LP wallet; the LP-only series is already "unhedged".
        hedge_net_contribution_usd = (total_ins_payout + total_ins_sellback) - total_ins_cost
        unhedged_strategy_value = final_value

    unhedged_cost_basis = investment + total_gas_fees
    unhedged_roi_pct = (unhedged_strategy_value / unhedged_cost_basis - 1) * 100 if unhedged_cost_basis else 0
    try:
        unhedged_apy = (
            ((unhedged_strategy_value / unhedged_cost_basis) ** (365 / total_days) - 1) * 100
            if total_days > 0 and unhedged_strategy_value > 0 else 0
        )
    except OverflowError:
        unhedged_apy = float("inf") if unhedged_strategy_value > unhedged_cost_basis else float("-inf")

    hodl_final_value = initial_usdc + initial_eth * final_price
    hodl_roi_pct = (hodl_final_value / investment - 1) * 100 if investment else 0
    try:
        hodl_apy = ((hodl_final_value / investment) ** (365 / total_days) - 1) * 100 if total_days > 0 and hodl_final_value > 0 else 0
    except OverflowError:
        hodl_apy = float("inf") if hodl_final_value > investment else float("-inf")

    # Polymarket execution: what we paid/received at DB mid vs with execution costs.
    buy_premium_usd = sum(p.get("spread_cost_buy", 0.0) + p.get("slippage_cost_buy", 0.0) for p in positions)
    sell_premium_usd = sum(p.get("spread_cost_sell", 0.0) + p.get("slippage_cost_sell", 0.0) for p in positions)
    total_exec_drag_usd = buy_premium_usd + sell_premium_usd
    mid_buy_cost_usd = total_ins_cost - buy_premium_usd
    mid_sellback_usd = total_ins_sellback + sell_premium_usd

    # Counterfactual: if insurance buys and sellbacks were done at mid (no spread/slippage).
    # This is an *approx* post-hoc adjustment: it does not re-simulate secondary effects.
    cf_mid_exec_final_value = final_value + total_exec_drag_usd
    cf_mid_exec_roi_pct = (cf_mid_exec_final_value / investment - 1) * 100 if investment else 0.0
    try:
        cf_mid_exec_apy = (
            ((cf_mid_exec_final_value / investment) ** (365 / total_days) - 1) * 100
            if total_days > 0 and cf_mid_exec_final_value > 0 and investment > 0 else 0.0
        )
    except OverflowError:
        cf_mid_exec_apy = float("inf") if cf_mid_exec_final_value > investment else float("-inf")

    # Delta-matched HODL baseline: hold the first position's deposit token split.
    delta_matched = None
    dm_final_value_raw: Optional[float] = None
    if positions:
        first = positions[0]
        dm_usdc = float(first.get("token0_dep", 0.0))
        dm_eth = float(first.get("token1_dep", 0.0))
        dm_final_value_raw = dm_usdc + dm_eth * final_price
        dm_roi_pct = (dm_final_value_raw / investment - 1) * 100 if investment else 0
        try:
            dm_apy = ((dm_final_value_raw / investment) ** (365 / total_days) - 1) * 100 if total_days > 0 and dm_final_value_raw > 0 else 0
        except OverflowError:
            dm_apy = float("inf") if dm_final_value_raw > investment else float("-inf")
        delta_matched = {
            "initial_tokens": {t0_sym: round(dm_usdc, 2), t1_sym: round(dm_eth, 6)},
            "final_value_usd": round(dm_final_value_raw, 2),
            "roi_pct": round(dm_roi_pct, 2),
            "apy": round(dm_apy, 2),
        }
    edge_lp_plus_hedge_usd = (final_value - dm_final_value_raw) if dm_final_value_raw is not None else None
    edge_lp_plus_hedge_pct = ((edge_lp_plus_hedge_usd / dm_final_value_raw) * 100.0) if dm_final_value_raw else None

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
            "total_slippage_cost_usdc": round(total_slippage_cost, 2),
            "total_insurance_cost_usdc": round(total_ins_cost, 2), "total_insurance_payout_usdc": round(total_ins_payout, 2),
            "total_insurance_sellback_usdc": round(total_ins_sellback, 2), "total_insurance_net_usdc": round(total_ins_net, 2),
            "lp_final_value_usd": round(final_value, 2),
            "final_value_usd": round(final_total_value, 2),
            "cost_basis_usd": round(cost_basis, 2),
            "external_costs": True,
            "roi_pct": round(roi_pct, 2),
            "apy": round(apy, 2),
        },
        "external_cashflows": {
            "enabled": True,
            "gas_total_usdc": round(total_gas_fees, 2),
            "polymarket_buy_usdc": round(total_ins_cost, 2),
            "polymarket_proceeds_usdc": round(polymarket_proceeds, 2),
            "polymarket_net_usdc": round(polymarket_proceeds - total_ins_cost, 2),
        },
        "baselines": {
            "hodl": {
                "final_value_usd": round(hodl_final_value, 2),
                "roi_pct": round(hodl_roi_pct, 2),
                "apy": round(hodl_apy, 2),
                "outperformance_vs_hodl_usd": round(final_value - hodl_final_value, 2),
                "outperformance_vs_hodl_pct": round(roi_pct - hodl_roi_pct, 2),
            },
            "unhedged_active_lp": {
                "final_value_usd": round(unhedged_strategy_value, 2),
                "roi_pct": round(unhedged_roi_pct, 2),
                "apy": round(unhedged_apy, 2),
                "hedge_net_contribution_usd": round(hedge_net_contribution_usd, 2),
                "note": (
                    "Reconstructed: strategy PnL minus insurance cost/payout/"
                    "sellback and Polymarket execution costs (spread+slippage)."
                    " Isolates the hedge's net contribution without rerunning"
                    " the simulation."
                ),
            },
            **({"delta_matched_hodl": delta_matched} if delta_matched is not None else {}),
        },
        "polymarket_execution": {
            "mid_price_counterfactual": {
                "insurance_buy_cost_usd": round(mid_buy_cost_usd, 2),
                "insurance_sellback_usd": round(mid_sellback_usd, 2),
                "exec_premium_buy_usd": round(buy_premium_usd, 2),
                "exec_premium_sell_usd": round(sell_premium_usd, 2),
                "exec_drag_total_usd": round(total_exec_drag_usd, 2),
                "note": (
                    "Derived from position-level spread_cost_* and slippage_cost_*."
                    " 'mid' is the recorded historical bet mid from DB."
                ),
            },
            "actual": {
                "insurance_buy_cost_usd": round(total_ins_cost, 2),
                "insurance_sellback_usd": round(total_ins_sellback, 2),
            },
        },
        "counterfactuals": {
            "db_mid_execution": {
                "final_value_usd": round(cf_mid_exec_final_value, 2),
                "roi_pct": round(cf_mid_exec_roi_pct, 2),
                "apy": round(cf_mid_exec_apy, 2),
                "delta_vs_actual": {
                    "final_value_usd": round(cf_mid_exec_final_value - final_value, 2),
                    "roi_pct": round(cf_mid_exec_roi_pct - roi_pct, 2),
                    "apy": round(cf_mid_exec_apy - apy, 2),
                },
                "note": "Approx: adds back spread+slippage execution drag; does not re-simulate.",
            }
        },
        "edge_lp_plus_hedge": (
            {"usd": round(edge_lp_plus_hedge_usd, 2), "pct": round(edge_lp_plus_hedge_pct, 2)}
            if edge_lp_plus_hedge_usd is not None and edge_lp_plus_hedge_pct is not None
            else {}
        ),
        "data_quality": data_quality or {},
        "run_metadata": run_metadata or {},
        "positions": pos_records,
        "snapshots": snapshots or [],
    }


def run_sweep(
    candles: List[Dict],
    pool_data: Dict,
    token_symbol: str,
    conn,
    *,
    initial_eth: float,
    initial_usdc: Optional[float] = None,
    price_token: int = 0,
    cooldown_hours: int = 1,
    gas_prices: Optional[Dict[str, int]] = None,
    spread: float = 0.0,
    slippage_cfg: Optional[SlippageConfig] = None,
) -> List[Dict]:
    """Run simulation for every valid Polymarket range combo, return ranked results."""
    get_range_combinations = _get_db_func("get_range_combinations")
    first_ts = int(candles[0]["periodStartUnix"])
    # Use markets valid at the backtest start timestamp (not only today's active markets).
    all_combos = get_range_combinations(token_symbol, conn, candle_ts=first_ts)
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
        # Skip ranges that do not bracket the entry price (a V3 position cannot
        # be opened when price is outside the range). The previous ``and`` here
        # made this filter vacuous — it could never be true since ``mn < mx``.
        if mn >= first_price or mx <= first_price:
            continue
        if mx <= mn:
            continue
        unique_ranges.append((mn, mx))

    logger.info(f"Sweeping {len(unique_ranges)} range combinations...")
    results = []
    sweep_errors = 0

    for idx, (mn, mx) in enumerate(unique_ranges):
        try:
            sl_per_1k = slippage_cfg.per_1k_contracts if slippage_cfg is not None else 0.0
            sl_max = slippage_cfg.max_per_contract if slippage_cfg is not None else 0.0
            positions, final_wallet, _snaps = simulate(
                candles, pool_data, token_symbol, 0.0, conn,
                price_token=price_token, cooldown_hours=cooldown_hours,
                fixed_range=(mn, mx), quiet=True,
                gas_prices=gas_prices, spread=spread,
                slippage_per_1k_contracts=sl_per_1k,
                slippage_max_per_contract=sl_max,
                initial_eth=initial_eth,
                initial_usdc=initial_usdc,
            )
        except Exception as exc:
            sweep_errors += 1
            if sweep_errors <= 5:
                logger.warning("Sweep simulate failed for range [%.2f, %.2f]: %s", mn, mx, exc)
            continue

        if not positions:
            continue

        first_wb = positions[0].get("wallet_before", {})
        inv_usd = float(first_wb.get("value_usd", 0.0))
        init_usdc = float(first_wb.get("usdc", 0.0))
        init_eth = float(first_wb.get("eth", 0.0))

        final_value = final_wallet["usdc"] + final_wallet["eth"] * final_price
        total_gas_fees = sum(p.get("gas_fee_open", 0.0) + p.get("gas_fee_close", 0.0) for p in positions)
        polymarket_proceeds = sum(p.get("insurance_payout", 0.0) + p.get("insurance_sellback", 0.0) for p in positions)
        cost_basis = inv_usd + total_gas_fees + sum(p.get("insurance_cost", 0.0) for p in positions)
        final_total_value = final_value + polymarket_proceeds
        roi_pct = (final_total_value / cost_basis - 1) * 100 if cost_basis else 0.0
        apy = ((final_total_value / cost_basis) ** (365 / total_days) - 1) * 100 if total_days > 0 and final_total_value > 0 and cost_basis > 0 else 0
        total_fees_usdc = sum(p["fees_earned_usdc"] for p in positions)
        total_fees_eth = sum(p["fees_earned_eth"] for p in positions)
        total_fees_usd = sum(p["fees_earned_usd"] for p in positions)
        total_il = sum(p["il"] for p in positions)
        total_ins_cost = sum(p["insurance_cost"] for p in positions)
        total_ins_payout = sum(p["insurance_payout"] for p in positions)
        total_ins_sellback = sum(p.get("insurance_sellback", 0) for p in positions)
        boundary_closes = sum(1 for p in positions if p.get("touched_lower") or p.get("touched_upper"))

        delta_usdc = final_wallet["usdc"] - init_usdc
        delta_eth = final_wallet["eth"] - init_eth

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
                "lp_value_usd": round(final_value, 2),
                "final_total_value_usd": round(final_total_value, 2),
                "cost_basis_usd": round(cost_basis, 2),
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
    if sweep_errors:
        logger.warning("Sweep skipped %d ranges due to simulate errors.", sweep_errors)
    return results


def _require(cfg: Dict, key: str):
    if key not in cfg:
        raise KeyError(f'Missing required config key: "{key}"')
    return cfg[key]


def main():
    # Config-driven entrypoint (no CLI flags).
    from config_loader import load_config, get_section

    cfg = load_config()
    bt = get_section(cfg, "backtest")

    pool = str(_require(bt, "pool"))
    days = int(_require(bt, "days"))
    lookback_days = int(bt.get("lookback_days", 0) or 0)
    initial_eth = float(_require(bt, "initial_eth"))
    initial_usdc = bt.get("initial_usdc", None)
    cooldown_hours = int(bt.get("cooldown_hours", 1) or 0)
    price_token = int(bt.get("price_token", 0) or 0)
    fixed_range_cfg = bt.get("fixed_range", None)
    sweep = bool(bt.get("sweep", False))
    spread = float(bt.get("spread", 0.04) or 0.0)
    slippage_per_1k = float(bt.get("slippage_per_1k_contracts", 0.0) or 0.0)
    slippage_max = float(bt.get("slippage_max_per_contract", 0.0) or 0.0)
    close_policy: ClosePolicy = str(bt.get("close_policy", "touch"))  # type: ignore[assignment]
    # External-cost accounting is always ON (no config flag).

    telemetry_cfg = bt.get("telemetry") or {}
    telemetry_enabled = bool(telemetry_cfg.get("enabled", False))
    telemetry_path = telemetry_cfg.get("path")

    output_path = str(bt.get("output_json", "active_backtest_results.json"))

    get_db_connection = _get_db_func("get_db_connection")

    logger.info(f"Fetching pool metadata for {pool}...")
    pool_data = fetch_pool_metadata(pool)
    token_symbol = _map_wrapped_symbol(pool_data["token1"]["symbol"])
    logger.info(f"Pool: {pool_data['token0']['symbol']}/{pool_data['token1']['symbol']} -> token={token_symbol}")

    now = datetime.now(timezone.utc)
    end_ts = int(now.timestamp())
    total_fetch_days = days + lookback_days
    start_ts = int((now - timedelta(days=total_fetch_days)).timestamp())

    start_date_str = (now - timedelta(days=total_fetch_days)).strftime("%Y-%m-%d")
    end_date_str = now.strftime("%Y-%m-%d")
    logger.info("Fetching historical gas prices via RPC block sampling...")
    gas_prices = fetch_daily_gas_prices(start_date_str, end_date_str)

    logger.info(f"Fetching {total_fetch_days} days of hourly candles ({days}d backtest + {lookback_days}d lookback)...")
    all_candles = fetch_hourly_candles(pool, start_ts, end_ts)
    logger.info(f"Fetched {len(all_candles)} hourly candles")

    if len(all_candles) < 10:
        logger.error("Too few candles fetched, aborting")
        sys.exit(1)

    candle_report = validate_candles(all_candles)
    if not candle_report.is_clean:
        logger.warning(
            "Candle feed quality issues: missing=%dh, duplicates=%d, out-of-order=%d, "
            "bad-close=%d, bad-hl=%d, feeGrowth-non-monotonic=%d (gaps=%d segments)",
            candle_report.missing_hours,
            candle_report.duplicate_ts,
            candle_report.out_of_order,
            candle_report.non_positive_close,
            candle_report.non_positive_hl,
            candle_report.fee_growth_non_monotonic,
            len(candle_report.gap_segments),
        )
    else:
        logger.info("Candle feed: %d candles, no gaps or anomalies detected.", candle_report.candle_count)

    gas_report = validate_gas_coverage(start_date_str, end_date_str, gas_prices)
    if gas_report.coverage_pct < 100.0:
        logger.warning(
            "Gas price coverage: %.0f%% (%d/%d days). Missing dates will see $0 gas cost.",
            gas_report.coverage_pct, gas_report.covered_days, gas_report.requested_days,
        )

    lookback_hours = lookback_days * 24
    if lookback_days > 0 and len(all_candles) > lookback_hours:
        candles = all_candles[lookback_hours:]
        warmup_candles = all_candles[:lookback_hours]
        logger.info(f"Split: {len(warmup_candles)} warmup candles + {len(candles)} backtest candles")
    else:
        candles = all_candles
        warmup_candles = None

    fixed_range = None
    if fixed_range_cfg:
        if isinstance(fixed_range_cfg, str):
            parts = fixed_range_cfg.split(",")
            fixed_range = (float(parts[0]), float(parts[1]))
        elif isinstance(fixed_range_cfg, (list, tuple)) and len(fixed_range_cfg) == 2:
            fixed_range = (float(fixed_range_cfg[0]), float(fixed_range_cfg[1]))
        else:
            raise ValueError('backtest.fixed_range must be null, "min,max", or [min, max]')

    conn = get_db_connection()
    try:
        if sweep:
            results = run_sweep(
                candles, pool_data, token_symbol, conn,
                initial_eth=initial_eth,
                initial_usdc=float(initial_usdc) if initial_usdc is not None else None,
                price_token=price_token,
                cooldown_hours=cooldown_hours,
                gas_prices=gas_prices,
                spread=spread,
                slippage_cfg=SlippageConfig(per_1k_contracts=slippage_per_1k, max_per_contract=slippage_max),
            )

            sweep_out = (
                output_path[:-5] + "_sweep.json"
                if output_path.endswith(".json")
                else output_path + "_sweep.json"
            )
            with open(sweep_out, "w") as f:
                json.dump(results, f, indent=2)
            logger.info(f"\n{'='*120}")
            logger.info(f"SWEEP RESULTS — {len(results)} ranges tested ({days}d)")
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
            logger.info(f"\nFull sweep table saved to {sweep_out}")

            if not results:
                logger.error("Sweep produced no valid ranges; aborting report generation.")
                return

            best = results[0]["range"]
            logger.info(f"\nBest range by APY: [{best[0]:.0f}, {best[1]:.0f}] — running full summary...")

            positions, final_wallet, hourly_snapshots = simulate(
                candles, pool_data, token_symbol, 0.0, conn,
                price_token=price_token, cooldown_hours=cooldown_hours,
                fixed_range=(float(best[0]), float(best[1])),
                warmup_candles=warmup_candles,
                all_candles=all_candles if warmup_candles else None,
                gas_prices=gas_prices,
                spread=spread,
                slippage_per_1k_contracts=slippage_per_1k,
                slippage_max_per_contract=slippage_max,
                close_policy=close_policy,
                telemetry=None,
                initial_eth=initial_eth,
                initial_usdc=float(initial_usdc) if initial_usdc is not None else None,
            )

            poly_report = validate_polymarket_coverage(hourly_snapshots)
            data_quality = {
                "candles": candle_report.to_dict(),
                "gas": gas_report.to_dict(),
                "polymarket": poly_report.to_dict(),
            }

            run_metadata = {
                "pool_id": pool,
                "days": days,
                "lookback_days": lookback_days,
                "cooldown_hours": cooldown_hours,
                "price_token": price_token,
                "fixed_range": [float(best[0]), float(best[1])],
                "spread": spread,
                "slippage_per_1k_contracts": slippage_per_1k,
                "slippage_max_per_contract": slippage_max,
                "close_policy": close_policy,
                "selection_mode": "sweep",
                "capital_model": "eth_first",
                "initial_eth": float(initial_eth),
                "initial_usdc": float(initial_usdc) if initial_usdc is not None else None,
                "external_costs": True,
                "incomplete": False,
                "warnings": [],
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "sweep_table_path": sweep_out,
            }
            summary = build_summary(
                positions, candles, 0.0, pool, token_symbol, final_wallet, price_token,
                snapshots=hourly_snapshots,
                data_quality=data_quality,
                run_metadata=run_metadata,
            )
            with open(output_path, "w") as f:
                json.dump(summary, f, indent=2)
            logger.info(f"\nBest-range summary saved to {output_path}")
        else:
            mode = "lookback sweep" if warmup_candles else ("fixed" if fixed_range else "heuristic")
            logger.info(f"Starting active strategy simulation (range selection: {mode})...")
            telemetry = None
            if telemetry_enabled and telemetry_path:
                rid = new_run_id()
                default_path = str(telemetry_path)
                # If a directory is provided, write a file inside it.
                if default_path.endswith("/") or os.path.isdir(default_path):
                    default_path = os.path.join(default_path, f"active_backtester_{rid}.jsonl")
                telemetry = TelemetrySink(path=default_path, run_id=rid, enabled=True)
            positions, final_wallet, hourly_snapshots = simulate(
                candles, pool_data, token_symbol, 0.0, conn,
                price_token=price_token, cooldown_hours=cooldown_hours,
                fixed_range=fixed_range,
                warmup_candles=warmup_candles,
                all_candles=all_candles if warmup_candles else None,
                gas_prices=gas_prices,
                spread=spread,
                slippage_per_1k_contracts=slippage_per_1k,
                slippage_max_per_contract=slippage_max,
                close_policy=close_policy,
                telemetry=telemetry,
                initial_eth=initial_eth,
                initial_usdc=float(initial_usdc) if initial_usdc is not None else None,
            )
            poly_report = validate_polymarket_coverage(hourly_snapshots)
            if poly_report.position_hours and (
                poly_report.lower_bid_coverage_pct < 80.0
                or poly_report.upper_bid_coverage_pct < 80.0
            ):
                logger.warning(
                    "Polymarket bid coverage while in-position: lower=%.0f%%, upper=%.0f%% "
                    "(insurance mark-to-market is sparse — treat equity curve with care).",
                    poly_report.lower_bid_coverage_pct,
                    poly_report.upper_bid_coverage_pct,
                )

            data_quality = {
                "candles": candle_report.to_dict(),
                "gas": gas_report.to_dict(),
                "polymarket": poly_report.to_dict(),
            }
            if telemetry is not None:
                telemetry.emit(
                    "data_quality",
                    int(candles[0]["periodStartUnix"]),
                    payload=data_quality,
                )
            warnings: List[Dict] = []
            incomplete = False
            if not candle_report.is_clean:
                incomplete = True
                warnings.append({
                    "type": "candles",
                    "severity": "warning",
                    "message": "Candle feed has gaps/anomalies; results are not strictly truthy.",
                    "details": candle_report.to_dict(),
                })
            if gas_report.coverage_pct < 100.0:
                warnings.append({
                    "type": "gas",
                    "severity": "warning",
                    "message": "Gas price coverage is incomplete; missing dates are treated as $0 gas.",
                    "details": gas_report.to_dict(),
                })
            if poly_report.position_hours and (
                poly_report.lower_bid_coverage_pct < 80.0
                or poly_report.upper_bid_coverage_pct < 80.0
            ):
                incomplete = True
                warnings.append({
                    "type": "polymarket",
                    "severity": "warning",
                    "message": "Polymarket historical bid coverage while in-position is sparse; hedge MTM is incomplete.",
                    "details": poly_report.to_dict(),
                })
            run_metadata = {
                "pool_id": pool,
                "days": days,
                "lookback_days": lookback_days,
                "cooldown_hours": cooldown_hours,
                "price_token": price_token,
                "fixed_range": list(fixed_range) if fixed_range else None,
                "spread": spread,
                "slippage_per_1k_contracts": slippage_per_1k,
                "slippage_max_per_contract": slippage_max,
                "close_policy": close_policy,
                "selection_mode": "lookback" if warmup_candles else ("fixed" if fixed_range else "heuristic"),
                "capital_model": "eth_first",
                "initial_eth": float(initial_eth),
                "initial_usdc": float(initial_usdc) if initial_usdc is not None else None,
                "external_costs": True,
                "incomplete": incomplete,
                "warnings": warnings,
                "generated_at": datetime.now(timezone.utc).isoformat(),
            }
            summary = build_summary(
                positions, candles, 0.0, pool, token_symbol, final_wallet, price_token,
                snapshots=hourly_snapshots,
                data_quality=data_quality,
                run_metadata=run_metadata,
            )
            with open(output_path, "w") as f:
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

            bl = summary.get("baselines", {})
            if bl:
                hodl = bl.get("hodl", {})
                unh = bl.get("unhedged_active_lp", {})
                logger.info(
                    f"HODL:      ${hodl.get('final_value_usd', 0):,.2f}  "
                    f"ROI {hodl.get('roi_pct', 0):.2f}%  "
                    f"(outperf {hodl.get('outperformance_vs_hodl_pct', 0):+.2f}% "
                    f"= ${hodl.get('outperformance_vs_hodl_usd', 0):+,.2f})"
                )
                logger.info(
                    f"Unhedged:  ${unh.get('final_value_usd', 0):,.2f}  "
                    f"ROI {unh.get('roi_pct', 0):.2f}%  "
                    f"hedge net ${unh.get('hedge_net_contribution_usd', 0):+,.2f}"
                )

            dq = summary.get("data_quality", {})
            if dq:
                c = dq.get("candles", {})
                g = dq.get("gas", {})
                p = dq.get("polymarket", {})
                logger.info(
                    f"Quality:   candles missing={c.get('missing_hours', 0)}h  "
                    f"gas={g.get('coverage_pct', 0):.0f}%  "
                    f"poly bids lower={p.get('lower_bid_coverage_pct', 0):.0f}% "
                    f"upper={p.get('upper_bid_coverage_pct', 0):.0f}%"
                )
            logger.info(f"\nResults saved to {output_path}")

    finally:
        conn.close()

"""Range filtering, scoring, and insurance lookup."""

import logging
import sys
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def _get_db_func(name):
    """Resolve db_utils function via the shim module so that
    @patch("active_backtester.<name>") intercepts calls correctly."""
    shim = sys.modules.get("active_backtester")
    if shim and hasattr(shim, name):
        return getattr(shim, name)
    import db_utils
    return getattr(db_utils, name)


def _map_wrapped_symbol(symbol: str) -> str:
    mapping = {"WETH": "ETH", "WBTC": "BTC", "WBNB": "BNB"}
    s = symbol.upper()
    if s.startswith("W") and len(s) > 1:
        s = s[1:]
    return mapping.get(symbol.upper(), s)


def _filter_ranges_for_price(
    combos: List[Dict],
    current_price: float,
    buffer_pct: float = 5.0,
    max_width_pct: float = 80.0,
) -> List[Dict]:
    """Keep ranges where price is inside, has adequate buffer, and isn't absurdly wide."""
    out = []
    seen = set()
    for c in combos:
        mn, mx = float(c["min"]), float(c["max"])
        key = (round(mn, 2), round(mx, 2))
        if key in seen:
            continue
        seen.add(key)
        if mn >= current_price or mx <= current_price:
            continue
        lower_dist = (current_price - mn) / current_price * 100
        upper_dist = (mx - current_price) / current_price * 100
        width_pct = (mx - mn) / current_price * 100
        if lower_dist >= buffer_pct and upper_dist >= buffer_pct and width_pct <= max_width_pct:
            out.append(c)
    return out


def _score_range(
    mn: float, mx: float,
    token_symbol: str,
    candle_ts: int,
    investment: float,
    conn,
) -> Optional[Dict]:
    """Score a range by estimating insurance cost at candle_ts."""
    get_clob_token_id = _get_db_func("get_clob_token_id")
    get_historical_bet_price = _get_db_func("get_historical_bet_price")

    # IMPORTANT: Markets rotate. Always resolve the CLOB token IDs "as-of" candle_ts,
    # otherwise historical bet price lookups may return None and block re-entry.
    lower_clob = get_clob_token_id(token_symbol, mn, "down", "Yes", conn, candle_ts=candle_ts)
    upper_clob = get_clob_token_id(token_symbol, mx, "up", "Yes", conn, candle_ts=candle_ts)

    lower_bet = get_historical_bet_price(lower_clob, candle_ts, conn) if lower_clob else None
    upper_bet = get_historical_bet_price(upper_clob, candle_ts, conn) if upper_clob else None

    if lower_bet is None and upper_bet is None:
        return None

    lower_bet = lower_bet if lower_bet is not None else 0.5
    upper_bet = upper_bet if upper_bet is not None else 0.5

    insurance_cost_rate = lower_bet + upper_bet

    return {
        "min": mn,
        "max": mx,
        "lower_bet_price": lower_bet,
        "upper_bet_price": upper_bet,
        "insurance_cost_rate": insurance_cost_rate,
        "range_width_pct": (mx - mn) / ((mn + mx) / 2) * 100,
    }


def pick_best_range(
    combos: List[Dict],
    current_price: float,
    token_symbol: str,
    candle_ts: int,
    investment: float,
    conn,
) -> Optional[Dict]:
    """
    Pick the best range to open right now.

    The active strategy benefits from tighter ranges (more fees) as long as
    insurance is affordable. Score = narrowness bonus minus insurance cost.
    """
    candidates = _filter_ranges_for_price(combos, current_price)
    if not candidates:
        candidates = _filter_ranges_for_price(combos, current_price, buffer_pct=2.0)
    if not candidates:
        return None

    shim = sys.modules.get("active_backtester")
    score_fn = getattr(shim, "_score_range", _score_range) if shim else _score_range

    scored = []
    for c in candidates:
        s = score_fn(float(c["min"]), float(c["max"]), token_symbol, candle_ts, investment, conn)
        if s:
            scored.append(s)

    if not scored:
        return None

    # Hard cap: don't choose ranges where either side has >10% implied probability.
    # We interpret Polymarket YES price as the probability proxy.
    cap = 0.20
    scored = [
        s for s in scored
        if float(s.get("lower_bet_price", 1.0)) <= cap and float(s.get("upper_bet_price", 1.0)) <= cap
    ]
    if not scored:
        return None

    max_width = max(s["range_width_pct"] for s in scored) or 1.0
    for s in scored:
        narrowness = 1.0 - (s["range_width_pct"] / max_width)
        s["score"] = narrowness - s["insurance_cost_rate"]

    scored.sort(key=lambda s: s["score"], reverse=True)
    return scored[0]


def _get_insurance_for_range(
    mn: float, mx: float, token_symbol: str, candle_ts: int, conn,
) -> Optional[Dict]:
    """Fetch historical bet prices for a specific range."""
    get_clob_token_id = _get_db_func("get_clob_token_id")
    get_historical_bet_price = _get_db_func("get_historical_bet_price")

    lower_clob = get_clob_token_id(token_symbol, mn, "down", "Yes", conn, candle_ts=candle_ts)
    upper_clob = get_clob_token_id(token_symbol, mx, "up", "Yes", conn, candle_ts=candle_ts)
    lower_bet = get_historical_bet_price(lower_clob, candle_ts, conn) if lower_clob else None
    upper_bet = get_historical_bet_price(upper_clob, candle_ts, conn) if upper_clob else None
    if lower_bet is None and upper_bet is None:
        return None
    return {
        "lower_bet_price": lower_bet if lower_bet is not None else 0.5,
        "upper_bet_price": upper_bet if upper_bet is not None else 0.5,
    }


def pick_best_range_by_sweep(
    lookback_candles: List[Dict],
    pool_data: Dict,
    all_combos: List[Dict],
    current_price: float,
    token_symbol: str,
    investment: float,
    conn,
    price_token: int,
    cooldown_hours: int,
    candle_ts: int,
    simulate_fn: Callable,
) -> Optional[Dict]:
    """Pick the best range by running simulate over lookback candles for each candidate.

    Ranks by **insurance efficiency**: sum(payout + sellback) - sum(cost).
    Falls back to the heuristic ``pick_best_range`` if no candidate survives.
    """
    candidates = _filter_ranges_for_price(all_combos, current_price)
    if not candidates:
        candidates = _filter_ranges_for_price(all_combos, current_price, buffer_pct=2.0)
    if not candidates:
        return None

    seen: set = set()
    unique: List[Tuple[float, float]] = []
    for c in candidates:
        mn, mx = float(c["min"]), float(c["max"])
        key = (round(mn, 2), round(mx, 2))
        if key not in seen:
            seen.add(key)
            unique.append((mn, mx))

    lookback_days = len(lookback_candles) / 24
    logger.info(f"  Lookback sweep: {len(unique)} candidates over {lookback_days:.0f}d of candles...")

    scored: List[Dict] = []
    for idx, (mn, mx) in enumerate(unique):
        # Enforce the same 10% per-side cap based on Polymarket YES price at candle_ts.
        ins_now = _get_insurance_for_range(mn, mx, token_symbol, candle_ts, conn)
        if ins_now is None:
            continue
        if float(ins_now.get("lower_bet_price", 1.0)) > 0.20 or float(ins_now.get("upper_bet_price", 1.0)) > 0.20:
            continue
        try:
            positions, _, _snaps = simulate_fn(
                lookback_candles, pool_data, token_symbol, investment, conn,
                price_token=price_token, cooldown_hours=cooldown_hours,
                fixed_range=(mn, mx), quiet=True,
            )
        except Exception:
            continue

        if not positions:
            continue

        total_cost = sum(p["insurance_cost"] for p in positions)
        total_payout = sum(p["insurance_payout"] for p in positions)
        total_sellback = sum(p.get("insurance_sellback", 0) for p in positions)
        efficiency = total_payout + total_sellback - total_cost

        scored.append({
            "min": mn,
            "max": mx,
            "insurance_efficiency": round(efficiency, 2),
            "positions_in_lookback": len(positions),
            "total_cost": round(total_cost, 2),
            "total_payout": round(total_payout, 2),
            "total_sellback": round(total_sellback, 2),
        })

    logger.info(f"  Lookback sweep done: {len(scored)}/{len(unique)} ranges scored")

    if not scored:
        logger.debug("Lookback sweep found no viable ranges, falling back to heuristic")
        return pick_best_range(all_combos, current_price, token_symbol, candle_ts, investment, conn)

    scored.sort(key=lambda s: s["insurance_efficiency"], reverse=True)
    best = scored[0]
    logger.info(
        f"  Sweep winner: [{best['min']:.0f}, {best['max']:.0f}] "
        f"efficiency={best['insurance_efficiency']:+,.0f} "
        f"(cost={best['total_cost']:,.0f} pay={best['total_payout']:,.0f} sell={best['total_sellback']:,.0f})"
    )

    insurance_info = _get_insurance_for_range(best["min"], best["max"], token_symbol, candle_ts, conn)
    if insurance_info is None:
        logger.debug("Best sweep range has no current insurance, falling back to heuristic")
        return pick_best_range(all_combos, current_price, token_symbol, candle_ts, investment, conn)

    return {
        "min": best["min"],
        "max": best["max"],
        "lower_bet_price": insurance_info["lower_bet_price"],
        "upper_bet_price": insurance_info["upper_bet_price"],
        "sweep_score": best["insurance_efficiency"],
        "sweep_positions": best["positions_in_lookback"],
        "sweep_cost": best["total_cost"],
        "sweep_payout": best["total_payout"],
        "sweep_sellback": best["total_sellback"],
    }

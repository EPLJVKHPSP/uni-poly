"""Range filtering, scoring, and insurance lookup."""

import logging
import sys
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# Default values for the strategy knobs introduced for the experiment matrix.
# Pre-existing behaviour is recovered when the caller passes selection_cfg=None
# (or omits these keys), so all changes here are non-breaking.
_DEFAULT_YES_CAP: float = 0.20
_DEFAULT_OBJECTIVE: str = "narrowness_minus_premium"


def _get_db_func(name):
    """Resolve db_utils function via the shim module so that
    @patch("active_backtester.<name>") intercepts calls correctly."""
    shim = sys.modules.get("active_backtester")
    if shim and hasattr(shim, name):
        return getattr(shim, name)
    import db_utils
    return getattr(db_utils, name)


def _end_date_to_ts(end_date: Any) -> Optional[int]:
    """Coerce a market end_date (datetime or ISO string) to a unix timestamp.
    Returns None when conversion is not possible or end_date is missing."""
    if end_date is None:
        return None
    if isinstance(end_date, datetime):
        if end_date.tzinfo is None:
            end_date = end_date.replace(tzinfo=timezone.utc)
        return int(end_date.timestamp())
    if isinstance(end_date, str):
        try:
            dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except Exception:
            return None
    return None


def _hedge_tte_hours(
    end_ts_lower: Optional[int],
    end_ts_upper: Optional[int],
    candle_ts: Optional[int],
) -> Optional[float]:
    """Time-to-expiry of the hedge (the earlier of the two leg end times).
    Returns hours, or None if both ends are unknown / candle_ts is None."""
    if candle_ts is None:
        return None
    candidates = [t for t in (end_ts_lower, end_ts_upper) if t is not None]
    if not candidates:
        return None
    earliest = min(candidates)
    return max(0.0, (earliest - int(candle_ts)) / 3600.0)


_STABLE_SYMBOLS = {"USDC", "USDT", "DAI", "USDE", "TUSD", "USDP", "FRAX", "GUSD", "BUSD", "USDM", "USDS"}


def _is_stable(symbol: str) -> bool:
    return (symbol or "").upper() in _STABLE_SYMBOLS


def _map_wrapped_symbol(symbol: str) -> str:
    mapping = {"WETH": "ETH", "WBTC": "BTC", "WBNB": "BNB", "WSOL": "SOL"}
    s = (symbol or "").upper()
    if s.startswith("W") and len(s) > 1:
        s = s[1:]
    return mapping.get((symbol or "").upper(), s)


def detect_pool_orientation(pool_data: Dict) -> Tuple[str, int]:
    """Return (volatile_token_symbol, price_token_index) for a Uniswap pool.

    The simulator's math is written assuming price = "USD per volatile asset".
    Different pools list the volatile asset as either ``token0`` (e.g. WBTC/USDC)
    or ``token1`` (e.g. USDC/WETH), so we inspect the symbols and pick the
    ``price_token`` index that yields the right convention.

    - If ``token1`` is a stablecoin, the raw close = stable/volatile; we need
      to invert it -> ``price_token = 1`` and the volatile symbol is ``token0``.
    - Otherwise we treat ``token1`` as the volatile asset (the legacy default)
      with ``price_token = 0``.
    """
    t0 = (pool_data.get("token0", {}) or {}).get("symbol", "") or ""
    t1 = (pool_data.get("token1", {}) or {}).get("symbol", "") or ""
    if _is_stable(t1) and not _is_stable(t0):
        return _map_wrapped_symbol(t0), 1
    return _map_wrapped_symbol(t1), 0


def _filter_ranges_for_price(
    combos: List[Dict],
    current_price: float,
    buffer_pct: float = 5.0,
    max_width_pct: float = 80.0,
) -> List[Dict]:
    """Keep ranges where price is inside, has adequate buffer, and isn't absurdly wide.

    ``buffer_pct`` and ``max_width_pct`` may be overridden per call from
    ``pick_best_range`` via ``selection_cfg``.
    """
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


def _meta_unpack(meta):
    """Unpack ``get_clob_token_id_with_meta`` return value.

    Tolerates both the legacy 2-tuple ``(clob, end_date)`` and the new
    3-tuple ``(clob, end_date, market_volume)`` so we keep backwards
    compatibility with any pinned db_utils version.
    """
    if meta is None:
        return None, None, 0.0
    try:
        if len(meta) >= 3:
            return meta[0], meta[1], float(meta[2] or 0.0)
        return meta[0], meta[1], 0.0
    except Exception:
        return None, None, 0.0


def _pick_leg(
    token_symbol: str,
    level: float,
    direction: str,
    candle_ts: int,
    conn,
    *,
    yes_cap: Optional[float],
    restrict_to_touch_markets: bool,
    min_market_volume: float,
    top_k: int = 5,
) -> Tuple[Optional[str], Optional[float], Optional[Any], float]:
    """Pick the best Polymarket market for a single hedge leg.

    Strategy: enumerate the top-``top_k`` candidates ordered by depth
    (cumulative ``market_volume DESC``), look up each one's historical
    YES mid, and return the **deepest market whose YES mid is at or
    below ``yes_cap``**. If no candidate satisfies the cap, fall back
    to the deepest one with valid price data so the caller still has
    real numbers to score (the caller's own yes_cap re-check / relax
    path then decides what to do).

    Returns ``(clob_token_id, yes_mid, end_date, market_volume)``. Any
    field may be ``None``/``0.0`` when the leg has no market data at
    all, in which case the caller should treat the range as un-hedgable
    at this candle.
    """
    get_historical_bet_price = _get_db_func("get_historical_bet_price")
    get_candidate_markets = _get_db_func("get_candidate_markets")

    try:
        try:
            cands = get_candidate_markets(
                token_symbol, level, direction, "Yes", conn, candle_ts=candle_ts,
                restrict_to_touch_markets=restrict_to_touch_markets,
                min_market_volume=min_market_volume,
            ) or []
        except TypeError:
            cands = get_candidate_markets(
                token_symbol, level, direction, "Yes", conn, candle_ts=candle_ts,
            ) or []
    except Exception:
        cands = []

    if not cands:
        # Last-resort fallback: try the single-row helper (which itself
        # uses depth-first ordering) so we still return something when
        # the candidate list is empty due to the volume floor.
        try:
            get_clob_token_id_with_meta = _get_db_func("get_clob_token_id_with_meta")
            try:
                meta = get_clob_token_id_with_meta(
                    token_symbol, level, direction, "Yes", conn, candle_ts=candle_ts,
                    restrict_to_touch_markets=restrict_to_touch_markets,
                    min_market_volume=0.0,
                )
            except TypeError:
                meta = get_clob_token_id_with_meta(token_symbol, level, direction, "Yes", conn, candle_ts=candle_ts)
            clob, end, vol = _meta_unpack(meta)
            if clob is None:
                return (None, None, None, 0.0)
            mid = get_historical_bet_price(clob, candle_ts, conn)
            return (clob, mid, end, float(vol or 0.0))
        except Exception:
            return (None, None, None, 0.0)

    deepest_with_data: Optional[Tuple[str, float, Any, float]] = None
    best_within_cap: Optional[Tuple[str, float, Any, float]] = None
    cap = float(yes_cap) if (yes_cap is not None and float(yes_cap) < 1.0) else None
    for c in cands[:max(int(top_k), 1)]:
        clob = c.get("clob_token_id")
        end = c.get("end_date")
        vol = float(c.get("market_volume") or 0.0)
        if not clob:
            continue
        mid = get_historical_bet_price(clob, candle_ts, conn)
        if mid is None:
            continue
        mid_f = float(mid)
        if deepest_with_data is None:
            # cands is depth-DESC, so the first valid one is the deepest.
            deepest_with_data = (clob, mid_f, end, vol)
        if cap is None or mid_f <= cap:
            if best_within_cap is None or vol > best_within_cap[3]:
                best_within_cap = (clob, mid_f, end, vol)
            # depth-DESC means we can stop the moment we have the deepest cap-passer
            # (any later candidate would be shallower).
            if best_within_cap is not None:
                break

    if best_within_cap is not None:
        return best_within_cap
    if deepest_with_data is not None:
        return deepest_with_data
    return (None, None, None, 0.0)


def _score_range(
    mn: float, mx: float,
    token_symbol: str,
    candle_ts: int,
    investment: float,
    conn,
    restrict_to_touch_markets: bool = False,
    min_market_volume: float = 0.0,
    yes_cap: Optional[float] = None,
) -> Optional[Dict]:
    """Score a range by estimating insurance cost at candle_ts.

    Returns each leg's ``clob_token_id``, historical mid YES price,
    ``end_date`` (for TTE), and ``market_volume`` (depth proxy used by
    the cost-aware scorer and the per-asset slippage builder).

    Per-leg market choice is delegated to ``_pick_leg`` which selects
    the deepest market whose YES is at-or-below ``yes_cap`` (so we get
    the slippage benefit of depth without running afoul of the
    strategy's premium-per-leg cap). The ``restrict_to_touch_markets``
    and ``min_market_volume`` knobs are forwarded for geometric +
    ghost-market safety.
    """
    lower_clob, lower_bet, lower_end_raw, lower_vol = _pick_leg(
        token_symbol, mn, "down", candle_ts, conn,
        yes_cap=yes_cap,
        restrict_to_touch_markets=restrict_to_touch_markets,
        min_market_volume=min_market_volume,
    )
    upper_clob, upper_bet, upper_end_raw, upper_vol = _pick_leg(
        token_symbol, mx, "up", candle_ts, conn,
        yes_cap=yes_cap,
        restrict_to_touch_markets=restrict_to_touch_markets,
        min_market_volume=min_market_volume,
    )

    if lower_bet is None and upper_bet is None:
        return None

    lower_bet = float(lower_bet) if lower_bet is not None else 0.5
    upper_bet = float(upper_bet) if upper_bet is not None else 0.5
    lower_end = _end_date_to_ts(lower_end_raw)
    upper_end = _end_date_to_ts(upper_end_raw)

    insurance_cost_rate = lower_bet + upper_bet
    tte_hours = _hedge_tte_hours(lower_end, upper_end, candle_ts)

    return {
        "min": mn,
        "max": mx,
        "lower_bet_price": lower_bet,
        "upper_bet_price": upper_bet,
        "insurance_cost_rate": insurance_cost_rate,
        "range_width_pct": (mx - mn) / ((mn + mx) / 2) * 100,
        "lower_end_ts": lower_end,
        "upper_end_ts": upper_end,
        "tte_hours": tte_hours,
        "lower_clob_token_id": lower_clob,
        "upper_clob_token_id": upper_clob,
        "lower_market_volume": float(lower_vol or 0.0),
        "upper_market_volume": float(upper_vol or 0.0),
    }


def pick_best_range(
    combos: List[Dict],
    current_price: float,
    token_symbol: str,
    candle_ts: int,
    investment: float,
    conn,
    selection_cfg: Optional[Dict[str, Any]] = None,
) -> Optional[Dict]:
    """
    Pick the best range to open right now.

    The active strategy benefits from tighter ranges (more fees) as long as
    insurance is affordable. Default score = narrowness bonus minus insurance
    cost (legacy behaviour).

    ``selection_cfg`` (all keys optional) lets the experiment runner override:
      - ``range_yes_cap``        (float, default 0.20)         hard cap on YES price per leg
      - ``min_hedge_tte_hours``  (float|None, default None)    drop candidates with shorter TTE
      - ``selection_objective``  ("narrowness_minus_premium"
                                | "min_premium_per_day"
                                | "min_total_exec_cost")     cost-aware scorer
      - ``fixed_range_pct``      (float|None)                  pick the candidate closest
                                                               to ±N%/2 width around current price
      - ``restrict_to_touch_markets`` (bool, default False)    forwarded to leg lookups
      - ``min_market_volume``    (float, default 0)            drop ghost markets per-leg
      - ``slippage_per_1k_default`` (float, default 0.02)      for cost-aware scorer
      - ``hedge_size_usd_hint``  (float|None)                  expected per-leg notional
                                                               used by cost-aware scorer
    """
    cfg = selection_cfg or {}
    yes_cap = float(cfg.get("range_yes_cap", _DEFAULT_YES_CAP))
    min_tte_hours = cfg.get("min_hedge_tte_hours")
    objective = str(cfg.get("selection_objective", _DEFAULT_OBJECTIVE) or _DEFAULT_OBJECTIVE)
    fixed_width_pct = cfg.get("fixed_range_pct")
    buffer_pct = float(cfg.get("range_buffer_pct", 5.0))
    max_width_pct = float(cfg.get("range_max_width_pct", 80.0))
    bypass_insurance = bool(cfg.get("bypass_insurance", False))
    restrict_to_touch_markets = bool(cfg.get("restrict_to_touch_markets", False))
    min_market_volume = float(cfg.get("min_market_volume", 0.0) or 0.0)
    slip_default = float(cfg.get("slippage_per_1k_default", 0.02) or 0.0)
    hedge_size_hint = cfg.get("hedge_size_usd_hint")
    # When True, instead of returning None whenever no candidate satisfies the
    # YES cap or TTE filter, fall back to the closest-to-spec candidate
    # ("least bad"). Used by restore-to-anchor mode so the strategy doesn't
    # sit idle around month-boundaries when only the about-to-expire
    # Polymarket market exists.
    relax_when_empty = bool(cfg.get("relax_filters_when_empty", False))

    candidates = _filter_ranges_for_price(
        combos, current_price, buffer_pct=buffer_pct, max_width_pct=max_width_pct,
    )
    if not candidates and relax_when_empty:
        # Progressively relax the buffer all the way down to 0 so we never sit
        # idle when the caller asked for max utilisation. Each step also bumps
        # the max width cap in case the only available ranges are very wide.
        for relax_buf, relax_mw in (
            (max(2.0, buffer_pct / 2.0), max_width_pct),
            (1.0, max(max_width_pct, 100.0)),
            (0.5, max(max_width_pct, 150.0)),
            (0.0, max(max_width_pct, 200.0)),
        ):
            candidates = _filter_ranges_for_price(
                combos, current_price, buffer_pct=relax_buf, max_width_pct=relax_mw,
            )
            if candidates:
                break
    elif not candidates:
        # Legacy single-step relaxation.
        candidates = _filter_ranges_for_price(
            combos, current_price, buffer_pct=max(2.0, buffer_pct / 2.0),
            max_width_pct=max_width_pct,
        )
    if not candidates:
        return None

    # No-hedge mode: short-circuit before any DB lookups for insurance prices.
    # Picks the candidate whose width is closest to fixed_range_pct (if set)
    # or the narrowest one otherwise. Returns zeroed bet prices so downstream
    # open_position knows there's no hedge to buy.
    if bypass_insurance:
        target_w = None
        if fixed_width_pct is not None:
            try:
                target_w = float(fixed_width_pct)
            except (TypeError, ValueError):
                target_w = None
        scored_nh: List[Dict] = []
        for c in candidates:
            mn, mx = float(c["min"]), float(c["max"])
            scored_nh.append({
                "min": mn,
                "max": mx,
                "lower_bet_price": 0.0,
                "upper_bet_price": 0.0,
                "insurance_cost_rate": 0.0,
                "range_width_pct": (mx - mn) / ((mn + mx) / 2) * 100,
                "lower_end_ts": None,
                "upper_end_ts": None,
                "tte_hours": None,
            })
        if target_w is not None and target_w > 0:
            scored_nh.sort(key=lambda s: abs(s["range_width_pct"] - target_w))
        else:
            scored_nh.sort(key=lambda s: s["range_width_pct"])
        return scored_nh[0]

    shim = sys.modules.get("active_backtester")
    score_fn = getattr(shim, "_score_range", _score_range) if shim else _score_range

    scored = []
    for c in candidates:
        # New scorer signature accepts the touch / min-volume filters and
        # ``yes_cap`` so per-leg market selection can prefer the deepest
        # market that still satisfies the strategy's premium cap. Older
        # versions (e.g. test shims) may not — degrade transparently.
        try:
            s = score_fn(
                float(c["min"]), float(c["max"]),
                token_symbol, candle_ts, investment, conn,
                restrict_to_touch_markets=restrict_to_touch_markets,
                min_market_volume=min_market_volume,
                yes_cap=yes_cap,
            )
        except TypeError:
            try:
                s = score_fn(
                    float(c["min"]), float(c["max"]),
                    token_symbol, candle_ts, investment, conn,
                    restrict_to_touch_markets=restrict_to_touch_markets,
                    min_market_volume=min_market_volume,
                )
            except TypeError:
                s = score_fn(float(c["min"]), float(c["max"]), token_symbol, candle_ts, investment, conn)
        if s:
            scored.append(s)

    if not scored:
        return None

    # Hard cap on YES price per leg. Default 0.20 preserves legacy behaviour.
    # Set ``range_yes_cap >= 1.0`` (or e.g. 0.99) to effectively disable it.
    if yes_cap is not None and yes_cap < 1.0:
        passing = [
            s for s in scored
            if float(s.get("lower_bet_price", 1.0)) <= yes_cap
            and float(s.get("upper_bet_price", 1.0)) <= yes_cap
        ]
        if passing:
            scored = passing
        elif relax_when_empty:
            # No candidate satisfies the YES cap — keep all and let the
            # downstream scorer pick the cheapest. Strategy stays active.
            logger.debug(
                "pick_best_range: YES cap %.2f rejected all %d candidates "
                "@ ts=%s; falling back to cheapest available.",
                yes_cap, len(scored), candle_ts,
            )
        else:
            return None

    # Optional time-to-expiry filter — drop short-dated hedges so we don't
    # roll on calendar churn alone (E1/E2 in the experiment matrix).
    if min_tte_hours is not None:
        try:
            min_tte = float(min_tte_hours)
        except (TypeError, ValueError):
            min_tte = None
        if min_tte is not None and min_tte > 0:
            with_tte = [s for s in scored if s.get("tte_hours") is not None]
            if with_tte:
                filtered = [s for s in with_tte if float(s["tte_hours"]) >= min_tte]
                if filtered:
                    scored = filtered
                elif relax_when_empty:
                    # No candidate has long-enough TTE — pick the longest-
                    # lived one available so the strategy stays active.
                    with_tte.sort(key=lambda s: float(s["tte_hours"]), reverse=True)
                    longest = float(with_tte[0]["tte_hours"])
                    logger.debug(
                        "pick_best_range: min_tte=%.0fh rejected all %d candidates "
                        "@ ts=%s; falling back to longest TTE=%.0fh.",
                        min_tte, len(with_tte), candle_ts, longest,
                    )
                    scored = with_tte
                else:
                    # Nothing meets the bar at this candle — caller (simulate)
                    # will treat None as "no entry now" and try again next hour.
                    return None

    # Fixed-width preference (E6): pick the candidate whose width is closest
    # to ``fixed_width_pct`` (full width as a % of current price).  We still
    # apply the YES cap above to avoid degenerate at-the-money picks.
    if fixed_width_pct is not None:
        try:
            target_w = float(fixed_width_pct)
        except (TypeError, ValueError):
            target_w = None
        if target_w is not None and target_w > 0:
            for s in scored:
                s["_width_distance"] = abs(float(s["range_width_pct"]) - target_w)
            scored.sort(key=lambda s: (s["_width_distance"], s["insurance_cost_rate"]))
            return scored[0]

    # Scoring objective.
    if objective == "min_premium_per_day":
        # Penalise expensive premium relative to how long the hedge runs:
        # cheaper or longer-lived hedges score higher.  TTE is in hours, so
        # we divide by max(tte_hours, 1) to get a per-hour premium and
        # negate so that higher = better (consistent with the legacy path).
        for s in scored:
            tte = float(s.get("tte_hours") or 24.0)
            tte = max(tte, 1.0)
            s["score"] = -(float(s["insurance_cost_rate"]) / tte)
    elif objective == "min_total_exec_cost":
        # Cost-aware scorer: minimise expected NET hedge cost per dollar of
        # IL coverage, accounting for size-aware slippage AND for the fact
        # that longer-dated hedges recover most of their premium via
        # sellback at close.  See docstring for the formula.
        try:
            size_hint = float(hedge_size_hint) if hedge_size_hint is not None else float(investment) * 0.10
        except (TypeError, ValueError):
            size_hint = float(investment) * 0.10
        size_hint = max(size_hint, 1.0)
        for s in scored:
            # Per-leg expected EXEC cost = premium + spread + size-aware
            # slippage. Slippage = n * (per_1k * n / 1000), where per_1k is
            # ~ slip_default scaled by sqrt(reference_depth / market_volume).
            ref_depth = 100_000.0  # USD volume that matches slip_default
            def _per_1k(volume_usd: float) -> float:
                v = max(float(volume_usd or 0.0), 1.0)
                # Square-root law: doubling depth halves per_1k roughly.
                scaled = slip_default * (ref_depth / v) ** 0.5
                return max(min(scaled, 0.20), 0.005)
            l_per_1k = _per_1k(s.get("lower_market_volume", 0.0))
            u_per_1k = _per_1k(s.get("upper_market_volume", 0.0))
            # contracts ≈ size_hint / max(yes, 0.005) — at low YES the same
            # USD coverage requires far more contracts, which super-linearly
            # raises slippage. This is what makes the cheap-but-shallow
            # market actually expensive.
            l_yes = max(float(s.get("lower_bet_price", 0.5) or 0.5), 0.005)
            u_yes = max(float(s.get("upper_bet_price", 0.5) or 0.5), 0.005)
            l_contracts = size_hint / l_yes
            u_contracts = size_hint / u_yes
            # Spread is symmetric across markets in our model (config.spread),
            # but we still want it in the cost so the scorer compares
            # apples-to-apples; use 0.04 (legacy default).
            spread_default = float(cfg.get("spread", 0.04) or 0.0)
            l_spread_cost = l_contracts * (spread_default / 2.0)
            u_spread_cost = u_contracts * (spread_default / 2.0)
            l_slip_cost = l_contracts * (l_per_1k * (l_contracts / 1000.0))
            u_slip_cost = u_contracts * (u_per_1k * (u_contracts / 1000.0))
            l_premium = l_contracts * l_yes
            u_premium = u_contracts * u_yes
            # Sellback recovery: assume we close at ~1/2 of TTE on average
            # if untouched, recovering ~70% of remaining intrinsic premium.
            # Touch probability is approximated by YES (fair-game), so the
            # expected sellback per contract is (1 - YES) * 0.5 * YES * 0.7.
            # This is a coarse proxy but it captures the central effect:
            # longer/deeper markets refund a meaningful chunk of premium.
            def _expected_sellback(contracts: float, yes: float) -> float:
                untouched_p = max(0.0, 1.0 - yes)
                # sell at ~half of remaining mid; recovery factor 0.7 is
                # a haircut for spread + slippage on the sell side.
                return contracts * untouched_p * yes * 0.5 * 0.7
            l_sellback = _expected_sellback(l_contracts, l_yes)
            u_sellback = _expected_sellback(u_contracts, u_yes)
            l_net = l_premium + l_spread_cost + l_slip_cost - l_sellback
            u_net = u_premium + u_spread_cost + u_slip_cost - u_sellback
            net_cost = l_net + u_net
            # Normalise by coverage (size_hint) so wider/narrower ranges are
            # comparable; lower net cost / coverage == better.
            s["expected_net_hedge_cost_usd"] = net_cost
            s["score"] = -(net_cost / max(size_hint, 1.0))
    else:
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

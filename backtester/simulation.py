"""Simulation loop, sweep mode, summary builder, and config-driven entrypoint."""

import json
import logging
import os
import sys
import math
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

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
    detect_pool_orientation,
    _filter_ranges_for_price,
    pick_best_range,
    pick_best_range_by_sweep,
    _get_insurance_for_range,
)
from .positions import (
    open_position,
    close_position,
    restore_to_anchor_swap,
)
from .polymarket_execution import (
    ClosePolicy,
    PolymarketFeeModel,
    SlippageConfig,
    apply_execution_costs,
    choose_close_price,
    polymarket_taker_fee_usd,
)
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


def _realized_vol_pct(
    candles: List[Dict],
    i_now: int,
    lookback_hours: int,
    *,
    forecast_mode: str = "perfect",
    price_token: int = 0,
) -> Optional[float]:
    """Return the realised stdev of hourly log-returns expressed as a percent.

    ``forecast_mode = "perfect"`` peeks forward at ``[i_now, i_now+lookback)``
    — a leaked-future signal used to establish a perfect-foresight upper bound.

    ``forecast_mode = "trailing"`` looks backward at
    ``[i_now-lookback, i_now)`` — the realisable, no-peek version.

    Returns ``None`` if there aren't enough samples for the requested window
    (e.g. at series boundaries).
    """
    import math
    if lookback_hours < 2:
        return None
    if forecast_mode == "perfect":
        lo, hi = i_now, i_now + int(lookback_hours)
    elif forecast_mode == "trailing":
        lo, hi = i_now - int(lookback_hours), i_now
    else:
        return None
    if lo < 0 or hi > len(candles) or hi - lo < 2:
        return None
    closes = []
    for c in candles[lo:hi]:
        try:
            cl = float(c["close"])
            if price_token == 1:
                cl = 1.0 / cl if cl else 0.0
            if cl > 0:
                closes.append(cl)
        except (KeyError, TypeError, ValueError):
            continue
    if len(closes) < 2:
        return None
    log_rets = [math.log(closes[k] / closes[k - 1]) for k in range(1, len(closes))]
    if len(log_rets) < 1:
        return None
    mean = sum(log_rets) / len(log_rets)
    var = sum((r - mean) ** 2 for r in log_rets) / max(len(log_rets) - 1, 1)
    return math.sqrt(var) * 100.0


def _build_outcome_to_market_index(
    *,
    fills_glob: str,
    markets_path: str,
) -> Dict[str, Tuple[str, str]]:
    """Map each Polymarket ``outcome_platform_id`` (a.k.a. CLOB token id) to
    its parent ``(market_platform_id, outcome_name)`` so the book lookup can
    resolve a hedge leg's clob_token_id to the right Probalytics ladder.

    Strategy:
      1. Prefer the local fills parquet (each row carries both ids + name).
      2. Fall back to the markets parquet's ``outcomes`` column (a list of
         dicts ``{"platform_id": ..., "name": ...}``).
    """
    import glob
    import pandas as pd

    out: Dict[str, Tuple[str, str]] = {}
    fills_paths = sorted(glob.glob(fills_glob))
    for p in fills_paths:
        try:
            df = pd.read_parquet(p, columns=["market_platform_id", "outcome_platform_id", "outcome_name"])
        except Exception:
            continue
        if df.empty:
            continue
        df = df.dropna(subset=["outcome_platform_id"]).drop_duplicates(subset=["outcome_platform_id"])
        for row in df.itertuples(index=False):
            opid = str(row.outcome_platform_id)
            if opid not in out:
                out[opid] = (str(row.market_platform_id), str(row.outcome_name))

    if os.path.exists(markets_path):
        try:
            mdf = pd.read_parquet(markets_path, columns=["market_platform_id", "outcomes"])
            for row in mdf.itertuples(index=False):
                outcomes = row.outcomes
                if outcomes is None:
                    continue
                for o in outcomes:
                    if isinstance(o, dict):
                        opid = o.get("platform_id")
                        name = o.get("name")
                    else:
                        try:
                            opid = o[1]
                            name = o[2]
                        except Exception:
                            continue
                    if opid and str(opid) not in out:
                        out[str(opid)] = (str(row.market_platform_id), str(name or "Yes"))
        except Exception:
            pass
    return out


def _build_strike_date_index(markets_path: str):
    """Build a (asset, strike_int) -> list of (market_platform_id, opened_at,
    closes_at, outcome_name_yes) records from Probalytics multi-strike
    "will-X-reach-Y-on-DATE" + binary "X-above-Y-on-DATE" markets, used as a
    fallback when a backtester-chosen monthly market isn't in Probalytics.

    Returns ``({}, lambda key, ts: None)`` if the markets parquet is missing
    or empty so callers can no-op.
    """
    import re
    import pandas as pd

    if not os.path.exists(markets_path):
        return {}, (lambda asset, strike, ts_int: None)

    try:
        mdf = pd.read_parquet(markets_path, columns=["market_platform_id", "slug", "outcomes", "opened_at", "closes_at"])
    except Exception:
        return {}, (lambda asset, strike, ts_int: None)

    # asset slug -> normalized name
    def _asset_of(slug: str) -> Optional[str]:
        s = (slug or "").lower()
        if s.startswith("ethereum-above-") or s.startswith("will-ethereum-reach-") or s.startswith("will-eth-reach-"):
            return "ETH"
        if s.startswith("bitcoin-above-") or s.startswith("will-bitcoin-reach-") or s.startswith("will-btc-reach-"):
            return "BTC"
        return None

    _STRIKE_RE_ABOVE = re.compile(r"-above-(\d+(?:k)?)-")
    _STRIKE_RE_REACH = re.compile(r"-reach-(\d+(?:[.,]\d+)?(?:k)?)-")

    def _strike_of(slug: str) -> Optional[int]:
        s = (slug or "").lower()
        m = _STRIKE_RE_ABOVE.search(s) or _STRIKE_RE_REACH.search(s)
        if not m:
            return None
        raw = m.group(1).replace(",", "")
        if raw.endswith("k"):
            try:
                return int(float(raw[:-1]) * 1000)
            except ValueError:
                return None
        try:
            return int(float(raw))
        except ValueError:
            return None

    def _yes_outcome(outcomes) -> Optional[str]:
        if outcomes is None:
            return None
        for o in outcomes:
            name = o.get("name") if isinstance(o, dict) else (o[2] if len(o) > 2 else None)
            if isinstance(name, str) and name.strip().lower() == "yes":
                return name
        return None

    idx: Dict[Tuple[str, int], List[Tuple[str, int, int, str]]] = {}
    for row in mdf.itertuples(index=False):
        asset = _asset_of(row.slug)
        strike = _strike_of(row.slug)
        if not asset or strike is None:
            continue
        yes = _yes_outcome(row.outcomes) or "Yes"
        try:
            opened = int(pd.Timestamp(row.opened_at).timestamp()) if row.opened_at is not None else 0
            closes = int(pd.Timestamp(row.closes_at).timestamp()) if row.closes_at is not None else 2 ** 31 - 1
        except Exception:
            opened, closes = 0, 2 ** 31 - 1
        idx.setdefault((asset, strike), []).append((str(row.market_platform_id), opened, closes, yes))

    for k in idx:
        idx[k].sort(key=lambda r: r[1])

    def _lookup(asset: str, strike: int, ts_int: int) -> Optional[Tuple[str, str]]:
        bucket = idx.get((asset, int(strike)))
        if not bucket:
            return None
        # Prefer the market whose [opened, closes] window contains ts.
        for mpid, opened, closes, yes in bucket:
            if opened <= ts_int <= closes:
                return (mpid, yes)
        # Else nearest by midpoint (markets adjacent to the trade).
        nearest = min(bucket, key=lambda r: abs(((r[1] + r[2]) // 2) - ts_int))
        return (nearest[0], nearest[3])

    return idx, _lookup


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
    touch_settlement_haircut: float = 0.0,
    sell_touched_at_market: bool = False,
    restrict_to_touch_markets: bool = False,
    polymarket_fee_category: str = "crypto",
    polymarket_fees_enabled: bool = True,
    selection_cfg: Optional[Dict] = None,
    restore_to_anchor: bool = False,
    hedge_sizing_mode: str = "il_only",
    hedge_lp_fee_credit_pct: float = 0.0,
    final_restore_at_end: bool = False,
    max_idle_hours: Optional[float] = None,
    require_full_insurance: bool = False,
    take_profit_yes_multiplier: Optional[float] = None,
    vol_regime_toggle: bool = False,
    vol_regime_threshold_pct: Optional[float] = None,
    vol_regime_lookback_hours: int = 24,
    vol_regime_forecast_mode: str = "perfect",
    conditional_hedging: bool = False,
    conditional_hedging_threshold_pct: Optional[float] = None,
    conditional_hedging_lookback_hours: int = 168,
    conditional_hedging_forecast_mode: str = "perfect",
    symmetric_range_pct: Optional[float] = None,
    alt_fee_uplift_pct: float = 0.0,
) -> Tuple[List[Dict], Dict]:
    """
    Walk through hourly candles with realistic wallet tracking.

    When *warmup_candles* and *all_candles* are provided, range selection uses
    a lookback sweep (insurance efficiency) instead of the heuristic scorer.
    Returns (positions, final_wallet, snapshots).

    ``selection_cfg`` (optional) is forwarded to ``pick_best_range`` and
    controls the experiment-matrix knobs (yes-cap, min hedge TTE, scoring
    objective, fixed-width preference).  Defaults to None == legacy behaviour.

    Restore-to-anchor knobs:

    - ``restore_to_anchor``: when True, the wallet is rebalanced to a fixed
      ``(anchor_usdc, anchor_eth)`` token split after every position close.
      The anchor is captured at the first successful open (== the actual
      tokens deployed into the LP) and threaded through every later round.
      Implies ``hedge_sizing_mode = "full_restore"`` and
      ``final_restore_at_end = True`` unless the caller explicitly overrides.
    - ``hedge_sizing_mode``: ``"il_only"`` (legacy: contracts == |IL| at the
      boundary) or ``"full_restore"`` (size contracts so the touched-side
      payout fully covers IL + restore swap fee + close gas).
    - ``hedge_lp_fee_credit_pct``: under-hedge by this fraction (0.0–1.0) on
      the assumption that LP fees will partially offset IL.
    - ``final_restore_at_end``: when True, run one more pool swap at
      ``final_price`` to align the wallet with the anchor at the end of the
      backtest window. Records ``final_restore_swap_fee`` and
      ``final_unfilled_usd`` in the returned snapshots tail.
    """
    # When restore_to_anchor is on, force the dependent knobs unless the
    # caller already overrode them. This keeps the user-facing surface small
    # (one switch is enough) while preserving the explicit-override path.
    if restore_to_anchor:
        if hedge_sizing_mode == "il_only":
            hedge_sizing_mode = "full_restore"
        # final_restore_at_end is a binary opt-in; default it ON in restore mode.
        final_restore_at_end = True if not final_restore_at_end else final_restore_at_end
        # Restore-to-anchor wants maximum token utilisation. Auto-enable the
        # graceful filter relaxation in pick_best_range so the strategy stays
        # active around month-boundaries when only the about-to-expire
        # Polymarket market satisfies the YES-cap / TTE filters.
        if selection_cfg is None:
            selection_cfg = {}
        selection_cfg.setdefault("relax_filters_when_empty", True)
    # If the caller asked for a max-idle bound, also turn on filter relaxation
    # so the buffer/cap/TTE filters don't keep us idle past the deadline.
    if max_idle_hours is not None and float(max_idle_hours) > 0:
        if selection_cfg is None:
            selection_cfg = {}
        selection_cfg.setdefault("relax_filters_when_empty", True)
    get_range_combinations = _get_db_func("get_range_combinations")
    get_clob_token_id = _get_db_func("get_clob_token_id")
    get_historical_bet_price = _get_db_func("get_historical_bet_price")
    get_candidate_markets = _get_db_func("get_candidate_markets")
    use_lookback = warmup_candles is not None and all_candles is not None and fixed_range is None
    # Range combinations depend on which Polymarket markets were active at a given time.
    # For historical backtests we should query combinations "as-of" the candle timestamp.
    # We keep an initial snapshot for early fallbacks, but selection will refresh per-open.
    all_combos = get_range_combinations(
        token_symbol, conn, restrict_to_touch_markets=restrict_to_touch_markets
    ) if conn is not None else []

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

    per_asset_slippage: Dict[str, float] = {}
    if conn is not None:
        try:
            from .slippage_fit import fit_all_assets

            per_asset_slippage = fit_all_assets(conn, asset_ids=None,
                                                 fallback_per_1k=float(slippage_per_1k_contracts or 0.0))
        except Exception as exc:
            # Any SQL error leaves the connection in an aborted transaction state
            # until we roll it back; the simulator continues even when slippage
            # fitting is unavailable, so make sure downstream queries still work.
            try:
                conn.rollback()
            except Exception:
                pass
            logger.warning("Per-asset slippage fit unavailable (%s); using flat per_1k_contracts.", exc)

    # Layer Probalytics-derived slippage on top: when both sources have a
    # value for the same outcome (clob_token_id), Probalytics wins because
    # it has taker-side + dispersion data data-api/trades lacks.
    try:
        from probalytics_pkg.slippage_fit import (
            load_fills as _pb_load_fills,
            fit_per_asset_slippage as _pb_fit,
        )
        pb_fills = _pb_load_fills()
        if not pb_fills.empty:
            pb_fit = _pb_fit(
                pb_fills,
                fallback_per_1k=float(slippage_per_1k_contracts or 0.02),
            )
            overrides = sum(1 for k in pb_fit if k in per_asset_slippage)
            adds = sum(1 for k in pb_fit if k not in per_asset_slippage)
            per_asset_slippage = {**per_asset_slippage, **pb_fit}
            logger.info(
                "Probalytics slippage fit applied: %d outcomes (%d overrides, %d new)",
                len(pb_fit), overrides, adds,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Probalytics slippage fit unavailable (%s); using bet_trades fit only.", exc)

    slippage_cfg = SlippageConfig(
        per_1k_contracts=float(slippage_per_1k_contracts or 0.0),
        max_per_contract=float(slippage_max_per_contract or 0.0),
        per_asset=per_asset_slippage,
    )

    fee_model = PolymarketFeeModel.for_category(polymarket_fee_category)
    if not polymarket_fees_enabled:
        fee_model = PolymarketFeeModel(
            category=fee_model.category,
            fee_rate=fee_model.fee_rate,
            exponent=fee_model.exponent,
            enabled=False,
        )

    # Build optional Probalytics on-demand orderbook lookup. When credentials
    # and the local fills cache are present we resolve each clob_token_id
    # (== Polymarket outcome platform_id) to its parent market_platform_id and
    # outcome name, then have apply_execution_costs walk the actual L2 ladder
    # at trade time. Falls back transparently to the parametric (spread +
    # fitted slippage) path on any error or cache miss.
    #
    # Two resolution layers:
    #   1. Direct: outcome_platform_id -> Probalytics market (exact match;
    #      works only when the backtester picks a market Probalytics tracks).
    #   2. Strike+date proxy: when the chosen monthly multi-strike market
    #      isn't in Probalytics' coverage, fall back to the Probalytics
    #      "will-X-reach-<STRIKE>-on-DATE" weekly market with the same asset
    #      + strike whose lifetime contains the trade timestamp. Same
    #      economic exposure (touch on the same strike), so the L2 quote is a
    #      good proxy for execution cost.
    book_lookup = None
    book_fetcher = None
    try:
        from probalytics_pkg.client import load_creds_from_env, ProbalyticsRest
        from probalytics_pkg.ondemand import OrderBookFetcher

        creds = load_creds_from_env()
        rest = ProbalyticsRest(creds)
        root = os.environ.get("PROBALYTICS_DATA_ROOT", "data/probalytics")
        outcome_to_market: Dict[str, Tuple[str, str]] = {}
        strike_lookup = None
        try:
            outcome_to_market = _build_outcome_to_market_index(
                fills_glob=os.path.join(root, "fills", "*.parquet"),
                markets_path=os.path.join(root, "markets.parquet"),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to build outcome->market index for book lookup: %s", exc)
            outcome_to_market = {}
        try:
            _strike_idx, strike_lookup = _build_strike_date_index(
                markets_path=os.path.join(root, "markets.parquet"),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to build strike+date index for book lookup: %s", exc)
            strike_lookup = None
            _strike_idx = {}

        if outcome_to_market or _strike_idx:
            book_fetcher = OrderBookFetcher(rest=rest, root=root)

            # Per-leg metadata is published into these dicts by the simulate
            # loop right before each open/close, keyed by clob_token_id, so
            # the strike-date proxy fallback knows what to look up.
            book_meta: Dict[str, Tuple[str, int]] = {}

            def _resolve(clob_id: str, ts_int: int) -> Optional[Tuple[str, str]]:
                m = outcome_to_market.get(str(clob_id))
                if m:
                    return m
                meta = book_meta.get(str(clob_id))
                if meta and strike_lookup is not None:
                    asset, strike = meta
                    return strike_lookup(asset, strike, int(ts_int))
                return None

            def _book_lookup(clob_id, ts_int):
                resolved = _resolve(str(clob_id), int(ts_int))
                if not resolved:
                    return None
                market_pid, outcome_name = resolved
                replay = book_fetcher.get(market_pid, datetime.fromtimestamp(int(ts_int), tz=timezone.utc))
                if replay is None:
                    return None
                snap = replay.snapshot_at(
                    datetime.fromtimestamp(int(ts_int), tz=timezone.utc),
                    outcome=outcome_name,
                )
                if snap is None:
                    return None
                return (snap.bids, snap.asks)

            book_lookup = _book_lookup
            logger.info(
                "On-demand orderbook fetcher armed: %d direct mappings, %d (asset,strike) buckets, root=%s",
                len(outcome_to_market), len(_strike_idx), root,
            )
        else:
            book_meta = {}
            logger.info("On-demand orderbook fetcher disabled (no mappings).")
    except Exception as exc:  # noqa: BLE001
        book_meta = {}
        logger.info("On-demand orderbook fetcher disabled (%s); using parametric slippage only.", exc)

    positions: List[Dict] = []
    snapshots: List[Dict] = []
    current_pos: Optional[Dict] = None
    lower_clob_id: Optional[str] = None
    upper_clob_id: Optional[str] = None
    i = 0
    log = logger.info if not quiet else logger.debug
    pending_close: Optional[Dict] = None
    delta_matched_qty: Optional[Dict[str, float]] = None

    # Idle-tracking for the optional max_idle_hours guarantee. ``idle_since_ts``
    # is set when current_pos becomes None (after a close, or at run start) and
    # cleared the moment a new position opens. Once (ts - idle_since_ts) crosses
    # max_idle_hours we force the selector to fall back to a fully-relaxed
    # filter set (buffer down to 0%, cap/TTE filters dropped) so the strategy
    # always re-enters within the user-specified window.
    max_idle_secs: Optional[float] = (
        float(max_idle_hours) * 3600.0
        if (max_idle_hours is not None and float(max_idle_hours) > 0)
        else None
    )
    idle_since_ts: Optional[int] = int(candles[0]["periodStartUnix"]) if candles else None

    # Restore-to-anchor state. ``anchor_*`` are populated AFTER the very first
    # successful open (so they always reflect the actual tokens deployed into
    # the pool, never the wallet's pre-LP layout). ``final_restore_*`` capture
    # the end-of-run alignment swap.
    anchor_usdc: Optional[float] = None
    anchor_eth: Optional[float] = None
    final_restore_record: Optional[Dict[str, float]] = None
    pool_swap_fee_rate = float(pool_data.get("feeTier", 0) or 0) / 1_000_000.0

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
                touch_settlement_haircut=touch_settlement_haircut,
                sell_touched_at_market=sell_touched_at_market,
                fee_model=fee_model,
                book_lookup=book_lookup,
                restore_to_anchor=restore_to_anchor,
                pool_swap_fee_rate=pool_swap_fee_rate,
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
            idle_since_ts = ts
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
            # ----------------------------------------------------------------
            # Vol-regime gating (Phase 30A / 30B).
            # ----------------------------------------------------------------
            # Both decisions depend on a single forecast of realised hourly
            # volatility over the next/last N hours. Perfect-foresight mode
            # uses the actual realised series — that's a theoretical upper
            # bound, not a tradeable strategy. Trailing mode uses historical
            # vol only, which is realisable.
            _vol_skip_cycle = False
            _vol_force_bypass = False
            if vol_regime_toggle and vol_regime_threshold_pct is not None:
                _v = _realized_vol_pct(
                    all_candles or candles,
                    (warmup_len + i) if (all_candles is not None and warmup_candles) else i,
                    vol_regime_lookback_hours,
                    forecast_mode=vol_regime_forecast_mode,
                    price_token=price_token,
                )
                if _v is not None and _v > float(vol_regime_threshold_pct):
                    _vol_skip_cycle = True
            if conditional_hedging and conditional_hedging_threshold_pct is not None:
                _v2 = _realized_vol_pct(
                    all_candles or candles,
                    (warmup_len + i) if (all_candles is not None and warmup_candles) else i,
                    conditional_hedging_lookback_hours,
                    forecast_mode=conditional_hedging_forecast_mode,
                    price_token=price_token,
                )
                # Low forecast vol → cheap to go uninsured: force bypass.
                # High forecast vol → keep the hedge (don't force bypass; selection_cfg default wins).
                if _v2 is not None and _v2 <= float(conditional_hedging_threshold_pct):
                    _vol_force_bypass = True
            if _vol_skip_cycle:
                # Sit out: don't open, just snapshot and idle.
                if idle_since_ts is None:
                    idle_since_ts = ts
                _snap(ts, current_price, current_pos, wallet)
                i += 1
                continue
            range_method = None
            range_info: Optional[Dict[str, Any]] = None
            # ----------------------------------------------------------------
            # Beefy ConcLiq-style synthetic symmetric range (Phase 31).
            # When ``symmetric_range_pct`` is set, build a fresh range as
            #   [P * (1 - w/200), P * (1 + w/200)]
            # at every open. This is the spec's
            #   [tickFloor - positionWidth*tickSpacing, tickFloor + positionWidth*tickSpacing]
            # behaviour, mapped to %-of-spot space (we don't simulate ticks
            # directly). Requires bypass_insurance because we don't try to
            # find a Polymarket combo to match a synthetic range.
            # ----------------------------------------------------------------
            if (
                symmetric_range_pct is not None
                and float(symmetric_range_pct) > 0.0
                and bool(selection_cfg and selection_cfg.get("bypass_insurance"))
            ):
                w = float(symmetric_range_pct) / 100.0
                mn = current_price * (1.0 - w / 2.0)
                mx = current_price * (1.0 + w / 2.0)
                insurance_info = {"lower_bet_price": 0.0, "upper_bet_price": 0.0}
                lower_clob_id = None
                upper_clob_id = None
                lower_meta = upper_meta = None
                lower_market_volume = upper_market_volume = 0.0
                scorer_lower_id = scorer_upper_id = None
                scorer_lower_end = scorer_upper_end = None
                range_method = "symmetric_synthetic"
            elif fixed_range is not None:
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
                    combos_at_ts = get_range_combinations(
                        token_symbol, conn, candle_ts=ts,
                        restrict_to_touch_markets=restrict_to_touch_markets,
                    ) if conn is not None else []
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
                    simulate_fn=(lambda *a, **kw: simulate(
                        *a, **kw,
                        initial_eth=initial_eth,
                        initial_usdc=initial_usdc,
                        touch_settlement_haircut=touch_settlement_haircut,
                        sell_touched_at_market=sell_touched_at_market,
                        restrict_to_touch_markets=restrict_to_touch_markets,
                        polymarket_fee_category=polymarket_fee_category,
                        polymarket_fees_enabled=polymarket_fees_enabled,
                    )),
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
                combos_at_ts = get_range_combinations(
                    token_symbol, conn, candle_ts=ts,
                    restrict_to_touch_markets=restrict_to_touch_markets,
                ) if conn is not None else all_combos
                range_info = pick_best_range(
                    combos_at_ts, current_price, token_symbol, ts, investment, conn,
                    selection_cfg=selection_cfg,
                )
                # max_idle_hours guarantee: if we've been idle longer than the
                # caller's deadline, force a fully-relaxed re-pick so we never
                # exceed the bound. Drops buffer/cap/TTE filters entirely and
                # picks the cheapest YES-cap-passing range that contains
                # current_price (or any range that contains it).
                if (
                    range_info is None
                    and max_idle_secs is not None
                    and idle_since_ts is not None
                    and (ts - idle_since_ts) >= max_idle_secs
                ):
                    forced_cfg = dict(selection_cfg or {})
                    forced_cfg["relax_filters_when_empty"] = True
                    forced_cfg["range_buffer_pct"] = 0.0
                    forced_cfg["range_max_width_pct"] = 200.0
                    forced_cfg["range_yes_cap"] = 0.99
                    forced_cfg["min_hedge_tte_hours"] = None
                    range_info = pick_best_range(
                        combos_at_ts, current_price, token_symbol, ts, investment, conn,
                        selection_cfg=forced_cfg,
                    )
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

            # Resolve Polymarket markets (and expiries) for this position
            # *before* opening so that per-asset slippage and the book
            # lookup (when armed) actually fire on the buy leg.
            #
            # Depth-aware selection priority:
            #   1. Use the markets the scorer already chose (they're the
            #      deepest-yet-cap-passing candidates per leg). This is
            #      the only path where leg pricing and leg execution
            #      actually agree, which matters because the scorer
            #      already paid the premium-side cost in the YES it
            #      reported and we now need to charge spread/slippage
            #      against that exact same market's depth.
            #   2. Fall back to a fresh ``get_candidate_markets`` lookup
            #      (also depth-DESC ordered) for paths that don't go
            #      through the scorer (fixed_range / lookback sweep).
            _sel_cfg = selection_cfg or {}
            _restrict_touch = bool(_sel_cfg.get("restrict_to_touch_markets", restrict_to_touch_markets))
            _min_vol = float(_sel_cfg.get("min_market_volume", 0.0) or 0.0)

            def _candidates(level, direction):
                if conn is None:
                    return []
                try:
                    return get_candidate_markets(
                        token_symbol, level, direction, "Yes", conn, candle_ts=ts,
                        restrict_to_touch_markets=_restrict_touch,
                        min_market_volume=_min_vol,
                    )
                except TypeError:
                    return get_candidate_markets(token_symbol, level, direction, "Yes", conn, candle_ts=ts)
                except Exception:
                    return []

            # 1) Prefer the leg ids the scorer already locked in.
            scorer_lower_id = None
            scorer_upper_id = None
            scorer_lower_vol = 0.0
            scorer_upper_vol = 0.0
            scorer_lower_end = None
            scorer_upper_end = None
            if range_info is not None and isinstance(range_info, dict):
                scorer_lower_id = range_info.get("lower_clob_token_id")
                scorer_upper_id = range_info.get("upper_clob_token_id")
                scorer_lower_vol = float(range_info.get("lower_market_volume") or 0.0)
                scorer_upper_vol = float(range_info.get("upper_market_volume") or 0.0)
                _le = range_info.get("lower_end_ts")
                _ue = range_info.get("upper_end_ts")
                if _le is not None:
                    try:
                        scorer_lower_end = int(_le)
                    except (TypeError, ValueError):
                        scorer_lower_end = None
                if _ue is not None:
                    try:
                        scorer_upper_end = int(_ue)
                    except (TypeError, ValueError):
                        scorer_upper_end = None

            # 2) Fallback resolution path (used by fixed_range / lookback /
            # any leg the scorer didn't fill in).
            lower_meta = None
            upper_meta = None
            if not scorer_lower_id or not scorer_upper_id:
                try:
                    lower_cands = _candidates(mn, "down") if not scorer_lower_id else []
                    upper_cands = _candidates(mx, "up") if not scorer_upper_id else []
                    if not lower_cands and not scorer_lower_id and _min_vol > 0:
                        lower_cands = get_candidate_markets(
                            token_symbol, mn, "down", "Yes", conn, candle_ts=ts,
                            restrict_to_touch_markets=_restrict_touch, min_market_volume=0.0,
                        ) if conn is not None else []
                    if not upper_cands and not scorer_upper_id and _min_vol > 0:
                        upper_cands = get_candidate_markets(
                            token_symbol, mx, "up", "Yes", conn, candle_ts=ts,
                            restrict_to_touch_markets=_restrict_touch, min_market_volume=0.0,
                        ) if conn is not None else []
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

            def _vol(meta):
                if not meta:
                    return 0.0
                try:
                    return float(meta.get("market_volume") or 0.0)
                except Exception:
                    return 0.0

            def _clob_fallback(level, direction):
                if conn is None:
                    return None
                try:
                    return get_clob_token_id(
                        token_symbol, level, direction, "Yes", conn, candle_ts=ts,
                        restrict_to_touch_markets=_restrict_touch,
                        min_market_volume=_min_vol,
                    )
                except TypeError:
                    return get_clob_token_id(token_symbol, level, direction, "Yes", conn, candle_ts=ts)

            lower_clob_id = scorer_lower_id or (lower_meta or {}).get("clob_token_id") or _clob_fallback(mn, "down")
            upper_clob_id = scorer_upper_id or (upper_meta or {}).get("clob_token_id") or _clob_fallback(mx, "up")
            lower_market_volume = scorer_lower_vol if scorer_lower_id else _vol(lower_meta)
            upper_market_volume = scorer_upper_vol if scorer_upper_id else _vol(upper_meta)

            # Plumb depth-aware per-asset slippage into the existing
            # SlippageConfig so apply_execution_costs uses a smaller
            # per_1k for deeper markets and a larger one for ghost
            # markets. Only set when no real Probalytics fit overrides
            # this asset, so live data still wins when present.
            try:
                ref_depth = 100_000.0
                slip_floor, slip_cap = 0.005, 0.20
                slip_default = float(slippage_per_1k or 0.02)

                def _depth_per_1k(volume_usd: float) -> float:
                    v = max(float(volume_usd or 0.0), 1.0)
                    scaled = slip_default * (ref_depth / v) ** 0.5
                    return max(min(scaled, slip_cap), slip_floor)

                if lower_clob_id and lower_clob_id not in slippage_cfg.per_asset:
                    slippage_cfg.per_asset[str(lower_clob_id)] = _depth_per_1k(lower_market_volume)
                if upper_clob_id and upper_clob_id not in slippage_cfg.per_asset:
                    slippage_cfg.per_asset[str(upper_clob_id)] = _depth_per_1k(upper_market_volume)
            except Exception:
                pass

            # Publish (asset, strike) into the shared book_meta dict so the
            # strike-date proxy fallback inside book_lookup can resolve a
            # clob_token_id we never directly indexed (i.e. the monthly
            # multi-strike markets the backtester picks).
            try:
                if lower_clob_id is not None:
                    book_meta[str(lower_clob_id)] = (token_symbol, int(round(float(mn))))
                if upper_clob_id is not None:
                    book_meta[str(upper_clob_id)] = (token_symbol, int(round(float(mx))))
            except Exception:
                pass

            _bypass = bool(selection_cfg and selection_cfg.get("bypass_insurance"))
            if _vol_force_bypass:
                _bypass = True
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
                lower_clob_token_id=None if _bypass else lower_clob_id,
                upper_clob_token_id=None if _bypass else upper_clob_id,
                lower_end_ts=None if _bypass else (scorer_lower_end if scorer_lower_id else _end_ts(lower_meta)),
                upper_end_ts=None if _bypass else (scorer_upper_end if scorer_upper_id else _end_ts(upper_meta)),
                lower_market_volume=0.0 if _bypass else lower_market_volume,
                upper_market_volume=0.0 if _bypass else upper_market_volume,
                fee_model=fee_model,
                book_lookup=book_lookup,
                bypass_insurance=_bypass,
                anchor_usdc=anchor_usdc,
                anchor_eth=anchor_eth,
                hedge_sizing_mode=hedge_sizing_mode,
                hedge_lp_fee_credit_pct=hedge_lp_fee_credit_pct,
                touch_settlement_haircut=touch_settlement_haircut,
            )
            if current_pos is None:
                _snap(ts, current_price, current_pos, wallet)
                i += 1
                continue

            # require_full_insurance: refuse to open if either leg ended up with
            # zero contracts (e.g. full_restore + LP-fee-credit drove restore_cost
            # to 0, or _solve_contracts_for_payout couldn't size against thin
            # books). The position is dropped, wallet is untouched, and we
            # continue idling so the max_idle_hours guard can re-pick a deeper
            # market on the next candle.
            if (
                require_full_insurance
                and not _bypass
                and (
                    float(current_pos.get("lower_contracts") or 0.0) <= 0.0
                    or float(current_pos.get("upper_contracts") or 0.0) <= 0.0
                )
            ):
                log(
                    f"  >> SKIP OPEN @ ${current_price:.0f} | "
                    f"{datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')}: "
                    f"insurance=$0 (lower={current_pos.get('lower_contracts'):.0f}, "
                    f"upper={current_pos.get('upper_contracts'):.0f}) — "
                    f"require_full_insurance is on"
                )
                current_pos = None
                lower_clob_id = None
                upper_clob_id = None
                _snap(ts, current_price, current_pos, wallet)
                i += 1
                continue

            # Position successfully opened — clear the idle-tracker so the next
            # close starts a fresh max_idle window.
            idle_since_ts = None

            # Capture the anchor at the very first successful open so every
            # later round (and the end-of-run final restore) targets the same
            # token split. This is the actual (X USDC, Y ETH) deployed into
            # the pool — not the user-supplied initial_eth which was settled
            # via _required_usdc_for_eth above.
            if anchor_usdc is None or anchor_eth is None:
                anchor_usdc = float(current_pos["token0_dep"])
                anchor_eth = float(current_pos["token1_dep"])
                # Backfill onto the first position record so build_summary can
                # surface the same numbers without recomputing.
                current_pos["anchor_usdc"] = anchor_usdc
                current_pos["anchor_eth"] = anchor_eth

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
            try:
                pool_L = float(candle.get("liquidity") or 0.0)
            except (TypeError, ValueError):
                pool_L = 0.0
            pool_L_arg: Optional[float] = pool_L if pool_L > 0 else None
            f_usdc, f_eth = compute_hourly_fee_split(
                candle, candles[i - 1],
                current_pos["liquidity"],
                current_pos["min_range"], current_pos["max_range"],
                dec0, dec1, price_token,
                pool_active_liquidity=pool_L_arg,
            )
            # Phase 31 — Beefy ConcLiq alt-position fee uplift.
            # The reference design deploys a *second* one-sided LP on the
            # heavier-inventory side after the main fill. That second
            # position earns fees concurrently with the main one whenever
            # spot is inside the alt range — effectively boosting strategy
            # fees by ~10-25% in a trending market. We don't simulate the
            # alt range explicitly (it would need a second active-liquidity
            # state machine); instead we apply a flat percent uplift on the
            # main-range fees as a calibrated approximation.
            if alt_fee_uplift_pct and alt_fee_uplift_pct > 0:
                _mult = 1.0 + float(alt_fee_uplift_pct) / 100.0
                f_usdc *= _mult
                f_eth *= _mult
            current_pos["accumulated_fees_usdc"] += f_usdc
            current_pos["accumulated_fees_eth"] += f_eth
            current_pos["candle_count"] += 1
            our_L = float(current_pos.get("liquidity") or 0.0)
            if pool_L > 0 and our_L > 0:
                dil = pool_L / (pool_L + our_L)
                current_pos["dilution_factor_sum"] = current_pos.get("dilution_factor_sum", 0.0) + dil
                current_pos["dilution_sample_count"] = current_pos.get("dilution_sample_count", 0) + 1
                current_pos["pool_L_sample_sum"] = current_pos.get("pool_L_sample_sum", 0.0) + pool_L
                current_pos["our_L_share_sum"] = current_pos.get("our_L_share_sum", 0.0) + (our_L / (pool_L + our_L))

        # ------------------------------------------------------------------
        # Take-profit on insurance legs (optional).
        # ------------------------------------------------------------------
        # Hypothesis: the YES legs we bought as IL insurance are themselves
        # tradeable. Each hour we mark-to-market each open leg at the current
        # bid (less spread, slippage, taker fee). If sell-side proceeds for
        # a leg are >= ``take_profit_yes_multiplier`` * what we paid for it,
        # we close that leg early and the position continues *unhedged* on
        # that side until LP touch/expiry.
        #
        # Costs/fees of the early sell are accumulated into the position's
        # existing sell-side cost fields so build_summary sees a consistent
        # picture; proceeds accumulate into ``insurance_sellback`` which
        # close_position adds to at the end.
        if (
            take_profit_yes_multiplier is not None
            and float(take_profit_yes_multiplier) > 0
            and (
                current_pos.get("lower_clob_token_id") is not None
                or current_pos.get("upper_clob_token_id") is not None
            )
        ):
            K = float(take_profit_yes_multiplier)
            for side, clob_key, contracts_key, cost_key in (
                ("lower", "lower_clob_token_id", "lower_contracts", "lower_insurance_cost_usdc"),
                ("upper", "upper_clob_token_id", "upper_contracts", "upper_insurance_cost_usdc"),
            ):
                contracts = float(current_pos.get(contracts_key, 0) or 0)
                cost_paid = float(current_pos.get(cost_key, 0) or 0)
                clob_id = current_pos.get(clob_key)
                if contracts <= 0 or cost_paid <= 0 or not clob_id or conn is None:
                    continue
                # Prefer the same execution path close_position uses: L2 book
                # via book_lookup, falling back to parametric mid. This keeps
                # the TP check honest — we'd see the same book a real operator
                # would when deciding to take profit.
                bids_for_sell = None
                asks_for_sell = None
                if book_lookup is not None:
                    try:
                        _bk = book_lookup(clob_id, ts)
                    except Exception:
                        _bk = None
                    if _bk:
                        bids_for_sell, asks_for_sell = _bk[0], _bk[1]
                if bids_for_sell:
                    mid_for_call = (
                        float(bids_for_sell[0]["price"])
                        if isinstance(bids_for_sell[0], dict)
                        else float(bids_for_sell[0][0])
                    )
                else:
                    try:
                        mid_for_call = get_historical_bet_price(clob_id, ts, conn)
                    except Exception:
                        mid_for_call = None
                if mid_for_call is None or mid_for_call <= 0:
                    continue
                try:
                    bid_exec, sp_cost, sl_cost = apply_execution_costs(
                        mid_price=float(mid_for_call),
                        spread=spread,
                        contracts=contracts,
                        side="sell",
                        slippage_cfg=slippage_cfg,
                        asset_id=clob_id,
                        fee_model=fee_model,
                        book_bids=bids_for_sell,
                        book_asks=asks_for_sell,
                    )
                except Exception:
                    continue
                if bid_exec <= 0:
                    continue
                proceeds = contracts * bid_exec
                if proceeds < K * cost_paid:
                    continue
                fee = polymarket_taker_fee_usd(contracts, bid_exec, fee_model)
                # Record early sellback into the position record. close_position
                # will add the proceeds into the wallet at close (external-cost
                # accounting parity); spread/slippage/fee are sunk friction
                # already netted in ``bid_exec`` * ``contracts``.
                current_pos["insurance_sellback"] = (
                    float(current_pos.get("insurance_sellback", 0.0) or 0.0) + proceeds
                )
                current_pos["spread_cost_sell"] = (
                    float(current_pos.get("spread_cost_sell", 0.0) or 0.0) + sp_cost
                )
                current_pos["slippage_cost_sell"] = (
                    float(current_pos.get("slippage_cost_sell", 0.0) or 0.0) + sl_cost
                )
                current_pos["polymarket_fee_sell"] = (
                    float(current_pos.get("polymarket_fee_sell", 0.0) or 0.0) + fee
                )
                # Track that this leg was sold early so close_position skips it.
                # Mutating contracts to 0 is enough; the close_position code
                # paths all gate on ``contracts > 0``.
                current_pos[contracts_key] = 0.0
                early_log_key = f"{side}_early_sold_ts"
                if early_log_key not in current_pos:
                    current_pos[early_log_key] = ts
                    current_pos[f"{side}_early_proceeds"] = proceeds
                    current_pos[f"{side}_early_buy_cost"] = cost_paid
                    log(
                        f"  >> TAKE PROFIT ({side.upper()}) @ ${current_price:.0f} | "
                        f"{datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')}: "
                        f"bid_exec=${bid_exec:.4f}/c × {contracts:,.0f}c = ${proceeds:,.0f}  "
                        f"(buy_cost=${cost_paid:,.0f}, mult={proceeds/cost_paid:.2f}x ≥ {K:.2f}x)"
                    )

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
                fee_model=fee_model,
                book_lookup=book_lookup,
                restore_to_anchor=restore_to_anchor,
                pool_swap_fee_rate=pool_swap_fee_rate,
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
            idle_since_ts = ts
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
                touch_settlement_haircut=touch_settlement_haircut,
                sell_touched_at_market=sell_touched_at_market,
                fee_model=fee_model,
                book_lookup=book_lookup,
                restore_to_anchor=restore_to_anchor,
                pool_swap_fee_rate=pool_swap_fee_rate,
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
            idle_since_ts = ts
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
            touch_settlement_haircut=touch_settlement_haircut,
            sell_touched_at_market=sell_touched_at_market,
            fee_model=fee_model,
            book_lookup=book_lookup,
            restore_to_anchor=restore_to_anchor,
            pool_swap_fee_rate=pool_swap_fee_rate,
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

    # ------------------------------------------------------------------
    # End-of-run final restore swap (restore-to-anchor mode only).
    # ------------------------------------------------------------------
    # After the last close (or directly if no position ever opened) align the
    # wallet with the anchor at ``final_price``. Surplus stays as USDC,
    # deficit means we end below the anchor and the unfilled amount is the
    # final loss vs the X USDC + Y ETH target.
    final_price_for_restore = (
        float(candles[-1]["close"]) if price_token == 0 else 1.0 / float(candles[-1]["close"])
    )
    if (
        final_restore_at_end
        and anchor_usdc is not None
        and anchor_eth is not None
    ):
        restore = restore_to_anchor_swap(
            have_usdc=wallet["usdc"],
            have_eth=wallet["eth"],
            anchor_usdc=anchor_usdc,
            anchor_eth=anchor_eth,
            price=final_price_for_restore,
            swap_fee_rate=pool_swap_fee_rate,
        )
        anchor_value_at_final = anchor_usdc + anchor_eth * final_price_for_restore
        wallet_value_pre = wallet["usdc"] + wallet["eth"] * final_price_for_restore
        wallet["usdc"] = restore["end_usdc"]
        wallet["eth"] = restore["end_eth"]
        final_restore_record = {
            "ts": int(candles[-1]["periodStartUnix"]),
            "price": float(final_price_for_restore),
            "wallet_pre": {
                "usdc": float(wallet_value_pre - wallet["eth"] * final_price_for_restore),
                "eth": float(wallet["eth"] + (wallet_value_pre - wallet["usdc"]) * 0.0),
            },
            "anchor_value_at_final_usd": float(anchor_value_at_final),
            "swap_amount_usd": float(restore["swap_amount_usd"]),
            "swap_fee_usd": float(restore["swap_fee_usd"]),
            "unfilled_usd": float(restore["unfilled_usd"]),
            "wallet_after": {
                "usdc": float(restore["end_usdc"]),
                "eth": float(restore["end_eth"]),
                "value_usd": float(restore["end_usdc"] + restore["end_eth"] * final_price_for_restore),
            },
        }
        log(
            "  >> FINAL RESTORE @ $%.0f | swap=$%.2f fee=$%.2f unfilled=$%.2f"
            % (
                final_price_for_restore,
                restore["swap_amount_usd"],
                restore["swap_fee_usd"],
                restore["unfilled_usd"],
            )
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
                "anchor_usdc": (round(anchor_usdc, 2) if anchor_usdc is not None else None),
                "anchor_eth": (round(anchor_eth, 6) if anchor_eth is not None else None),
                "final_restore": final_restore_record,
            },
        )

    if book_fetcher is not None:
        st = book_fetcher.stats()
        n_book_open = sum(1 for p in positions if p.get("book_used_open"))
        n_book_close = sum(1 for p in positions if p.get("book_used_close"))
        logger.info(
            "Orderbook coverage: opens_book=%d/%d closes_book=%d/%d  "
            "fetcher hits=%d misses=%d empty=%d errors=%d",
            n_book_open, len(positions), n_book_close, len(positions),
            st["cache_hits"], st["cache_misses"], st["empty_responses"], st["errors"],
        )

    # Stash restore-to-anchor metadata on the returned wallet (sentinel keys
    # prefixed with ``__`` so legacy consumers ignore them). build_summary
    # picks these up to compute the new headline ROI; tests that only read
    # ``usdc``/``eth`` are unaffected.
    if anchor_usdc is not None:
        wallet["__anchor_usdc__"] = float(anchor_usdc)
    if anchor_eth is not None:
        wallet["__anchor_eth__"] = float(anchor_eth)
    if final_restore_record is not None:
        wallet["__final_restore__"] = final_restore_record
    wallet["__restore_to_anchor__"] = bool(restore_to_anchor)
    wallet["__hedge_sizing_mode__"] = str(hedge_sizing_mode)
    wallet["__hedge_lp_fee_credit_pct__"] = float(hedge_lp_fee_credit_pct or 0.0)
    wallet["__pool_swap_fee_rate__"] = float(pool_swap_fee_rate)

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
    risk_free_rate_apy: float = 0.0,
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
    total_polymarket_fees = sum(
        p.get("polymarket_fee_buy", 0.0) + p.get("polymarket_fee_sell", 0.0) for p in positions
    )

    # ------------------------------------------------------------------
    # Restore-to-anchor aggregates.
    # ------------------------------------------------------------------
    restore_to_anchor_on = bool(final_wallet.get("__restore_to_anchor__", False))
    anchor_usdc_meta = final_wallet.get("__anchor_usdc__")
    anchor_eth_meta = final_wallet.get("__anchor_eth__")
    final_restore_meta = final_wallet.get("__final_restore__")
    hedge_sizing_mode_meta = str(final_wallet.get("__hedge_sizing_mode__", "il_only"))
    hedge_lp_fee_credit_pct_meta = float(final_wallet.get("__hedge_lp_fee_credit_pct__", 0.0) or 0.0)
    pool_swap_fee_rate_meta = float(final_wallet.get("__pool_swap_fee_rate__", 0.0) or 0.0)

    total_restore_swap_fees = sum(p.get("restore_swap_fee", 0.0) for p in positions)
    total_restore_swap_amount = sum(p.get("restore_swap_amount", 0.0) for p in positions)
    # IMPORTANT: per-position ``restore_unfilled_usd`` is the *cumulative*
    # wallet-vs-anchor gap after that close (because the restore swap targets
    # the same anchor every time, so the prior round's surplus is rolled into
    # the next wallet). Summing it would double-count. The end-state value is
    # the last close's unfilled (or the final-restore unfilled when present).
    if final_restore_meta:
        cumulative_unfilled_usd = float(final_restore_meta.get("unfilled_usd", 0.0) or 0.0)
        total_restore_swap_fees += float(final_restore_meta.get("swap_fee_usd", 0.0) or 0.0)
        total_restore_swap_amount += float(final_restore_meta.get("swap_amount_usd", 0.0) or 0.0)
    else:
        # Fallback: pick the last position with a recorded restore_unfilled_usd
        # (i.e. the most recent close in restore mode). Zero when nothing ran.
        cumulative_unfilled_usd = 0.0
        for p in reversed(positions):
            if "restore_unfilled_usd" in p:
                cumulative_unfilled_usd = float(p.get("restore_unfilled_usd", 0.0) or 0.0)
                break

    # ---- Opportunity cost on capital that is NOT earning the risk-free rate ----
    # Two forgone-yield buckets:
    #   (a) Polymarket capital: USDC locked in YES contracts pays no interest;
    #       a treasury would have earned ``insurance_cost * duration_days * rfr``.
    #   (b) Idle wallet: between positions, the wallet sits in USDC/ETH; the USDC
    #       leg could be in a money-market fund instead. Use snapshot-derived
    #       hourly idle USDC (when ``current_pos`` is None in the simulator the
    #       snapshot's ``strategy_usd`` equals the wallet value, so we read it
    #       directly).
    rfr = max(float(risk_free_rate_apy or 0.0), 0.0)
    poly_opp_cost = 0.0
    idle_opp_cost = 0.0
    if rfr > 0.0:
        for p in positions:
            dur_yrs = max(float(p.get("duration_hours") or 0.0), 0.0) / (24.0 * 365.0)
            poly_opp_cost += float(p.get("insurance_cost", 0.0)) * dur_yrs * rfr
        # Idle wallet: integrate hourly snapshots that are *between* positions.
        # snapshot["strategy_usd"] == wallet value when no position is open.
        if snapshots:
            in_position_intervals = [
                (int(p["open_ts"]), int(p["close_ts"])) for p in positions
            ]
            # Sort once; cheap O(n log n).
            in_position_intervals.sort()
            j = 0
            for snap in snapshots:
                ts = int(snap["ts"])
                # Advance j past intervals that ended at or before ts.
                while j < len(in_position_intervals) and in_position_intervals[j][1] <= ts:
                    j += 1
                inside = (
                    j < len(in_position_intervals)
                    and in_position_intervals[j][0] <= ts < in_position_intervals[j][1]
                )
                if inside:
                    continue
                # 1 hour of idle capital = strategy_usd / (24*365) yrs.
                idle_opp_cost += float(snap.get("strategy_usd", 0.0)) * (1.0 / (24.0 * 365.0)) * rfr
    total_opportunity_cost = poly_opp_cost + idle_opp_cost

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
    if restore_to_anchor_on and anchor_usdc_meta is not None and anchor_eth_meta is not None:
        # Restore-to-anchor headline accounting:
        # - Money out (cost basis) = investment + gas + premium. (Restore swap
        #   fees are NOT added separately because they are already netted into
        #   ``cumulative_unfilled_usd`` via the per-close swap accounting.)
        # - Money in / out at end (final value) = anchor value at final price
        #   plus the cumulative surplus/deficit we tracked across rounds.
        # Insurance proceeds DO show up — they are baked into each round's
        # ``restore_unfilled_usd`` (since the restore swap consumed payout +
        # sellback as available USDC). So we do NOT add them here a second time.
        cost_basis = investment + total_gas_fees + total_ins_cost
        anchor_value_at_final = float(anchor_usdc_meta) + float(anchor_eth_meta) * final_price
        final_total_value = anchor_value_at_final + cumulative_unfilled_usd
    else:
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
            "polymarket_fee_buy_usdc": round(p.get("polymarket_fee_buy", 0.0), 4),
            "polymarket_fee_sell_usdc": round(p.get("polymarket_fee_sell", 0.0), 4),
            "polymarket_fee_total_usdc": round(
                p.get("polymarket_fee_buy", 0.0) + p.get("polymarket_fee_sell", 0.0),
                4,
            ),
            "insurance_cost_usdc": round(p["insurance_cost"], 2),
            "insurance_payout_usdc": round(p["insurance_payout"], 2),
            "insurance_sellback_usdc": round(p.get("insurance_sellback", 0), 2),
            "insurance_net_usdc": round(p["insurance_net"], 2),
            "touched_lower": p.get("touched_lower", False),
            "touched_upper": p.get("touched_upper", False),
            # Per-leg market metadata so a downstream auditor can
            # reproduce which Polymarket markets were actually used at
            # each open and check their depth (cumulative USD volume).
            "lower_clob_token_id": p.get("lower_clob_token_id"),
            "upper_clob_token_id": p.get("upper_clob_token_id"),
            "lower_market_volume_usd": round(float(p.get("lower_market_volume") or 0.0), 2),
            "upper_market_volume_usd": round(float(p.get("upper_market_volume") or 0.0), 2),
            # Pool-dilution telemetry. ``avg_dilution_factor`` is the mean
            # multiplier applied to the historical fee-growth (1.0 == no
            # dilution, 0.5 == fees halved). ``avg_pool_share`` is the
            # complementary "us / (us + pool)" share. Both averaged across
            # in-position hours.
            "avg_dilution_factor": (
                round(p["dilution_factor_sum"] / p["dilution_sample_count"], 6)
                if p.get("dilution_sample_count") else None
            ),
            "avg_pool_share": (
                round(p["our_L_share_sum"] / p["dilution_sample_count"], 6)
                if p.get("dilution_sample_count") else None
            ),
            "avg_pool_active_liquidity": (
                round(p["pool_L_sample_sum"] / p["dilution_sample_count"], 4)
                if p.get("dilution_sample_count") else None
            ),
            # Restore-to-anchor extras (zero / null when restore mode is off).
            "anchor_usdc": (round(float(p["anchor_usdc"]), 2) if p.get("anchor_usdc") is not None else None),
            "anchor_eth": (round(float(p["anchor_eth"]), 6) if p.get("anchor_eth") is not None else None),
            "restore_cost_lower_usdc": round(p.get("restore_cost_lower", 0.0) or 0.0, 2),
            "restore_cost_upper_usdc": round(p.get("restore_cost_upper", 0.0) or 0.0, 2),
            "restore_swap_amount_usd": round(p.get("restore_swap_amount", 0.0) or 0.0, 2),
            "restore_swap_fee_usd": round(p.get("restore_swap_fee", 0.0) or 0.0, 2),
            "restore_unfilled_usd": round(p.get("restore_unfilled_usd", 0.0) or 0.0, 2),
            "wallet_vs_anchor_usd": round(p.get("wallet_vs_anchor_usd", 0.0) or 0.0, 2),
            "wallet_at_close_pre_restore": (
                {
                    t0_sym: round(p["wallet_at_close_pre_restore"]["usdc"], 2),
                    t1_sym: round(p["wallet_at_close_pre_restore"]["eth"], 6),
                    "value_usd": round(p["wallet_at_close_pre_restore"]["value_usd"], 2),
                } if p.get("wallet_at_close_pre_restore") else None
            ),
            "wallet_after_restore": (
                {
                    t0_sym: round(p["wallet_after_restore"]["usdc"], 2),
                    t1_sym: round(p["wallet_after_restore"]["eth"], 6),
                    "value_usd": round(p["wallet_after_restore"]["value_usd"], 2),
                } if p.get("wallet_after_restore") else None
            ),
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
            if total_days > 0 and unhedged_strategy_value > 0 and unhedged_cost_basis > 0 else 0
        )
    except (OverflowError, ZeroDivisionError):
        unhedged_apy = float("inf") if unhedged_strategy_value > unhedged_cost_basis else float("-inf")

    hodl_final_value = initial_usdc + initial_eth * final_price
    hodl_roi_pct = (hodl_final_value / investment - 1) * 100 if investment else 0
    try:
        hodl_apy = ((hodl_final_value / investment) ** (365 / total_days) - 1) * 100 if total_days > 0 and hodl_final_value > 0 else 0
    except OverflowError:
        hodl_apy = float("inf") if hodl_final_value > investment else float("-inf")

    # ------------------------------------------------------------------
    # REAL-CASH-TERMS metric (the honest "did I make money?" number).
    # ------------------------------------------------------------------
    # The simulator uses external-cost accounting: insurance proceeds
    # (payouts + sellback) are added INTO the wallet at close, but the
    # premium PAID is never subtracted from the wallet. So the headline
    # "wallet vs HODL" silently treats premium as funded from a separate
    # pocket. To answer "would my real cash beat HODL?" we have to
    # subtract the gross premium back out:
    #
    #   real_cash_final = sim_wallet_final - gross_premium_paid
    #                                       + gas_total (gas already inside?)
    #
    # gas in this codebase is *also* external (paid outside the wallet),
    # so total external outflow = total_ins_cost + total_gas_fees.
    # Restore swap fees are inside-wallet (deducted at restore time), so
    # we don't subtract them here. Insurance proceeds are inside-wallet
    # too (already in final_value), so we don't add them here either.
    real_cash_final_value = final_value - total_ins_cost - total_gas_fees
    real_cash_vs_hodl_usd = real_cash_final_value - hodl_final_value
    real_cash_roi_pct = (
        (real_cash_final_value / investment - 1) * 100 if investment else 0
    )
    try:
        real_cash_apy = (
            ((real_cash_final_value / investment) ** (365 / total_days) - 1) * 100
            if total_days > 0 and real_cash_final_value > 0 and investment > 0 else
            (float("-inf") if real_cash_final_value <= 0 else 0)
        )
    except (OverflowError, ZeroDivisionError):
        real_cash_apy = float("-inf") if real_cash_final_value <= 0 else (
            float("inf") if real_cash_final_value > investment else float("-inf")
        )
    real_cash_outperformance_pct = real_cash_roi_pct - hodl_roi_pct

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
            "total_polymarket_taker_fees_usdc": round(total_polymarket_fees, 4),
            "opportunity_cost": {
                "risk_free_rate_apy": round(rfr, 4),
                "polymarket_capital_usdc": round(poly_opp_cost, 2),
                "idle_wallet_usdc": round(idle_opp_cost, 2),
                "total_usdc": round(total_opportunity_cost, 2),
                "note": "Forgone risk-free yield on Polymarket-locked capital and wallet cash between positions. Subtracted from ROI/APY in the *_net_of_opp_cost fields below.",
            },
            "roi_pct_net_of_opp_cost": round(
                (final_total_value / cost_basis - 1) * 100 - (total_opportunity_cost / cost_basis * 100)
                if cost_basis else 0.0,
                2,
            ),
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
            # ----------------------------------------------------------
            # REAL CASH TERMS — the honest answer.
            # The sim's "Wallet vs HODL" silently treats insurance premium
            # as funded from a separate pocket (external-cost accounting).
            # In real life premium is YOUR cash. This block subtracts it back
            # so the operator sees the actual P&L they would experience.
            # ----------------------------------------------------------
            "real_cash_terms": {
                "real_cash_final_value_usd": round(real_cash_final_value, 2),
                "real_cash_roi_pct": round(real_cash_roi_pct, 2),
                "real_cash_apy_pct": round(real_cash_apy, 2) if real_cash_apy not in (float("inf"), float("-inf")) else None,
                "real_cash_vs_hodl_usd": round(real_cash_vs_hodl_usd, 2),
                "real_cash_vs_hodl_pct": round(real_cash_outperformance_pct, 2),
                "gross_premium_paid_usd": round(total_ins_cost, 2),
                "gas_paid_external_usd": round(total_gas_fees, 2),
                "note": (
                    "real_cash_final = sim_wallet_final - gross_premium - gas. "
                    "Subtracts the external-cost outflows (insurance premium "
                    "and gas) the sim does not deduct from the LP wallet. "
                    "This is the metric to use when judging whether the "
                    "strategy actually makes money on the same starting cash "
                    "as a HODLer would have."
                ),
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
        "restore_to_anchor": {
            "enabled": restore_to_anchor_on,
            "hedge_sizing_mode": hedge_sizing_mode_meta,
            "hedge_lp_fee_credit_pct": round(hedge_lp_fee_credit_pct_meta, 4),
            "pool_swap_fee_rate": round(pool_swap_fee_rate_meta, 6),
            "anchor": (
                {
                    t0_sym: round(float(anchor_usdc_meta), 2),
                    t1_sym: round(float(anchor_eth_meta), 6),
                    "value_at_entry_usd": round(
                        float(anchor_usdc_meta) + float(anchor_eth_meta) * entry_price, 2,
                    ),
                    "value_at_final_usd": round(
                        float(anchor_usdc_meta) + float(anchor_eth_meta) * final_price, 2,
                    ),
                }
                if anchor_usdc_meta is not None and anchor_eth_meta is not None
                else None
            ),
            "cumulative_unfilled_usd": round(cumulative_unfilled_usd, 2),
            "total_restore_swap_fees_usd": round(total_restore_swap_fees, 2),
            "total_restore_swap_amount_usd": round(total_restore_swap_amount, 2),
            "final_restore": (
                {
                    "swap_amount_usd": round(float(final_restore_meta["swap_amount_usd"]), 2),
                    "swap_fee_usd": round(float(final_restore_meta["swap_fee_usd"]), 2),
                    "unfilled_usd": round(float(final_restore_meta["unfilled_usd"]), 2),
                    "wallet_after": final_restore_meta.get("wallet_after"),
                }
                if final_restore_meta else None
            ),
            "headline_roi_pct": round(roi_pct, 2) if restore_to_anchor_on else None,
            "headline_final_value_usd": round(final_total_value, 2) if restore_to_anchor_on else None,
            "note": (
                "headline ROI uses anchor_value_at_final + cumulative_unfilled_usd "
                "(insurance proceeds are netted into per-close restore_unfilled_usd)."
            ),
        },
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
    restrict_to_touch_markets: bool = False,
    polymarket_fee_category: str = "crypto",
    polymarket_fees_enabled: bool = True,
) -> List[Dict]:
    """Run simulation for every valid Polymarket range combo, return ranked results."""
    get_range_combinations = _get_db_func("get_range_combinations")
    first_ts = int(candles[0]["periodStartUnix"])
    # Use markets valid at the backtest start timestamp (not only today's active markets).
    all_combos = get_range_combinations(
        token_symbol, conn, candle_ts=first_ts,
        restrict_to_touch_markets=restrict_to_touch_markets,
    )
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
                restrict_to_touch_markets=restrict_to_touch_markets,
                polymarket_fee_category=polymarket_fee_category,
                polymarket_fees_enabled=polymarket_fees_enabled,
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
    close_policy: ClosePolicy = str(bt.get("close_policy", "pessimistic"))  # type: ignore[assignment]
    touch_settlement_haircut = float(bt.get("touch_settlement_haircut", 0.03) or 0.0)
    sell_touched_at_market = bool(bt.get("sell_touched_at_market", True))
    risk_free_rate_apy = float(bt.get("risk_free_rate_apy", 0.045) or 0.0)
    perp_funding_rate_apy = float(bt.get("perp_funding_rate_apy", 0.10) or 0.0)
    gas_strict = bool(bt.get("gas_strict", True))
    priority_fee_gwei = float(bt.get("priority_fee_gwei", 2.0) or 0.0)
    restrict_to_touch_markets = bool(bt.get("restrict_to_touch_markets", True))
    polymarket_fee_category = str(bt.get("polymarket_fee_category", "crypto") or "crypto").lower()
    polymarket_fees_enabled = bool(bt.get("polymarket_fees_enabled", True))
    # External-cost accounting is always ON (no config flag).

    # Experiment matrix knobs (all optional — null/default keeps legacy behaviour).
    selection_cfg: Dict[str, Any] = {}
    _yes_cap = bt.get("range_yes_cap")
    if _yes_cap is not None:
        selection_cfg["range_yes_cap"] = float(_yes_cap)
    _min_tte = bt.get("min_hedge_tte_hours")
    if _min_tte is not None:
        selection_cfg["min_hedge_tte_hours"] = float(_min_tte)
    _objective = bt.get("selection_objective")
    if _objective:
        selection_cfg["selection_objective"] = str(_objective)
    _fixed_w = bt.get("fixed_range_pct")
    if _fixed_w is not None:
        selection_cfg["fixed_range_pct"] = float(_fixed_w)
    _buffer = bt.get("range_buffer_pct")
    if _buffer is not None:
        selection_cfg["range_buffer_pct"] = float(_buffer)
    _max_w = bt.get("range_max_width_pct")
    if _max_w is not None:
        selection_cfg["range_max_width_pct"] = float(_max_w)
    if bool(bt.get("bypass_insurance", False)):
        selection_cfg["bypass_insurance"] = True
    if bool(bt.get("relax_filters_when_empty", False)):
        selection_cfg["relax_filters_when_empty"] = True
    # Depth-aware market selection (default-on): refuse ghost markets and
    # let pick_best_range / get_candidate_markets order by depth.
    _min_vol = bt.get("min_market_volume_usd")
    if _min_vol is None:
        _min_vol = 1000.0  # ghost-market floor
    selection_cfg["min_market_volume"] = float(_min_vol or 0.0)
    selection_cfg["restrict_to_touch_markets"] = bool(restrict_to_touch_markets)
    selection_cfg["slippage_per_1k_default"] = float(slippage_per_1k or 0.02)
    selection_cfg["spread"] = float(spread or 0.0)
    selection_cfg = selection_cfg or None
    if selection_cfg:
        logger.info("Selection knobs active: %s", selection_cfg)

    # Restore-to-anchor knobs (defaults preserve legacy behaviour).
    restore_to_anchor = bool(bt.get("restore_to_anchor", False))
    hedge_sizing_mode = str(bt.get("hedge_sizing_mode", "il_only") or "il_only")
    hedge_lp_fee_credit_pct = float(bt.get("hedge_lp_fee_credit_pct", 0.0) or 0.0)
    final_restore_at_end = bool(bt.get("final_restore_at_end", False))
    _mih_raw = bt.get("max_idle_hours")
    max_idle_hours = float(_mih_raw) if (_mih_raw is not None and float(_mih_raw) > 0) else None
    require_full_insurance = bool(bt.get("require_full_insurance", False))
    if require_full_insurance:
        logger.info("require_full_insurance ON — positions with insurance_cost=$0 will be skipped")
    _tp_raw = bt.get("take_profit_yes_multiplier")
    take_profit_yes_multiplier = (
        float(_tp_raw) if (_tp_raw is not None and float(_tp_raw) > 0) else None
    )
    if take_profit_yes_multiplier is not None:
        logger.info(
            "take_profit_yes_multiplier=%.2fx — each YES leg is hourly MTM-checked; "
            "sell when sell-side proceeds >= %.2fx of buy cost.",
            take_profit_yes_multiplier, take_profit_yes_multiplier,
        )
    # ----- Phase 30 vol-regime knobs -----
    vol_regime_toggle = bool(bt.get("vol_regime_toggle", False))
    vol_regime_threshold_pct = bt.get("vol_regime_threshold_pct")
    if vol_regime_threshold_pct is not None:
        try:
            vol_regime_threshold_pct = float(vol_regime_threshold_pct)
        except (TypeError, ValueError):
            vol_regime_threshold_pct = None
    vol_regime_lookback_hours = int(bt.get("vol_regime_lookback_hours", 24) or 24)
    vol_regime_forecast_mode = str(bt.get("vol_regime_forecast_mode", "perfect"))
    if vol_regime_toggle:
        logger.info(
            "vol_regime_toggle ON: %s-forecast hourly-stddev over next/last %dh; "
            "skip cycle when realised vol > %.3f%%.",
            vol_regime_forecast_mode, vol_regime_lookback_hours,
            vol_regime_threshold_pct or float("nan"),
        )
    conditional_hedging = bool(bt.get("conditional_hedging", False))
    conditional_hedging_threshold_pct = bt.get("conditional_hedging_threshold_pct")
    if conditional_hedging_threshold_pct is not None:
        try:
            conditional_hedging_threshold_pct = float(conditional_hedging_threshold_pct)
        except (TypeError, ValueError):
            conditional_hedging_threshold_pct = None
    conditional_hedging_lookback_hours = int(bt.get("conditional_hedging_lookback_hours", 168) or 168)
    conditional_hedging_forecast_mode = str(bt.get("conditional_hedging_forecast_mode", "perfect"))
    if conditional_hedging:
        logger.info(
            "conditional_hedging ON: %s-forecast hourly-stddev over next/last %dh; "
            "bypass insurance when realised vol <= %.3f%%.",
            conditional_hedging_forecast_mode, conditional_hedging_lookback_hours,
            conditional_hedging_threshold_pct or float("nan"),
        )
    # ----- Phase 31 Beefy ConcLiq knobs -----
    symmetric_range_pct = bt.get("symmetric_range_pct")
    if symmetric_range_pct is not None:
        try:
            symmetric_range_pct = float(symmetric_range_pct)
            if symmetric_range_pct <= 0:
                symmetric_range_pct = None
        except (TypeError, ValueError):
            symmetric_range_pct = None
    if symmetric_range_pct is not None:
        logger.info(
            "symmetric_range_pct=%.2f%% — Beefy ConcLiq mode: each open builds a "
            "synthetic [P*(1-w/2), P*(1+w/2)] range. Requires bypass_insurance=True.",
            symmetric_range_pct,
        )
    alt_fee_uplift_pct = float(bt.get("alt_fee_uplift_pct", 0.0) or 0.0)
    if alt_fee_uplift_pct > 0:
        logger.info(
            "alt_fee_uplift_pct=%.1f%% — boost main-range LP fees to approximate "
            "the Beefy alt single-sided position's contribution.",
            alt_fee_uplift_pct,
        )
    if restore_to_anchor:
        logger.info(
            "Restore-to-anchor mode ON (hedge_sizing_mode=%s, lp_fee_credit_pct=%.2f, final_restore=%s)",
            hedge_sizing_mode, hedge_lp_fee_credit_pct, final_restore_at_end or True,
        )

    telemetry_cfg = bt.get("telemetry") or {}
    telemetry_enabled = bool(telemetry_cfg.get("enabled", False))
    telemetry_path = telemetry_cfg.get("path")

    output_path = str(bt.get("output_json", "active_backtest_results.json"))

    get_db_connection = _get_db_func("get_db_connection")

    logger.info(f"Fetching pool metadata for {pool}...")
    pool_data = fetch_pool_metadata(pool)
    detected_symbol, detected_price_token = detect_pool_orientation(pool_data)
    # If the user pinned price_token in config, respect it; otherwise use the
    # auto-detected orientation so e.g. WBTC/USDC (token0=WBTC) works without
    # the caller having to know the token order.
    if bt.get("price_token") is None:
        price_token = detected_price_token
    token_symbol = detected_symbol
    logger.info(
        "Pool: %s/%s -> volatile=%s  price_token=%d (auto-detected)",
        pool_data["token0"]["symbol"],
        pool_data["token1"]["symbol"],
        token_symbol,
        price_token,
    )

    # Allow a fixed window end via config to keep sweeps deterministic across
    # subprocess invocations (without this each child has its own ``now()``
    # and the 180-day window slides between configs in the same sweep,
    # invalidating apples-to-apples comparisons).
    _pinned_end_ts = bt.get("simulation_end_ts")
    if _pinned_end_ts is not None:
        try:
            end_ts = int(_pinned_end_ts)
            now = datetime.fromtimestamp(end_ts, tz=timezone.utc)
            logger.info(
                "simulation_end_ts pinned via config: %s (epoch %d)",
                now.isoformat(), end_ts,
            )
        except (TypeError, ValueError):
            now = datetime.now(timezone.utc)
            end_ts = int(now.timestamp())
    else:
        now = datetime.now(timezone.utc)
        end_ts = int(now.timestamp())
    total_fetch_days = days + lookback_days
    start_ts = int((now - timedelta(days=total_fetch_days)).timestamp())

    start_date_str = (now - timedelta(days=total_fetch_days)).strftime("%Y-%m-%d")
    end_date_str = now.strftime("%Y-%m-%d")
    logger.info("Fetching historical gas prices via RPC block sampling...")
    gas_prices = fetch_daily_gas_prices(start_date_str, end_date_str)
    # Inject priority fee + strict-mode preferences so downstream gas_cost_usd
    # calls behave consistently without having to thread two extra params.
    if isinstance(gas_prices, dict):
        gas_prices["__priority_fee_gwei__"] = priority_fee_gwei
        gas_prices["__strict__"] = bool(gas_strict)

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

    # Pure LP replay (Phase 31 Beefy): synthetic symmetric range + bypass_insurance never
    # touches Polymarket tables — skip Postgres so installs without a polymarket DB still run.
    _beefy_no_poly = (
        not sweep
        and symmetric_range_pct is not None
        and isinstance(selection_cfg, dict)
        and bool(selection_cfg.get("bypass_insurance"))
    )
    if _beefy_no_poly:
        conn = None
        logger.info(
            "Skipping PostgreSQL connection (symmetric_range_pct + bypass_insurance); "
            "Polymarket/insurance leg is disabled for this config.",
        )
    else:
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
                restrict_to_touch_markets=restrict_to_touch_markets,
                polymarket_fee_category=polymarket_fee_category,
                polymarket_fees_enabled=polymarket_fees_enabled,
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
                touch_settlement_haircut=touch_settlement_haircut,
                sell_touched_at_market=sell_touched_at_market,
                restrict_to_touch_markets=restrict_to_touch_markets,
                polymarket_fee_category=polymarket_fee_category,
                polymarket_fees_enabled=polymarket_fees_enabled,
                selection_cfg=selection_cfg,
                restore_to_anchor=restore_to_anchor,
                hedge_sizing_mode=hedge_sizing_mode,
                hedge_lp_fee_credit_pct=hedge_lp_fee_credit_pct,
                final_restore_at_end=final_restore_at_end,
                max_idle_hours=max_idle_hours,
                require_full_insurance=require_full_insurance,
                take_profit_yes_multiplier=take_profit_yes_multiplier,
                vol_regime_toggle=vol_regime_toggle,
                vol_regime_threshold_pct=vol_regime_threshold_pct,
                vol_regime_lookback_hours=vol_regime_lookback_hours,
                vol_regime_forecast_mode=vol_regime_forecast_mode,
                conditional_hedging=conditional_hedging,
                conditional_hedging_threshold_pct=conditional_hedging_threshold_pct,
                conditional_hedging_lookback_hours=conditional_hedging_lookback_hours,
                conditional_hedging_forecast_mode=conditional_hedging_forecast_mode,
                symmetric_range_pct=symmetric_range_pct,
                alt_fee_uplift_pct=alt_fee_uplift_pct,
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
                "polymarket_fee_category": polymarket_fee_category,
                "polymarket_fees_enabled": polymarket_fees_enabled,
                "selection_mode": "sweep",
                "capital_model": "eth_first",
                "initial_eth": float(initial_eth),
                "initial_usdc": float(initial_usdc) if initial_usdc is not None else None,
                "external_costs": True,
                "incomplete": False,
                "warnings": [],
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "sweep_table_path": sweep_out,
                "restore_to_anchor": restore_to_anchor,
                "hedge_sizing_mode": hedge_sizing_mode,
                "hedge_lp_fee_credit_pct": hedge_lp_fee_credit_pct,
                "final_restore_at_end": final_restore_at_end or restore_to_anchor,
            }
            summary = build_summary(
                positions, candles, 0.0, pool, token_symbol, final_wallet, price_token,
                snapshots=hourly_snapshots,
                data_quality=data_quality,
                run_metadata=run_metadata,
                risk_free_rate_apy=risk_free_rate_apy,
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
                touch_settlement_haircut=touch_settlement_haircut,
                sell_touched_at_market=sell_touched_at_market,
                restrict_to_touch_markets=restrict_to_touch_markets,
                polymarket_fee_category=polymarket_fee_category,
                polymarket_fees_enabled=polymarket_fees_enabled,
                selection_cfg=selection_cfg,
                restore_to_anchor=restore_to_anchor,
                hedge_sizing_mode=hedge_sizing_mode,
                hedge_lp_fee_credit_pct=hedge_lp_fee_credit_pct,
                final_restore_at_end=final_restore_at_end,
                max_idle_hours=max_idle_hours,
                require_full_insurance=require_full_insurance,
                take_profit_yes_multiplier=take_profit_yes_multiplier,
                vol_regime_toggle=vol_regime_toggle,
                vol_regime_threshold_pct=vol_regime_threshold_pct,
                vol_regime_lookback_hours=vol_regime_lookback_hours,
                vol_regime_forecast_mode=vol_regime_forecast_mode,
                conditional_hedging=conditional_hedging,
                conditional_hedging_threshold_pct=conditional_hedging_threshold_pct,
                conditional_hedging_lookback_hours=conditional_hedging_lookback_hours,
                conditional_hedging_forecast_mode=conditional_hedging_forecast_mode,
                symmetric_range_pct=symmetric_range_pct,
                alt_fee_uplift_pct=alt_fee_uplift_pct,
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
                "polymarket_fee_category": polymarket_fee_category,
                "polymarket_fees_enabled": polymarket_fees_enabled,
                "selection_mode": "lookback" if warmup_candles else ("fixed" if fixed_range else "heuristic"),
                "capital_model": "eth_first",
                "initial_eth": float(initial_eth),
                "initial_usdc": float(initial_usdc) if initial_usdc is not None else None,
                "external_costs": True,
                "incomplete": incomplete,
                "warnings": warnings,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "selection_cfg": selection_cfg,
                "restore_to_anchor": restore_to_anchor,
                "hedge_sizing_mode": hedge_sizing_mode,
                "hedge_lp_fee_credit_pct": hedge_lp_fee_credit_pct,
                "final_restore_at_end": final_restore_at_end or restore_to_anchor,
            }
            summary = build_summary(
                positions, candles, 0.0, pool, token_symbol, final_wallet, price_token,
                snapshots=hourly_snapshots,
                data_quality=data_quality,
                run_metadata=run_metadata,
                risk_free_rate_apy=risk_free_rate_apy,
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
        if conn is not None:
            conn.close()

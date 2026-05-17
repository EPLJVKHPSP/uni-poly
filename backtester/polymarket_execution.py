"""Polymarket execution and close-policy math.

Pure functions only: no DB, no I/O. Keep unit-consistent with the rest of the
repo: Polymarket prices are in USD per contract (0..1).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Literal, Optional

ClosePolicy = Literal["touch", "next_candle", "pessimistic"]


# ---------------------------------------------------------------------------
# Polymarket dynamic taker-fee model
# ---------------------------------------------------------------------------
#
# As of 2026 Polymarket charges a probability-weighted *taker* fee.
# The official formula (docs.polymarket.com/trading/fees) is::
#
#     fee_usd = C × feeRate × p × (1 - p)
#
# where C is the number of shares, p is the trade price (0..1), and
# ``feeRate`` is a per-category constant.
#
# We keep an ``exponent`` field on PolymarketFeeModel for forward-compat
# (the on-chain ``info.fd`` blob exposes ``e`` separately and Polymarket
# could in principle change it), but every published category currently
# uses exponent = 1. Setting exp=1 reduces ``(p×(1−p))^exp`` to the
# linear ``p×(1−p)`` form documented above.
#
# Verified against the published per-category fee tables for 100 shares:
#   Crypto    feeRate=0.07  → peak $1.75 @ p=0.5; $0.63 @ p=0.10
#   Sports    feeRate=0.03  → peak $0.75 @ p=0.5
#   Finance/Politics/Mentions/Tech feeRate=0.04 → peak $1.00 @ p=0.5
#   Economics/Culture/Weather/Other feeRate=0.05 → peak $1.25 @ p=0.5
#
# Makers pay 0 (and receive rebates) but our hedge uses taker fills at
# open/close, so we charge the full taker rate. Geopolitics is fee-free.
#
# The p × (1 - p) term peaks at p = 0.5 and decays to ~0 at the extremes,
# which is convenient for our hedge: the legs we actually trade are usually
# at p ≤ 0.20, where the effective taker rate is far below the headline 1.75%.

POLYMARKET_FEE_TABLE: Dict[str, tuple[float, float]] = {
    # category -> (feeRate, exponent)   per docs.polymarket.com/trading/fees
    "crypto":      (0.07, 1.0),   # peak $1.75 / 100 sh @ p=0.5
    "sports":      (0.03, 1.0),   # peak $0.75 / 100 sh
    "finance":     (0.04, 1.0),   # peak $1.00 / 100 sh
    "politics":    (0.04, 1.0),
    "mentions":    (0.04, 1.0),
    "tech":        (0.04, 1.0),
    "economics":   (0.05, 1.0),   # peak $1.25 / 100 sh
    "culture":     (0.05, 1.0),
    "weather":     (0.05, 1.0),
    "other":       (0.05, 1.0),
    "general":     (0.05, 1.0),
    "geopolitics": (0.0,  1.0),   # fee-free
}

MIN_FEE_USD = 1e-5  # Polymarket rounds anything below 0.00001 USDC to zero.


@dataclass(frozen=True)
class PolymarketFeeModel:
    """Polymarket per-category taker-fee parameters.

    Defaults to the Crypto category (where our LP hedge lives). The model is
    intentionally a thin wrapper around the published formula so we can swap
    in updated values without touching the math.
    """

    category: str = "crypto"
    fee_rate: float = 0.07
    exponent: float = 1.0
    enabled: bool = True

    @classmethod
    def for_category(cls, category: str) -> "PolymarketFeeModel":
        cat = (category or "crypto").lower()
        if cat not in POLYMARKET_FEE_TABLE:
            cat = "crypto"
        rate, exp = POLYMARKET_FEE_TABLE[cat]
        return cls(category=cat, fee_rate=rate, exponent=exp, enabled=rate > 0.0)


def polymarket_taker_fee_usd(
    contracts: float,
    price: float,
    model: Optional[PolymarketFeeModel],
) -> float:
    """USD taker fee for a fill of ``contracts`` shares at ``price``.

    Returns 0 when the model is disabled, contracts ≤ 0, or the computed fee
    is below Polymarket's $0.00001 dust threshold.
    """
    if model is None or not model.enabled:
        return 0.0
    n = max(float(contracts or 0.0), 0.0)
    p = max(min(float(price or 0.0), 1.0), 0.0)
    if n <= 0.0 or p <= 0.0 or p >= 1.0:
        return 0.0
    base = (p * (1.0 - p)) ** float(model.exponent)
    fee = n * float(model.fee_rate) * base
    return fee if fee >= MIN_FEE_USD else 0.0


def polymarket_fee_per_contract(
    price: float,
    model: Optional[PolymarketFeeModel],
) -> float:
    """USD taker fee per single contract at ``price`` (used to adjust exec px)."""
    if model is None or not model.enabled:
        return 0.0
    p = max(min(float(price or 0.0), 1.0), 0.0)
    if p <= 0.0 or p >= 1.0:
        return 0.0
    base = (p * (1.0 - p)) ** float(model.exponent)
    return float(model.fee_rate) * base


@dataclass(frozen=True)
class SlippageConfig:
    """Size-aware execution penalty for Polymarket.

    The model is intentionally simple because historical depth is unreliable.

    - ``per_1k_contracts``: additional USD *per contract* at 1,000 contracts.
      The per-contract penalty scales linearly with size: impact_per_contract =
      per_1k_contracts * (contracts / 1000).
    - ``max_per_contract``: cap to avoid nonsensical impacts.
    - ``per_asset``: optional ``{asset_id -> per_1k_contracts}`` override. When
      a caller passes ``asset_id`` to ``apply_execution_costs`` and we have a
      fitted impact for that asset, we use it instead of the global default.
      This lets us replace the flat ``spread`` with a per-market slippage
      curve fitted from realized trade prints (see ``backtester.slippage_fit``).
    """

    per_1k_contracts: float = 0.0
    max_per_contract: float = 0.0
    per_asset: Dict[str, float] = field(default_factory=dict)

    @property
    def enabled(self) -> bool:
        return (self.per_1k_contracts or 0.0) > 0.0 and (self.max_per_contract or 0.0) >= 0.0

    def per_1k_for(self, asset_id: Optional[str]) -> float:
        """Return the best-known per_1k for ``asset_id`` (else the default)."""
        if asset_id and self.per_asset:
            override = self.per_asset.get(asset_id)
            if override is not None:
                return float(override)
        return float(self.per_1k_contracts or 0.0)


def slippage_per_contract_usd(
    contracts: float,
    cfg: Optional[SlippageConfig],
    asset_id: Optional[str] = None,
) -> float:
    """Return additional execution penalty per contract (USD/contract)."""
    if cfg is None:
        return 0.0
    per_1k = cfg.per_1k_for(asset_id)
    if per_1k <= 0.0:
        return 0.0
    n = max(float(contracts or 0.0), 0.0)
    impact = per_1k * (n / 1000.0)
    cap = float(cfg.max_per_contract or 0.0)
    if cap > 0.0:
        impact = min(impact, cap)
    return max(impact, 0.0)


def _best_of_levels(levels) -> Optional[float]:
    """Return the best (top-of-book) price from a level list, or None if empty."""
    if not levels:
        return None
    for lev in levels:
        if isinstance(lev, dict):
            p, s = float(lev["price"]), float(lev["size"])
        else:
            p, s = float(lev[0]), float(lev[1])
        if s > 0.0:
            return p
    return None


def _vwap_walk(levels, contracts: float) -> tuple[Optional[float], float]:
    """Walk a level list filling ``contracts`` and return (vwap, filled).

    Levels must be sorted best-to-worst (asks ascending, bids descending).
    Returns (None, 0.0) if no liquidity at all.
    """
    n = max(float(contracts or 0.0), 0.0)
    if n <= 0.0 or not levels:
        return (None, 0.0)
    remaining = n
    notional = 0.0
    filled = 0.0
    for lev in levels:
        if isinstance(lev, dict):
            p, s = float(lev["price"]), float(lev["size"])
        else:
            p, s = float(lev[0]), float(lev[1])
        if s <= 0.0:
            continue
        take = min(s, remaining)
        notional += p * take
        filled += take
        remaining -= take
        if remaining <= 1e-12:
            break
    if filled <= 0.0:
        return (None, 0.0)
    return (notional / filled, filled)


def apply_execution_costs(
    *,
    mid_price: float,
    spread: float,
    contracts: float,
    side: Literal["buy", "sell"],
    slippage_cfg: Optional[SlippageConfig],
    asset_id: Optional[str] = None,
    fee_model: Optional[PolymarketFeeModel] = None,
    book_bids: Optional[list] = None,
    book_asks: Optional[list] = None,
) -> tuple[float, float, float]:
    """Compute executable price and cost decomposition.

    Returns ``(exec_price, spread_cost_usd, slippage_cost_usd)``.

    - **spread_cost_usd**: difference between mid and (bid/ask), as a USD cost.
    - **slippage_cost_usd**: size-aware penalty beyond the bid/ask.
    - The Polymarket dynamic taker fee (when ``fee_model`` is provided) is
      folded into ``exec_price`` so callers who only consume ``exec_price``
      see the all-in number. The fee is **also** rolled into the
      ``slippage_cost_usd`` bucket so total cost decomposition stays exact:
      ``slippage_cost`` then captures both depth impact and exchange fees.
      Use ``polymarket_taker_fee_usd`` directly if you need to break the fee
      out separately for reporting.

    Book-walk path:
      When ``book_bids`` and ``book_asks`` (or just the relevant side) are
      supplied, we *replace* the spread + fitted-slippage estimate with a
      true VWAP walk against the actual L2 ladder at trade time. The
      decomposition still cleanly splits crossing-the-spread vs depth-impact:

        - ``spread_cost = n * (best_quote - midpoint)``
        - ``slippage_cost = n * (vwap - best_quote)`` (buy) /
          ``n * (best_quote - vwap)`` (sell)

      If the requested side has no liquidity at all (or doesn't fill), we
      transparently fall back to the parametric path so the backtest never
      stalls on missing data.
    """
    mid = float(mid_price)
    spr = float(spread or 0.0)
    n = max(float(contracts or 0.0), 0.0)

    if side == "buy":
        # Book-walk path takes precedence when L2 is available.
        if book_asks and n > 0.0:
            best_ask = _best_of_levels(book_asks)
            best_bid = _best_of_levels(book_bids) if book_bids else None
            vwap, filled = _vwap_walk(book_asks, n)
            if best_ask is not None and vwap is not None and filled >= n - 1e-9:
                book_mid = (best_bid + best_ask) / 2.0 if best_bid is not None else best_ask
                spread_cost = n * (best_ask - book_mid)
                impact = max(vwap - best_ask, 0.0)
                exec_price = min(best_ask + impact, 1.0)
                fee_per = polymarket_fee_per_contract(exec_price, fee_model)
                if fee_per > 0.0:
                    exec_price = min(exec_price + fee_per, 1.0)
                slip_cost = n * (exec_price - best_ask)
                return exec_price, spread_cost, slip_cost
        ask = min(mid + spr / 2.0, 1.0)
        spread_cost = n * (ask - mid)
        impact = slippage_per_contract_usd(n, slippage_cfg, asset_id=asset_id)
        exec_price = min(ask + impact, 1.0)
        fee_per = polymarket_fee_per_contract(exec_price, fee_model)
        if fee_per > 0.0:
            exec_price = min(exec_price + fee_per, 1.0)
        slip_cost = n * (exec_price - ask)
        return exec_price, spread_cost, slip_cost

    if side == "sell":
        if book_bids and n > 0.0:
            best_bid = _best_of_levels(book_bids)
            best_ask = _best_of_levels(book_asks) if book_asks else None
            vwap, filled = _vwap_walk(book_bids, n)
            if best_bid is not None and vwap is not None and filled >= n - 1e-9:
                book_mid = (best_bid + best_ask) / 2.0 if best_ask is not None else best_bid
                spread_cost = n * (book_mid - best_bid)
                impact = max(best_bid - vwap, 0.0)
                exec_price = max(best_bid - impact, 0.0)
                fee_per = polymarket_fee_per_contract(exec_price, fee_model)
                if fee_per > 0.0:
                    exec_price = max(exec_price - fee_per, 0.0)
                slip_cost = n * (best_bid - exec_price)
                return exec_price, spread_cost, slip_cost
        bid = max(mid - spr / 2.0, 0.0)
        spread_cost = n * (mid - bid)
        impact = slippage_per_contract_usd(n, slippage_cfg, asset_id=asset_id)
        exec_price = max(bid - impact, 0.0)
        fee_per = polymarket_fee_per_contract(exec_price, fee_model)
        if fee_per > 0.0:
            exec_price = max(exec_price - fee_per, 0.0)
        slip_cost = n * (bid - exec_price)
        return exec_price, spread_cost, slip_cost

    raise ValueError(f"Unknown side={side!r}")


def choose_close_price(
    *,
    policy: ClosePolicy,
    touched_lower: bool,
    touched_upper: bool,
    min_range: float,
    max_range: float,
    candle_close_price: float,
    next_candle_close_price: Optional[float] = None,
) -> tuple[float, str]:
    """Select the close price consistent with the chosen close policy.

    Returns (close_price, source_tag).

    No-lookahead guarantee:
    - ``touch`` / ``pessimistic`` use only the current candle information.
    - ``next_candle`` requires the *next* candle close (one-step lookahead).
    """
    cp = float(candle_close_price)
    mn = float(min_range)
    mx = float(max_range)

    if not (touched_lower or touched_upper):
        return cp, "candle_close"

    if policy == "touch":
        return (mn if touched_lower else mx), "boundary"

    if policy == "pessimistic":
        boundary = mn if touched_lower else mx
        # "Worse-of" in price space under the repo's convention (USD per token):
        # lower touch -> worse is the lower price, upper touch -> worse is higher.
        if touched_lower:
            return min(boundary, cp), "worse_of_boundary_vs_close"
        return max(boundary, cp), "worse_of_boundary_vs_close"

    if policy == "next_candle":
        if next_candle_close_price is None:
            # If the run ends immediately after the touch, we cannot close on a
            # future candle. Fall back to the current close explicitly.
            return cp, "candle_close_fallback_no_next"
        return float(next_candle_close_price), "next_candle_close"

    raise ValueError(f"Unknown close policy: {policy}")


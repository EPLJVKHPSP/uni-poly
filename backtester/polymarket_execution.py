"""Polymarket execution and close-policy math.

Pure functions only: no DB, no I/O. Keep unit-consistent with the rest of the
repo: Polymarket prices are in USD per contract (0..1).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

ClosePolicy = Literal["touch", "next_candle", "pessimistic"]


@dataclass(frozen=True)
class SlippageConfig:
    """Size-aware execution penalty for Polymarket.

    The model is intentionally simple because historical depth is unreliable.

    - ``per_1k_contracts``: additional USD *per contract* at 1,000 contracts.
      The per-contract penalty scales linearly with size: impact_per_contract =
      per_1k_contracts * (contracts / 1000).
    - ``max_per_contract``: cap to avoid nonsensical impacts.
    """

    per_1k_contracts: float = 0.0
    max_per_contract: float = 0.0

    @property
    def enabled(self) -> bool:
        return (self.per_1k_contracts or 0.0) > 0.0 and (self.max_per_contract or 0.0) >= 0.0


def slippage_per_contract_usd(contracts: float, cfg: Optional[SlippageConfig]) -> float:
    """Return additional execution penalty per contract (USD/contract)."""
    if cfg is None or not cfg.enabled:
        return 0.0
    n = max(float(contracts or 0.0), 0.0)
    impact = float(cfg.per_1k_contracts) * (n / 1000.0)
    cap = float(cfg.max_per_contract or 0.0)
    if cap > 0.0:
        impact = min(impact, cap)
    return max(impact, 0.0)


def apply_execution_costs(
    *,
    mid_price: float,
    spread: float,
    contracts: float,
    side: Literal["buy", "sell"],
    slippage_cfg: Optional[SlippageConfig],
) -> tuple[float, float, float]:
    """Compute executable price and cost decomposition.

    Returns (exec_price, spread_cost_usd, slippage_cost_usd).

    - **spread_cost_usd**: difference between mid and (bid/ask), as a USD cost.
    - **slippage_cost_usd**: size-aware penalty beyond the bid/ask.
    """
    mid = float(mid_price)
    spr = float(spread or 0.0)
    n = max(float(contracts or 0.0), 0.0)

    if side == "buy":
        ask = min(mid + spr / 2.0, 1.0)
        spread_cost = n * (ask - mid)
        impact = slippage_per_contract_usd(n, slippage_cfg)
        exec_price = min(ask + impact, 1.0)
        slip_cost = n * (exec_price - ask)
        return exec_price, spread_cost, slip_cost

    if side == "sell":
        bid = max(mid - spr / 2.0, 0.0)
        spread_cost = n * (mid - bid)
        impact = slippage_per_contract_usd(n, slippage_cfg)
        exec_price = max(bid - impact, 0.0)
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


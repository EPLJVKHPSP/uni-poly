"""Position open/close lifecycle."""

import sys
from typing import Callable, Dict, List, Optional, Tuple

from il import (
    calculate_il_at_price,
    liquidity_from_tokens,
    tokens_from_liquidity,
)

from .polymarket_execution import (
    PolymarketFeeModel,
    SlippageConfig,
    apply_execution_costs,
    polymarket_taker_fee_usd,
)
from .fee_math import (
    _tokens_for_strategy_human,
    _tokens_for_strategy_scaled,
    _liquidity_for_strategy,
)
from .gas import GAS_MINT, GAS_SWAP, GAS_BURN_COLLECT, gas_cost_usd


def _get_db_func(name):
    """Resolve db_utils function via the shim module so that
    @patch("active_backtester.<name>") intercepts calls correctly."""
    shim = sys.modules.get("active_backtester")
    if shim and hasattr(shim, name):
        return getattr(shim, name)
    import db_utils
    return getattr(db_utils, name)


def open_position(
    candle: Dict,
    pool_data: Dict,
    min_range: float,
    max_range: float,
    wallet: Dict,
    insurance_info: Dict,
    price_token: int = 0,
    gas_prices: Optional[Dict[str, int]] = None,
    spread: float = 0.0,
    slippage_cfg: Optional[SlippageConfig] = None,
    lower_clob_token_id: Optional[str] = None,
    upper_clob_token_id: Optional[str] = None,
    lower_end_ts: Optional[int] = None,
    upper_end_ts: Optional[int] = None,
    fee_model: Optional[PolymarketFeeModel] = None,
    book_lookup: Optional[Callable[[Optional[str], int], Optional[Tuple[List, List]]]] = None,
) -> Dict:
    """Open a position using the full wallet. Returns position record."""
    close_price = float(candle["close"])
    entry_price = close_price if price_token == 0 else 1.0 / close_price
    ts = int(candle["periodStartUnix"])

    def _book_for(clob_id: Optional[str]) -> tuple[Optional[list], Optional[list]]:
        if book_lookup is None or not clob_id:
            return (None, None)
        try:
            res = book_lookup(clob_id, ts)
        except Exception:
            return (None, None)
        if not res:
            return (None, None)
        bids, asks = res
        return (bids, asks)

    dec0 = int(pool_data["token0"]["decimals"])
    dec1 = int(pool_data["token1"]["decimals"])
    decimal_diff = dec1 - dec0

    wallet_value = wallet["usdc"] + wallet["eth"] * entry_price
    swap_fee_rate = int(pool_data.get("feeTier", 0)) / 1_000_000
    gas_open = gas_cost_usd(GAS_MINT + GAS_SWAP, ts, entry_price, gas_prices or {})

    deposit_value = wallet_value
    lower_mid = insurance_info["lower_bet_price"]
    upper_mid = insurance_info["upper_bet_price"]
    # Executable prices include spread + optional size-aware slippage.
    # Spread cost and slippage are tracked separately for transparency.
    lower_exec_ask, lower_spread_cost, lower_slip_cost = apply_execution_costs(
        mid_price=lower_mid,
        spread=spread,
        contracts=0.0,  # placeholder; updated once contracts are known
        side="buy",
        slippage_cfg=slippage_cfg,
        fee_model=fee_model,
    )
    upper_exec_ask, upper_spread_cost, upper_slip_cost = apply_execution_costs(
        mid_price=upper_mid,
        spread=spread,
        contracts=0.0,
        side="buy",
        slippage_cfg=slippage_cfg,
        fee_model=fee_model,
    )

    # External-cost accounting is always ON:
    # - gas + Polymarket insurance are NOT deducted from LP principal
    # - only the in-pool rebalance swap fee (if any) reduces the LP deposit value
    swap_fee = 0.0
    swap_amount_usd = 0.0
    for _ in range(5):
        needed_usdc, _ = _tokens_for_strategy_human(min_range, max_range, deposit_value, entry_price)
        swap_amount_usd = abs(wallet["usdc"] - needed_usdc)
        swap_fee = swap_amount_usd * swap_fee_rate
        deposit_value = wallet_value - swap_fee
        if deposit_value <= 0:
            return None

    t0, t1 = _tokens_for_strategy_human(min_range, max_range, deposit_value, entry_price)
    il_lower = calculate_il_at_price(entry_price, t0, t1, min_range, min_range, max_range)
    il_upper = calculate_il_at_price(entry_price, t0, t1, max_range, min_range, max_range)

    lower_contracts = abs(min(0, il_lower["IL"]))
    upper_contracts = abs(min(0, il_upper["IL"]))

    # Compute insurance execution costs for the final contract sizes.
    lower_bids, lower_asks = _book_for(lower_clob_token_id)
    upper_bids, upper_asks = _book_for(upper_clob_token_id)
    lower_exec_ask, lower_spread_cost, lower_slip_cost = apply_execution_costs(
        mid_price=lower_mid,
        spread=spread,
        contracts=lower_contracts,
        side="buy",
        slippage_cfg=slippage_cfg,
        asset_id=lower_clob_token_id,
        fee_model=fee_model,
        book_bids=lower_bids,
        book_asks=lower_asks,
    )
    upper_exec_ask, upper_spread_cost, upper_slip_cost = apply_execution_costs(
        mid_price=upper_mid,
        spread=spread,
        contracts=upper_contracts,
        side="buy",
        slippage_cfg=slippage_cfg,
        asset_id=upper_clob_token_id,
        fee_model=fee_model,
        book_bids=upper_bids,
        book_asks=upper_asks,
    )
    book_used_open = bool(lower_asks) or bool(upper_asks)

    lower_cost = lower_contracts * lower_exec_ask
    upper_cost = upper_contracts * upper_exec_ask
    total_insurance_cost = lower_cost + upper_cost

    spread_cost_buy = lower_spread_cost + upper_spread_cost
    slippage_cost_buy = lower_slip_cost + upper_slip_cost
    fee_cost_buy = (
        polymarket_taker_fee_usd(lower_contracts, lower_exec_ask, fee_model)
        + polymarket_taker_fee_usd(upper_contracts, upper_exec_ask, fee_model)
    )

    token0_dep, token1_dep = _tokens_for_strategy_human(
        min_range, max_range, deposit_value, entry_price,
    )

    amt0_scaled, amt1_scaled = _tokens_for_strategy_scaled(
        min_range, max_range, deposit_value, entry_price, decimal_diff,
    )
    liquidity = _liquidity_for_strategy(
        entry_price, min_range, max_range, amt0_scaled, amt1_scaled, dec0, dec1,
    )

    # "Human-unit" liquidity: the scalar L such that token0 = L*(sqrt(P)-sqrt(Pa))
    # under this project's USDC/ETH + "price = USD per ETH" convention. Used for
    # mark-to-market and withdrawal math instead of re-applying a fresh V3 split
    # to ``deposit_value`` at a different price (which would be wrong).
    l_human = liquidity_from_tokens(
        entry_price, token0_dep, token1_dep, min_range, max_range,
    )

    return {
        "open_ts": ts,
        "entry_price": entry_price,
        "min_range": min_range,
        "max_range": max_range,
        "lower_clob_token_id": lower_clob_token_id,
        "upper_clob_token_id": upper_clob_token_id,
        "lower_end_ts": lower_end_ts,
        "upper_end_ts": upper_end_ts,
        "wallet_before": {"usdc": wallet["usdc"], "eth": wallet["eth"], "value_usd": wallet_value},
        "deposit_value": deposit_value,
        "token0_dep": token0_dep,
        "token1_dep": token1_dep,
        "liquidity": liquidity,
        "L_human": l_human,
        "dec0": dec0,
        "dec1": dec1,
        "lower_bet_price": insurance_info["lower_bet_price"],
        "upper_bet_price": insurance_info["upper_bet_price"],
        "lower_contracts": lower_contracts,
        "upper_contracts": upper_contracts,
        "lower_insurance_cost_usdc": lower_cost,
        "upper_insurance_cost_usdc": upper_cost,
        "insurance_cost": total_insurance_cost,
        "swap_fee": swap_fee,
        "swap_amount": swap_amount_usd,
        "gas_fee_open": gas_open,
        "spread_cost_buy": spread_cost_buy,
        "slippage_cost_buy": slippage_cost_buy,
        "polymarket_fee_buy": fee_cost_buy,
        "book_used_open": book_used_open,
        "accumulated_fees_usdc": 0.0,
        "accumulated_fees_eth": 0.0,
        "candle_count": 0,
    }


def close_position(
    pos: Dict,
    close_candle: Dict,
    touched_lower: bool,
    touched_upper: bool,
    price_token: int = 0,
    token_symbol: str = "ETH",
    conn=None,
    gas_prices: Optional[Dict[str, int]] = None,
    spread: float = 0.0,
    slippage_cfg: Optional[SlippageConfig] = None,
    close_price_override: Optional[float] = None,
    expired: bool = False,
    touch_settlement_haircut: float = 0.0,
    sell_touched_at_market: bool = False,
    fee_model: Optional[PolymarketFeeModel] = None,
    book_lookup: Optional[Callable[[Optional[str], int], Optional[Tuple[List, List]]]] = None,
) -> Tuple[Dict, Dict]:
    """Settle a position. Returns (pos, new_wallet).

    Polymarket settlement modelling:

    - When ``sell_touched_at_market`` is False (legacy / unit-test default),
      a touched-side YES is assumed to pay ``contracts * (1 - touch_settlement_haircut)``.
      With the default haircut of 0 this is the original "$1 per contract"
      behaviour preserved by the unit tests.
    - When ``sell_touched_at_market`` is True (the realistic mode used by
      ``simulate``), the touched-side YES is sold at the prevailing best-bid
      drawn from ``bet_price_history`` at ``close_ts``, with execution costs
      (spread + slippage) applied on the sell side. If no historical bid
      exists for that timestamp we fall back to ``contracts * (1 - touch_settlement_haircut)``.

    The untouched-side YES is always sold at the current bid when conn data
    exists; if not, the sellback is **0** (no more silently inflating the
    sellback with the entry-time mid).
    """
    if close_price_override is not None:
        touch_price = float(close_price_override)
    else:
        if touched_lower:
            touch_price = pos["min_range"]
        elif touched_upper:
            touch_price = pos["max_range"]
        else:
            cp = float(close_candle["close"])
            touch_price = cp if price_token == 0 else 1.0 / cp

    il = calculate_il_at_price(
        pos["entry_price"], pos["token0_dep"], pos["token1_dep"],
        touch_price, pos["min_range"], pos["max_range"],
    )

    close_ts = int(close_candle["periodStartUnix"])

    get_clob_token_id = _get_db_func("get_clob_token_id")
    get_historical_bet_price = _get_db_func("get_historical_bet_price")

    payout = 0.0
    insurance_sellback = 0.0
    spread_cost_sell = 0.0
    slippage_cost_sell = 0.0
    fee_cost_sell = 0.0

    book_used_close = {"flag": False}

    def _book_for(clob_id: Optional[str]) -> tuple[Optional[list], Optional[list]]:
        if book_lookup is None or not clob_id:
            return (None, None)
        try:
            res = book_lookup(clob_id, close_ts)
        except Exception:
            return (None, None)
        if not res:
            return (None, None)
        return res[0], res[1]

    def _sell_yes(contracts: float, clob_id: Optional[str]) -> Tuple[float, float, float, float, bool]:
        """Sell ``contracts`` of a YES at the strict-past bid drawn from DB.

        Returns ``(proceeds_usd, spread_cost, slippage_cost, fee_cost, used_market_bid)``.
        ``used_market_bid`` is False when no historical bid was available.
        ``fee_cost`` is the Polymarket dynamic taker fee paid on the sell.

        When an L2 snapshot is available via ``book_lookup`` we walk the bid
        side directly; otherwise we fall back to the historical mid + fitted
        slippage path.
        """
        if contracts <= 0 or not clob_id:
            return 0.0, 0.0, 0.0, 0.0, False
        bids, asks = _book_for(clob_id)
        if bids:
            book_used_close["flag"] = True
            mid_for_call = float(bids[0]["price"]) if isinstance(bids[0], dict) else float(bids[0][0])
            bid_exec, sp_cost, sl_cost = apply_execution_costs(
                mid_price=mid_for_call,
                spread=spread,
                contracts=float(contracts),
                side="sell",
                slippage_cfg=slippage_cfg,
                asset_id=clob_id,
                fee_model=fee_model,
                book_bids=bids,
                book_asks=asks,
            )
            fee = polymarket_taker_fee_usd(float(contracts), bid_exec, fee_model)
            return contracts * bid_exec, sp_cost, sl_cost, fee, True
        if conn is None:
            return 0.0, 0.0, 0.0, 0.0, False
        mid_price = get_historical_bet_price(clob_id, close_ts, conn)
        if mid_price is None:
            return 0.0, 0.0, 0.0, 0.0, False
        bid_exec, sp_cost, sl_cost = apply_execution_costs(
            mid_price=float(mid_price),
            spread=spread,
            contracts=float(contracts),
            side="sell",
            slippage_cfg=slippage_cfg,
            asset_id=clob_id,
            fee_model=fee_model,
        )
        fee = polymarket_taker_fee_usd(float(contracts), bid_exec, fee_model)
        return contracts * bid_exec, sp_cost, sl_cost, fee, True

    lower_clob = pos.get("lower_clob_token_id")
    upper_clob = pos.get("upper_clob_token_id")
    if conn is not None and lower_clob is None and pos["lower_contracts"] > 0:
        lower_clob = get_clob_token_id(
            token_symbol, pos["min_range"], "down", "Yes", conn, candle_ts=close_ts
        )
    if conn is not None and upper_clob is None and pos["upper_contracts"] > 0:
        upper_clob = get_clob_token_id(
            token_symbol, pos["max_range"], "up", "Yes", conn, candle_ts=close_ts
        )

    if expired:
        # Force-close at insurance market expiry: sell BOTH legs at the
        # prevailing bid (one leg will be near-1, the other near-0). No
        # special "payout" claim — that's accounted for via the bid itself.
        if pos["lower_contracts"] > 0:
            proceeds, sp, sl, fc, _ok = _sell_yes(pos["lower_contracts"], lower_clob)
            insurance_sellback += proceeds
            spread_cost_sell += sp
            slippage_cost_sell += sl
            fee_cost_sell += fc
        if pos["upper_contracts"] > 0:
            proceeds, sp, sl, fc, _ok = _sell_yes(pos["upper_contracts"], upper_clob)
            insurance_sellback += proceeds
            spread_cost_sell += sp
            slippage_cost_sell += sl
            fee_cost_sell += fc
    else:
        if touched_lower and pos["lower_contracts"] > 0:
            if sell_touched_at_market:
                proceeds, sp, sl, fc, ok = _sell_yes(pos["lower_contracts"], lower_clob)
                if ok:
                    payout += proceeds
                    spread_cost_sell += sp
                    slippage_cost_sell += sl
                    fee_cost_sell += fc
                else:
                    payout += pos["lower_contracts"] * (1.0 - touch_settlement_haircut)
            else:
                payout += pos["lower_contracts"] * (1.0 - touch_settlement_haircut)

        if touched_upper and pos["upper_contracts"] > 0:
            if sell_touched_at_market:
                proceeds, sp, sl, fc, ok = _sell_yes(pos["upper_contracts"], upper_clob)
                if ok:
                    payout += proceeds
                    spread_cost_sell += sp
                    slippage_cost_sell += sl
                    fee_cost_sell += fc
                else:
                    payout += pos["upper_contracts"] * (1.0 - touch_settlement_haircut)
            else:
                payout += pos["upper_contracts"] * (1.0 - touch_settlement_haircut)

        if not touched_lower and pos["lower_contracts"] > 0:
            proceeds, sp, sl, fc, _ok = _sell_yes(pos["lower_contracts"], lower_clob)
            insurance_sellback += proceeds
            spread_cost_sell += sp
            slippage_cost_sell += sl
            fee_cost_sell += fc

        if not touched_upper and pos["upper_contracts"] > 0:
            proceeds, sp, sl, fc, _ok = _sell_yes(pos["upper_contracts"], upper_clob)
            insurance_sellback += proceeds
            spread_cost_sell += sp
            slippage_cost_sell += sl
            fee_cost_sell += fc

    # Withdrawal must come from the position's *liquidity*, not from re-splitting
    # ``deposit_value`` at ``touch_price`` (which is only correct when
    # ``touch_price == entry_price``). Fall back to the old approximation if
    # ``L_human`` was not stored (backwards compatibility with tests that
    # synthesize positions by hand).
    l_human = pos.get("L_human")
    if l_human is None:
        l_human = liquidity_from_tokens(
            pos["entry_price"], pos["token0_dep"], pos["token1_dep"],
            pos["min_range"], pos["max_range"],
        )

    wd_usdc, wd_eth = tokens_from_liquidity(
        touch_price, pos["min_range"], pos["max_range"], l_human,
    )

    fees_usdc = pos["accumulated_fees_usdc"]
    fees_eth = pos["accumulated_fees_eth"]
    fees_total_usd = fees_usdc + fees_eth * touch_price

    gas_close = gas_cost_usd(GAS_BURN_COLLECT, close_ts, touch_price, gas_prices or {})

    # External-cost accounting is always ON: gas and insurance are tracked outside the LP wallet.
    new_wallet_usdc = wd_usdc + fees_usdc
    new_wallet_eth = wd_eth + fees_eth
    new_wallet_value = new_wallet_usdc + new_wallet_eth * touch_price

    pos["close_ts"] = close_ts
    pos["close_price"] = touch_price
    pos["touched_lower"] = touched_lower
    pos["touched_upper"] = touched_upper
    pos["wd_usdc"] = wd_usdc
    pos["wd_eth"] = wd_eth
    pos["il"] = il["IL"]
    pos["il_pct"] = il["IL_pct"]
    pos["insurance_payout"] = 0.0 if expired else payout
    pos["insurance_sellback"] = insurance_sellback
    pos["insurance_net"] = pos["insurance_payout"] + pos["insurance_sellback"] - pos["insurance_cost"]
    pos["fees_earned_usdc"] = fees_usdc
    pos["fees_earned_eth"] = fees_eth
    pos["fees_earned_usd"] = fees_total_usd
    pos["gas_fee_close"] = gas_close
    pos["spread_cost_sell"] = spread_cost_sell
    pos["slippage_cost_sell"] = slippage_cost_sell
    pos["polymarket_fee_sell"] = fee_cost_sell
    pos["polymarket_fee_total"] = pos.get("polymarket_fee_buy", 0.0) + fee_cost_sell
    pos["book_used_close"] = bool(book_used_close["flag"])
    pos["wallet_after"] = {"usdc": new_wallet_usdc, "eth": new_wallet_eth, "value_usd": new_wallet_value}
    pos["duration_hours"] = (pos["close_ts"] - pos["open_ts"]) / 3600
    if expired:
        pos["close_reason"] = "expiry"
    elif touched_lower:
        pos["close_reason"] = "lower"
    elif touched_upper:
        pos["close_reason"] = "upper"
    else:
        pos["close_reason"] = "period_end"

    return pos, {"usdc": new_wallet_usdc, "eth": new_wallet_eth}

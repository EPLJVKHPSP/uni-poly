"""Per-asset slippage estimator from Polymarket trade prints.

We approximate the price impact of buying ``N`` contracts on a Polymarket
binary outcome token by looking at *recent realized fills*:

  impact_bps_per_contract  ≈   (max_fill_px - min_fill_px) within a short
                                forward window (e.g. 60 minutes) of an
                                aggressive buy print
  per_1k_contracts         =   impact_bps_per_contract * 1000

In practice we don't have order-book snapshots, so we use this as a
lower-bound proxy: when many trades cluster at one price, slippage is small;
when prints walk the book, the spread between the cheapest and dearest fill
in a window is a reasonable proxy.

For each ``asset`` (CLOB token id) we fit a single number:

    per_1k_contracts = clip(median(intra-window range / cluster_size_kc),
                            min=2 bps, max=200 bps)

The result is cached per-asset and consumed by ``apply_execution_costs``.
"""

from __future__ import annotations

import logging
import statistics
from datetime import timedelta
from typing import Dict, Optional

logger = logging.getLogger(__name__)

DEFAULT_PER_1K = 0.02  # 2¢ per 1k contracts when we have no fit
DEFAULT_MAX_PER_CONTRACT = 0.10  # 10¢ hard cap per contract
WINDOW_MINUTES = 60
MIN_CLUSTER_TRADES = 3


def fit_asset_slippage(
    cur,
    asset: str,
    fallback_per_1k: float = DEFAULT_PER_1K,
) -> float:
    """Estimate ``per_1k_contracts`` impact for a single asset.

    Returns USD-per-contract impact for a 1k-contract aggressive order.
    Falls back to ``fallback_per_1k`` if there isn't enough data.
    """
    cur.execute(
        """
        SELECT ts, price, size, side
        FROM bet_trades
        WHERE asset = %s
        ORDER BY ts ASC
        """,
        (asset,),
    )
    rows = cur.fetchall()
    if len(rows) < MIN_CLUSTER_TRADES * 4:
        return fallback_per_1k

    impacts = []
    window = timedelta(minutes=WINDOW_MINUTES)
    n = len(rows)
    j = 0
    for i in range(n):
        ts_i, _px_i, _sz_i, _sd_i = rows[i]
        while j < n and rows[j][0] - ts_i <= window:
            j += 1
        cluster = rows[i:j]
        if len(cluster) < MIN_CLUSTER_TRADES:
            continue
        prices = [float(r[1]) for r in cluster]
        sizes = [float(r[2]) for r in cluster if float(r[2] or 0) > 0]
        if not sizes:
            continue
        size_k = sum(sizes) / 1000.0
        if size_k <= 0:
            continue
        rng = max(prices) - min(prices)
        impacts.append(rng / size_k)

    if not impacts:
        return fallback_per_1k

    impact = statistics.median(impacts)
    return max(0.002, min(impact, 0.20))


def fit_all_assets(
    conn,
    asset_ids: Optional[list] = None,
    fallback_per_1k: float = DEFAULT_PER_1K,
) -> Dict[str, float]:
    """Build a {asset_id -> per_1k_contracts} map for the requested assets.

    If ``asset_ids`` is None, fit every asset that appears in ``bet_trades``.
    """
    out: Dict[str, float] = {}
    cur = conn.cursor()
    try:
        if asset_ids is None:
            cur.execute("SELECT DISTINCT asset FROM bet_trades")
            asset_ids = [r[0] for r in cur.fetchall()]
        for asset in asset_ids:
            out[asset] = fit_asset_slippage(cur, asset, fallback_per_1k=fallback_per_1k)
        logger.info("Fit slippage for %d assets (median=%.4f)",
                    len(out),
                    statistics.median(out.values()) if out else float("nan"))
        return out
    finally:
        cur.close()

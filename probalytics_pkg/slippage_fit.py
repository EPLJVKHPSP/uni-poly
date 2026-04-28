"""Per-asset slippage estimator from Probalytics fills.

Probalytics records both ``price`` (executed) and ``normalized_price``
(= ``1 - price`` for the opposite outcome). The ``normalized_price`` axis
is invariant to YES/NO so we can pool all fills of a market onto one
probability trace.

We don't have an L2 mid in the fills table, so a true per-fill impact
isn't available without a book pull. Instead we use a **window-dispersion**
heuristic that's been the accepted proxy for prediction-market venues:

    per_1k = median over taker-fill clusters of
             ( max_normalized_price - min_normalized_price )  /  (sum_size / 1000)

The Probalytics version improves on the data-api/trades version in
``backtester.slippage_fit`` because:

  - Taker side is known (we exclude maker fills, which are noise).
  - Both YES and NO trades land on the same probability axis.
  - Fee column is real, so we can also report median fee bps per asset.

For each (market, outcome) we return a single ``per_1k_contracts`` value
keyed by ``outcome_platform_id`` (= Polymarket clob_token_id) so the
existing ``SlippageConfig.per_asset`` dict slots straight in.
"""

from __future__ import annotations

import glob
import logging
import math
import os
import statistics
from typing import Dict, Optional, Sequence

import pandas as pd

logger = logging.getLogger(__name__)


def load_fills(root: str = "data/probalytics") -> pd.DataFrame:
    files = sorted(glob.glob(os.path.join(root, "fills", "*.parquet")))
    if not files:
        return pd.DataFrame()
    return pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)


def fit_per_asset_slippage(
    fills: pd.DataFrame,
    *,
    window_seconds: int = 60,
    min_cluster_size: int = 3,
    min_clusters_per_asset: int = 5,
    fallback_per_1k: float = 0.02,
    max_per_1k: float = 0.20,
    min_per_1k: float = 0.002,
) -> Dict[str, float]:
    """Return ``{outcome_platform_id (= clob_token_id): per_1k_contracts}``.

    Algorithm:

      1. Pool taker fills per *market* onto the YES probability axis using
         ``normalized_price``.
      2. Within each rolling ``window_seconds`` cluster of taker fills,
         compute ``(max - min)`` of ``normalized_price`` and total size.
      3. Take the median ratio ``(price_range / total_size_kc)`` across
         clusters as that market's per-1k impact.
      4. Replicate the per-market coefficient onto every outcome of that
         market (YES and NO inherit the same impact model).

    Outcomes with fewer than ``min_clusters_per_asset`` valid clusters fall
    back to ``fallback_per_1k``.
    """
    if fills.empty:
        return {}

    import pandas as pd
    df = fills.copy()
    df = df[df["taker_side"].isin(["BUY", "SELL"])]
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df = df.sort_values(["market_platform_id", "timestamp"])

    # Per-market window dispersion → one coefficient per market.
    per_market: Dict[str, float] = {}
    win = pd.Timedelta(seconds=window_seconds)
    for market_id, sub in df.groupby("market_platform_id", sort=False):
        ts = sub["timestamp"].values
        n = len(sub)
        impacts: list[float] = []
        prices = sub["normalized_price"].astype(float).values
        sizes = sub["size"].astype(float).values
        j = 0
        for i in range(n):
            while j < n and (ts[j] - ts[i]) <= win.to_timedelta64():
                j += 1
            if j - i < min_cluster_size:
                continue
            cluster_prices = prices[i:j]
            cluster_sizes = sizes[i:j]
            tot_size_k = cluster_sizes.sum() / 1000.0
            if tot_size_k <= 0:
                continue
            rng = float(cluster_prices.max() - cluster_prices.min())
            if rng <= 0:
                continue
            impacts.append(rng / tot_size_k)
        if len(impacts) < min_clusters_per_asset:
            per_market[market_id] = fallback_per_1k
            continue
        med = float(statistics.median(impacts))
        per_market[market_id] = max(min_per_1k, min(med, max_per_1k))

    # Replicate per-market coefficient onto all outcomes of that market.
    out: Dict[str, float] = {}
    for market_id, outcomes_sub in df.groupby("market_platform_id", sort=False):
        coef = per_market.get(market_id, fallback_per_1k)
        for outcome_id in outcomes_sub["outcome_platform_id"].unique():
            out[str(outcome_id)] = coef

    return out


def slippage_summary(per_asset: Dict[str, float]) -> dict:
    if not per_asset:
        return {"assets": 0}
    vals = list(per_asset.values())
    return {
        "assets": len(per_asset),
        "median_per_1k": statistics.median(vals),
        "p10_per_1k": float(pd.Series(vals).quantile(0.10)),
        "p90_per_1k": float(pd.Series(vals).quantile(0.90)),
    }

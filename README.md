# Polymarket × Uniswap V3 Active LP Backtester

> **Headline finding:** the *strategy* works. **Polymarket doesn't — yet.**
> At mid-price (zero-execution-cost) accounting, the anchor-mode hedged LP
> beats HODL on both BTC and WETH pools. At realistic execution it loses,
> by **almost exactly the cost of crossing Polymarket's spread + slippage**.
> Prediction-market depth, not the hedging idea, is the binding constraint.

Backtesting framework for an **active Uniswap V3 LP** strategy that uses
**Polymarket prediction markets** as **impermanent-loss (IL) insurance** and
restores the LP back to its initial token-ratio (anchor) on every cycle.

---

## TL;DR — the one number that matters

| Pool                     |  Strategy ROI (EXEC) |  Strategy ROI (MID) |  HODL ROI |  EXEC↔MID gap |  MID − HODL |
| ------------------------ | -------------------: | ------------------: | --------: | ------------: | ----------: |
| **WBTC/USDC 0.05%**      |          **−47.25%** |          **+0.78%** |   −7.99%  |  **+48.03 pp** | **+8.77 pp** |
| **USDC/WETH 0.05%**      |          **−28.70%** |          **−6.69%** |  −16.69%  |  **+22.01 pp** | **+10.00 pp** |

> **Read this row by row:** the strategy column on the right (MID) is the
> world without Polymarket execution friction. **In that world the hedged LP
> beats HODL on both pools.** The world we actually live in (EXEC, with 4 ¢
> spread + fitted slippage + Polymarket taker fees) gives back **all of that
> alpha and more** — entirely because monthly multi-strike YES contracts
> aren't deep enough to absorb the order sizes the strategy generates.

180-day window ending **May 16 2026**, anchor mode, full insurance enforced
on every cycle. Full numbers in §4.

---

## Why this is a depth problem, not a strategy problem

The MID counterfactual is built by reconstructing the same trades at
Polymarket's **CLOB midpoint** (`prices-history`) and removing the recorded
spread + slippage drag. Everything else is identical between the two
columns: same range-selection logic, same anchor restoration, same idle-USDC
opportunity cost, same Polymarket taker fees, same gas, same touch-resolution
haircut. The only thing missing in MID is the part of the cost that comes
from **crossing the bid-ask** and **walking the order book**.

|  Component (USD, 180 d)                  |   WBTC/USDC |  USDC/WETH |
| ---------------------------------------- | ----------: | ---------: |
| Notional deployed (`investment_usd`)     |   **$873,881** |   **$251,143** |
| Insurance bought, **EXEC**               |    $426,226 |    $45,926 |
| Insurance bought, **MID**                |    $289,535 |    $27,543 |
|   *of which spread cost (EXEC)*          |     $32,705 |     $4,032 |
|   *of which slippage cost (EXEC)*        |    **$160,654** |    **$18,446** |
|   *of which Polymarket taker fees*       |     $14,020 |     $2,000 |
| **Total execution drag**                 |    **$193,360 (22 % of capital)** |    **$22,477 (9 % of capital)** |

Two things stand out:

1. **Slippage > spread on every position.** On the BTC run the slippage
   bill is **5× the spread bill** ($161 k vs $33 k). The order ladders for
   the 15–20 ¢ YES contracts the algorithm selects simply do not have enough
   resting size to absorb the 6-figure-USDC clips this strategy throws at
   them — the taker walks down the book by 3–5 ¢ on a contract whose mid is
   15 ¢, i.e. 20–30 % impact per leg.

2. **Drag scales super-linearly with deployed capital.** On WETH at \$251 k
   notional, drag is ~9 % of capital. On BTC at \$874 k it balloons to
   ~22 %. Same algorithm, same markets, same window — but order size
   landed against thinner books on the BTC side and the slippage curve
   bites harder.

In other words: **the venue is the bottleneck, not the model.** Polymarket's
monthly multi-strike touch markets clear retail-sized hedges fine; they
don't clear the size that institutional LPs would want to hedge.

---

## When this strategy is and isn't deployable today

|                                  | Suitable today?                       | Why                                                                |
| -------------------------------- | ------------------------------------- | ------------------------------------------------------------------ |
| **Hobby / retail LP** (≤ ~$25 k) |  Plausibly yes                        | Order sizes stay inside the top-of-book; slippage ≈ spread; the MID-EXEC gap shrinks proportionally and the hedge can pay for itself when realised vol > 30 %. |
| **Mid-size LP** (~$50–250 k)     |  Marginal                             | The ~$251 k WETH cell already burned 9 % of capital in pure execution drag over 180 d. Profitable only in a high-realised-vol regime. |
| **Institutional LP** (≥ $500 k)  |  **Not yet**                          | Drag scales to 20 %+ of deployed capital. No realistic vol regime closes that gap. Polymarket's order-book depth on monthly multi-strike contracts is the hard cap. |

> **What changes the answer:** longer-dated touch markets (lower rollover
> frequency), two-sided market-making programs from Polymarket, deeper
> on-chain CLOB liquidity (e.g. coordinator subsidies on multi-strike
> markets), or a tier-1 prediction-market venue with order-book depth on the
> order of perp DEXs would all collapse the execution-drag column. The
> hedging idea is correct; the venue isn't ready.

---

## Table of contents

1. [Strategy — anchor-mode hedged LP](#1--strategy--anchor-mode-hedged-lp)
2. [Range selection algorithm](#2--range-selection-algorithm)
3. [EXEC vs MID reporting](#3--exec-vs-mid-reporting)
4. [180-day headline numbers](#4--180-day-headline-numbers)
5. [Realism audit — what was changed](#5--realism-audit--what-was-changed)
6. [Architecture](#6--architecture)
7. [Setup](#7--setup)
8. [Running a backtest](#8--running-a-backtest)
9. [Reports](#9--reports)
10. [Tests](#10--tests)
11. [Known limitations](#11--known-limitations)

---

## 1 · Strategy — anchor-mode hedged LP

For each cycle:

1. **Open** a concentrated LP position on a target pool (default
   `WBTC/USDC 0.05%` or `USDC/WETH 0.05%` on Mainnet) at the symmetric range
   `[price · (1−w/2), price · (1+w/2)]` chosen by the range-selection
   algorithm (§2).
2. **Buy two YES legs on Polymarket** — lower (price touches \$X) and
   upper (price touches \$Y) — sized to fully insure the IL the LP would
   accrue if either boundary is hit (`hedge_sizing_mode = "full_restore"`,
   minus an `hedge_lp_fee_credit_pct` discount for expected LP-fee income).
3. **Hold** until either (a) a boundary is breached (force-close at the
   *next* candle, `pessimistic` policy), or (b) the Polymarket touch market's
   `end_date` passes (force-close + roll into the next monthly market).
4. **Restore to anchor.** On close, sell residual LP and remaining YES
   contracts, then swap back to the original token ratio
   (`restore_to_anchor=true`). This is the change vs the realism-audit
   baseline: each cycle starts from the *same* notional and same token
   split, so cycles are independent and IL doesn't compound across them.
5. **Cooldown** (`cooldown_hours`, default 1 h), then re-open against a
   freshly-scored range.

Strict-rule enforcement on every cycle:

* `require_full_insurance = true` — skip the cycle if either Polymarket leg
  cannot be fully filled.
* `max_idle_hours = 24` — the run is only valid if no consecutive 24 h
  window passes without an insured position.

---

## 2 · Range selection algorithm

On every candle where no position is open the algorithm:

1. **Fetches the candidate universe** — all touch markets (`touch_any_time`
   resolution) for the underlying that are *active as-of the current
   candle timestamp* (`start_date ≤ ts ≤ end_date`).
2. **Filters** to ranges that:
   * **contain the current price** with at least a 5 % buffer on each side,
   * are **not wider than 80 %** of the current price,
   * have a Polymarket YES probability **≤ `range_yes_cap`** on *both* legs
     (default 10 % on the WETH best, 2 % on the BTC best — a hard cap on
     premium per cycle).
3. **Scores** each surviving range:
   ```
   score = narrowness_bonus  −  insurance_cost_rate
   narrowness_bonus      = 1 − (range_width / widest_candidate)
   insurance_cost_rate   = lower_yes_price + upper_yes_price
   ```
4. **Opens the top-scoring range**, executing both YES legs through the
   configured execution model (§3).

The "cheapest and narrowest" objective biases entries toward correctly-priced
deep-OTM insurance. That's a feature for premium efficiency but a bug for
realised payouts (most legs expire worthless), and is the structural reason
the *unhedged* and *MID-priced* baselines beat the *EXEC* strategy in any
flat or trending regime.

---

## 3 · EXEC vs MID reporting

Every result is reported in two variants:

| Variant   | What it represents                                                                                                                                                                                                                       |
| --------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **EXEC**  | Realistic execution: flat bid-ask spread (default 4 ¢), per-asset fitted slippage, Polymarket dynamic taker-fee curve, and (where available) real L2 VWAP book-walk against Probalytics order-book snapshots.                            |
| **MID**   | Counterfactual: execution drag removed. Insurance is priced at the CLOB mid-point with zero spread/slippage. Polymarket taker fees are still folded in. Represents *"best-possible execution if Polymarket had full historical depth."* |

> **Why Polymarket doesn't expose historical depth:** the public API serves
> `prices-history` (hourly mid-points) but no historical L2 snapshots.
> Probalytics fills the last ~7 days via ClickHouse; the rest of any
> 180-day window uses the official mid-point and EXEC layers a
> parametric spread + slippage model on top.

The EXEC↔MID gap measures the cost of crossing Polymarket's bid-ask spread
and walking the book. On thinly-traded monthly multi-strike markets (most
of what this algorithm selects), the gap is **the** result of the
backtest — see the TL;DR.

---

## 4 · 180-day headline numbers

Window: 2025-11-17 → 2026-05-16. Anchor mode, full insurance every cycle,
`max_idle_hours = 24`. Configs in `best_btc_anchor.config.json` and
`best_weth_anchor.config.json`. Reports rendered to
`WBTC_USDC_best_pool_with_anchor.html` and
`WETH_USDC_best_pool_with_anchor.html`.

|  Metric                                  |  WBTC/USDC 0.05% (anchor)         |  USDC/WETH 0.05% (anchor)        |
| ---------------------------------------- | --------------------------------: | -------------------------------: |
| Initial principal                        | 5 BTC ($460,054 at open)          | 50 ETH ($150,973 at open)        |
| Cumulative notional (`investment_usd`)   | $873,881                          | $251,143                         |
| Cycles                                   | 20 (20 / 20 insured)              | 15 (15 / 15 insured)             |
| Lower / upper boundary touches           | 5 / 4                             | 2 / 1                            |
| Underlying move                          | $92,011 → $78,038 (−15.2 %)       | $3,019 → $2,181 (−27.8 %)        |
| **EXEC strategy ROI / APY**              | **−47.25 % / −72.68 %**           | **−28.70 % / −49.64 %**          |
| **MID strategy ROI / APY**               | **+0.78 % / +1.58 %**             | **−6.69 % / −13.10 %**           |
| HODL ROI / APY                           | −7.99 % / −15.55 %                | −16.69 % / −30.95 %              |
| Unhedged active LP ROI                   | −21.61 %                          | −15.66 %                         |
| **EXEC vs HODL**                         | **−39.26 pp ROI · −57.13 pp APY** | **−12.01 pp ROI · −18.69 pp APY** |
| **MID vs HODL**                          | **+8.77 pp ROI · +17.13 pp APY**  | **+10.00 pp ROI · +17.85 pp APY** |
| LP fees earned                           | $41,382                           | $31,212                          |
| Insurance buy (EXEC / MID)               | $426,226 / $289,535               | $45,926 / $27,543                |
| Insurance payout                         | $71,027                           | $3,956                           |
| Insurance sellback (EXEC / MID)          | $149,763 / $206,432               | $11,078 / $15,172                |
| Spread cost                              | $32,705                           | $4,032                           |
| Slippage cost                            | $160,654                          | $18,446                          |
| Polymarket taker fees                    | $14,020                           | $2,000                           |
| Gas (180 d)                              | $2,953                            | $66                              |
| Total IL realised                        | −$417,219                         | −$47,790                         |
| **Total execution drag**                 | **$193,360 (≈ +48 pp ROI)**       | **$22,477 (≈ +22 pp ROI)**       |

---

## 5 · Realism audit — what was changed

These eleven fixes (carried over from the `realism-audit-180d` baseline)
are what makes the EXEC↔MID comparison honest. Removing any of them would
overstate either the strategy or the counterfactual.

1. **No-lookahead pricing.** `get_historical_bet_price` uses
   `strict_past=True`; `ts ≤ target_ts` only. Cycles that find no
   historical mid are skipped, not opened with a guessed price.
2. **Pessimistic close policy.** Boundary breaches close at the *next*
   candle's price, not the breaching candle.
3. **Realistic touch settlement.** YES contracts on a touched leg are
   sold at the prevailing best-bid less a `touch_settlement_haircut`
   (default 3 ¢), not credited at \$1.00.
4. **Gas from real RPC data.** `fetch_daily_gas_prices` samples
   `baseFeePerGas` from Ethereum blocks; `priority_fee_gwei` tip layered on
   top. ≥ 100 % day coverage validated.
5. **Touch-anytime markets only.** `parser.py` classifies every fetched
   market by resolution rule; `restrict_to_touch_markets=true` filters
   out `close_on_date` and other non-touch markets.
6. **Polymarket dynamic taker-fee curve.**
   `fee = C × feeRate × (p · (1−p))^exp`, `crypto` category by default;
   folded into both buy and sell legs.
7. **Per-asset slippage** fit from real fills via Probalytics + per-market
   fit from local `bet_trades` as fallback.
8. **Real L2 book-walk** for the last ~7 days via Probalytics
   `OrderBookFetcher` with on-demand caching and LOCF reconstruction.
9. **Historical price backfill** for Oct 2025 → present
   (`scripts/backfill_bet_price_history.py`) — 98 %/98 % lower/upper
   coverage on the 180-day window.
10. **Opportunity cost** on idle USDC (`risk_free_rate_apy`, default 4.5 %).
11. **Markets queried as-of candle timestamp** so the candidate universe
    rotates correctly on monthly market changes.

---

## 6 · Architecture

```
active_backtester.py             Backward-compat shim; config-driven entrypoint

backtester/
  simulation.py                  Core loop: open/close/rebalance, expiry, anchor restore, summaries
  positions.py                   Position lifecycle: LP deposit/withdraw + insurance execution + restore
  range_selection.py             Candidate filtering, scoring, Polymarket YES cap
  gas.py                         Historical baseFee sampling via Ethereum RPC (daily)
  polymarket_execution.py        apply_execution_costs: spread, slippage, fee curve, book-walk
  graph_client.py                The Graph client for Uniswap V3 candles
  slippage_fit.py                Per-market slippage curve fit from bet_trades

probalytics_pkg/                 Probalytics REST + ClickHouse, market-universe sync, on-demand books, LOCF VWAP
polymarket_history_pkg/          CLOB prices-history, bet-trades sync, strike-slug reconstruction

scripts/
  viz_report.py                  Plotly HTML report — headline ROI / cumulative PnL (EXEC + MID) / trade lists
  backfill_bet_price_history.py  Parallel CLOB history backfill
  probalytics_sync.py            CLI orchestrator: markets + fills + orderbooks
  probalytics_status.py          Local Probalytics data coverage summary

db_utils.py                      Postgres queries (markets, CLOB IDs, price history)
parser.py                        Sync Polymarket markets → price_events (targeted + keyset)
polymarket_history.py            Sync CLOB price history → bet_price_history
docker-compose.yml               PostgreSQL + pgAdmin
config.json                      Default backtest parameters
best_btc_anchor.config.json      Headline BTC anchor config
best_weth_anchor.config.json     Headline WETH anchor config
```

---

## 7 · Setup

### 1) Database

```bash
docker-compose up -d
```

### 2) Python

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3) `.env`

**Required:**
```bash
THEGRAPH_API_KEY=<your-key>
DB_HOST=localhost
DB_PORT=5432
DB_NAME=polymarket
DB_USER=polymarket
DB_PASSWORD=polymarket_pw
```

**Recommended:**
```bash
ETH_RPC_URL=https://ethereum.publicnode.com    # for gas sampling
BACKTEST_SUBGRAPH_ID=5zvR82QoaXYFyDEKLZ9t6v9adgnptxYpKpSbxtgVENFV
```

**Optional — Probalytics (real L2 fills + orderbooks):**
```bash
PROBALYTICS_API_KEY=<key>
PROBALYTICS_API_SECRET=<secret>
PROBALYTICS_CLICKHOUSE_HOST=<host>
PROBALYTICS_CLICKHOUSE_DATABASE=<db>
PROBALYTICS_CLICKHOUSE_USER=<user>
PROBALYTICS_CLICKHOUSE_PASSWORD=<password>
PROBALYTICS_DATA_ROOT=data/probalytics
```

---

## 8 · Running a backtest

### Populate Polymarket data (one-time)

```bash
# Reconstruct canonical ETH/BTC strike slugs and sync market universe (~30 s):
python3 parser.py --mode targeted --start 2025-11-01 --end 2026-05-16

# Sync historical CLOB midpoints for all fetched markets:
python3 polymarket_history.py --underlyings BTC,ETH

# Backfill any historical gaps (idempotent):
python3 -m scripts.backfill_bet_price_history --start 2025-11-01 --end 2026-05-16

# Optional: per-market slippage fits from trades:
python3 -m polymarket_history_pkg.trades_sync
```

### Run the headline configs

```bash
set -a && source .env && set +a
source venv/bin/activate

# BTC (≈ 2 min):
BACKTEST_CONFIG_PATH=best_btc_anchor.config.json \
    python3 -c "from backtester import main; main()"
BACKTEST_CONFIG_PATH=best_btc_anchor.config.json \
    python3 scripts/viz_report.py
# → writes best_btc_anchor.json (raw simulation output) and
#         WBTC_USDC_best_pool_with_anchor.html (Plotly report)

# WETH (≈ 2 min):
BACKTEST_CONFIG_PATH=best_weth_anchor.config.json \
    python3 -c "from backtester import main; main()"
BACKTEST_CONFIG_PATH=best_weth_anchor.config.json \
    python3 scripts/viz_report.py
# → writes best_weth_anchor.json and
#         WETH_USDC_best_pool_with_anchor.html
```

The same `BACKTEST_CONFIG_PATH` controls both the simulator and the report
generator: `report.input_json`, `report.output_html`, and `report.title`
all live in the config file.

### Run a custom config

Copy either headline config and tune:

```jsonc
{
  "backtest": {
    "pool":                       "0x...",       // Uniswap V3 pool
    "days":                       180,
    "initial_eth":                50.0,          // (or initial_usdc)
    "fixed_range_pct":            30.0,          // ± symmetric range, %
    "range_yes_cap":              0.10,          // hard cap on YES probability
    "min_hedge_tte_hours":        336,           // require ≥ 14 d to expiry
    "max_idle_hours":             24,            // strict-rule budget
    "require_full_insurance":     true,          // skip cycle if any leg unfillable
    "restore_to_anchor":          true,          // anchor mode
    "hedge_sizing_mode":          "full_restore",
    "hedge_lp_fee_credit_pct":    0.70,          // pre-credit expected LP fees
    "spread":                     0.04,
    "slippage_per_1k_contracts":  0.02,
    "polymarket_fee_category":    "crypto",
    "risk_free_rate_apy":         0.045,
    "output_json":                "<your-name>.json"
  },
  "report": {
    "input_json":   "<your-name>.json",
    "output_html":  "<your-name>.html",
    "title":        "Your title"
  }
}
```

---

## 9 · Reports

`scripts/viz_report.py` generates a self-contained Plotly HTML report with
seven panels, top-to-bottom:

1. **Headline ROI / APY** — EXEC and MID side-by-side; *Strategy vs HODL*
   on the top row.
2. **Balances + cashflow ledger** — initial / final / Polymarket / LP fees,
   EXEC vs MID columns.
3. **Strategy ranges over time** — price + range bands + entry markers.
4. **Cumulative PnL — EXEC** — strategy curve, HODL, cumulative
   insurance net (EXEC), cumulative LP fees.
5. **Cumulative PnL — MID** — same layout but with the strategy curve
   reconstructed by adding back per-position spread + slippage drag and
   the insurance net recomputed at MID prices.
6. **Trade list — EXEC** — per-cycle ledger.
7. **Trade list — MID** — same columns; only the insurance columns are
   re-priced.

Both trade-list tables use the same column layout (`Days`, `Why entered`,
`Why closed`, range, width, buffers, insurance buy / payout / sell / net,
LP fees, IL, Δ wallet) — only the EXEC/MID tag on the insurance columns
differs.

---

## 10 · Tests

```bash
make test
# or
pytest tests/ -v
```

Test coverage:

* `test_polymarket_execution.py` — spread/slippage model, book-walk VWAP, fee curve
* `test_simulate.py` — core simulation loop, range selection, close policies, anchor restore
* `test_db_utils.py` — strict no-lookahead queries
* `test_probalytics_slippage_fit.py` — slippage estimator from fills
* `test_strike_slugs.py` — slug reconstruction

---

## 11 · Known limitations

* **Historical L2 depth.** Probalytics retains order-book data for ~7 days.
  Older positions use the parametric slippage model. MID prices use the
  official CLOB midpoint (`prices-history`) which can be stale on illiquid
  markets — this *understates* execution drag if anything.
* **Monthly market rotation.** The range universe is re-queried as-of each
  candle timestamp, but Polymarket's strike spacing changes between months,
  which affects which ranges are selectable in different periods.
* **No cross-market delta hedge.** The strategy takes unhedged ETH/BTC
  delta in the LP. A perp-funded LP baseline (constant funding-rate proxy)
  would be the natural next comparison.
* **Anchor restoration assumes deep AMM liquidity.** The
  `restore_to_anchor=true` swap back to the original token ratio is priced
  at the next-candle close with `swap_fee_usdc` accounting; for very large
  LPs the realised swap impact would exceed this approximation.
* **MID is not zero-cost.** Polymarket taker fees, opportunity cost on
  idle USDC, gas, and swap fees are *all retained* in the MID
  counterfactual — only the spread + slippage components are removed. So
  the MID column is an *upper bound* on what better Polymarket execution
  could deliver, not a free-money number.

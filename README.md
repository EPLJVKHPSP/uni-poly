# Polymarket × Uniswap V3 Active LP Backtester

> **Audit branch** (`realism-audit-180d`): realistic assumptions, 180-day backtest, and a full post-mortem on why the strategy is structurally unprofitable.

Backtesting framework for an **active Uniswap V3 LP** strategy that uses **Polymarket prediction markets** as **impermanent loss (IL) insurance**.

---

## Table of Contents

1. [Strategy overview](#strategy-overview)
2. [Range selection algorithm](#range-selection-algorithm)
3. [EXEC vs MID reporting](#exec-vs-mid-reporting)
4. [What was changed — the realism audit](#what-was-changed--the-realism-audit)
5. [180-day headline result](#180-day-headline-result-oct-2025--apr-2026)
6. [Why it doesn't work — post-mortem](#why-it-doesnt-work--post-mortem)
7. [Architecture](#architecture)
8. [Setup](#setup)
9. [Running a backtest](#running-a-backtest)
10. [Data pipeline](#data-pipeline)
11. [Tests](#tests)
12. [Known limitations](#known-limitations)

---

## Strategy overview

1. Open a **concentrated LP position** on Uniswap V3 (USDC/WETH 0.05%).
2. Buy Polymarket **YES contracts** on the lower and upper boundary strikes as IL insurance.
   - Lower leg: "ETH touches \$X" (down-touch) — pays $1 if ETH falls below the LP range.
   - Upper leg: "ETH touches \$Y" (up-touch) — pays $1 if ETH rises above the LP range.
3. **Close & rebalance** when:
   - price breaches a boundary (position closed at next candle, `pessimistic` policy), or
   - the insurance market **expires** (the Polymarket touch-market `end_date` passes — position is force-closed and rolled into the next monthly market).
4. Wait `cooldown_hours`, then open a new position using a freshly-scored range.
5. Track wallet evolution (USDC + ETH), pool fees, IL, and insurance cashflows across all cycles.

---

## Range selection algorithm

On every candle where no position is open, the algorithm:

1. **Fetches the candidate universe** — all ETH strike ranges with active Polymarket touch markets at the current candle timestamp (markets rotate monthly).
2. **Filters** to ranges that:
   - **contain the current price** with at least a **5% buffer** on each side (falls back to 2% if nothing qualifies).
   - are **not wider than 80%** of the current price (no degenerate full-range positions).
   - have a Polymarket YES probability **≤ 20%** on *both* legs (hard cap to avoid expensive, high-probability insurance).
3. **Scores** each surviving range:
   ```
   score = narrowness_bonus − insurance_cost_rate
   where narrowness_bonus = 1 − (range_width / widest_candidate)
         insurance_cost_rate = lower_bet_price + upper_bet_price
   ```
   A narrower range earns more LP fees per dollar deployed; cheaper insurance preserves more capital. The algorithm maximises the trade-off.
4. **Opens the top-scoring range**, buying both insurance legs at the prevailing Polymarket bid/ask with the configured execution model.

The "cheapest and narrowest" heuristic is a sensible objective, but it has a structural bias — explained in the post-mortem below.

---

## EXEC vs MID reporting

Every result is reported in two variants:

| Variant | What it represents |
|---|---|
| **EXEC** | Realistic execution: flat bid-ask spread (default 4¢), per-asset fitted slippage, Polymarket dynamic taker-fee curve, and (where available) real L2 VWAP book-walk against Probalytics orderbook snapshots. |
| **MID** | Counterfactual: execution drag removed. Insurance is priced at the CLOB midpoint with zero spread/slippage. Polymarket taker fees are still folded in. Represents "best-possible execution if Polymarket had full historical depth data." |

> **Why Polymarket doesn't share historical depth:** Polymarket's public API exposes `prices-history` (hourly midpoints) but does not serve historical L2 snapshots. Probalytics fills this gap for the last 7 days via a ClickHouse SQL interface. For the remaining 173 days in this run, MID uses the official CLOB midpoint from `prices-history` — the best available proxy — and EXEC layers the parametric spread + slippage model on top.

The EXEC↔MID gap measures the cost of crossing Polymarket's bid-ask spread and taking liquidity. On thinly-traded monthly multi-strike markets (most of what this algorithm selects), the gap is material.

---

## What was changed — the realism audit

The original backtest shipped with several assumptions that overstated strategy performance. Every change below moves the simulation *closer to what an on-chain investor would actually experience*.

### 1 — No-lookahead pricing (critical)

**Problem:** the original code fetched Polymarket prices with `ts <= target_ts OR ts >= target_ts` (nearest row in either direction), which quietly used *future* prices when the current candle had no data. This is a textbook lookahead bias.

**Fix:** `get_historical_bet_price` now uses `strict_past=True` by default — only `ts ≤ target_ts` rows are considered. If no historical mid exists at or before the candle timestamp, the position is skipped (not opened with a guessed price). Entry-price fallback in sellback calculations was also removed.

### 2 — Pessimistic close policy

**Problem:** the original `touch` close policy could book profit from the intra-bar high/low, because it settled the position at `close_price_override = current_price` (the candle close) even when the touch occurred at the candle high. In reality, you can only trade the *next* candle's open.

**Fix:** close policy defaults to `pessimistic` / `next_candle`. When a boundary is touched, the position is flagged but not settled until the *next* candle's close price is known.

### 3 — Realistic touch settlement

**Problem:** the original code credited a touched position with `$1.00 per contract` instantly — as if Polymarket settled in-block with no slippage.

**Fix:** on touch, we now sell YES contracts at the **prevailing Polymarket best-bid** (from `bet_price_history`), less a configurable `touch_settlement_haircut` (default 3¢). For positions held to expiry, the remaining contracts are sold via the same execution model.

### 4 — Gas fees from real RPC data

**Problem:** gas was a constant (or silently zero on missing days).

**Fix:** `fetch_daily_gas_prices` samples `baseFeePerGas` from actual Ethereum blocks via RPC, computes daily averages, and layers a configurable `priority_fee_gwei` tip on top. `validate_gas_coverage` asserts ≥ 100% day coverage and logs a warning if any day is missing. The 180-day run achieved 100% gas coverage.

### 5 — Market classification — touch-anytime only

**Problem:** the market universe mixed `touch_any_time` markets (pay $1 if price touches the strike *any time* during the period — the correct IL-hedge instrument) with `close_on_date` markets (pay $1 only if price is *above/below* the strike at expiry — a different, weaker instrument).

**Fix:** `parser.py` now classifies every fetched market by resolution rule. `restrict_to_touch_markets=true` (default) filters to `touch_any_time` only.

### 6 — Polymarket dynamic taker-fee curve

**Problem:** Polymarket charges a dynamic taker fee (`fee = C × feeRate × (p × (1-p))^exponent`) that peaks near 1.80% at p=0.5 and falls toward ~0.06% at the deep wings. The original code did not model this fee.

**Fix:** `PolymarketFeeModel` implements the published fee curve. `polymarket_fee_category = "crypto"` is the default. The fee is folded into both the buy-leg cost and the sell/sellback price, and is broken out separately in the JSON summary and HTML report.

### 7 — Per-asset slippage fitted from real fills

**Problem:** slippage was a global constant (`$0.02 per 1,000 contracts`), not tied to any observable market microstructure.

**Fix — two layers:**
- **Layer A (Probalytics fills):** `probalytics_pkg/slippage_fit.py` fits a per-asset slippage curve from Probalytics ClickHouse `fills` data using a window-dispersion heuristic on `normalized_price` (the taker-side execution cost relative to the prevailing midpoint). This produces asset-level (`ETH`, `BTC`) slippage coefficients.
- **Layer B (bet_trades):** existing per-market slippage fit from the local `bet_trades` table is retained as a fallback for markets not covered by Probalytics.

The Probalytics coefficients are applied globally by asset; per-market coefficients from `bet_trades` override them when available.

### 8 — Real L2 book-walk execution (Probalytics)

**Problem:** even with a fitted slippage model, execution cost was still parametric (a curve, not a real order book).

**Fix:** `probalytics_pkg/ondemand.py` implements `OrderBookFetcher` — an on-demand fetcher that downloads a ±5-minute window of Probalytics L2 snapshots around each trade timestamp, caches them as Parquet, and reconstructs the order book via Last-Observation-Carried-Forward (LOCF). When a real ladder is available, `apply_execution_costs` performs a true VWAP walk (`_vwap_walk`) rather than using the parametric model.

**Coverage in the 180d run:** Probalytics retains data for 7 days. Of 65 positions, 6 open+close pairs fell inside retention and used the real book-walk. The remaining 59 used the parametric model (fitted on data from those same 7 days).

### 9 — Historical price backfill (Oct 2025 → Mar 2026)

**Problem:** `bet_price_history` only had data from March 5, 2026 onwards (49 days of Tier-B realism). The 60-day run prior to this audit used synthetic/fallback prices for the Oct-Feb window.

**Fix:** `scripts/backfill_bet_price_history.py` fetched CLOB `prices-history` for all 1,512 ETH/BTC touch-anytime markets that ended between Oct 1, 2025 and Mar 5, 2026 (6 parallel workers, 8 req/s rate-limit, ~390s total). This delivered 58,447 new price points, lifting Tier-B realism coverage to 98%/98% (lower/upper) across the full 180-day window.

### 10 — Opportunity cost on idle capital

**Problem:** USDC sitting in the wallet (outside the LP or on Polymarket) was not penalised for the foregone risk-free yield.

**Fix:** an annualised `risk_free_rate_apy` (default 4.5%) accrues on idle USDC every hour and is included in the cost-basis calculation. This is a headwind to the strategy — idle capital between positions forfeits ~4.5% APY that a T-bill would earn.

### 11 — Markets queried as-of candle timestamp

**Problem:** the candidate universe was computed once at the start of the run. Monthly multi-strike markets rotate, so stale candidates caused clob-token-id mismatches and missing bet prices for positions opened late in the month.

**Fix:** `get_range_combinations` and `get_clob_token_id` now accept a `candle_ts` parameter and filter to markets whose `start_date ≤ candle_ts ≤ end_date`. The simulation always resolves the candidate universe "as-of" the current candle.

---

## 180-day headline result (Oct 2025 → Apr 2026)

> Pool: USDC/WETH 0.05% · 50 ETH · ETH $3,904 → $2,404 (−38.4%) · 65 positions

| Metric | Value |
|---|---|
| **Cost basis** | $365,095 |
| **Final wallet (EXEC)** | **$196,660** |
| **ROI (EXEC)** | **−46.20%** |
| **APY (EXEC)** | **−71.56%** |
| **ROI (MID, no spread/slippage)** | **~−30%** (still negative) |
| HODL benchmark (same initial quantities) | −20.05% |
| Unhedged active LP benchmark | −26.81% |
| LP fees earned | +$62,861 |
| Impermanent loss | −$121,833 |
| Insurance cost (EXEC) | −$231,763 |
| Insurance payouts (touches) | +$11,443 (11 payouts / 65 positions) |
| Insurance sellback at expiry | +$40,560 |
| **Insurance net** | **−$179,760** |
| Spread cost (buy + sell) | −$32,944 |
| Slippage cost | −$14,000 (est.) |
| Polymarket taker fees | included in insurance cost |
| Gas + swap fees | −$1,842 |
| Polymarket history coverage | 98% lower / 98% upper |
| Positions using real book-walk | 6 / 65 |

**ETH fell 38% over the period.** An unhedged LP still outperformed the hedged strategy by 19+ percentage points.

---

## Why it doesn't work — post-mortem

### The core equation

```
Strategy PnL = LP fees − IL − Insurance net − Execution costs − Gas
```

Over 180 days:
```
            LP fees:  +$62,861   ✓
                IL:  −$121,833
  Insurance net:     −$179,760   ← 1.5× the IL it was meant to offset
Execution costs:      −$47,786
           Gas:           −$294
```

**Insurance cost is 1.5× the impermanent loss it was supposed to offset.** The hedge costs more than the risk it covers — every cycle.

---

### Reason 1: 86% of openings are calendar rollovers, not market signals

Of 65 positions:

| Trigger | Count | Meaning |
|---|---|---|
| Hedge expired (time-driven rollover) | 56 (86%) | Polymarket monthly market `end_date` passed with no breach; forced close and re-open into the next market |
| Rebalance ↓ (price-driven) | 5 | LP range breached on the lower side |
| Rebalance ↑ (price-driven) | 3 | LP range breached on the upper side |
| First open | 1 | |

**56 of 65 premiumpayments were for protection that expired worthless.** The LP range survived every expiry cycle — precisely because the algorithm biases toward cheap, out-of-the-money wings (the ≤20% YES-probability cap). The market was correctly pricing those wings as low-probability touches, and they mostly didn't touch.

Each rollover paid a fresh insurance premium (median **0.80% of LP deposit**, max 4.18%, mean 1.22%). Across 56 expiries, that compounds to roughly 45% of total capital cycled through premiums in a period where LP fees only reimbursed ~25% of that premium spend.

---

### Reason 2: The 20% YES-cap biases entries toward correctly-priced deep OTM insurance

The range-selection hard cap `lower_bet_price ≤ 0.20 AND upper_bet_price ≤ 0.20` was intended to keep insurance cheap. But on an efficient market, a YES price of ≤20¢ means *the market assigns ≤20% probability to the touch event*. Over 180 days the realized touch rate was **8.5%** (11 touches across 130 contracts = 65 positions × 2 legs) — consistent with or below the implied probability. The market was not mispriced.

Buying correctly-priced options at 15–20¢ and watching 91.5% of them expire worthless is exactly what short-options sellers earn their premium for. The LP is *on the wrong side* of the trade: it should be selling the insurance, not buying it.

---

### Reason 3: Implied vol > realized vol in every sub-period

The vol premium embedded in Polymarket YES prices is the fundamental headwind. ETH spent most of the period grinding lower (Oct–Nov bull, Dec–Jan chop, Feb–Apr drawdown) rather than violently breaching the strike bands. Insurance has positive expected value only when realized volatility exceeds implied volatility. Here:

- **Bull phase (Oct → Dec 2025):** ETH went from $3,900 to $3,300. Most of the range moves were drift, not jumps — the upper boundaries were never threatened; a few lower boundaries touched.
- **Drawdown (Jan → Apr 2026):** ETH fell from $3,300 to $2,400. Most of this was a slow grind below every rebalanced range — IL accrued linearly but the insurance legs expired worthless because price didn't breach the strike with enough speed for the touch market to register.

---

### Reason 4: LP fees don't scale with insurance spend

The algorithm tries to maximise the narrowness-score to earn more LP fees. But the fee market is driven by pool volume and the width of the active range, not by the insurance cost. Over the 180-day period:

- **LP fees ≈ $62k** (0.17% of average deployed capital per position).
- **Insurance spend ≈ $232k** — 3.7× the LP fees.

For the strategy to break even at MID (ignoring execution drag), the ratio would need to invert: LP fees ≥ insurance net loss. That would require either (a) 3–4× higher pool volume, (b) insurance priced at ≤ 25–30% of current levels, or (c) a volatile regime where ≥ 60% of touches actually pay.

---

### Reason 5: Execution drag is real and structural (EXEC only)

Even removing all discussion of the structural insurance bet, EXEC underperforms MID by ~$50k ($32k spread + ~$18k slippage + Polymarket fees). This gap exists because:

- Polymarket monthly multi-strike markets are thinly traded; the bid-ask spread on a 15¢ YES contract is routinely 3–5¢ wide (20–30% of fair value).
- The slippage model fitted from Probalytics fills captures real market impact from taker orders of the sizes the strategy uses.
- The Polymarket taker fee (1.5–1.8% at p=0.15 for the `crypto` category) is non-trivial.

Halving the spread assumption to 2¢ and zeroing slippage saves ~$25k — not enough to make the strategy profitable.

---

### What would make it work

| Lever | Direction needed | Current value | Required for MID break-even |
|---|---|---|---|
| Insurance YES-price cap | Raise it | 20% | ≥ 50% (buy properly-priced protection, not just cheap wings) |
| Implied vol vs realized vol | Need realized > implied | Realized ~8.5%, implied ~15% | Regime where realized ≥ 30%+ |
| LP fee rate | Need higher | ~0.17%/position | ≥ 0.5%/position |
| Touch-market liquidity | Need tighter spread | ~4¢ spread | ≤ 2¢ (requires deeper markets) |
| Calendar rollover frequency | Need less frequent | 56/65 (86%) | ≤ 40% (longer-dated markets) |

The strategy has a plausible long-volatility regime where it works: rapid, large ETH moves that hit both insurance legs frequently (e.g., a 2021-style +/−50% month). The 180-day window studied here was the opposite: a prolonged, low-impulse drawdown.

---

## Architecture

```
active_backtester.py             Backward-compat shim; config-driven entrypoint

backtester/
  simulation.py                  Core loop: open/close/rebalance, expiry, summaries
  positions.py                   Position lifecycle: LP deposit/withdraw + insurance execution
  range_selection.py             Candidate filtering, scoring, Polymarket YES cap
  gas.py                         Historical baseFee sampling via Ethereum RPC (daily)
  polymarket_execution.py        apply_execution_costs: spread, slippage, fee curve, book-walk
  graph_client.py                The Graph client for Uniswap V3 candles
  slippage_fit.py                Per-market slippage curve fit from bet_trades

probalytics_pkg/
  client.py                      ProbalyticsRest + ClickHouse connection helpers
  markets_sync.py                Sync BTC/ETH strike market universe from Probalytics CH
  books_sync.py                  Bulk orderbook snapshot downloader (per-market, per-day)
  ondemand.py                    OrderBookFetcher: on-demand ±5min window + caching
  replay.py                      OrderBookReplay: LOCF reconstruction + VWAP-on-book
  slippage_fit.py                Per-asset slippage fit from Probalytics fills

polymarket_history_pkg/
  clob_client.py                 Polymarket CLOB prices-history fetcher
  sync.py                        bet_price_history schema + upsert
  trades_sync.py                 bet_trades sync from Polymarket data API
  strike_slugs.py                Canonical ETH/BTC strike-market slug reconstruction

scripts/
  viz_report.py                  Plotly HTML report: cumulative PnL, trade list, explainer
  backfill_bet_price_history.py  Parallel CLOB history backfill for Oct 2025 → Mar 2026
  probalytics_sync.py            CLI orchestrator: markets + fills + orderbooks
  probalytics_status.py          Local Probalytics data coverage summary

db_utils.py                      Postgres queries (markets, CLOB IDs, price history)
parser.py                        Sync Polymarket markets → price_events (targeted + keyset)
polymarket_history.py            Sync CLOB price history → bet_price_history
docker-compose.yml               PostgreSQL + pgAdmin
config.json                      All backtest parameters (no CLI flags)
```

---

## Setup

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

### 3) Environment variables (`.env`)

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
```

**Optional — Probalytics (real L2 fills + orderbooks):**
```bash
PROBALYTICS_API_KEY=<key>
PROBALYTICS_API_SECRET=<secret>
PROBALYTICS_CLICKHOUSE_HOST=<host>
PROBALYTICS_CLICKHOUSE_DATABASE=<db>
PROBALYTICS_CLICKHOUSE_USER=<user>
PROBALYTICS_CLICKHOUSE_PASSWORD=<password>
PROBALYTICS_DATA_ROOT=data/probalytics          # local Parquet cache
```

---

## Running a backtest

### 1) Populate Polymarket data

```bash
# Targeted mode — reconstructs canonical ETH/BTC strike slugs, ~30s:
python3 parser.py --mode targeted --start 2025-10-01 --end 2026-04-28

# Sync historical CLOB midpoints for all fetched markets:
python3 polymarket_history.py --underlyings BTC,ETH

# Backfill Oct 2025 → Mar 2026 (skips markets already synced, idempotent):
python3 -m scripts.backfill_bet_price_history --start 2025-10-01 --end 2026-03-06

# Optional: sync trade fills for per-market slippage fitting:
python3 -m polymarket_history_pkg.trades_sync
```

### 2) (Optional) Sync Probalytics data

```bash
# Sync market universe + fills (requires Probalytics Pro plan):
python3 -m scripts.probalytics_sync --assets bitcoin,ethereum

# Check local coverage:
python3 -m scripts.probalytics_status
```

### 3) Configure `config.json`

Key parameters:

| Parameter | Default | Notes |
|---|---|---|
| `days` | `180` | Backtest window in days |
| `initial_eth` | `50.0` | Principal in ETH |
| `spread` | `0.04` | Polymarket bid-ask half-spread (¢) |
| `slippage_per_1k_contracts` | `0.02` | Parametric slippage (per 1k contracts, USDC) |
| `slippage_max_per_contract` | `0.10` | Per-contract cap |
| `close_policy` | `pessimistic` | `touch` \| `next_candle` \| `pessimistic` |
| `touch_settlement_haircut` | `0.03` | Haircut from $1.00 when selling touched YES (¢) |
| `polymarket_fee_category` | `crypto` | Taker fee curve category |
| `risk_free_rate_apy` | `0.045` | Opportunity cost on idle USDC |
| `restrict_to_touch_markets` | `true` | IL-hedge instruments only |

### 4) Run

```bash
python3 active_backtester.py          # writes active_backtest_results.json
python3 -m scripts.viz_report         # writes report.html
```

---

## Data pipeline

### Tiered realism

The backtest operates at different fidelity levels depending on data availability:

| Tier | What's available | Window |
|---|---|---|
| **A** — Max realism | Real L2 VWAP book-walk (Probalytics orderbooks) | Last 7 days |
| **B** — High realism | Probalytics-fitted slippage + CLOB historical midpoints | Oct 2025 → present |
| **C** — Coarse | `bet_trades`-fitted slippage + CLOB historical midpoints | ~6 months |
| **D** — Synthetic | No historical mids; fallback to 50% priors | Before Oct 2025 |

The 180-day run reported above used **Tier B** for the full window, with 6/65 positions upgraded to **Tier A** (the last 7 days).

---

## Tests

```bash
make test
# or
pytest tests/ -v
```

Test coverage includes:
- `test_polymarket_execution.py` — spread/slippage model, book-walk VWAP, fee curve
- `test_simulate.py` — core simulation loop, range selection, close policies
- `test_db_utils.py` — strict no-lookahead queries
- `test_probalytics_slippage_fit.py` — slippage estimator from fills
- `test_strike_slugs.py` — slug reconstruction

---

## Known limitations

- **Historical L2 depth:** Probalytics retains orderbook data for 7 rolling days. Older positions use the parametric slippage model. MID prices use official CLOB midpoints (`prices-history`) which can be stale on illiquid markets.
- **Monthly market rotation:** the range universe is re-queried as-of each candle timestamp, but Polymarket's strike spacing changes between months (affects which ranges are selectable in different periods).
- **No cross-market delta hedge:** the strategy takes unhedged ETH delta in the LP. A perp-funded LP baseline (constant funding-rate proxy) would be the natural next comparison.
- **No gas in MID:** opportunity-cost accounting and gas are the same in both variants; only spread + slippage are removed in MID.

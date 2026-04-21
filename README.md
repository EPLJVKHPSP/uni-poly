# Polymarket-Uniswap V3 Active LP Backtester

Backtesting framework for an **active Uniswap V3 LP** strategy that uses **Polymarket prediction markets** as **impermanent loss (IL) insurance**.

This repo is **configuration-first** (no CLI flags): edit `config.json`, run the backtest, then generate `report.html`.

## Strategy (high level)

1. Open a concentrated LP position on Uniswap V3 (USDC/WETH).
2. Buy Polymarket “Yes” contracts on the **lower** and **upper** boundaries as insurance.
3. Close & rebalance when:
   - price touches a boundary (per `close_policy`), or
   - the insurance market **expires** (forced close; insurance value becomes 0).
4. Wait `cooldown_hours`, then open a new position using dynamically selected ranges.
5. Track wallet evolution (USDC + ETH), pool fees, IL, and insurance cashflows across cycles.

## Architecture

```
active_backtester.py        Backward-compat shim; entrypoint for config-driven runs

backtester/
  simulation.py             Core loop (open/close/rebalance, expiry handling, summaries)
  positions.py              Position lifecycle (LP deposit/withdraw + insurance execution)
  range_selection.py        Candidate filtering + scoring + Polymarket caps
  gas.py                    Historical baseFee sampler via Ethereum RPC (daily averages)
  graph_client.py           The Graph client for Uniswap candles

db_utils.py                 Postgres queries (Polymarket markets, CLOB IDs, price history)
parser.py                   Sync Polymarket markets into `price_events`
polymarket_history.py       Sync CLOB price history into `bet_price_history`
scripts/viz_report.py       Plotly HTML report generator (`report.html`)
docker-compose.yml          PostgreSQL database
```

## Setup

### 1) Database

Start Postgres (or point `.env` at an existing instance):

```bash
docker-compose up -d
```

Populate Polymarket tables:

```bash
python3 parser.py
python3 polymarket_history.py
```

### 2) Python

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3) Environment variables (`.env`)

Required:

```bash
THEGRAPH_API_KEY=<your-key>
DB_HOST=localhost
DB_PORT=5432
DB_NAME=polymarket
DB_USER=polymarket
DB_PASSWORD=polymarket_pw
```

Recommended (for reliable gas sampling; prevents “gas fees = 0” when RPC is rate-limited):

```bash
ETH_RPC_URL=https://ethereum.publicnode.com
```

## Running a backtest

### 1) Configure `config.json`

The backtester uses an **ETH-first** capital model:

- `backtest.initial_eth`: your principal in ETH (X)
- `backtest.initial_usdc`: optional override for required USDC (Y). If `null`, it is computed to match the chosen entry range.

Other key knobs:

- `backtest.days`: backtest window length
- `backtest.lookback_days`: `0` = heuristic range selection; `>0` enables lookback sweep selection
- `backtest.cooldown_hours`: skip this many 1h candles after each close
- `backtest.spread`, `backtest.slippage_*`: Polymarket execution model
- `backtest.close_policy`: `touch` | `next_candle` | `pessimistic`

### 2) Run the backtest (writes JSON)

```bash
python3 active_backtester.py
```

Outputs:

- `active_backtest_results.json`: best-run summary + snapshots + position ledger
- (if `backtest.sweep=true`) `active_backtest_results_sweep.json`: ranked sweep table

### 3) Generate the HTML report (Plotly)

```bash
python3 scripts/viz_report.py
```

This writes `report.html` (self-contained HTML + Plotly CDN JS).

## What you’ll see in `report.html` (example output)

The report is built from `active_backtest_results.json` and includes:

### Balances + Cashflow

A compact summary table (EXEC vs MID) with:

- **Initial LP deposit**: ETH + USDC quantities at first entry (and USD notional)
- **HODL repriced at end**: same initial quantities repriced at the final ETH price
- **Final LP wallet** after closing the last position: ETH + USDC quantities (and USD notional)
- **Cashflows**:
  - gas spent (USD)
  - Polymarket deposited (insurance buy cost)
  - Polymarket payout
  - Polymarket sellback (incl. last position)
  - Uniswap fees earned (USD)
- **Combined**: cost basis vs final total value, ROI/APY (EXEC vs MID)

### Strategy Ranges Over Time

One chart showing:

- pool price (ETH/USD)
- the active LP range overlay (upper/lower bands)
- open/close markers (rebalance points)
- legend directly under the chart

### Rebalance History (EXEC / with premium)

A per-position ledger showing, for each cycle:

- open/close date, close reason
- entry/close prices and chosen range
- insurance: buy (EXEC), payout, sell (EXEC)
- fees (USD), IL (USDC), and Δ wallet value (USD)

### Rebalance History (MID / no premium)

Same layout as above, but insurance buy/sell reconstructed at MID prices by removing execution drag (spread/slippage).

## Tests

```bash
make test
```

## Known limitations / modeling notes

- **Polymarket execution is sensitivity-based, not depth-truthy.**
  Historical orderbook depth is not reliably available, so execution is modeled via:
  - flat spread (`backtest.spread`)
  - optional size-aware slippage (`backtest.slippage_*`)
- **Gas is sampled** from daily average `baseFeePerGas` using an Ethereum RPC. If the RPC is rate-limited/unavailable, gas falls back to $0 for missing days (use `ETH_RPC_URL`).

For a deeper discussion of “truth blockers” and design constraints, see `docs/truth_blockers_and_plan.md`.

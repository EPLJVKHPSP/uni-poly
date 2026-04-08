# Superprotocol Polymarket

Uniswap V3 LP range optimizer that hedges impermanent loss (IL) using Polymarket prediction markets.

## How It Works

1. **Parser** (`parser.py`) — fetches active crypto price events from the [Polymarket Gamma API](https://gamma-api.polymarket.com), enriches them with CoinGecko ATH data, and upserts into a PostgreSQL database. Also stores CLOB token IDs for each market outcome, enabling historical price lookups.
2. **Historical Price Sync** (`polymarket_history.py`) — pulls full bet price history from the [Polymarket CLOB API](https://docs.polymarket.com/developers/CLOB/timeseries) (`/prices-history`) for every tracked market and stores it in `bet_price_history`. This powers the historical backtest mode.
3. **Range Optimizer** (`range_optimizer.py`) — iterates over all possible LP price ranges derived from Polymarket levels, backtests each one via a Uniswap V3 backtester, calculates IL at boundaries, prices insurance from Polymarket bet markets, and selects the range with the highest **net APY** (LP yield minus insurance cost). Supports two modes:
   - **Live mode** (default) — uses current bet prices for insurance cost
   - **Historical mode** (`--historical`) — uses bet prices from the backtest period start for a true end-to-end backtest
4. **Single Range** (`single_range.py`) — runs the same pipeline for a single user-defined range from `config.json`.
5. **IL Calculator** (`il.py`) — computes impermanent loss at any target price for a concentrated liquidity position.
6. **DB Utilities** (`db_utils.py`) — shared PostgreSQL helpers: connection management, unique tokens, range combinations, insurance cost lookups (live and historical).
7. **Current Price** (`get_current_price.js`) — fetches the live pool price from Uniswap V3 subgraph.

## Supported Tokens

BTC, ETH, SOL, BNB, PUMP, LINK, ENA

## Prerequisites

- Python 3.10+
- Node.js 18+
- PostgreSQL 16 (via Docker or local)
- [CoinGecko API](https://www.coingecko.com/en/api) (free tier)
- [The Graph API key](https://thegraph.com/studio/) (free tier, for Uniswap V3 subgraph)

## Quick Start

### 1. Start the database

```bash
docker-compose up -d
```

### 2. Install Python dependencies

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. Install JS backtest dependencies

```bash
cd uniswap-v3-backtest
npm install
cd ..
```

### 4. Configure environment

```bash
cp .env.example .env
# edit .env with your DB credentials if different from defaults
```

Default DB credentials (from `docker-compose.yml`):

| Variable | Default |
|---|---|
| `DB_NAME` | `polymarket` |
| `DB_USER` | `polymarket` |
| `DB_PASSWORD` | `polymarket_pw` |
| `DB_HOST` | `localhost` |
| `DB_PORT` | `5432` |

### 5. Parse Polymarket data

```bash
python parser.py
```

### 6. Sync historical bet prices

```bash
# Hourly granularity (default)
python polymarket_history.py

# 15-minute granularity
python polymarket_history.py --fidelity 15
```

### 7. Run the optimizer

```bash
# Live mode: uses current bet prices
python range_optimizer.py

# Historical mode: uses bet prices from the backtest period
python range_optimizer.py --historical

# Single range from config
python single_range.py
```

## Historical Backtest Mode

By default the optimizer uses **live** Polymarket bet prices for insurance cost calculations. This answers: *"If I open a position right now, what's the best range?"*

With `--historical`, the optimizer instead looks up what the bet price was at the **start of the backtest period**. This answers: *"If I had opened this position 30 days ago and bought insurance at that time, what would my actual net return have been?"*

### Pipeline for historical backtest

```
1. python parser.py                    # Fetch markets + CLOB token IDs
2. python polymarket_history.py        # Sync historical bet prices
3. python range_optimizer.py --historical   # Backtest with period-matched insurance
```

### Data flow

```
Polymarket Gamma API → parser.py → price_events (+ clob_token_id)
                                         ↓
Polymarket CLOB API → polymarket_history.py → bet_price_history
                                                      ↓
config.json → range_optimizer.py ←── db_utils.py (ranges, historical prices)
                  ↓
          JS backtester (The Graph subgraph)
                  ↓
          il.py (IL math)
                  ↓
       Best range + net APY → optimization_results.json
```

## Database Schema

### `price_events`
Stores parsed Polymarket market data with current prices and CLOB token references.

| Column | Type | Description |
|---|---|---|
| `market_id` | BIGINT | Polymarket market ID (PK with side) |
| `side` | TEXT | "Yes" or "No" (PK with market_id) |
| `underlying` | TEXT | Token symbol (BTC, ETH, ...) |
| `level` | NUMERIC | Price level from market question |
| `direction` | TEXT | "up" or "down" |
| `price` | NUMERIC | Current bet price |
| `clob_token_id` | TEXT | CLOB token ID for /prices-history lookups |
| `end_date` | TIMESTAMPTZ | Market expiration |
| `active` | BOOLEAN | Whether market is still open |

### `bet_price_history`
Time-series of historical bet prices per CLOB token.

| Column | Type | Description |
|---|---|---|
| `clob_token_id` | TEXT | CLOB token ID (PK with ts) |
| `ts` | TIMESTAMPTZ | Price timestamp (PK with clob_token_id) |
| `price` | NUMERIC | Bet price at that time |

## Configuration

Edit `uniswap-v3-backtest/config.json`:

```json
{
  "poolID": "0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640",
  "minRange": 2800,
  "maxRange": 3400,
  "investmentAmount": 1000,
  "days": 30,
  "period": "hourly",
  "protocol": 0,
  "priceToken": 0
}
```

## Project Structure

```
├── parser.py               # Polymarket data fetcher & DB writer
├── polymarket_history.py    # Historical bet price syncer (CLOB API)
├── range_optimizer.py       # Main optimizer (multi-range, live or historical)
├── single_range.py          # Single-range analysis
├── il.py                    # Impermanent loss math
├── db_utils.py              # PostgreSQL utilities (live + historical lookups)
├── get_current_price.js     # Uniswap V3 live price fetcher
├── docker-compose.yml       # PostgreSQL container
├── requirements.txt         # Python dependencies
├── uniswap-v3-backtest/     # JS backtester (Revert Finance fork)
│   ├── config.json
│   ├── example.js
│   ├── backtest.mjs
│   └── ...
└── notepad.txt              # Development notes & ideas
```

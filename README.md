# Polymarket-Uniswap V3 Active LP Backtester

Backtesting framework for an active Uniswap V3 LP strategy that uses Polymarket prediction markets as impermanent loss (IL) insurance.

## Strategy

1. Open a concentrated LP position on Uniswap V3 (USDC/ETH)
2. Simultaneously buy Polymarket "Yes" contracts on price boundaries as IL insurance
3. When price touches a boundary: close LP, collect insurance payout, sell back untriggered contracts
4. Wait 1 hour for price stabilization, then reopen with a dynamically selected range
5. Repeat — tracking a wallet of USDC + ETH across all cycles

## Architecture

```
active_backtester.py   Main backtester — fetches candles from The Graph, simulates LP + insurance
├── db_utils.py        PostgreSQL queries for Polymarket range/price data
└── il.py              Impermanent loss math for Uniswap V3 concentrated liquidity

parser.py              Syncs Polymarket events into price_events table
polymarket_history.py  Syncs CLOB price history into bet_price_history table
docker-compose.yml     PostgreSQL 16 database
```

## Setup

```bash
# Start Postgres
docker-compose up -d

# Python environment
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Configure .env
THEGRAPH_API_KEY=<your-key>
DB_NAME=polymarket
DB_USER=polymarket
DB_PASSWORD=polymarket_pw
DB_HOST=localhost
DB_PORT=5432

# Populate database
python parser.py
python polymarket_history.py
```

## Usage

### Dynamic range selection (recommended)
```bash
python active_backtester.py --days 90 --investment 100000
```

### Fixed range
```bash
python active_backtester.py --days 90 --investment 100000 --fixed-range "1800,3400"
```

### Sweep all Polymarket ranges
```bash
python active_backtester.py --days 90 --investment 100000 --sweep
```

### Interactive HTML report (Plotly)
1) Run a backtest to generate `active_backtest_results.json`:

```bash
python active_backtester.py --days 90 --investment 100000
```

2) Generate the HTML report:

```bash
python scripts/viz_report.py --input active_backtest_results.json --output report.html
```

### CLI flags
| Flag | Default | Description |
|------|---------|-------------|
| `--pool` | USDC/WETH 0.05% | Uniswap V3 pool address |
| `--days` | 90 | Backtest window in days |
| `--investment` | 100000 | Initial investment in USD |
| `--cooldown` | 1 | Hours to wait after closing before reopening |
| `--price-token` | 0 | Which token is the price base (0 or 1) |
| `--fixed-range` | None | Force a specific range, e.g. `"2000,2400"` |
| `--sweep` | False | Sweep all Polymarket ranges and rank by APY |
| `--spread` | 0.04 | Polymarket bid-ask spread estimate in $/contract |
| `--output` | `active_backtest_results.json` | Output JSON path |

## Output

Results are saved as JSON with per-position details including:
- Wallet state before/after each position (USDC + ETH quantities)
- Fees earned (split into USDC and ETH, in-kind)
- IL suffered
- Insurance cost, payout, sellback (untriggered contracts sold at market)
- Gas fees per open/close (historical baseFeePerGas sampled from Ethereum blocks via free RPC)
- Spread cost per buy/sell (configurable via `--spread`)
- Token quantity deltas vs initial deposit

## Known Limitations

**Polymarket bid-ask spread is estimated, not historical.** The `--spread` flag (default $0.04/contract) models the cost of buying at the ask and selling at the bid. The Polymarket CLOB API only exposes live orderbook data (`GET /book`, `GET /spread`) — there is no historical bid-ask endpoint, and the undocumented `/orderbook-history` endpoint has been non-functional since February 2026. Historical spread data cannot be retrieved retroactively, so the backtester uses a flat configurable estimate. Typical real spreads range from $0.03-$0.10 depending on market liquidity. Set `--spread 0` to disable.

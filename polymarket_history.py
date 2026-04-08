"""
Fetch and store historical bet prices from the Polymarket CLOB API.

For each market in price_events that has a clob_token_id, pulls the full
price history from /prices-history and stores it in bet_price_history.
"""

import os
import time
import logging
import requests
import psycopg2
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

CLOB_BASE_URL = "https://clob.polymarket.com"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def get_db_connection():
    return psycopg2.connect(
        dbname=os.getenv("DB_NAME", "polymarket"),
        user=os.getenv("DB_USER", "polymarket"),
        password=os.getenv("DB_PASSWORD", "polymarket_pw"),
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", "5432")),
    )


def ensure_history_table(cur):
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS bet_price_history (
            clob_token_id   TEXT        NOT NULL,
            ts              TIMESTAMPTZ NOT NULL,
            price           NUMERIC     NOT NULL,
            PRIMARY KEY (clob_token_id, ts)
        )
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_bph_token_ts
        ON bet_price_history(clob_token_id, ts)
        """
    )


def fetch_price_history(clob_token_id, start_ts=None, end_ts=None, fidelity=60):
    """
    Fetch historical prices for a single CLOB token from Polymarket.

    Args:
        clob_token_id: The CLOB asset/token ID
        start_ts: Unix timestamp for range start (optional)
        end_ts: Unix timestamp for range end (optional)
        fidelity: Granularity in minutes (default 60 = hourly)

    Returns:
        List of {t: unix_ts, p: price} dicts
    """
    params = {
        "market": clob_token_id,
        "interval": "all",
        "fidelity": fidelity,
    }
    if start_ts:
        params["startTs"] = int(start_ts)
    if end_ts:
        params["endTs"] = int(end_ts)

    try:
        resp = requests.get(
            f"{CLOB_BASE_URL}/prices-history",
            params=params,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("history", [])
    except Exception as e:
        logger.error(f"Failed to fetch history for {clob_token_id[:16]}...: {e}")
        return []


def upsert_price_history(cur, clob_token_id, history):
    """Bulk-upsert price history rows."""
    if not history:
        return 0

    rows = []
    for point in history:
        ts = datetime.fromtimestamp(point["t"], tz=timezone.utc)
        price = point["p"]
        rows.append((clob_token_id, ts, price))

    from psycopg2.extras import execute_values

    execute_values(
        cur,
        """
        INSERT INTO bet_price_history (clob_token_id, ts, price)
        VALUES %s
        ON CONFLICT (clob_token_id, ts) DO UPDATE SET price = EXCLUDED.price
        """,
        rows,
    )
    return len(rows)


def sync_all_markets(fidelity=60):
    """
    For every market in price_events that has a clob_token_id,
    fetch historical prices and store them in bet_price_history.
    """
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        ensure_history_table(cur)
        conn.commit()

        cur.execute(
            """
            SELECT DISTINCT clob_token_id, market_id, side, underlying, level, direction
            FROM price_events
            WHERE clob_token_id IS NOT NULL
              AND active = true
            ORDER BY underlying, level
            """
        )
        markets = cur.fetchall()
        logger.info(f"Found {len(markets)} active markets with CLOB token IDs")

        total_points = 0
        for i, (clob_id, mkt_id, side, underlying, level, direction) in enumerate(markets, 1):
            logger.info(
                f"[{i}/{len(markets)}] {underlying} {direction} ${level} "
                f"({side}) market_id={mkt_id}"
            )

            history = fetch_price_history(clob_id, fidelity=fidelity)
            if history:
                count = upsert_price_history(cur, clob_id, history)
                total_points += count
                logger.info(f"  Stored {count} price points")
            else:
                logger.warning(f"  No history returned")

            conn.commit()

            # Respect rate limits
            time.sleep(0.3)

        logger.info(f"\nDone. Total price points stored: {total_points}")
    finally:
        conn.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Sync Polymarket historical bet prices")
    parser.add_argument(
        "--fidelity",
        type=int,
        default=60,
        help="Data granularity in minutes (default: 60 = hourly)",
    )
    args = parser.parse_args()
    sync_all_markets(fidelity=args.fidelity)

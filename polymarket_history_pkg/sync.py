"""DB schema management and sync orchestration for bet price history."""

import os
import time
import logging

import psycopg2
from datetime import datetime, timezone
from dotenv import load_dotenv

from .clob_client import fetch_price_history

load_dotenv(override=True)

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


def upsert_price_history(cur, clob_token_id, history):
    """Bulk-upsert price history rows."""
    if not history:
        return 0

    dedup_by_ts = {}
    for point in history:
        ts = datetime.fromtimestamp(point["t"], tz=timezone.utc)
        dedup_by_ts[ts] = point["p"]

    rows = [(clob_token_id, ts, price) for ts, price in dedup_by_ts.items()]
    rows.sort(key=lambda r: r[1])

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

            time.sleep(0.3)

        logger.info(f"\nDone. Total price points stored: {total_points}")
    finally:
        conn.close()

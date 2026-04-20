"""
Polymarket historical bet-price sync — backward-compatibility shim.

All logic lives in ``polymarket_history_pkg``.  This file re-exports every
symbol so existing imports and ``python polymarket_history.py`` keep working.

The orchestration function ``sync_all_markets`` is defined here (rather than
merely re-exported) so that ``@patch("polymarket_history.fetch_price_history")``
in tests correctly intercepts calls made inside the function.
"""

import os
import time  # noqa: F401  — kept so @patch("polymarket_history.time.sleep") works
import logging
import requests  # noqa: F401  — kept so @patch("polymarket_history.requests.get") works
import psycopg2

from datetime import datetime, timezone
from dotenv import load_dotenv

from polymarket_history_pkg.clob_client import CLOB_BASE_URL, fetch_price_history  # noqa: F401
from polymarket_history_pkg.sync import (
    ensure_history_table,  # noqa: F401
    upsert_price_history,  # noqa: F401
)

load_dotenv(override=True)

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
            SELECT DISTINCT clob_token_id, market_id, side, underlying, level, direction, active, closed_time
            FROM price_events
            WHERE clob_token_id IS NOT NULL
            ORDER BY underlying, level
            """
        )
        markets = cur.fetchall()
        logger.info(f"Found {len(markets)} markets with CLOB token IDs")

        total_points = 0
        for i, row in enumerate(markets, 1):
            clob_id, mkt_id, side, underlying, level, direction, *rest = row
            active = rest[0] if len(rest) >= 1 else True
            closed_time = rest[1] if len(rest) >= 2 else None
            logger.info(
                f"[{i}/{len(markets)}] {underlying} {direction} ${level} "
                f"({side}) market_id={mkt_id}"
            )

            history = fetch_price_history(clob_id, fidelity=fidelity)
            if not history and (closed_time is not None or active is False):
                history = fetch_price_history(clob_id, fidelity=720)
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

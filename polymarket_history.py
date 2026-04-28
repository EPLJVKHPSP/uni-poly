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


def sync_all_markets(
    fidelity=60,
    resolution_types=("touch_any_time",),
    underlyings=None,
    sleep_s: float = 0.3,
):
    """
    For every market in price_events that has a clob_token_id,
    fetch historical prices and store them in bet_price_history.

    By default we restrict to ``touch_any_time`` markets (the IL-hedge
    instruments). Pass ``resolution_types=None`` to sync every market.
    """
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        ensure_history_table(cur)
        conn.commit()

        clauses = ["clob_token_id IS NOT NULL"]
        params: list = []
        if resolution_types:
            clauses.append("resolution_type = ANY(%s)")
            params.append(list(resolution_types))
        if underlyings:
            clauses.append("underlying = ANY(%s)")
            params.append(list(underlyings))
        where = " AND ".join(clauses)

        cur.execute(
            f"""
            SELECT DISTINCT clob_token_id, market_id, side, underlying, level, direction, active, closed_time
            FROM price_events
            WHERE {where}
            ORDER BY underlying, level
            """,
            params,
        )
        markets = cur.fetchall()
        logger.info(
            f"Found {len(markets)} markets with CLOB token IDs "
            f"(resolution_types={resolution_types}, underlyings={underlyings})"
        )

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

            time.sleep(sleep_s)

        logger.info(f"\nDone. Total price points stored: {total_points}")
    finally:
        conn.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Sync Polymarket historical bet prices")
    parser.add_argument("--fidelity", type=int, default=60, help="Granularity in minutes (60=hourly)")
    parser.add_argument(
        "--resolution-types",
        default="touch_any_time",
        help="Comma-separated resolution_types to sync. 'all' = no filter. Default: touch_any_time",
    )
    parser.add_argument("--underlyings", default="BTC,ETH", help="Comma-separated allowlist (default BTC,ETH)")
    parser.add_argument("--sleep", type=float, default=0.3, help="Sleep seconds between requests")
    args = parser.parse_args()

    rt = None if args.resolution_types.lower() == "all" else tuple(s.strip() for s in args.resolution_types.split(",") if s.strip())
    ul = None if not args.underlyings else tuple(s.strip().upper() for s in args.underlyings.split(",") if s.strip())
    sync_all_markets(fidelity=args.fidelity, resolution_types=rt, underlyings=ul, sleep_s=args.sleep)

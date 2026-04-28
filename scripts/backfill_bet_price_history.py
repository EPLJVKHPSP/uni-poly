"""Backfill ``bet_price_history`` for ETH/BTC touch-anytime markets that
ended in a given window and currently have **zero** rows in the table.

The default window is 2025-10-01 .. 2026-03-06 (Oct 2025 → just before the
existing Mar 5 2026 sync), which lifts the high-realism backtest window from
~49 days to ~180 days.

Markets are fetched in parallel against Polymarket's public CLOB
``/prices-history`` endpoint with a global rate limit. Already-synced markets
are skipped automatically — running this script repeatedly is idempotent and
cheap.

Usage::

    python -m scripts.backfill_bet_price_history          # default window
    python -m scripts.backfill_bet_price_history \\
        --start 2025-10-01 --end 2026-03-06 \\
        --workers 6 --rate 8 --fidelity 60
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import List, Tuple

import psycopg2
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from polymarket_history_pkg.clob_client import fetch_price_history  # noqa: E402
from polymarket_history_pkg.sync import (  # noqa: E402
    ensure_history_table,
    upsert_price_history,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("backfill")


def _connect():
    return psycopg2.connect(
        dbname=os.getenv("DB_NAME", "polymarket"),
        user=os.getenv("DB_USER", "polymarket"),
        password=os.getenv("DB_PASSWORD", "polymarket_pw"),
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", "5432")),
    )


def _select_targets(start: datetime, end: datetime) -> List[Tuple[str, int, str, str, float, str]]:
    """Distinct ETH/BTC touch markets ending in [start, end) that have **no**
    rows in ``bet_price_history`` yet."""
    sql = """
    SELECT DISTINCT pe.clob_token_id, pe.market_id, pe.side, pe.underlying,
                    pe.level::float8, pe.direction
    FROM price_events pe
    WHERE pe.underlying IN ('ETH','BTC')
      AND pe.resolution_type = 'touch_any_time'
      AND pe.clob_token_id IS NOT NULL
      AND pe.end_date >= %s
      AND pe.end_date <  %s
      AND NOT EXISTS (
          SELECT 1 FROM bet_price_history bph
          WHERE bph.clob_token_id = pe.clob_token_id
          LIMIT 1
      )
    ORDER BY pe.underlying, pe.level::float8
    """
    conn = _connect()
    try:
        cur = conn.cursor()
        ensure_history_table(cur)
        conn.commit()
        cur.execute(sql, (start, end))
        return cur.fetchall()
    finally:
        conn.close()


class _RateLimiter:
    """Simple shared token-bucket rate limiter (req/sec)."""

    def __init__(self, rate_per_second: float):
        self.interval = 1.0 / max(float(rate_per_second), 0.1)
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            if now < self._next:
                time.sleep(self._next - now)
                now = time.monotonic()
            self._next = now + self.interval


def _fetch_one(
    clob_id: str,
    fidelity: int,
    rate: _RateLimiter,
) -> Tuple[str, list]:
    rate.wait()
    history = fetch_price_history(clob_id, fidelity=fidelity)
    if not history:
        rate.wait()
        # Closed markets sometimes only respond on coarser fidelity.
        history = fetch_price_history(clob_id, fidelity=720)
    return clob_id, history or []


def main() -> int:
    load_dotenv(override=True)
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--start", default="2025-10-01", help="UTC start date (inclusive)")
    p.add_argument("--end", default="2026-03-06", help="UTC end date (exclusive)")
    p.add_argument("--workers", type=int, default=6)
    p.add_argument("--rate", type=float, default=8.0, help="max requests per second (global)")
    p.add_argument("--fidelity", type=int, default=60, help="minutes per bucket (60=hourly)")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    end = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc)

    targets = _select_targets(start, end)
    logger.info(
        "backfill window %s..%s -> %d markets to fetch (workers=%d, rate=%.1fr/s, fidelity=%dm)",
        start.date(), end.date(), len(targets), args.workers, args.rate, args.fidelity,
    )
    if args.dry_run or not targets:
        for row in targets[:10]:
            logger.info("  sample target: %s %s $%s (%s)", row[3], row[5], row[4], row[0][:16])
        return 0

    rate = _RateLimiter(args.rate)
    write_conn = _connect()
    write_cur = write_conn.cursor()

    total_points = 0
    completed = 0
    empty = 0
    started_at = time.monotonic()

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(_fetch_one, row[0], args.fidelity, rate): row
            for row in targets
        }
        for fut in as_completed(futures):
            row = futures[fut]
            clob_id = row[0]
            try:
                _id, history = fut.result()
            except Exception as exc:  # noqa: BLE001
                logger.warning("fetch failed for %s: %s", clob_id[:16], exc)
                continue
            n = upsert_price_history(write_cur, clob_id, history) if history else 0
            if n == 0:
                empty += 1
            total_points += n
            completed += 1
            if completed % 50 == 0 or completed == len(targets):
                write_conn.commit()
                elapsed = time.monotonic() - started_at
                rate_obs = completed / max(elapsed, 1e-6)
                eta = (len(targets) - completed) / max(rate_obs, 1e-6)
                logger.info(
                    "progress %d/%d (%.1f%%)  points=%s  empty=%d  elapsed=%.0fs  eta=%.0fs",
                    completed, len(targets), 100 * completed / max(len(targets), 1),
                    f"{total_points:,}", empty, elapsed, eta,
                )
    write_conn.commit()
    write_conn.close()

    logger.info("done. %d markets, %s price points, %d empty responses",
                completed, f"{total_points:,}", empty)
    return 0


if __name__ == "__main__":
    sys.exit(main())

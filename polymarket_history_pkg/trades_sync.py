"""Polymarket public trades sync.

Pulls historical trade prints from ``https://data-api.polymarket.com/trades``
into a local ``bet_trades`` table. We use these prints to:

- Derive a more realistic per-market spread/slippage curve than the flat
  ``spread`` config knob.
- Cross-check ``bet_price_history`` for outliers and gaps.

The data API returns *taker* fills (default) sorted by timestamp DESC. We use
keyset-style pagination via ``offset`` (max 10000) per condition_id.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from typing import Iterable, List, Optional, Tuple

import psycopg2
import requests
from dotenv import load_dotenv

load_dotenv(override=True)

logger = logging.getLogger(__name__)

DATA_API_BASE = "https://data-api.polymarket.com"
PAGE_LIMIT = 500


def _connect():
    return psycopg2.connect(
        dbname=os.getenv("DB_NAME", "polymarket"),
        user=os.getenv("DB_USER", "polymarket"),
        password=os.getenv("DB_PASSWORD", "polymarket_pw"),
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", "5432")),
    )


def ensure_trades_table(cur) -> None:
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS bet_trades (
            tx_hash         TEXT NOT NULL,
            asset           TEXT NOT NULL,
            condition_id    TEXT,
            side            TEXT NOT NULL,   -- 'BUY' | 'SELL'
            price           NUMERIC NOT NULL,
            size            NUMERIC NOT NULL,
            ts              TIMESTAMPTZ NOT NULL,
            outcome_index   INT,
            taker_wallet    TEXT,
            PRIMARY KEY (tx_hash, asset, side, ts)
        )
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_bet_trades_asset_ts
        ON bet_trades(asset, ts)
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_bet_trades_condition_ts
        ON bet_trades(condition_id, ts)
        """
    )


def _fetch_trades_page(condition_id: str, offset: int) -> list:
    """Fetch one page of trades for a condition from data-api.

    The endpoint accepts ``market`` as a comma-separated array of conditionIds
    and returns at most ``limit`` rows.
    """
    params = {
        "market": condition_id,
        "limit": PAGE_LIMIT,
        "offset": offset,
        "takerOnly": "true",
    }
    last_exc: Optional[Exception] = None
    for attempt in range(5):
        try:
            r = requests.get(f"{DATA_API_BASE}/trades", params=params, timeout=20)
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(0.4 * (2 ** attempt))
                continue
            r.raise_for_status()
            data = r.json()
            return data if isinstance(data, list) else (data.get("data") or [])
        except Exception as exc:
            last_exc = exc
            time.sleep(0.3 * (2 ** attempt))
    if last_exc:
        logger.warning("Failed to fetch trades for %s offset=%s: %s", condition_id, offset, last_exc)
    return []


def fetch_all_trades(condition_id: str, max_offset: int = 10_000) -> list:
    """Walk pages until empty or the API offset cap is reached."""
    rows: list = []
    offset = 0
    while offset < max_offset:
        page = _fetch_trades_page(condition_id, offset)
        if not page:
            break
        rows.extend(page)
        if len(page) < PAGE_LIMIT:
            break
        offset += PAGE_LIMIT
        time.sleep(0.06)
    return rows


def upsert_trades(cur, condition_id: str, trades: Iterable[dict]) -> int:
    """Insert/upsert raw trade rows into bet_trades."""
    rows: List[Tuple] = []
    for t in trades:
        try:
            ts = datetime.fromtimestamp(int(t["timestamp"]), tz=timezone.utc)
        except (KeyError, TypeError, ValueError):
            continue
        tx = t.get("transactionHash") or t.get("tx_hash") or ""
        asset = t.get("asset") or ""
        if not tx or not asset:
            continue
        rows.append(
            (
                tx,
                asset,
                condition_id or t.get("conditionId"),
                str(t.get("side") or "").upper(),
                float(t.get("price") or 0.0),
                float(t.get("size") or 0.0),
                ts,
                t.get("outcomeIndex"),
                t.get("proxyWallet"),
            )
        )
    if not rows:
        return 0
    from psycopg2.extras import execute_values

    execute_values(
        cur,
        """
        INSERT INTO bet_trades
        (tx_hash, asset, condition_id, side, price, size, ts, outcome_index, taker_wallet)
        VALUES %s
        ON CONFLICT (tx_hash, asset, side, ts)
        DO UPDATE SET price = EXCLUDED.price, size = EXCLUDED.size
        """,
        rows,
    )
    return len(rows)


def sync_all_trades(
    only_active: bool = False,
    resolution_types=("touch_any_time",),
    underlyings=("BTC", "ETH"),
) -> None:
    """For every distinct condition_id present in price_events, fetch & store trades.

    Defaults restrict to ``touch_any_time`` BTC/ETH markets — the IL-hedge
    universe. Pass ``resolution_types=None`` and ``underlyings=None`` to
    sync the entire ``price_events`` universe (slow).
    """
    conn = _connect()
    try:
        cur = conn.cursor()
        ensure_trades_table(cur)
        conn.commit()

        clauses = ["condition_id IS NOT NULL"]
        params: list = []
        if only_active:
            clauses.append("active = true")
        if resolution_types:
            clauses.append("resolution_type = ANY(%s)")
            params.append(list(resolution_types))
        if underlyings:
            clauses.append("underlying = ANY(%s)")
            params.append(list(underlyings))
        where = "WHERE " + " AND ".join(clauses)
        cur.execute(
            f"SELECT DISTINCT condition_id FROM price_events {where}",
            params,
        )
        condition_ids = [r[0] for r in cur.fetchall()]
        logger.info("Trades sync: %d distinct conditions to scan", len(condition_ids))

        total = 0
        for i, cid in enumerate(condition_ids, 1):
            trades = fetch_all_trades(cid)
            n = upsert_trades(cur, cid, trades)
            conn.commit()
            total += n
            if i % 25 == 0 or i == len(condition_ids):
                logger.info("  [%d/%d] %d trades stored so far", i, len(condition_ids), total)
            time.sleep(0.05)

        logger.info("Trades sync done. Total inserted/updated rows: %d", total)
    finally:
        conn.close()


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="Sync Polymarket trade prints")
    ap.add_argument("--only-active", action="store_true", default=os.getenv("TRADES_ONLY_ACTIVE", "false").lower() == "true")
    ap.add_argument("--resolution-types", default="touch_any_time", help="Comma-separated, or 'all'")
    ap.add_argument("--underlyings", default="BTC,ETH", help="Comma-separated allowlist; empty=all")
    args = ap.parse_args()
    rt = None if args.resolution_types.lower() == "all" else tuple(s.strip() for s in args.resolution_types.split(",") if s.strip())
    ul = tuple(s.strip().upper() for s in args.underlyings.split(",") if s.strip()) or None
    sync_all_trades(only_active=args.only_active, resolution_types=rt, underlyings=ul)

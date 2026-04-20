"""Database utilities for Polymarket data extraction and queries."""

import os
import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv
from typing import List, Dict, Optional, Any

load_dotenv(override=True)


def get_db_connection():
    """Create PostgreSQL connection using environment variables or defaults."""
    db_config = {
        "dbname": os.getenv("DB_NAME", "polymarket"),
        "user": os.getenv("DB_USER", "polymarket"),
        "password": os.getenv("DB_PASSWORD", "polymarket_pw"),
        "host": os.getenv("DB_HOST", "localhost"),
        "port": int(os.getenv("DB_PORT", "5432")),
    }
    return psycopg2.connect(**db_config)


def get_range_combinations(token_symbol: str, conn=None, candle_ts=None) -> List[Dict]:
    """
    Get all range combinations for a given token from Polymarket data.

    Extracts all 'down' direction levels (lower bounds) and 'up' direction
    levels (upper bounds), then generates all valid combinations where min < max.
    """
    if conn is None:
        conn = get_db_connection()
        should_close = True
    else:
        should_close = False

    from datetime import datetime, timezone as tz

    if candle_ts is not None and isinstance(candle_ts, (int, float)):
        candle_ts = datetime.fromtimestamp(int(candle_ts), tz=tz.utc)

    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if candle_ts is None:
                cur.execute(
                    """
                    SELECT DISTINCT ON (level)
                        level, price, market_id, market_question, event_id
                    FROM price_events
                    WHERE underlying = %s AND direction = 'down'
                      AND side = 'Yes' AND active = true
                    ORDER BY level, price ASC
                    """,
                    (token_symbol,),
                )
            else:
                cur.execute(
                    """
                    SELECT DISTINCT ON (level)
                        level, price, market_id, market_question, event_id
                    FROM price_events
                    WHERE underlying = %s AND direction = 'down'
                      AND side = 'Yes'
                      AND (created_at IS NULL OR created_at <= %s)
                      AND (end_date IS NULL OR end_date >= %s)
                    ORDER BY level, price ASC
                    """,
                    (token_symbol, candle_ts, candle_ts),
                )
            down_levels = cur.fetchall()

            if candle_ts is None:
                cur.execute(
                    """
                    SELECT DISTINCT ON (level)
                        level, price, market_id, market_question, event_id
                    FROM price_events
                    WHERE underlying = %s AND direction = 'up'
                      AND side = 'Yes' AND active = true
                    ORDER BY level, price ASC
                    """,
                    (token_symbol,),
                )
            else:
                cur.execute(
                    """
                    SELECT DISTINCT ON (level)
                        level, price, market_id, market_question, event_id
                    FROM price_events
                    WHERE underlying = %s AND direction = 'up'
                      AND side = 'Yes'
                      AND (created_at IS NULL OR created_at <= %s)
                      AND (end_date IS NULL OR end_date >= %s)
                    ORDER BY level, price ASC
                    """,
                    (token_symbol, candle_ts, candle_ts),
                )
            up_levels = cur.fetchall()

            combinations = []
            for down in down_levels:
                for up in up_levels:
                    min_level = float(down['level'])
                    max_level = float(up['level'])
                    if min_level < max_level:
                        combinations.append({
                            "min": min_level,
                            "max": max_level,
                            "lower_bet_price": float(down['price']),
                            "upper_bet_price": float(up['price']),
                            "lower_market_id": int(down['market_id']),
                            "upper_market_id": int(up['market_id']),
                            "lower_market_question": down['market_question'],
                            "upper_market_question": up['market_question'],
                            "lower_event_id": int(down['event_id']),
                            "upper_event_id": int(up['event_id']),
                        })

            return combinations
    finally:
        if should_close:
            conn.close()


def get_clob_token_id(
    token_symbol: str,
    level: float,
    direction: str,
    side: str = "Yes",
    conn=None,
    candle_ts=None,
) -> Optional[str]:
    """Look up the CLOB token ID for a specific Polymarket market."""
    from datetime import datetime, timezone as tz

    if candle_ts is not None and isinstance(candle_ts, (int, float)):
        candle_ts = datetime.fromtimestamp(int(candle_ts), tz=tz.utc)

    if conn is None:
        conn = get_db_connection()
        should_close = True
    else:
        should_close = False

    try:
        with conn.cursor() as cur:
            if candle_ts is None:
                cur.execute(
                    """
                    SELECT clob_token_id
                    FROM price_events
                    WHERE underlying = %s
                      AND level = %s
                      AND direction = %s
                      AND side = %s
                      AND active = true
                      AND clob_token_id IS NOT NULL
                    LIMIT 1
                    """,
                    (token_symbol, level, direction, side),
                )
            else:
                cur.execute(
                    """
                    SELECT clob_token_id
                    FROM price_events
                    WHERE underlying = %s
                      AND level = %s
                      AND direction = %s
                      AND side = %s
                      AND clob_token_id IS NOT NULL
                      AND (created_at IS NULL OR created_at <= %s)
                      AND (end_date IS NULL OR end_date >= %s)
                    LIMIT 1
                    """,
                    (token_symbol, level, direction, side, candle_ts, candle_ts),
                )
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        if should_close:
            conn.close()


def get_historical_bet_price(
    clob_token_id: str,
    target_ts,
    conn=None,
) -> Optional[float]:
    """
    Get the bet price at (or closest around) a target timestamp.
    Uses the bet_price_history table populated by polymarket_history.py.
    """
    from datetime import datetime, timezone as tz

    if isinstance(target_ts, (int, float)):
        target_ts = datetime.fromtimestamp(target_ts, tz=tz.utc)

    if conn is None:
        conn = get_db_connection()
        should_close = True
    else:
        should_close = False

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT price
                FROM bet_price_history
                WHERE clob_token_id = %s AND ts <= %s
                ORDER BY ts DESC
                LIMIT 1
                """,
                (clob_token_id, target_ts),
            )
            row = cur.fetchone()
            if row:
                return float(row[0])

            cur.execute(
                """
                SELECT price
                FROM bet_price_history
                WHERE clob_token_id = %s AND ts >= %s
                ORDER BY ts ASC
                LIMIT 1
                """,
                (clob_token_id, target_ts),
            )
            row = cur.fetchone()
            return float(row[0]) if row else None
    finally:
        if should_close:
            conn.close()


def get_candidate_markets(
    token_symbol: str,
    level: float,
    direction: str,
    side: str = "Yes",
    conn=None,
    candle_ts=None,
) -> List[Dict[str, Any]]:
    """
    Return candidate Polymarket markets for a given (underlying, level, direction, side)
    that are valid at candle_ts and have a future (non-null) end_date.

    Each row includes: clob_token_id, market_id, end_date.
    """
    from datetime import datetime, timezone as tz

    if candle_ts is not None and isinstance(candle_ts, (int, float)):
        candle_ts = datetime.fromtimestamp(int(candle_ts), tz=tz.utc)

    if conn is None:
        conn = get_db_connection()
        should_close = True
    else:
        should_close = False

    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if candle_ts is None:
                cur.execute(
                    """
                    SELECT clob_token_id, market_id, end_date
                    FROM price_events
                    WHERE underlying = %s
                      AND level = %s
                      AND direction = %s
                      AND side = %s
                      AND active = true
                      AND clob_token_id IS NOT NULL
                      AND end_date IS NOT NULL
                    ORDER BY end_date ASC
                    """,
                    (token_symbol, level, direction, side),
                )
            else:
                cur.execute(
                    """
                    SELECT clob_token_id, market_id, end_date
                    FROM price_events
                    WHERE underlying = %s
                      AND level = %s
                      AND direction = %s
                      AND side = %s
                      AND clob_token_id IS NOT NULL
                      AND end_date IS NOT NULL
                      AND end_date > %s
                      AND (created_at IS NULL OR created_at <= %s)
                    ORDER BY end_date ASC
                    """,
                    (token_symbol, level, direction, side, candle_ts, candle_ts),
                )
            return list(cur.fetchall())
    finally:
        if should_close:
            conn.close()


def has_any_price_history(clob_token_id: str, conn=None) -> bool:
    """True if bet_price_history has any rows for clob_token_id."""
    if conn is None:
        conn = get_db_connection()
        should_close = True
    else:
        should_close = False
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1
                FROM bet_price_history
                WHERE clob_token_id = %s
                LIMIT 1
                """,
                (clob_token_id,),
            )
            return cur.fetchone() is not None
    finally:
        if should_close:
            conn.close()


def has_past_price_history(clob_token_id: str, target_ts, conn=None) -> bool:
    """True if bet_price_history has at least one row with ts <= target_ts."""
    from datetime import datetime, timezone as tz

    if isinstance(target_ts, (int, float)):
        target_ts = datetime.fromtimestamp(int(target_ts), tz=tz.utc)

    if conn is None:
        conn = get_db_connection()
        should_close = True
    else:
        should_close = False
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1
                FROM bet_price_history
                WHERE clob_token_id = %s AND ts <= %s
                LIMIT 1
                """,
                (clob_token_id, target_ts),
            )
            return cur.fetchone() is not None
    finally:
        if should_close:
            conn.close()

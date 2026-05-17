"""Database utilities for Polymarket data extraction and queries."""

import os
from dotenv import load_dotenv
from typing import List, Dict, Optional, Any, Tuple

load_dotenv(override=True)


def _psycopg2_connect(**kwargs):
    try:
        import psycopg2
    except ImportError as exc:  # insured-only dependency
        raise ImportError(
            "Polymarket / insured-range backtests need PostgreSQL. "
            "Install: pip install psycopg2-binary"
        ) from exc
    return psycopg2.connect(**kwargs)


def _real_dict_cursor():
    from psycopg2.extras import RealDictCursor

    return RealDictCursor


def get_db_connection():
    """Create PostgreSQL connection using environment variables or defaults."""
    db_config = {
        "dbname": os.getenv("DB_NAME", "polymarket"),
        "user": os.getenv("DB_USER", "polymarket"),
        "password": os.getenv("DB_PASSWORD", "polymarket_pw"),
        "host": os.getenv("DB_HOST", "localhost"),
        "port": int(os.getenv("DB_PORT", "5432")),
    }
    return _psycopg2_connect(**db_config)


def _resolution_filter_sql(cur, restrict_to_touch: bool) -> str:
    """Return a SQL fragment restricting to touch-style markets when possible.

    Defensive: if the ``resolution_type`` column doesn't exist (legacy DBs that
    haven't been re-parsed yet) we silently no-op so that callers keep working.
    """
    if not restrict_to_touch:
        return ""
    try:
        cur.execute(
            """
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'price_events' AND column_name = 'resolution_type'
            """
        )
        if cur.fetchone() is None:
            return ""
    except Exception:
        return ""
    return " AND (resolution_type IS NULL OR resolution_type = 'touch_any_time') "


def get_range_combinations(
    token_symbol: str,
    conn=None,
    candle_ts=None,
    restrict_to_touch_markets: bool = False,
) -> List[Dict]:
    """
    Get all range combinations for a given token from Polymarket data.

    Extracts all 'down' direction levels (lower bounds) and 'up' direction
    levels (upper bounds), then generates all valid combinations where min < max.

    When ``restrict_to_touch_markets=True``, only markets whose
    ``resolution_type`` column is ``touch_any_time`` (or NULL for legacy rows)
    are considered — this is the only resolution rule that geometrically
    matches an LP's barrier-hit risk.
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
        with conn.cursor(cursor_factory=_real_dict_cursor()) as cur:
            touch_filter = _resolution_filter_sql(cur, restrict_to_touch_markets)
            if candle_ts is None:
                cur.execute(
                    f"""
                    SELECT DISTINCT ON (level)
                        level, price, market_id, market_question, event_id
                    FROM price_events
                    WHERE underlying = %s AND direction = 'down'
                      AND side = 'Yes' AND active = true
                      {touch_filter}
                    ORDER BY level, price ASC
                    """,
                    (token_symbol,),
                )
            else:
                cur.execute(
                    f"""
                    SELECT DISTINCT ON (level)
                        level, price, market_id, market_question, event_id
                    FROM price_events
                    WHERE underlying = %s AND direction = 'down'
                      AND side = 'Yes'
                      AND (created_at IS NULL OR created_at <= %s)
                      AND (end_date IS NULL OR end_date >= %s)
                      {touch_filter}
                    ORDER BY level, price ASC
                    """,
                    (token_symbol, candle_ts, candle_ts),
                )
            down_levels = cur.fetchall()

            if candle_ts is None:
                cur.execute(
                    f"""
                    SELECT DISTINCT ON (level)
                        level, price, market_id, market_question, event_id
                    FROM price_events
                    WHERE underlying = %s AND direction = 'up'
                      AND side = 'Yes' AND active = true
                      {touch_filter}
                    ORDER BY level, price ASC
                    """,
                    (token_symbol,),
                )
            else:
                cur.execute(
                    f"""
                    SELECT DISTINCT ON (level)
                        level, price, market_id, market_question, event_id
                    FROM price_events
                    WHERE underlying = %s AND direction = 'up'
                      AND side = 'Yes'
                      AND (created_at IS NULL OR created_at <= %s)
                      AND (end_date IS NULL OR end_date >= %s)
                      {touch_filter}
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
    restrict_to_touch_markets: bool = False,
    min_market_volume: float = 0.0,
) -> Optional[str]:
    """Look up the CLOB token ID for a specific Polymarket market.

    Selection priority (deepest-first):
      1. ``market_volume DESC NULLS LAST`` — pick the most-traded market
         at this (level, direction). Higher volume == better depth ==
         lower slippage when we hit the order book.
      2. ``end_date ASC`` as a tiebreaker — prefer the soonest expiry
         when depth is identical (legacy behaviour).

    Optional filters:
      - ``restrict_to_touch_markets``: drop markets whose
        ``resolution_type`` is not ``touch_any_time`` (or NULL). Touch is
        the only resolution geometry that matches LP barrier-hit risk.
      - ``min_market_volume``: drop markets whose cumulative trading
        volume is below this USD threshold. Set to e.g. 1000 to refuse
        ghost markets that have never been traded.
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
        with conn.cursor() as cur:
            touch_filter = _resolution_filter_sql(cur, restrict_to_touch_markets)
            vol_filter = ""
            params_extra: tuple = ()
            if min_market_volume and float(min_market_volume) > 0.0:
                vol_filter = " AND market_volume IS NOT NULL AND market_volume >= %s "
                params_extra = (float(min_market_volume),)
            if candle_ts is None:
                cur.execute(
                    f"""
                    SELECT clob_token_id
                    FROM price_events
                    WHERE underlying = %s
                      AND level = %s
                      AND direction = %s
                      AND side = %s
                      AND active = true
                      AND clob_token_id IS NOT NULL
                      {touch_filter}
                      {vol_filter}
                    ORDER BY market_volume DESC NULLS LAST, end_date ASC NULLS LAST
                    LIMIT 1
                    """,
                    (token_symbol, level, direction, side, *params_extra),
                )
            else:
                cur.execute(
                    f"""
                    SELECT clob_token_id
                    FROM price_events
                    WHERE underlying = %s
                      AND level = %s
                      AND direction = %s
                      AND side = %s
                      AND clob_token_id IS NOT NULL
                      AND (created_at IS NULL OR created_at <= %s)
                      AND (end_date IS NULL OR end_date >= %s)
                      {touch_filter}
                      {vol_filter}
                    ORDER BY market_volume DESC NULLS LAST, end_date ASC NULLS LAST
                    LIMIT 1
                    """,
                    (token_symbol, level, direction, side, candle_ts, candle_ts, *params_extra),
                )
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        if should_close:
            conn.close()


def get_clob_token_id_with_meta(
    token_symbol: str,
    level: float,
    direction: str,
    side: str = "Yes",
    conn=None,
    candle_ts=None,
    restrict_to_touch_markets: bool = False,
    min_market_volume: float = 0.0,
) -> Optional[Tuple[str, Any, float]]:
    """Like get_clob_token_id but also returns end_date and market_volume.

    Returns ``(clob_token_id, end_date, market_volume)`` or None when no
    row matches. The third tuple element is 0.0 when ``market_volume`` is
    NULL in the DB (lets callers treat it as a depth proxy uniformly).

    Same depth-first ordering and optional touch / min-volume filters as
    ``get_clob_token_id``.
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
        with conn.cursor() as cur:
            touch_filter = _resolution_filter_sql(cur, restrict_to_touch_markets)
            vol_filter = ""
            params_extra: tuple = ()
            if min_market_volume and float(min_market_volume) > 0.0:
                vol_filter = " AND market_volume IS NOT NULL AND market_volume >= %s "
                params_extra = (float(min_market_volume),)
            if candle_ts is None:
                cur.execute(
                    f"""
                    SELECT clob_token_id, end_date, market_volume
                    FROM price_events
                    WHERE underlying = %s
                      AND level = %s
                      AND direction = %s
                      AND side = %s
                      AND active = true
                      AND clob_token_id IS NOT NULL
                      {touch_filter}
                      {vol_filter}
                    ORDER BY market_volume DESC NULLS LAST, end_date ASC NULLS LAST
                    LIMIT 1
                    """,
                    (token_symbol, level, direction, side, *params_extra),
                )
            else:
                cur.execute(
                    f"""
                    SELECT clob_token_id, end_date, market_volume
                    FROM price_events
                    WHERE underlying = %s
                      AND level = %s
                      AND direction = %s
                      AND side = %s
                      AND clob_token_id IS NOT NULL
                      AND (created_at IS NULL OR created_at <= %s)
                      AND (end_date IS NULL OR end_date >= %s)
                      {touch_filter}
                      {vol_filter}
                    ORDER BY market_volume DESC NULLS LAST, end_date ASC NULLS LAST
                    LIMIT 1
                    """,
                    (token_symbol, level, direction, side, candle_ts, candle_ts, *params_extra),
                )
            row = cur.fetchone()
            if not row:
                return None
            mv = float(row[2]) if row[2] is not None else 0.0
            return (row[0], row[1], mv)
    finally:
        if should_close:
            conn.close()


def get_historical_bet_price(
    clob_token_id: str,
    target_ts,
    conn=None,
    strict_past: bool = True,
) -> Optional[float]:
    """
    Get the bet price at (or closest around) a target timestamp.
    Uses the bet_price_history table populated by polymarket_history.py.

    When ``strict_past=True`` (the default) only rows with ``ts <= target_ts``
    are considered, returning None if none exist. This is the no-lookahead
    semantics required by the simulator. Pass ``strict_past=False`` to allow
    falling back to the closest *future* row (only useful for ad-hoc lookups
    or for filling gaps after a backtest run).
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

            if strict_past:
                return None

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
    restrict_to_touch_markets: bool = False,
    min_market_volume: float = 0.0,
) -> List[Dict[str, Any]]:
    """
    Return candidate Polymarket markets for a given (underlying, level,
    direction, side) that are valid at candle_ts and have a future
    (non-null) end_date.

    Each row includes: ``clob_token_id``, ``market_id``, ``end_date``,
    and ``market_volume`` (the depth proxy used by downstream slippage
    estimation).

    Selection priority is **deepest-first**: rows are ordered by
    ``market_volume DESC NULLS LAST``, then ``end_date ASC`` as a
    tiebreaker. Callers that take ``[0]`` therefore get the deepest
    market available at the (level, direction, candle_ts) cell, which
    minimises slippage at execution. The previous behaviour ordered by
    ``end_date ASC`` only and systematically picked the smallest-volume
    market at each open.

    When ``restrict_to_touch_markets=True``, drop non-touch markets so
    the hedge geometry actually matches LP barrier-hit risk.

    When ``min_market_volume > 0``, drop ghost markets whose cumulative
    USD trading volume is below the threshold (e.g. recommend ~$1k to
    skip never-traded mids).
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
        with conn.cursor(cursor_factory=_real_dict_cursor()) as cur:
            touch_filter = _resolution_filter_sql(cur, restrict_to_touch_markets)
            vol_filter = ""
            params_extra: tuple = ()
            if min_market_volume and float(min_market_volume) > 0.0:
                vol_filter = " AND market_volume IS NOT NULL AND market_volume >= %s "
                params_extra = (float(min_market_volume),)
            if candle_ts is None:
                cur.execute(
                    f"""
                    SELECT clob_token_id, market_id, end_date, market_volume
                    FROM price_events
                    WHERE underlying = %s
                      AND level = %s
                      AND direction = %s
                      AND side = %s
                      AND active = true
                      AND clob_token_id IS NOT NULL
                      AND end_date IS NOT NULL
                      {touch_filter}
                      {vol_filter}
                    ORDER BY market_volume DESC NULLS LAST, end_date ASC
                    """,
                    (token_symbol, level, direction, side, *params_extra),
                )
            else:
                cur.execute(
                    f"""
                    SELECT clob_token_id, market_id, end_date, market_volume
                    FROM price_events
                    WHERE underlying = %s
                      AND level = %s
                      AND direction = %s
                      AND side = %s
                      AND clob_token_id IS NOT NULL
                      AND end_date IS NOT NULL
                      AND end_date > %s
                      AND (created_at IS NULL OR created_at <= %s)
                      {touch_filter}
                      {vol_filter}
                    ORDER BY market_volume DESC NULLS LAST, end_date ASC
                    """,
                    (token_symbol, level, direction, side, candle_ts, candle_ts, *params_extra),
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

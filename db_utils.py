"""Database utilities for Polymarket data extraction and queries."""

import os
import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv
from typing import List, Dict, Optional, Tuple
from decimal import Decimal

# Load environment variables
load_dotenv()


def get_db_connection():
    """
    Create PostgreSQL connection using environment variables or defaults.
    
    Returns:
        psycopg2.connection: Database connection object
    """
    db_config = {
        "dbname": os.getenv("DB_NAME", "polymarket"),
        "user": os.getenv("DB_USER", "polymarket"),
        "password": os.getenv("DB_PASSWORD", "polymarket_pw"),
        "host": os.getenv("DB_HOST", "localhost"),
        "port": int(os.getenv("DB_PORT", "5432")),
    }
    
    return psycopg2.connect(**db_config)


def get_unique_tokens(conn=None) -> List[str]:
    """
    Get all unique token symbols from price_events table.
    
    Args:
        conn: Optional database connection. If None, creates new connection.
        
    Returns:
        List[str]: List of unique token symbols (e.g., ['BTC', 'ETH', 'SOL', ...])
    """
    if conn is None:
        conn = get_db_connection()
        should_close = True
    else:
        should_close = False
    
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT underlying 
                FROM price_events 
                WHERE underlying IS NOT NULL 
                AND active = true
                ORDER BY underlying
                """
            )
            tokens = [row[0] for row in cur.fetchall()]
            return tokens
    finally:
        if should_close:
            conn.close()


def get_range_combinations(token_symbol: str, conn=None) -> List[Dict]:
    """
    Get all range combinations for a given token from Polymarket data.
    
    Extracts all 'down' direction levels (lower bounds) and 'up' direction 
    levels (upper bounds), then generates all valid combinations.
    
    Args:
        token_symbol: Token symbol (e.g., 'ETH', 'BTC')
        conn: Optional database connection. If None, creates new connection.
        
    Returns:
        List[Dict]: List of range dictionaries with structure:
            [{
                "min": 2400.0,
                "max": 4000.0,
                "lower_bet_price": 0.27,
                "upper_bet_price": 0.125,
                "lower_market_id": 800463,
                "upper_market_id": 800451,
                ...
            }, ...]
    """
    if conn is None:
        conn = get_db_connection()
        should_close = True
    else:
        should_close = False
    
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Get all 'down' direction levels (lower bounds)
            cur.execute(
                """
                SELECT DISTINCT ON (level)
                    level,
                    price,
                    market_id,
                    market_question,
                    event_id
                FROM price_events
                WHERE underlying = %s
                AND direction = 'down'
                AND side = 'Yes'
                AND active = true
                ORDER BY level, price ASC
                """,
                (token_symbol,)
            )
            down_levels = cur.fetchall()
            
            # Get all 'up' direction levels (upper bounds)
            cur.execute(
                """
                SELECT DISTINCT ON (level)
                    level,
                    price,
                    market_id,
                    market_question,
                    event_id
                FROM price_events
                WHERE underlying = %s
                AND direction = 'up'
                AND side = 'Yes'
                AND active = true
                ORDER BY level, price ASC
                """,
                (token_symbol,)
            )
            up_levels = cur.fetchall()
            
            # Generate all combinations
            combinations = []
            for down in down_levels:
                for up in up_levels:
                    min_level = float(down['level'])
                    max_level = float(up['level'])
                    
                    # Only keep valid ranges (min < max)
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


def get_insurance_cost(
    token_symbol: str,
    level: float,
    direction: str,
    coverage_amount: float,
    conn=None
) -> Optional[float]:
    """
    Calculate the cost to buy insurance coverage for a specific level.
    
    Args:
        token_symbol: Token symbol (e.g., 'ETH')
        level: Price level to insure
        direction: 'down' or 'up'
        coverage_amount: Amount to cover in USD (e.g., 50.0 for $50 coverage)
        conn: Optional database connection
        
    Returns:
        float: Cost in USD to buy the insurance, or None if market not found
        
    Example:
        To cover $50 loss if ETH drops to $2400:
        cost = get_insurance_cost('ETH', 2400.0, 'down', 50.0)
        # If bet price is $0.27, returns 50 * 0.27 = 13.5
    """
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
                FROM price_events
                WHERE underlying = %s
                AND level = %s
                AND direction = %s
                AND side = 'Yes'
                AND active = true
                ORDER BY price ASC
                LIMIT 1
                """,
                (token_symbol, level, direction)
            )
            
            result = cur.fetchone()
            if result is None:
                return None
            
            bet_price = float(result[0])
            
            # To get coverage_amount payout, we need coverage_amount contracts
            # Each contract costs bet_price and pays $1 if it wins
            # Cost = number_of_contracts * price_per_contract
            cost = coverage_amount * bet_price
            
            return cost
    finally:
        if should_close:
            conn.close()


def get_insurance_market_info(
    token_symbol: str,
    level: float,
    direction: str,
    conn=None
) -> Optional[Dict]:
    """
    Get market information for insurance at a specific level.
    
    Args:
        token_symbol: Token symbol (e.g., 'ETH')
        level: Price level to insure
        direction: 'down' or 'up'
        conn: Optional database connection
        
    Returns:
        Dict with market information or None if not found:
        {
            'market_id': int,
            'market_question': str,
            'event_id': int,
            'price': float,
            'level': float
        }
    """
    if conn is None:
        conn = get_db_connection()
        should_close = True
    else:
        should_close = False
    
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT 
                    market_id,
                    market_question,
                    event_id,
                    price,
                    level
                FROM price_events
                WHERE underlying = %s
                AND level = %s
                AND direction = %s
                AND side = 'Yes'
                AND active = true
                ORDER BY price ASC
                LIMIT 1
                """,
                (token_symbol, level, direction)
            )
            
            result = cur.fetchone()
            if result is None:
                return None
            
            return {
                'market_id': int(result['market_id']),
                'market_question': result['market_question'],
                'event_id': int(result['event_id']),
                'price': float(result['price']),
                'level': float(result['level'])
            }
    finally:
        if should_close:
            conn.close()


def get_bet_price(
    token_symbol: str,
    level: float,
    direction: str,
    conn=None
) -> Optional[float]:
    """
    Get the current bet price for a specific level and direction.
    
    Args:
        token_symbol: Token symbol
        level: Price level
        direction: 'down' or 'up'
        conn: Optional database connection
        
    Returns:
        float: Bet price (0.0 to 1.0), or None if not found
    """
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
                FROM price_events
                WHERE underlying = %s
                AND level = %s
                AND direction = %s
                AND side = 'Yes'
                AND active = true
                ORDER BY price ASC
                LIMIT 1
                """,
                (token_symbol, level, direction)
            )
            
            result = cur.fetchone()
            if result is None:
                return None
            
            return float(result[0])
    finally:
        if should_close:
            conn.close()


def get_clob_token_id(
    token_symbol: str,
    level: float,
    direction: str,
    side: str = "Yes",
    conn=None,
) -> Optional[str]:
    """
    Look up the CLOB token ID for a specific market.

    Args:
        token_symbol: e.g. 'ETH'
        level: Price level
        direction: 'down' or 'up'
        side: 'Yes' or 'No'
        conn: Optional DB connection

    Returns:
        CLOB token ID string, or None if not found
    """
    if conn is None:
        conn = get_db_connection()
        should_close = True
    else:
        should_close = False

    try:
        with conn.cursor() as cur:
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
    Get the bet price closest to (but not after) a target timestamp.

    Uses the bet_price_history table populated by polymarket_history.py.

    Args:
        clob_token_id: CLOB token ID
        target_ts: datetime or unix timestamp
        conn: Optional DB connection

    Returns:
        Price as float, or None if no data
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
                WHERE clob_token_id = %s
                  AND ts <= %s
                ORDER BY ts DESC
                LIMIT 1
                """,
                (clob_token_id, target_ts),
            )
            row = cur.fetchone()
            return float(row[0]) if row else None
    finally:
        if should_close:
            conn.close()


def get_historical_bet_price_series(
    clob_token_id: str,
    start_ts=None,
    end_ts=None,
    conn=None,
) -> List[Dict]:
    """
    Get the full price time-series for a CLOB token between two timestamps.

    Args:
        clob_token_id: CLOB token ID
        start_ts: Start datetime or unix timestamp (optional)
        end_ts: End datetime or unix timestamp (optional)
        conn: Optional DB connection

    Returns:
        List of {'ts': datetime, 'price': float} dicts, ordered by time
    """
    from datetime import datetime, timezone as tz

    def _to_dt(v):
        if v is None:
            return None
        if isinstance(v, (int, float)):
            return datetime.fromtimestamp(v, tz=tz.utc)
        return v

    start_ts = _to_dt(start_ts)
    end_ts = _to_dt(end_ts)

    if conn is None:
        conn = get_db_connection()
        should_close = True
    else:
        should_close = False

    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            conditions = ["clob_token_id = %s"]
            params: list = [clob_token_id]

            if start_ts:
                conditions.append("ts >= %s")
                params.append(start_ts)
            if end_ts:
                conditions.append("ts <= %s")
                params.append(end_ts)

            query = f"""
                SELECT ts, price
                FROM bet_price_history
                WHERE {' AND '.join(conditions)}
                ORDER BY ts ASC
            """
            cur.execute(query, params)
            return [
                {"ts": row["ts"], "price": float(row["price"])}
                for row in cur.fetchall()
            ]
    finally:
        if should_close:
            conn.close()


if __name__ == "__main__":
    print("Testing db_utils...")
    
    conn = get_db_connection()
    
    print("\nUnique tokens:")
    tokens = get_unique_tokens(conn)
    print(tokens)
    
    if tokens:
        test_token = tokens[0]
        print(f"\nRange combinations for {test_token}:")
        ranges = get_range_combinations(test_token, conn)
        print(f"Found {len(ranges)} combinations")
        if ranges:
            print("Sample range:", ranges[0])
    
    conn.close()
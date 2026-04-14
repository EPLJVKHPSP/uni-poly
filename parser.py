import requests
import psycopg2
import json
from decimal import Decimal
import re
from datetime import datetime, timezone, timedelta
import os
from dotenv import load_dotenv

# Load environment variables (.env should win over pre-set shell vars)
load_dotenv(override=True)


def to_decimal(value):
    """Best-effort normalize numeric-like values to Decimal or None."""
    if value in (None, "", "null"):
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def parse_json_list(maybe_list_or_str):
    """Polymarket sometimes returns JSON-encoded strings instead of lists."""
    if isinstance(maybe_list_or_str, list):
        return maybe_list_or_str
    if isinstance(maybe_list_or_str, str):
        try:
            return json.loads(maybe_list_or_str)
        except json.JSONDecodeError:
            return []
    return []


# Whitelist of tokens we care about (restricted set you specified)
TOKEN_KEYWORDS = {
    # BTC
    "btc": "BTC",
    "bitcoin": "BTC",

    # ETH
    "eth": "ETH",
    "ethereum": "ETH",

    # SOL
    "sol": "SOL",
    "solana": "SOL",

    # BNB
    "bnb": "BNB",
    "binance": "BNB",
    "binance coin": "BNB",

    # PUMP / pump.fun
    "pump": "PUMP",
    "pump.fun": "PUMP",
    "pumpfun": "PUMP",

    # LINK / Chainlink
    "link": "LINK",
    "chainlink": "LINK",


    # ENA / Ethena
    "ena": "ENA",
    "ethena": "ENA",

    # NOT-USED:

    # UNI / Uniswap
    #"uni": "UNI",
    #"uniswap": "UNI",

    # HYPE / Hyperliquid
    #"hype": "HYPE",
    #"hyperliquid": "HYPE",
}


# CoinGecko IDs for the same underlying symbols
COINGECKO_IDS = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "SOL": "solana",
    "BNB": "wbnb",
    "PUMP": "pump-fun",
    "LINK": "chainlink",
    "ENA": "ethena",
}


def fetch_ath_by_symbol():
    """
    Fetch all‑time‑high (ATH) prices in USD for each configured symbol
    from the CoinGecko /coins/markets endpoint.

    Returns a dict like {"BTC": Decimal(...), "ETH": Decimal(...), ...}.
    If the request fails for any reason, returns an empty dict.
    """
    try:
        ids = ",".join(sorted(set(COINGECKO_IDS.values())))
        url = "https://api.coingecko.com/api/v3/coins/markets"
        params = {
            "vs_currency": "usd",
            "ids": ids,
        }
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        # Fail gracefully: no ATH enrichment, but parsing still works
        return {}

    ath_by_symbol = {}
    for entry in data:
        cid = entry.get("id")
        if not cid:
            continue

        # Find which symbol this CoinGecko id corresponds to
        for sym, coingecko_id in COINGECKO_IDS.items():
            if coingecko_id == cid:
                ath_val = entry.get("ath")
                if ath_val is not None:
                    ath_by_symbol[sym] = to_decimal(ath_val)
                break

    return ath_by_symbol


def infer_underlying_symbol(texts):
    """Infer an underlying token symbol from event/market text."""
    haystack = " ".join(t for t in texts if t).lower()
    # simple word-tokenisation: split on non-alphanumeric
    tokens = re.split(r"[^a-z0-9]+", haystack)
    tokens = [t for t in tokens if t]

    # first try exact-token matches for keys that are single words
    for key, symbol in TOKEN_KEYWORDS.items():
        if " " not in key and key in tokens:
            return symbol

    # then fall back to substring match for multi-word keys like "binance coin", "pump.fun"
    for key, symbol in TOKEN_KEYWORDS.items():
        if " " in key or "." in key:
            if key in haystack:
                return symbol

    return None


def infer_level_and_direction(question_text):
    """Extract a price/level and direction (up/down/unknown) from a question string."""
    if not question_text:
        return None, None

    # Find something that looks like a number, optionally with $ and commas
    m = re.search(r"\$?\s*([0-9][0-9,\.]*)", question_text)
    level = None
    if m:
        raw_num = m.group(1).replace(",", "")
        try:
            level = Decimal(raw_num)
        except Exception:
            level = None

    q_lower = question_text.lower()
    up_keywords = [
        "reach",
        "hit",
        "above",
        "over",
        "at least",
        "market cap hit",
        "all time high",
        "ath",
    ]
    down_keywords = [
        "dip",
        "below",
        "under",
        "at most",
        "drop to",
    ]

    if any(k in q_lower for k in up_keywords):
        direction = "up"
    elif any(k in q_lower for k in down_keywords):
        direction = "down"
    else:
        direction = "unknown"

    return level, direction


# Keywords to positively identify "price / level" type events
INCLUDE_PRICE_KEYWORDS = [
    "price",
    "what-price",
    "price-on",
    "market-cap",
    "market-cap-",
    "floor-price",
    "all-time-high",
    "ath",
    "hit-",
    "reach-",
    "dip-to",
    "dip-",
    "above-",
    "below-",
]

# Used to exclude short-term "up or down today" style markets
EXCLUDE_SHORT_TERM_KEYWORDS = [
    "up-or-down",
    "up-or-down-on",
    "up-or-down-in",
    "up-or-down-",
    "above-on-",
    "below-on-",
    "between",
]

# Exclude FDV / fully-diluted-valuation markets as requested
EXCLUDE_FDV_KEYWORDS = [
    "fdv",
    "fully-diluted-valuation",
    "fully-diluted-valuation-",
    "fdv-",
]

# Exclude NFT collection floor price style markets (CryptoPunks, Pudgy Penguins, BAYC, etc.)
EXCLUDE_NFT_FLOOR_KEYWORDS = [
    "cryptopunks",
    "punk",
    "punks",
    "pudgy penguins",
    "pudgy-penguins",
    "pudgy",
    "bored ape",
    "bored-ape",
    "bored ape yacht club",
    "bayc",
    "mutant ape",
    "mutant-ape",
    "mayc",
    "azuki",
    "degods",
    "y00ts",
]

# Exclude dominance-style metrics (e.g. Bitcoin dominance)
EXCLUDE_DOMINANCE_KEYWORDS = [
    "dominance",
    "btc-dominance",
    "bitcoin-dominance",
]

# Exclude gas price, NFT collections (milady), and buyback markets
EXCLUDE_OTHER_KEYWORDS = [
    "gas",
    "gas-price",
    "gwei",
    "milady",
    "buyback",
    "buybacks",
]


def is_price_like_event(event):
    """Decide if an event is a price-related crypto market worth keeping."""
    slug = (event.get("slug") or "").lower()
    title = (event.get("title") or "").lower()
    text = f"{slug} {title}"

    # Drop short-term up/down or above-on/below-on style markets
    if any(bad in text for bad in EXCLUDE_SHORT_TERM_KEYWORDS):
        return False

    # Drop FDV / fully-diluted valuation style markets
    if any(bad in text for bad in EXCLUDE_FDV_KEYWORDS):
        return False

    # Drop NFT collection floor-price style markets (CryptoPunks, Pudgy Penguins, BAYC, etc.)
    if any(bad in text for bad in EXCLUDE_NFT_FLOOR_KEYWORDS):
        return False

    # Drop dominance-style metrics (e.g. Bitcoin dominance)
    if any(bad in text for bad in EXCLUDE_DOMINANCE_KEYWORDS):
        return False

    # Drop gas price, milady NFT, and buyback markets
    if any(bad in text for bad in EXCLUDE_OTHER_KEYWORDS):
        return False

    # Keep only clearly price/level related markets
    return any(good in text for good in INCLUDE_PRICE_KEYWORDS)


# Fetch ALL active events from Polymarket Gamma API (paginated by limit/offset)
url = "https://gamma-api.polymarket.com/events"
all_events = []
limit = 100
offset = 0

while True:
    params = {
        "order": "id",
        "ascending": "false",
        "closed": "false",  # only active/open events
        "limit": limit,
        "offset": offset,
    }

    response = requests.get(url, params=params)
    response.raise_for_status()
    data = response.json()

    # Gamma API may return either a bare list or a dict with an "events" key
    if isinstance(data, list):
        batch = data
    else:
        batch = data.get("events", [])

    if not batch:
        break

    all_events.extend(batch)
    offset += limit


events = all_events

# Fetch ATH levels for configured underlyings from CoinGecko
ath_by_symbol = fetch_ath_by_symbol()

# Connect to PostgreSQL using environment variables or defaults
conn = psycopg2.connect(
    dbname=os.getenv("DB_NAME", "polymarket"),
    user=os.getenv("DB_USER", "polymarket"),
    password=os.getenv("DB_PASSWORD", "polymarket_pw"),
    host=os.getenv("DB_HOST", "localhost"),
    port=int(os.getenv("DB_PORT", "5432")),
)

cur = conn.cursor()

# Create table if it doesn't exist (non-destructive)
cur.execute(
    """
    CREATE TABLE IF NOT EXISTS price_events (
        event_id        BIGINT,
        event_slug      TEXT,
        event_title     TEXT,
        underlying      TEXT,
        market_id       BIGINT,
        market_question TEXT,
        side            TEXT,
        level           NUMERIC,
        direction       TEXT,
        price           NUMERIC,
        event_volume    NUMERIC,
        market_volume   NUMERIC,
        end_date        TIMESTAMPTZ,
        active          BOOLEAN,
        clob_token_id   TEXT,
        PRIMARY KEY (market_id, side)
    )
    """
)

# Create indexes for better query performance
cur.execute(
    """
    CREATE INDEX IF NOT EXISTS idx_price_events_underlying 
    ON price_events(underlying) WHERE active = true
    """
)
cur.execute(
    """
    CREATE INDEX IF NOT EXISTS idx_price_events_level_direction 
    ON price_events(underlying, level, direction, side) WHERE active = true
    """
)
# Ensure a unique constraint exists for ON CONFLICT target
cur.execute(
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_price_events_market_side
    ON price_events(market_id, side)
    """
)

# Add clob_token_id column if it doesn't exist (migration for existing DBs)
cur.execute(
    """
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'price_events' AND column_name = 'clob_token_id'
        ) THEN
            ALTER TABLE price_events ADD COLUMN clob_token_id TEXT;
        END IF;
    END $$
    """
)
conn.commit()

for event in events:
    # Require a valid end_date and skip short-term markets (< 7 days from now)
    end_date_str = event.get("endDate")
    if not end_date_str:
        continue

    try:
        end_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
    except Exception:
        continue

    if end_dt - datetime.now(timezone.utc) < timedelta(days=7):
        # Skip very short-term events (less than ~1 week)
        continue

    # Only keep clearly price-related events (no short-term up/down, no FDV)
    if not is_price_like_event(event):
        continue

    # Event-level volume
    event_volume = to_decimal(event.get("volume"))

    for market in event.get("markets", []):
        market_id = market.get("id")
        question = market.get("question")

        # Market-level volume
        market_volume = to_decimal(market.get("volume"))

        # Outcomes and prices are often JSON-encoded strings
        raw_outcomes = market.get("outcomes")
        outcomes = parse_json_list(raw_outcomes)

        # Only keep binary Yes/No markets; skip bucketed or multi-outcome markets
        if not outcomes:
            continue
        normalized_outcomes = [str(o).strip().lower() for o in outcomes]
        if not (len(normalized_outcomes) == 2 and set(normalized_outcomes) == {"yes", "no"}):
            # Skip markets where sides are not strictly Yes/No (e.g. buckets like '80k', '100k')
            continue

        raw_prices = market.get("outcomePrices")
        prices_raw = parse_json_list(raw_prices)

        # Coerce each price to Decimal (or None)
        norm_prices = [to_decimal(p) for p in prices_raw]

        # Extract CLOB token IDs (maps 1:1 with outcomes: [Yes_id, No_id])
        raw_clob_ids = market.get("clobTokenIds")
        clob_token_ids = parse_json_list(raw_clob_ids)

        # Derive underlying symbol and level/direction from text
        underlying = infer_underlying_symbol([event.get("title"), question])
        if not underlying:
            # Skip non-token-specific markets (e.g. MicroStrategy NAV, macro, etc.)
            continue

        level, direction = infer_level_and_direction(question)

        # If this is an all‑time‑high style market, always override the level with
        # the CoinGecko ATH data (e.g. "Ethereum all time high by December 31?",
        # "SOL ATH before 2026?"). We ignore any numeric values parsed from dates
        # such as 2026, 2027, 31, etc.
        combined_text = f"{(event.get('title') or '')} {question or ''}".lower()
        if (
            "all time high" in combined_text
            or "all-time-high" in combined_text
            or " ath" in combined_text
        ):
            ath_level = ath_by_symbol.get(underlying)
            if ath_level is not None:
                level = ath_level
                # ATH questions are directionally "up" even if the wording was ambiguous
                if direction == "unknown":
                    direction = "up"

        # Insert or update one row per side (e.g. Yes / No)
        for idx, (side, price) in enumerate(zip(outcomes, norm_prices)):
            clob_token_id = clob_token_ids[idx] if idx < len(clob_token_ids) else None
            cur.execute(
                """
                INSERT INTO price_events
                (event_id, event_slug, event_title, underlying,
                 market_id, market_question, side, level, direction,
                 price, event_volume, market_volume, end_date, active,
                 clob_token_id)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (market_id, side) 
                DO UPDATE SET
                    event_id = EXCLUDED.event_id,
                    event_slug = EXCLUDED.event_slug,
                    event_title = EXCLUDED.event_title,
                    underlying = EXCLUDED.underlying,
                    market_question = EXCLUDED.market_question,
                    level = EXCLUDED.level,
                    direction = EXCLUDED.direction,
                    price = EXCLUDED.price,
                    event_volume = EXCLUDED.event_volume,
                    market_volume = EXCLUDED.market_volume,
                    end_date = EXCLUDED.end_date,
                    active = EXCLUDED.active,
                    clob_token_id = EXCLUDED.clob_token_id
                """,
                (
                    event.get("id"),
                    event.get("slug") or "",
                    event.get("title"),
                    underlying,
                    market_id,
                    question,
                    side,
                    level,
                    direction,
                    price,
                    event_volume,
                    market_volume,
                    end_date_str,
                    event.get("active"),
                    clob_token_id,
                ),
            )

conn.commit()
cur.close()
conn.close()
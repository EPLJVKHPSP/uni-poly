import requests
import psycopg2
import json
from decimal import Decimal
import re
from datetime import datetime, timezone, timedelta
import time
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


def classify_resolution_type(question_text: str, description_text: str) -> str:
    """Classify how a Polymarket market resolves.

    Only ``touch_any_time`` markets are structurally suitable as IL insurance:
    they pay $1 the moment the underlying ever touches the level before
    expiry, which matches the geometry of an LP's barrier-hit risk. All other
    resolution rules introduce basis risk (the hedge can pay nothing even
    when the LP gets fully ranged-out, or vice versa).

    Returns one of:
      - "touch_any_time"   (good — pays on any historical touch)
      - "close_on_date"    (resolves to the price on a specific date)
      - "period_end"       (resolves at the end of a period — week/month/year)
      - "average_period"   (resolves to TWAP/average over a period)
      - "unknown"          (insufficient/contradictory evidence)
    """
    q = (question_text or "").lower()
    d = (description_text or "").lower()
    blob = f"{q} {d}"

    # Strongest signal: explicit "any time" / "at any point" / "ever" wording.
    touch_signals = [
        "any time",
        "at any time",
        "anytime",
        "at any point",
        "ever",
        " dip ",
        " dip-",
        "dip to",
        "dips to",
        "will dip",
        "reach",
        "reaches",
        "hit ",
        "hits ",
        "touch",
        "touches",
    ]
    if any(s in blob for s in touch_signals):
        return "touch_any_time"

    # Date-locked: "on <date>", "on the date", resolution sources that point at
    # a single timestamp. Includes Polymarket's hourly close-price boilerplate
    # ("...the Close price for the BTC/USDT 1 hour candle that ends on...").
    date_locked_signals = [
        "on the date of resolution",
        "as of the resolution date",
        "closing price on",
        "price on ",
        "price-on-",
        "above-on-",
        "below-on-",
        "close price for the",
        "1 hour candle that ends",
        "1h candle that ends",
        "candle that ends on the time",
    ]
    if any(s in blob for s in date_locked_signals):
        return "close_on_date"

    # Period end style: end-of-month/year/quarter tickets.
    period_end_signals = [
        "end of the month",
        "end of the year",
        "end of the quarter",
        "end of q",
        "end-of-month",
        "end-of-year",
        "end-of-quarter",
    ]
    if any(s in blob for s in period_end_signals):
        return "period_end"

    # TWAP / average style.
    avg_signals = ["average", "twap", "vwap", "mean over"]
    if any(s in blob for s in avg_signals):
        return "average_period"

    return "unknown"


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


def fetch_events_by_slugs(slugs, batch_size: int = 50, sleep_s: float = 0.05) -> list:
    """Fetch full event objects for a list of known slugs from Gamma.

    Gamma's ``GET /events?slug=A&slug=B&...`` accepts multiple ``slug`` values
    in a single call. **The default ``limit`` is 1**, so we MUST pass an
    explicit ``limit`` ≥ batch size or the API silently truncates the response
    to a single event. Empty hits (slug not deployed) are skipped.
    Returns a flat list of event dicts. Slugs that 404/return [] are ignored.
    """
    url = "https://gamma-api.polymarket.com/events"
    found: list = []
    slugs = list(slugs)
    total = len(slugs)
    if total == 0:
        return found
    for start in range(0, total, batch_size):
        batch = slugs[start : start + batch_size]
        # ``limit`` MUST equal at least the batch size (default is 1).
        params = [("slug", s) for s in batch] + [("limit", str(max(batch_size, 50)))]
        last_err = None
        data = None
        for attempt in range(5):
            try:
                resp = requests.get(url, params=params, timeout=20)
                resp.raise_for_status()
                data = resp.json()
                last_err = None
                break
            except requests.HTTPError as e:
                last_err = e
                status = getattr(e.response, "status_code", None)
                if status is not None and status >= 500:
                    time.sleep(0.4 * (2 ** attempt))
                    continue
                if status == 404:
                    data = []
                    break
                raise
            except Exception as e:
                last_err = e
                time.sleep(0.4 * (2 ** attempt))
        if data is None:
            print(f"[gamma] slug batch {start}/{total} failed: {last_err}")
            continue
        if isinstance(data, list):
            found.extend(data)
        elif isinstance(data, dict):
            found.extend(data.get("events", []) or data.get("data", []) or [])
        if (start // batch_size) % 20 == 0:
            print(f"[gamma] slug fetch progress: {start + len(batch)}/{total} (events kept: {len(found)})")
        time.sleep(sleep_s)
    print(f"[gamma] slug fetch complete: {len(found)} non-empty events from {total} slugs")
    return found


def fetch_events_keyset(*, closed: bool, end_date_min=None, tag_slug=None, max_events=None) -> list:
    """
    Fetch events from Polymarket Gamma API using keyset pagination.

    Note: the keyset endpoint rejects `offset`; use `after_cursor`/`next_cursor`.
    """
    url = "https://gamma-api.polymarket.com/events/keyset"
    events: list = []
    limit = 200
    after_cursor = None

    while True:
        params = {
            "order": "id",
            "ascending": "false",
            "closed": "true" if closed else "false",
            "limit": limit,
        }
        if tag_slug:
            params["tag_slug"] = tag_slug
        if after_cursor:
            params["after_cursor"] = after_cursor
        if end_date_min is not None:
            # Gamma expects RFC3339 / ISO-8601 date-time
            params["end_date_min"] = end_date_min.isoformat().replace("+00:00", "Z")

        data = None
        last_err = None
        for attempt in range(5):
            try:
                resp = requests.get(url, params=params, timeout=20)
                resp.raise_for_status()
                data = resp.json()
                last_err = None
                break
            except requests.HTTPError as e:
                last_err = e
                status = getattr(e.response, "status_code", None)
                if status is not None and status >= 500:
                    time.sleep(1.0 + attempt * 1.5)
                    continue
                raise
            except Exception as e:
                last_err = e
                time.sleep(1.0 + attempt * 1.5)
        if data is None:
            # Gamma sometimes returns intermittent 5xx on large keyset scans.
            # For closed events we prefer partial data over blocking the whole ETL.
            if closed:
                break
            raise last_err if last_err else RuntimeError("Gamma API returned no data")

        if isinstance(data, list):
            batch = data
            next_cursor = None
        else:
            batch = data.get("events", []) or data.get("data", []) or []
            next_cursor = data.get("next_cursor")

        if not batch:
            break

        events.extend(batch)
        if len(events) % 2000 == 0:
            kind = "closed" if closed else "open"
            print(f"[gamma] fetched {len(events)} {kind} events...")
        if max_events is not None and len(events) >= int(max_events):
            events = events[: int(max_events)]
            break
        if not next_cursor:
            break
        after_cursor = next_cursor

    return events


def sync_price_events():
    # Connect to PostgreSQL using environment variables or defaults
    conn = psycopg2.connect(
        dbname=os.getenv("DB_NAME", "polymarket"),
        user=os.getenv("DB_USER", "polymarket"),
        password=os.getenv("DB_PASSWORD", "polymarket_pw"),
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", "5432")),
    )

    cur = conn.cursor()

    # Fetch ATH levels for configured underlyings from CoinGecko
    ath_by_symbol = fetch_ath_by_symbol()

    _ensure_price_events_schema(cur)
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_price_events_market_side ON price_events(market_id, side)")
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

    # Add lifecycle columns if they don't exist (migration for existing DBs)
    cur.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'price_events' AND column_name = 'created_at'
            ) THEN
                ALTER TABLE price_events ADD COLUMN created_at TIMESTAMPTZ;
            END IF;

            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'price_events' AND column_name = 'closed_time'
            ) THEN
                ALTER TABLE price_events ADD COLUMN closed_time TIMESTAMPTZ;
            END IF;
        END $$
        """
    )
    conn.commit()

    # Restrict to a small set of underlyings (default ETH+BTC; the only assets
    # with deep, hourly-trading strike-style markets that overlap a WETH/USDC
    # LP). Override via ``ALLOWED_UNDERLYINGS=BTC,ETH,SOL`` env var.
    raw_allow = (os.getenv("ALLOWED_UNDERLYINGS") or "ETH,BTC").upper()
    allowed_underlyings = {s.strip() for s in raw_allow.split(",") if s.strip()}
    print(f"[etl] underlying allow-list: {sorted(allowed_underlyings)}")

    # Closed-events lookback. The default LP backtest window is ~60 days, so
    # 120 days of closed events is plenty (covers warmup + a small buffer).
    # Override with CLOSED_LOOKBACK_DAYS=180 if you need deeper history.
    closed_lookback_days = int(os.getenv("CLOSED_LOOKBACK_DAYS", "120"))

    print("[gamma] fetching open events...")
    open_events = fetch_events_keyset(closed=False, tag_slug="crypto")
    print(f"[gamma] open events fetched: {len(open_events)}")
    print(f"[gamma] fetching closed events (last {closed_lookback_days}d)...")
    max_closed = os.getenv("MAX_CLOSED_EVENTS")
    closed_events = fetch_events_keyset(
        closed=True,
        end_date_min=datetime.now(timezone.utc) - timedelta(days=closed_lookback_days),
        tag_slug="crypto",
        max_events=int(max_closed) if max_closed not in (None, "", "null") else None,
    )
    print(f"[gamma] closed events fetched: {len(closed_events)}")
    events = open_events + closed_events
    print(f"[etl] total events to process: {len(events)}")

    written = _process_and_upsert_events(
        events,
        cur,
        ath_by_symbol,
        allowed_underlyings=allowed_underlyings,
        apply_event_filters=True,
    )
    conn.commit()
    cur.close()
    conn.close()
    print(f"[etl] wrote {written} rows to price_events")


def sync_strike_slugs(
    start: "date | datetime | str",
    end: "date | datetime | str",
    *,
    assets=("BTC", "ETH"),
    include_daily: bool = True,
    include_hourly: bool = True,
    hours=range(24),
):
    """Targeted ingestion: build the expected strike-market slugs for the
    backtest window and fetch only those events from Gamma.

    Far cheaper than ``sync_price_events`` (no 16k-event keyset crawl). Use
    when you know the backtest window up-front.
    """
    from polymarket_history_pkg.strike_slugs import all_strike_slugs, parse_iso_date

    start_d = parse_iso_date(start)
    end_d = parse_iso_date(end)
    candidates = all_strike_slugs(
        start_d, end_d, assets=assets,
        include_daily=include_daily, include_hourly=include_hourly, hours=hours,
    )
    slugs = [c.slug for c in candidates]
    print(
        f"[targeted] generating {len(slugs)} candidate slugs "
        f"({start_d.isoformat()} .. {end_d.isoformat()}, assets={list(assets)}, "
        f"daily={include_daily}, hourly={include_hourly})"
    )

    events = fetch_events_by_slugs(slugs)

    conn = psycopg2.connect(
        dbname=os.getenv("DB_NAME", "polymarket"),
        user=os.getenv("DB_USER", "polymarket"),
        password=os.getenv("DB_PASSWORD", "polymarket_pw"),
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", "5432")),
    )
    cur = conn.cursor()
    _ensure_price_events_schema(cur)
    conn.commit()

    ath_by_symbol = fetch_ath_by_symbol()
    allowed = {a.upper() for a in assets}

    written = _process_and_upsert_events(
        events,
        cur,
        ath_by_symbol,
        allowed_underlyings=allowed,
        apply_event_filters=False,  # slugs are already strike-only by construction
    )
    conn.commit()
    cur.close()
    conn.close()
    print(f"[targeted] wrote {written} rows to price_events from {len(events)} events")


def _process_and_upsert_events(
    events,
    cur,
    ath_by_symbol,
    *,
    allowed_underlyings,
    apply_event_filters: bool,
) -> int:
    """Walk events → markets → upsert into ``price_events``.

    When ``apply_event_filters`` is True we run the keyset-style heuristics
    (``is_price_like_event``, allowlist underlying inference). The targeted
    slug path skips them because the slug catalogue is already exact.
    """
    written = 0
    for event in events:
        end_date_str = event.get("endDate")
        if not end_date_str:
            continue
        try:
            end_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
        except Exception:
            continue

        is_closed_event = bool(event.get("closed"))
        if apply_event_filters and not is_closed_event:
            if end_dt - datetime.now(timezone.utc) < timedelta(days=7):
                continue
        if apply_event_filters and not is_price_like_event(event):
            continue
        if apply_event_filters:
            event_underlying_guess = infer_underlying_symbol([event.get("title"), event.get("slug")])
            if event_underlying_guess and event_underlying_guess not in allowed_underlyings:
                continue

        event_volume = to_decimal(event.get("volume"))

        for market in event.get("markets", []):
            market_id = market.get("id")
            question = market.get("question")
            market_description = market.get("description") or ""
            resolution_type = classify_resolution_type(question, market_description)
            condition_id = market.get("conditionId")

            market_created_at = market.get("createdAt")
            market_closed_time = market.get("closedTime")

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
            if underlying not in allowed_underlyings:
                # Final safety net: even if event title was generic, drop markets
                # whose inferred underlying is outside our allow-list.
                continue

            level, direction = infer_level_and_direction(question)
            # Strike-only filter: drop markets without a numeric price level (these
            # are "Will X happen?" markets that can't function as an IL hedge).
            # We allow ATH-style markets to pass since their level is filled in
            # below from CoinGecko.
            combined_text_check = f"{(event.get('title') or '')} {question or ''}".lower()
            is_ath_like = any(kw in combined_text_check for kw in ("all time high", "all-time high", " ath ", "ath?", "ath."))
            if level is None and not is_ath_like:
                continue

            # If this is an all‑time‑high style market, always override the level with CoinGecko ATH
            combined_text = f"{(event.get('title') or '')} {question or ''}".lower()
            if (
                "all time high" in combined_text
                or "all-time-high" in combined_text
                or " ath" in combined_text
            ):
                ath_level = ath_by_symbol.get(underlying)
                if ath_level is not None:
                    level = ath_level
                    if direction == "unknown":
                        direction = "up"

            # Insert or update one row per side (e.g. Yes / No)
            for idx, (side, price) in enumerate(zip(outcomes, norm_prices)):
                clob_token_id = clob_token_ids[idx] if idx < len(clob_token_ids) else None
                cur.execute(
                    """
                    INSERT INTO price_events
                    (event_id, event_slug, event_title, underlying,
                     market_id, market_question, market_description, resolution_type,
                     condition_id, side, level, direction,
                     price, event_volume, market_volume, end_date,
                     created_at, closed_time,
                     active, clob_token_id)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (market_id, side)
                    DO UPDATE SET
                        event_id = EXCLUDED.event_id,
                        event_slug = EXCLUDED.event_slug,
                        event_title = EXCLUDED.event_title,
                        underlying = EXCLUDED.underlying,
                        market_question = EXCLUDED.market_question,
                        market_description = EXCLUDED.market_description,
                        resolution_type = EXCLUDED.resolution_type,
                        condition_id = EXCLUDED.condition_id,
                        level = EXCLUDED.level,
                        direction = EXCLUDED.direction,
                        price = EXCLUDED.price,
                        event_volume = EXCLUDED.event_volume,
                        market_volume = EXCLUDED.market_volume,
                        end_date = EXCLUDED.end_date,
                        created_at = EXCLUDED.created_at,
                        closed_time = EXCLUDED.closed_time,
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
                        market_description,
                        resolution_type,
                        condition_id,
                        side,
                        level,
                        direction,
                        price,
                        event_volume,
                        market_volume,
                        end_date_str,
                        market_created_at,
                        market_closed_time,
                        event.get("active"),
                        clob_token_id,
                    ),
                )
                written += 1
                if written % 2000 == 0:
                    print(f"[db] upserted {written} rows...")

    return written


def _ensure_price_events_schema(cur) -> None:
    """Idempotent schema bootstrap for ``price_events`` (also used by targeted mode)."""
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS price_events (
            event_id        BIGINT,
            event_slug      TEXT,
            event_title     TEXT,
            underlying      TEXT,
            market_id       BIGINT,
            market_question TEXT,
            market_description TEXT,
            resolution_type TEXT,
            side            TEXT,
            level           NUMERIC,
            direction       TEXT,
            price           NUMERIC,
            event_volume    NUMERIC,
            market_volume   NUMERIC,
            end_date        TIMESTAMPTZ,
            created_at      TIMESTAMPTZ,
            closed_time     TIMESTAMPTZ,
            active          BOOLEAN,
            clob_token_id   TEXT,
            condition_id    TEXT,
            PRIMARY KEY (market_id, side)
        )
        """
    )
    for col, ddl in (
        ("market_description", "ALTER TABLE price_events ADD COLUMN IF NOT EXISTS market_description TEXT"),
        ("resolution_type",    "ALTER TABLE price_events ADD COLUMN IF NOT EXISTS resolution_type TEXT"),
        ("condition_id",       "ALTER TABLE price_events ADD COLUMN IF NOT EXISTS condition_id TEXT"),
    ):
        cur.execute(ddl)
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_price_events_underlying ON price_events(underlying) WHERE active = true"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_price_events_lookup ON price_events(underlying, level, direction, side) WHERE active = true"
    )


if __name__ == "__main__":
    import argparse
    from datetime import date

    ap = argparse.ArgumentParser(description="Polymarket strike-market ETL")
    ap.add_argument(
        "--mode",
        choices=("keyset", "targeted"),
        default=os.getenv("PARSER_MODE", "targeted"),
        help="keyset: paginate /events?tag_slug=crypto. targeted: build expected ETH/BTC strike slugs and fetch only those.",
    )
    ap.add_argument("--start", default=None, help="targeted-mode start date (YYYY-MM-DD). Default: today - 60d")
    ap.add_argument("--end",   default=None, help="targeted-mode end date (YYYY-MM-DD). Default: today + 7d")
    ap.add_argument("--no-daily",  action="store_true", help="targeted: skip daily 'what-price-will-X-hit' events")
    ap.add_argument("--no-hourly", action="store_true", help="targeted: skip hourly 'X-above-on-DATE-HOUR' events")
    ap.add_argument("--assets", default="BTC,ETH", help="comma-separated underlyings (BTC,ETH supported)")
    args = ap.parse_args()

    if args.mode == "keyset":
        sync_price_events()
    else:
        today = datetime.now(timezone.utc).date()
        start_d = date.fromisoformat(args.start) if args.start else today - timedelta(days=60)
        end_d = date.fromisoformat(args.end) if args.end else today + timedelta(days=7)
        sync_strike_slugs(
            start_d, end_d,
            assets=tuple(s.strip() for s in args.assets.split(",") if s.strip()),
            include_daily=not args.no_daily,
            include_hourly=not args.no_hourly,
        )
-- Polymarket telemetry gap root-cause queries
--
-- Goal: classify missing lower_bid/upper_bid into one of:
--   (A) no mapping exists (no clob_token_id)
--   (B) mapping exists but no bet_price_history rows
--   (C) history exists but only after the target timestamp (no past rows <= ts)
--
-- NOTE: Replace variables as needed:
--   :underlying   e.g. 'ETH'
--   :level        e.g. 2000
--   :direction    'down' or 'up'
--   :side         'Yes'
--   :target_ts    timestamptz (NOT unix int), e.g. '2026-01-01T00:00:00Z'

-- 1) Mapping exists at time t?
SELECT
  clob_token_id,
  market_id,
  created_at,
  end_date
FROM price_events
WHERE underlying = :underlying
  AND level = :level
  AND direction = :direction
  AND side = :side
  AND clob_token_id IS NOT NULL
  AND (created_at IS NULL OR created_at <= :target_ts)
  AND (end_date IS NULL OR end_date >= :target_ts)
ORDER BY end_date ASC NULLS LAST;

-- 2) Any history rows for a clob_token_id?
SELECT
  COUNT(*) AS rows_total,
  MIN(ts) AS first_ts,
  MAX(ts) AS last_ts
FROM bet_price_history
WHERE clob_token_id = :clob_token_id;

-- 3) Past row exists at or before t?
SELECT
  ts,
  price
FROM bet_price_history
WHERE clob_token_id = :clob_token_id
  AND ts <= :target_ts
ORDER BY ts DESC
LIMIT 1;

-- 4) If (3) is empty, what is the first row after t? (diagnostic only; do NOT use in backtest logic)
SELECT
  ts,
  price
FROM bet_price_history
WHERE clob_token_id = :clob_token_id
  AND ts > :target_ts
ORDER BY ts ASC
LIMIT 1;


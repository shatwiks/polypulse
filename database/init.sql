-- ==============================================================================
-- PolyPulse ClickHouse Schema & Materialized Analytics Infrastructure
-- ==============================================================================
-- Engine Family: ReplacingMergeTree (for at-least-once deduplication)
-- Pre-aggregation: AggregatingMergeTree (for sub-10ms analytical queries)
-- Timestamps: UTC DateTime64(3, 'UTC')
-- ==============================================================================

CREATE DATABASE IF NOT EXISTS polypulse;

-- ------------------------------------------------------------------------------
-- 1. Raw Market Events
-- ------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS polypulse.market_events (
  event_id UUID,
  venue LowCardinality(String),
  market_id String,
  event_type LowCardinality(String),
  event_time DateTime64(3, 'UTC') CODEC(DoubleDelta, ZSTD(1)),
  received_at DateTime64(3, 'UTC') CODEC(DoubleDelta, ZSTD(1)),
  sequence UInt64 CODEC(T64, ZSTD(1)),
  payload String CODEC(ZSTD(3)),
  exchange_timestamp Nullable(DateTime64(3, 'UTC')) CODEC(DoubleDelta, ZSTD(1)),
  received_timestamp DateTime64(3, 'UTC') CODEC(DoubleDelta, ZSTD(1)),
  sequence_number Nullable(UInt64) CODEC(T64, ZSTD(1)),
  price Nullable(Decimal64(6)) CODEC(T64, ZSTD(1)),
  quantity Nullable(Decimal64(6)) CODEC(T64, ZSTD(1)),
  side Nullable(String),
  source_id Nullable(String),
  raw_payload String CODEC(ZSTD(3))
) ENGINE = ReplacingMergeTree(received_at)
PARTITION BY toYYYYMM(event_time)
ORDER BY (venue, market_id, event_id)
TTL event_time + INTERVAL 14 DAY
SETTINGS non_replicated_deduplication_window = 1000;

-- Non-destructive migrations for legacy instances
ALTER TABLE polypulse.market_events ADD COLUMN IF NOT EXISTS exchange_timestamp Nullable(DateTime64(3, 'UTC'));
ALTER TABLE polypulse.market_events ADD COLUMN IF NOT EXISTS received_timestamp DateTime64(3, 'UTC');
ALTER TABLE polypulse.market_events ADD COLUMN IF NOT EXISTS sequence_number Nullable(UInt64);
ALTER TABLE polypulse.market_events ADD COLUMN IF NOT EXISTS price Nullable(Decimal64(6));
ALTER TABLE polypulse.market_events ADD COLUMN IF NOT EXISTS quantity Nullable(Decimal64(6));
ALTER TABLE polypulse.market_events ADD COLUMN IF NOT EXISTS side Nullable(String);
ALTER TABLE polypulse.market_events ADD COLUMN IF NOT EXISTS source_id Nullable(String);
ALTER TABLE polypulse.market_events ADD COLUMN IF NOT EXISTS raw_payload String;

-- ------------------------------------------------------------------------------
-- 2. Raw Public Trades & Executed Fills
-- ------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS polypulse.trades (
  event_id UUID,
  venue LowCardinality(String),
  market_id String,
  source_id Nullable(String),
  side LowCardinality(String),
  wallet Nullable(String) CODEC(ZSTD(1)),
  price Decimal64(6) CODEC(T64, ZSTD(1)),
  quantity Nullable(Decimal64(6)) CODEC(T64, ZSTD(1)),
  exchange_timestamp Nullable(DateTime64(3, 'UTC')) CODEC(DoubleDelta, ZSTD(1)),
  received_timestamp DateTime64(3, 'UTC') CODEC(DoubleDelta, ZSTD(1))
) ENGINE = ReplacingMergeTree(received_timestamp)
PARTITION BY toYYYYMM(received_timestamp)
ORDER BY (venue, market_id, event_id)
TTL received_timestamp + INTERVAL 90 DAY
SETTINGS non_replicated_deduplication_window = 1000;

ALTER TABLE polypulse.trades MODIFY COLUMN quantity Nullable(Decimal64(6));

-- ------------------------------------------------------------------------------
-- 3. Orderbook Top-of-Book Snapshots
-- ------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS polypulse.orderbook_snapshots (
  observation_id UInt64,
  venue LowCardinality(String),
  market_id String,
  snapshot_time DateTime64(3, 'UTC') CODEC(DoubleDelta, ZSTD(1)),
  best_bid Nullable(Decimal64(6)) CODEC(T64, ZSTD(1)),
  best_ask Nullable(Decimal64(6)) CODEC(T64, ZSTD(1)),
  bid_depth Decimal64(6) CODEC(T64, ZSTD(1)),
  ask_depth Decimal64(6) CODEC(T64, ZSTD(1)),
  sequence UInt64 CODEC(T64, ZSTD(1)),
  valid UInt8
) ENGINE = ReplacingMergeTree(snapshot_time)
PARTITION BY toYYYYMM(snapshot_time)
ORDER BY (venue, market_id, observation_id)
TTL snapshot_time + INTERVAL 14 DAY
SETTINGS non_replicated_deduplication_window = 1000;

-- Non-destructive Phase 1 -> Phase 2/4 migrations
ALTER TABLE polypulse.orderbook_snapshots ADD COLUMN IF NOT EXISTS observation_id UInt64;
ALTER TABLE polypulse.orderbook_snapshots ADD COLUMN IF NOT EXISTS valid UInt8 DEFAULT 1;
ALTER TABLE polypulse.orderbook_snapshots MODIFY COLUMN best_bid Nullable(Decimal64(6));
ALTER TABLE polypulse.orderbook_snapshots MODIFY COLUMN best_ask Nullable(Decimal64(6));
ALTER TABLE polypulse.orderbook_snapshots MODIFY COLUMN bid_depth Decimal64(6);
ALTER TABLE polypulse.orderbook_snapshots MODIFY COLUMN ask_depth Decimal64(6);

-- ------------------------------------------------------------------------------
-- 4. Feed Health & Telemetry
-- ------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS polypulse.feed_health (
  id UInt64,
  venue LowCardinality(String),
  status LowCardinality(String),
  observed_at DateTime64(3, 'UTC') CODEC(DoubleDelta, ZSTD(1)),
  detail Nullable(String) CODEC(ZSTD(1))
) ENGINE = ReplacingMergeTree(observed_at)
PARTITION BY toYYYYMM(observed_at)
ORDER BY (venue, id)
TTL observed_at + INTERVAL 7 DAY
SETTINGS non_replicated_deduplication_window = 1000;

-- ------------------------------------------------------------------------------
-- 5. Ingestion Error Log
-- ------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS polypulse.ingestion_errors (
  id UInt64,
  venue LowCardinality(String),
  market_id Nullable(String),
  event_id Nullable(UUID),
  error String CODEC(ZSTD(2)),
  observed_at DateTime64(3, 'UTC') CODEC(DoubleDelta, ZSTD(1))
) ENGINE = ReplacingMergeTree(observed_at)
PARTITION BY toYYYYMM(observed_at)
ORDER BY (venue, id)
TTL observed_at + INTERVAL 7 DAY
SETTINGS non_replicated_deduplication_window = 1000;

-- ------------------------------------------------------------------------------
-- 6. Materialized View Target: 1-Minute OHLCV & Trade Candles
-- ------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS polypulse.trade_candles_1m (
  venue LowCardinality(String),
  market_id String,
  window_start DateTime64(3, 'UTC'),
  trade_count AggregateFunction(count),
  total_volume AggregateFunction(sum, Decimal(18, 6)),
  buy_volume AggregateFunction(sum, Decimal(18, 6)),
  sell_volume AggregateFunction(sum, Decimal(18, 6)),
  open_price AggregateFunction(argMin, Decimal(18, 6), DateTime64(3, 'UTC')),
  high_price AggregateFunction(max, Decimal(18, 6)),
  low_price AggregateFunction(min, Decimal(18, 6)),
  close_price AggregateFunction(argMax, Decimal(18, 6), DateTime64(3, 'UTC')),
  avg_price AggregateFunction(avg, Decimal(18, 6))
) ENGINE = AggregatingMergeTree()
PARTITION BY toYYYYMM(window_start)
ORDER BY (venue, market_id, window_start)
TTL window_start + INTERVAL 180 DAY
SETTINGS non_replicated_deduplication_window = 1000;

CREATE MATERIALIZED VIEW IF NOT EXISTS polypulse.mv_trade_candles_1m
TO polypulse.trade_candles_1m AS
SELECT
  venue,
  market_id,
  toStartOfMinute(received_timestamp) AS window_start,
  countState() AS trade_count,
  sumState(ifNull(quantity, toDecimal64(0, 6))) AS total_volume,
  sumState(if(side = 'buy', ifNull(quantity, toDecimal64(0, 6)), toDecimal64(0, 6))) AS buy_volume,
  sumState(if(side = 'sell', ifNull(quantity, toDecimal64(0, 6)), toDecimal64(0, 6))) AS sell_volume,
  argMinState(price, received_timestamp) AS open_price,
  maxState(price) AS high_price,
  minState(price) AS low_price,
  argMaxState(price, received_timestamp) AS close_price,
  avgState(price) AS avg_price
FROM polypulse.trades
GROUP BY venue, market_id, window_start;

-- ------------------------------------------------------------------------------
-- 7. Materialized View Target: 1-Minute Orderbook & Spread Summary
-- ------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS polypulse.book_summary_1m (
  venue LowCardinality(String),
  market_id String,
  window_start DateTime64(3, 'UTC'),
  snapshot_count AggregateFunction(count),
  avg_best_bid AggregateFunction(avg, Decimal(18, 6)),
  avg_best_ask AggregateFunction(avg, Decimal(18, 6)),
  min_spread AggregateFunction(min, Decimal(18, 6)),
  max_spread AggregateFunction(max, Decimal(18, 6)),
  avg_spread AggregateFunction(avg, Decimal(18, 6)),
  avg_bid_depth AggregateFunction(avg, Decimal(18, 6)),
  avg_ask_depth AggregateFunction(avg, Decimal(18, 6)),
  latest_best_bid AggregateFunction(argMax, Decimal(18, 6), DateTime64(3, 'UTC')),
  latest_best_ask AggregateFunction(argMax, Decimal(18, 6), DateTime64(3, 'UTC'))
) ENGINE = AggregatingMergeTree()
PARTITION BY toYYYYMM(window_start)
ORDER BY (venue, market_id, window_start)
TTL window_start + INTERVAL 180 DAY
SETTINGS non_replicated_deduplication_window = 1000;

CREATE MATERIALIZED VIEW IF NOT EXISTS polypulse.mv_book_summary_1m
TO polypulse.book_summary_1m AS
SELECT
  venue,
  market_id,
  toStartOfMinute(snapshot_time) AS window_start,
  countState() AS snapshot_count,
  avgState(assumeNotNull(best_bid)) AS avg_best_bid,
  avgState(assumeNotNull(best_ask)) AS avg_best_ask,
  minState(assumeNotNull(best_ask) - assumeNotNull(best_bid)) AS min_spread,
  maxState(assumeNotNull(best_ask) - assumeNotNull(best_bid)) AS max_spread,
  avgState(assumeNotNull(best_ask) - assumeNotNull(best_bid)) AS avg_spread,
  avgState(bid_depth) AS avg_bid_depth,
  avgState(ask_depth) AS avg_ask_depth,
  argMaxState(assumeNotNull(best_bid), snapshot_time) AS latest_best_bid,
  argMaxState(assumeNotNull(best_ask), snapshot_time) AS latest_best_ask
FROM polypulse.orderbook_snapshots
WHERE valid = 1 AND best_bid IS NOT NULL AND best_ask IS NOT NULL AND best_ask > best_bid
GROUP BY venue, market_id, window_start;

-- ------------------------------------------------------------------------------
-- 8. Materialized View Target: 10-Second Venue Ingestion Rates
-- ------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS polypulse.venue_event_stats_10s (
  venue LowCardinality(String),
  event_type LowCardinality(String),
  window_start DateTime64(3, 'UTC'),
  event_count UInt64
) ENGINE = SummingMergeTree((event_count))
PARTITION BY toYYYYMM(window_start)
ORDER BY (venue, event_type, window_start)
TTL window_start + INTERVAL 30 DAY
SETTINGS non_replicated_deduplication_window = 1000;

CREATE MATERIALIZED VIEW IF NOT EXISTS polypulse.mv_venue_event_stats_10s
TO polypulse.venue_event_stats_10s AS
SELECT
  venue,
  event_type,
  toStartOfInterval(event_time, INTERVAL 10 SECOND) AS window_start,
  count() AS event_count
FROM polypulse.market_events
GROUP BY venue, event_type, window_start;

-- ------------------------------------------------------------------------------
-- 9. Wallet-Attributed Trades (Polygon V2 fills and verified demo trades)
-- ------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS polypulse.wallet_trades (
  trade_id String,
  event_id UUID,
  wallet_address LowCardinality(String),
  venue LowCardinality(String),
  market_id String,
  attribution_type LowCardinality(String),
  side LowCardinality(String),
  price Decimal64(6) CODEC(T64, ZSTD(1)),
  quantity Decimal64(6) CODEC(T64, ZSTD(1)),
  notional Decimal64(6) CODEC(T64, ZSTD(1)),
  exchange_timestamp Nullable(DateTime64(3, 'UTC')) CODEC(DoubleDelta, ZSTD(1)),
  received_timestamp DateTime64(3, 'UTC') CODEC(DoubleDelta, ZSTD(1)),
  chain_id UInt32 CODEC(T64, ZSTD(1)),
  exchange_address LowCardinality(String),
  transaction_hash Nullable(String),
  block_number Nullable(UInt64),
  log_index Nullable(UInt32),
  condition_id Nullable(String),
  token_id String,
  outcome_label Nullable(String),
  timestamp_source LowCardinality(String),
  match_type LowCardinality(String),
  source LowCardinality(String)
) ENGINE = ReplacingMergeTree(received_timestamp)
PARTITION BY toYYYYMM(received_timestamp)
ORDER BY (wallet_address, venue, market_id, trade_id)
TTL received_timestamp + INTERVAL 180 DAY
SETTINGS non_replicated_deduplication_window = 1000;

-- ------------------------------------------------------------------------------
-- 10. Multi-Horizon Trade Markouts (Adverse selection & post-trade drift)
-- ------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS polypulse.trade_markouts (
  trade_id String,
  wallet_address LowCardinality(String),
  venue LowCardinality(String),
  market_id String,
  side LowCardinality(String),
  horizon_seconds UInt16,
  calc_version LowCardinality(String),
  status LowCardinality(String),
  exec_price Decimal64(6) CODEC(T64, ZSTD(1)),
  future_mid Nullable(Decimal64(6)) CODEC(T64, ZSTD(1)),
  markout Nullable(Decimal64(6)) CODEC(T64, ZSTD(1)),
  markout_bps Nullable(Decimal64(4)) CODEC(T64, ZSTD(1)),
  observation_time Nullable(DateTime64(3, 'UTC')) CODEC(DoubleDelta, ZSTD(1)),
  trade_timestamp DateTime64(3, 'UTC') CODEC(DoubleDelta, ZSTD(1)),
  calculated_at DateTime64(3, 'UTC') CODEC(DoubleDelta, ZSTD(1)),
  token_id Nullable(String),
  condition_id Nullable(String)
) ENGINE = ReplacingMergeTree(calculated_at)
PARTITION BY toYYYYMM(trade_timestamp)
ORDER BY (wallet_address, horizon_seconds, calc_version, trade_id)
TTL trade_timestamp + INTERVAL 180 DAY
SETTINGS non_replicated_deduplication_window = 1000;


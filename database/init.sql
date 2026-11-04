CREATE DATABASE IF NOT EXISTS polypulse;
CREATE TABLE IF NOT EXISTS polypulse.market_events (
  event_id UUID, venue LowCardinality(String), market_id String,
  event_type LowCardinality(String), event_time DateTime64(3, 'UTC'),
  received_at DateTime64(3, 'UTC'), sequence UInt64, payload String
) ENGINE = ReplacingMergeTree(received_at)
PARTITION BY toYYYYMM(event_time)
ORDER BY (venue, market_id, event_id);

CREATE TABLE IF NOT EXISTS polypulse.orderbook_snapshots (
  venue LowCardinality(String), market_id String,
  snapshot_time DateTime64(3, 'UTC'), best_bid Nullable(Decimal64(4)),
  best_ask Nullable(Decimal64(4)), bid_depth Decimal64(4), ask_depth Decimal64(4),
  sequence UInt64
) ENGINE = MergeTree
PARTITION BY toYYYYMM(snapshot_time)
ORDER BY (venue, market_id, snapshot_time);

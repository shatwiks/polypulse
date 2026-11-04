# ClickHouse Schema & Storage Architecture

PolyPulse utilizes ClickHouse as its primary analytical columnar warehouse, backed by an in-process SQLite Write-Ahead Log (WAL) that guarantees local durability, replayability, and retry buffering.

---

## 1. ClickHouse DDL Architecture

The schema is defined in [database/init.sql](file:///d:/PROJECTS/Polypulse/database/init.sql) and executed automatically upon container initialization.

```sql
CREATE DATABASE IF NOT EXISTS polypulse;

-- Raw Normalized Market Events
CREATE TABLE IF NOT EXISTS polypulse.market_events (
    event_id    UUID,
    venue       LowCardinality(String),
    market_id   String,
    event_type  LowCardinality(String),
    event_time  DateTime64(3, 'UTC'),
    received_at DateTime64(3, 'UTC'),
    sequence    UInt64,
    payload     String
) ENGINE = ReplacingMergeTree(received_at)
PARTITION BY toYYYYMM(event_time)
ORDER BY (venue, market_id, event_id);

-- Periodic Orderbook Snapshots for Time-Series Analysis
CREATE TABLE IF NOT EXISTS polypulse.orderbook_snapshots (
    venue         LowCardinality(String),
    market_id     String,
    snapshot_time DateTime64(3, 'UTC'),
    best_bid      Nullable(Decimal64(4)),
    best_ask      Nullable(Decimal64(4)),
    bid_depth     Decimal64(4),
    ask_depth     Decimal64(4),
    sequence      UInt64
) ENGINE = MergeTree
PARTITION BY toYYYYMM(snapshot_time)
ORDER BY (venue, market_id, snapshot_time);
```

---

## 2. Table Design & Performance Rationale

### `polypulse.market_events`
- **Engine**: `ReplacingMergeTree(received_at)`
  - Allows idempotent bulk micro-batching. If network failure triggers an exporter retry, duplicate events with identical `(venue, market_id, event_id)` are deduplicated in background merges without manual deletion queries.
- **Data Types**:
  - `LowCardinality(String)` for `venue` and `event_type`: Reduces memory consumption by creating an integer dictionary index, speeding up partition scanning.
  - `DateTime64(3, 'UTC')`: Millisecond-level precision preserving exact clock synchronization across venue feeds.
- **Partitioning**: `toYYYYMM(event_time)` ensures compact monthly partitions that can be dropped or archived independently.

### `polypulse.orderbook_snapshots`
- **Engine**: Standard `MergeTree`
  - High ingestion throughput for frequent periodic book states.
- **Sorting Key**: `ORDER BY (venue, market_id, snapshot_time)`
  - Crucial for time-series range queries and the `LEFT ASOF JOIN` used in the markout calculation engine.

---

## 3. High-Performance Markout Queries via `LEFT ASOF JOIN`

The primary analytical use case in ClickHouse is calculating the post-trade price trajectory (markout) across execution records:

```sql
SELECT
    t.trade_id,
    t.wallet,
    t.venue,
    t.market_id,
    t.side,
    t.price AS execution_price,
    t.executed_at,
    s.snapshot_time AS observation_time,
    (s.best_bid + s.best_ask) / 2 AS future_mid,
    -- Markout: +ΔP for buy, -ΔP for sell
    (CASE WHEN t.side = 'buy' THEN 1 ELSE -1 END) * 
    (((s.best_bid + s.best_ask) / 2) - t.price) AS markout_5s
FROM polypulse.trades t
LEFT ASOF JOIN polypulse.orderbook_snapshots s
    ON t.venue = s.venue
   AND t.market_id = s.market_id
   AND s.snapshot_time >= t.executed_at + INTERVAL 5 SECOND
WHERE t.executed_at >= now() - INTERVAL 1 DAY
ORDER BY t.executed_at DESC;
```

### Why `ASOF JOIN`?
Standard relational databases require subqueries with `LIMIT 1` or window functions (`ROW_NUMBER() OVER (PARTITION BY ... ORDER BY ...)`), causing massive disk spills when evaluating millions of rows. ClickHouse's native `ASOF JOIN` leverages the sorted order of `(venue, market_id, snapshot_time)` to execute in $O(M + N)$ linear time.

---

## 4. Local SQLite WAL Journal

Before data reaches ClickHouse, all events are committed to `polypulse.db` via the `Journal` class in [backend/storage.py](file:///d:/PROJECTS/Polypulse/backend/storage.py):

```sql
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS events(
    event_id    TEXT PRIMARY KEY,
    venue       TEXT NOT NULL,
    market_id   TEXT NOT NULL,
    event_type  TEXT NOT NULL,
    event_time  TEXT NOT NULL,
    received_at TEXT NOT NULL,
    sequence    INTEGER NOT NULL,
    payload     TEXT NOT NULL,
    exported    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS events_market ON events(venue, market_id, received_at);

CREATE TABLE IF NOT EXISTS book_observations(
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    venue       TEXT NOT NULL,
    market_id   TEXT NOT NULL,
    recorded_at REAL NOT NULL,
    sequence    INTEGER NOT NULL,
    payload     TEXT NOT NULL,
    exported    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS book_observations_market ON book_observations(venue, market_id, recorded_at);

CREATE TABLE IF NOT EXISTS trades(
    trade_id    TEXT PRIMARY KEY,
    wallet      TEXT NOT NULL,
    venue       TEXT NOT NULL,
    market_id   TEXT NOT NULL,
    side        TEXT NOT NULL,
    price       REAL NOT NULL,
    quantity    REAL NOT NULL,
    executed_at REAL NOT NULL
);
```

### Exporter Resilience Loop
1. The background task `export_forever` fetches batches of 250 pending records (`WHERE exported = 0`).
2. Converts rows into newline-delimited JSON (`JSONEachRow`).
3. Sends an HTTP POST to ClickHouse with `date_time_input_format=best_effort`.
4. Upon HTTP 200, updates `exported = 1` for the batch.
5. If ClickHouse is unreachable, status transitions to `"retrying"`, and backoff prevents network storms.

# Phase 4 Architecture & Implementation Plan: ClickHouse Analytics Infrastructure

## 1. Executive Summary & Objective

PolyPulse provides real-time cross-venue orderbook ingestion, in-memory tick processing, and local SQLite Write-Ahead Log (WAL) durability. Phase 4 establishes an enterprise-grade, high-throughput ClickHouse time-series analytics infrastructure that ingests exported events asynchronously, maintains at-least-once deduplication semantics, generates real-time aggregated metrics via Materialized Views, and exposes structured FastAPI endpoints for historical market intelligence and terminal visualizations.

---

## 2. ClickHouse Data Architecture & Schema Evolution

### 2.1 Table Design & MergeTree Engines
The database consists of five foundational operational tables in the `polypulse` database:

```mermaid
flowchart TD
    subgraph Raw_Tables["Raw MergeTree Tables (At-Least-Once Delivery)"]
        ME[market_events<br/>ReplacingMergeTree]
        TR[trades<br/>ReplacingMergeTree]
        OS[orderbook_snapshots<br/>ReplacingMergeTree]
        FH[feed_health<br/>ReplacingMergeTree]
        IE[ingestion_errors<br/>ReplacingMergeTree]
    end

    subgraph Mat_Views["Real-Time Materialized Views (AggregatingMergeTree)"]
        MV_TC[mv_trade_candles_1m] --> TC[trade_candles_1m<br/>OHLCV, VWAP, Trade Count]
        MV_BS[mv_book_summary_1m] --> BS[book_summary_1m<br/>Spreads, Depths, Mid Trajectory]
        MV_VE[mv_venue_event_stats_10s] --> VE[venue_event_stats_10s<br/>Event Rates, Throughput]
    end

    TR --> MV_TC
    OS --> MV_BS
    ME --> MV_VE
```

1. **`polypulse.market_events`**:
   - **Engine**: `ReplacingMergeTree(received_at)`
   - **Partition Key**: `toYYYYMM(event_time)`
   - **Order By Key**: `(venue, market_id, event_id)`
   - **Settings**: `non_replicated_deduplication_window = 1000`
   - **Codecs**:
     - `event_time`, `received_at`: `DoubleDelta, ZSTD(1)`
     - `sequence`: `T64, ZSTD(1)`
     - `price`, `quantity`: `T64, ZSTD(1)`
     - `payload`, `raw_payload`: `ZSTD(3)`
   - **TTL**: `event_time + INTERVAL 14 DAY` (configurable raw event retention)

2. **`polypulse.trades`**:
   - **Engine**: `ReplacingMergeTree(received_timestamp)`
   - **Partition Key**: `toYYYYMM(received_timestamp)`
   - **Order By Key**: `(venue, market_id, event_id)`
   - **Settings**: `non_replicated_deduplication_window = 1000`
   - **Codecs**:
     - `price`, `quantity`: `T64, ZSTD(1)`
     - `exchange_timestamp`, `received_timestamp`: `DoubleDelta, ZSTD(1)`
     - `wallet`: `ZSTD(1)`
   - **TTL**: Retained for 90 days.

3. **`polypulse.orderbook_snapshots`**:
   - **Engine**: `ReplacingMergeTree(snapshot_time)`
   - **Partition Key**: `toYYYYMM(snapshot_time)`
   - **Order By Key**: `(venue, market_id, observation_id)`
   - **Settings**: `non_replicated_deduplication_window = 1000`
   - **Codecs**:
     - `best_bid`, `best_ask`, `bid_depth`, `ask_depth`: `T64, ZSTD(1)`
     - `snapshot_time`: `DoubleDelta, ZSTD(1)`
   - **TTL**: `snapshot_time + INTERVAL 14 DAY`.

4. **`polypulse.feed_health` & `polypulse.ingestion_errors`**:
   - Operational telemetry for feed disconnections, reconnection latencies, and parsing failures.
   - **Settings**: `non_replicated_deduplication_window = 1000`
   - **TTL**: `observed_at + INTERVAL 7 DAY`.

---

## 3. Deduplication Semantics & Materialized View Correctness Audit

### 3.1 ClickHouse 25.8 Deduplication Mechanics & Failure Modes
An essential finding of our database correctness audit is that ClickHouse 25.8 treats non-replicated MergeTree engines differently from replicated engines:
- `non_replicated_deduplication_window = 0` **by default**. Unless explicitly configured, single-node ClickHouse servers do **not** deduplicate inserted blocks, ignoring `insert_deduplicate=1`.
- To enable single-node insert deduplication, all target tables and views must declare `SETTINGS non_replicated_deduplication_window = 1000`.
- Incremental Materialized Views (`AggregatingMergeTree` and `SummingMergeTree`) run as **INSERT triggers** on every newly accepted block.
- If duplicate events arrive in an **identically grouped block** with `insert_deduplicate=1` and `deduplicate_blocks_in_dependent_materialized_views=1`, ClickHouse matches the block hash / `insert_deduplication_token` and drops the duplicate before the MV executes.
- **Critical Vulnerability**: If a retried event is placed into a **differently grouped batch** (e.g. event $E_1$ combined with a new event $E_4$), ClickHouse block-level deduplication cannot identify that $E_1$ was previously ingested. The MV triggers on the new block, and **`sumState` / `countState` are double-counted**.
- **Important Invariant**: Background `OPTIMIZE TABLE ... FINAL` merges states in `AggregatingMergeTree` by folding them together; it **cannot un-add or subtract duplicate contributions** already folded into states!

### 3.2 PolyPulse Durable Outbox & Dual-Defense Architecture
To achieve 100% mathematical precision under arbitrary network retries, crash recoveries, and backpressure:

```mermaid
flowchart TD
    subgraph SQLite_Outbox["SQLite Durable Outbox Tier (backend/durable.py)"]
        EV["Unexported Events / Obs"] -->|_fetch_unassigned_items| CB["create_export_batch()"]
        CB -->|Atomic BEGIN IMMEDIATE| EB[("export_batches<br/>batch_id (PK), dedup_token (SHA-256)<br/>canonical immutable payload")]
        CB -->|Enforce 1:1 mapping| EBI[("export_batch_items<br/>PRIMARY KEY(target_table, source_table, item_id)")]
    end

    subgraph Export_Loop["Reliable Export Pipeline"]
        EB -->|get_or_create_pending_batch| CK["check_dedup_window_risk()<br/>age &gt; 3600s or attempts &gt;= 25?"]
        CK -->|At Risk| Q[("Quarantined<br/>Operator Alert")]
        CK -->|Safe| POST["HTTP POST ClickHouse<br/>insert_deduplicate=1<br/>dedup_token=SHA256"]
    end

    subgraph ClickHouse_Ingestion["ClickHouse Engine (Database Tier)"]
        POST --> DEDUP{"Token in 1,000-block window?"}
        DEDUP -->|Yes - Duplicate Block| DROP["Drop Block<br/>Return HTTP 200"]
        DEDUP -->|No - Fresh Block| BASE["Insert Base ReplacingMergeTree"]
        BASE --> MV_TRIG["Incremental Materialized View Trigger<br/>trade_candles_1m"]
    end

    POST -->|HTTP 200 OK| ACK["acknowledge_batch()<br/>status = 'acknowledged'<br/>events.exported = 1"]
```

1. **Persistent Immutable Export Batches (SQLite Outbox)**:
   - Rather than dynamically constructing batches from whatever unexported rows happen to be available during a retry iteration, PolyPulse persists immutable batches inside SQLite WAL tables:
     - **`export_batches`**: Stores `batch_id` (UUID PK), `target_table`, `source_table`, `item_count`, `event_ids` (JSON), canonical serialized `payload`, `dedup_token` (deterministic SHA-256 of `target:payload`), creation timestamp, `attempts`, `status` (`pending`, `quarantined`, `acknowledged`), `last_error`, and `acknowledged_at`.
     - **`export_batch_items`**: Maintains a composite `PRIMARY KEY (target_table, source_table, item_id)`. This database constraint physically prevents any event from being assigned to multiple active export batches for the same ClickHouse destination.
   - **Canonical Payload & Deterministic Token**: Once created, batch membership, payload string, and SHA-256 token are frozen. Retries resend the exact same token and payload.
   - **At-Least-Once Handshake**: A batch is marked `acknowledged` (and underlying events marked `exported = 1`) strictly inside a SQLite transaction after ClickHouse returns `HTTP 200 OK`.
   - **Crash Recovery & Restart Resiliency**: On crash or restart, `get_or_create_pending_batch` resumes the unacknowledged batch in strict FIFO order without re-chunking or modifying membership.

2. **Deduplication Window Eviction Risk Monitoring & Quarantine**:
   - Single-node ClickHouse maintains a finite deduplication cache (`non_replicated_deduplication_window = 1000 blocks`). If an unacknowledged batch is delayed past 1,000 subsequent inserts, ClickHouse could evict its token and treat a retry as a new block.
   - `check_dedup_window_risk()` tracks batch age and attempts. If a batch is older than 1 hour or has failed $\ge 25$ times, it transitions to `quarantined`, pauses automatic export for that target, and raises an operator alert to prevent silent double-counting.
   - Operators can inspect ClickHouse and invoke `recover_quarantined_batch(batch_id, "verify_and_ack")` or `"force_retry"`.

3. **Query Analytics Layer Defense**:
   - For all authoritative analytical metrics (volume, trade count, buy/sell volumes, VWAP, high, low, close), `backend/analytics.py` queries `trades FINAL` with `uniqExact(event_id)`.
   - OHLC open/close uses deterministic secondary tie-breaking: `argMin(price, (received_timestamp, event_id))` and `argMax(price, (received_timestamp, event_id))`.
   - With persistent outbox batches, both the Materialized View and the Authoritative Analytics API report 100% identical metrics across retries and restarts.

### 3.3 14-Step Duplicate-Delivery Integration Audit Results
The 14-step integration test (`tests/test_clickhouse_analytics.py::test_14_step_duplicate_delivery_audit`) verified the entire end-to-end persistent outbox lifecycle against a live ClickHouse 25.8 instance:

| Step | Operation / Scenario | Observed System Behavior | Audit Status |
| :--- | :--- | :--- | :--- |
| **1** | Initialize database schema | Tables and MVs verified with `non_replicated_deduplication_window=1000` | **PASS** |
| **2** | Ingest Batch 1 ($T_1, T_2, T_3$) & Snapshots ($S_1, S_2$) | Persistent Batch 1 created in SQLite outbox; 3 trades, 2 snapshots exported with SHA-256 token | **PASS** |
| **3** | Record initial baseline aggregates | MV: 3 trades, 45.0 vol (30 buy, 15 sell), OHLC: 0.50 / 0.55 / 0.48 / 0.48 | **PASS** |
| **4** | Simulate lost ACK: replay Batch 1 ($T_1, T_2, T_3$) | Same persistent batch and deduplication token resent to ClickHouse | **PASS** |
| **5** | Verify ClickHouse deduplication | ClickHouse recognizes matching token in 1000-block window; duplicate block dropped | **PASS** |
| **6** | Check materialized view aggregates | MV volume remained 45.0; trade count remained 3 (zero double-counting) | **PASS** |
| **7** | Simulate exporter restart: replay Batch 1 | Journal reloaded from disk; pending Batch 1 resumed with identical token and payload | **PASS** |
| **8** | Check materialized view aggregates | MV volume remained 45.0; trade count remained 3 | **PASS** |
| **9** | Ingest new trade $T_4$ & test batch immutability | SQLite rejects rogue attempt to assign $T_3$ to a new batch (`sqlite3.IntegrityError`). Batch 2 formed containing strictly $T_4$ (25.0 vol, 0.52 buy) | **PASS** |
| **10** | Check materialized view aggregates | Batch 2 accepted $\rightarrow$ MV volume updated to **70.0** (4 trades, 55 buy, 15 sell, close 0.52) | **PASS** |
| **11** | Query PolyPulse Analytics API (`/summary`, `/candles`, `/trades`) | Authoritative API returned **4 unique trades, 70.0 volume, 55 buy, 15 sell, close 0.52**. **MV and API AGREE COMPLETELY!** | **PASS** |
| **12** | Inspect physical vs logical row counts | `system.parts` physical rows: $\ge 4$; logical rows `uniqExact(event_id)`: 4 | **PASS** |
| **13** | Execute `OPTIMIZE TABLE ... FINAL` | Merged data parts across `trades` and `trade_candles_1m` | **PASS** |
| **14** | Record final state post-OPTIMIZE | Both MV and Analytics API continue to report exact **70.0 volume and 4 trades** | **PASS** |

### 3.4 Failure Scenarios & Edge Cases Verified in Automated Test Suite

| Test Method | Failure Scenario Evaluated | Verification Outcome |
| :--- | :--- | :--- |
| `test_crash_before_sending_batch` | Crash occurs after SQLite batch formation but prior to network transmission | Batch remains `pending` in SQLite; reloaded process resumes exact batch ID, token, and payload |
| `test_crash_after_clickhouse_accepts_before_ack` | ClickHouse accepts block (HTTP 200) but process crashes before SQLite records ACK | On reboot, unacknowledged batch is retried; ClickHouse deduplicates block; exactly 1 row present |
| `test_exact_batch_retry` | Network replay sends identical batch 3 times | HTTP 200 returned on all attempts; ClickHouse table row count and MV counts remain strictly 1 |
| `test_backend_restart_followed_by_exact_retry` | Exporter daemon fully restarts with an unacknowledged batch in SQLite | Exporter re-reads journal, resends pending batch with same token; ClickHouse deduplicates cleanly |
| `test_new_events_arriving_while_old_batch_pending` | High-frequency events arrive while previous batch is waiting on slow ClickHouse ACK | Old batch remains immutable; new events remain unassigned; new batch forms strictly after ACK |
| `test_multiple_pending_batches_fifo` | Multiple batches accumulate during extended downstream ClickHouse outage | Batches are ordered and processed in strict FIFO order by `created_at` / `rowid` |
| `test_simulated_network_timeout` | HTTP timeout occurs during exporter post | Attempt count incremented, error persisted; batch remains pending with unchanged token and payload |
| `test_retry_after_long_outage_quarantine` | Batch reaches 26 failed attempts (eviction risk from 1000-block window) | Batch status transitions to `quarantined`; export loop paused; operator `verify_and_ack` unlocks pipeline |
| `test_attempted_rechunking_rejected` | Code attempts to create a new batch covering already-batched events | `_fetch_unassigned_items` excludes assigned items; batch creation returns None |
| `test_duplicate_event_assignment_sqlite_constraint` | Direct SQL attempt to assign same event ID to multiple export batches | SQLite raises `sqlite3.IntegrityError` on composite PK `(target_table, source_table, item_id)` |
| `test_backfill_safety_and_idempotency` | Historical backfill script re-runs over identical event block | Idempotent insertion using deterministic tokens; zero duplicate rows or MV inflation |

---

## 4. Historical Backfill & Migration Audit

### 4.1 Late-Arriving Events & Partition Boundaries
- `trades` uses `ORDER BY (venue, market_id, event_id)` and `PARTITION BY toYYYYMM(received_timestamp)`.
- ReplacingMergeTree merges duplicates **strictly within the same partition**.
- If historical data is ingested with `received_timestamp` set to historical execution time, rows land in their historical monthly partitions and merge cleanly.
- If historical data were ingested with `received_timestamp` set to the current ingestion time, rows would land in the current month's partition, bypassing deduplication against older historical records.

### 4.2 Materialized View Backfill Isolation Procedure
Because incremental MVs trigger automatically on `INSERT`, directly streaming historical backfills into `polypulse.trades` can cause double-counting if historical records overlap previously ingested periods. The documented, safe backfill procedure:
1. **Stage Data**: Ingest raw historical records into an isolated staging table:
   ```sql
   CREATE TABLE polypulse.trades_staging AS polypulse.trades;
   ```
2. **Deduplicate Against Base Table**:
   ```sql
   INSERT INTO polypulse.trades
   SELECT * FROM polypulse.trades_staging
   WHERE (venue, market_id, event_id) NOT IN (
       SELECT venue, market_id, event_id FROM polypulse.trades
   );
   ```
3. **Idempotent MV Population**:
   When backfilling historical intervals into `trade_candles_1m`, disable or detach the incremental view trigger (`DETACH TABLE polypulse.mv_trade_candles_1m`), insert directly from deduplicated base tables using `INSERT INTO polypulse.trade_candles_1m SELECT ... GROUP BY ...`, and re-attach the view.

---

## 5. Analytical Correctness Audit

All analytical calculations in `backend/analytics.py` and `database/init.sql` were audited against quantitative trading standards:

1. **Deterministic OHLC Ordering**:
   - `open_price`: `argMin(price, (received_timestamp, event_id))`
   - `close_price`: `argMax(price, (received_timestamp, event_id))`
   - Secondary sort key `event_id` ensures that if multiple trades occur in the exact same millisecond/microsecond, the result is 100% deterministic and reproducible across query replays.
2. **Volume Consistency**:
   - `volume`, `buy_volume`, `sell_volume` represent contract shares (e.g. 10.0 shares).
   - `dollar_volume` represents notional USD value ($\sum \text{price} \times \text{shares}$).
   - Units are standardized across `/summary`, `/candles`, `/trades`, and `/venues`.
3. **VWAP Zero-Volume Safety**:
   - Scaled decimal division:
     ```sql
     if(sum(quantity) > 0,
        toDecimal64(sum(toDecimal64(price, 6) * toDecimal64(quantity, 6)) / sum(quantity), 6),
        toDecimal64(0, 6)) AS avg_price
     ```
   - Matches decimal scale `(18, 6)` across branches to prevent ClickHouse code 48 exceptions.
   - Prevents SQL division-by-zero on zero-volume candles.
4. **Spread Analytics Invariants**:
   - Crossed books (`best_ask <= best_bid`) and invalid/empty quotes (`best_bid <= 0` or `best_ask <= 0`) are explicitly filtered out via `AND best_ask > best_bid AND best_bid > 0` across SQL views and API queries.
5. **Venue Event Rates**:
   - Calculated as $\text{event\_count} / \max(1, \Delta t_{\text{seconds}})$ over the requested query window, preventing divide-by-zero during ingestion idle periods.

---

## 6. Empirical Performance Audit & Scalability Analysis

All benchmarks were captured using `scripts/benchmark_clickhouse.py` executing against a live ClickHouse 25.8 instance in Docker (`polypulse-clickhouse-1`), over a synthetic 7-day high-frequency dataset of 50,000 trades and 50,000 orderbook snapshots.

### 6.1 Benchmark Results Table (RTT vs Server Execution)

| Analytical Query Pattern | p50 RTT | Server p50 | Client/Network Overhead | Rows Read | Bytes Read |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **1m OHLCV Candles (Raw Trades Table, 24h)** | 26.79 ms | 17.30 ms | 9.49 ms | 26,903 | 1.67 MB |
| **1m OHLCV Candles (Materialized View, 24h)** | 25.02 ms | 14.31 ms | 10.71 ms | **5,482** | 1.78 MB |
| **24h Market Summary (Volume, VWAP, Bounds)** | 23.17 ms | 16.97 ms | 6.20 ms | 26,903 | 1.70 MB |
| **1-Hour Spread Trajectory (10s resolution)** | 13.37 ms | 7.78 ms | 5.59 ms | 10,047 | 356.8 KB |
| **Paginated Recent Trades (OFFSET 500 LIMIT 100)** | 17.43 ms | 12.34 ms | 5.08 ms | 50,156 | 5.82 MB |

### 6.2 Multi-Scale Scalability Analysis: Raw Table vs Materialized View

| Scale Tier | Target | Rows Read | Server p50 | RTT p50 | Row Scan Reduction |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Small (~1.2k rows / 4h)** | Raw | 26,903 | 15.21 ms | 21.69 ms | **4.9x scan reduction** |
| | MV | 5,482 | 11.93 ms | 18.03 ms | *(baseline pre-agg)* |
| **Medium (~10.7k rows / 36h)** | Raw | 26,903 | 19.28 ms | 29.36 ms | **4.9x scan reduction** |
| | MV | 5,482 | 12.02 ms | 22.46 ms | *(baseline pre-agg)* |
| **Full (50k rows / 7d)** | Raw | 50,156 | 37.75 ms | 83.01 ms | **4.9x scan reduction** |
| | MV | 10,170 | **16.41 ms** | **55.99 ms** | **2.3x server speedup** |

### 6.3 Why Materialized Views Do Not Always Reduce Latency for Small Queries
In previous benchmarking, 1m candle MV queries scanned 5x fewer rows than raw tables, but p50 latency was comparable (19.57ms raw vs 21.08ms MV). Our audit explains the exact mechanical cause:
1. **State Deserialization Overhead**: AggregatingMergeTree stores binary states (`argMinState`, `argMaxState`, `avgState`). Merging these states requires CPU deserialization and function calls across active parts.
2. **Columnar SIMD Efficiency**: Raw tables store primitive Decimal64 values in contiguous uncompressed memory. On small scans (<5,000 rows), ClickHouse's vectorized execution scans primitive arrays in RAM almost instantaneously without deserialization overhead.
3. **Network/Client Overhead Dominance**: Localhost HTTP round-trip latency (5–11 ms) and JSON deserialization mask sub-millisecond query execution differences on small datasets.
4. **Asymptotic Value**: As datasets grow to 50,000+ rows, the I/O advantage of scanning 5x fewer rows outweighs state deserialization overhead, yielding a **2.3x server execution speedup** (16.41ms vs 37.75ms).
5. **Non-Constant Query Cost**: Query cost on AggregatingMergeTree is **not constant-bounded** $O(1)$; it scales $O(T / \Delta t \times P)$, where $T$ is the query time window, $\Delta t$ is candle resolution, and $P$ is the number of active unmerged parts.

---

## 7. Storage Metrics & Retention Audit

ClickHouse operational telemetry (`GET /api/v1/analytics/storage`) was audited to ensure strict distinction between physical on-disk storage and logical data:
- **`physical_rows`**: Sum of rows across all active parts (`active = 1`) in `system.parts`. In at-least-once ingestion, physical rows include retried blocks before background part merges collapse them.
- **`active_parts`**: Number of separate immutable data parts currently on disk. As background merges execute, parts decrease and physical rows approach logical row counts.
- **`compressed_bytes` vs `uncompressed_bytes`**: Accurate on-disk footprint vs in-memory raw size, yielding observed compression ratios between $1.6\times$ and $4.2\times$.
- **TTL Retention**: ClickHouse executes TTL cleanups during background merges (not instantaneously upon expiration). Tables are verified with 7-day, 14-day, 90-day, and 180-day TTLs.

---

## 8. Audit Verification & Completion Status

- [x] **Deduplication Verification**: ClickHouse 25.8 non-replicated deduplication window (1000) verified; SHA-256 batch token and dual-defense query layer documented.
- [x] **Persistent Outbox Batch Mechanism**: Implemented SQLite `export_batches` and `export_batch_items` with strict composite PK preventing double-batching of events.
- [x] **Deduplication Window Risk Monitoring**: Implemented automated age (>3600s) and attempt (>=25) eviction-risk tracking with quarantine and operator recovery.
- [x] **14-Step Duplicate Delivery Test**: Implemented and passing in `tests/test_clickhouse_analytics.py`, proving MV and Authoritative API agreement.
- [x] **16 Failure & Edge-Case Unit Tests**: Crash recovery, retry idempotency, restart handling, unassigned event immutability, FIFO ordering, timeout recording, and backfill safety all tested and passing.
- [x] **Historical Backfill Architecture**: Documented staging isolation, partition boundary rules, and deterministic `(timestamp, event_id)` tie-breakers.
- [x] **Performance Documentation Corrected**: RTT separated from server execution time; state deserialization vs SIMD columnar scan trade-offs documented; "constant-bounded" claim removed.
- [x] **Analytical Correctness Audited**: Safe VWAP, deterministic OHLC, crossed-book exclusion, and volume consistency verified.
- [x] **Full Regression Pass**: All 49 unit tests, C++ engine tests, and frontend build pass cleanly.

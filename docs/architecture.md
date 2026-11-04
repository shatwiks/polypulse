# System Architecture

PolyPulse is a high-performance cross-venue market data ingestion, normalization, and markout pipeline designed for prediction markets (such as Polymarket and Kalshi). It provides a unified orderbook interface, deterministic tick-level event processing, local SQLite journaling, scalable ClickHouse time-series persistence, and a real-time reactive trading terminal.

---

## 1. High-Level Architecture Overview

PolyPulse is split into four decoupled architectural tiers:
1. **Ingestion & Self-Healing Pipeline (Python AsyncIO)**: Ingests raw public and authenticated WebSocket streams, enforces sequence integrity, detects gaps, deduplicates payloads, and normalizes disparate venue schemas into integer ticks.
2. **Core Computational Engine (C++20)**: Maintains contiguous sorted orderbook levels, computes Top-of-Book quotes, calculates zero-allocation Volume-Weighted Average Price (VWAP), and evaluates Order-Flow Imbalance (OFI).
3. **Storage & Analytics Tier (SQLite WAL & ClickHouse)**: Guarantees local durability and offline replay via SQLite in Write-Ahead Log (WAL) mode, and asynchronously micro-batches events to a ClickHouse columnar warehouse for post-trade markout scoring.
4. **Presentation & Terminal Layer (FastAPI & React 19)**: Streams live book updates and metrics via WebSockets to a React terminal utilizing off-main-thread Web Workers for high-throughput JSON deserialization.

![System Architecture Overview](assets/architecture-overview.svg)

---

## 2. End-to-End Component Interaction

```mermaid
flowchart TD
    subgraph Venues ["Prediction Market Venues"]
        PM["Polymarket CLOB<br/>(Public WebSocket)"]
        KL["Kalshi Trade API v2<br/>(RSA-Signed WebSocket)"]
        PG["Polygon EVM Fills<br/>(Planned - Phase 9)"]
    end

    subgraph Ingestion ["Ingestion & Reliability Layer (Python AsyncIO)"]
        WS_C["WebSocket Client Pool"]
        WD["30s Silence Watchdog & Heartbeat"]
        DEDUP["SHA-256 Rolling Deduplication (10k items)"]
        SEQ["Sequence & Gap Evaluator"]
        NORM["Normalizer: Integer Ticks (1/10,000 unit)"]
    end

    subgraph Engine ["High-Performance Engine (C++20)"]
        DLL["C FFI Interface (extern 'C')"]
        OB["std::vector&lt;PriceLevel&gt;<br/>Contiguous Memory Layout"]
        VWAP["VWAP Kernel (Extended Precision)"]
        OFI["Order-Flow Imbalance Kernel"]
    end

    subgraph Storage ["Storage & Analytics Layer"]
        SQLITE[("SQLite Journal (WAL Mode)<br/>polypulse.db")]
        EXPORTER["Async HTTP Batch Exporter"]
        CLICKHOUSE[("ClickHouse OLAP<br/>ReplacingMergeTree & ASOF JOIN")]
        MARKOUT["Post-Trade Markout Calculator<br/>5s, 30s, 60s Horizons"]
    end

    subgraph Presentation ["Presentation & API Layer"]
        API["FastAPI Gateway (/api/v1/*)"]
        WS_SRV["WebSocket Broadcaster (/ws/markets, /ws/wallets)"]
        WORKER["Web Worker Stream Parser (off-thread)"]
        UI["React 19 Terminal (Orderbook, Scanner, Wallets)"]
    end

    PM -->|Raw JSON / Ping| WS_C
    KL -->|Raw Delta / Snapshot| WS_C
    PG -.->|Contract Logs| WS_C

    WS_C --> WD
    WD --> DEDUP
    DEDUP --> SEQ
    SEQ -->|Valid Next Seq| NORM
    SEQ -->|Gap Detected| WS_C

    NORM -->|C ctypes Bridge| DLL
    DLL --> OB
    OB --> VWAP
    OB --> OFI

    NORM -->|Write Event| SQLITE
    OB -->|Write Snapshot| SQLITE
    SQLITE --> EXPORTER
    EXPORTER --> CLICKHOUSE
    CLICKHOUSE --> MARKOUT

    OB -->|Broadcast Message| WS_SRV
    API --> SQLITE
    API --> OB
    WS_SRV --> WORKER
    WORKER --> UI
    API --> UI
```

---

## 3. Market-Data Event Lifecycle

The lifecycle of an orderbook delta or snapshot follows strict sequence and state invariants to prevent race conditions and state corruption.

```mermaid
sequenceDiagram
    autonumber
    participant Venue as Venue WebSocket (Polymarket / Kalshi)
    participant Feed as AsyncIO Connector & Watchdog
    participant Norm as Normalizer
    participant Engine as C++20 OrderBook Engine
    participant Journal as SQLite WAL Journal
    participant Broadcaster as WebSocket Broadcast Queue
    participant Terminal as React 19 Client (Web Worker)

    Venue->>Feed: Raw WS Frame (Delta or Snapshot)
    Feed->>Feed: SHA-256 Digest Check (Duplicate Filter)
    Feed->>Feed: Sequence Continuity Check (seq == prev + 1)
    alt Sequence Gap Detected
        Feed->>Feed: Increment gap counter & abort session
        Feed->>Venue: Reconnect & Resubscribe (Trigger Snapshot)
    else Sequence Valid
        Feed->>Norm: Parse raw payload to integer ticks
        Norm->>Norm: Scale floats by 10,000 (clamp 0..10,000)
        Norm->>Engine: pp_set_level(side, price_ticks, qty_units, seq)
        Engine->>Engine: Binary Search (std::lower_bound) & in-place update
        Norm->>Journal: Insert into events table (WAL commit)
        Engine->>Journal: Record book_observation (Snapshot)
        Engine->>Broadcaster: Queue JSON delta (maxsize=100)
        Broadcaster->>Terminal: WebSocket broadcast frame
        Terminal->>Terminal: Off-thread JSON parse in stream.worker.ts
        Terminal->>Terminal: Re-render orderbook ladder & spreads
    end
```

---

## 4. Key Architectural Decisions

### Contiguous Flat Vector vs. Pointer Trees
Typical financial engines use red-black trees (`std::map`) or skip lists for orderbooks. In prediction markets, binary contracts have bounded price levels (0.00 to 1.00 dollars, represented as 0 to 10,000 integer ticks). Most active prediction market contracts have fewer than 20 distinct active price levels.
- A contiguous `std::vector<PriceLevel>` guarantees cache-line locality: four 16-byte `PriceLevel` structs fit inside a single 64-byte L1 cache line.
- `std::lower_bound` executes in under 70 ns with virtually zero cache misses and zero dynamic heap allocation during normal quote movements.

### Zero-Copy / Extended Precision Arithmetic
- Arithmetic in `vwap()` uses 80-bit extended precision (`long double`) to prevent integer overflow when multiplying large contract quantities by tick prices, while avoiding intermediate dynamic floating-point allocations.
- Fixed-point integer ticks eliminate floating-point non-determinism and representation errors across Python, C++, and ClickHouse.

### Local Write-Ahead Log (WAL) Before Remote Warehouse
- Remote warehouse connectivity (e.g., ClickHouse) can experience network latency or transient network partitions.
- By committing all events synchronously to a local SQLite database in WAL mode before asynchronous export, the application retains zero event loss and can replay state deterministically upon cold startup.

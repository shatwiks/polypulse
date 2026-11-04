# Changelog

All notable changes to the PolyPulse project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [0.1.0] - 2026-11-04

### Added
- **Core Engine (C++20)**:
  - Low-latency sorted orderbook with contiguous memory cache locality (`std::vector<PriceLevel>`).
  - Integer tick normalization ($10^4$ scale factor: 1 tick = \$0.0001, bounded $0 \le P \le 10\,000$).
  - Zero-allocation volume-weighted average price (`pp_vwap`) using 80-bit extended precision (`long double`).
  - Real-time order-flow imbalance (`pp_imbalance`) normalized quote pressure metric.
  - Zero-alloc Top-of-Book quote extraction (`pp_quote`) and memory buffer copy (`pp_levels`).
  - Full C foreign function interface (`extern "C"`) and Python `ctypes` bindings.
- **Ingestion & Self-Healing Pipeline**:
  - Polymarket public CLOB WebSocket feed connector with active PING/PONG keepalives.
  - Kalshi authenticated WebSocket v2 connector with RSA-PSS SHA-256 request signatures.
  - 30-second silence watchdog automatically terminating dead connections.
  - Per-subscription sequence gap evaluator triggering automated resubscription and snapshot recovery.
  - SHA-256 rolling digest queue (10,000 capacity) for high-frequency duplicate packet rejection.
  - Synthetic demo mode generator with repeatable pseudo-random stream (seed 42).
- **Persistence & Storage Architecture**:
  - In-process SQLite Write-Ahead Log (`polypulse.db`) for zero-loss local event journaling and offline replay.
  - Micro-batch HTTP exporter (`export_forever`) streaming journaled events to ClickHouse.
  - ClickHouse database DDL: `polypulse.market_events` with `ReplacingMergeTree(received_at)` and `polypulse.orderbook_snapshots` with `MergeTree`.
- **Analytics & Markout Scoring**:
  - Post-trade markout scoring across 5s, 30s, and 60s horizons: $\text{Markout} = \text{Dir} \times (P_{\text{mid}} - P_{\text{exec}})$.
  - Zero-lookahead null safety returning explicit `null` for unobserved future ticks without artificial interpolation.
  - Cross-venue spread engine calculating gross quote differentials between verified pairs.
- **Presentation & React Terminal**:
  - FastAPI asynchronous REST API (`/api/v1/markets`, `/orderbook`, `/spreads`, `/wallets`, `/health`).
  - Real-time WebSocket streaming endpoints (`/ws/markets`, `/ws/wallets`).
  - React 19 terminal interface featuring dark technical design (#080B12, purple #8B5CF6, cyan #22D3EE).
  - Dedicated Web Worker (`stream.worker.ts`) offloading JSON stream deserialization from the UI thread.
  - Real-time orderbook ladder, cross-venue scanner, wallet intelligence explorer, and diagnostics dashboard.
- **Developer Experience & Benchmarking**:
  - Reproducible C++20 microbenchmark harness (`engine/benchmarks/benchmark.cpp`, `scripts/benchmark.py`).
  - Multi-tier test suite covering C++ assertions, Python pipeline normalization, and TypeScript builds.
  - Comprehensive documentation suite under `docs/` and animated SVG architecture diagrams.

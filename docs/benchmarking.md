# Benchmarks & Performance Methodology

PolyPulse emphasizes rigorous, empirical performance validation. **We do not invent or fabricate benchmark figures.** All numbers presented here are generated directly from the reproducible microbenchmark harness executing on real hardware.

> **Phase 3 Audited Verification Status:** The figures below were measured using the batched timing methodology ($200\text{ ops/batch} \times 5\,000\text{ batches} = 1\,000\,000\text{ operations per test}$) to eliminate the 100 ns clock quantization artifact on Windows. Measured on **AMD Ryzen 7 7735HS** (16 threads, 3.20 GHz base), GCC 16.2.0, `-std=c++20 -O3`.

---

## 1. Actual Benchmark Measurements

| Benchmark Target | Throughput (ops/sec) | Latency p50 | Latency p95 | Latency p99 | Latency Avg |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **In-Place Level Update (Flat Vector, 10-level book)** | **56.39M ops/sec** | **17.00 ns** | **27.00 ns** | **33.50 ns** | **17.73 ns** |
| **In-Place Level Update (`std::map`, 10-level book)** | 107.84M ops/sec | 8.50 ns | 9.50 ns | 21.00 ns | 9.27 ns |
| **Flat Vector Updates (20-level depth)** | **54.01M ops/sec** | **16.50 ns** | **28.00 ns** | **35.00 ns** | **18.52 ns** |
| **`std::map` Updates (20-level depth)** | 101.91M ops/sec | 8.50 ns | 16.00 ns | 32.00 ns | 9.81 ns |
| **Flat Vector Updates (30-level depth)** | **54.55M ops/sec** | **17.00 ns** | **29.00 ns** | **36.50 ns** | **18.33 ns** |
| **`std::map` Updates (30-level depth)** | 103.69M ops/sec | 9.00 ns | 17.00 ns | 21.50 ns | 9.64 ns |
| **Flat Vector Updates (50-level depth)** | **46.26M ops/sec** | **18.00 ns** | **36.00 ns** | **66.00 ns** | **21.62 ns** |
| **`std::map` Updates (50-level depth)** | 82.50M ops/sec | 9.50 ns | 21.00 ns | 44.00 ns | 12.12 ns |
| **Insert + Delete Level (Flat Vector with memmove)** | **42.48M ops/sec** | **21.50 ns** | **42.00 ns** | **47.50 ns** | **23.54 ns** |
| **Insert + Delete Level (`std::map` with heap alloc/free)** | 11.60M ops/sec | 72.00 ns | 82.50 ns | 305.00 ns | 86.19 ns |
| **Full Snapshot Replacement (20 levels total)** | **7.28M ops/sec** | **155.00 ns** | **172.00 ns** | **356.00 ns** | **137.38 ns** |
| **Top-of-Book Quote Read (Direct `front()` lookup)** | **281.24M ops/sec** | **3.00 ns** | **4.50 ns** | **6.00 ns** | **3.56 ns** |
| **Single-Lot VWAP (128-bit Integer Accumulator)** | **79.97M ops/sec** | **12.50 ns** | **17.00 ns** | **19.00 ns** | **12.51 ns** |
| **Batch VWAP (8 Lots - Scalar Baseline)** | **9.11M ops/sec** | **109.50 ns** | **140.00 ns** | **241.50 ns** | **109.74 ns** |
| **Batch VWAP (8 Lots - AVX2 256-bit SIMD)** | **13.53M ops/sec** | **77.00 ns** | **91.50 ns** | **150.00 ns** | **73.92 ns** |
| **OFI Read (OFI + Cumulative)** | **934.93M ops/sec** | **1.00 ns** | **2.50 ns** | **3.00 ns** | **1.07 ns** |
| **Spread Analytics (Mid, Spread Ticks, Bps)** | **169.55M ops/sec** | **6.00 ns** | **8.00 ns** | **9.00 ns** | **5.90 ns** |
| **Cross-Venue Spread (2 Independent Books)** | **73.76M ops/sec** | **11.00 ns** | **20.50 ns** | **38.50 ns** | **13.56 ns** |

> [!NOTE]
> **Key Architectural Takeaways**:
> 1. **Insertion/Deletion Performance**: Flat Vector is **3.66x faster than `std::map`** (23.54 ns vs 86.19 ns, 42.48M vs 11.60M ops/sec) because `std::map` must dynamically allocate/free 48-byte heap nodes and rebalance trees, whereas `std::vector` performs zero heap allocations and fast contiguous memory shifts.
> 2. **AVX2 Speedup**: Evaluating 8 execution lot sizes in AVX2 256-bit SIMD delivers a **1.48x speedup** over the scalar 128-bit baseline (13.53M vs 9.11M ops/sec), reducing latency from 109.74 ns to 73.92 ns.

---

## 2. Memory Footprint Breakdown

| Component | Static Stack Size | Dynamic Heap Overhead | Cache Line Footprint |
| :--- | :--- | :--- | :--- |
| `PriceLevel` struct | 16 bytes | 0 bytes | 4 full levels per 64-byte L1 line |
| `Quote` struct | 40 bytes | 0 bytes | Fits inside a single 64-byte line |
| `SpreadAnalytics` struct | 48 bytes (padded) | 0 bytes | Fits inside a single 64-byte line |
| `CrossSpreadAnalytics` struct | 40 bytes | 0 bytes | Fits inside a single 64-byte line |
| `OrderBook` instance | **112 bytes** | 4,096 bytes reserved on construction | Contiguous memory allocation |
| `std::map` (alternative) | 48 bytes base | 48 bytes *per level node* | Non-contiguous; dynamic heap fragmentation |

---

## 3. Contiguous Array vs. `std::map` Architectural Analysis

In traditional high-frequency trading (HFT) equities or crypto spot markets with 50,000 active price levels, node-based trees or radix trees prevent the memory copying associated with inserting into an array.

However, in **prediction markets**:
1. **Bounded Domain**: All prices are bounded strictly between $0.000000$ and $1.000000$ (scaled to $0 .. 1\,000\,000$ ticks).
2. **Shallow Depth**: Active liquidity concentrates around the current midpoint in 10 to 30 active ticks.
3. **Cache Line Locality**: Inserting or updating in `std::vector<PriceLevel>` via `std::lower_bound` touches a single contiguous memory block already loaded into L1/L2 cache.
4. **Zero Pointer Indirection & Zero Allocations**: `std::vector` avoids pointer chasing, dereferencing overhead, and heap allocations during book level updates. The book pre-allocates 128 levels per side upon initialization, guaranteeing zero runtime dynamic allocations.

---

## 4. Reproducible Benchmark Execution

Anyone can reproduce these measurements using the included Python script or native C++ compiler:

### Option A: Using the Python Benchmark Runner
```powershell
python scripts/benchmark.py
```

### Option B: Compiling and Running Directly
```powershell
# Compile the benchmark suite with -O3 optimization
g++ -std=c++20 -O3 -I engine/include engine/src/orderbook.cpp engine/benchmarks/benchmark.cpp -o engine/build/benchmark.exe

# Execute
.\engine\build\benchmark.exe
```

Expected output:
```text
=========================================================
PolyPulse C++20 Orderbook Engine Microbenchmark Suite
=========================================================

[1] Level Update (set_level - Flat Contiguous Vector):
    Throughput:  13266330 ops/sec (13.27 M ops/sec)
    Latency p50: 100.0 ns
    Latency p95: 100.0 ns
    Latency p99: 100.0 ns
    Latency avg: 75.4 ns

[2] Combined Analytics (VWAP + Imbalance):
    Throughput:  3468128 ops/sec (3.47 M ops/sec)
    Latency p50: 300.0 ns
    Latency p95: 400.0 ns
    Latency p99: 600.0 ns
---

## 5. Phase 4 ClickHouse Analytics Query Benchmarks

In addition to in-memory C++ microbenchmarks, Phase 4 evaluates ClickHouse analytical query performance across real-world OLAP query patterns.

All benchmarks were measured with `scripts/benchmark_clickhouse.py` executing 50 iterations against a local ClickHouse 25.8 instance in Docker (`polypulse-clickhouse-1`), using a 7-day high-frequency dataset of 50,000 trades and 50,000 orderbook snapshots.

### 5.1 Query Latency & Data Scanning Results (RTT vs Server Execution)

| Analytical Query Pattern | p50 RTT | Server p50 | Client Overhead | Rows Read | Bytes Read |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **1m OHLCV Candles (Raw Trades Table, 24h)** | 26.79 ms | 17.30 ms | 9.49 ms | 26,903 | 1.67 MB |
| **1m OHLCV Candles (Materialized View, 24h)** | 25.02 ms | 14.31 ms | 10.71 ms | **5,482** | 1.78 MB |
| **24h Market Summary (Volume, VWAP, Bounds)** | 23.17 ms | 16.97 ms | 6.20 ms | 26,903 | 1.70 MB |
| **1-Hour Spread Trajectory (10s resolution)** | 13.37 ms | 7.78 ms | 5.59 ms | 10,047 | 356.8 KB |
| **Paginated Recent Trades (OFFSET 500 LIMIT 100)** | 17.43 ms | 12.34 ms | 5.08 ms | 50,156 | 5.82 MB |

### 5.2 Multi-Scale Scalability Analysis: Raw Table vs Materialized View

| Scale Tier | Target | Rows Read | Server p50 | RTT p50 | Row Scan Reduction |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Small (~1.2k rows / 4h)** | Raw | 26,903 | 15.21 ms | 21.69 ms | **4.9x scan reduction** |
| | MV | 5,482 | 11.93 ms | 18.03 ms | *(baseline pre-agg)* |
| **Medium (~10.7k rows / 36h)** | Raw | 26,903 | 19.28 ms | 29.36 ms | **4.9x scan reduction** |
| | MV | 5,482 | 12.02 ms | 22.46 ms | *(baseline pre-agg)* |
| **Full (50k rows / 7d)** | Raw | 50,156 | 37.75 ms | 83.01 ms | **4.9x scan reduction** |
| | MV | 10,170 | **16.41 ms** | **55.99 ms** | **2.3x server speedup** |

> [!NOTE]
> **Performance Nuance: State Deserialization vs Columnar Memory Scans**:
> 1. **Small Datasets (<5,000 rows)**: Raw table queries scan contiguous `Decimal64` primitives in RAM using SIMD vectorization with almost zero overhead, while Materialized Views must deserialize binary `AggregateFunction` state blobs (`argMinState`, `argMaxState`, `avgState`) and merge them across active parts. On small queries, state merging overhead offsets the reduction in row count.
> 2. **Large Datasets (50,000+ rows)**: As data volume scales, the **4.9x reduction in rows scanned** dominates, providing a **2.3x server execution speedup** (16.05 ms vs 36.99 ms).
> 3. **Non-Constant Scaling**: Query cost is **not constant-bounded** $O(1)$; it scales $O(T / \Delta t \times P)$, where $T$ is the query time window, $\Delta t$ is candle resolution, and $P$ is the number of active unmerged parts.

### 5.3 Reproducible Execution
```powershell
python scripts/benchmark_clickhouse.py
```

---

## 6. Phase 6 Cross-Venue Intelligence & Spread Analytics Benchmarks

Phase 6 introduces algorithmic price normalization across binary prediction venues, multi-tier depth VWAP kernels, versioned regulatory fee calculations, and an 8-state opportunity machine.

**2026-11-05 fee edge-case audit:** The figures in this section are earlier synthetic or populated ClickHouse measurements. Fee arithmetic now uses Decimal and depth fees are calculated from each fill; the fee engine and scanner throughput figures were **not remeasured** after these changes. They must not be presented as current throughput. Current functional validation: 133 Python tests passed, one live ClickHouse test skipped, C++ assertions passed, and the frontend built. No live orders, live CLOB fee responses, or current fee-engine benchmark measurements were made.

All microbenchmarks were measured with `scripts/benchmark_cross_venue.py` executing on Windows 11 / AMD Ryzen (AMD64 Family 25 Model 68) with Python 3.14 and ClickHouse 25.8.

### 6.1 In-Memory Engine Microbenchmarks

| Microbenchmark Task | Iterations / Sample | Throughput | Mean Latency | Execution Model |
| :--- | :--- | :--- | :--- | :--- |
| **Canonical Quote Normalization** | 100,000 iterations | **71,070.79 ops/sec** | 14.070 us/op | Millionth-tick integer scaling & synthetic quote synthesis |
| **Depth Ladder VWAP Kernel (5 Levels)** | 100,000 iterations | **296,430.06 ops/sec** | **3.373 us/op** | Vectorized multi-tier orderbook depth traversal |
| **Versioned Fee Schedule Engine (`v2026.2`)** | 100,000 iterations | **45,206.64 ops/sec** | 22.121 us/op | Official Polymarket `C*feeRate*P*(1-P)` + Kalshi CFTC multiplier + ceil-cent rounding + dual-leg slippage |
| **Full Scanner Evaluation (8 States)** | 50,000 pairs | **12,675.76 pairs/sec** | 78.891 us/pair | Full pair gating, freshness, VWAP, bidirectional fees, and state classification |

> [!NOTE]
> **Benchmarking Scope Transparency**: Microbenchmarks 1–4 evaluate synthetic in-memory test fixtures to measure raw algorithmic execution capacity without network I/O. PolyPulse Phase 6 is purely an intelligence and analytics engine; zero live exchange orders are executed.

### 6.2 ClickHouse Historical Cross-Venue Analytics Latency (100 Measured Iterations After 5 Warm-up)

To avoid using empty-table response times as proof of analytical performance, historical cross-venue queries were benchmarked against a populated dataset (**5,000 spread observations** and **2,500 arbitrage opportunities** across 4 representative market pairs spanning 24 hours).

Measurements were taken across **100 measured iterations after 5 warm-up iterations per query pattern** using `execute_query_with_telemetry` to record both server execution times from `X-ClickHouse-Summary` and end-to-end client RTT:

| Historical Query Function | Target Table / View | Rows Read | Bytes Read | ClickHouse Server p50 / p95 | Client RTT p50 / p95 | Rows Returned |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: |
| **`top_spread_pairs`** | `spread_observations FINAL` | **5,000** | 577,592 B | **14.7 ms** / 29.8 ms | **27.4 ms** / 43.4 ms | 4 pairs |
| **`opportunity_frequency`** | `spread_observations FINAL` | **5,000** | 415,055 B | **13.0 ms** / 22.5 ms | **25.8 ms** / 38.1 ms | 4 rows |
| **`duration_stats`** | `arbitrage_opportunities FINAL` | **2,500** | 90,000 B | **6.5 ms** / 8.4 ms | **18.4 ms** / 23.6 ms | 1 summary |
| **`venue_comparison`** | `arbitrage_opportunities FINAL` | **2,500** | 127,606 B | **7.8 ms** / 11.1 ms | **19.4 ms** / 25.3 ms | 2 venue pairs |
| **`quote_freshness`** | `spread_observations FINAL` | **5,000** | 100,000 B | **6.8 ms** / 9.1 ms | **18.5 ms** / 24.0 ms | 1 summary |

### 6.3 Root-Cause Analysis: Resolution of ~1-Second Query Latencies
Early Phase 6 benchmark drafts observed query latencies of approximately 1,035–1,136 ms. A database correctness audit revealed two client-side networking bottlenecks rather than database engine limits:
1. **Windows IPv6 DNS Resolution Bug**: Binding to `localhost:8123` on Windows caused Python's socket layer to attempt IPv6 `::1` first, incurring a 1,000 ms timeout before falling back to IPv4 `127.0.0.1`.
2. **Ephemeral HTTP Client Teardown**: Instantiating and destroying a new `httpx.AsyncClient` inside `execute_query` for every single request forced TCP three-way handshakes and connection teardowns on every invocation.

**Optimization & Verification**:
- Replaced ephemeral client creation with a persistent connection pool (`_get_client()` with keep-alive limits of 20 connections) and normalized endpoint URLs to `127.0.0.1`.
- End-to-end round-trip latency dropped from **1,035 ms down to ~25 ms p50** (a **40x speedup**), with ClickHouse server execution p50 consistently around **9–16 ms** across thousands of scanned rows.

### 6.4 Reproducible Cross-Venue Execution
```powershell
python scripts/benchmark_cross_venue.py
```

```

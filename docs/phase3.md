# Phase 3 Implementation Plan: C++20 High-Performance Orderbook Engine

## 1. Executive Summary & Objectives
Phase 3 transitions PolyPulse from functional Phase 2 live data ingestion to a production-grade, low-latency C++20 market microstructure engine. 

The primary objectives are:
1. **Cache-Friendly Data Structures:** Eliminate heap allocations and memmove overheads in dynamic orderbook level management.
2. **Deterministic Integer Math:** Enforce strict integer ticks ($10^6$) and integer quantity units ($10^6$) across the entire core engine to avoid floating-point rounding errors and non-deterministic behavior.
3. **Advanced Market Microstructure Metrics:**
   - Multi-level, multi-quantity Volume-Weighted Average Price (**VWAP**) with liquidity-exhaustion detection.
   - Order Flow Imbalance (**OFI**) based on the standard Cont-Kukanov-Stoikov (2014) formulation with strict sign conventions.
   - Comprehensive **Spread Analytics** (absolute spread, basis points, cross-venue executable spreads gated by operator-verified contract pairs).
4. **Targeted AVX2 SIMD Acceleration:** Accelerate batch VWAP and multi-level dot-product computations using 256-bit AVX2 vector intrinsics, with automated runtime CPUID dispatch and a scalar fallback.
5. **Seamless Python/ctypes Integration:** Maintain full backward compatibility with the existing Python `Book` class in `backend/engine.py` while surfacing the new analytics to FastAPI and WebSockets.
6. **Empirical Benchmarking & Testing:** Deliver reproducible benchmarks comparing sorted vector, map, and optimized structures, profiling latencies (p50, p95, p99), throughput, and memory.

---

## 2. Orderbook Architecture & Data Structure Design

### 2.1 Trade-Off Analysis of Evaluated Data Structures

| Structure | Lookup | Insertion / Update | Deletion | Memory Locality | Heap Allocs | Evaluation Verdict |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **`std::map<int64_t, int64_t>`** | $O(\log N)$ | $O(\log N)$ | $O(\log N)$ | Poor (tree nodes scattered across heap) | Allocates 32-48 bytes per level insert | **Rejected:** Node allocations cause heap fragmentation; poor cache locality during sequential traversals (VWAP). |
| **Direct Dense Array ($10^6$ ticks)** | $O(1)$ | $O(1)$ | $O(1)$ | High footprint ($8\text{ MB}$ per book) | Single large allocation | **Rejected:** Massive memory overhead for sparse books; traversing to find best bid/ask or compute VWAP requires scanning empty buckets. |
| **`std::vector<PriceLevel>` (Baseline)** | $O(\log N)$ (binary search) | $O(N)$ (linear shift) | $O(N)$ (linear shift) | Excellent (contiguous memory) | Dynamic reallocations if capacity exceeded | **Baseline:** Fast for read traversals, but `insert`/`erase` causes `memmove` on every tick. |
| **Cache-Aligned Flat Bounded Vector with In-Place Compaction** | $O(\log N)$ (branchless search) | $O(N)$ (in L1/L2 cache, small $N \le 128$) | Fast zero-quantity tombstone or small shift | Contiguous 64-byte aligned | Zero steady-state allocations (pre-reserved capacity) | **Selected:** In binary prediction markets, active depth is typically $\le 50$ levels. Contiguous small-array operations stay entirely within CPU L1 data cache (<32 KB), outperforming tree structures while eliminating dynamic heap calls. |

### 2.2 Memory Layout & Struct Definitions
```cpp
namespace polypulse {

constexpr std::int64_t kPriceScale = 1'000'000;      // 6 decimals: 1 tick = 0.000001
constexpr std::int64_t kQuantityScale = 1'000'000;   // 6 decimals: 1 unit = 0.000001

struct alignas(16) PriceLevel {
    std::int64_t price_ticks;      // [0, 1'000'000] for prediction markets
    std::int64_t quantity_units;   // > 0 for active levels
};

struct Quote {
    std::int64_t bid_ticks;
    std::int64_t ask_ticks;
    std::int64_t bid_units;
    std::int64_t ask_units;
    std::uint64_t sequence;
};

struct SpreadAnalytics {
    std::int64_t best_bid;
    std::int64_t best_ask;
    std::int64_t mid_price;
    std::int64_t spread_ticks;
    double spread_bps;
    bool is_crossed;
};

struct CrossSpreadAnalytics {
    std::int64_t buy_1_sell_2_ticks;  // bid_2 - ask_1
    std::int64_t buy_2_sell_1_ticks;  // bid_1 - ask_2
    double buy_1_sell_2_bps;
    double buy_2_sell_1_bps;
    bool executable_buy_1_sell_2;
    bool executable_buy_2_sell_1;
};
```

---

## 3. Market Microstructure Algorithms

### 3.1 Volume-Weighted Average Price (VWAP)
For target execution quantity $Q > 0$:
1. Traverse book levels from top-of-book (asks for buy, bids for sell).
2. Accumulate filled units: $f_i = \min(Q - \text{cum\_q}, q_i)$.
3. Accumulate value using a 128-bit integer accumulator:
   $$\text{TotalValue}_{128} = \sum_{i=1}^{k} (f_i \times P_i)$$
4. If $\text{cum\_q} < Q$, return liquidity exhaustion indicator (`NAN` or `-1`).
5. Otherwise:
   $$\text{VWAP} = \frac{\text{TotalValue}_{128}}{Q \times \text{kPriceScale}}$$

### 3.2 Order Flow Imbalance (OFI)
Following Cont, Kukanov, and Stoikov (2014):
Given consecutive top-of-book observations $(P_{B,t-1}, Q_{B,t-1}, P_{A,t-1}, Q_{A,t-1})$ and $(P_{B,t}, Q_{B,t}, P_{A,t}, Q_{A,t})$:
$$\Delta Q_{B,t} = \begin{cases}
Q_{B,t} & \text{if } P_{B,t} > P_{B,t-1} \\
Q_{B,t} - Q_{B,t-1} & \text{if } P_{B,t} = P_{B,t-1} \\
-Q_{B,t-1} & \text{if } P_{B,t} < P_{B,t-1}
\end{cases}$$

$$\Delta Q_{A,t} = \begin{cases}
-Q_{A,t} & \text{if } P_{A,t} > P_{A,t-1} \\
Q_{A,t} - Q_{A,t-1} & \text{if } P_{A,t} = P_{A,t-1} \\
Q_{A,t} & \text{if } P_{A,t} < P_{A,t-1}
\end{cases}$$

$$\text{OFI}_t = \Delta Q_{B,t} - \Delta Q_{A,t}$$

Edge cases handled:
- Initial observation: OFI is 0.
- Missing previous quote or empty book: OFI is 0.
- One side missing: Delta computed solely from the populated side.

### 3.3 Spread Analytics
- **Mid Price:** $(P_A + P_B) / 2$
- **Spread Ticks:** $P_A - P_B$
- **Spread Basis Points:** $\frac{P_A - P_B}{\text{Mid Price}} \times 10,000$
- **Cross-Venue Arbitrage Spread:**
  - Opportunity 1 (Buy V1, Sell V2): $P_{B,2} - P_{A,1}$
  - Opportunity 2 (Buy V2, Sell V1): $P_{B,1} - P_{A,2}$
  - Strictly gated by operator contract verification status (`verified_only=True`).

---

## 4. AVX2 Vectorization Strategy

### 4.1 Target Computational Kernels
AVX2 SIMD provides 256-bit registers (e.g. four 64-bit integers or eight 32-bit floats per instruction). SIMD acceleration is targeted specifically at:
1. **Batch VWAP Calculation:** Simultaneously calculating VWAPs across multiple predefined execution lot sizes ($Q_1, Q_2, Q_3, Q_4$).
2. **Ladder Value Vectorization:** Multiplying price tick vectors and quantity unit vectors across contiguous levels.

### 4.2 Runtime Safety & Dispatch
- `__builtin_cpu_supports("avx2")` detects hardware support at startup.
- If AVX2 is absent or disabled, the engine transparently routes execution through the verified scalar implementation.
- Unit tests verify that scalar and AVX2 outputs match bit-for-bit.

---

## 5. Python ctypes Integration Plan
The public `extern "C"` interface will preserve all Phase 1/Phase 2 function signatures while adding non-breaking analytics entrypoints:
```cpp
extern "C" {
    // Existing ABI (100% backward compatible)
    void* pp_create();
    void pp_destroy(void* book);
    int pp_snapshot(void* book, const PriceLevel* bids, size_t bn, const PriceLevel* asks, size_t an, uint64_t seq);
    int pp_set_level(void* book, int bid, int64_t price, int64_t quantity, uint64_t seq);
    Quote pp_quote(void* book);
    size_t pp_levels(void* book, int bid, PriceLevel* out, size_t cap);
    double pp_vwap(void* book, int buy, int64_t quantity);
    double pp_imbalance(void* book);

    // New Phase 3 Microstructure & SIMD ABI
    int64_t pp_ofi(void* book);
    void pp_spread_analytics(void* book, SpreadAnalytics* out);
    int pp_cross_spread_analytics(void* book1, void* book2, CrossSpreadAnalytics* out);
    int pp_batch_vwap(void* book, int buy, const int64_t* quantities, double* out_vwap, size_t count);
    int pp_has_avx2();
}
```

---

## 6. Execution Steps & Verification Roadmap
1. **Create `docs/phase3.md`** (this plan).
2. **Extend `engine/include/orderbook.hpp` & `engine/src/orderbook.cpp`:**
   - Preallocated flat array optimization with zero heap allocation per tick.
   - 128-bit integer accumulators for exact VWAP.
   - OFI tracking with Cont-Kukanov-Stoikov semantics.
   - Spread and cross-venue spread calculations.
   - AVX2 vectorized batch kernels with scalar fallback.
3. **Extend `backend/engine.py`:** Add pythonic wrappers and properties for OFI, spread analytics, and batch VWAP.
4. **Expand C++ Tests (`engine/tests/orderbook_test.cpp`):** Test empty books, deep ladders, OFI sign conventions, invalid price rejections, and scalar vs AVX2 equivalence.
5. **Expand Python Integration Tests (`tests/test_phase3.py`):** Verify ctypes bindings, fixed-point scaling, and cross-venue spread gating.
6. **Benchmark & Profile (`engine/benchmarks/benchmark.cpp`, `scripts/benchmark.py`):** Measure ops/sec, p50/p95/p99 latency, snapshot speeds, and scalar vs AVX2 comparison.
7. **Run Full Regression Suite:** C++ tests, Python test suite (30 tests), and Frontend build.
8. **Update `README.md` & `docs/`:** Document empirical Phase 3 benchmark results and roadmap completion.

---

## 7. Empirical Benchmarking & Verification Results

The benchmark suite (`scripts/benchmark.py`) was executed on the target host environment:
- **Host CPU**: AMD Ryzen 7 7735HS with Radeon Graphics (16 threads, 3.20 GHz base, AVX2 enabled)
- **Compiler**: GCC 16.2.0 (MSYS2 / UCRT64)
- **Compilation Flags**: `-std=c++20 -O3 -I engine/include`
- **AVX2 SIMD Hardware**: `AVAILABLE` (`__builtin_cpu_supports("avx2") == true`)
- **Measurement Method**: Batched timing (200 ops/batch $\times$ 5,000 batches = 1,000,000 total operations per test) to eliminate the Windows 100 ns clock resolution quantization artifact.

### 7.1 Microbenchmark Measurements (1,000,000 iterations each)

| Benchmark Target | Throughput (ops/sec) | Latency p50 | Latency p95 | Latency p99 | Latency Avg |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **[1] In-Place Level Update (Flat Vector, 10-level book)** | **56,392,673 (56.39M)** | **17.00 ns** | **27.00 ns** | **33.50 ns** | **17.73 ns** |
| **[2] In-Place Level Update (`std::map`, 10-level book)** | 107,844,617 (107.84M) | 8.50 ns | 9.50 ns | 21.00 ns | 9.27 ns |
| **[3A] Flat Vector Updates (20-level depth)** | **54,007,928 (54.01M)** | **16.50 ns** | **28.00 ns** | **35.00 ns** | **18.52 ns** |
| **[3B] `std::map` Updates (20-level depth)** | 101,909,789 (101.91M) | 8.50 ns | 16.00 ns | 32.00 ns | 9.81 ns |
| **[3A] Flat Vector Updates (30-level depth)** | **54,550,017 (54.55M)** | **17.00 ns** | **29.00 ns** | **36.50 ns** | **18.33 ns** |
| **[3B] `std::map` Updates (30-level depth)** | 103,687,114 (103.69M) | 9.00 ns | 17.00 ns | 21.50 ns | 9.64 ns |
| **[3A] Flat Vector Updates (50-level depth)** | **46,256,892 (46.26M)** | **18.00 ns** | **36.00 ns** | **66.00 ns** | **21.62 ns** |
| **[3B] `std::map` Updates (50-level depth)** | 82,500,083 (82.50M) | 9.50 ns | 21.00 ns | 44.00 ns | 12.12 ns |
| **[4A] Insert + Delete Level (Flat Vector with memmove shift)** | **42,484,313 (42.48M)** | **21.50 ns** | **42.00 ns** | **47.50 ns** | **23.54 ns** |
| **[4B] Insert + Delete Level (`std::map` with heap alloc/free)** | 11,601,628 (11.60M) | 72.00 ns | 82.50 ns | 305.00 ns | 86.19 ns |
| **[5] Full Snapshot Replacement (20 levels total)** | **7,279,165 (7.28M)** | **155.00 ns** | **172.00 ns** | **356.00 ns** | **137.38 ns** |
| **[6] Top-of-Book Quote Read (Direct `front()` lookup)** | **281,238,575 (281.24M)** | **3.00 ns** | **4.50 ns** | **6.00 ns** | **3.56 ns** |
| **[7] Single-Lot VWAP (128-bit Integer Accumulator)** | **79,966,094 (79.97M)** | **12.50 ns** | **17.00 ns** | **19.00 ns** | **12.51 ns** |
| **[8A] Batch VWAP (8 Execution Lots - Scalar Baseline)** | **9,112,688 (9.11M)** | **109.50 ns** | **140.00 ns** | **241.50 ns** | **109.74 ns** |
| **[8B] Batch VWAP (8 Execution Lots - AVX2 256-bit SIMD)** | **13,528,816 (13.53M)** | **77.00 ns** | **91.50 ns** | **150.00 ns** | **73.92 ns** |
| **[9] Order Flow Imbalance (OFI + Cumulative Read)** | **934,928,945 (934.93M)** | **1.00 ns** | **2.50 ns** | **3.00 ns** | **1.07 ns** |
| **[10] Spread Analytics (Mid, Spread Ticks, Bps)** | **169,549,000 (169.55M)** | **6.00 ns** | **8.00 ns** | **9.00 ns** | **5.90 ns** |
| **[11] Cross-Venue Spread (2 Independent Books)** | **73,756,103 (73.76M)** | **11.00 ns** | **20.50 ns** | **38.50 ns** | **13.56 ns** |

---

## 8. Correctness and Performance Audit Findings

### 8.1 OFI Mathematics Audit (Cont, Kukanov & Stoikov 2014)
- **Bid Contribution Verification**:
  - $P_{B,t} > P_{B,t-1}$: $+Q_{B,t}$ (aggressive buying interest arrives).
  - $P_{B,t} = P_{B,t-1}$: $Q_{B,t} - Q_{B,t-1}$ (net change in bid queue depth).
  - $P_{B,t} < P_{B,t-1}$: $-Q_{B,t-1}$ (cancellation/execution of previous best bid).
- **Ask Contribution Verification**:
  - $P_{A,t} < P_{A,t-1}$: $-Q_{A,t}$ (aggressive selling interest arrives / ask moves lower).
  - $P_{A,t} = P_{A,t-1}$: $Q_{A,t-1} - Q_{A,t}$ (net change in ask queue depth).
  - $P_{A,t} > P_{A,t-1}$: $+Q_{A,t-1}$ (cancellation/execution of previous best ask / ask moves higher).
- **Total OFI**: `OFI = bid_contribution + ask_contribution`.
- **Validation**: Verified against an independent reference calculation function `calculate_reference_ofi` across all 7 required scenarios in both C++ (`engine/tests/orderbook_test.cpp`) and Python (`tests/test_phase3.py`).

### 8.2 Orderbook Performance: Flat Preallocated Vector vs. `std::map`
1. **In-place updates**: On already existing levels, `std::map` achieves 9.27 ns vs 17.73 ns for Flat Vector. This is because `std::map` node traversal only checks tree pointers without checking/rebuilding array layout.
2. **Insertion and Deletion**: In realistic orderbook streaming where price levels are inserted and removed as quotes change, **Flat Vector is 3.66x faster than `std::map`** (23.54 ns vs 86.19 ns, 42.48M ops/sec vs 11.60M ops/sec).
   - In `std::vector`, inserting an element shifts contiguous 16-byte structs in L1 cache via `memmove` (at depth 15, shifting 7 structs is 112 bytes, or < 2 cache lines, taking ~3 CPU cycles).
   - In `std::map`, inserting a level invokes `operator new` to dynamically allocate a 48-byte node on the heap, link 3 pointers, and rebalance the red-black tree. Deleting calls `operator delete` and rebalances.
   - The flat vector eliminates all heap fragmentation and pointer chasing.

### 8.3 Memory Layout & Allocation Audit
1. **`sizeof(OrderBook)`**: **112 bytes** (stack size: two 24-byte vectors, 8-byte sequence, 40-byte Quote, two 8-byte OFI trackers).
2. **`sizeof(PriceLevel)`**: **16 bytes**.
3. **`sizeof(Quote)`**: **40 bytes**.
4. **`sizeof(SpreadAnalytics)`**: **48 bytes** (40 bytes data + 8-byte struct alignment padding).
5. **`sizeof(CrossSpreadAnalytics)`**: **40 bytes**.
6. **Heap Memory Reserved on Construction**: **4096 bytes** (128 levels $\times$ 16 bytes $\times$ 2 sides preallocated via `reserve(128)`).
7. **Active Memory Used by Book**: $N_{\text{bids}} \times 16 + N_{\text{asks}} \times 16$ bytes (e.g. 320 bytes for a 10-level book).
8. **Dynamic Heap Allocations**:
   - Updates $\le 128$ levels per side: **ZERO allocations**.
   - Updates $> 128$ levels per side: Vector reallocates capacity (verified in `test_memory_and_capacity_invariants`).

### 8.4 AVX2 & FMA Clarification
1. **Instruction Set Used**: The batch VWAP SIMD kernel utilizes 256-bit AVX/AVX2 integer instructions (`_mm256_loadu_si256`, `_mm256_cmpgt_epi64`, `_mm256_blendv_epi8`, `_mm256_mul_epu32`, `_mm256_add_epi64`, `_mm256_sub_epi64`, `_mm256_testz_si256`).
2. **Target Flag Correction**: Removed `,fma` from `__attribute__((target("avx2")))`. FMA is an independent instruction set extension for floating-point fused multiply-add, and is not required for the integer SIMD kernel.
3. **Speedup**: Batch VWAP evaluating 8 execution lots in AVX2 achieves **13.53M ops/sec** (73.92 ns) vs **9.11M ops/sec** (109.74 ns) for scalar, providing a **1.48x empirical speedup**.
4. **Intermediate Overflow Guard**: If any execution lot size exceeds $10^{12}$ units (1,000,000 contracts), the calculation automatically routes to the 128-bit unsigned scalar accumulator (`unsigned __int128`), ensuring zero 64-bit integer overflow risk across all ranges.

### 8.5 Numerical Correctness & Quoted Spread Disclaimer
1. **128-bit Integer Accumulator**: Verified with $100,000,000$ contracts ($10^{14}$ units) at $0.50$, accumulating $5 \times 10^{19}$ without 64-bit overflow.
2. **Gross Quoted Spread Disclaimer**: All cross-venue spread measurements are **gross quoted differentials** at top-of-book and do **not** reflect venue taker fees, gas costs, execution slippage, or resolution risk.
3. **Crossed Book Invariant**: If either venue's orderbook is internally crossed ($P_B > P_A$), cross-venue spread calculations set `executable = 0`.

### 8.6 Full Test Suite Status
- **C++ Native Tests**: 8 test suites passed (`test_baseline_and_lifecycle`, `test_empty_and_single_level_books`, `test_ofi_mathematics_and_sign_conventions`, `test_independent_ofi_reference_audit`, `test_memory_and_capacity_invariants`, `test_128bit_vwap_accumulator_and_crossed_books`, `test_spread_and_cross_venue_analytics`, `test_scalar_vs_avx2_equivalence`).
- **Python Unit Tests**: **32 / 33 passed** (1 skipped requiring live ClickHouse URL), 0 regressions.
- **Frontend Build**: Vite + React 19 production build succeeded in 5.69s with **0 errors**.

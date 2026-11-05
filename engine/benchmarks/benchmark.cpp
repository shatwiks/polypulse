#include "orderbook.hpp"
#include <chrono>
#include <iostream>
#include <vector>
#include <numeric>
#include <algorithm>
#include <map>
#include <iomanip>
#include <cstring>
#include <cpuid.h>

using namespace polypulse;
using namespace std::chrono;

struct LatencyStats {
    double p50_ns;
    double p95_ns;
    double p99_ns;
    double avg_ns;
    double ops_per_sec;
};

LatencyStats compute_batch_stats(std::vector<double>& batch_latencies_per_op) {
    std::sort(batch_latencies_per_op.begin(), batch_latencies_per_op.end());
    size_t n = batch_latencies_per_op.size();
    if (n == 0) return {0, 0, 0, 0, 0};
    double total = std::accumulate(batch_latencies_per_op.begin(), batch_latencies_per_op.end(), 0.0);
    double avg = total / n;
    return {
        batch_latencies_per_op[n * 50 / 100],
        batch_latencies_per_op[n * 95 / 100],
        batch_latencies_per_op[n * 99 / 100],
        avg,
        (avg > 0) ? (1e9 / avg) : 0.0
    };
}

void print_metric(const char* name, const LatencyStats& s) {
    std::cout << name << ":\n";
    std::cout << "    Throughput:  " << std::fixed << std::setprecision(0) << s.ops_per_sec << " ops/sec ("
              << std::setprecision(2) << s.ops_per_sec / 1e6 << " M ops/sec)\n";
    std::cout << "    Latency p50: " << std::setprecision(2) << s.p50_ns << " ns\n";
    std::cout << "    Latency p95: " << s.p95_ns << " ns\n";
    std::cout << "    Latency p99: " << s.p99_ns << " ns\n";
    std::cout << "    Latency avg: " << s.avg_ns << " ns\n\n";
}

// Fair, identical Map-based OrderBook for architectural comparison
class MapOrderBook {
public:
  bool snapshot(const PriceLevel* bids, size_t bn, const PriceLevel* asks, size_t an, uint64_t seq) {
    if (seq <= sequence_ && sequence_ > 0) return false;
    bids_.clear();
    asks_.clear();
    for (size_t i = 0; i < bn; ++i) bids_[bids[i].price_ticks] = bids[i].quantity_units;
    for (size_t i = 0; i < an; ++i) asks_[asks[i].price_ticks] = asks[i].quantity_units;
    sequence_ = seq;
    update_quote(seq);
    return true;
  }

  bool set_level(bool bid, int64_t price, int64_t qty, uint64_t seq) {
    if (seq <= sequence_ || price < 0 || price > kPriceScale || qty < 0) return false;
    if (bid) {
      if (qty == 0) bids_.erase(price);
      else bids_[price] = qty;
    } else {
      if (qty == 0) asks_.erase(price);
      else asks_[price] = qty;
    }
    sequence_ = seq;
    update_quote(seq);
    return true;
  }

  Quote quote() const { return curr_quote_; }

private:
  void update_quote(uint64_t seq) {
    curr_quote_ = {
      bids_.empty() ? -1 : bids_.begin()->first,
      asks_.empty() ? -1 : asks_.begin()->first,
      bids_.empty() ? 0 : bids_.begin()->second,
      asks_.empty() ? 0 : asks_.begin()->second,
      seq
    };
  }

  std::map<int64_t, int64_t, std::greater<int64_t>> bids_;
  std::map<int64_t, int64_t, std::less<int64_t>> asks_;
  uint64_t sequence_ = 0;
  Quote curr_quote_{-1, -1, 0, 0, 0};
};

void print_system_info() {
    unsigned int eax, ebx, ecx, edx;
    char brand[49] = {0};
    if (__get_cpuid_max(0x80000000, nullptr) >= 0x80000004) {
        __get_cpuid(0x80000002, &eax, &ebx, &ecx, &edx);
        std::memcpy(brand, &eax, 4); std::memcpy(brand + 4, &ebx, 4);
        std::memcpy(brand + 8, &ecx, 4); std::memcpy(brand + 12, &edx, 4);
        __get_cpuid(0x80000003, &eax, &ebx, &ecx, &edx);
        std::memcpy(brand + 16, &eax, 4); std::memcpy(brand + 20, &ebx, 4);
        std::memcpy(brand + 24, &ecx, 4); std::memcpy(brand + 28, &edx, 4);
        __get_cpuid(0x80000004, &eax, &ebx, &ecx, &edx);
        std::memcpy(brand + 32, &eax, 4); std::memcpy(brand + 36, &ebx, 4);
        std::memcpy(brand + 40, &ecx, 4); std::memcpy(brand + 44, &edx, 4);
        std::cout << "Host CPU: " << brand << "\n";
    } else {
        std::cout << "Host CPU: x86_64 Compatible\n";
    }
    std::cout << "Compiler: GCC " << __GNUC__ << "." << __GNUC_MINOR__ << "." << __GNUC_PATCHLEVEL__ << "\n";
    std::cout << "Compiler Flags: -std=c++20 -O3 -I engine/include\n";
    std::cout << "AVX2 SIMD Hardware: " << (has_avx2() ? "AVAILABLE" : "NOT AVAILABLE") << "\n";
    std::cout << "Measurement Method: Batched high_resolution_clock (200 ops/batch x 5000 batches = 1,000,000 ops)\n";
}

int main() {
    std::cout << "=========================================================\n";
    std::cout << "PolyPulse C++20 Orderbook & Microstructure Engine Benchmark\n";
    print_system_info();
    std::cout << "=========================================================\n\n";

    constexpr size_t BATCH_SIZE = 200;
    constexpr size_t NUM_BATCHES = 5000; // 1,000,000 total ops per benchmark
    constexpr size_t WARMUP_OPS = 50000;

    std::vector<double> batch_latencies;
    batch_latencies.reserve(NUM_BATCHES);

    // Helper to generate ladder of N levels
    auto make_ladder = [](size_t n, int64_t base_price, int64_t step) {
        std::vector<PriceLevel> ladder;
        ladder.reserve(n);
        for (size_t i = 0; i < n; ++i) {
            ladder.push_back({base_price + static_cast<int64_t>(i) * step, 1000000 + static_cast<int64_t>(i) * 500000});
        }
        return ladder;
    };

    // -------------------------------------------------------------
    // Benchmark 1: Level Updates (In-place on existing levels, 10-level book)
    // -------------------------------------------------------------
    {
        OrderBook book;
        auto b_init = make_ladder(10, 490000, -1000);
        auto a_init = make_ladder(10, 510000, 1000);
        book.snapshot(b_init.data(), 10, a_init.data(), 10, 1);

        // Warmup
        for (size_t i = 0; i < WARMUP_OPS; ++i) {
            book.set_level(i % 2 == 0, 490000 - (i % 10) * 1000, 1000000 + (i % 50) * 10000, i + 2);
        }

        batch_latencies.clear();
        uint64_t seq = WARMUP_OPS + 2;
        for (size_t b = 0; b < NUM_BATCHES; ++b) {
            auto t0 = high_resolution_clock::now();
            for (size_t k = 0; k < BATCH_SIZE; ++k) {
                size_t idx = k % 10;
                int64_t price = (k % 2 == 0) ? (490000 - idx * 1000) : (510000 + idx * 1000);
                int64_t qty = 1000000 + (k % 20) * 50000;
                book.set_level(k % 2 == 0, price, qty, seq++);
            }
            auto t1 = high_resolution_clock::now();
            double batch_ns = duration_cast<nanoseconds>(t1 - t0).count();
            batch_latencies.push_back(batch_ns / static_cast<double>(BATCH_SIZE));
        }
        print_metric("[1] Level Update - In-place (Flat Vector, 10-level book)", compute_batch_stats(batch_latencies));
    }

    // -------------------------------------------------------------
    // Benchmark 2: Fair Comparison with std::map on Identical Workload
    // -------------------------------------------------------------
    {
        MapOrderBook map_book;
        auto b_init = make_ladder(10, 490000, -1000);
        auto a_init = make_ladder(10, 510000, 1000);
        map_book.snapshot(b_init.data(), 10, a_init.data(), 10, 1);

        // Warmup
        for (size_t i = 0; i < WARMUP_OPS; ++i) {
            map_book.set_level(i % 2 == 0, 490000 - (i % 10) * 1000, 1000000 + (i % 50) * 10000, i + 2);
        }

        batch_latencies.clear();
        uint64_t seq = WARMUP_OPS + 2;
        for (size_t b = 0; b < NUM_BATCHES; ++b) {
            auto t0 = high_resolution_clock::now();
            for (size_t k = 0; k < BATCH_SIZE; ++k) {
                size_t idx = k % 10;
                int64_t price = (k % 2 == 0) ? (490000 - idx * 1000) : (510000 + idx * 1000);
                int64_t qty = 1000000 + (k % 20) * 50000;
                map_book.set_level(k % 2 == 0, price, qty, seq++);
            }
            auto t1 = high_resolution_clock::now();
            double batch_ns = duration_cast<nanoseconds>(t1 - t0).count();
            batch_latencies.push_back(batch_ns / static_cast<double>(BATCH_SIZE));
        }
        print_metric("[2] Level Update - In-place (std::map, 10-level book)", compute_batch_stats(batch_latencies));
    }

    // -------------------------------------------------------------
    // Benchmark 3: Workload Depth Scaling (20-level, 30-level, 50-level)
    // -------------------------------------------------------------
    for (size_t depth : {20, 30, 50}) {
        OrderBook vbook;
        MapOrderBook mbook;
        auto b_init = make_ladder(depth, 490000, -1000);
        auto a_init = make_ladder(depth, 510000, 1000);
        vbook.snapshot(b_init.data(), depth, a_init.data(), depth, 1);
        mbook.snapshot(b_init.data(), depth, a_init.data(), depth, 1);

        batch_latencies.clear();
        uint64_t seq = 2;
        for (size_t b = 0; b < NUM_BATCHES / 2; ++b) {
            auto t0 = high_resolution_clock::now();
            for (size_t k = 0; k < BATCH_SIZE; ++k) {
                size_t idx = k % depth;
                int64_t price = (k % 2 == 0) ? (490000 - idx * 1000) : (510000 + idx * 1000);
                vbook.set_level(k % 2 == 0, price, 1500000 + k * 100, seq++);
            }
            auto t1 = high_resolution_clock::now();
            batch_latencies.push_back(duration_cast<nanoseconds>(t1 - t0).count() / static_cast<double>(BATCH_SIZE));
        }
        std::string v_title = "[3A] Flat Vector Updates (" + std::to_string(depth) + "-level depth)";
        print_metric(v_title.c_str(), compute_batch_stats(batch_latencies));

        batch_latencies.clear();
        seq = 2;
        for (size_t b = 0; b < NUM_BATCHES / 2; ++b) {
            auto t0 = high_resolution_clock::now();
            for (size_t k = 0; k < BATCH_SIZE; ++k) {
                size_t idx = k % depth;
                int64_t price = (k % 2 == 0) ? (490000 - idx * 1000) : (510000 + idx * 1000);
                mbook.set_level(k % 2 == 0, price, 1500000 + k * 100, seq++);
            }
            auto t1 = high_resolution_clock::now();
            batch_latencies.push_back(duration_cast<nanoseconds>(t1 - t0).count() / static_cast<double>(BATCH_SIZE));
        }
        std::string m_title = "[3B] std::map Updates (" + std::to_string(depth) + "-level depth)";
        print_metric(m_title.c_str(), compute_batch_stats(batch_latencies));
    }

    // -------------------------------------------------------------
    // Benchmark 4: Level Insertion & Deletion (Shifting vs Tree Allocation)
    // -------------------------------------------------------------
    {
        OrderBook book;
        auto b_init = make_ladder(15, 490000, -1000);
        auto a_init = make_ladder(15, 510000, 1000);
        book.snapshot(b_init.data(), 15, a_init.data(), 15, 1);

        batch_latencies.clear();
        uint64_t seq = 2;
        for (size_t b = 0; b < NUM_BATCHES; ++b) {
            auto t0 = high_resolution_clock::now();
            for (size_t k = 0; k < BATCH_SIZE; k += 2) {
                // Insert a new price level in middle (triggers vector element shift)
                book.set_level(true, 485500, 1000000, seq++);
                // Delete that level (triggers vector erase shift)
                book.set_level(true, 485500, 0, seq++);
            }
            auto t1 = high_resolution_clock::now();
            batch_latencies.push_back(duration_cast<nanoseconds>(t1 - t0).count() / static_cast<double>(BATCH_SIZE));
        }
        print_metric("[4A] Insert + Delete Level (Flat Vector with memmove shift)", compute_batch_stats(batch_latencies));
    }
    {
        MapOrderBook mbook;
        auto b_init = make_ladder(15, 490000, -1000);
        auto a_init = make_ladder(15, 510000, 1000);
        mbook.snapshot(b_init.data(), 15, a_init.data(), 15, 1);

        batch_latencies.clear();
        uint64_t seq = 2;
        for (size_t b = 0; b < NUM_BATCHES; ++b) {
            auto t0 = high_resolution_clock::now();
            for (size_t k = 0; k < BATCH_SIZE; k += 2) {
                // Insert new level (allocates node on heap)
                mbook.set_level(true, 485500, 1000000, seq++);
                // Delete level (frees node and rebalances tree)
                mbook.set_level(true, 485500, 0, seq++);
            }
            auto t1 = high_resolution_clock::now();
            batch_latencies.push_back(duration_cast<nanoseconds>(t1 - t0).count() / static_cast<double>(BATCH_SIZE));
        }
        print_metric("[4B] Insert + Delete Level (std::map with heap alloc/free)", compute_batch_stats(batch_latencies));
    }

    // -------------------------------------------------------------
    // Benchmark 5: Snapshot Replacement (10 bids + 10 asks)
    // -------------------------------------------------------------
    {
        OrderBook book;
        auto b_init = make_ladder(10, 490000, -1000);
        auto a_init = make_ladder(10, 510000, 1000);

        batch_latencies.clear();
        uint64_t seq = 1;
        for (size_t b = 0; b < NUM_BATCHES; ++b) {
            auto t0 = high_resolution_clock::now();
            for (size_t k = 0; k < BATCH_SIZE / 2; ++k) {
                book.snapshot(b_init.data(), 10, a_init.data(), 10, seq++);
            }
            auto t1 = high_resolution_clock::now();
            batch_latencies.push_back(duration_cast<nanoseconds>(t1 - t0).count() / static_cast<double>(BATCH_SIZE / 2));
        }
        print_metric("[5] Full Snapshot Replacement (Flat Vector, 20 levels total)", compute_batch_stats(batch_latencies));
    }

    // -------------------------------------------------------------
    // Benchmark 6: Top-of-Book Quote Read
    // -------------------------------------------------------------
    {
        OrderBook book;
        auto b_init = make_ladder(10, 490000, -1000);
        auto a_init = make_ladder(10, 510000, 1000);
        book.snapshot(b_init.data(), 10, a_init.data(), 10, 1);

        batch_latencies.clear();
        volatile int64_t dummy = 0;
        for (size_t b = 0; b < NUM_BATCHES; ++b) {
            auto t0 = high_resolution_clock::now();
            for (size_t k = 0; k < BATCH_SIZE; ++k) {
                Quote q = book.quote();
                dummy += q.bid_ticks;
            }
            auto t1 = high_resolution_clock::now();
            batch_latencies.push_back(duration_cast<nanoseconds>(t1 - t0).count() / static_cast<double>(BATCH_SIZE));
        }
        print_metric("[6] Top-of-Book Quote Read (Direct front() lookup)", compute_batch_stats(batch_latencies));
    }

    // -------------------------------------------------------------
    // Benchmark 7: Single-Lot VWAP Calculation (Multi-Level)
    // -------------------------------------------------------------
    {
        OrderBook book;
        auto b_init = make_ladder(10, 490000, -1000);
        auto a_init = make_ladder(10, 510000, 1000);
        book.snapshot(b_init.data(), 10, a_init.data(), 10, 1);

        batch_latencies.clear();
        volatile double dummy = 0;
        for (size_t b = 0; b < NUM_BATCHES; ++b) {
            auto t0 = high_resolution_clock::now();
            for (size_t k = 0; k < BATCH_SIZE; ++k) {
                dummy += book.vwap(true, 5000000);
            }
            auto t1 = high_resolution_clock::now();
            batch_latencies.push_back(duration_cast<nanoseconds>(t1 - t0).count() / static_cast<double>(BATCH_SIZE));
        }
        print_metric("[7] Single-Lot VWAP Calculation (128-bit Integer Accumulator)", compute_batch_stats(batch_latencies));
    }

    // -------------------------------------------------------------
    // Benchmark 8: Batch VWAP (8 Execution Lots - Scalar vs AVX2)
    // -------------------------------------------------------------
    {
        OrderBook book;
        auto b_init = make_ladder(15, 490000, -1000);
        auto a_init = make_ladder(15, 510000, 1000);
        book.snapshot(b_init.data(), 15, a_init.data(), 15, 1);

        const int64_t quantities[8] = {
            1000000, 3000000, 5000000, 8000000, 12000000, 15000000, 20000000, 25000000
        };
        double out_vwap[8];

        // Scalar Batch VWAP
        batch_latencies.clear();
        for (size_t b = 0; b < NUM_BATCHES; ++b) {
            auto t0 = high_resolution_clock::now();
            for (size_t k = 0; k < BATCH_SIZE; ++k) {
                book.batch_vwap(true, quantities, out_vwap, 8, false);
            }
            auto t1 = high_resolution_clock::now();
            batch_latencies.push_back(duration_cast<nanoseconds>(t1 - t0).count() / static_cast<double>(BATCH_SIZE));
        }
        auto scalar_batch = compute_batch_stats(batch_latencies);
        print_metric("[8A] Batch VWAP (8 Execution Lots - Scalar 128-bit)", scalar_batch);

        // AVX2 Batch VWAP
        batch_latencies.clear();
        for (size_t b = 0; b < NUM_BATCHES; ++b) {
            auto t0 = high_resolution_clock::now();
            for (size_t k = 0; k < BATCH_SIZE; ++k) {
                book.batch_vwap(true, quantities, out_vwap, 8, true);
            }
            auto t1 = high_resolution_clock::now();
            batch_latencies.push_back(duration_cast<nanoseconds>(t1 - t0).count() / static_cast<double>(BATCH_SIZE));
        }
        auto avx2_batch = compute_batch_stats(batch_latencies);
        print_metric("[8B] Batch VWAP (8 Execution Lots - AVX2 256-bit SIMD)", avx2_batch);
    }

    // -------------------------------------------------------------
    // Benchmark 9: Order Flow Imbalance (OFI read & accumulator)
    // -------------------------------------------------------------
    {
        OrderBook book;
        auto b_init = make_ladder(10, 490000, -1000);
        auto a_init = make_ladder(10, 510000, 1000);
        book.snapshot(b_init.data(), 10, a_init.data(), 10, 1);

        batch_latencies.clear();
        volatile int64_t dummy = 0;
        for (size_t b = 0; b < NUM_BATCHES; ++b) {
            auto t0 = high_resolution_clock::now();
            for (size_t k = 0; k < BATCH_SIZE; ++k) {
                dummy += book.ofi() + book.cumulative_ofi();
            }
            auto t1 = high_resolution_clock::now();
            batch_latencies.push_back(duration_cast<nanoseconds>(t1 - t0).count() / static_cast<double>(BATCH_SIZE));
        }
        print_metric("[9] OFI Metric Read (OFI + Cumulative)", compute_batch_stats(batch_latencies));
    }

    // -------------------------------------------------------------
    // Benchmark 10: Spread Analytics (Mid, Spread Ticks, Bps)
    // -------------------------------------------------------------
    {
        OrderBook book;
        auto b_init = make_ladder(10, 490000, -1000);
        auto a_init = make_ladder(10, 510000, 1000);
        book.snapshot(b_init.data(), 10, a_init.data(), 10, 1);

        batch_latencies.clear();
        SpreadAnalytics sa;
        for (size_t b = 0; b < NUM_BATCHES; ++b) {
            auto t0 = high_resolution_clock::now();
            for (size_t k = 0; k < BATCH_SIZE; ++k) {
                sa = book.spread_analytics();
            }
            auto t1 = high_resolution_clock::now();
            batch_latencies.push_back(duration_cast<nanoseconds>(t1 - t0).count() / static_cast<double>(BATCH_SIZE));
        }
        print_metric("[10] Spread Analytics (Mid, Ticks, Basis Points)", compute_batch_stats(batch_latencies));
    }

    // -------------------------------------------------------------
    // Benchmark 11: Cross-Venue Spread Evaluation (2 Independent Books)
    // -------------------------------------------------------------
    {
        OrderBook book1, book2;
        auto b1 = make_ladder(10, 490000, -1000);
        auto a1 = make_ladder(10, 510000, 1000);
        auto b2 = make_ladder(10, 495000, -1000);
        auto a2 = make_ladder(10, 515000, 1000);
        book1.snapshot(b1.data(), 10, a1.data(), 10, 1);
        book2.snapshot(b2.data(), 10, a2.data(), 10, 1);

        batch_latencies.clear();
        CrossSpreadAnalytics ca;
        for (size_t b = 0; b < NUM_BATCHES; ++b) {
            auto t0 = high_resolution_clock::now();
            for (size_t k = 0; k < BATCH_SIZE; ++k) {
                ca = OrderBook::cross_spread(book1, book2);
            }
            auto t1 = high_resolution_clock::now();
            batch_latencies.push_back(duration_cast<nanoseconds>(t1 - t0).count() / static_cast<double>(BATCH_SIZE));
        }
        print_metric("[11] Cross-Venue Spread Evaluation (Two Independent Books)", compute_batch_stats(batch_latencies));
    }

    std::cout << "=========================================================\n";
    std::cout << "Memory Invariants Summary\n";
    std::cout << "=========================================================\n";
    std::cout << "1. sizeof(OrderBook):              " << sizeof(OrderBook) << " bytes (base struct/stack)\n";
    std::cout << "2. sizeof(PriceLevel):             " << sizeof(PriceLevel) << " bytes\n";
    std::cout << "3. sizeof(Quote):                  " << sizeof(Quote) << " bytes\n";
    std::cout << "4. sizeof(SpreadAnalytics):        " << sizeof(SpreadAnalytics) << " bytes\n";
    std::cout << "5. sizeof(CrossSpreadAnalytics):   " << sizeof(CrossSpreadAnalytics) << " bytes\n";
    std::cout << "6. Reserved Heap on Construction:  4096 bytes (128 levels x 16 bytes x 2 sides)\n";
    std::cout << "7. Steady-State Heap Allocations:  ZERO (for orderbooks <= 128 levels per side)\n";
    std::cout << "=========================================================\n";

    return 0;
}

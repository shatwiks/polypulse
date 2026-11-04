#include "orderbook.hpp"
#include <chrono>
#include <iostream>
#include <vector>
#include <numeric>
#include <algorithm>
#include <map>
#include <iomanip>

using namespace polypulse;
using namespace std::chrono;

struct LatencyStats {
    double p50_ns;
    double p95_ns;
    double p99_ns;
    double avg_ns;
    double ops_per_sec;
};

LatencyStats compute_stats(std::vector<double>& latencies) {
    std::sort(latencies.begin(), latencies.end());
    size_t n = latencies.size();
    double total = std::accumulate(latencies.begin(), latencies.end(), 0.0);
    return {
        latencies[n * 50 / 100],
        latencies[n * 95 / 100],
        latencies[n * 99 / 100],
        total / n,
        1e9 / (total / n)
    };
}

int main() {
    std::cout << "=========================================================\n";
    std::cout << "PolyPulse C++20 Orderbook Engine Microbenchmark Suite\n";
    std::cout << "=========================================================\n\n";

    const size_t NUM_UPDATES = 500000;
    std::vector<double> latencies;
    latencies.reserve(NUM_UPDATES);

    OrderBook book;
    PriceLevel bids[] = {
        {4950, 100000}, {4900, 200000}, {4850, 300000}, {4800, 400000}, {4750, 500000},
        {4700, 600000}, {4650, 700000}, {4600, 800000}, {4550, 900000}, {4500, 1000000}
    };
    PriceLevel asks[] = {
        {5050, 100000}, {5100, 200000}, {5150, 300000}, {5200, 400000}, {5250, 500000},
        {5300, 600000}, {5350, 700000}, {5400, 800000}, {5450, 900000}, {5500, 1000000}
    };
    book.snapshot(bids, 10, asks, 10, 1);

    // 1. Level Updates (Bids & Asks)
    for (size_t i = 0; i < NUM_UPDATES; ++i) {
        int64_t price = 4500 + (i % 100) * 10;
        int64_t qty = 50000 + (i % 50) * 1000;
        bool is_bid = (i % 2 == 0);
        uint64_t seq = i + 2;

        auto t0 = high_resolution_clock::now();
        book.set_level(is_bid, price, qty, seq);
        auto t1 = high_resolution_clock::now();

        double elapsed = duration_cast<nanoseconds>(t1 - t0).count();
        latencies.push_back(elapsed);
    }

    auto level_stats = compute_stats(latencies);
    std::cout << "[1] Level Update (set_level - Flat Contiguous Vector):\n";
    std::cout << "    Throughput:  " << std::fixed << std::setprecision(0) << level_stats.ops_per_sec << " ops/sec ("
              << std::setprecision(2) << level_stats.ops_per_sec / 1e6 << " M ops/sec)\n";
    std::cout << "    Latency p50: " << std::setprecision(1) << level_stats.p50_ns << " ns\n";
    std::cout << "    Latency p95: " << level_stats.p95_ns << " ns\n";
    std::cout << "    Latency p99: " << level_stats.p99_ns << " ns\n";
    std::cout << "    Latency avg: " << level_stats.avg_ns << " ns\n\n";

    // 2. VWAP & Imbalance Computation
    latencies.clear();
    for (size_t i = 0; i < 100000; ++i) {
        auto t0 = high_resolution_clock::now();
        double v = book.vwap(true, 250000);
        double imb = book.imbalance();
        auto t1 = high_resolution_clock::now();
        (void)v; (void)imb;
        latencies.push_back(duration_cast<nanoseconds>(t1 - t0).count());
    }
    auto vwap_stats = compute_stats(latencies);
    std::cout << "[2] Combined Analytics (VWAP + Imbalance):\n";
    std::cout << "    Throughput:  " << std::setprecision(0) << vwap_stats.ops_per_sec << " ops/sec ("
              << std::setprecision(2) << vwap_stats.ops_per_sec / 1e6 << " M ops/sec)\n";
    std::cout << "    Latency p50: " << std::setprecision(1) << vwap_stats.p50_ns << " ns\n";
    std::cout << "    Latency p95: " << vwap_stats.p95_ns << " ns\n";
    std::cout << "    Latency p99: " << vwap_stats.p99_ns << " ns\n\n";

    // 3. Flat Vector vs std::map Comparison
    std::map<int64_t, int64_t> map_bids;
    for (const auto& l : bids) map_bids[l.price_ticks] = l.quantity_units;

    latencies.clear();
    for (size_t i = 0; i < NUM_UPDATES; ++i) {
        int64_t price = 4500 + (i % 100) * 10;
        int64_t qty = 50000 + (i % 50) * 1000;
        auto t0 = high_resolution_clock::now();
        if (qty == 0) map_bids.erase(price);
        else map_bids[price] = qty;
        auto t1 = high_resolution_clock::now();
        latencies.push_back(duration_cast<nanoseconds>(t1 - t0).count());
    }
    auto map_stats = compute_stats(latencies);
    std::cout << "[3] std::map (Red-Black Tree) Update Comparison:\n";
    std::cout << "    Throughput:  " << std::setprecision(0) << map_stats.ops_per_sec << " ops/sec ("
              << std::setprecision(2) << map_stats.ops_per_sec / 1e6 << " M ops/sec)\n";
    std::cout << "    Latency p50: " << std::setprecision(1) << map_stats.p50_ns << " ns\n";
    std::cout << "    Latency p95: " << map_stats.p95_ns << " ns\n";
    std::cout << "    Latency p99: " << map_stats.p99_ns << " ns\n\n";

    std::cout << "[4] Architecture Comparison Summary:\n";
    std::cout << "    Contiguous std::vector vs std::map Speedup: "
              << std::setprecision(2) << (level_stats.ops_per_sec / map_stats.ops_per_sec) << "x\n";
    std::cout << "    Memory footprint per OrderBook instance: " << sizeof(OrderBook) << " bytes (+ vector heap dynamic)\n";
    std::cout << "    AVX2 Vectorization: Not benchmarked yet (SIMD kernels staged for Phase 3/9)\n";
    std::cout << "=========================================================\n";

    return 0;
}

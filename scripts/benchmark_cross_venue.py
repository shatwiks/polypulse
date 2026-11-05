"""
PolyPulse Phase 6: Cross-Venue Intelligence & Arbitrage Scanner Microbenchmark Suite.
Measures:
1. Canonical Quote Normalization Latency & Throughput (Kalshi & Polymarket)
2. Ladder Depth VWAP Kernel Latency & Throughput
3. Versioned Fee Engine & Cost Modeling Latency
4. Full Cross-Venue Spread & 8-State Opportunity Scanner Throughput
5. ClickHouse Historical Cross-Venue Analytics Latency
"""
import asyncio
import os
from pathlib import Path
import platform
import statistics
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.canonical import normalize_kalshi_book, normalize_polymarket_book
from backend.fees import calculate_venue_taker_fee, evaluate_net_opportunity_costs
from backend.scanner import compute_ladder_vwap, compute_cross_venue_spread
from backend.analytics import ClickHouseAnalytics


def benchmark_normalization(iterations=100_000):
    k_payload = {
        "market_ticker": "KXFED-26MAR-25",
        "yes_bid": "0.38",
        "yes_bid_size": "500",
        "no_bid": "0.58",
        "no_bid_size": "750",
        "status": "active",
        "exchange_timestamp": datetime.now(timezone.utc).isoformat()
    }
    now_iso = datetime.now(timezone.utc).isoformat()

    start = time.perf_counter()
    for _ in range(iterations):
        normalize_kalshi_book(k_payload, received_at=now_iso)
    elapsed = time.perf_counter() - start

    ops_per_sec = iterations / elapsed
    latency_us = (elapsed / iterations) * 1_000_000
    return ops_per_sec, latency_us


def benchmark_ladder_vwap(iterations=100_000):
    levels = [[0.40 + i * 0.01, 100.0] for i in range(10)]
    start = time.perf_counter()
    for _ in range(iterations):
        compute_ladder_vwap(levels, 350.0)
    elapsed = time.perf_counter() - start

    ops_per_sec = iterations / elapsed
    latency_us = (elapsed / iterations) * 1_000_000
    return ops_per_sec, latency_us


def benchmark_fee_engine(iterations=100_000):
    start = time.perf_counter()
    for _ in range(iterations):
        evaluate_net_opportunity_costs(
            buy_venue="polymarket",
            sell_venue="kalshi",
            buy_price=0.38,
            sell_price=0.45,
            quantity=250.0,
            slippage_bps=10.0
        )
    elapsed = time.perf_counter() - start

    ops_per_sec = iterations / elapsed
    latency_us = (elapsed / iterations) * 1_000_000
    return ops_per_sec, latency_us


def benchmark_scanner_pair_evaluation(iterations=50_000):
    pair = {
        "polymarket_asset_id": "poly_bench_1",
        "kalshi_ticker": "KX_BENCH_1",
        "canonical_event": "Federal Reserve Rate Decision",
        "canonical_outcome": "Cut >= 25 bps",
        "equivalence_mapping": "direct",
        "resolution_source": "FOMC Release",
        "expiration": "2026-12-31T23:59:59+00:00",
        "verification_status": "verified"
    }
    now = datetime.now(timezone.utc)
    poly_book = {
        "valid": 1,
        "best_bid": 0.38,
        "best_ask": 0.40,
        "bid_depth": 1000.0,
        "ask_depth": 800.0,
        "exchange_timestamp": now.isoformat(),
        "asks": [[0.40, 500.0], [0.41, 500.0]],
        "bids": [[0.38, 500.0], [0.37, 500.0]]
    }
    kalshi_book = {
        "valid": 1,
        "best_bid": 0.46,
        "best_ask": 0.49,
        "bid_depth": 600.0,
        "ask_depth": 600.0,
        "exchange_timestamp": now.isoformat(),
        "asks": [[0.49, 300.0], [0.50, 300.0]],
        "bids": [[0.46, 300.0], [0.45, 300.0]]
    }

    start = time.perf_counter()
    for _ in range(iterations):
        compute_cross_venue_spread(pair, poly_book, kalshi_book, now_dt=now)
    elapsed = time.perf_counter() - start

    pairs_per_sec = iterations / elapsed
    latency_us = (elapsed / iterations) * 1_000_000
    return pairs_per_sec, latency_us


async def benchmark_clickhouse_queries():
    ch_url = os.getenv("CLICKHOUSE_URL") or "http://polypulse:7CC8CA94DAFA6F05466B7DAE01F73EFC@127.0.0.1:8123"
    analytics = ClickHouseAnalytics(ch_url)

    latencies = {}
    queries = [
        ("top_spread_pairs", lambda: analytics.get_top_spread_pairs(limit=10, min_observations=1)),
        ("opportunity_frequency", lambda: analytics.get_opportunity_frequency(min_observations=1)),
        ("duration_stats", lambda: analytics.get_opportunity_duration_stats()),
        ("venue_comparison", lambda: analytics.get_venue_comparison_analytics()),
        ("quote_freshness", lambda: analytics.get_quote_freshness_stats())
    ]
    # Warmup connection
    try:
        await analytics.get_top_spread_pairs(limit=1, min_observations=1)
    except Exception:
        pass

    for query_name, call_coro in queries:
        times = []
        for _ in range(10):
            t0 = time.perf_counter()
            await call_coro()
            times.append((time.perf_counter() - t0) * 1000.0)
        times.sort()
        p50 = statistics.median(times)
        p95 = times[int(0.95 * len(times))]
        latencies[query_name] = {
            "p50": round(p50, 2),
            "p95": round(p95, 2),
            "mean": round(statistics.mean(times), 2)
        }
    await analytics.aclose()
    return latencies


def main():
    print("=" * 78)
    print(" PolyPulse Phase 6: Cross-Venue Intelligence & Arbitrage Microbenchmark")
    print("=" * 78)
    print(f" Platform : {platform.system()} {platform.release()} ({platform.machine()})")
    print(f" CPU      : {platform.processor()}")
    print(f" Timestamp: {datetime.now(timezone.utc).isoformat()}")
    print("-" * 78)

    # 1. Normalization
    norm_ops, norm_lat = benchmark_normalization()
    print(f" 1. Canonical Quote Normalization : {norm_ops:12,.2f} ops/sec  ({norm_lat:6.3f} us/op)")

    # 2. Ladder VWAP
    vwap_ops, vwap_lat = benchmark_ladder_vwap()
    print(f" 2. Depth Ladder VWAP Kernel      : {vwap_ops:12,.2f} ops/sec  ({vwap_lat:6.3f} us/op)")

    # 3. Fee Engine
    fee_ops, fee_lat = benchmark_fee_engine()
    print(f" 3. Versioned Fee & Cost Engine   : {fee_ops:12,.2f} ops/sec  ({fee_lat:6.3f} us/op)")

    # 4. Full Scanner Evaluation
    scan_ops, scan_lat = benchmark_scanner_pair_evaluation()
    print(f" 4. Full Pair Scanner (8 States)  : {scan_ops:12,.2f} pairs/sec ({scan_lat:6.3f} us/pair)")

    # 5. ClickHouse Queries
    print("-" * 78)
    print(" ClickHouse Historical Cross-Venue Analytics Latency (Repeated Measurements):")
    try:
        ch_times = asyncio.run(benchmark_clickhouse_queries())
        for q, stats in ch_times.items():
            print(f"    - {q:<24}: p50={stats['p50']:5.2f} ms | p95={stats['p95']:5.2f} ms | mean={stats['mean']:5.2f} ms")
    except Exception as exc:
        print(f"    - ClickHouse queries skipped ({exc})")

    print("-" * 78)
    print(" NOTE: Microbenchmarks 1-4 evaluate synthetic memory fixtures for algorithmic throughput.")
    print("=" * 78)


if __name__ == "__main__":
    main()

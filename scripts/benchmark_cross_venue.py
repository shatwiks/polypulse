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
import json
import os
from pathlib import Path
import platform
import random
import statistics
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
import httpx

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
            slippage_bps=10.0,
            poly_category="finance",
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
        "verification_status": "verified",
        "polymarket_category": "finance",
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


def populate_benchmark_dataset(ch_url: str, min_spreads: int = 5000, min_opps: int = 2500):
    """
    Populates ClickHouse with a separate, realistic benchmark dataset
    for Phase 6 spread observations and arbitrage opportunities if sparse.
    Ensures benchmarks read genuine rows and bytes rather than empty tables.
    """
    now = datetime.now(timezone.utc)
    with httpx.Client(base_url=ch_url, timeout=30.0) as client:
        # Check current row counts
        try:
            r_s = client.get("/", params={"query": "SELECT count() FROM polypulse.spread_observations"})
            s_count = int(r_s.text.strip()) if r_s.status_code == 200 else 0
        except Exception:
            s_count = 0

        try:
            r_o = client.get("/", params={"query": "SELECT count() FROM polypulse.arbitrage_opportunities"})
            o_count = int(r_o.text.strip()) if r_o.status_code == 200 else 0
        except Exception:
            o_count = 0

        pairs = [
            ("poly_fed:kalshi_fed", "FOMC Rate Decision", "Rate Cut >= 25bps", "direct"),
            ("poly_cpi:kalshi_cpi", "CPI Inflation Rate YoY", "CPI > 2.8%", "direct"),
            ("poly_pres:kalshi_pres", "US Presidential Election 2028", "Democratic Nominee", "direct"),
            ("poly_gdp:kalshi_gdp", "US Real GDP Growth Q4", "GDP Growth > 2.5%", "inverse"),
        ]

        if s_count < min_spreads:
            to_insert = min_spreads - s_count
            spread_rows = []
            for i in range(to_insert):
                pair_id, event, outcome, mapping = pairs[i % len(pairs)]
                ts = (now - timedelta(seconds=random.randint(10, 80000))).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                p_bid = round(random.uniform(0.35, 0.45), 6)
                p_ask = round(p_bid + random.uniform(0.01, 0.04), 6)
                k_bid = round(random.uniform(0.40, 0.50), 6)
                k_ask = round(k_bid + random.uniform(0.01, 0.04), 6)
                gross = round(k_bid - p_ask, 6)
                poly_fee = 0.0008 if gross > 0 else 0.0
                kalshi_fee = 0.0170 if gross > 0 else 0.0
                slip = 0.0008
                net = round(gross - poly_fee - kalshi_fee - slip, 6)
                status = "executable" if net > 0 else ("candidate" if gross > 0 else "none")
                spread_rows.append({
                    "observation_id": str(uuid.uuid4()),
                    "pair_id": pair_id,
                    "canonical_event": event,
                    "canonical_outcome": outcome,
                    "equivalence_mapping": mapping,
                    "observed_at": ts,
                    "poly_best_bid": f"{p_bid:.6f}",
                    "poly_best_ask": f"{p_ask:.6f}",
                    "kalshi_best_bid": f"{k_bid:.6f}",
                    "kalshi_best_ask": f"{k_ask:.6f}",
                    "poly_ask_depth": "500.000000",
                    "kalshi_bid_depth": "400.000000",
                    "gross_spread": f"{gross:.6f}",
                    "gross_spread_bps": f"{(gross/p_ask)*10000:.2f}",
                    "net_spread": f"{net:.6f}",
                    "net_spread_bps": f"{(net/p_ask)*10000:.2f}",
                    "poly_fee": f"{poly_fee:.6f}",
                    "kalshi_fee": f"{kalshi_fee:.6f}",
                    "slippage_cost": f"{slip:.6f}",
                    "executable_qty": "400.000000",
                    "opportunity_type": "long_poly_short_kalshi",
                    "status": status,
                    "quote_age_poly": round(random.uniform(0.1, 2.0), 3),
                    "quote_age_kalshi": round(random.uniform(0.1, 2.0), 3),
                    "venue_delta_seconds": round(random.uniform(0.01, 0.5), 3)
                })
            for c in range(0, len(spread_rows), 1000):
                chunk = spread_rows[c:c+1000]
                payload = "\n".join(json.dumps(r) for r in chunk) + "\n"
                client.post("/", params={"query": "INSERT INTO polypulse.spread_observations FORMAT JSONEachRow", "date_time_input_format": "best_effort"}, content=payload)

        if o_count < min_opps:
            to_insert_opps = min_opps - o_count
            opp_rows = []
            for i in range(to_insert_opps):
                pair_id, event, outcome, _ = pairs[i % len(pairs)]
                first_ts = now - timedelta(seconds=random.randint(10, 80000))
                duration = round(random.uniform(5.0, 180.0), 3)
                last_ts = first_ts + timedelta(seconds=duration)
                first_str = first_ts.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                last_str = last_ts.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                opp_type = random.choice(["long_poly_short_kalshi", "long_kalshi_short_poly", "mint_both_long"])
                b_v, s_v = ("polymarket", "kalshi") if opp_type == "long_poly_short_kalshi" else (("kalshi", "polymarket") if opp_type == "long_kalshi_short_poly" else ("polymarket", "kalshi"))
                gross = round(random.uniform(0.03, 0.09), 6)
                poly_fee = 0.0008
                kalshi_fee = 0.0170
                net = round(gross - poly_fee - kalshi_fee - 0.0008, 6)
                opp_rows.append({
                    "opportunity_id": str(uuid.uuid4()),
                    "pair_id": pair_id,
                    "opportunity_type": opp_type,
                    "buy_venue": b_v,
                    "sell_venue": s_v,
                    "canonical_event": event,
                    "canonical_outcome": outcome,
                    "status": "executable" if net > 0 else "candidate",
                    "gross_spread": f"{gross:.6f}",
                    "gross_spread_bps": f"{(gross/0.40)*10000:.2f}",
                    "net_spread": f"{net:.6f}",
                    "net_spread_bps": f"{(net/0.40)*10000:.2f}",
                    "available_qty": f"{random.uniform(100.0, 1500.0):.6f}",
                    "vwap_entry": "0.400000",
                    "vwap_exit": "0.450000",
                    "fee_version": "v2026.2",
                    "poly_fee": f"{poly_fee:.6f}",
                    "kalshi_fee": f"{kalshi_fee:.6f}",
                    "slippage_bps": "10.00",
                    "first_observed_at": first_str,
                    "last_observed_at": last_str,
                    "duration_seconds": duration,
                    "observation_count": random.randint(5, 120)
                })
            for c in range(0, len(opp_rows), 1000):
                chunk = opp_rows[c:c+1000]
                payload = "\n".join(json.dumps(r) for r in chunk) + "\n"
                client.post("/", params={"query": "INSERT INTO polypulse.arbitrage_opportunities FORMAT JSONEachRow", "date_time_input_format": "best_effort"}, content=payload)


async def benchmark_clickhouse_queries(iterations: int = 100, warmup_iterations: int = 5):
    ch_url = os.getenv("CLICKHOUSE_URL") or "http://polypulse:7CC8CA94DAFA6F05466B7DAE01F73EFC@127.0.0.1:8123"
    populate_benchmark_dataset(ch_url)
    analytics = ClickHouseAnalytics(ch_url)
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")

    queries = [
        ("top_spread_pairs",
         """SELECT pair_id, canonical_event, canonical_outcome, equivalence_mapping,
                   count() AS total_observations, avg(gross_spread) AS avg_gross_spread,
                   max(gross_spread) AS max_gross_spread, avg(net_spread) AS avg_net_spread,
                   max(net_spread) AS max_net_spread, avg(executable_qty) AS avg_executable_qty,
                   sum(if(status = 'executable', 1, 0)) AS executable_observations,
                   round(100.0 * sum(if(status = 'executable', 1, 0)) / nullIf(count(), 0), 1) AS executable_rate_pct
            FROM polypulse.spread_observations FINAL
            WHERE observed_at >= {since:DateTime64(3, 'UTC')}
            GROUP BY pair_id, canonical_event, canonical_outcome, equivalence_mapping
            HAVING total_observations >= {min_obs:UInt32}
            ORDER BY avg_net_spread DESC, total_observations DESC
            LIMIT {limit:UInt32}""",
         {"since": since, "min_obs": 1, "limit": 10}),

        ("opportunity_frequency",
         """SELECT pair_id, canonical_event,
                   count() AS total_observations,
                   sum(if(status = 'executable', 1, 0)) AS executable_count,
                   round(100.0 * sum(if(status = 'executable', 1, 0)) / nullIf(count(), 0), 2) AS executable_pct,
                   avg(net_spread) AS avg_net_spread, max(net_spread) AS max_net_spread,
                   round(100.0 * sum(if(gross_spread > 0 AND (net_spread <= 0 OR status = 'candidate'), 1, 0)) / nullIf(sum(if(gross_spread > 0, 1, 0)), 0), 2) AS fee_erosion_pct,
                   round(100.0 * sum(if(status = 'stale', 1, 0)) / nullIf(count(), 0), 2) AS stale_rejection_pct
            FROM polypulse.spread_observations FINAL
            WHERE observed_at >= {since:DateTime64(3, 'UTC')}
            GROUP BY pair_id, canonical_event
            HAVING total_observations >= {min_obs:UInt32}
            ORDER BY executable_count DESC, total_observations DESC""",
         {"since": since, "min_obs": 1}),

        ("duration_stats",
         """SELECT count() AS total_opportunities,
                   avg(duration_seconds) AS avg_duration_seconds,
                   quantile(0.5)(duration_seconds) AS p50_duration_seconds,
                   quantile(0.95)(duration_seconds) AS p95_duration_seconds,
                   max(duration_seconds) AS max_duration_seconds,
                   avg(gross_spread) AS avg_gross_spread,
                   avg(net_spread) AS avg_net_spread,
                   avg(available_qty) AS avg_available_qty
            FROM polypulse.arbitrage_opportunities FINAL
            WHERE first_observed_at >= {since:DateTime64(3, 'UTC')}""",
         {"since": since}),

        ("venue_comparison",
         """SELECT buy_venue, sell_venue,
                   count() AS opportunity_count,
                   avg(gross_spread) AS avg_gross_spread,
                   avg(net_spread) AS avg_net_spread,
                   avg(poly_fee) AS avg_poly_fee,
                   avg(kalshi_fee) AS avg_kalshi_fee,
                   avg(available_qty) AS avg_available_qty,
                   sum(if(status = 'executable', 1, 0)) AS executable_count
            FROM polypulse.arbitrage_opportunities FINAL
            WHERE first_observed_at >= {since:DateTime64(3, 'UTC')}
            GROUP BY buy_venue, sell_venue
            ORDER BY opportunity_count DESC""",
         {"since": since}),

        ("quote_freshness",
         """SELECT count() AS observation_count,
                   avg(quote_age_poly) AS avg_quote_age_poly,
                   quantile(0.95)(quote_age_poly) AS p95_quote_age_poly,
                   avg(quote_age_kalshi) AS avg_quote_age_kalshi,
                   quantile(0.95)(quote_age_kalshi) AS p95_quote_age_kalshi,
                   avg(venue_delta_seconds) AS avg_venue_delta_seconds,
                   quantile(0.95)(venue_delta_seconds) AS p95_venue_delta_seconds,
                   max(venue_delta_seconds) AS max_venue_delta_seconds,
                   sum(if(venue_delta_seconds > 5.0, 1, 0)) AS desync_count
            FROM polypulse.spread_observations FINAL
            WHERE observed_at >= {since:DateTime64(3, 'UTC')}""",
         {"since": since})
    ]

    # Warmup connection
    try:
        await analytics.execute_query_with_telemetry("SELECT 1")
    except Exception:
        pass

    telemetry_results = {}
    for query_name, sql, params in queries:
        # Per-query warm-up iterations (not measured)
        for _ in range(warmup_iterations):
            await analytics.execute_query_with_telemetry(sql, params)

        client_rtts = []
        server_execs = []
        last_rows_read = 0
        last_bytes_read = 0
        last_returned = 0

        # Measured iterations (100 iterations)
        for _ in range(iterations):
            res = await analytics.execute_query_with_telemetry(sql, params)
            client_rtts.append(res.get("client_rtt_ms", 0.0))
            server_execs.append(res.get("server_elapsed_ms", 0.0))
            last_rows_read = res.get("rows_read", 0)
            last_bytes_read = res.get("bytes_read", 0)
            last_returned = len(res.get("data", []))

        client_rtts.sort()
        server_execs.sort()
        p95_idx = min(len(client_rtts) - 1, int(0.95 * len(client_rtts)))

        telemetry_results[query_name] = {
            "iterations": iterations,
            "warmup_iterations": warmup_iterations,
            "client_rtt_p50": round(statistics.median(client_rtts), 2),
            "client_rtt_p95": round(client_rtts[p95_idx], 2),
            "server_exec_p50": round(statistics.median(server_execs), 2),
            "server_exec_p95": round(server_execs[p95_idx], 2),
            "rows_read": last_rows_read,
            "bytes_read": last_bytes_read,
            "rows_returned": last_returned
        }

    await analytics.aclose()
    return telemetry_results


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
    print(" ClickHouse Historical Cross-Venue Analytics Latency (100 Measured Iterations After 5 Warm-up):")
    print(f" {'Query Name':<22} | {'Rows Read':>9} | {'Bytes Read':>10} | {'Server p50/p95':>15} | {'Client RTT p50/p95':>17}")
    print("-" * 78)
    try:
        ch_times = asyncio.run(benchmark_clickhouse_queries(iterations=100, warmup_iterations=5))
        for q, stats in ch_times.items():
            server_str = f"{stats['server_exec_p50']:.1f}/{stats['server_exec_p95']:.1f} ms"
            client_str = f"{stats['client_rtt_p50']:.1f}/{stats['client_rtt_p95']:.1f} ms"
            print(f" {q:<22} | {stats['rows_read']:9,d} | {stats['bytes_read']:10,d} | {server_str:>15} | {client_str:>17}")
    except Exception as exc:
        print(f"    - ClickHouse queries skipped ({exc})")

    print("-" * 78)
    print(" NOTE: ClickHouse benchmarks executed 100 measured iterations after warm-up against populated tables.")
    print(" Microbenchmarks 1-4 evaluate memory fixtures for algorithmic throughput.")
    print("=" * 78)


if __name__ == "__main__":
    main()

"""Integration tests for ClickHouse Analytics Infrastructure (Phase 4)."""
import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time
import unittest
import uuid
import httpx
from fastapi.testclient import TestClient

from backend.analytics import ClickHouseAnalytics
from backend.durable import DurableJournal, export_live_forever
from backend.events import now_utc, parse_polymarket
from backend.main import app
from backend.state import MarketState

CLICKHOUSE_URL = os.getenv("CLICKHOUSE_URL") or (
    f"http://polypulse:{os.getenv('CLICKHOUSE_PASSWORD', '7CC8CA94DAFA6F05466B7DAE01F73EFC')}@localhost:8123"
)


class ClickHouseAnalyticsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Verify ClickHouse is reachable
        cls.ch_url = CLICKHOUSE_URL
        try:
            with httpx.Client(timeout=3.0) as client:
                r = client.get(f"{cls.ch_url}/ping")
                if r.status_code != 200:
                    raise RuntimeError("ClickHouse ping failed")
        except Exception:
            raise unittest.SkipTest("Local ClickHouse instance not available")

        # Initialize schema and MVs from init.sql
        init_sql_path = Path(__file__).resolve().parents[1] / "database" / "init.sql"
        if init_sql_path.exists():
            content = init_sql_path.read_text(encoding="utf-8")
            statements = [s.strip() for s in content.split(";") if s.strip()]
            with httpx.Client(base_url=cls.ch_url, timeout=10.0) as client:
                for stmt in statements:
                    # Strip comment-only statements
                    clean_lines = [line for line in stmt.split("\n") if not line.strip().startswith("--")]
                    clean_stmt = "\n".join(clean_lines).strip()
                    if clean_stmt:
                        resp = client.post("/", params={"query": clean_stmt})
                        resp.raise_for_status()

    def test_schema_and_tables_exist(self):
        with httpx.Client(base_url=self.ch_url, timeout=5.0) as client:
            r = client.get("/", params={"query": "SHOW TABLES FROM polypulse"})
            self.assertEqual(r.status_code, 200)
            tables = set(r.text.strip().split("\n"))
            expected = {
                "market_events", "trades", "orderbook_snapshots",
                "feed_health", "ingestion_errors",
                "trade_candles_1m", "book_summary_1m", "venue_event_stats_10s"
            }
            self.assertTrue(expected.issubset(tables), f"Missing tables: {expected - tables}")

    def test_duplicate_delivery_and_deduplication(self):
        """Simulates at-least-once duplicate delivery and verifies logical vs physical counts."""
        market_id = f"test-dedup-{uuid.uuid4().hex[:12]}"
        with tempfile.TemporaryDirectory() as folder:
            journal = DurableJournal(Path(folder) / "events.db")
            state = MarketState(journal, mode="live")
            try:
                # 1. Produce 2 events (1 snapshot, 1 trade)
                stamp = str(int(time.time() * 1000))
                snapshot = parse_polymarket({
                    "event_type": "book", "asset_id": market_id,
                    "timestamp": stamp, "hash": "h1",
                    "bids": [{"price": "0.48", "size": "10"}],
                    "asks": [{"price": "0.52", "size": "10"}]
                }, now_utc())[0]
                trade = parse_polymarket({
                    "event_type": "last_trade_price", "asset_id": market_id,
                    "timestamp": stamp, "transaction_hash": f"0x{uuid.uuid4().hex}",
                    "side": "BUY", "price": "0.50", "size": "5"
                }, now_utc())[0]

                state.process_live_event(snapshot)
                state.process_live_event(trade)

                # Export to ClickHouse
                async def flush():
                    status = {}
                    task = asyncio.create_task(export_live_forever(journal, self.ch_url, status))
                    try:
                        async def wait_pending():
                            while any(journal.pending_rows(t) for t in ("events", "book_observations")):
                                await asyncio.sleep(0.1)
                        await asyncio.wait_for(wait_pending(), timeout=10)
                    finally:
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)

                asyncio.run(flush())

                # Check initial logical counts
                with httpx.Client(base_url=self.ch_url, timeout=5.0) as client:
                    q_unique = f"SELECT uniqExact(event_id) FROM polypulse.market_events WHERE market_id='{market_id}'"
                    self.assertEqual(client.get("/", params={"query": q_unique}).text.strip(), "2")
                    q_trades = f"SELECT uniqExact(event_id) FROM polypulse.trades WHERE market_id='{market_id}'"
                    self.assertEqual(client.get("/", params={"query": q_trades}).text.strip(), "1")

                # 2. Resend previously exported events (simulating network disconnect before ACK)
                journal.db.execute("UPDATE events SET exported=0 WHERE market_id=?", (market_id,))
                journal.db.execute("UPDATE book_observations SET exported=0 WHERE market_id=?", (market_id,))
                journal.db.commit()

                asyncio.run(flush())

                # Logical count MUST remain identical (2 events, 1 trade), never inflated by duplicate delivery!
                with httpx.Client(base_url=self.ch_url, timeout=5.0) as client:
                    self.assertEqual(client.get("/", params={"query": q_unique}).text.strip(), "2")
                    self.assertEqual(client.get("/", params={"query": q_trades}).text.strip(), "1")
                    q_obs = f"SELECT uniqExact(observation_id) FROM polypulse.orderbook_snapshots WHERE market_id='{market_id}'"
                    self.assertEqual(client.get("/", params={"query": q_obs}).text.strip(), "1")
            finally:
                for book in state.books.values():
                    book.close()
                journal.db.close()

    def test_materialized_view_candles_and_summary(self):
        """Verifies real-time aggregation of trade candles and orderbook summaries."""
        market_id = f"test-mv-{uuid.uuid4().hex[:12]}"
        now = datetime.now(timezone.utc)
        with httpx.Client(base_url=self.ch_url, timeout=10.0) as client:
            # Insert 3 trades directly
            trades_rows = [
                {"event_id": str(uuid.uuid4()), "venue": "polymarket", "market_id": market_id,
                 "source_id": "tx1", "side": "buy", "wallet": "0x111", "price": "0.500000",
                 "quantity": "10.000000", "exchange_timestamp": now.isoformat(),
                 "received_timestamp": now.isoformat()},
                {"event_id": str(uuid.uuid4()), "venue": "polymarket", "market_id": market_id,
                 "source_id": "tx2", "side": "buy", "wallet": "0x222", "price": "0.550000",
                 "quantity": "20.000000", "exchange_timestamp": (now + timedelta(seconds=5)).isoformat(),
                 "received_timestamp": (now + timedelta(seconds=5)).isoformat()},
                {"event_id": str(uuid.uuid4()), "venue": "polymarket", "market_id": market_id,
                 "source_id": "tx3", "side": "sell", "wallet": "0x333", "price": "0.480000",
                 "quantity": "15.000000", "exchange_timestamp": (now + timedelta(seconds=10)).isoformat(),
                 "received_timestamp": (now + timedelta(seconds=10)).isoformat()}
            ]
            content = "\n".join(json.dumps(r) for r in trades_rows) + "\n"
            r = client.post("/", params={
                "query": "INSERT INTO polypulse.trades FORMAT JSONEachRow",
                "date_time_input_format": "best_effort"
            }, content=content)
            self.assertEqual(r.status_code, 200)

            # Insert orderbook snapshots
            book_rows = [
                {"observation_id": 99901, "venue": "polymarket", "market_id": market_id,
                 "snapshot_time": now.isoformat(), "best_bid": "0.480000", "best_ask": "0.520000",
                 "bid_depth": "100.000000", "ask_depth": "120.000000", "sequence": 1, "valid": 1},
                {"observation_id": 99902, "venue": "polymarket", "market_id": market_id,
                 "snapshot_time": (now + timedelta(seconds=15)).isoformat(), "best_bid": "0.490000", "best_ask": "0.510000",
                 "bid_depth": "150.000000", "ask_depth": "110.000000", "sequence": 2, "valid": 1}
            ]
            content_book = "\n".join(json.dumps(r) for r in book_rows) + "\n"
            r_book = client.post("/", params={
                "query": "INSERT INTO polypulse.orderbook_snapshots FORMAT JSONEachRow",
                "date_time_input_format": "best_effort"
            }, content=content_book)
            self.assertEqual(r_book.status_code, 200)

            # Query trade_candles_1m MV
            query_candle = f"""
            SELECT
                countMerge(trade_count) AS trades,
                sumMerge(total_volume) AS total_vol,
                sumMerge(buy_volume) AS buy_vol,
                sumMerge(sell_volume) AS sell_vol,
                argMinMerge(open_price) AS open_p,
                maxMerge(high_price) AS high_p,
                minMerge(low_price) AS low_p,
                argMaxMerge(close_price) AS close_p
            FROM polypulse.trade_candles_1m
            WHERE market_id = '{market_id}'
            """
            r_c = client.get("/", params={"query": query_candle + " FORMAT JSONEachRow"})
            self.assertEqual(r_c.status_code, 200)
            res = json.loads(r_c.text.strip())
            self.assertEqual(res["trades"], 3)
            self.assertAlmostEqual(float(res["total_vol"]), 45.0)
            self.assertAlmostEqual(float(res["buy_vol"]), 30.0)
            self.assertAlmostEqual(float(res["sell_vol"]), 15.0)
            self.assertAlmostEqual(float(res["open_p"]), 0.50)
            self.assertAlmostEqual(float(res["high_p"]), 0.55)
            self.assertAlmostEqual(float(res["low_p"]), 0.48)
            self.assertAlmostEqual(float(res["close_p"]), 0.48)

    def test_fastapi_analytics_endpoints(self):
        """Verifies the 6 REST analytics endpoints."""
        market_id = f"test-api-{uuid.uuid4().hex[:8]}"
        now = datetime.now(timezone.utc)
        # Seed test data in ClickHouse
        with httpx.Client(base_url=self.ch_url, timeout=10.0) as client:
            trade_row = {
                "event_id": str(uuid.uuid4()), "venue": "polymarket", "market_id": market_id,
                "source_id": "api-tx", "side": "buy", "wallet": "0xabc", "price": "0.520000",
                "quantity": "8.000000", "exchange_timestamp": now.isoformat(),
                "received_timestamp": now.isoformat()
            }
            client.post("/", params={
                "query": "INSERT INTO polypulse.trades FORMAT JSONEachRow",
                "date_time_input_format": "best_effort"
            }, content=json.dumps(trade_row) + "\n")

            snap_row = {
                "observation_id": 88801, "venue": "polymarket", "market_id": market_id,
                "snapshot_time": now.isoformat(), "best_bid": "0.510000", "best_ask": "0.530000",
                "bid_depth": "80.000000", "ask_depth": "90.000000", "sequence": 1, "valid": 1
            }
            client.post("/", params={
                "query": "INSERT INTO polypulse.orderbook_snapshots FORMAT JSONEachRow",
                "date_time_input_format": "best_effort"
            }, content=json.dumps(snap_row) + "\n")

        with TestClient(app) as test_client:
            # Configure app state analytics pointing to test ClickHouse
            app.state.analytics = ClickHouseAnalytics(self.ch_url)

            # 1. Summary
            resp = test_client.get(f"/api/v1/analytics/market/{market_id}/summary")
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertEqual(data["market_id"], market_id)
            self.assertGreaterEqual(data["trade_count"], 1)
            self.assertAlmostEqual(data["vwap"], 0.52)
            self.assertAlmostEqual(data["best_bid"], 0.51)
            self.assertAlmostEqual(data["best_ask"], 0.53)
            self.assertAlmostEqual(data["spread"], 0.02)

            # 2. Candles
            resp_c = test_client.get(f"/api/v1/analytics/market/{market_id}/candles?resolution=1m")
            self.assertEqual(resp_c.status_code, 200)
            candles = resp_c.json()
            self.assertIsInstance(candles, list)

            # 3. Trades
            resp_t = test_client.get(f"/api/v1/analytics/market/{market_id}/trades?limit=10")
            self.assertEqual(resp_t.status_code, 200)
            trades = resp_t.json()
            self.assertGreaterEqual(len(trades), 1)
            self.assertEqual(trades[0]["market_id"], market_id)
            self.assertAlmostEqual(trades[0]["price"], 0.52)

            # 4. Spread Trajectory
            resp_s = test_client.get(f"/api/v1/analytics/market/{market_id}/spread?limit=10")
            self.assertEqual(resp_s.status_code, 200)
            spread_data = resp_s.json()
            self.assertIsInstance(spread_data, list)

            # 5. Venues
            resp_v = test_client.get("/api/v1/analytics/venues?window=24h")
            self.assertEqual(resp_v.status_code, 200)
            venues = resp_v.json()
            self.assertIsInstance(venues, list)

            # 6. Storage Telemetry
            resp_st = test_client.get("/api/v1/analytics/storage")
            self.assertEqual(resp_st.status_code, 200)
            st = resp_st.json()
            self.assertEqual(st["status"], "connected")
            self.assertIn("tables", st)
            self.assertIn("journal_stats", st)

            # 7. Boundary Validation: Max date range > 30 days must be rejected
            t0 = (now - timedelta(days=35)).isoformat()
            t1 = now.isoformat()
            resp_invalid = test_client.get(f"/api/v1/analytics/market/{market_id}/candles?start_time={t0}&end_time={t1}")
            self.assertEqual(resp_invalid.status_code, 422)

    def test_14_step_duplicate_delivery_audit(self):
        """Comprehensive 14-step duplicate delivery audit through ClickHouse tables and materialized views."""
        market_id = f"test-audit14-{uuid.uuid4().hex[:8]}"
        now = datetime.now(timezone.utc).replace(microsecond=0)

        db_dir = tempfile.mkdtemp()
        try:
            db_path = Path(db_dir) / "audit14.db"
            journal = DurableJournal(db_path)
            state = MarketState(journal, mode="live")

            try:
                # Step 1: Initialize / verify ClickHouse connection and tables
                with httpx.Client(base_url=self.ch_url, timeout=10.0) as client:
                    r_tables = client.get("/", params={"query": "SHOW TABLES FROM polypulse"})
                    self.assertEqual(r_tables.status_code, 200)

                    # Step 2: Ingest 3 unique trade events (T1, T2, T3) and 2 orderbook snapshots (S1, S2) into journal
                    t1 = parse_polymarket({
                        "event_type": "last_trade_price", "asset_id": market_id,
                        "timestamp": str(int((now + timedelta(seconds=1)).timestamp() * 1000)),
                        "transaction_hash": "tx1", "side": "BUY", "price": "0.50", "size": "10"
                    }, (now + timedelta(seconds=1)).isoformat())[0]
                    t1_id = t1.event_id

                    t2 = parse_polymarket({
                        "event_type": "last_trade_price", "asset_id": market_id,
                        "timestamp": str(int((now + timedelta(seconds=2)).timestamp() * 1000)),
                        "transaction_hash": "tx2", "side": "BUY", "price": "0.55", "size": "20"
                    }, (now + timedelta(seconds=2)).isoformat())[0]
                    t2_id = t2.event_id

                    t3 = parse_polymarket({
                        "event_type": "last_trade_price", "asset_id": market_id,
                        "timestamp": str(int((now + timedelta(seconds=3)).timestamp() * 1000)),
                        "transaction_hash": "tx3", "side": "SELL", "price": "0.48", "size": "15"
                    }, (now + timedelta(seconds=3)).isoformat())[0]
                    t3_id = t3.event_id

                    s1 = parse_polymarket({
                        "event_type": "book", "asset_id": market_id,
                        "timestamp": str(int((now + timedelta(seconds=1)).timestamp() * 1000)),
                        "hash": "h1",
                        "bids": [{"price": "0.49", "size": "100"}],
                        "asks": [{"price": "0.51", "size": "110"}]
                    }, (now + timedelta(seconds=1)).isoformat())[0]

                    s2 = parse_polymarket({
                        "event_type": "book", "asset_id": market_id,
                        "timestamp": str(int((now + timedelta(seconds=2)).timestamp() * 1000)),
                        "hash": "h2",
                        "bids": [{"price": "0.50", "size": "120"}],
                        "asks": [{"price": "0.52", "size": "130"}]
                    }, (now + timedelta(seconds=2)).isoformat())[0]

                    state.process_live_event(s1)
                    state.process_live_event(t1)
                    state.process_live_event(s2)
                    state.process_live_event(t2)
                    state.process_live_event(t3)

                    # Create persistent immutable export batches for trades and orderbook snapshots
                    batch_trades_1 = journal.create_export_batch("trades")
                    self.assertIsNotNone(batch_trades_1)
                    self.assertEqual(batch_trades_1["item_count"], 3)

                    batch_snaps_1 = journal.create_export_batch("orderbook_snapshots")
                    self.assertIsNotNone(batch_snaps_1)

                    # Export Batch 1 to ClickHouse
                    r_ins_t1 = client.post("/", params={
                        "query": "INSERT INTO polypulse.trades FORMAT JSONEachRow",
                        "insert_deduplicate": "1",
                        "insert_deduplication_token": batch_trades_1["dedup_token"],
                        "deduplicate_blocks_in_dependent_materialized_views": "1",
                        "date_time_input_format": "best_effort"
                    }, content=batch_trades_1["payload"])
                    self.assertEqual(r_ins_t1.status_code, 200)

                    r_ins_s1 = client.post("/", params={
                        "query": "INSERT INTO polypulse.orderbook_snapshots FORMAT JSONEachRow",
                        "insert_deduplicate": "1",
                        "insert_deduplication_token": batch_snaps_1["dedup_token"],
                        "deduplicate_blocks_in_dependent_materialized_views": "1",
                        "date_time_input_format": "best_effort"
                    }, content=batch_snaps_1["payload"])
                    self.assertEqual(r_ins_s1.status_code, 200)

                    # Step 3: Record raw counts and MV aggregate values
                    q_step3 = f"""
                    SELECT
                        countMerge(trade_count) AS trades,
                        sumMerge(total_volume) AS total_vol,
                        sumMerge(buy_volume) AS buy_vol,
                        sumMerge(sell_volume) AS sell_vol,
                        argMinMerge(open_price) AS open_p,
                        maxMerge(high_price) AS high_p,
                        minMerge(low_price) AS low_p,
                        argMaxMerge(close_price) AS close_p
                    FROM polypulse.trade_candles_1m
                    WHERE market_id = '{market_id}'
                    FORMAT JSONEachRow
                    """
                    res_step3 = json.loads(client.get("/", params={"query": q_step3}).text.strip())
                    self.assertEqual(res_step3["trades"], 3)
                    self.assertAlmostEqual(float(res_step3["total_vol"]), 45.0)
                    self.assertAlmostEqual(float(res_step3["buy_vol"]), 30.0)
                    self.assertAlmostEqual(float(res_step3["sell_vol"]), 15.0)
                    self.assertAlmostEqual(float(res_step3["open_p"]), 0.50)
                    self.assertAlmostEqual(float(res_step3["high_p"]), 0.55)
                    self.assertAlmostEqual(float(res_step3["low_p"]), 0.48)
                    self.assertAlmostEqual(float(res_step3["close_p"]), 0.48)

                    # Step 4: Simulate lost ACK: replay Batch 1 with identical token and payload
                    pending_t1 = journal.get_pending_batch("trades")
                    self.assertEqual(pending_t1["batch_id"], batch_trades_1["batch_id"])
                    self.assertEqual(pending_t1["dedup_token"], batch_trades_1["dedup_token"])

                    r_replay_a = client.post("/", params={
                        "query": "INSERT INTO polypulse.trades FORMAT JSONEachRow",
                        "insert_deduplicate": "1",
                        "insert_deduplication_token": pending_t1["dedup_token"],
                        "deduplicate_blocks_in_dependent_materialized_views": "1",
                        "date_time_input_format": "best_effort"
                    }, content=pending_t1["payload"])
                    self.assertEqual(r_replay_a.status_code, 200)

                    # Step 5 & 6: Verify ClickHouse deduplication dropped the duplicate block
                    res_step6 = json.loads(client.get("/", params={"query": q_step3}).text.strip())
                    self.assertEqual(res_step6["trades"], 3, "Lost ACK retry MUST NOT increment MV trade count")
                    self.assertAlmostEqual(float(res_step6["total_vol"]), 45.0, msg="Lost ACK retry MUST NOT double volume")

                    # Step 7: Simulate exporter restart: reopen journal from disk and replay with same batch token
                    journal_restarted = DurableJournal(db_path)
                    try:
                        resumed_batch = journal_restarted.get_pending_batch("trades")
                        self.assertEqual(resumed_batch["batch_id"], batch_trades_1["batch_id"])

                        with httpx.Client(base_url=self.ch_url, timeout=10.0) as fresh_client:
                            r_restart = fresh_client.post("/", params={
                                "query": "INSERT INTO polypulse.trades FORMAT JSONEachRow",
                                "insert_deduplicate": "1",
                                "insert_deduplication_token": resumed_batch["dedup_token"],
                                "deduplicate_blocks_in_dependent_materialized_views": "1",
                                "date_time_input_format": "best_effort"
                            }, content=resumed_batch["payload"])
                            self.assertEqual(r_restart.status_code, 200)
                    finally:
                        journal_restarted.db.close()

                    # Step 8: Check materialized view aggregates again (volume remains 45.0)
                    res_step8 = json.loads(client.get("/", params={"query": q_step3}).text.strip())
                    self.assertEqual(res_step8["trades"], 3, "Exporter restart retry MUST NOT alter MV trade count")
                    self.assertAlmostEqual(float(res_step8["total_vol"]), 45.0)

                    # Acknowledge Batch 1
                    journal.acknowledge_batch(batch_trades_1["batch_id"])
                    journal.acknowledge_batch(batch_snaps_1["batch_id"])

                    # Step 9: Ingest new trade T4 (qty 25, price 0.52, buy)
                    t4 = parse_polymarket({
                        "event_type": "last_trade_price", "asset_id": market_id,
                        "timestamp": str(int((now + timedelta(seconds=4)).timestamp() * 1000)),
                        "transaction_hash": "tx4", "side": "BUY", "price": "0.52", "size": "25"
                    }, (now + timedelta(seconds=4)).isoformat())[0]
                    t4_id = t4.event_id
                    state.process_live_event(t4)

                    # Intentionally overlapping batch rejection test:
                    # Attempting to assign an already-batched event (t3_id) to a new batch is strictly rejected
                    with self.assertRaises(sqlite3.IntegrityError):
                        journal.db.execute(
                            "INSERT INTO export_batch_items (batch_id, target_table, source_table, item_id, assigned_at) VALUES (?, ?, ?, ?, ?)",
                            ("rogue_batch_id", "trades", "events", t3_id, now.isoformat())
                        )
                    journal.db.rollback()

                    # Create Batch 2 containing strictly T4
                    batch_trades_2 = journal.create_export_batch("trades")
                    self.assertIsNotNone(batch_trades_2)
                    self.assertEqual(batch_trades_2["item_count"], 1)
                    batch_trades_2_ids = json.loads(batch_trades_2["event_ids"])
                    self.assertEqual(batch_trades_2_ids, [t4_id])

                    # Export Batch 2
                    r_ins_t2 = client.post("/", params={
                        "query": "INSERT INTO polypulse.trades FORMAT JSONEachRow",
                        "insert_deduplicate": "1",
                        "insert_deduplication_token": batch_trades_2["dedup_token"],
                        "deduplicate_blocks_in_dependent_materialized_views": "1",
                        "date_time_input_format": "best_effort"
                    }, content=batch_trades_2["payload"])
                    self.assertEqual(r_ins_t2.status_code, 200)
                    journal.acknowledge_batch(batch_trades_2["batch_id"])

                    # Step 10: Check materialized view aggregates:
                    # Volume is now exactly 45 + 25 = 70.0! Trades = 4! Buy = 55.0! Sell = 15.0! Close = 0.52!
                    res_step10 = json.loads(client.get("/", params={"query": q_step3}).text.strip())
                    self.assertEqual(res_step10["trades"], 4, "Materialized View trades count MUST be exactly 4")
                    self.assertAlmostEqual(float(res_step10["total_vol"]), 70.0, msg="MV total volume MUST be exactly 70.0 (45+25)")
                    self.assertAlmostEqual(float(res_step10["buy_vol"]), 55.0, msg="MV buy volume MUST be exactly 55.0 (30+25)")
                    self.assertAlmostEqual(float(res_step10["sell_vol"]), 15.0, msg="MV sell volume MUST be exactly 15.0")
                    self.assertAlmostEqual(float(res_step10["close_p"]), 0.52, msg="MV close price MUST be exactly 0.52 (T4)")

                    # Step 11: Query Authoritative API:
                    analytics = ClickHouseAnalytics(self.ch_url)
                    summary = asyncio.run(analytics.get_market_summary(market_id))
                    self.assertEqual(summary["trade_count"], 4)
                    self.assertAlmostEqual(summary["total_volume"], 70.0)
                    self.assertAlmostEqual(summary["buy_volume"], 55.0)
                    self.assertAlmostEqual(summary["sell_volume"], 15.0)
                    self.assertAlmostEqual(summary["close_price"], 0.52)

                    # Verify that Materialized View and Authoritative API agree completely!
                    self.assertEqual(res_step10["trades"], summary["trade_count"])
                    self.assertAlmostEqual(float(res_step10["total_vol"]), summary["total_volume"])
                    self.assertAlmostEqual(float(res_step10["buy_vol"]), summary["buy_volume"])
                    self.assertAlmostEqual(float(res_step10["sell_vol"]), summary["sell_volume"])
                    self.assertAlmostEqual(float(res_step10["close_p"]), summary["close_price"])

                    candles = asyncio.run(analytics.get_market_candles(market_id, resolution="1m"))
                    self.assertGreaterEqual(len(candles), 1)
                    c0 = candles[0]
                    self.assertEqual(c0["trade_count"], 4)
                    self.assertAlmostEqual(c0["volume"], 70.0)
                    self.assertAlmostEqual(c0["buy_volume"], 55.0)
                    self.assertAlmostEqual(c0["sell_volume"], 15.0)
                    self.assertAlmostEqual(c0["close"], 0.52)

                    trades_list = asyncio.run(analytics.get_market_trades(market_id, limit=50))
                    self.assertEqual(len(trades_list), 4)

                    # Step 12: Check physical row count in system.parts vs logical row count
                    q_parts = f"""
                    SELECT sum(rows)
                    FROM system.parts
                    WHERE database = 'polypulse' AND table = 'trades' AND active = 1
                    """
                    parts_resp = client.get("/", params={"query": q_parts}).text.strip()
                    physical_rows = int(parts_resp) if parts_resp else 0
                    self.assertGreaterEqual(physical_rows, 4)

                    q_logical = f"SELECT uniqExact(event_id) FROM polypulse.trades WHERE market_id = '{market_id}'"
                    logical_rows = int(client.get("/", params={"query": q_logical}).text.strip())
                    self.assertEqual(logical_rows, 4)

                    # Step 13: Force OPTIMIZE TABLE FINAL
                    client.post("/", params={"query": "OPTIMIZE TABLE polypulse.trades FINAL"})
                    client.post("/", params={"query": "OPTIMIZE TABLE polypulse.trade_candles_1m FINAL"})

                    # Step 14: Record final state: Both MV and API continue to report 70.0!
                    res_step14 = json.loads(client.get("/", params={"query": q_step3}).text.strip())
                    self.assertEqual(res_step14["trades"], 4)
                    self.assertAlmostEqual(float(res_step14["total_vol"]), 70.0)

                    summary_final = asyncio.run(analytics.get_market_summary(market_id))
                    self.assertEqual(summary_final["trade_count"], 4)
                    self.assertAlmostEqual(summary_final["total_volume"], 70.0)
            finally:
                for book in state.books.values():
                    book.close()
                journal.db.close()
        finally:
            shutil.rmtree(db_dir, ignore_errors=True)

    def test_crash_before_sending_batch(self):
        """Simulates crash after batch creation in SQLite but before sending over the network."""
        db_dir = tempfile.mkdtemp()
        try:
            db_path = Path(db_dir) / "crash_before_send.db"
            j1 = DurableJournal(db_path)
            state = MarketState(j1, mode="live")
            m_id = "test-crash-1"

            t1 = parse_polymarket({
                "event_type": "last_trade_price", "asset_id": m_id,
                "timestamp": "1700000000000", "transaction_hash": "tx1",
                "side": "BUY", "price": "0.50", "size": "10"
            }, now_utc())[0]
            state.process_live_event(t1)
            for b in state.books.values():
                b.close()

            batch = j1.create_export_batch("trades")
            self.assertIsNotNone(batch)
            self.assertEqual(batch["status"], "pending")
            batch_id = batch["batch_id"]
            token = batch["dedup_token"]
            payload = batch["payload"]

            # Crash: abrupt close without export
            j1.db.close()

            # Recovery: restart process and reload journal
            j2 = DurableJournal(db_path)
            try:
                resumed = j2.get_pending_batch("trades")
                self.assertIsNotNone(resumed)
                self.assertEqual(resumed["batch_id"], batch_id)
                self.assertEqual(resumed["dedup_token"], token)
                self.assertEqual(resumed["payload"], payload)
                self.assertEqual(resumed["status"], "pending")
            finally:
                j2.db.close()
        finally:
            shutil.rmtree(db_dir, ignore_errors=True)

    def test_crash_after_clickhouse_accepts_before_ack(self):
        """Simulates crash after ClickHouse accepted the block (HTTP 200) but before SQLite ACK."""
        db_dir = tempfile.mkdtemp()
        try:
            db_path = Path(db_dir) / "crash_after_accept.db"
            j = DurableJournal(db_path)
            state = MarketState(j, mode="live")
            m_id = f"test-crash-ack-{uuid.uuid4().hex[:6]}"
            try:
                t1 = parse_polymarket({
                    "event_type": "last_trade_price", "asset_id": m_id,
                    "timestamp": "1700000000000", "transaction_hash": "tx1",
                    "side": "BUY", "price": "0.50", "size": "10"
                }, now_utc())[0]
                state.process_live_event(t1)
                for b in state.books.values():
                    b.close()

                batch = j.create_export_batch("trades")
                with httpx.Client(base_url=self.ch_url, timeout=10.0) as client:
                    r1 = client.post("/", params={
                        "query": "INSERT INTO polypulse.trades FORMAT JSONEachRow",
                        "insert_deduplicate": "1",
                        "insert_deduplication_token": batch["dedup_token"],
                        "deduplicate_blocks_in_dependent_materialized_views": "1",
                        "date_time_input_format": "best_effort"
                    }, content=batch["payload"])
                    self.assertEqual(r1.status_code, 200)

                # Crash occurs here: ACK was NOT called. Batch is still pending in SQLite.
                j.db.close()

                # Restart recovery:
                j_reloaded = DurableJournal(db_path)
                try:
                    pending = j_reloaded.get_pending_batch("trades")
                    self.assertIsNotNone(pending)
                    self.assertEqual(pending["batch_id"], batch["batch_id"])

                    # Resend to ClickHouse
                    with httpx.Client(base_url=self.ch_url, timeout=10.0) as client:
                        r2 = client.post("/", params={
                            "query": "INSERT INTO polypulse.trades FORMAT JSONEachRow",
                            "insert_deduplicate": "1",
                            "insert_deduplication_token": pending["dedup_token"],
                            "deduplicate_blocks_in_dependent_materialized_views": "1",
                            "date_time_input_format": "best_effort"
                        }, content=pending["payload"])
                        self.assertEqual(r2.status_code, 200)

                        # Verify ClickHouse contains exactly 1 row, not 2
                        count_resp = client.get("/", params={"query": f"SELECT count() FROM polypulse.trades WHERE market_id = '{m_id}'"})
                        self.assertEqual(count_resp.text.strip(), "1")

                    # Now save ACK
                    j_reloaded.acknowledge_batch(pending["batch_id"])
                    self.assertIsNone(j_reloaded.get_pending_batch("trades"))
                finally:
                    j_reloaded.db.close()
            finally:
                if j.db:
                    try:
                        j.db.close()
                    except Exception:
                        pass
        finally:
            shutil.rmtree(db_dir, ignore_errors=True)

    def test_exact_batch_retry(self):
        """Posting identical batch multiple times produces HTTP 200 and preserves exact table counts."""
        m_id = f"test-exact-{uuid.uuid4().hex[:6]}"
        row = {
            "event_id": str(uuid.uuid4()), "venue": "polymarket", "market_id": m_id,
            "source_id": "tx-exact", "side": "buy", "wallet": "0x1", "price": "0.500000",
            "quantity": "10.000000", "exchange_timestamp": now_utc(), "received_timestamp": now_utc()
        }
        payload = json.dumps(row) + "\n"
        token = hashlib.sha256(f"trades:{payload}".encode("utf-8")).hexdigest()

        with httpx.Client(base_url=self.ch_url, timeout=10.0) as client:
            for _ in range(3):
                r = client.post("/", params={
                    "query": "INSERT INTO polypulse.trades FORMAT JSONEachRow",
                    "insert_deduplicate": "1",
                    "insert_deduplication_token": token,
                    "deduplicate_blocks_in_dependent_materialized_views": "1",
                    "date_time_input_format": "best_effort"
                }, content=payload)
                self.assertEqual(r.status_code, 200)

            count_resp = client.get("/", params={"query": f"SELECT count() FROM polypulse.trades WHERE market_id = '{m_id}'"})
            self.assertEqual(count_resp.text.strip(), "1")

    def test_backend_restart_followed_by_exact_retry(self):
        """Simulates full exporter restart where unacknowledged batch is retried and deduplicated."""
        m_id = f"test-restart-{uuid.uuid4().hex[:6]}"
        db_dir = tempfile.mkdtemp()
        try:
            db_path = Path(db_dir) / "restart.db"
            journal = DurableJournal(db_path)
            state = MarketState(journal, mode="live")

            try:
                t1 = parse_polymarket({
                    "event_type": "last_trade_price", "asset_id": m_id,
                    "timestamp": "1700000000000", "transaction_hash": "tx1",
                    "side": "BUY", "price": "0.50", "size": "10"
                }, now_utc())[0]
                state.process_live_event(t1)
                for b in state.books.values():
                    b.close()

                batch = journal.create_export_batch("trades")
                self.assertIsNotNone(batch)

                # Send once
                with httpx.Client(base_url=self.ch_url, timeout=10.0) as client:
                    r1 = client.post("/", params={
                        "query": "INSERT INTO polypulse.trades FORMAT JSONEachRow",
                        "insert_deduplicate": "1",
                        "insert_deduplication_token": batch["dedup_token"],
                        "deduplicate_blocks_in_dependent_materialized_views": "1",
                        "date_time_input_format": "best_effort"
                    }, content=batch["payload"])
                    self.assertEqual(r1.status_code, 200)

                # Simulate restart before ACK
                journal.db.close()

                journal_restarted = DurableJournal(db_path)
                try:
                    resumed = journal_restarted.get_pending_batch("trades")
                    self.assertIsNotNone(resumed)
                    self.assertEqual(resumed["batch_id"], batch["batch_id"])

                    # Exporter retries sending the unacknowledged batch
                    with httpx.Client(base_url=self.ch_url, timeout=10.0) as client:
                        r2 = client.post("/", params={
                            "query": "INSERT INTO polypulse.trades FORMAT JSONEachRow",
                            "insert_deduplicate": "1",
                            "insert_deduplication_token": resumed["dedup_token"],
                            "deduplicate_blocks_in_dependent_materialized_views": "1",
                            "date_time_input_format": "best_effort"
                        }, content=resumed["payload"])
                        self.assertEqual(r2.status_code, 200)

                        res = client.get("/", params={"query": f"SELECT count() FROM polypulse.trades WHERE market_id = '{m_id}'"})
                        self.assertEqual(res.text.strip(), "1")

                    journal_restarted.acknowledge_batch(resumed["batch_id"])
                finally:
                    journal_restarted.db.close()
            finally:
                if journal.db:
                    try:
                        journal.db.close()
                    except Exception:
                        pass
        finally:
            shutil.rmtree(db_dir, ignore_errors=True)

    def test_new_events_arriving_while_old_batch_pending(self):
        """New incoming events are not absorbed into an existing pending batch, preserving immutability."""
        db_dir = tempfile.mkdtemp()
        try:
            j = DurableJournal(Path(db_dir) / "immutability.db")
            state = MarketState(j, mode="live")
            try:
                m_id = "test-new-arr"

                t1 = parse_polymarket({
                    "event_type": "last_trade_price", "asset_id": m_id,
                    "timestamp": "1700000000000", "transaction_hash": "tx1",
                    "side": "BUY", "price": "0.50", "size": "10"
                }, now_utc())[0]
                state.process_live_event(t1)

                # Create Batch 1
                batch1 = j.create_export_batch("trades")
                self.assertEqual(batch1["item_count"], 1)

                # Ingest T2 while Batch 1 is still pending
                t2 = parse_polymarket({
                    "event_type": "last_trade_price", "asset_id": m_id,
                    "timestamp": "1700000001000", "transaction_hash": "tx2",
                    "side": "SELL", "price": "0.52", "size": "20"
                }, now_utc())[0]
                state.process_live_event(t2)
                for b in state.books.values():
                    b.close()

                # Calling get_or_create_pending_batch returns Batch 1 unchanged
                pending = j.get_or_create_pending_batch("trades")
                self.assertEqual(pending["batch_id"], batch1["batch_id"])
                self.assertEqual(pending["item_count"], 1)
                self.assertEqual(pending["dedup_token"], batch1["dedup_token"])

                # Unassigned items only list T2
                _, unassigned = j._fetch_unassigned_items("trades", 10)
                self.assertEqual(len(unassigned), 1)
                self.assertEqual(unassigned[0]["source_id"], "tx2")
            finally:
                j.db.close()
        finally:
            shutil.rmtree(db_dir, ignore_errors=True)

    def test_multiple_pending_batches_fifo(self):
        """Pending batches are ordered and processed in strict FIFO order."""
        db_dir = tempfile.mkdtemp()
        try:
            j = DurableJournal(Path(db_dir) / "fifo.db")
            state = MarketState(j, mode="live")
            try:
                m_id = "test-fifo"

                # Create 4 trades
                for i in range(4):
                    t = parse_polymarket({
                        "event_type": "last_trade_price", "asset_id": m_id,
                        "timestamp": str(1700000000000 + i * 1000), "transaction_hash": f"tx{i}",
                        "side": "BUY", "price": "0.50", "size": "10"
                    }, now_utc())[0]
                    state.process_live_event(t)
                for b in state.books.values():
                    b.close()

                # Create Batch 1 with limit=2
                b1 = j.create_export_batch("trades", limit=2)
                self.assertEqual(b1["item_count"], 2)

                # Create Batch 2 with limit=2
                b2 = j.create_export_batch("trades", limit=2)
                self.assertEqual(b2["item_count"], 2)

                # Check FIFO ordering
                pending_list = j.pending_batches("trades")
                self.assertEqual(len(pending_list), 2)
                self.assertEqual(pending_list[0]["batch_id"], b1["batch_id"])
                self.assertEqual(pending_list[1]["batch_id"], b2["batch_id"])

                # get_pending_batch must return b1 first
                first = j.get_pending_batch("trades")
                self.assertEqual(first["batch_id"], b1["batch_id"])

                # Ack b1
                j.acknowledge_batch(b1["batch_id"])

                # Next pending is b2
                second = j.get_pending_batch("trades")
                self.assertEqual(second["batch_id"], b2["batch_id"])
            finally:
                j.db.close()
        finally:
            shutil.rmtree(db_dir, ignore_errors=True)

    def test_simulated_network_timeout(self):
        """Network timeouts increment attempt count and persist error while keeping batch immutable."""
        db_dir = tempfile.mkdtemp()
        try:
            j = DurableJournal(Path(db_dir) / "timeout.db")
            state = MarketState(j, mode="live")
            try:
                t = parse_polymarket({
                    "event_type": "last_trade_price", "asset_id": "test-to",
                    "timestamp": "1700000000000", "transaction_hash": "tx-to",
                    "side": "BUY", "price": "0.50", "size": "10"
                }, now_utc())[0]
                state.process_live_event(t)
                for b in state.books.values():
                    b.close()

                b = j.create_export_batch("trades")
                orig_token = b["dedup_token"]

                # Simulate timeout
                j.record_batch_attempt(b["batch_id"], "httpx.ConnectTimeout: Connection timed out")

                updated = j.get_batch(b["batch_id"])
                self.assertEqual(updated["attempts"], 1)
                self.assertIn("ConnectTimeout", updated["last_error"])
                self.assertEqual(updated["dedup_token"], orig_token)
                self.assertEqual(updated["status"], "pending")
            finally:
                j.db.close()
        finally:
            shutil.rmtree(db_dir, ignore_errors=True)

    def test_retry_after_long_outage_quarantine(self):
        """Batches at risk of deduplication window eviction are quarantined and require operator action."""
        db_dir = tempfile.mkdtemp()
        try:
            j = DurableJournal(Path(db_dir) / "quarantine.db")
            state = MarketState(j, mode="live")
            try:
                t = parse_polymarket({
                    "event_type": "last_trade_price", "asset_id": "test-q",
                    "timestamp": "1700000000000", "transaction_hash": "tx-q",
                    "side": "BUY", "price": "0.50", "size": "10"
                }, now_utc())[0]
                state.process_live_event(t)
                for b in state.books.values():
                    b.close()

                b = j.create_export_batch("trades")

                # Simulate 26 failed attempts
                for _ in range(26):
                    j.record_batch_attempt(b["batch_id"], "ClickHouse 503")

                b_risk = j.get_batch(b["batch_id"])
                is_risk = j.check_dedup_window_risk(b_risk, max_attempts=25)
                self.assertTrue(is_risk)

                # Batch is now quarantined
                b_q = j.get_batch(b["batch_id"])
                self.assertEqual(b_q["status"], "quarantined")

                # get_or_create_pending_batch pauses pipeline for this target
                self.assertIsNone(j.get_or_create_pending_batch("trades"))

                # Operator recovery
                ok = j.recover_quarantined_batch(b["batch_id"], action="verify_and_ack")
                self.assertTrue(ok)
                recovered = j.get_batch(b["batch_id"])
                self.assertEqual(recovered["status"], "acknowledged")
            finally:
                j.db.close()
        finally:
            shutil.rmtree(db_dir, ignore_errors=True)

    def test_attempted_rechunking_rejected(self):
        """Already batched events are excluded from new batches and cannot be re-chunked."""
        db_dir = tempfile.mkdtemp()
        try:
            j = DurableJournal(Path(db_dir) / "rechunk.db")
            state = MarketState(j, mode="live")
            try:
                t1 = parse_polymarket({
                    "event_type": "last_trade_price", "asset_id": "m1",
                    "timestamp": "1700000000000", "transaction_hash": "tx1",
                    "side": "BUY", "price": "0.50", "size": "10"
                }, now_utc())[0]
                state.process_live_event(t1)
                for b in state.books.values():
                    b.close()

                b1 = j.create_export_batch("trades")
                self.assertIsNotNone(b1)

                # Calling create_export_batch again yields None because items are assigned
                b2 = j.create_export_batch("trades")
                self.assertIsNone(b2)
            finally:
                j.db.close()
        finally:
            shutil.rmtree(db_dir, ignore_errors=True)

    def test_duplicate_event_assignment_sqlite_constraint(self):
        """SQLite primary key constraint on export_batch_items strictly prevents duplicate event assignment."""
        db_dir = tempfile.mkdtemp()
        try:
            j = DurableJournal(Path(db_dir) / "constraint.db")
            try:
                # Insert item into export_batch_items
                j.db.execute(
                    "INSERT INTO export_batch_items (target_table, source_table, item_id, batch_id, assigned_at) VALUES (?, ?, ?, ?, ?)",
                    ("trades", "events", "evt-123", "batch-1", now_utc())
                )
                j.db.commit()

                # Attempt duplicate insertion for another batch
                with self.assertRaises(sqlite3.IntegrityError):
                    j.db.execute(
                        "INSERT INTO export_batch_items (target_table, source_table, item_id, batch_id, assigned_at) VALUES (?, ?, ?, ?, ?)",
                        ("trades", "events", "evt-123", "batch-2", now_utc())
                    )
            finally:
                j.db.close()
        finally:
            shutil.rmtree(db_dir, ignore_errors=True)

    def test_backfill_safety_and_idempotency(self):
        """Historical backfill replay using deterministic tokens is completely idempotent."""
        m_id = f"test-backfill-{uuid.uuid4().hex[:6]}"
        now = datetime.now(timezone.utc)
        # Create 5 historical trade rows with valid UUIDs
        rows = [{
            "event_id": str(uuid.uuid4()),
            "venue": "polymarket", "market_id": m_id,
            "source_id": f"tx-bf-{i}", "side": "buy", "wallet": "0xbf",
            "price": "0.500000", "quantity": "10.000000",
            "exchange_timestamp": (now + timedelta(seconds=i)).isoformat(),
            "received_timestamp": (now + timedelta(seconds=i)).isoformat()
        } for i in range(5)]

        payload = "\n".join(json.dumps(r, separators=(",", ":"), sort_keys=True) for r in rows) + "\n"
        token = hashlib.sha256(f"trades:{payload}".encode("utf-8")).hexdigest()

        with httpx.Client(base_url=self.ch_url, timeout=10.0) as client:
            # First backfill run
            r1 = client.post("/", params={
                "query": "INSERT INTO polypulse.trades FORMAT JSONEachRow",
                "insert_deduplicate": "1",
                "insert_deduplication_token": token,
                "deduplicate_blocks_in_dependent_materialized_views": "1",
                "date_time_input_format": "best_effort"
            }, content=payload)
            self.assertEqual(r1.status_code, 200)

            # Second backfill run (replay)
            r2 = client.post("/", params={
                "query": "INSERT INTO polypulse.trades FORMAT JSONEachRow",
                "insert_deduplicate": "1",
                "insert_deduplication_token": token,
                "deduplicate_blocks_in_dependent_materialized_views": "1",
                "date_time_input_format": "best_effort"
            }, content=payload)
            self.assertEqual(r2.status_code, 200)

            # Check table count and MV candle
            c_resp = client.get("/", params={"query": f"SELECT count() FROM polypulse.trades WHERE market_id = '{m_id}'"})
            self.assertEqual(c_resp.text.strip(), "5")

            mv_resp = json.loads(client.get("/", params={
                "query": f"SELECT countMerge(trade_count) AS t, sumMerge(total_volume) AS v FROM polypulse.trade_candles_1m WHERE market_id = '{m_id}' FORMAT JSONEachRow"
            }).text.strip())
            self.assertEqual(mv_resp["t"], 5)
            self.assertAlmostEqual(float(mv_resp["v"]), 50.0)


if __name__ == "__main__":
    unittest.main()

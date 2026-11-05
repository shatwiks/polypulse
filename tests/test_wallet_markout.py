"""Tests for PolyPulse Phase 5: Wallet Intelligence and Markout Analytics Engine."""
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
import shutil
import tempfile
import unittest

from backend.events import NormalizedEvent, now_utc, utc_timestamp
from backend.wallet import (
    WalletTrade, extract_wallet_trades, create_demo_wallet_trade, normalize_address
)
from backend.markout import (
    TradeMarkout, CURRENT_CALC_VERSION, DEFAULT_HORIZONS,
    calculate_markout_metrics, validate_snapshot_quotes, evaluate_markout
)
from backend.durable import DurableJournal


class TestAddressNormalization(unittest.TestCase):
    def test_valid_lowercase_and_uppercase(self):
        raw = "0x1234567890abcdef1234567890abcdef12345678"
        self.assertEqual(normalize_address(raw), raw.lower())
        self.assertEqual(normalize_address(raw.upper()), raw.lower())

    def test_evm_topic_32_byte_padding_stripped(self):
        topic = "0x000000000000000000000000e111180000d2663c0091e4f400237545b87b996b"
        expected = "0xe111180000d2663c0091e4f400237545b87b996b"
        self.assertEqual(normalize_address(topic), expected)

    def test_invalid_address_raises(self):
        with self.assertRaises(ValueError):
            normalize_address("0xinvalid")
        with self.assertRaises(ValueError):
            normalize_address("1234")
        with self.assertRaises(ValueError):
            normalize_address("")


class TestWalletTradeExtraction(unittest.TestCase):
    def test_polygon_v2_fill_extracts_maker_and_taker(self):
        tx_hash = "0xabc123def456"
        log = {
            "transactionHash": tx_hash,
            "logIndex": "0xa",
            "blockNumber": "0x12345"
        }
        decoded = {
            "maker": "0x1111111111111111111111111111111111111111",
            "taker": "0x2222222222222222222222222222222222222222",
            "order_hash": "0x999",
            "fee_units": 1000,
            "token_id": "1001"
        }
        event = NormalizedEvent(
            venue="polygon",
            market_id="1001",
            event_type="fill",
            received_timestamp="2026-03-31T12:00:00.000Z",
            raw_payload={"log": log, "decoded": decoded},
            exchange_timestamp="2026-03-31T12:00:00.000Z",
            source_id=f"{tx_hash}:10",
            price="0.550000",
            quantity="100.000000",
            side="buy"  # maker bought tokens
        )

        trades = extract_wallet_trades(event)
        self.assertEqual(len(trades), 2)

        maker_trade = trades[0]
        self.assertEqual(maker_trade.attribution_type, "maker")
        self.assertEqual(maker_trade.wallet_address, decoded["maker"])
        self.assertEqual(maker_trade.side, "buy")
        self.assertEqual(maker_trade.trade_id, f"polygon:{tx_hash}:10:maker")
        self.assertEqual(maker_trade.price, "0.550000")
        self.assertEqual(maker_trade.quantity, "100.000000")
        self.assertEqual(maker_trade.notional, "55.000000")

        taker_trade = trades[1]
        self.assertEqual(taker_trade.attribution_type, "taker")
        self.assertEqual(taker_trade.wallet_address, decoded["taker"])
        self.assertEqual(taker_trade.side, "sell")  # taker has opposite side
        self.assertEqual(taker_trade.trade_id, f"polygon:{tx_hash}:10:taker")
        self.assertEqual(taker_trade.notional, "55.000000")

    def test_polymarket_public_trade_zero_fabrication(self):
        event = NormalizedEvent(
            venue="polymarket",
            market_id="token-abc",
            event_type="trade",
            received_timestamp="2026-03-31T12:00:00.000Z",
            raw_payload={"price": "0.50", "size": "10"},
            price="0.500000",
            side="buy"
        )
        self.assertEqual(extract_wallet_trades(event), [])

    def test_demo_trade_creation(self):
        trade_dict = {
            "trade_id": "test-123",
            "wallet": "0x3333333333333333333333333333333333333333",
            "venue": "polymarket",
            "market_id": "mkt-1",
            "side": "buy",
            "price": 0.60,
            "quantity": 50,
            "executed_at": 1774958400.0
        }
        wt = create_demo_wallet_trade(trade_dict)
        self.assertIsNotNone(wt)
        self.assertEqual(wt.trade_id, "demo:test-123")
        self.assertEqual(wt.wallet_address, "0x3333333333333333333333333333333333333333")
        self.assertEqual(wt.attribution_type, "synthetic")
        self.assertEqual(wt.side, "buy")
        self.assertEqual(wt.price, "0.600000")
        self.assertEqual(wt.quantity, "50.000000")
        self.assertEqual(wt.notional, "30.000000")


class TestMarkoutMathematics(unittest.TestCase):
    def test_buy_markout_and_bps(self):
        # Executed buy at 0.50, future mid moved to 0.55 (+0.05 / +500 bps)
        markout, bps = calculate_markout_metrics(0.50, "buy", 0.55)
        self.assertAlmostEqual(markout, 0.05, places=5)
        self.assertAlmostEqual(bps, 1000.0, places=2)  # (0.05 / 0.50) * 10,000 = 1000 bps

    def test_sell_markout_and_bps(self):
        # Executed sell at 0.50, future mid moved down to 0.45 (+0.05 favorable for seller)
        markout, bps = calculate_markout_metrics(0.50, "sell", 0.45)
        self.assertAlmostEqual(markout, 0.05, places=5)
        self.assertAlmostEqual(bps, 1000.0, places=2)

        # Executed sell at 0.50, future mid rose to 0.55 (-0.05 adverse selection for seller)
        neg_markout, neg_bps = calculate_markout_metrics(0.50, "sell", 0.55)
        self.assertAlmostEqual(neg_markout, -0.05, places=5)
        self.assertAlmostEqual(neg_bps, -1000.0, places=2)

    def test_reject_crossed_and_locked_books(self):
        # Crossed book: best_ask < best_bid
        status, mid = validate_snapshot_quotes(0.52, 0.50, valid_flag=1)
        self.assertEqual(status, "crossed")
        self.assertIsNone(mid)

        # Locked book: best_ask == best_bid
        status_locked, mid_locked = validate_snapshot_quotes(0.50, 0.50, valid_flag=1)
        self.assertEqual(status_locked, "crossed")
        self.assertIsNone(mid_locked)

        # Non-positive quote
        status_zero, mid_zero = validate_snapshot_quotes(0.0, 0.50, valid_flag=1)
        self.assertEqual(status_zero, "crossed")
        self.assertIsNone(mid_zero)

        # Valid normal book
        status_ok, mid_ok = validate_snapshot_quotes(0.48, 0.52, valid_flag=1)
        self.assertEqual(status_ok, "available")
        self.assertAlmostEqual(mid_ok, 0.50, places=4)


class TestDurableJournalWalletIntelligence(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_wallet.db")
        self.journal = DurableJournal(self.db_path)

    def tearDown(self):
        self.journal.db.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_record_and_query_wallet_trades(self):
        addr = "0x4444444444444444444444444444444444444444"
        trade = {
            "trade_id": "t1",
            "wallet": addr,
            "venue": "polygon",
            "market_id": "m1",
            "side": "buy",
            "price": 0.50,
            "quantity": 100,
            "executed_at": 1774958400.0
        }
        self.journal.add_trade(trade)

        trades = self.journal.get_wallet_trades(addr)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["wallet_address"], addr)
        self.assertEqual(trades[0]["side"], "buy")

        profile = self.journal.get_wallet_profile(addr)
        self.assertEqual(profile["trade_count"], 1)
        self.assertEqual(profile["market_count"], 1)
        self.assertAlmostEqual(profile["total_volume"], 100.0)

    def test_markout_calculation_with_tolerance_window(self):
        addr = "0x5555555555555555555555555555555555555555"
        base_time = 1774958400.0

        # Record trade at base_time
        self.journal.add_trade({
            "trade_id": "t_markout",
            "wallet": addr,
            "venue": "polygon",
            "market_id": "m1",
            "side": "buy",
            "price": 0.50,
            "quantity": 10,
            "executed_at": base_time
        })

        # Insert snapshot at base_time + 5s with mid = 0.55
        snap_view = {
            "best_bid": 0.54,
            "best_ask": 0.56,
            "bids": [[540000, 1000000]],
            "asks": [[560000, 1000000]],
            "valid": 1
        }
        self.journal.db.execute("""
            INSERT INTO book_observations (venue, market_id, recorded_at, sequence, payload)
            VALUES (?, ?, ?, ?, ?)
        """, ("polygon", "m1", base_time + 5.2, 1, json.dumps(snap_view)))
        self.journal.db.commit()

        # Compute markouts for 5s horizon
        res = self.journal.compute_and_store_markouts("demo:t_markout", horizons=[5])
        self.assertEqual(len(res), 1)
        m5 = res[0]
        self.assertEqual(m5["status"], "available")
        self.assertAlmostEqual(m5["future_mid"], 0.55, places=4)
        self.assertAlmostEqual(m5["markout"], 0.05, places=4)
        self.assertAlmostEqual(m5["markout_bps"], 1000.0, places=2)

    def test_crossed_orderbook_rejection_in_markouts(self):
        addr = "0x6666666666666666666666666666666666666666"
        base_time = 1774958400.0

        self.journal.add_trade({
            "trade_id": "t_crossed",
            "wallet": addr,
            "venue": "polygon",
            "market_id": "m2",
            "side": "buy",
            "price": 0.50,
            "quantity": 10,
            "executed_at": base_time
        })

        # Insert crossed snapshot at base_time + 5s (best_bid = 0.55 > best_ask = 0.52)
        crossed_view = {
            "best_bid": 0.55,
            "best_ask": 0.52,
            "bids": [[550000, 1000000]],
            "asks": [[520000, 1000000]],
            "valid": 1
        }
        self.journal.db.execute("""
            INSERT INTO book_observations (venue, market_id, recorded_at, sequence, payload)
            VALUES (?, ?, ?, ?, ?)
        """, ("polygon", "m2", base_time + 5.1, 1, json.dumps(crossed_view)))
        self.journal.db.commit()

        res = self.journal.compute_and_store_markouts("demo:t_crossed", horizons=[5])
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["status"], "crossed")
        self.assertIsNone(res[0]["future_mid"])
        self.assertIsNone(res[0]["markout"])

    def test_ranking_minimum_sample_size_threshold(self):
        w_small = "0x1111111111111111111111111111111111111111"
        w_large = "0x2222222222222222222222222222222222222222"
        base_time = 1774958400.0

        # w_small has only 2 trades
        for i in range(2):
            self.journal.add_trade({
                "trade_id": f"small_{i}", "wallet": w_small, "venue": "polygon",
                "market_id": "m1", "side": "buy", "price": 0.50, "quantity": 10,
                "executed_at": base_time + i * 10
            })

        # w_large has 6 trades
        for i in range(6):
            self.journal.add_trade({
                "trade_id": f"large_{i}", "wallet": w_large, "venue": "polygon",
                "market_id": "m1", "side": "buy", "price": 0.50, "quantity": 10,
                "executed_at": base_time + i * 10
            })

        rankings = self.journal.get_wallet_rankings(horizon=30, min_trades=5)
        self.assertEqual(len(rankings), 2)

        # w_large should be qualified and ranked first
        self.assertEqual(rankings[0]["wallet"], w_large)
        self.assertTrue(rankings[0]["qualified"])
        self.assertEqual(rankings[0]["sample_status"], "qualified")

        # w_small should be insufficient_sample
        self.assertEqual(rankings[1]["wallet"], w_small)
        self.assertFalse(rankings[1]["qualified"])
        self.assertEqual(rankings[1]["sample_status"], "insufficient_sample")

    def test_recompute_with_version_preservation(self):
        addr = "0x7777777777777777777777777777777777777777"
        base_time = 1774958400.0

        self.journal.add_trade({
            "trade_id": "t_version",
            "wallet": addr,
            "venue": "polygon",
            "market_id": "m1",
            "side": "buy",
            "price": 0.50,
            "quantity": 10,
            "executed_at": base_time
        })

        snap_view = {"best_bid": 0.51, "best_ask": 0.53, "valid": 1}
        self.journal.db.execute("""
            INSERT INTO book_observations (venue, market_id, recorded_at, sequence, payload)
            VALUES (?, ?, ?, ?, ?)
        """, ("polygon", "m1", base_time + 5.0, 1, json.dumps(snap_view)))
        self.journal.db.commit()

        # Compute v1.0
        res_v1 = self.journal.compute_and_store_markouts("demo:t_version", horizons=[5], calc_version="v1.0")
        self.assertEqual(res_v1[0]["calc_version"], "v1.0")

        # Compute v2.0
        res_v2 = self.journal.compute_and_store_markouts("demo:t_version", horizons=[5], calc_version="v2.0")
        self.assertEqual(res_v2[0]["calc_version"], "v2.0")

        # Both records should exist independently in trade_markouts
        rows = self.journal.db.execute(
            "SELECT calc_version FROM trade_markouts WHERE trade_id = 'demo:t_version' AND horizon_seconds = 5"
        ).fetchall()
        versions = {r[0] for r in rows}
        self.assertEqual(versions, {"v1.0", "v2.0"})

    def test_outbox_batch_creation_for_wallet_tables(self):
        addr = "0x8888888888888888888888888888888888888888"
        base_time = 1774958400.0

        self.journal.add_trade({
            "trade_id": "t_outbox",
            "wallet": addr,
            "venue": "polygon",
            "market_id": "m1",
            "side": "buy",
            "price": 0.50,
            "quantity": 10,
            "executed_at": base_time
        })

        # Test wallet_trades export batch
        batch = self.journal.create_export_batch("wallet_trades")
        self.assertIsNotNone(batch)
        self.assertEqual(batch["target_table"], "wallet_trades")
        self.assertEqual(batch["item_count"], 1)
        self.assertIn("0x8888888888888888888888888888888888888888", batch["payload"])

        # Acknowledge batch
        ack = self.journal.acknowledge_batch(batch["batch_id"])
        self.assertTrue(ack)

        # Confirm exported flag in SQLite
        row = self.journal.db.execute("SELECT exported FROM wallet_trades WHERE wallet_address = ?", (addr,)).fetchone()
        self.assertEqual(row[0], 1)


class TestFastAPIWalletEndpoints(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        from backend.main import app
        from backend.state import MarketState

        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_api_wallet.db")
        self.journal = DurableJournal(self.db_path)
        self.state = MarketState(self.journal, [], mode="demo")

        app.state.market = self.state
        self.client = TestClient(app)

        # Seed sample trades
        self.addr = "0x9999999999999999999999999999999999999999"
        base_time = 1774958400.0
        for i in range(5):
            self.journal.add_trade({
                "trade_id": f"api_trade_{i}",
                "wallet": self.addr,
                "venue": "polygon",
                "market_id": "m_test",
                "side": "buy",
                "price": 0.50,
                "quantity": 20,
                "executed_at": base_time + i * 5
            })

        # Seed snapshot
        snap = {"best_bid": 0.52, "best_ask": 0.54, "valid": 1}
        self.journal.db.execute("""
            INSERT INTO book_observations (venue, market_id, recorded_at, sequence, payload)
            VALUES (?, ?, ?, ?, ?)
        """, ("polygon", "m_test", base_time + 5.0, 1, json.dumps(snap)))
        self.journal.db.commit()

    def tearDown(self):
        self.journal.db.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_wallets_list_endpoint(self):
        resp = self.client.get("/api/v1/wallets")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(len(data) >= 1)
        wallets = [w["wallet"] for w in data]
        self.assertIn(self.addr, wallets)

    def test_wallet_profile_endpoint(self):
        resp = self.client.get(f"/api/v1/wallets/{self.addr}")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["wallet_address"], self.addr)
        self.assertEqual(data["trade_count"], 5)
        self.assertAlmostEqual(data["total_volume"], 100.0)

    def test_wallet_trades_endpoint(self):
        resp = self.client.get(f"/api/v1/wallets/{self.addr}/trades?limit=10")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(len(data), 5)
        self.assertEqual(data[0]["wallet_address"], self.addr)

    def test_wallet_markouts_endpoint(self):
        resp = self.client.get(f"/api/v1/wallets/{self.addr}/markouts")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["wallet"], self.addr)
        self.assertEqual(len(data["summary"]), 3)  # 5s, 30s, 60s
        self.assertTrue("average_markout" in data["summary"][0])

    def test_wallet_performance_endpoint(self):
        resp = self.client.get(f"/api/v1/wallets/{self.addr}/performance")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["wallet"], self.addr)
        self.assertIn("profile", data)
        self.assertIn("horizons", data)
        self.assertIn("by_side", data)

    def test_wallet_rankings_endpoint(self):
        resp = self.client.get("/api/v1/analytics/wallet-rankings?min_trades=5")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(len(data) >= 1)
        first = data[0]
        self.assertTrue(first["qualified"])
        self.assertEqual(first["sample_status"], "qualified")

    def test_markout_distribution_endpoint(self):
        resp = self.client.get("/api/v1/analytics/markout-distribution?horizon=30")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["horizon_seconds"], 30)
        self.assertIn("quantiles", data)

    def test_invalid_address_returns_422(self):
        resp = self.client.get("/api/v1/wallets/not-an-address")
        self.assertEqual(resp.status_code, 422)


if __name__ == "__main__":
    unittest.main()

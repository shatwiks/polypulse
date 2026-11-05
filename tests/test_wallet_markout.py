"""Tests for PolyPulse Phase 5: Wallet Intelligence and Markout Analytics Engine.

Comprehensive audit test suite verifying:
1. Address normalization and EIP-55 checksum formatting.
2. Polygon CTF Exchange V2 event semantics (maker fill vs taker summary).
3. Match types: Complementary, MINT (both buyers), and MERGE (both sellers).
4. Multiple maker orders matched by one taker.
5. Exchange contract address exclusion.
6. Unresolved match structure rejection (zero synthetic trades).
7. Uint256 token ID decimal string preservation.
8. Deterministic collision-resistant trade IDs.
9. Markout timing: observation tolerance, quote freshness (stale), quote invariants (crossed),
   and pending vs finalized unavailable thresholds.
10. Horizon-specific sample size qualification guardrails (N >= 5 applied per horizon).
11. Execution-time partitioning (stable partition across month boundaries) and calc_version retention.
12. FastAPI Phase 5 analytics endpoints.
"""
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
import shutil
import tempfile
import unittest

from backend.events import NormalizedEvent, now_utc, utc_timestamp
from backend.wallet import (
    WalletTrade, extract_wallet_trades, create_demo_wallet_trade,
    normalize_address, to_checksum_address, CTF_EXCHANGE_V2,
    NEG_RISK_EXCHANGE_V2, NEG_RISK_ADAPTER, KNOWN_EXCHANGES, ZERO_ADDRESS
)
from backend.markout import (
    TradeMarkout, CURRENT_CALC_VERSION, DEFAULT_HORIZONS,
    calculate_markout_metrics, validate_snapshot_quotes, evaluate_markout
)
from backend.polygon import ORDER_FILLED_V2_TOPIC, decode_v2_fill
from backend.durable import DurableJournal
from backend.state import MarketState
from fastapi.testclient import TestClient
from backend.main import app, state


class TestAddressNormalizationAndEIP55(unittest.TestCase):
    def test_eip55_checksum_test_vectors(self):
        # Official Ethereum EIP-55 test vectors
        vectors = [
            "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed",
            "0xfB6916095ca1df60bB79Ce92cE3Ea74c37c5d359",
            "0xdbF03B407c01E7cD3CBea99509d93f8DDDC8C6FB",
            "0xD1220A0cf47c7B9Be7A2E6BA89F429762e7b9aDb"
        ]
        for v in vectors:
            self.assertEqual(to_checksum_address(v.lower()), v)
            self.assertEqual(to_checksum_address(v.upper()), v)

    def test_lowercase_normalized_separate_from_checksum(self):
        mixed = "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed"
        self.assertEqual(normalize_address(mixed), mixed.lower())
        self.assertEqual(to_checksum_address(mixed), mixed)

    def test_valid_topic_padding_stripped(self):
        topic = "0x0000000000000000000000005aaeb6053f3e94c9b9a09f33669435e7ef1beaed"
        expected = "0x5aaeb6053f3e94c9b9a09f33669435e7ef1beaed"
        self.assertEqual(normalize_address(topic), expected)

    def test_malformed_topic_padding_rejected(self):
        # Non-zero byte in leading 12 zero bytes
        malformed = "0x0000000000000000000000015aaeb6053f3e94c9b9a09f33669435e7ef1beaed"
        with self.assertRaises(ValueError):
            normalize_address(malformed)

    def test_invalid_address_formats_rejected(self):
        with self.assertRaises(ValueError):
            normalize_address("0xinvalid")
        with self.assertRaises(ValueError):
            normalize_address("1234")
        with self.assertRaises(ValueError):
            normalize_address("")
        with self.assertRaises(ValueError):
            normalize_address("0x" + "a" * 38)  # 19 bytes
        with self.assertRaises(ValueError):
            normalize_address("0x" + "a" * 42)  # 21 bytes


class TestV2EventSemanticsAndAttribution(unittest.TestCase):
    def setUp(self):
        self.tx_hash = "0xabc123def4567890abc123def4567890abc123def4567890abc123def4567890"
        self.maker_addr = "0x1111111111111111111111111111111111111111"
        self.taker_addr = "0x2222222222222222222222222222222222222222"

    def test_complementary_matching_maker_taker(self):
        log = {"transactionHash": self.tx_hash, "logIndex": "0xa", "blockNumber": "0x100", "address": CTF_EXCHANGE_V2}
        decoded = {
            "maker": self.maker_addr,
            "taker": self.taker_addr,
            "order_hash": "0x999",
            "fee_units": 1000,
            "token_id": "1001",
            "match_type": "complementary",
            "chain_id": 137,
            "timestamp_source": "block_header"
        }
        event = NormalizedEvent(
            venue="polygon", market_id="1001", event_type="fill",
            received_timestamp="2026-03-31T12:00:00.000Z",
            raw_payload={"log": log, "decoded": decoded},
            exchange_timestamp="2026-03-31T12:00:00.000Z",
            source_id=f"{self.tx_hash}:10", price="0.600000", quantity="100.000000", side="buy"
        )
        trades = extract_wallet_trades(event)
        self.assertEqual(len(trades), 2)
        
        maker, taker = trades[0], trades[1]
        self.assertEqual(maker.attribution_type, "maker")
        self.assertEqual(maker.side, "buy")
        self.assertEqual(maker.token_id, "1001")
        self.assertEqual(maker.price, "0.600000")
        self.assertEqual(maker.match_type, "complementary")

        self.assertEqual(taker.attribution_type, "taker")
        self.assertEqual(taker.side, "sell")  # Opposite side for complementary
        self.assertEqual(taker.token_id, "1001")
        self.assertEqual(taker.price, "0.600000")
        self.assertEqual(taker.match_type, "complementary")

    def test_mint_matching_both_participants_are_buyers(self):
        # MINT matching: Both maker and taker want to BUY complementary tokens.
        # Exchange mints a complete set.
        token_a = "21742633143463906290569050155826241533067272736897614950488156847949938836455"
        token_b = "21742633143463906290569050155826241533067272736897614950488156847949938836456"
        log = {"transactionHash": self.tx_hash, "logIndex": "0xb", "blockNumber": "0x100", "address": CTF_EXCHANGE_V2}
        decoded = {
            "maker": self.maker_addr,
            "taker": self.taker_addr,
            "maker_token_id": token_a,
            "taker_token_id": token_b,
            "match_type": "mint",
            "chain_id": 137,
            "condition_id": "0xcond123"
        }
        event = NormalizedEvent(
            venue="polygon", market_id=token_a, event_type="fill",
            received_timestamp="2026-03-31T12:00:00.000Z",
            raw_payload={"log": log, "decoded": decoded},
            exchange_timestamp="2026-03-31T12:00:00.000Z",
            source_id=f"{self.tx_hash}:11", price="0.650000", quantity="50.000000", side="buy"
        )
        trades = extract_wallet_trades(event)
        self.assertEqual(len(trades), 2)
        
        maker, taker = trades[0], trades[1]
        # Maker buys Token A at 0.65
        self.assertEqual(maker.attribution_type, "maker")
        self.assertEqual(maker.side, "buy")
        self.assertEqual(maker.token_id, token_a)
        self.assertEqual(maker.price, "0.650000")
        self.assertEqual(maker.match_type, "mint")

        # Taker buys Token B at 1.0 - 0.65 = 0.35!
        self.assertEqual(taker.attribution_type, "taker")
        self.assertEqual(taker.side, "buy")
        self.assertEqual(taker.token_id, token_b)
        self.assertEqual(taker.price, "0.350000")
        self.assertEqual(taker.match_type, "mint")

    def test_merge_matching_both_participants_are_sellers(self):
        # MERGE matching: Both maker and taker want to SELL complementary tokens.
        token_a = "1001"
        token_b = "1002"
        log = {"transactionHash": self.tx_hash, "logIndex": "0xc", "blockNumber": "0x100", "address": CTF_EXCHANGE_V2}
        decoded = {
            "maker": self.maker_addr,
            "taker": self.taker_addr,
            "maker_token_id": token_a,
            "taker_token_id": token_b,
            "match_type": "merge",
            "chain_id": 137
        }
        event = NormalizedEvent(
            venue="polygon", market_id=token_a, event_type="fill",
            received_timestamp="2026-03-31T12:00:00.000Z",
            raw_payload={"log": log, "decoded": decoded},
            exchange_timestamp="2026-03-31T12:00:00.000Z",
            source_id=f"{self.tx_hash}:12", price="0.400000", quantity="50.000000", side="sell"
        )
        trades = extract_wallet_trades(event)
        self.assertEqual(len(trades), 2)
        
        maker, taker = trades[0], trades[1]
        self.assertEqual(maker.side, "sell")
        self.assertEqual(maker.token_id, token_a)
        self.assertEqual(maker.price, "0.400000")
        self.assertEqual(maker.match_type, "merge")

        self.assertEqual(taker.side, "sell")
        self.assertEqual(taker.token_id, token_b)
        self.assertEqual(taker.price, "0.600000")  # 1.0 - 0.40
        self.assertEqual(taker.match_type, "merge")

    def test_multiple_maker_orders_matched_by_one_taker(self):
        # 1 taker order fills against 2 different makers in the same transaction
        maker_1 = "0x1111111111111111111111111111111111111111"
        maker_2 = "0x3333333333333333333333333333333333333333"
        taker = self.taker_addr

        event1 = NormalizedEvent(
            venue="polygon", market_id="1001", event_type="fill",
            received_timestamp="2026-03-31T12:00:00.000Z",
            raw_payload={"log": {"transactionHash": self.tx_hash, "logIndex": "0x1", "address": CTF_EXCHANGE_V2},
                         "decoded": {"maker": maker_1, "taker": taker, "token_id": "1001", "match_type": "complementary"}},
            source_id=f"{self.tx_hash}:1", price="0.500000", quantity="50.000000", side="buy"
        )
        event2 = NormalizedEvent(
            venue="polygon", market_id="1001", event_type="fill",
            received_timestamp="2026-03-31T12:00:00.000Z",
            raw_payload={"log": {"transactionHash": self.tx_hash, "logIndex": "0x2", "address": CTF_EXCHANGE_V2},
                         "decoded": {"maker": maker_2, "taker": taker, "token_id": "1001", "match_type": "complementary"}},
            source_id=f"{self.tx_hash}:2", price="0.510000", quantity="50.000000", side="buy"
        )

        trades1 = extract_wallet_trades(event1)
        trades2 = extract_wallet_trades(event2)

        trade_ids = [t.trade_id for t in trades1 + trades2]
        self.assertEqual(len(trade_ids), 4)
        self.assertEqual(len(set(trade_ids)), 4, "All 4 trades must have collision-free deterministic IDs")

    def test_taker_summary_event_excluded(self):
        # In a summary event, taker is the exchange contract address
        event = NormalizedEvent(
            venue="polygon", market_id="1001", event_type="fill",
            received_timestamp="2026-03-31T12:00:00.000Z",
            raw_payload={
                "log": {"transactionHash": self.tx_hash, "logIndex": "0x5", "address": CTF_EXCHANGE_V2},
                "decoded": {
                    "maker": self.maker_addr,
                    "taker": CTF_EXCHANGE_V2.lower(),  # Exchange is taker
                    "token_id": "1001",
                    "classification": "taker_summary"
                }
            },
            source_id=f"{self.tx_hash}:5", price="0.500000", quantity="100.000000", side="buy"
        )
        trades = extract_wallet_trades(event)
        self.assertEqual(trades, [], "Taker summary events must be excluded from wallet attribution")

    def test_exchange_contract_address_excluded_as_wallet(self):
        # Exchange contracts must never be attributed as trading wallets
        with self.assertRaises(ValueError):
            WalletTrade(
                trade_id="test", event_id="ev1",
                wallet_address=CTF_EXCHANGE_V2,  # Exchange cannot be wallet
                venue="polygon", market_id="m1", attribution_type="maker",
                side="buy", price_ticks=500000, quantity_units=1000000,
                price="0.500000", quantity="1.000000", notional="0.500000",
                exchange_timestamp="2026-03-31T12:00:00Z", received_timestamp="2026-03-31T12:00:00Z"
            )

    def test_unresolved_match_type_excluded(self):
        event = NormalizedEvent(
            venue="polygon", market_id="1001", event_type="fill",
            received_timestamp="2026-03-31T12:00:00.000Z",
            raw_payload={
                "log": {"transactionHash": self.tx_hash, "logIndex": "0x6", "address": CTF_EXCHANGE_V2},
                "decoded": {
                    "maker": self.maker_addr,
                    "taker": self.taker_addr,
                    "token_id": "1001",
                    "match_type": "unresolved"
                }
            },
            source_id=f"{self.tx_hash}:6", price="0.500000", quantity="100.000000", side="buy"
        )
        self.assertEqual(extract_wallet_trades(event), [], "Unresolved match types must not generate synthetic trades")

    def test_token_id_uint256_decimal_preservation(self):
        uint256_val = "115792089237316195423570985008687907853269984665640564039457584007913129639935"
        trade = WalletTrade(
            trade_id="uint256_test", event_id="ev_uint",
            wallet_address=self.maker_addr, venue="polygon", market_id=uint256_val,
            attribution_type="maker", side="buy", price_ticks=500000, quantity_units=1000000,
            price="0.500000", quantity="1.000000", notional="0.500000",
            exchange_timestamp="2026-03-31T12:00:00Z", received_timestamp="2026-03-31T12:00:00Z",
            token_id=uint256_val
        )
        self.assertEqual(trade.token_id, uint256_val)
        self.assertEqual(trade.as_clickhouse_row()["token_id"], uint256_val)


class TestMarkoutTimingAndStatuses(unittest.TestCase):
    def setUp(self):
        self.trade_ts = 1774958400.0  # Reference epoch
        self.wallet = "0x1111111111111111111111111111111111111111"

    def test_observation_window_tolerance(self):
        # Horizon 5s -> Target time = trade_ts + 5.0
        # Window: [trade_ts + 4.5, trade_ts + 10.0]
        snapshots = [
            {"snapshot_time": self.trade_ts + 5.2, "best_bid": 0.54, "best_ask": 0.56, "valid": 1}
        ]
        res = evaluate_markout(
            trade_id="t1", wallet_address=self.wallet, venue="polygon",
            market_id="m1", side="buy", exec_price=0.50, exec_timestamp=self.trade_ts,
            horizon_seconds=5, snapshots=snapshots, current_time=self.trade_ts + 15.0
        )
        self.assertEqual(res.status, "available")
        self.assertAlmostEqual(res.future_mid, 0.55, places=4)
        self.assertAlmostEqual(res.markout_bps, 1000.0, places=2)

    def test_stale_quote_status(self):
        # Candidate snapshot exists within tolerance window, but fails quote freshness (age > 10s or is_stale)
        snapshots = [
            {"snapshot_time": self.trade_ts + 5.0, "best_bid": 0.54, "best_ask": 0.56, "valid": 1, "is_stale": True}
        ]
        res = evaluate_markout(
            trade_id="t_stale", wallet_address=self.wallet, venue="polygon",
            market_id="m1", side="buy", exec_price=0.50, exec_timestamp=self.trade_ts,
            horizon_seconds=5, snapshots=snapshots, current_time=self.trade_ts + 15.0
        )
        self.assertEqual(res.status, "stale")
        self.assertIsNone(res.future_mid)
        self.assertIsNone(res.markout)

    def test_crossed_quote_status(self):
        snapshots = [
            {"snapshot_time": self.trade_ts + 5.0, "best_bid": 0.55, "best_ask": 0.50, "valid": 1}
        ]
        res = evaluate_markout(
            trade_id="t_cross", wallet_address=self.wallet, venue="polygon",
            market_id="m1", side="buy", exec_price=0.50, exec_timestamp=self.trade_ts,
            horizon_seconds=5, snapshots=snapshots, current_time=self.trade_ts + 15.0
        )
        self.assertEqual(res.status, "crossed")
        self.assertIsNone(res.future_mid)

    def test_pending_vs_finalized_unavailable_threshold(self):
        # For 60s horizon: target = trade_ts + 60. Finalize threshold = trade_ts + 60 + 5 + 5 = trade_ts + 70.
        # At trade_ts + 30s with no snapshot: status must be "pending"
        res_pending = evaluate_markout(
            trade_id="t_60", wallet_address=self.wallet, venue="polygon",
            market_id="m1", side="buy", exec_price=0.50, exec_timestamp=self.trade_ts,
            horizon_seconds=60, snapshots=[], current_time=self.trade_ts + 30.0
        )
        self.assertEqual(res_pending.status, "pending")

        # After trade_ts + 75s with no snapshot: finalized as "unavailable"
        res_unavailable = evaluate_markout(
            trade_id="t_60", wallet_address=self.wallet, venue="polygon",
            market_id="m1", side="buy", exec_price=0.50, exec_timestamp=self.trade_ts,
            horizon_seconds=60, snapshots=[], current_time=self.trade_ts + 75.0
        )
        self.assertEqual(res_unavailable.status, "unavailable")


class TestLeaderboardQualificationPerHorizon(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_leaderboard.db")
        self.journal = DurableJournal(self.db_path)
        self.wallet = "0x8888888888888888888888888888888888888888"
        self.base_time = 1774958400.0

        # Create 20 trades for this wallet
        for i in range(20):
            t_id = f"demo:t_{i}"
            t_time = self.base_time + i * 100  # distinct time windows
            mkt = f"m_test_{i}"
            self.journal.add_trade({
                "trade_id": f"t_{i}", "wallet": self.wallet, "venue": "polygon",
                "market_id": mkt, "side": "buy", "price": 0.50, "quantity": 10,
                "executed_at": t_time
            })

            # 5s horizon: provide snapshots for 12 trades
            if i < 12:
                self._insert_snapshot(mkt, t_time + 5.0, 0.55, 0.57)
            # 30s horizon: provide snapshots for 7 trades
            if i < 7:
                self._insert_snapshot(mkt, t_time + 30.0, 0.56, 0.58)
            # 60s horizon: provide snapshots for only 3 trades
            if i < 3:
                self._insert_snapshot(mkt, t_time + 60.0, 0.57, 0.59)

    def _insert_snapshot(self, market_id, snap_time, bid, ask):
        view = {"best_bid": bid, "best_ask": ask, "valid": 1}
        self.journal.db.execute("""
            INSERT INTO book_observations (venue, market_id, recorded_at, sequence, payload)
            VALUES (?, ?, ?, ?, ?)
        """, ("polygon", market_id, snap_time, 1, json.dumps(view)))
        self.journal.db.commit()

    def tearDown(self):
        self.journal.db.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_horizon_specific_sample_size_qualification(self):
        # 5-second leaderboard: 12 available >= 5 -> Qualified!
        r5 = self.journal.get_wallet_rankings(horizon=5, min_trades=5)
        self.assertEqual(len(r5), 1)
        w5 = r5[0]
        self.assertEqual(w5["available_count"], 12)
        self.assertTrue(w5["qualified"])
        self.assertEqual(w5["sample_status"], "qualified")

        # 30-second leaderboard: 7 available >= 5 -> Qualified!
        r30 = self.journal.get_wallet_rankings(horizon=30, min_trades=5)
        self.assertEqual(len(r30), 1)
        w30 = r30[0]
        self.assertEqual(w30["available_count"], 7)
        self.assertTrue(w30["qualified"])
        self.assertEqual(w30["sample_status"], "qualified")

        # 60-second leaderboard: only 3 available < 5 -> Disqualified (insufficient_sample)!
        r60 = self.journal.get_wallet_rankings(horizon=60, min_trades=5)
        self.assertEqual(len(r60), 1)
        w60 = r60[0]
        self.assertEqual(w60["available_count"], 3)
        self.assertFalse(w60["qualified"], "Wallet must NOT qualify for 60s leaderboard with N=3 < 5")
        self.assertEqual(w60["sample_status"], "insufficient_sample")

        # Verify all exposed audit fields
        self.assertEqual(w60["total_trades"], 20)
        self.assertIn("unavailable_count", w60)
        self.assertIn("stale_count", w60)
        self.assertIn("crossed_count", w60)
        self.assertIn("avg_markout", w60)
        self.assertIn("avg_markout_bps", w60)
        self.assertIn("positive_percentage", w60)


class TestClickHouseVersioningAndMonthBoundaries(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_partition.db")
        self.journal = DurableJournal(self.db_path)
        self.wallet = "0x9999999999999999999999999999999999999999"
        self.exec_time = 1774958400.0  # May 31, 2026 execution

        self.journal.add_trade({
            "trade_id": "month_trade", "wallet": self.wallet, "venue": "polygon",
            "market_id": "m_partition", "side": "buy", "price": 0.50, "quantity": 10,
            "executed_at": self.exec_time
        })

    def tearDown(self):
        self.journal.db.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_recomputation_preserves_immutable_trade_timestamp(self):
        # Initial calculation
        self.journal.compute_and_store_markouts("demo:month_trade", horizons=[5], calc_version="v1.0")
        row1 = self.journal.db.execute("SELECT * FROM trade_markouts WHERE trade_id = 'demo:month_trade'").fetchone()
        orig_trade_ts = row1["trade_timestamp"]
        self.assertIsNotNone(orig_trade_ts)

        # Force recompute on a subsequent date (simulated)
        self.journal.compute_and_store_markouts("demo:month_trade", horizons=[5], calc_version="v1.0", force=True)
        row2 = self.journal.db.execute("SELECT * FROM trade_markouts WHERE trade_id = 'demo:month_trade'").fetchone()
        
        # trade_timestamp must remain unchanged, ensuring stable monthly partitioning
        self.assertEqual(row2["trade_timestamp"], orig_trade_ts)

    def test_distinct_calc_versions_coexist(self):
        self.journal.compute_and_store_markouts("demo:month_trade", horizons=[5], calc_version="v1.0")
        self.journal.compute_and_store_markouts("demo:month_trade", horizons=[5], calc_version="v2.0")

        rows = self.journal.db.execute("SELECT calc_version FROM trade_markouts WHERE trade_id = 'demo:month_trade'").fetchall()
        versions = [r["calc_version"] for r in rows]
        self.assertIn("v1.0", versions)
        self.assertIn("v2.0", versions)
        self.assertEqual(len(versions), 2, "Different calc_versions must be retained side by side")


class TestFastAPIWalletEndpoints(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_api_wallet.db")
        self.journal = DurableJournal(self.db_path)
        self.state_inst = MarketState(self.journal, mode="demo")
        app.state.market = self.state_inst
        app.state.market_state = self.state_inst
        self.client = TestClient(app)

        self.addr = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        base_time = 1774958400.0

        for i in range(5):
            self.journal.add_trade({
                "trade_id": f"api_t_{i}",
                "wallet": self.addr,
                "venue": "polygon",
                "market_id": "m_test",
                "side": "buy",
                "price": 0.50,
                "quantity": 20,
                "executed_at": base_time + i * 5.0
            })

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
        self.assertEqual(len(data["summary"]), 3)

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

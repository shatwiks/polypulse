import asyncio
import base64
from dataclasses import replace
from decimal import Decimal
import json
import logging
import os
from pathlib import Path
import tempfile
import unittest
from fastapi.testclient import TestClient

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa
from cryptography.hazmat.primitives import hashes

from backend.durable import DurableJournal, export_live_forever
from backend.events import (NormalizedEvent, now_utc, parse_kalshi, parse_polymarket,
                            price_ticks, quantity_units)
from backend.live import (KalshiSequence, reconnect_forever, discover_polymarket_assets,
                          kalshi_headers, kalshi, polymarket, reconcile_polymarket_orderbook)
from backend.polygon import ORDER_FILLED_V2_TOPIC, decode_v2_fill, polygon_fills
from backend.state import MarketState
from backend.main import app
from backend.logging_config import JsonFormatter

STAMP = now_utc()


class Phase2Tests(unittest.TestCase):
    def setUp(self):
        global STAMP
        STAMP = now_utc()
        self.folder = tempfile.TemporaryDirectory()
        self.journal = DurableJournal(Path(self.folder.name) / "journal.db")
        self.state = MarketState(self.journal, mode="live")

    def tearDown(self):
        for book in self.state.books.values():
            book.close()
        self.journal.db.close()
        self.folder.cleanup()

    def poly_snapshot(self):
        return {"event_type": "book", "market": "0xcondition", "asset_id": "101",
            "timestamp": "1793786400000", "hash": "bookhash",
            "bids": [{"price": "0.490001", "size": "1.123456"}],
            "asks": [{"price": "0.510000", "size": "2.000001"}]}

    def test_polymarket_snapshot_and_incremental_trade_parsers(self):
        snapshot = parse_polymarket(self.poly_snapshot(), STAMP)[0]
        self.assertEqual(snapshot.exchange_timestamp, "2026-11-04T10:00:00.000+00:00")
        self.assertEqual(snapshot.bids, ((490001, 1123456),))
        self.assertEqual(snapshot.asks, ((510000, 2000001),))
        self.assertEqual(snapshot.event_id, parse_polymarket(self.poly_snapshot(), now_utc())[0].event_id)
        changes = {"event_type": "price_change", "timestamp": "1793786401000",
            "price_changes": [
                {"asset_id": "101", "side": "BUY", "price": "0.500001", "size": "3.000001"},
                {"asset_id": "102", "side": "SELL", "price": "0.520000", "size": "0"}]}
        parsed = parse_polymarket(changes, STAMP)
        self.assertEqual([(e.market_id,e.side,e.price,e.quantity) for e in parsed],
            [("101","bid","0.500001","3.000001"),("102","ask","0.520000","0")])
        trade = parse_polymarket({"event_type":"last_trade_price","asset_id":"101",
            "side":"SELL","price":"0.49","size":"219.217767","transaction_hash":"0xtx",
            "timestamp":"1793786402000"}, STAMP)[0]
        self.assertEqual((trade.event_type,trade.quantity,trade.source_id), ("trade","219.217767","0xtx"))
        optional_size = parse_polymarket({"event_type":"last_trade_price", "asset_id":"101",
            "side":"BUY", "price":"0.49", "timestamp":"1793786402000"}, STAMP)[0]
        self.assertIsNone(optional_size.quantity)
        with self.assertRaises(ValueError):
            parse_polymarket({"event_type":"price_change","price_changes":[
                {"asset_id":"101","side":"UNKNOWN","price":"0.5","size":"1"}]}, STAMP)

    def test_kalshi_snapshot_delta_normalization_and_sequence(self):
        snapshot_message = {"type":"orderbook_snapshot","sid":2,"seq":10,
            "sending_ts_ms":1793786400000,"msg":{"market_ticker":"KX-X",
            "yes_dollars_fp":[["0.4400","12.000001"]],
            "no_dollars_fp":[["0.5300","7.000002"]]}}
        event = parse_kalshi(snapshot_message, STAMP)
        self.assertEqual((event.bids,event.asks), (((440000,12000001),),((470000,7000002),)))
        self.state.process_live_event(event)
        self.assertEqual(self.state.get("kalshi","KX-X")["best_ask"], .47)
        tracker = KalshiSequence()
        self.assertEqual(tracker.accept(2,10), "ok")
        self.assertEqual(tracker.accept(2,11), "ok")
        self.assertEqual(tracker.accept(2,11), "duplicate")
        self.assertEqual(tracker.accept(2,13), "gap")
        delta_message = {"type":"orderbook_delta","sid":2,"seq":11,
            "sending_ts_ms":1793786401000,"msg":{"market_ticker":"KX-X",
            "side":"no","price_dollars":"0.5300","delta_fp":"-2.000001",
            "ts_ms":1793786401000}}
        delta_event, delta = parse_kalshi(delta_message, STAMP)
        absolute = 7000002 + delta
        self.state.process_live_event(replace(delta_event, quantity=str(Decimal(absolute)/1_000_000)))
        self.assertEqual(self.state.get("kalshi","KX-X")["asks"][0], [.47,5.000001])
        self.state.invalidate_venue("kalshi","sequence_gap")
        self.assertFalse(self.state.get("kalshi","KX-X")["valid"])
        self.assertIsNone(self.state.get("kalshi","KX-X")["best_ask"])
        with self.assertRaises(ValueError):
            self.state.process_live_event(replace(delta_event, raw_payload={"new":True}))
        recovered = replace(event, raw_payload={**snapshot_message,"recovered":True},
                            sequence_number=14, source_id="2:14:clock")
        self.state.process_live_event(recovered)
        self.assertTrue(self.state.get("kalshi","KX-X")["valid"])
        self.assertEqual(self.state.get("kalshi","KX-X")["asks"][0], [.47,7.000002])

    def test_duplicate_out_of_order_and_snapshot_consistency(self):
        event = parse_polymarket(self.poly_snapshot(), STAMP)[0]
        self.assertTrue(self.state.process_live_event(event))
        self.assertFalse(self.state.process_live_event(event))
        self.assertEqual(self.journal.db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)
        change = parse_polymarket({"event_type":"price_change","timestamp":"1793786401000",
            "price_changes":[{"asset_id":"101","side":"BUY","price":"0.500001","size":"3"}]}, STAMP)[0]
        self.state.process_live_event(change)
        self.assertEqual(self.state.get("polymarket","101")["best_bid"], .500001)
        # A later authoritative snapshot replaces the reconstructed book.
        later = parse_polymarket({**self.poly_snapshot(),"timestamp":"1793786402000",
            "hash":"newhash","bids":[{"price":"0.48","size":"4"}]}, STAMP)[0]
        self.state.process_live_event(later)
        self.assertEqual(self.state.get("polymarket","101")["bids"], [[.48,4.]])
        self.assertEqual(self.state.counters["duplicates"], 1)

    def test_reconnect_accepts_identical_authoritative_snapshot(self):
        original = parse_polymarket(self.poly_snapshot(), STAMP)[0]
        self.state.process_live_event(original)
        self.state.invalidate_venue("polymarket", "reconnecting")
        self.assertFalse(self.state.get("polymarket","101")["valid"])
        redelivered = parse_polymarket(self.poly_snapshot(), now_utc())[0]
        self.assertFalse(self.state.process_live_event(redelivered))
        self.assertTrue(self.state.get("polymarket","101")["valid"])
        self.assertEqual(self.journal.db.execute("SELECT COUNT(*) FROM events").fetchone()[0],1)

    def test_journal_restart_replays_unapplied_event(self):
        event = parse_polymarket(self.poly_snapshot(), STAMP)[0].with_book_sequence(1)
        self.assertTrue(self.journal.append(event))
        self.assertEqual(len(self.journal.unprocessed()), 1)
        self.journal.db.close()
        self.journal = DurableJournal(Path(self.folder.name) / "journal.db")
        self.state.journal = self.journal
        self.assertEqual(self.state.replay_unprocessed(), 1)
        self.assertEqual(len(self.journal.unprocessed()), 0)
        self.assertFalse(self.state.get("polymarket","101")["valid"])
        self.assertEqual(self.journal.stats()["snapshots"], 1)

    def test_polygon_v2_fill_decoder(self):
        words = [0, 101, 5_000_000, 10_000_000, 10_000, 0, 0]
        log = {"topics":[ORDER_FILLED_V2_TOPIC,"0x"+"1"*64,
            "0x"+"0"*24+"a"*40,"0x"+"0"*24+"b"*40],
            "data":"0x"+"".join(f"{word:064x}" for word in words),
            "transactionHash":"0xabc","logIndex":"0x2"}
        event = decode_v2_fill(log,1793786400)
        self.assertEqual((event.market_id,event.side,event.price,event.quantity),
            ("101","buy","0.500000","10"))
        self.assertEqual(event.raw_payload["decoded"]["maker"],"0x"+"a"*40)

    def test_disconnect_invalidation_and_backoff(self):
        self.state.process_live_event(parse_polymarket(self.poly_snapshot(), STAMP)[0])
        async def connect():
            raise ConnectionError("disconnect")
        async def stop_after_backoff(seconds):
            self.assertGreaterEqual(seconds,1)
            raise asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(reconnect_forever("polymarket",self.state,connect,stop_after_backoff))
        self.assertFalse(self.state.get("polymarket","101")["valid"])
        self.assertEqual(self.state.counters["reconnects"],1)

    def test_clickhouse_failure_retry_keeps_pending(self):
        event = parse_polymarket(self.poly_snapshot(), STAMP)[0]
        self.state.process_live_event(event)
        class Response:
            def raise_for_status(self): pass
        class Client:
            calls = 0
            async def post(self, *args, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    raise httpx.ConnectError("temporary outage")
                return Response()
        client = Client()
        status = {}
        async def sleep(seconds):
            if client.calls > 1:
                raise asyncio.CancelledError()
            self.assertGreaterEqual(self.journal.stats()["pending_export"],1)
        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(export_live_forever(self.journal,"http://clickhouse:8123",status,client,sleep))
        self.assertEqual(status["clickhouse"],"connected")
        self.assertEqual(self.journal.pending_rows("events"),[])
        self.assertEqual(self.journal.pending_rows("book_observations"),[])

    def test_public_discovery_parses_v2_and_v1_ids(self):
        class Response:
            def __init__(self, body): self.body = body
            def raise_for_status(self): pass
            def json(self):
                return self.body
        class Client:
            async def get(self,url,*args,**kwargs):
                if url.endswith("/101"):
                    return Response({"positionIds":["101","102"],"question":"Open outcome"})
                return Response({"markets":[{"id":"101","question":"Open outcome"}]})
        result = asyncio.run(discover_polymarket_assets(1,Client()))
        self.assertEqual(result,[("101","Open outcome")])

    def test_kalshi_auth_headers_support_current_key_types(self):
        message = b"1793786400000GET/trade-api/ws/v2"
        for private in (ed25519.Ed25519PrivateKey.generate(),
                        rsa.generate_private_key(public_exponent=65537, key_size=2048)):
            path = Path(self.folder.name) / "kalshi.pem"
            path.write_bytes(private.private_bytes(serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
            headers = kalshi_headers("key-id", path, 1793786400000)
            signature = base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"])
            if isinstance(private, ed25519.Ed25519PrivateKey):
                private.public_key().verify(signature, message)
            else:
                private.public_key().verify(signature, message,
                    padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                        salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())

    def test_kalshi_connector_gap_then_fresh_snapshot(self):
        private = ed25519.Ed25519PrivateKey.generate()
        path = Path(self.folder.name) / "kalshi.pem"
        path.write_bytes(private.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        snapshot = {"type":"orderbook_snapshot", "sid":2, "seq":1,
            "sending_ts_ms":1793786400000, "msg":{"market_ticker":"KX-X",
            "yes_dollars_fp":[["0.44","12"]], "no_dollars_fp":[["0.53","7"]]}}
        gap = {"type":"orderbook_delta", "sid":2, "seq":3,
            "sending_ts_ms":1793786401000, "msg":{"market_ticker":"KX-X",
            "side":"yes", "price_dollars":"0.45", "delta_fp":"1"}}
        recovered = {**snapshot, "sid":5, "seq":1,
            "msg":{**snapshot["msg"], "yes_dollars_fp":[["0.46","4"]]}}
        frames = [[snapshot,gap],[recovered]]
        class Socket:
            def __init__(self, messages): self.messages=list(messages)
            async def __aenter__(self): return self
            async def __aexit__(self,*args): pass
            async def send(self,*args): pass
            async def recv(self):
                if self.messages:
                    return json.dumps(self.messages.pop(0))
                raise asyncio.CancelledError()
        def connect(*args, **kwargs): return Socket(frames.pop(0))
        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(kalshi(self.state,["KX-X"],"key-id",path,websocket_connect=connect))
        self.assertEqual(self.state.counters["gaps"],1)
        self.assertEqual(self.state.counters["recoveries"],1)
        self.assertEqual(self.state.books[("kalshi","KX-X")].view()["best_bid"],.46)

    def test_polymarket_one_subscription_per_socket_and_snapshot_restoration(self):
        sent = []
        restored = []
        state = self.state
        snapshot = self.poly_snapshot()
        class Socket:
            def __init__(self, number):
                self.number = number
                self.calls = 0
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def send(self, data): sent.append((self.number, data))
            async def recv(self):
                self.calls += 1
                if self.calls == 1:
                    return json.dumps(snapshot)
                if self.number == 1:
                    raise ConnectionResetError("simulated reset")
                restored.append(state.get("polymarket", "101")["valid"])
                raise asyncio.CancelledError()
        sockets = iter([Socket(1), Socket(2)])
        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(polymarket(state, ["101", "101"], websocket_connect=lambda *a, **k: next(sockets)))
        subscriptions = [(number, json.loads(data)) for number, data in sent if data.startswith("{")]
        self.assertEqual(subscriptions, [(1,{"assets_ids":["101"],"type":"market"}),
                                         (2,{"assets_ids":["101"],"type":"market"})])
        self.assertEqual(restored, [True])
        self.assertEqual(state.counters["recoveries"], 1)
        self.assertEqual(self.journal.db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)

    def test_contract_registry_requires_manual_verification_and_persists(self):
        poly = parse_polymarket(self.poly_snapshot(), STAMP)[0]
        kalshi_message = {"type":"orderbook_snapshot","sid":2,"seq":1,
            "sending_ts_ms":1793786400000,"msg":{"market_ticker":"KX-X",
            "yes_dollars_fp":[["0.44","12"]],"no_dollars_fp":[["0.53","7"]]}}
        self.state.process_live_event(poly)
        self.state.process_live_event(parse_kalshi(kalshi_message, STAMP))
        self.state.pairs = [{"name":"Title match", "polymarket":"101", "kalshi":"KX-X"}]
        self.assertEqual(self.state.spreads(), [])
        pair = {"polymarket_asset_id":"101", "kalshi_ticker":"KX-X",
            "event_description":"Reviewed outcome", "outcome_equivalence":"Both YES if the same event occurs",
            "resolution_source":"Official settlement documents", "expiration":"2026-12-01T00:00:00+00:00",
            "verification_status":"unverified"}
        self.journal.save_contract_pair(pair)
        self.assertEqual(self.state.spreads(), [])
        pair["verification_status"] = "verified"
        self.journal.save_contract_pair(pair)
        self.assertEqual(len(self.state.spreads()), 1)
        pair["expiration"] = "2020-01-01T00:00:00+00:00"
        self.journal.save_contract_pair(pair)
        self.assertEqual(self.state.spreads(), [])
        pair["expiration"] = "2026-12-01T00:00:00+00:00"
        self.journal.save_contract_pair(pair)
        self.journal.db.close()
        self.journal = DurableJournal(Path(self.folder.name) / "journal.db")
        self.state.journal = self.journal
        self.assertEqual(self.journal.contract_pairs()[0]["verification_status"], "verified")
        self.assertTrue(self.journal.delete_contract_pair("101", "KX-X"))
        self.assertEqual(self.state.spreads(), [])

    def test_contract_registry_api_validation(self):
        app.state.market = self.state
        client = TestClient(app)
        pair = {"polymarket_asset_id":"101", "kalshi_ticker":"KX-X",
            "event_description":"Reviewed outcome", "outcome_equivalence":"Same YES settlement",
            "resolution_source":"Official rules", "expiration":"2026-12-01T00:00:00Z",
            "verification_status":"unverified"}
        self.assertEqual(client.put("/api/v1/contracts", json={**pair,"expiration":"2026-12-01T00:00:00"}).status_code,422)
        self.assertEqual(client.put("/api/v1/contracts", json=pair).status_code,200)
        self.assertEqual(client.get("/api/v1/contracts").json()[0]["verification_status"],"unverified")
        self.assertEqual(client.delete("/api/v1/contracts/101/KX-X").status_code,200)

    def test_structured_log_redacts_url_userinfo(self):
        record = logging.LogRecord("backend.durable", logging.WARNING, __file__, 1,
            "retry http://user:secret@localhost:8123", (), None)
        rendered = JsonFormatter().format(record)
        self.assertNotIn("secret", rendered)
        self.assertIn("[redacted]@", rendered)

    def test_polygon_checkpoint_restart_and_duplicate_log_simulation(self):
        words = [0, 101, 5_000_000, 10_000_000, 10_000, 0, 0]
        log = {"topics":[ORDER_FILLED_V2_TOPIC,"0x"+"1"*64,
            "0x"+"0"*24+"a"*40,"0x"+"0"*24+"b"*40],
            "data":"0x"+"".join(f"{word:064x}" for word in words),
            "transactionHash":"0xabc","logIndex":"0x2","blockNumber":"0x64"}
        class Response:
            def __init__(self, result): self.result = result
            def raise_for_status(self): pass
            def json(self): return {"result":self.result}
        class Client:
            async def post(self, url, json):
                method = json["method"]
                return Response({"eth_chainId":"0x89","eth_blockNumber":"0x64",
                    "eth_getLogs":[log],"eth_getBlockByNumber":{"timestamp":"0x6ae34860"}}[method])
        async def stop(_): raise asyncio.CancelledError()
        for run in (1, 2):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(polygon_fills(self.state,"https://example.invalid/rpc",
                    confirmations=0,start_block=100,client=Client(),sleep=stop))
            self.assertEqual(self.journal.checkpoint("polygon_v2"),100)
            if run == 1:
                self.journal.db.close()
                self.journal = DurableJournal(Path(self.folder.name) / "journal.db")
                self.state.journal = self.journal
                self.journal.set_checkpoint("polygon_v2",99)
        self.assertEqual(self.journal.db.execute("SELECT COUNT(*) FROM events WHERE venue='polygon'").fetchone()[0],1)
        self.assertEqual(self.state.counters["duplicates"],1)

    def test_polymarket_rest_orderbook_reconciliation(self):
        # 1. Establish initial book with a snapshot
        event = parse_polymarket(self.poly_snapshot(), STAMP)[0]
        self.state.process_live_event(event)
        self.assertTrue(self.state.get("polymarket", "101")["valid"])

        class MockResponse:
            def __init__(self, data, status_code=200):
                self._data = data
                self.status_code = status_code
            def json(self): return self._data

        class MockClient:
            def __init__(self, response):
                self._response = response
            async def get(self, url):
                return self._response

        # 2. Matching reconciliation: REST has same top of book
        matching_data = {
            "market": "0xcondition", "asset_id": "101", "timestamp": "1793786400000",
            "hash": "newhash",
            "bids": [{"price": "0.490001", "size": "1.123456"}],
            "asks": [{"price": "0.510000", "size": "2.000001"}]
        }
        res = asyncio.run(reconcile_polymarket_orderbook("101", self.state, client=MockClient(MockResponse(matching_data))))
        self.assertTrue(res)
        self.assertEqual(self.state.counters.get("reconciliations_matched"), 1)
        self.assertTrue(self.state.get("polymarket", "101")["valid"])

        # 3. Diverging reconciliation: REST has different best bid/ask
        diverging_data = {
            "market": "0xcondition", "asset_id": "101", "timestamp": "1793786401000",
            "hash": "updatedhash",
            "bids": [{"price": "0.495000", "size": "5.000000"}],
            "asks": [{"price": "0.505000", "size": "4.000000"}]
        }
        res2 = asyncio.run(reconcile_polymarket_orderbook("101", self.state, client=MockClient(MockResponse(diverging_data))))
        self.assertTrue(res2)
        self.assertEqual(self.state.counters.get("reconciliations_diverged"), 1)
        view = self.state.get("polymarket", "101")
        self.assertEqual(view["best_bid"], 0.495)
        self.assertEqual(view["best_ask"], 0.505)

        # 4. Network failure: returns False, book untouched
        class FailingClient:
            async def get(self, url):
                raise httpx.ConnectError("Connection refused")

        res3 = asyncio.run(reconcile_polymarket_orderbook("101", self.state, client=FailingClient()))
        self.assertFalse(res3)

    def test_operator_auth_enforced_when_configured(self):
        app.state.market = self.state
        client = TestClient(app)
        pair = {"polymarket_asset_id": "101", "kalshi_ticker": "KX-AUTH",
                "event_description": "Protected pair", "outcome_equivalence": "Equiv",
                "resolution_source": "Rules", "expiration": "2026-12-01T00:00:00Z",
                "verification_status": "unverified"}

        try:
            os.environ["OPERATOR_KEY"] = "test-secret-operator-key-xyz"

            # 1. Without header -> 403
            resp = client.put("/api/v1/contracts", json=pair)
            self.assertEqual(resp.status_code, 403)

            # 2. With invalid header -> 403
            resp = client.put("/api/v1/contracts", json=pair, headers={"X-Operator-Key": "wrong-key"})
            self.assertEqual(resp.status_code, 403)

            # 3. With valid X-Operator-Key -> 200
            resp = client.put("/api/v1/contracts", json=pair, headers={"X-Operator-Key": "test-secret-operator-key-xyz"})
            self.assertEqual(resp.status_code, 200)

            # 4. Delete without header -> 403
            resp = client.delete("/api/v1/contracts/101/KX-AUTH")
            self.assertEqual(resp.status_code, 403)

            # 5. Delete with Bearer authorization -> 200
            resp = client.delete("/api/v1/contracts/101/KX-AUTH", headers={"Authorization": "Bearer test-secret-operator-key-xyz"})
            self.assertEqual(resp.status_code, 200)

        finally:
            os.environ.pop("OPERATOR_KEY", None)

    def test_credential_and_pem_redaction_in_structured_logs(self):
        # 1. PEM key block redaction
        pem = "-----BEGIN PRIVATE KEY-----\nMIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQg\n-----END PRIVATE KEY-----"
        record = logging.LogRecord("backend.live", logging.ERROR, __file__, 1, f"Failed with {pem}", (), None)
        rendered = JsonFormatter().format(record)
        self.assertNotIn("MIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQg", rendered)
        self.assertIn("[redacted-private-key]", rendered)

        # 2. Kalshi header redactions
        msg = "Connecting with KALSHI-ACCESS-KEY: secret-id and KALSHI-ACCESS-SIGNATURE: secret-sig"
        record2 = logging.LogRecord("backend.live", logging.INFO, __file__, 1, msg, (), None)
        rendered2 = JsonFormatter().format(record2)
        self.assertNotIn("secret-id", rendered2)
        self.assertNotIn("secret-sig", rendered2)
        self.assertIn("KALSHI-ACCESS-KEY: [redacted]", rendered2)
        self.assertIn("KALSHI-ACCESS-SIGNATURE: [redacted]", rendered2)


if __name__ == "__main__":
    unittest.main()

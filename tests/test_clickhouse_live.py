"""Opt-in real ClickHouse test: set CLICKHOUSE_URL before running."""
import asyncio
import os
from pathlib import Path
import tempfile
import time
import unittest
import uuid

import httpx

from backend.durable import DurableJournal, export_live_forever
from backend.events import now_utc, parse_polymarket
from backend.state import MarketState


@unittest.skipUnless(os.getenv("CLICKHOUSE_URL"), "set CLICKHOUSE_URL for real database verification")
class ClickHouseLiveTests(unittest.TestCase):
    def test_batch_export_retry_and_logical_counts(self):
        url = os.environ["CLICKHOUSE_URL"]
        market_id = f"phase2-check-{uuid.uuid4().hex}"
        with tempfile.TemporaryDirectory() as folder:
            journal = DurableJournal(Path(folder) / "events.db")
            state = MarketState(journal, mode="live")
            try:
                stamp = str(int(time.time() * 1000))
                snapshot = parse_polymarket({"event_type":"book", "asset_id":market_id,
                    "timestamp":stamp, "hash":"check-book",
                    "bids":[{"price":"0.49","size":"2"}],
                    "asks":[{"price":"0.51","size":"3"}]}, now_utc())[0]
                trade = parse_polymarket({"event_type":"last_trade_price", "asset_id":market_id,
                    "timestamp":stamp, "transaction_hash":"0xcheck", "side":"BUY",
                    "price":"0.5", "size":"1"}, now_utc())[0]
                state.process_live_event(snapshot)
                state.process_live_event(trade)
                state.set_feed_status("polymarket", "connected")
                journal.log_error("polymarket", market_id, None, "test observation")

                async def flush():
                    status = {}
                    task = asyncio.create_task(export_live_forever(journal,url,status))
                    try:
                        async def pending():
                            while any(journal.pending_rows(table) for table in
                                      ("events","book_observations","feed_health","ingestion_errors")):
                                await asyncio.sleep(.1)
                        await asyncio.wait_for(pending(), timeout=20)
                    finally:
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                    self.assertEqual(status.get("clickhouse"), "connected")
                    return status

                asyncio.run(flush())
                with httpx.Client(timeout=10) as client:
                    def query(sql):
                        response = client.post(url, params={"query":sql})
                        response.raise_for_status()
                        return response.text.strip()
                    quoted = market_id.replace("'", "''")
                    for table, expected in (("market_events",2),("trades",1),
                                            ("orderbook_snapshots",1),("ingestion_errors",1)):
                        self.assertEqual(query(f"SELECT count() FROM polypulse.{table} WHERE market_id='{quoted}'"), str(expected))
                    self.assertGreaterEqual(int(query("SELECT count() FROM polypulse.feed_health WHERE venue='polymarket'")),1)
                    self.assertEqual(query(f"SELECT uniqExact(event_id) FROM polypulse.market_events WHERE market_id='{quoted}'"), "2")
                    self.assertEqual(query(f"SELECT uniqExact(event_id) FROM polypulse.trades WHERE market_id='{quoted}'"), "1")
                    self.assertEqual(query(f"SELECT uniqExact(observation_id) FROM polypulse.orderbook_snapshots WHERE market_id='{quoted}'"), "1")
                    query_start = time.perf_counter()
                    query(f"SELECT uniqExact(event_id) FROM polypulse.market_events WHERE market_id='{quoted}'")
                    print(f"ClickHouse logical count query elapsed_ms={(time.perf_counter()-query_start)*1000:.3f}")

                # Resend a previously accepted event batch, simulating a lost acknowledgement.
                journal.db.execute("UPDATE events SET exported=0 WHERE market_id=?",(market_id,))
                journal.db.commit()
                asyncio.run(flush())
                with httpx.Client(timeout=10) as client:
                    response = client.post(url, params={"query":
                        f"SELECT uniqExact(event_id) FROM polypulse.market_events WHERE market_id='{market_id}'"})
                    response.raise_for_status()
                    self.assertEqual(response.text.strip(),"2")
            finally:
                for book in state.books.values():
                    book.close()
                journal.db.close()

import tempfile
import time
import unittest
from pathlib import Path

from backend.engine import Book
from backend.normalizers import kalshi_delta, kalshi_snapshot, poly_messages, units
from backend.state import MarketState
from backend.storage import Journal


class PipelineTests(unittest.TestCase):
    def test_engine_snapshot_update_and_vwap(self):
        book = Book()
        try:
            book.snapshot([(5000, 100_000), (4800, 200_000)], [(5500, 100_000), (5700, 200_000)], 1)
            self.assertEqual(book.view()["best_bid"], 0.5)
            self.assertAlmostEqual(book.view()["buy_vwap_10"], 0.55)
            book.set_level("bid", 5100, 50_000, 2)
            self.assertEqual(book.view()["best_bid"], 0.51)
            with self.assertRaises(ValueError):
                book.set_level("bid", 5200, 50_000, 2)
        finally:
            book.close()

    def test_venue_normalization(self):
        market, bids, asks = kalshi_snapshot({"msg": {"market_ticker": "KX-X", "yes_dollars_fp": [["0.44", "12.00"]], "no_dollars_fp": [["0.53", "7.00"]]}})
        self.assertEqual((market, bids, asks), ("KX-X", [(4400, 120_000)], [(4700, 70_000)]))
        self.assertEqual(kalshi_delta({"msg": {"market_ticker": "KX-X", "side": "no", "price_dollars": "0.53", "delta_fp": "-2.00"}}, 70_000), ("KX-X", "ask", 4700, 50_000))
        messages = list(poly_messages({"event_type": "price_change", "price_changes": [{"asset_id": "123", "side": "BUY", "price": "0.51", "size": "0"}]}))
        self.assertEqual(messages[0], ("123", "level", None, None, "bid", 5100, 0))
        with self.assertRaises(ValueError):
            units("0.12345", price=True)

    def test_journal_restore_and_missing_markout(self):
        with tempfile.TemporaryDirectory() as folder:
            journal = Journal(Path(folder) / "test.db")
            try:
                state = MarketState(journal)
                state.snapshot("polymarket", "asset", [(4900, 100_000)], [(5100, 100_000)], 1)
                state.level("polymarket", "asset", "bid", 5000, 90_000, 2)
                self.assertEqual(journal.stats()["events"], 2)
                with self.assertRaises(ValueError):
                    state.snapshot("polymarket", "asset", [(4900, 100_000)], [(5100, 100_000)], 1)
                state.reset_venue("polymarket")
                state.snapshot("polymarket", "asset", [(4900, 100_000)], [(5100, 100_000)], 1)
                self.assertEqual(journal.stats()["events"], 2)
                self.assertEqual(journal.stats()["snapshots"], 3)
                self.assertEqual(state.get("polymarket", "asset")["best_bid"], 0.49)
                self.assertIsNone(journal.future_mid("polymarket", "asset", time.time() + 60))
                for book in state.books.values():
                    book.close()
                restored = MarketState(journal)
                self.assertEqual(restored.get("polymarket", "asset")["best_bid"], 0.49)
                for book in restored.books.values():
                    book.close()
            finally:
                journal.db.close()


if __name__ == "__main__":
    unittest.main()

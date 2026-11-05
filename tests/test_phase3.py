import math
import unittest
from backend.engine import Book, PRICE_SCALE, QUANTITY_SCALE


class Phase3EngineTests(unittest.TestCase):
    def setUp(self):
        self.book = Book()

    def tearDown(self):
        self.book.close()

    def test_empty_book_state(self):
        v = self.book.view()
        self.assertEqual(v["bids"], [])
        self.assertEqual(v["asks"], [])
        self.assertIsNone(v["best_bid"])
        self.assertIsNone(v["best_ask"])
        self.assertIsNone(v["buy_vwap_10"])
        self.assertEqual(v["imbalance"], 0.0)
        self.assertEqual(v["ofi"], 0.0)
        self.assertIsNone(v["mid_price"])
        self.assertIsNone(v["spread"])
        self.assertIsNone(v["spread_bps"])

        sa = self.book.spread_analytics()
        self.assertIsNone(sa["best_bid"])
        self.assertIsNone(sa["best_ask"])
        self.assertIsNone(sa["mid_price"])
        self.assertFalse(sa["is_crossed"])

    def test_single_level_and_crossing(self):
        # Single bid at 0.48, qty 10.0
        self.book.snapshot([(480000, 10000000)], [], 1)
        self.assertEqual(self.book.imbalance(), 1.0)
        self.assertIsNone(self.book.vwap(buy=True, quantity=5.0)) # No asks
        self.assertAlmostEqual(self.book.vwap(buy=False, quantity=5.0), 0.48)

        # Add ask at 0.46 (crossed book!)
        self.book.set_level("ask", 460000, 10000000, 2)
        sa = self.book.spread_analytics()
        self.assertTrue(sa["is_crossed"])
        self.assertAlmostEqual(sa["spread"], -0.02)
        self.assertEqual(sa["mid_price"], 0.47)

    def test_vwap_and_integer_ticks(self):
        bids = [
            (500000, 10000000), # 0.50 Q=10
            (490000, 20000000), # 0.49 Q=20
            (480000, 30000000), # 0.48 Q=30
        ]
        asks = [
            (510000, 10000000), # 0.51 Q=10
            (520000, 20000000), # 0.52 Q=20
            (530000, 30000000), # 0.53 Q=30
        ]
        self.book.snapshot(bids, asks, 1)

        # Buy VWAP:
        # Q = 5 -> all from ask level 1 @ 0.51 -> VWAP = 0.51
        self.assertAlmostEqual(self.book.vwap(buy=True, quantity=5.0), 0.51)
        self.assertEqual(self.book.vwap_ticks(buy=True, quantity=5.0), 510000)

        # Q = 15 -> 10 @ 0.51 + 5 @ 0.52 = (5.10 + 2.60) / 15 = 7.70 / 15 = 0.5133333333333333
        expected_vwap_15 = (10 * 0.51 + 5 * 0.52) / 15
        self.assertAlmostEqual(self.book.vwap(buy=True, quantity=15.0), expected_vwap_15, places=7)

        # Q = 70 -> total available ask is 10 + 20 + 30 = 60 -> exceeds liquidity!
        self.assertIsNone(self.book.vwap(buy=True, quantity=70.0))
        self.assertIsNone(self.book.vwap_ticks(buy=True, quantity=70.0))

        # Sell VWAP:
        # Q = 15 -> 10 @ 0.50 + 5 @ 0.49 = (5.00 + 2.45) / 15 = 7.45 / 15 = 0.49666666666666665
        expected_sell_15 = (10 * 0.50 + 5 * 0.49) / 15
        self.assertAlmostEqual(self.book.vwap(buy=False, quantity=15.0), expected_sell_15, places=7)

    def test_order_flow_imbalance_cont_kukanov_stoikov(self):
        # Initial snapshot: Bid 0.50 Q=10, Ask 0.52 Q=10
        self.book.snapshot([(500000, 10000000)], [(520000, 10000000)], 1)
        self.assertEqual(self.book.ofi(), 0.0)
        self.assertEqual(self.book.cumulative_ofi(), 0.0)

        # Step 1: Bid price improves to 0.51 with Q=5 (buying pressure)
        # delta_b = +5.0, delta_a = 0 -> OFI = +5.0
        self.book.set_level("bid", 510000, 5000000, 2)
        self.assertAlmostEqual(self.book.ofi(), 5.0)
        self.assertAlmostEqual(self.book.cumulative_ofi(), 5.0)

        # Step 2: Bid quantity increases at 0.51 from 5.0 to 9.0
        # delta_b = 9 - 5 = +4.0 -> OFI = +4.0
        self.book.set_level("bid", 510000, 9000000, 3)
        self.assertAlmostEqual(self.book.ofi(), 4.0)
        self.assertAlmostEqual(self.book.cumulative_ofi(), 9.0)

        # Step 3: Ask price improves (drops from 0.52 to 0.515 with Q=6) -> selling pressure
        # delta_a = +6.0 -> OFI = 0 - 6.0 = -6.0
        self.book.set_level("ask", 515000, 6000000, 4)
        self.assertAlmostEqual(self.book.ofi(), -6.0)
        self.assertAlmostEqual(self.book.cumulative_ofi(), 3.0)

        # Step 4: Ask level at 0.515 is removed (Q=0), retreats to 0.52 (Q=10)
        # Ask price rose from 0.515 to 0.52: delta_a = -prev_q = -6.0 -> OFI = 0 - (-6.0) = +6.0
        self.book.set_level("ask", 515000, 0, 5)
        self.assertAlmostEqual(self.book.ofi(), 6.0)
        self.assertAlmostEqual(self.book.cumulative_ofi(), 9.0)

    def test_cross_venue_spread_analytics(self):
        b1 = Book()
        b2 = Book()
        try:
            # Venue 1: Bid 0.49, Ask 0.51
            b1.snapshot([(490000, 10000000)], [(510000, 10000000)], 1)
            # Venue 2: Bid 0.52, Ask 0.54
            b2.snapshot([(520000, 10000000)], [(540000, 10000000)], 1)

            res = Book.cross_spread(b1, b2)
            self.assertIsNotNone(res)
            # Buy V1 @ 0.51, Sell V2 @ 0.52 -> spread +0.01 -> Executable!
            self.assertAlmostEqual(res["buy_1_sell_2_spread"], 0.01)
            self.assertTrue(res["executable_buy_1_sell_2"])
            self.assertGreater(res["buy_1_sell_2_bps"], 0.0)

            # Buy V2 @ 0.54, Sell V1 @ 0.49 -> spread -0.05 -> Not executable
            self.assertAlmostEqual(res["buy_2_sell_1_spread"], -0.05)
            self.assertFalse(res["executable_buy_2_sell_1"])
        finally:
            b1.close()
            b2.close()

    def test_batch_vwap_and_avx2_equivalence(self):
        asks = [
            (500000, 10000000), # 10 @ 0.50
            (510000, 20000000), # 20 @ 0.51
            (520000, 30000000), # 30 @ 0.52
            (530000, 40000000), # 40 @ 0.53
            (540000, 50000000), # 50 @ 0.54
        ]
        self.book.snapshot([], asks, 1)

        quantities = [5.0, 15.0, 30.0, 60.0, 100.0, 150.0, 200.0]
        vwap_scalar = self.book.batch_vwap(buy=True, quantities=quantities, use_avx2=False)
        vwap_avx2 = self.book.batch_vwap(buy=True, quantities=quantities, use_avx2=True)

        for s, a in zip(vwap_scalar, vwap_avx2):
            if s is None:
                self.assertIsNone(a)
            else:
                self.assertIsNotNone(a)
                self.assertAlmostEqual(s, a, places=8)

        # 200.0 exceeds 150.0 total depth -> should be None in both
        self.assertIsNone(vwap_scalar[-1])
        self.assertIsNone(vwap_avx2[-1])

    def test_large_event_sequence_integrity(self):
        # 1000 sequential updates
        self.book.snapshot([(500000, 1000000)], [(501000, 1000000)], 1)
        for seq in range(2, 1002):
            price = 500000 + (seq % 10) * 1000
            qty = 1000000 + seq * 1000
            self.book.set_level(True if seq % 2 == 0 else False, price, qty, seq)

        v = self.book.view()
        self.assertEqual(v["sequence"], 1001)
        self.assertIsNotNone(v["best_bid"])
        self.assertIsNotNone(v["best_ask"])
        self.assertGreater(len(v["bids"]), 0)

    def test_independent_ofi_reference_audit(self):
        def ref_ofi(prev, curr):
            # Bid contribution
            b_contrib = 0
            if prev["bid_p"] is not None and curr["bid_p"] is not None:
                if curr["bid_p"] > prev["bid_p"]:
                    b_contrib = curr["bid_q"]
                elif curr["bid_p"] == prev["bid_p"]:
                    b_contrib = curr["bid_q"] - prev["bid_q"]
                else:
                    b_contrib = -prev["bid_q"]
            elif curr["bid_p"] is not None:
                b_contrib = curr["bid_q"]
            elif prev["bid_p"] is not None:
                b_contrib = -prev["bid_q"]

            # Ask contribution
            a_contrib = 0
            if prev["ask_p"] is not None and curr["ask_p"] is not None:
                if curr["ask_p"] < prev["ask_p"]:
                    a_contrib = -curr["ask_q"]
                elif curr["ask_p"] == prev["ask_p"]:
                    a_contrib = prev["ask_q"] - curr["ask_q"]
                else:
                    a_contrib = prev["ask_q"]
            elif curr["ask_p"] is not None:
                a_contrib = -curr["ask_q"]
            elif prev["ask_p"] is not None:
                a_contrib = prev["ask_q"]

            return b_contrib + a_contrib

        # Initial book
        self.book.snapshot([(490000, 1000000)], [(520000, 2000000)], 1)
        self.assertEqual(self.book.ofi(), 0.0)
        prev = {"bid_p": 490000, "bid_q": 1.0, "ask_p": 520000, "ask_q": 2.0}

        # Case 1: Bid improvement (0.49 -> 0.50) with Q: 1.0 -> 2.5
        self.book.set_level("bid", 500000, 2500000, 2)
        curr1 = {"bid_p": 500000, "bid_q": 2.5, "ask_p": 520000, "ask_q": 2.0}
        self.assertAlmostEqual(self.book.ofi(), ref_ofi(prev, curr1))
        self.assertAlmostEqual(self.book.ofi(), 2.5)
        prev = curr1

        # Case 2: Bid retreat (0.50 canceled, retreats to 0.49) with Q: 2.5 -> 1.0
        self.book.set_level("bid", 500000, 0, 3)
        curr2 = {"bid_p": 490000, "bid_q": 1.0, "ask_p": 520000, "ask_q": 2.0}
        self.assertAlmostEqual(self.book.ofi(), ref_ofi(prev, curr2))
        self.assertAlmostEqual(self.book.ofi(), -2.5)
        prev = curr2

        # Case 3: Ask improvement (0.52 -> 0.51) with Q: 2.0 -> 4.5
        self.book.set_level("ask", 510000, 4500000, 4)
        curr3 = {"bid_p": 490000, "bid_q": 1.0, "ask_p": 510000, "ask_q": 4.5}
        self.assertAlmostEqual(self.book.ofi(), ref_ofi(prev, curr3))
        self.assertAlmostEqual(self.book.ofi(), -4.5)
        prev = curr3

        # Case 4: Ask retreat (0.51 canceled, retreats to 0.52) with Q: 4.5 -> 2.0
        self.book.set_level("ask", 510000, 0, 5)
        curr4 = {"bid_p": 490000, "bid_q": 1.0, "ask_p": 520000, "ask_q": 2.0}
        self.assertAlmostEqual(self.book.ofi(), ref_ofi(prev, curr4))
        self.assertAlmostEqual(self.book.ofi(), 4.5)
        prev = curr4

        # Case 5: Unchanged prices with quantity additions
        self.book.set_level("bid", 490000, 1800000, 6) # +0.8
        curr5 = {"bid_p": 490000, "bid_q": 1.8, "ask_p": 520000, "ask_q": 2.0}
        self.assertAlmostEqual(self.book.ofi(), ref_ofi(prev, curr5))
        self.assertAlmostEqual(self.book.ofi(), 0.8)
        prev = curr5

        self.book.set_level("ask", 520000, 2600000, 7) # -0.6
        curr6 = {"bid_p": 490000, "bid_q": 1.8, "ask_p": 520000, "ask_q": 2.6}
        self.assertAlmostEqual(self.book.ofi(), ref_ofi(prev, curr6))
        self.assertAlmostEqual(self.book.ofi(), -0.6)
        prev = curr6

        # Case 6: Unchanged prices with quantity removals
        self.book.set_level("bid", 490000, 1100000, 8) # -0.7
        curr7 = {"bid_p": 490000, "bid_q": 1.1, "ask_p": 520000, "ask_q": 2.6}
        self.assertAlmostEqual(self.book.ofi(), ref_ofi(prev, curr7))
        self.assertAlmostEqual(self.book.ofi(), -0.7)
        prev = curr7

        self.book.set_level("ask", 520000, 1900000, 9) # +0.7
        curr8 = {"bid_p": 490000, "bid_q": 1.1, "ask_p": 520000, "ask_q": 1.9}
        self.assertAlmostEqual(self.book.ofi(), ref_ofi(prev, curr8))
        self.assertAlmostEqual(self.book.ofi(), 0.7)
        prev = curr8

        # Case 7: Bid removal to empty
        self.book.set_level("bid", 490000, 0, 10)
        curr9 = {"bid_p": None, "bid_q": 0.0, "ask_p": 520000, "ask_q": 1.9}
        self.assertAlmostEqual(self.book.ofi(), ref_ofi(prev, curr9))
        self.assertAlmostEqual(self.book.ofi(), -1.1)

    def test_memory_and_capacity(self):
        # Initial preallocated buffer
        self.assertEqual(self.book.capacity(bid=True), 128)
        self.assertEqual(self.book.capacity(bid=False), 128)

        # Insert 128 levels on bids
        for i in range(128):
            self.book.set_level("bid", 100000 + i * 1000, 1000000, i + 1)
        self.assertEqual(self.book.capacity(bid=True), 128)

        # Insert 129th level: vector reallocates
        self.book.set_level("bid", 90000, 1000000, 129)
        self.assertGreater(self.book.capacity(bid=True), 128)

    def test_128bit_integer_accumulator(self):
        # 100 million contracts (10^14 units) at 0.50
        self.book.snapshot([], [(500000, 100000000000000)], 1)
        # Quantity in contracts = 100,000,000.0
        vwap = self.book.vwap(buy=True, quantity=100000000.0)
        self.assertAlmostEqual(vwap, 0.50)
        ticks = self.book.vwap_ticks(buy=True, quantity=100000000.0)
        self.assertEqual(ticks, 500000)


if __name__ == "__main__":
    unittest.main()

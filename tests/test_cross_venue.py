"""
Comprehensive test suite for PolyPulse Phase 6: Cross-Venue Intelligence & Arbitrage Scanner.
Covers:
- Extended contract registry persistence and schema migration
- Title-only match rejection and settlement compatibility checks
- Direct equivalence spread calculation (long poly/short kalshi and long kalshi/short poly)
- Inverse equivalence spread calculation (mint and merge arbitrage)
- Strict gating: unverified, rejected, expired, and crossed books excluded from scanner
- Operator authentication enforcement on all mutating review endpoints
"""
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from backend.durable import DurableJournal
from backend.main import app
from backend.state import MarketState
from backend.scanner import (
    compute_cross_venue_spread,
    validate_contract_compatibility,
    calculate_kalshi_fee,
    calculate_polymarket_fee,
)


class TestCrossVenueIntelligence(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.db_path = Path(self.tmp_dir) / "journal.db"
        self.journal = DurableJournal(self.db_path)
        self.state = MarketState(self.journal, mode="live")
        app.state.market = self.state

        self.future_exp = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        self.past_exp = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()

    def tearDown(self):
        try:
            self.journal.db.close()
        except Exception:
            pass

    def test_extended_contract_registry_persistence(self):
        """Verifies all Phase 6 contract fields persist and round-trip correctly."""
        pair = {
            "polymarket_asset_id": "poly_fed_01",
            "kalshi_ticker": "KXFED-26MAR-25",
            "canonical_event": "Federal Reserve Target Rate Decision - March 2026",
            "canonical_outcome": "Target Range Upper Bound at 4.25%",
            "event_description": "Legacy Fed Event",
            "outcome_equivalence": "Legacy outcome description",
            "equivalence_mapping": "direct",
            "resolution_source": "Federal Reserve Board Press Release / H.4.1 Release",
            "resolution_criteria": "Settles YES if the FOMC statements specify target rate upper bound of 4.25%",
            "polymarket_expiration": self.future_exp,
            "kalshi_expiration": self.future_exp,
            "expiration": self.future_exp,
            "settlement_details": "Polymarket settles via UMA in USDC; Kalshi settles in USD under CFTC rules.",
            "verification_status": "verified",
            "reviewer": "chief_quant_operator",
        }
        self.journal.save_contract_pair(pair)

        fetched = self.journal.get_contract_pair("poly_fed_01", "KXFED-26MAR-25")
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched["canonical_event"], pair["canonical_event"])
        self.assertEqual(fetched["canonical_outcome"], pair["canonical_outcome"])
        self.assertEqual(fetched["equivalence_mapping"], "direct")
        self.assertEqual(fetched["resolution_source"], pair["resolution_source"])
        self.assertEqual(fetched["resolution_criteria"], pair["resolution_criteria"])
        self.assertEqual(fetched["verification_status"], "verified")
        self.assertEqual(fetched["reviewer"], "chief_quant_operator")
        self.assertIsNotNone(fetched["verified_at"])

    def test_title_only_matching_rejected_by_validator(self):
        """Verifies that matching on market title alone without resolution source/criteria is rejected."""
        title_only_pair = {
            "polymarket_asset_id": "poly_unverified",
            "kalshi_ticker": "KX_UNVERIFIED",
            "canonical_event": "Fed Rate Decision",
            "resolution_source": "",  # Empty resolution source
            "expiration": self.future_exp,
        }
        compat, msg = validate_contract_compatibility(title_only_pair)
        self.assertFalse(compat)
        self.assertIn("Resolution source is required", msg)

    def test_expired_contract_rejected_by_validator(self):
        """Verifies that expired contracts are flagged and rejected."""
        expired_pair = {
            "polymarket_asset_id": "poly_old",
            "kalshi_ticker": "KX_OLD",
            "canonical_event": "2024 Presidential Election",
            "resolution_source": "AP News",
            "expiration": self.past_exp,
        }
        compat, msg = validate_contract_compatibility(expired_pair)
        self.assertFalse(compat)
        self.assertIn("has expired", msg)

    def test_direct_spread_calculation_long_poly_short_kalshi(self):
        """
        Direct equivalence: Poly YES == Kalshi YES.
        Poly Ask: 0.40, Kalshi Bid: 0.46.
        Long Poly / Short Kalshi gives gross spread: 0.46 - 0.40 = +0.06.
        """
        pair = {
            "polymarket_asset_id": "p1",
            "kalshi_ticker": "k1",
            "canonical_event": "Fed Cut 25bps",
            "equivalence_mapping": "direct",
            "expiration": self.future_exp,
            "verification_status": "verified",
        }
        poly_book = {
            "valid": 1,
            "best_bid": 0.38,
            "best_ask": 0.40,
            "bid_depth": 500.0,
            "ask_depth": 600.0,
        }
        kalshi_book = {
            "valid": 1,
            "best_bid": 0.46,
            "best_ask": 0.49,
            "bid_depth": 400.0,
            "ask_depth": 300.0,
        }
        spread = compute_cross_venue_spread(pair, poly_book, kalshi_book)
        self.assertIsNotNone(spread)
        self.assertAlmostEqual(spread["buy_polymarket_sell_kalshi"], 0.06)
        self.assertAlmostEqual(spread["buy_kalshi_sell_polymarket"], -0.11)
        self.assertEqual(spread["opportunity_type"], "long_poly_short_kalshi")
        self.assertTrue(spread["is_arbitrage"])
        self.assertEqual(spread["executable_qty"], 400.0)  # min(poly_ask_depth=600, kalshi_bid_depth=400)
        self.assertAlmostEqual(spread["best_spread"], 0.06)
        self.assertGreater(spread["best_spread_bps"], 1400)  # (0.06 / 0.40) * 10,000 = 1500 bps

    def test_direct_spread_calculation_long_kalshi_short_poly(self):
        """
        Direct equivalence: Poly YES == Kalshi YES.
        Kalshi Ask: 0.35, Poly Bid: 0.42.
        Long Kalshi / Short Poly gives gross spread: 0.42 - 0.35 = +0.07.
        """
        pair = {
            "polymarket_asset_id": "p2",
            "kalshi_ticker": "k2",
            "canonical_event": "CPI MoM > 0.2%",
            "equivalence_mapping": "direct",
            "expiration": self.future_exp,
            "verification_status": "verified",
        }
        poly_book = {
            "valid": 1,
            "best_bid": 0.42,
            "best_ask": 0.45,
            "bid_depth": 1000.0,
            "ask_depth": 800.0,
        }
        kalshi_book = {
            "valid": 1,
            "best_bid": 0.32,
            "best_ask": 0.35,
            "bid_depth": 450.0,
            "ask_depth": 500.0,
        }
        spread = compute_cross_venue_spread(pair, poly_book, kalshi_book)
        self.assertIsNotNone(spread)
        self.assertAlmostEqual(spread["buy_kalshi_sell_polymarket"], 0.07)
        self.assertEqual(spread["opportunity_type"], "long_kalshi_short_poly")
        self.assertTrue(spread["is_arbitrage"])
        self.assertEqual(spread["executable_qty"], 500.0)  # min(kalshi_ask_depth=500, poly_bid_depth=1000)

    def test_inverse_spread_mint_arbitrage(self):
        """
        Inverse equivalence: Poly YES == Kalshi NO (Poly YES + Kalshi YES covers all outcomes to 1.00).
        Poly Ask: 0.45, Kalshi Ask: 0.48.
        Total Cost to Buy Both = 0.45 + 0.48 = 0.93.
        Guaranteed Payout = 1.00.
        Mint Gross Arbitrage Spread = 1.00 - 0.93 = +0.07.
        """
        pair = {
            "polymarket_asset_id": "p_inv_1",
            "kalshi_ticker": "k_inv_1",
            "canonical_event": "Election Winner Dem vs Rep",
            "equivalence_mapping": "inverse",
            "expiration": self.future_exp,
            "verification_status": "verified",
        }
        poly_book = {
            "valid": 1,
            "best_bid": 0.43,
            "best_ask": 0.45,
            "bid_depth": 700.0,
            "ask_depth": 350.0,
        }
        kalshi_book = {
            "valid": 1,
            "best_bid": 0.46,
            "best_ask": 0.48,
            "bid_depth": 600.0,
            "ask_depth": 500.0,
        }
        spread = compute_cross_venue_spread(pair, poly_book, kalshi_book)
        self.assertIsNotNone(spread)
        self.assertEqual(spread["equivalence_mapping"], "inverse")
        self.assertAlmostEqual(spread["best_spread"], 0.07)
        self.assertEqual(spread["opportunity_type"], "mint_both_long")
        self.assertTrue(spread["is_arbitrage"])
        self.assertEqual(spread["executable_qty"], 350.0)  # min(poly_ask_depth=350, kalshi_ask_depth=500)

    def test_inverse_spread_merge_arbitrage(self):
        """
        Inverse equivalence: Poly YES == Kalshi NO.
        Poly Bid: 0.54, Kalshi Bid: 0.52.
        Upfront Revenue from Selling Both = 0.54 + 0.52 = 1.06.
        Maximum Payout Liability = 1.00.
        Merge Gross Arbitrage Spread = 1.06 - 1.00 = +0.06.
        """
        pair = {
            "polymarket_asset_id": "p_inv_2",
            "kalshi_ticker": "k_inv_2",
            "canonical_event": "Unemployment Rate > 4.5%",
            "equivalence_mapping": "inverse",
            "expiration": self.future_exp,
            "verification_status": "verified",
        }
        poly_book = {
            "valid": 1,
            "best_bid": 0.54,
            "best_ask": 0.57,
            "bid_depth": 800.0,
            "ask_depth": 400.0,
        }
        kalshi_book = {
            "valid": 1,
            "best_bid": 0.52,
            "best_ask": 0.55,
            "bid_depth": 650.0,
            "ask_depth": 500.0,
        }
        spread = compute_cross_venue_spread(pair, poly_book, kalshi_book)
        self.assertIsNotNone(spread)
        self.assertEqual(spread["equivalence_mapping"], "inverse")
        self.assertAlmostEqual(spread["best_spread"], 0.06)
        self.assertEqual(spread["opportunity_type"], "merge_both_short")
        self.assertTrue(spread["is_arbitrage"])
        self.assertEqual(spread["executable_qty"], 650.0)  # min(poly_bid_depth=800, kalshi_bid_depth=650)

    def test_unverified_and_rejected_pairs_excluded_from_scanner(self):
        """Verifies that unverified, rejected, and incompatible pairs are strictly omitted from spreads()."""
        book_p = {"valid": 1, "best_bid": 0.40, "best_ask": 0.45, "bid_depth": 100, "ask_depth": 100}
        book_k = {"valid": 1, "best_bid": 0.50, "best_ask": 0.55, "bid_depth": 100, "ask_depth": 100}

        for status in ("unverified", "rejected", "incompatible"):
            pair = {
                "polymarket_asset_id": f"p_{status}",
                "kalshi_ticker": f"k_{status}",
                "canonical_event": f"Event {status}",
                "equivalence_mapping": "direct",
                "expiration": self.future_exp,
                "verification_status": status,
            }
            res = compute_cross_venue_spread(pair, book_p, book_k)
            self.assertIsNone(res, f"Pair with status '{status}' MUST NOT be processed by scanner")

    def test_expired_pair_excluded_from_scanner(self):
        """Verifies that expired pairs are strictly omitted even if marked verified."""
        book_p = {"valid": 1, "best_bid": 0.40, "best_ask": 0.45, "bid_depth": 100, "ask_depth": 100}
        book_k = {"valid": 1, "best_bid": 0.50, "best_ask": 0.55, "bid_depth": 100, "ask_depth": 100}
        expired_pair = {
            "polymarket_asset_id": "p_exp",
            "kalshi_ticker": "k_exp",
            "canonical_event": "Expired Event",
            "equivalence_mapping": "direct",
            "expiration": self.past_exp,
            "verification_status": "verified",
        }
        res = compute_cross_venue_spread(expired_pair, book_p, book_k)
        self.assertIsNone(res, "Expired pair MUST be omitted from scanner")

    def test_crossed_or_invalid_books_excluded(self):
        """Verifies that invalid or crossed orderbooks (ask <= bid) produce no spread."""
        pair = {
            "polymarket_asset_id": "p_cr",
            "kalshi_ticker": "k_cr",
            "canonical_event": "Crossed Book Event",
            "equivalence_mapping": "direct",
            "expiration": self.future_exp,
            "verification_status": "verified",
        }
        # Crossed book on Polymarket: ask 0.40 <= bid 0.45
        crossed_p = {"valid": 1, "best_bid": 0.45, "best_ask": 0.40, "bid_depth": 100, "ask_depth": 100}
        normal_k = {"valid": 1, "best_bid": 0.44, "best_ask": 0.48, "bid_depth": 100, "ask_depth": 100}
        self.assertIsNone(compute_cross_venue_spread(pair, crossed_p, normal_k))

        # Invalid book flag
        invalid_p = {"valid": 0, "best_bid": 0.40, "best_ask": 0.45, "bid_depth": 100, "ask_depth": 100}
        self.assertIsNone(compute_cross_venue_spread(pair, invalid_p, normal_k))

    def test_operator_auth_enforced_on_mutating_endpoints(self):
        """Verifies that operator authentication protects PUT, POST /verify, and DELETE."""
        client = TestClient(app)
        os.environ["OPERATOR_KEY"] = "super-secret-operator-key-99"

        pair_data = {
            "polymarket_asset_id": "poly_auth_test",
            "kalshi_ticker": "KX_AUTH_TEST",
            "canonical_event": "Fed Rate Hold",
            "canonical_outcome": "Rate stays 4.25%-4.50%",
            "resolution_source": "FOMC Statement",
            "resolution_criteria": "Official press release",
            "expiration": self.future_exp,
            "verification_status": "unverified",
        }

        # 1. PUT unauthenticated -> 403
        r_unauth = client.put("/api/v1/contracts", json=pair_data)
        self.assertEqual(r_unauth.status_code, 403)

        # 2. PUT with wrong key -> 403
        r_wrong = client.put("/api/v1/contracts", json=pair_data, headers={"X-Operator-Key": "wrong"})
        self.assertEqual(r_wrong.status_code, 403)

        # 3. PUT with valid header -> 200
        r_valid = client.put("/api/v1/contracts", json=pair_data, headers={"X-Operator-Key": "super-secret-operator-key-99"})
        self.assertEqual(r_valid.status_code, 200)

        # 4. POST /verify unauthenticated -> 403
        verify_data = {
            "polymarket_asset_id": "poly_auth_test",
            "kalshi_ticker": "KX_AUTH_TEST",
            "verification_status": "verified",
            "reviewer": "quant_auditor",
        }
        r_ver_unauth = client.post("/api/v1/contracts/verify", json=verify_data)
        self.assertEqual(r_ver_unauth.status_code, 403)

        # 5. POST /verify with valid Bearer auth -> 200
        r_ver_auth = client.post(
            "/api/v1/contracts/verify",
            json=verify_data,
            headers={"Authorization": "Bearer super-secret-operator-key-99"}
        )
        self.assertEqual(r_ver_auth.status_code, 200)
        self.assertEqual(r_ver_auth.json()["verification_status"], "verified")

        # 6. DELETE unauthenticated -> 403
        r_del_unauth = client.delete("/api/v1/contracts/poly_auth_test/KX_AUTH_TEST")
        self.assertEqual(r_del_unauth.status_code, 403)

        # 7. DELETE with valid auth -> 200
        r_del_auth = client.delete(
            "/api/v1/contracts/poly_auth_test/KX_AUTH_TEST",
            headers={"X-Operator-Key": "super-secret-operator-key-99"}
        )
        self.assertEqual(r_del_auth.status_code, 200)

        # Clean up env
        del os.environ["OPERATOR_KEY"]


if __name__ == "__main__":
    unittest.main()

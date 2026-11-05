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
    compute_ladder_vwap,
)
from backend.fees import (
    get_fee_schedule,
    calculate_venue_taker_fee,
    evaluate_net_opportunity_costs,
    DEFAULT_FEE_VERSION,
)
from backend.canonical import (
    normalize_kalshi_book,
    normalize_polymarket_book,
    to_price_ticks,
    to_quantity_units,
    ticks_to_float,
    units_to_float,
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

    def test_kalshi_canonical_normalization_with_no_bid_transformation(self):
        """
        Tests Kalshi canonical transformation:
        Kalshi YES bid: 0.38 (380,000 ticks)
        Kalshi NO bid: 0.58 (580,000 ticks) -> implies YES ask: 1.0 - 0.58 = 0.42 (420,000 ticks)
        Synthetic NO ask: 1.0 - 0.38 = 0.62 (620,000 ticks)
        """
        payload = {
            "market_ticker": "KXFED-MAR26",
            "yes_bid": "0.38",
            "yes_bid_size": "500",
            "no_bid": "0.58",
            "no_bid_size": "750",
            "status": "active",
            "exchange_timestamp": datetime.now(timezone.utc).isoformat(),
        }
        quote = normalize_kalshi_book(payload, received_at=datetime.now(timezone.utc).isoformat())
        self.assertTrue(quote.valid)
        self.assertFalse(quote.stale)
        self.assertEqual(quote.yes_bid_ticks, 380_000)
        self.assertEqual(quote.yes_ask_ticks, 420_000)
        self.assertEqual(quote.no_bid_ticks, 580_000)
        self.assertEqual(quote.no_ask_ticks, 620_000)
        self.assertEqual(quote.yes_bid_qty, 500.0)
        self.assertEqual(quote.yes_ask_qty, 750.0)
        self.assertTrue(quote.is_executable())

    def test_polymarket_canonical_normalization_preserves_token_id(self):
        """Tests Polymarket canonical normalization preserves outcome token ID and computes complementary NO quote."""
        tok_id = "115792089237316195423570985008687907853269984665640564039457584007913129639935"
        payload = {
            "market_id": "0xfeedbeef",
            "best_bid": "0.45",
            "best_ask": "0.47",
            "bid_depth": "1000",
            "ask_depth": "1200",
            "status": "active",
            "exchange_timestamp": datetime.now(timezone.utc).isoformat(),
        }
        quote = normalize_polymarket_book(payload, received_at=datetime.now(timezone.utc).isoformat(), outcome_token_id=tok_id)
        self.assertTrue(quote.valid)
        self.assertFalse(quote.stale)
        self.assertEqual(quote.outcome_id, tok_id)
        self.assertEqual(quote.yes_bid_ticks, 450_000)
        self.assertEqual(quote.yes_ask_ticks, 470_000)
        # NO bid = 1.0 - 0.47 = 0.53
        self.assertEqual(quote.no_bid_ticks, 530_000)
        # NO ask = 1.0 - 0.45 = 0.55
        self.assertEqual(quote.no_ask_ticks, 550_000)
        self.assertTrue(quote.is_executable())

    def test_canonical_normalization_rejects_crossed_quotes(self):
        """Tests that crossed books (ask <= bid) produce valid=False with diagnostic reason."""
        crossed = {
            "market_ticker": "KX-CROSSED",
            "yes_bid": "0.52",
            "yes_ask": "0.48",
            "yes_bid_size": "100",
            "yes_ask_size": "100",
        }
        quote = normalize_kalshi_book(crossed, received_at=datetime.now(timezone.utc).isoformat())
        self.assertFalse(quote.valid)
        self.assertFalse(quote.is_executable())
        self.assertIn("Crossed or locked", quote.rejection_reason)

    def test_canonical_normalization_detects_stale_quotes(self):
        """Tests that quotes older than max_age_seconds are flagged as stale."""
        old_time = (datetime.now(timezone.utc) - timedelta(seconds=25)).isoformat()
        payload = {
            "market_ticker": "KX-STALE",
            "yes_bid": "0.40",
            "yes_ask": "0.45",
            "yes_bid_size": "100",
            "yes_ask_size": "100",
            "exchange_timestamp": old_time,
        }
        quote = normalize_kalshi_book(payload, received_at=datetime.now(timezone.utc).isoformat(), max_age_seconds=10.0)
        self.assertTrue(quote.valid)
        self.assertTrue(quote.stale)
        self.assertFalse(quote.is_executable())


    def test_ladder_vwap_calculation_and_insufficient_depth(self):
        """Verifies ladder VWAP across levels and insufficient liquidity detection."""
        levels = [[0.40, 100.0], [0.42, 200.0], [0.45, 300.0]]
        # 1. Fill 150: 100 @ 0.40 + 50 @ 0.42 = 40 + 21 = 61 -> 61 / 150 = 0.406667
        vwap, filled = compute_ladder_vwap(levels, 150.0)
        self.assertIsNotNone(vwap)
        self.assertAlmostEqual(vwap, round(61.0 / 150.0, 6))
        self.assertEqual(filled, 150.0)

        # 2. Target 1000 with only 600 available -> (None, 600.0)
        vwap_insufficient, filled_part = compute_ladder_vwap(levels, 1000.0)
        self.assertIsNone(vwap_insufficient)
        self.assertEqual(filled_part, 600.0)

    def test_fee_schedule_and_cost_engine_separation(self):
        """Verifies known trading fees separated from configurable slippage."""
        sched = get_fee_schedule()
        self.assertEqual(sched.version, DEFAULT_FEE_VERSION)

        # Kalshi fee at P=0.40: min(0.07 * 0.40 * 0.60, 0.02) = min(0.0168, 0.02) = 0.0168
        fee_k = calculate_venue_taker_fee("kalshi", 0.40, 100.0)
        self.assertEqual(fee_k["per_unit_fee"], 0.0168)
        self.assertEqual(fee_k["total_fee"], 1.68)

        # Polymarket 0 bps CLOB taker fee
        fee_p = calculate_venue_taker_fee("polymarket", 0.38, 100.0)
        self.assertEqual(fee_p["per_unit_fee"], 0.0)
        self.assertEqual(fee_p["total_fee"], 0.0)

        # Net opportunity cost model with 10 bps slippage
        cost_eval = evaluate_net_opportunity_costs(
            buy_venue="polymarket",
            sell_venue="kalshi",
            buy_price=0.38,
            sell_price=0.42,
            quantity=100.0,
            slippage_bps=10.0
        )
        # Gross = 0.42 - 0.38 = 0.04 -> $4.00
        self.assertEqual(cost_eval["gross_spread"], 0.04)
        self.assertEqual(cost_eval["total_gross_profit"], 4.00)
        # Kalshi fee at 0.42: min(0.07 * 0.42 * 0.58, 0.02) = 0.017052 -> total $1.7052 -> round(1.71)
        self.assertEqual(cost_eval["total_known_fees"], round(fee_k["total_fee"] if False else 0.017052 * 100, 4))
        # Net profit after fees is positive
        self.assertGreater(cost_eval["total_net_profit"], 0.0)
        # Slippage cost is separated
        self.assertGreater(cost_eval["total_slippage_cost"], 0.0)

    def test_opportunity_state_machine_transitions(self):
        """Verifies candidate vs executable vs stale vs insufficient_liquidity states."""
        pair = {
            "polymarket_asset_id": "p_state",
            "kalshi_ticker": "k_state",
            "canonical_event": "State Machine Test",
            "equivalence_mapping": "direct",
            "expiration": self.future_exp,
            "verification_status": "verified",
        }
        # 1. Executable: Net spread > 0 and available liquidity > 0
        book_p = {
            "valid": 1,
            "best_bid": 0.30,
            "best_ask": 0.35,
            "bid_depth": 500,
            "ask_depth": 500,
            "exchange_timestamp": datetime.now(timezone.utc).isoformat()
        }
        book_k = {
            "valid": 1,
            "best_bid": 0.45,
            "best_ask": 0.50,
            "bid_depth": 500,
            "ask_depth": 500,
            "exchange_timestamp": datetime.now(timezone.utc).isoformat()
        }
        res_exec = compute_cross_venue_spread(pair, book_p, book_k)
        self.assertEqual(res_exec["status"], "executable")
        self.assertTrue(res_exec["is_arbitrage"])

        # 2. Candidate: Gross spread > 0, but fee eats entire spread (e.g. Poly ask 0.40, Kalshi bid 0.41, fee ~0.017)
        book_p_tight = {
            "valid": 1,
            "best_bid": 0.38,
            "best_ask": 0.40,
            "bid_depth": 500,
            "ask_depth": 500,
            "exchange_timestamp": datetime.now(timezone.utc).isoformat()
        }
        book_k_tight = {
            "valid": 1,
            "best_bid": 0.41,
            "best_ask": 0.45,
            "bid_depth": 500,
            "ask_depth": 500,
            "exchange_timestamp": datetime.now(timezone.utc).isoformat()
        }
        res_cand = compute_cross_venue_spread(pair, book_p_tight, book_k_tight)
        self.assertEqual(res_cand["status"], "candidate")
        self.assertFalse(res_cand["is_arbitrage"])

        # 3. Insufficient Liquidity: depth is 0
        book_p_empty = {
            "valid": 1,
            "best_bid": 0.30,
            "best_ask": 0.35,
            "bid_depth": 0,
            "ask_depth": 0,
            "exchange_timestamp": datetime.now(timezone.utc).isoformat()
        }
        res_liq = compute_cross_venue_spread(pair, book_p_empty, book_k)
        self.assertEqual(res_liq["status"], "insufficient_liquidity")
        self.assertFalse(res_liq["is_arbitrage"])

        # 4. Stale: quote age > 10s or delta > 5s
        book_p_stale = {
            "valid": 1,
            "best_bid": 0.30,
            "best_ask": 0.35,
            "bid_depth": 500,
            "ask_depth": 500,
            "exchange_timestamp": (datetime.now(timezone.utc) - timedelta(seconds=20)).isoformat()
        }
        res_stale = compute_cross_venue_spread(pair, book_p_stale, book_k)
        self.assertEqual(res_stale["status"], "stale")
        self.assertFalse(res_stale["is_arbitrage"])

    def test_cross_venue_fastapi_endpoints(self):
        """Verifies GET /api/v1/cross-venue/pairs, /fees, /spreads, /opportunities, /analytics."""
        client = TestClient(app)
        pair = {
            "polymarket_asset_id": "poly_ep_1",
            "kalshi_ticker": "KX_EP_1",
            "canonical_event": "Endpoint Test Event",
            "canonical_outcome": "YES",
            "equivalence_mapping": "direct",
            "resolution_source": "BLS Release",
            "resolution_criteria": "Settles YES on official print",
            "expiration": self.future_exp,
            "verification_status": "verified",
            "reviewer": "test_operator",
        }
        self.journal.save_contract_pair(pair)

        # 1. GET /api/v1/cross-venue/pairs
        r_pairs = client.get("/api/v1/cross-venue/pairs?status=verified")
        self.assertEqual(r_pairs.status_code, 200)
        p_data = r_pairs.json()
        self.assertGreaterEqual(p_data["total"], 1)

        # 2. GET /api/v1/cross-venue/pairs/{pair_id}
        r_single = client.get("/api/v1/cross-venue/pairs/poly_ep_1:KX_EP_1")
        self.assertEqual(r_single.status_code, 200)
        self.assertEqual(r_single.json()["pair"]["polymarket_asset_id"], "poly_ep_1")

        # 3. GET /api/v1/cross-venue/fees
        r_fees = client.get("/api/v1/cross-venue/fees")
        self.assertEqual(r_fees.status_code, 200)
        f_data = r_fees.json()
        self.assertEqual(f_data["default_version"], "v2026.1")
        self.assertIn("v2026.1", f_data["schedules"])

        # 4. GET /api/v1/cross-venue/opportunities
        r_opps = client.get("/api/v1/cross-venue/opportunities")
        self.assertEqual(r_opps.status_code, 200)
        self.assertIn("opportunities", r_opps.json())

        # 5. GET /api/v1/cross-venue/analytics
        r_ana = client.get("/api/v1/cross-venue/analytics?window=24h")
        self.assertEqual(r_ana.status_code, 200)
        self.assertIn(r_ana.json()["source"], ("clickhouse", "sqlite_local"))

        # 6. GET /api/v1/cross-venue/notifications
        r_notif = client.get("/api/v1/cross-venue/notifications")
        self.assertEqual(r_notif.status_code, 200)
        self.assertIsInstance(r_notif.json(), list)

    def test_operator_review_endpoint_with_auth(self):
        """Verifies PUT /api/v1/cross-venue/pairs/{pair_id}/review enforces auth and records review."""
        client = TestClient(app)
        os.environ["OPERATOR_KEY"] = "super-secret-key-123"

        pair = {
            "polymarket_asset_id": "poly_rev_1",
            "kalshi_ticker": "KX_REV_1",
            "canonical_event": "Review Test Event",
            "canonical_outcome": "YES",
            "equivalence_mapping": "direct",
            "resolution_source": "CFTC Order",
            "expiration": self.future_exp,
            "verification_status": "unverified",
        }
        self.journal.save_contract_pair(pair)

        review_payload = {
            "verification_status": "verified",
            "reviewer": "senior_risk_operator",
            "canonical_event": "Review Test Event Verified",
            "resolution_criteria": "Settles YES per rule 4",
        }

        # 1. Without auth header -> 403
        r_unauth = client.put("/api/v1/cross-venue/pairs/poly_rev_1:KX_REV_1/review", json=review_payload)
        self.assertEqual(r_unauth.status_code, 403)

        # 2. With valid auth header -> 200
        r_auth = client.put(
            "/api/v1/cross-venue/pairs/poly_rev_1:KX_REV_1/review",
            json=review_payload,
            headers={"X-Operator-Key": "super-secret-key-123"}
        )
        self.assertEqual(r_auth.status_code, 200)
        self.assertEqual(r_auth.json()["pair"]["verification_status"], "verified")
        self.assertEqual(r_auth.json()["pair"]["reviewer"], "senior_risk_operator")

        del os.environ["OPERATOR_KEY"]

    def test_notification_engine_cooldown_and_transitions(self):
        """Verifies notification generation, deduplication within cooldown, and invalidation transitions."""
        from backend.notifications import NotificationEngine, NotificationConfig

        config = NotificationConfig(
            min_net_spread_bps=10.0,
            min_executable_qty=5.0,
            cooldown_seconds=30.0
        )
        engine = NotificationEngine(config)

        spread = {
            "pair_id": "p_notif:k_notif",
            "canonical_event": "Fed Rate",
            "status": "executable",
            "net_spread": 0.05,
            "best_spread_bps": 1250.0,
            "executable_qty": 100.0,
            "opportunity_type": "long_poly_short_kalshi",
            "polymarket": {"valid": 1},
            "quote_age_seconds": {"polymarket": 0.5, "kalshi": 0.6, "venue_delta": 0.1}
        }

        # 1. Initial executable quote emits executable_quote event
        ev1 = engine.evaluate_spread(spread, current_time=1000.0)
        self.assertIsNotNone(ev1)
        self.assertEqual(ev1.event_type, "executable_quote")

        # 2. Same spread 5s later (within 30s cooldown) is dropped (returns None)
        ev2 = engine.evaluate_spread(spread, current_time=1005.0)
        self.assertIsNone(ev2)

        # 3. Same spread 35s later (after 30s cooldown) is emitted
        ev3 = engine.evaluate_spread(spread, current_time=1036.0)
        self.assertIsNotNone(ev3)

        # 4. Status transitions to stale -> emits invalidated_opportunity
        stale_spread = dict(spread)
        stale_spread["status"] = "stale"
        ev_inv = engine.evaluate_spread(stale_spread, current_time=1040.0)
        self.assertIsNotNone(ev_inv)
        self.assertEqual(ev_inv.event_type, "invalidated_opportunity")

    def test_durable_outbox_batch_for_cross_venue_tables(self):
        """Verifies SQLite outbox batches for all Phase 6 cross-venue tables."""
        # 1. Spread Observation
        obs_id = self.journal.record_spread_observation({
            "pair_id": "p_durable:k_durable",
            "canonical_event": "Outbox Batch Test Event",
            "canonical_outcome": "YES",
            "equivalence_mapping": "direct",
            "poly_best_bid": 0.35,
            "poly_best_ask": 0.38,
            "kalshi_best_bid": 0.44,
            "kalshi_best_ask": 0.48,
            "poly_ask_depth": 500,
            "kalshi_bid_depth": 300,
            "gross_spread": 0.06,
            "gross_spread_bps": 1500.0,
            "net_spread": 0.043,
            "net_spread_bps": 1075.0,
            "poly_fee": 0.0,
            "kalshi_fee": 0.017,
            "slippage_cost": 0.0004,
            "executable_qty": 300,
            "opportunity_type": "long_poly_short_kalshi",
            "status": "executable",
            "quote_age_poly": 0.5,
            "quote_age_kalshi": 0.6,
            "venue_delta_seconds": 0.1
        })
        self.assertIsNotNone(obs_id)

        # 2. Create pending batch in SQLite
        batch = self.journal.get_or_create_pending_batch("spread_observations")
        self.assertIsNotNone(batch)
        self.assertEqual(batch["target_table"], "spread_observations")
        self.assertEqual(batch["item_count"], 1)
        self.assertTrue(len(batch["dedup_token"]) == 64)  # SHA-256
        self.assertEqual(batch["status"], "pending")

        # 3. Acknowledge batch
        self.journal.acknowledge_batch(batch["batch_id"])
        acked = self.journal.get_batch(batch["batch_id"])
        self.assertEqual(acked["status"], "acknowledged")

    def test_cross_venue_websocket_feed(self):
        """Verifies WebSocket /ws/cross-venue connects and emits initial scanner frame."""
        client = TestClient(app)
        with client.websocket_connect("/ws/cross-venue") as ws:
            frame = ws.receive_json()
            self.assertEqual(frame["type"], "initial_state")
            self.assertIn("pairs", frame["data"])
            self.assertIn("spreads", frame["data"])
            self.assertIn("opportunities", frame["data"])
            self.assertIn("venue_health", frame["data"])


if __name__ == "__main__":
    unittest.main()

"""Deterministic CLOB fee edge cases and depth execution cost checks."""
from decimal import Decimal

import httpx
import pytest

from backend.fees import (
    calculate_venue_taker_fee,
    fetch_polymarket_token_fee_config,
    normalize_polymarket_api_fee_config,
)
from backend.scanner import compute_cross_venue_spread
from tests.test_phase6_fee_runtime import books, pair


def clob_config(clob_payload, gamma_payload=None):
    def respond(request):
        if request.url.host == "gamma-api.polymarket.com":
            return httpx.Response(200, json=[gamma_payload or {
                "conditionId": "0xcondition", "clobTokenIds": '["token-1"]'}])
        assert request.url.path == "/clob-markets/0xcondition"
        return httpx.Response(200, json=clob_payload)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        return fetch_polymarket_token_fee_config("token-1", client=client, use_cache=False)


def test_explicit_null_fd_requires_fee_disabled_market_metadata():
    disabled = {"conditionId": "0xcondition", "clobTokenIds": '["token-1"]',
                "feesEnabled": False}
    config = clob_config({"fd": None}, disabled)
    assert config["fee_status"] == "known"
    assert config["fees_enabled"] is False
    assert config["fd"] is None
    assert config["source"] == "gamma_fees_enabled_false"
    assert calculate_venue_taker_fee("polymarket", .5, 10,
                                     poly_token_fee_config=config)["total_fee"] == 0
    assert normalize_polymarket_api_fee_config({"fd": None, "feesEnabled": False})["fee_status"] == "known"
    assert normalize_polymarket_api_fee_config({"fd": None})["fee_status"] == "unknown"
    assert normalize_polymarket_api_fee_config({"fd": None, "feesEnabled": True})["fee_status"] == "unknown"


@pytest.mark.parametrize("clob_payload", [
    {"feesEnabled": True, "feeType": "sports_fees_v2"},
    {"feesEnabled": False},
])
def test_missing_fd_field_fails_closed_even_if_gamma_has_category(clob_payload):
    config = clob_config(clob_payload,
                         {"conditionId": "0xcondition", "clobTokenIds": '["token-1"]',
                          "feeType": "sports_fees_v2"})
    assert config["fee_status"] == "unknown"
    assert config["source"] == "missing_clob_fd"


@pytest.mark.parametrize("fd", [{}, {"r": ".04", "e": "bad", "to": True},
                                     {"r": .04, "e": 1, "to": "true"}])
def test_malformed_fd_object_fails_closed(fd):
    assert clob_config({"fd": fd})["fee_status"] == "unknown"


def test_fee_enabled_valid_clob_curve():
    config = clob_config({"feesEnabled": True, "fd": {"r": .04, "e": 2, "to": True}})
    assert config["fee_status"] == "known"
    assert config["fd"] == {"r": .04, "e": 2.0, "to": True}
    assert calculate_venue_taker_fee("polymarket", .4, 100,
                                     poly_token_fee_config=config)["total_fee"] == .2304


def test_fee_configuration_unavailable_fails_closed():
    def unavailable(request):
        return httpx.Response(503)

    with httpx.Client(transport=httpx.MockTransport(unavailable)) as client:
        config = fetch_polymarket_token_fee_config("token-1", client=client, use_cache=False)
    assert config["fee_status"] == "unknown"
    left, right = books(.38, .4, .46, .49, config)
    result = compute_cross_venue_spread(pair(), left, right)
    assert result["fee_status"] == "unknown"
    assert result["status"] != "executable"


def test_decimal_rounding_boundaries_for_polymarket_and_kalshi():
    curve = {"fd": {"r": .04, "e": 1, "to": True}}
    below = calculate_venue_taker_fee("polymarket", Decimal("0.5"), Decimal("0.000499"),
                                     poly_token_fee_config=curve)
    tie = calculate_venue_taker_fee("polymarket", Decimal("0.5"), Decimal("0.0005"),
                                   poly_token_fee_config=curve)
    assert below["total_fee"] == 0.0
    assert tie["total_fee"] == .00001
    exact_cent = calculate_venue_taker_fee("kalshi", Decimal("0.4"), 100,
                                            kalshi_ticker="FED-27DEC")
    above_cent = calculate_venue_taker_fee("kalshi", Decimal("0.4000001"), 100,
                                            kalshi_ticker="FED-27DEC")
    assert exact_cent["total_fee"] == 1.68
    assert above_cent["total_fee"] == 1.69


def test_small_clob_rate_retains_decimal_precision():
    curve = {"fd": {"r": "0.000000001", "e": 1, "to": True}}
    fee = calculate_venue_taker_fee("polymarket", Decimal("0.5"), 1_000_000,
                                    poly_token_fee_config=curve)
    assert fee["total_fee"] == .00025


def test_direct_depth_fee_uses_each_fill_price_and_quantity():
    left, right = books(.15, .2, .8, .85, {"fd": {"r": .04, "e": 2, "to": True}})
    left.update(bid_depth=2, ask_depth=2, bids=[[.15, 2]], asks=[[.2, 1], [.6, 1]])
    right.update(bid_depth=2, ask_depth=2, bids=[[.8, 1], [.7, 1]], asks=[[.85, 2]])
    spread = compute_cross_venue_spread(pair(), left, right, requested_sizes=[2])
    assert spread["executable_qty"] == 1  # Top quote does not claim deeper liquidity.
    row = spread["vwap_by_size"]["2"]
    assert row["fillable"] is True
    assert row["vwap_spread"] == .35
    assert row["buy_venue_fee"] == .00332  # .00102 at .20 plus .00230 at .60
    assert row["sell_venue_fee"] == .03    # Kalshi cent ceiling once for the order
    assert row["net_spread"] == .33334
    assert row["buy_venue_fee"] != calculate_venue_taker_fee(
        "polymarket", row["buy_vwap"], 2,
        poly_token_fee_config=left["token_fee_config"])["total_fee"]


def test_inverse_depth_fee_uses_each_fill_price_and_quantity():
    left, right = books(.1, .2, .1, .3, {"fd": {"r": .04, "e": 2, "to": True}})
    left.update(bid_depth=2, ask_depth=2, bids=[[.1, 2]], asks=[[.2, 1], [.4, 1]])
    right.update(bid_depth=2, ask_depth=2, bids=[[.1, 2]], asks=[[.3, 1], [.4, 1]])
    row = compute_cross_venue_spread(pair("inverse"), left, right,
                                     requested_sizes=[2])["vwap_by_size"]["2"]
    assert row["fillable"] is True
    assert row["vwap_spread"] == .35
    assert row["buy_venue_fee"] == .00332
    assert row["sell_venue_fee"] == .04
    assert row["net_spread"] == .32834


def test_unknown_fee_in_fillable_depth_row_stays_unknown():
    left, right = books(.15, .2, .8, .85, {"fee_status": "unknown"})
    left.update(bid_depth=2, ask_depth=2, bids=[[.15, 2]], asks=[[.2, 1], [.6, 1]])
    right.update(bid_depth=2, ask_depth=2, bids=[[.8, 1], [.7, 1]], asks=[[.85, 2]])
    row = compute_cross_venue_spread(pair(), left, right, requested_sizes=[2])["vwap_by_size"]["2"]
    assert row["fee_status"] == "unknown"
    assert row["net_spread"] is None
    assert row["is_executable"] is False


def test_stale_depth_row_cannot_claim_executable():
    left, right = books(.15, .2, .8, .85, {"fd": {"r": .04, "e": 2, "to": True}})
    left.update(bid_depth=2, ask_depth=2, bids=[[.15, 2]], asks=[[.2, 1], [.6, 1]],
                last_received_timestamp="2020-01-01T00:00:00Z")
    right.update(bid_depth=2, ask_depth=2, bids=[[.8, 1], [.7, 1]], asks=[[.85, 2]],
                 last_received_timestamp="2020-01-01T00:00:00Z")
    spread = compute_cross_venue_spread(pair(), left, right, requested_sizes=[2])
    assert spread["status"] == "stale"
    assert spread["vwap_by_size"]["2"]["is_executable"] is False

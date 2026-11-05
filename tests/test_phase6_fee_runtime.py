"""Independent deterministic checks for the active Phase 6 fee configuration."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from backend.fees import (
    DEFAULT_FEE_VERSION,
    calculate_venue_taker_fee,
    fetch_polymarket_token_fee_config,
    get_fee_schedule,
    get_polymarket_market_fee_rate,
    normalize_polymarket_api_fee_config,
)
from backend.main import settings
from backend.scanner import compute_cross_venue_spread
from backend.state import MarketState


def pair(mapping="direct"):
    return {"polymarket_asset_id": "token-1", "kalshi_ticker": "FED-27DEC",
            "verification_status": "verified", "equivalence_mapping": mapping,
            "expiration": (datetime.now(timezone.utc) + timedelta(days=2)).isoformat(),
            "polymarket_category": "sports"}


def books(poly_bid, poly_ask, kalshi_bid, kalshi_ask, fee_config=None):
    left = {"valid": True, "best_bid": poly_bid, "best_ask": poly_ask,
            "bid_depth": 1, "ask_depth": 1}
    if fee_config is not None:
        left["token_fee_config"] = fee_config
    right = {"valid": True, "best_bid": kalshi_bid, "best_ask": kalshi_ask,
             "bid_depth": 1, "ask_depth": 1}
    return left, right


def test_v2026_2_is_default():
    assert DEFAULT_FEE_VERSION == "v2026.2"
    assert get_fee_schedule().version == "v2026.2"
    assert calculate_venue_taker_fee("kalshi", .42, 1, kalshi_ticker="FED-27DEC")["total_fee"] == .02


def test_runtime_scanner_uses_configured_active_version(monkeypatch):
    left, right = books(.38, .40, .46, .49)
    for configured, expected in (("v2026.1", .0174), ("v2026.2", .02)):
        monkeypatch.setenv("POLYPULSE_FEE_VERSION", configured)
        active = settings()["fee_version"]
        state = MarketState.__new__(MarketState)
        state.fee_version = active
        state.journal = SimpleNamespace(contract_pairs=lambda verified_only: [pair()])
        state.get = lambda venue, market_id: left if venue == "polymarket" else right
        result = state.spreads()[0]
        assert result["fee_version"] == configured
        assert result["kalshi_fee"] == expected
    monkeypatch.setenv("POLYPULSE_FEE_VERSION", "unsupported")
    with pytest.raises(ValueError, match="POLYPULSE_FEE_VERSION"):
        settings()


def test_sports_category_coefficient_is_five_percent():
    assert get_polymarket_market_fee_rate("sports")["fee_rate"] == .05
    assert calculate_venue_taker_fee("polymarket", .5, 100, poly_category="sports")["total_fee"] == 1.25


def test_token_specific_curve_overrides_sports_category():
    config = {"fd": {"r": .04, "e": 2, "to": False}}
    result = calculate_venue_taker_fee("polymarket", .4, 100,
                                       poly_category="sports", poly_token_fee_config=config)
    assert result["fee_rate"] == .04
    assert result["fee_exponent"] == 2
    assert result["taker_only"] is False
    assert result["total_fee"] == .2304


def test_clob_market_fd_fetch_preserves_all_fields():
    requests = []

    def respond(request):
        requests.append(str(request.url))
        if request.url.host == "gamma-api.polymarket.com":
            return httpx.Response(200, json=[
                {"conditionId": "0xwrong", "clobTokenIds": '["token-11"]'},
                {"conditionId": "0xcondition", "clobTokenIds": '["token-1"]'},
            ])
        if request.url.path == "/clob-markets/0xcondition":
            return httpx.Response(200, json={"fd": {"r": .04, "e": 2, "to": True}})
        raise AssertionError(f"Unexpected request: {request.url}")

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        result = fetch_polymarket_token_fee_config("token-1", client=client, use_cache=False)
    assert result["fee_status"] == "known"
    assert result["fd"] == {"r": .04, "e": 2.0, "to": True}
    assert result["condition_id"] == "0xcondition"
    assert len(requests) == 2


def test_uninterpretable_clob_fd_does_not_fall_back_to_gamma_category():
    def respond(request):
        if request.url.host == "gamma-api.polymarket.com":
            return httpx.Response(200, json=[{"conditionId": "0xcondition",
                                               "clobTokenIds": '["token-1"]',
                                               "feeType": "sports_fees_v2"}])
        return httpx.Response(200, json={"fd": {"r": .05, "to": True}})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        config = fetch_polymarket_token_fee_config("token-1", client=client, use_cache=False)
    assert config["fee_status"] == "unknown"
    left, right = books(.38, .4, .46, .49, config)
    assert compute_cross_venue_spread(pair(), left, right)["status"] != "executable"


def test_non_default_exponent_formula():
    result = calculate_venue_taker_fee("polymarket", .4, 100,
                                       poly_token_fee_config={"fd": {"r": .04, "e": 2, "to": True}})
    assert result["total_fee"] == .2304


@pytest.mark.parametrize("config", [
    {"fd": {"r": .04, "to": True}},
    {"fd": {"r": .04, "e": -1, "to": True}},
    {"fd": {"r": .04, "e": 1, "to": "yes"}},
    {"base_fee": 500},
    {"base_fee": 500, "feesEnabled": False},
])
def test_unknown_configuration_fails_closed(config):
    normalized = normalize_polymarket_api_fee_config(config)
    assert normalized["fee_status"] == "unknown"
    if "base_fee" in config:
        assert normalized["base_fee_bps"] == 500
        assert normalized["base_fee_unit"] == "basis_points"
    left, right = books(.38, .40, .46, .49, config)
    result = compute_cross_venue_spread(pair(), left, right)
    assert result["fee_status"] == "unknown"
    assert result["net_spread"] is None
    assert result["status"] != "executable"


@pytest.mark.parametrize("mapping,prices,expected_type,poly_price", [
    ("direct", (.38, .40, .46, .49), "long_poly_short_kalshi", .40),
    ("direct", (.48, .50, .39, .42), "long_kalshi_short_poly", .48),
    ("inverse", (.43, .45, .46, .48), "mint_both_long", .45),
    ("inverse", (.54, .56, .52, .54), "merge_both_short", .54),
])
def test_all_execution_directions_use_token_curve_and_cent_rounded_kalshi(
        mapping, prices, expected_type, poly_price):
    left, right = books(*prices, fee_config={"fd": {"r": .04, "e": 2, "to": True}})
    result = compute_cross_venue_spread(pair(mapping), left, right)
    assert result["opportunity_type"] == expected_type
    assert result["fee_version"] == "v2026.2"
    assert result["kalshi_fee"] == .02
    assert result["poly_fee"] == round(.04 * (poly_price * (1 - poly_price)) ** 2, 5)
    assert result["fee_status"] == "known"

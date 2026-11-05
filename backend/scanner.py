"""
PolyPulse Phase 6: Cross-Venue Intelligence & Arbitrage Scanner Engine.
Provides contract matching evaluation, direct & inverse spread calculations,
executable depth, fee adjustments, and strict gating for verified pairs.
"""
from datetime import datetime, timezone
from typing import Dict, Any, Optional, Tuple


def calculate_kalshi_fee(price: float) -> float:
    """
    Standard Kalshi taker fee schedule for binary prediction contracts:
    fee = min(0.07 * price * (1.0 - price), 0.02)
    """
    if price <= 0.0 or price >= 1.0:
        return 0.0
    return round(min(0.07 * price * (1.0 - price), 0.02), 6)


def calculate_polymarket_fee(price: float) -> float:
    """
    Standard Polymarket orderbook taker fee (0.00 on CTF orderbook execution).
    """
    return 0.0


def validate_contract_compatibility(pair: Dict[str, Any], now_dt: Optional[datetime] = None) -> Tuple[bool, str]:
    """
    Validates whether a contract pair is compatible, non-ambiguous, and non-expired.
    Enforces the Zero Title-Only Matching Invariant.
    """
    now = now_dt or datetime.now(timezone.utc)

    # 1. Reject empty or title-only matches
    canonical_event = pair.get("canonical_event") or pair.get("event_description", "")
    resolution_source = pair.get("resolution_source", "")
    resolution_criteria = pair.get("resolution_criteria", "")

    if not canonical_event.strip():
        return False, "Canonical event description is required"
    if not resolution_source.strip():
        return False, "Resolution source is required; matching on title alone is forbidden"

    # 2. Check expiration validity
    exp_str = pair.get("expiration")
    if not exp_str:
        return False, "Expiration timestamp is required"
    try:
        exp_dt = datetime.fromisoformat(exp_str.replace("Z", "+00:00"))
        if exp_dt.tzinfo is None:
            return False, "Expiration must be timezone-aware UTC"
        if exp_dt <= now:
            return False, f"Contract pair has expired (expiration={exp_str}, now={now.isoformat()})"
    except Exception as exc:
        return False, f"Invalid expiration format: {exc}"

    # 3. Check individual venue expirations if provided
    poly_exp_str = pair.get("polymarket_expiration")
    kalshi_exp_str = pair.get("kalshi_expiration")
    if poly_exp_str and kalshi_exp_str:
        try:
            poly_exp = datetime.fromisoformat(poly_exp_str.replace("Z", "+00:00"))
            kalshi_exp = datetime.fromisoformat(kalshi_exp_str.replace("Z", "+00:00"))
            disparity_hours = abs((poly_exp - kalshi_exp).total_seconds()) / 3600.0
            settlement_details = pair.get("settlement_details", "")
            if disparity_hours > 48.0 and not settlement_details.strip():
                return False, f"Expiration disparity exceeds 48h ({disparity_hours:.1f}h) without explanatory settlement details"
        except Exception:
            pass

    # 4. Check equivalence mapping enum
    mapping = pair.get("equivalence_mapping", "direct")
    if mapping not in ("direct", "inverse"):
        return False, f"Invalid equivalence mapping '{mapping}'; must be 'direct' or 'inverse'"

    return True, "Compatible"


def compute_cross_venue_spread(
    pair: Dict[str, Any],
    left: Dict[str, Any],
    right: Dict[str, Any],
    now_dt: Optional[datetime] = None
) -> Optional[Dict[str, Any]]:
    """
    Computes mathematical cross-venue spread between Polymarket (left) and Kalshi (right)
    for a verified contract pair.

    Supports both 'direct' (Poly YES == Kalshi YES) and 'inverse' (Poly YES == Kalshi NO) mappings.
    """
    now = now_dt or datetime.now(timezone.utc)

    # 1. Gate on verification status: ONLY verified pairs enter executable scanner
    if pair.get("verification_status") != "verified":
        return None

    # 2. Gate on expiration
    exp_str = pair.get("expiration")
    if not exp_str:
        return None
    try:
        exp_dt = datetime.fromisoformat(exp_str.replace("Z", "+00:00"))
        if exp_dt <= now:
            return None
    except Exception:
        return None

    # 3. Gate on orderbook health
    if not left or not right:
        return None
    if not left.get("valid") or not right.get("valid"):
        return None

    poly_bid = left.get("best_bid")
    poly_ask = left.get("best_ask")
    kalshi_bid = right.get("best_bid")
    kalshi_ask = right.get("best_ask")

    # Both books must have valid active quotes
    if poly_bid is None or poly_ask is None or kalshi_bid is None or kalshi_ask is None:
        return None
    if poly_ask <= poly_bid or kalshi_ask <= kalshi_bid:
        return None  # Crossed or locked books

    # Extract top of book quantities (or default to depth)
    poly_bid_qty = left.get("bid_depth", 0.0)
    poly_ask_qty = left.get("ask_depth", 0.0)
    kalshi_bid_qty = right.get("bid_depth", 0.0)
    kalshi_ask_qty = right.get("ask_depth", 0.0)

    mapping = pair.get("equivalence_mapping", "direct")
    canonical_event = pair.get("canonical_event") or pair.get("event_description", "")
    canonical_outcome = pair.get("canonical_outcome") or pair.get("outcome_equivalence", "")

    buy_poly_sell_kalshi = None
    buy_kalshi_sell_polymarket = None
    best_spread = None
    best_spread_bps = None
    opportunity_type = "none"
    executable_qty = 0.0
    net_spread = None
    poly_fee = 0.0
    kalshi_fee = 0.0

    if mapping == "direct":
        # Direct: Poly YES == Kalshi YES
        # Leg 1: Buy Poly YES at ask, Sell Kalshi YES at bid
        # Profit = Kalshi Bid - Poly Ask
        buy_poly = round(kalshi_bid - poly_ask, 6)
        # Leg 2: Buy Kalshi YES at ask, Sell Poly YES at bid
        # Profit = Poly Bid - Kalshi Ask
        buy_kalshi = round(poly_bid - kalshi_ask, 6)

        buy_poly_sell_kalshi = buy_poly
        buy_kalshi_sell_polymarket = buy_kalshi

        if buy_poly > buy_kalshi:
            best_spread = buy_poly
            opportunity_type = "long_poly_short_kalshi" if buy_poly > 0 else "none"
            executable_qty = min(poly_ask_qty, kalshi_bid_qty)
            entry_price = poly_ask
            poly_fee = calculate_polymarket_fee(poly_ask)
            kalshi_fee = calculate_kalshi_fee(kalshi_bid)
        else:
            best_spread = buy_kalshi
            opportunity_type = "long_kalshi_short_poly" if buy_kalshi > 0 else "none"
            executable_qty = min(kalshi_ask_qty, poly_bid_qty)
            entry_price = kalshi_ask
            poly_fee = calculate_polymarket_fee(poly_bid)
            kalshi_fee = calculate_kalshi_fee(kalshi_ask)

        if entry_price > 0:
            best_spread_bps = round((best_spread / entry_price) * 10_000.0, 2)
        net_spread = round(best_spread - poly_fee - kalshi_fee, 6)

    elif mapping == "inverse":
        # Inverse: Poly YES == Kalshi NO (Poly YES + Kalshi YES covers all states to payout 1.00)
        # Leg 1 (Mint / Long Both): Buy Poly YES at ask, Buy Kalshi YES at ask
        # Cost = Poly Ask + Kalshi Ask; Guaranteed Payout = 1.00
        # Profit = 1.00 - (Poly Ask + Kalshi Ask)
        mint_spread = round(1.00 - (poly_ask + kalshi_ask), 6)

        # Leg 2 (Merge / Short Both): Sell Poly YES at bid, Sell Kalshi YES at bid
        # Upfront Revenue = Poly Bid + Kalshi Bid; Max Payout Liability = 1.00
        # Profit = (Poly Bid + Kalshi Bid) - 1.00
        merge_spread = round((poly_bid + kalshi_bid) - 1.00, 6)

        # Map to legacy directional fields for UI compatibility
        buy_poly_sell_kalshi = mint_spread
        buy_kalshi_sell_polymarket = merge_spread

        if mint_spread > merge_spread:
            best_spread = mint_spread
            opportunity_type = "mint_both_long" if mint_spread > 0 else "none"
            executable_qty = min(poly_ask_qty, kalshi_ask_qty)
            entry_price = poly_ask + kalshi_ask
            poly_fee = calculate_polymarket_fee(poly_ask)
            kalshi_fee = calculate_kalshi_fee(kalshi_ask)
        else:
            best_spread = merge_spread
            opportunity_type = "merge_both_short" if merge_spread > 0 else "none"
            executable_qty = min(poly_bid_qty, kalshi_bid_qty)
            entry_price = 1.00
            poly_fee = calculate_polymarket_fee(poly_bid)
            kalshi_fee = calculate_kalshi_fee(kalshi_bid)

        if entry_price > 0:
            best_spread_bps = round((best_spread / entry_price) * 10_000.0, 2)
        net_spread = round(best_spread - poly_fee - kalshi_fee, 6)

    is_arbitrage = bool(best_spread is not None and best_spread > 0)

    return {
        "pair_id": f"{pair['polymarket_asset_id']}:{pair['kalshi_ticker']}",
        "polymarket_asset_id": pair["polymarket_asset_id"],
        "kalshi_ticker": pair["kalshi_ticker"],
        "name": canonical_event,
        "canonical_event": canonical_event,
        "canonical_outcome": canonical_outcome,
        "equivalence_mapping": mapping,
        "resolution_source": pair.get("resolution_source", ""),
        "resolution_criteria": pair.get("resolution_criteria", ""),
        "settlement_details": pair.get("settlement_details", ""),
        "expiration": exp_str,
        "reviewer": pair.get("reviewer", ""),
        "verified_at": pair.get("verified_at"),
        "polymarket": left,
        "kalshi": right,
        "buy_polymarket_sell_kalshi": buy_poly_sell_kalshi,
        "buy_kalshi_sell_polymarket": buy_kalshi_sell_polymarket,
        "best_spread": best_spread,
        "best_spread_bps": best_spread_bps,
        "net_spread": net_spread,
        "poly_fee": poly_fee,
        "kalshi_fee": kalshi_fee,
        "executable_qty": executable_qty,
        "executable_notional": round(executable_qty * (poly_ask if mapping == "direct" else 1.0), 2),
        "opportunity_type": opportunity_type,
        "is_arbitrage": is_arbitrage,
        "note": "Gross quote difference before fees, depth and settlement risk"
    }

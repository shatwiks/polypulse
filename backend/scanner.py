"""
PolyPulse Phase 6: Cross-Venue Intelligence & Arbitrage Scanner Engine.
Provides:
- 8-state Opportunity State Machine:
  candidate | executable | insufficient_liquidity | stale | invalid_book | unverified_contract | expired | closed
- Depth-aware VWAP calculations across multiple order sizes
- Clock synchronization & quote freshness validation
- Exact fee deduction (via backend.fees) and net profit modeling
- Direct and Inverse (Mint/Merge) binary outcome arbitrage
"""
import math
from datetime import datetime, timezone
from typing import Dict, Any, Optional, Tuple, List

from backend.fees import (
    calculate_venue_taker_fee,
    evaluate_net_opportunity_costs,
    DEFAULT_FEE_VERSION
)


def calculate_kalshi_fee(price: float) -> float:
    """Standard Kalshi taker fee per contract."""
    info = calculate_venue_taker_fee("kalshi", price, 1.0)
    return info["per_unit_fee"]


def calculate_polymarket_fee(
    price: float,
    quantity: float = 1.0,
    poly_category: Optional[str] = None,
    poly_fee_rate: Optional[float] = None,
    poly_fees_enabled: Optional[bool] = None,
    poly_token_fee_config: Optional[Dict[str, Any]] = None
) -> Optional[float]:
    """Calculates Polymarket taker fee per contract using C * feeRate * P * (1 - P)."""
    info = calculate_venue_taker_fee(
        "polymarket",
        price,
        quantity,
        poly_category=poly_category,
        poly_fee_rate=poly_fee_rate,
        poly_fees_enabled=poly_fees_enabled,
        poly_token_fee_config=poly_token_fee_config
    )
    return info["per_unit_fee"]


def compute_ladder_vwap(levels: List[List[float]], target_qty: float) -> Tuple[Optional[float], float]:
    """
    Computes depth-weighted volume weighted average price (VWAP) across sorted price levels [[price, qty], ...].
    Returns (vwap_price, filled_qty). If available depth < target_qty, returns (None, available_depth).
    """
    if not levels or target_qty <= 0.0:
        return None, 0.0

    remaining = target_qty
    notional = 0.0
    total_filled = 0.0

    for level in levels:
        price = float(level[0])
        qty = float(level[1])
        fill = min(remaining, qty)
        notional += fill * price
        total_filled += fill
        remaining -= fill
        if remaining <= 1e-9:
            break

    if total_filled < target_qty - 1e-6:
        # Insufficient liquidity to fill target quantity
        return None, round(total_filled, 4)

    return round(notional / total_filled, 6), round(total_filled, 4)


def validate_contract_compatibility(pair: Dict[str, Any], now_dt: Optional[datetime] = None) -> Tuple[bool, str]:
    """
    Validates whether a contract pair is compatible, non-ambiguous, and non-expired.
    Enforces the Zero Title-Only Matching Invariant.
    """
    now = now_dt or datetime.now(timezone.utc)

    canonical_event = pair.get("canonical_event") or pair.get("event_description", "")
    resolution_source = pair.get("resolution_source", "")
    resolution_criteria = pair.get("resolution_criteria", "")

    if not canonical_event.strip():
        return False, "Canonical event description is required"
    if not resolution_source.strip():
        return False, "Resolution source is required; matching on title alone is forbidden"

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

    mapping = pair.get("equivalence_mapping", "direct")
    if mapping not in ("direct", "inverse"):
        return False, f"Invalid equivalence mapping '{mapping}'; must be 'direct' or 'inverse'"

    return True, "Compatible"


def compute_cross_venue_spread(
    pair: Dict[str, Any],
    left: Dict[str, Any],
    right: Dict[str, Any],
    now_dt: Optional[datetime] = None,
    max_quote_age_seconds: float = 10.0,
    max_venue_delta_seconds: float = 5.0,
    requested_sizes: Optional[List[float]] = None,
    fee_version: str = DEFAULT_FEE_VERSION,
) -> Optional[Dict[str, Any]]:
    """
    Computes depth-aware cross-venue intelligence, state-machine opportunity classification,
    and net spreads for verified Polymarket (left) and Kalshi (right) orderbooks.
    """
    now = now_dt or datetime.now(timezone.utc)
    requested_sizes = requested_sizes or [10.0, 50.0, 100.0, 500.0]

    # --- 1. Contract Gating ---
    if pair.get("verification_status") != "verified":
        return None

    exp_str = pair.get("expiration")
    if not exp_str:
        return None
    try:
        exp_dt = datetime.fromisoformat(exp_str.replace("Z", "+00:00"))
        if exp_dt <= now:
            return None
    except Exception:
        return None

    # --- 2. Orderbook Structure & Validation ---
    if not left or not right:
        return None
    if not left.get("valid") or not right.get("valid"):
        return None

    poly_bid = left.get("best_bid")
    poly_ask = left.get("best_ask")
    kalshi_bid = right.get("best_bid")
    kalshi_ask = right.get("best_ask")

    if poly_bid is None or poly_ask is None or kalshi_bid is None or kalshi_ask is None:
        return None
    if poly_ask <= poly_bid or kalshi_ask <= kalshi_bid:
        return None  # Crossed or locked books

    # --- 3. Timestamp Synchronization & Freshness ---
    left_ts_str = left.get("last_received_timestamp") or left.get("exchange_timestamp")
    right_ts_str = right.get("last_received_timestamp") or right.get("exchange_timestamp")
    
    quote_age_left = 0.0
    quote_age_right = 0.0
    venue_delta_s = 0.0
    is_stale = False

    if left_ts_str:
        try:
            l_dt = datetime.fromisoformat(str(left_ts_str).replace("Z", "+00:00"))
            quote_age_left = max(0.0, (now - l_dt).total_seconds())
        except Exception:
            pass

    if right_ts_str:
        try:
            r_dt = datetime.fromisoformat(str(right_ts_str).replace("Z", "+00:00"))
            quote_age_right = max(0.0, (now - r_dt).total_seconds())
        except Exception:
            pass

    if left_ts_str and right_ts_str:
        try:
            l_dt = datetime.fromisoformat(str(left_ts_str).replace("Z", "+00:00"))
            r_dt = datetime.fromisoformat(str(right_ts_str).replace("Z", "+00:00"))
            venue_delta_s = abs((l_dt - r_dt).total_seconds())
        except Exception:
            pass

    if quote_age_left > max_quote_age_seconds or quote_age_right > max_quote_age_seconds or venue_delta_s > max_venue_delta_seconds:
        is_stale = True

    # --- 4. Liquidity & Ladders ---
    poly_bid_qty = float(left.get("bid_depth", 0.0))
    poly_ask_qty = float(left.get("ask_depth", 0.0))
    kalshi_bid_qty = float(right.get("bid_depth", 0.0))
    kalshi_ask_qty = float(right.get("ask_depth", 0.0))

    mapping = pair.get("equivalence_mapping", "direct")
    canonical_event = pair.get("canonical_event") or pair.get("event_description", "")
    canonical_outcome = pair.get("canonical_outcome") or pair.get("outcome_equivalence", "")

    # Top-of-book spread variables
    buy_poly_sell_kalshi = None
    buy_kalshi_sell_polymarket = None
    best_spread = None
    best_spread_bps = None
    opportunity_type = "none"
    executable_qty = 0.0
    net_spread = None
    poly_fee = 0.0
    kalshi_fee = 0.0

    poly_asks = left.get("asks", [])
    poly_bids = left.get("bids", [])
    kalshi_asks = right.get("asks", [])
    kalshi_bids = right.get("bids", [])

    size_vwap_results = {}

    if mapping == "direct":
        buy_poly = round(kalshi_bid - poly_ask, 6)
        buy_kalshi = round(poly_bid - kalshi_ask, 6)

        buy_poly_sell_kalshi = buy_poly
        buy_kalshi_sell_polymarket = buy_kalshi

        if buy_poly > buy_kalshi:
            best_spread = buy_poly
            opportunity_type = "long_poly_short_kalshi" if buy_poly > 0 else "none"
            executable_qty = min(poly_ask_qty, kalshi_bid_qty)
            buy_v, sell_v = "polymarket", "kalshi"
            buy_p, sell_p = poly_ask, kalshi_bid
        else:
            best_spread = buy_kalshi
            opportunity_type = "long_kalshi_short_poly" if buy_kalshi > 0 else "none"
            executable_qty = min(kalshi_ask_qty, poly_bid_qty)
            buy_v, sell_v = "kalshi", "polymarket"
            buy_p, sell_p = kalshi_ask, poly_bid

        poly_token_fee_config = (
            left.get("token_fee_config")
            or left.get("fee_config")
            or pair.get("polymarket_fee_config")
            or pair.get("token_fee_config")
        )
        poly_fee_rate = left.get("fee_rate") if left.get("fee_rate") is not None else pair.get("polymarket_fee_rate")
        poly_taker_bps = left.get("taker_bps") if left.get("taker_bps") is not None else pair.get("polymarket_taker_bps")
        poly_fees_enabled = left.get("fees_enabled") if left.get("fees_enabled") is not None else pair.get("polymarket_fees_enabled")
        poly_category = left.get("category") or pair.get("polymarket_category") or pair.get("category")

        cost_eval = evaluate_net_opportunity_costs(
            version=fee_version,
            buy_venue=buy_v,
            sell_venue=sell_v,
            buy_price=buy_p,
            sell_price=sell_p,
            quantity=max(1.0, executable_qty),
            kalshi_ticker=pair.get("kalshi_ticker"),
            poly_taker_bps=poly_taker_bps,
            poly_fees_enabled=poly_fees_enabled,
            poly_category=poly_category,
            poly_fee_rate=poly_fee_rate,
            poly_token_fee_config=poly_token_fee_config,
        )
        fee_status = cost_eval.get("fee_status", "known")
        net_spread = cost_eval["net_spread"]
        estimated_net_spread = cost_eval.get("estimated_net_spread")
        best_spread_bps = cost_eval["gross_spread_bps"]
        poly_fee = cost_eval["buy_venue_fee"] if buy_v == "polymarket" else cost_eval["sell_venue_fee"]
        kalshi_fee = cost_eval["buy_venue_fee"] if buy_v == "kalshi" else cost_eval["sell_venue_fee"]
        strategy_category = "direct_spread"
        inventory_requirement = "Requires pre-existing contract inventory or 100% collateral to sell short leg"

        # VWAP calculations across requested sizes
        for sz in requested_sizes:
            if opportunity_type == "long_poly_short_kalshi":
                poly_v_ask, p_filled = compute_ladder_vwap(poly_asks, sz)
                kalshi_v_bid, k_filled = compute_ladder_vwap(kalshi_bids, sz)
                if poly_v_ask is not None and kalshi_v_bid is not None:
                    vwap_spread = round(kalshi_v_bid - poly_v_ask, 6)
                    size_vwap_results[str(int(sz))] = {
                        "fillable": True,
                        "buy_vwap": poly_v_ask,
                        "sell_vwap": kalshi_v_bid,
                        "vwap_spread": vwap_spread,
                    }
                else:
                    size_vwap_results[str(int(sz))] = {"fillable": False, "max_available": min(p_filled, k_filled)}
            elif opportunity_type == "long_kalshi_short_poly":
                kalshi_v_ask, k_filled = compute_ladder_vwap(kalshi_asks, sz)
                poly_v_bid, p_filled = compute_ladder_vwap(poly_bids, sz)
                if kalshi_v_ask is not None and poly_v_bid is not None:
                    vwap_spread = round(poly_v_bid - kalshi_v_ask, 6)
                    size_vwap_results[str(int(sz))] = {
                        "fillable": True,
                        "buy_vwap": kalshi_v_ask,
                        "sell_vwap": poly_v_bid,
                        "vwap_spread": vwap_spread,
                    }
                else:
                    size_vwap_results[str(int(sz))] = {"fillable": False, "max_available": min(k_filled, p_filled)}
            else:
                size_vwap_results[str(int(sz))] = {"fillable": False, "vwap_spread": best_spread}

    elif mapping == "inverse":
        mint_spread = round(1.00 - (poly_ask + kalshi_ask), 6)
        merge_spread = round((poly_bid + kalshi_bid) - 1.00, 6)

        buy_poly_sell_kalshi = mint_spread
        buy_kalshi_sell_polymarket = merge_spread

        poly_token_fee_config = (
            left.get("token_fee_config")
            or left.get("fee_config")
            or pair.get("polymarket_fee_config")
            or pair.get("token_fee_config")
        )
        poly_fee_rate = left.get("fee_rate") if left.get("fee_rate") is not None else pair.get("polymarket_fee_rate")
        poly_taker_bps = left.get("taker_bps") if left.get("taker_bps") is not None else pair.get("polymarket_taker_bps")
        poly_fees_enabled = left.get("fees_enabled") if left.get("fees_enabled") is not None else pair.get("polymarket_fees_enabled")
        poly_category = left.get("category") or pair.get("polymarket_category") or pair.get("category")

        if mint_spread > merge_spread:
            best_spread = mint_spread
            opportunity_type = "mint_both_long" if mint_spread > 0 else "none"
            executable_qty = min(poly_ask_qty, kalshi_ask_qty)
            cost_eval = evaluate_net_opportunity_costs(
                version=fee_version,
                buy_venue="polymarket",
                sell_venue="kalshi",
                buy_price=poly_ask,
                sell_price=kalshi_ask,
                quantity=max(1.0, executable_qty),
                is_inverse_long_both=True,
                kalshi_ticker=pair.get("kalshi_ticker"),
                poly_taker_bps=poly_taker_bps,
                poly_fees_enabled=poly_fees_enabled,
                poly_category=poly_category,
                poly_fee_rate=poly_fee_rate,
                poly_token_fee_config=poly_token_fee_config,
            )
            strategy_category = "complementary_payoff_parity"
            inventory_requirement = "Long both outcomes: cash capital only (no short inventory required)"
        else:
            best_spread = merge_spread
            opportunity_type = "merge_both_short" if merge_spread > 0 else "none"
            executable_qty = min(poly_bid_qty, kalshi_bid_qty)
            cost_eval = evaluate_net_opportunity_costs(
                version=fee_version,
                buy_venue="polymarket",
                sell_venue="kalshi",
                buy_price=poly_bid,
                sell_price=kalshi_bid,
                quantity=max(1.0, executable_qty),
                is_inverse_short_both=True,
                kalshi_ticker=pair.get("kalshi_ticker"),
                poly_taker_bps=poly_taker_bps,
                poly_fees_enabled=poly_fees_enabled,
                poly_category=poly_category,
                poly_fee_rate=poly_fee_rate,
                poly_token_fee_config=poly_token_fee_config,
            )
            strategy_category = "complementary_payoff_parity"
            inventory_requirement = "Short both outcomes: requires pre-existing inventory on both venues or 100% cash collateral"

        fee_status = cost_eval.get("fee_status", "known")
        net_spread = cost_eval["net_spread"]
        estimated_net_spread = cost_eval.get("estimated_net_spread")
        best_spread_bps = cost_eval["gross_spread_bps"]
        poly_fee = cost_eval["buy_venue_fee"]
        kalshi_fee = cost_eval["sell_venue_fee"]

        # VWAP calculations for inverse mapping
        for sz in requested_sizes:
            if opportunity_type in ("synthetic_long_both", "mint_both_long"):
                p_ask_v, p_filled = compute_ladder_vwap(poly_asks, sz)
                k_ask_v, k_filled = compute_ladder_vwap(kalshi_asks, sz)
                if p_ask_v is not None and k_ask_v is not None:
                    vwap_spread = round(1.00 - (p_ask_v + k_ask_v), 6)
                    size_vwap_results[str(int(sz))] = {
                        "fillable": True,
                        "poly_ask_vwap": p_ask_v,
                        "kalshi_ask_vwap": k_ask_v,
                        "vwap_spread": vwap_spread,
                    }
                else:
                    size_vwap_results[str(int(sz))] = {"fillable": False, "max_available": min(p_filled, k_filled)}
            elif opportunity_type in ("synthetic_short_both", "merge_both_short"):
                p_bid_v, p_filled = compute_ladder_vwap(poly_bids, sz)
                k_bid_v, k_filled = compute_ladder_vwap(kalshi_bids, sz)
                if p_bid_v is not None and k_bid_v is not None:
                    vwap_spread = round((p_bid_v + k_bid_v) - 1.00, 6)
                    size_vwap_results[str(int(sz))] = {
                        "fillable": True,
                        "poly_bid_vwap": p_bid_v,
                        "kalshi_bid_vwap": k_bid_v,
                        "vwap_spread": vwap_spread,
                    }
                else:
                    size_vwap_results[str(int(sz))] = {"fillable": False, "max_available": min(p_filled, k_filled)}
            else:
                size_vwap_results[str(int(sz))] = {"fillable": False, "vwap_spread": best_spread}

    # --- 5. Opportunity State Machine Classification ---
    # candidate | executable | insufficient_liquidity | stale | invalid_book | unverified_contract | expired | closed
    if is_stale:
        status = "stale"
    elif executable_qty <= 0.0:
        status = "insufficient_liquidity"
    elif fee_status == "unknown" or net_spread is None or estimated_net_spread is None:
        status = "candidate"  # Missing/invalid fee config -> cannot claim executable net spread
    elif estimated_net_spread > 0.0 and net_spread > 0.0 and best_spread > 0.0:
        status = "executable"
    elif best_spread is not None and best_spread > 0.0:
        status = "candidate"  # Gross spread positive, but net spread after fees/slippage <= 0
    else:
        status = "candidate"

    is_arbitrage = bool(status == "executable")

    return {
        "pair_id": f"{pair['polymarket_asset_id']}:{pair['kalshi_ticker']}",
        "polymarket_asset_id": pair["polymarket_asset_id"],
        "kalshi_ticker": pair["kalshi_ticker"],
        "name": canonical_event,
        "canonical_event": canonical_event,
        "canonical_outcome": canonical_outcome,
        "equivalence_mapping": mapping,
        "strategy_category": strategy_category,
        "inventory_requirement": inventory_requirement,
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
        "estimated_net_spread": estimated_net_spread,
        "fee_status": fee_status,
        "fee_version": cost_eval["fee_schedule_version"],
        "poly_fee": poly_fee,
        "kalshi_fee": kalshi_fee,
        "total_net_profit": cost_eval.get("total_net_profit"),
        "total_estimated_net_profit": cost_eval.get("total_estimated_net_profit"),
        "total_slippage_cost": cost_eval.get("total_slippage_cost"),
        "executable_qty": executable_qty,
        "executable_notional": round(executable_qty * (poly_ask if mapping == "direct" else 1.0), 2),
        "opportunity_type": opportunity_type,
        "status": status,
        "is_arbitrage": is_arbitrage,
        "quote_age_seconds": {
            "polymarket": round(quote_age_left, 2),
            "kalshi": round(quote_age_right, 2),
            "venue_delta": round(venue_delta_s, 2),
        },
        "vwap_by_size": size_vwap_results,
        "note": "Theoretical gross quote difference. Net spread accounts for fees and slippage buffer. Subject to execution, legging, and oracle divergence risk."
    }

"""
PolyPulse Phase 6: Versioned Fee & Execution Cost Engine.
Provides official fee schedules, series-specific multipliers, regulatory rounding rules,
dynamic Polymarket category fee rates, and multi-leg net spread calculations for
Polymarket and Kalshi binary prediction markets.
"""
import math
from dataclasses import dataclass
from typing import Dict, Any, Optional

DEFAULT_FEE_VERSION = "v2026.1"

# Series multipliers for Kalshi CFTC-regulated binary option markets
KALSHI_SERIES_MULTIPLIERS = {
    "INTRATE": 0.07,
    "FED": 0.07,
    "CPI": 0.07,
    "REC": 0.07,
    "ECON": 0.07,
    "POL": 0.07,
    "PRES": 0.07,
    "INDEX": 0.035,
    "SPX": 0.035,
    "NASDAQ": 0.035,
    "DEFAULT": 0.07
}

# Category defaults for Polymarket dynamic market-specific fee schedules
POLYMARKET_CATEGORY_FEES = {
    "finance": {"fees_enabled": True, "taker_bps": 20.0, "description": "Finance / Rate / Derivative markets"},
    "economics": {"fees_enabled": True, "taker_bps": 15.0, "description": "Macroeconomic Indicator markets"},
    "politics": {"fees_enabled": True, "taker_bps": 10.0, "description": "Elections / Political nomination markets"},
    "geopolitics": {"fees_enabled": False, "taker_bps": 0.0, "description": "Geopolitical events (fee-free)"},
    "default": {"fees_enabled": False, "taker_bps": 0.0, "description": "Standard binary CLOB baseline (fee-free)"}
}


@dataclass(frozen=True)
class FeeScheduleVersion:
    version: str
    effective_date: str
    source: str
    polymarket_maker_bps: float
    polymarket_taker_bps: float
    polymarket_settlement_fee_pct: float
    kalshi_maker_bps: float
    kalshi_taker_formula: str
    kalshi_max_taker_fee: float
    kalshi_min_fee: float
    kalshi_rounding_rule: str
    default_slippage_bps: float
    notes: str


FEE_SCHEDULES: Dict[str, FeeScheduleVersion] = {
    "v2026.1": FeeScheduleVersion(
        version="v2026.1",
        effective_date="2026-01-01T00:00:00Z",
        source="Kalshi CFTC Rulebook Chapter 3 / Polymarket CTF Exchange V2 Architecture",
        polymarket_maker_bps=0.0,
        polymarket_taker_bps=0.0,
        polymarket_settlement_fee_pct=2.0,
        kalshi_maker_bps=0.0,
        kalshi_taker_formula="min(0.07 * P * (1 - P), 0.02)",
        kalshi_max_taker_fee=0.02,
        kalshi_min_fee=0.0001,
        kalshi_rounding_rule="unrounded",
        default_slippage_bps=10.0,
        notes="Legacy preliminary schedule without contract-level cent rounding."
    ),
    "v2026.2": FeeScheduleVersion(
        version="v2026.2",
        effective_date="2026-11-01T00:00:00Z",
        source="Kalshi Official CFTC Fee Schedule (Appendix B) / Polymarket Dynamic Category Fee Model",
        polymarket_maker_bps=0.0,
        polymarket_taker_bps=0.0,  # Baseline default; dynamic market metadata overrides this
        polymarket_settlement_fee_pct=2.0,  # 2% on net redemption profits
        kalshi_maker_bps=0.0,
        kalshi_taker_formula="ceil(multiplier * contracts * P * (1 - P) * 100) / 100",
        kalshi_max_taker_fee=0.02,  # $0.02 max fee per contract
        kalshi_min_fee=0.01,        # $0.01 minimum fee per executed order
        kalshi_rounding_rule="ceil_whole_cents",  # CFTC official order-level cent ceiling
        default_slippage_bps=10.0,
        notes="Official CFTC series multipliers, contract-quantity integer cent rounding, and dynamic Polymarket market-specific fee schedules."
    )
}


def get_fee_schedule(version: str = DEFAULT_FEE_VERSION) -> FeeScheduleVersion:
    return FEE_SCHEDULES.get(version, FEE_SCHEDULES[DEFAULT_FEE_VERSION])


def get_kalshi_series_multiplier(ticker: Optional[str]) -> float:
    if not ticker:
        return KALSHI_SERIES_MULTIPLIERS["DEFAULT"]
    ticker_upper = ticker.upper()
    for prefix, mult in KALSHI_SERIES_MULTIPLIERS.items():
        if prefix != "DEFAULT" and prefix in ticker_upper:
            return mult
    return KALSHI_SERIES_MULTIPLIERS["DEFAULT"]


def get_polymarket_market_fee_rate(
    market_category: Optional[str] = None,
    fees_enabled: Optional[bool] = None,
    custom_taker_bps: Optional[float] = None,
    version: str = DEFAULT_FEE_VERSION
) -> Dict[str, Any]:
    """
    Determines active Polymarket taker fee rate and whether fees are enabled.
    Supports explicit overrides, market metadata, and category defaults.
    """
    if custom_taker_bps is not None:
        enabled = fees_enabled if fees_enabled is not None else (custom_taker_bps > 0)
        return {
            "fees_enabled": enabled,
            "taker_bps": float(custom_taker_bps) if enabled else 0.0,
            "category": "custom"
        }

    cat_key = (market_category or "default").lower()
    cat_info = POLYMARKET_CATEGORY_FEES.get(cat_key, POLYMARKET_CATEGORY_FEES["default"])

    enabled = fees_enabled if fees_enabled is not None else cat_info["fees_enabled"]
    bps = cat_info["taker_bps"] if enabled else 0.0
    return {
        "fees_enabled": enabled,
        "taker_bps": float(bps),
        "category": cat_key
    }


def calculate_venue_taker_fee(
    venue: str,
    price: float,
    quantity: float,
    version: str = DEFAULT_FEE_VERSION,
    kalshi_ticker: Optional[str] = None,
    poly_taker_bps: Optional[float] = None,
    poly_fees_enabled: Optional[bool] = None,
    poly_category: Optional[str] = None
) -> Dict[str, Any]:
    """
    Calculates exact known taker fee for an order.
    Supports official CFTC cent-rounding rules and series multipliers for Kalshi,
    and market-specific dynamic taker fee rates across Finance, Politics, Economics, and Geopolitics for Polymarket.
    """
    sched = get_fee_schedule(version)
    if price <= 0.0 or price >= 1.0 or quantity <= 0.0:
        return {"per_unit_fee": 0.0, "total_fee": 0.0, "version": sched.version}

    if venue == "polymarket":
        poly_rate_info = get_polymarket_market_fee_rate(
            market_category=poly_category,
            fees_enabled=poly_fees_enabled,
            custom_taker_bps=poly_taker_bps,
            version=version
        )
        if not poly_rate_info["fees_enabled"]:
            return {
                "per_unit_fee": 0.0,
                "total_fee": 0.0,
                "taker_bps": 0.0,
                "fees_enabled": False,
                "category": poly_rate_info.get("category", "default"),
                "version": sched.version
            }

        active_taker_bps = poly_rate_info["taker_bps"]
        per_unit = (active_taker_bps / 10_000.0) * price
        total = round(per_unit * quantity, 4)
        per_unit_rounded = round(total / quantity, 6) if quantity > 0 else 0.0
        return {
            "per_unit_fee": per_unit_rounded,
            "total_fee": total,
            "taker_bps": active_taker_bps,
            "fees_enabled": True,
            "category": poly_rate_info.get("category", "default"),
            "version": sched.version
        }

    elif venue == "kalshi":
        multiplier = get_kalshi_series_multiplier(kalshi_ticker)
        if sched.kalshi_rounding_rule == "ceil_whole_cents":
            # Official CFTC order-level formula:
            # fee_cents = ceil(multiplier * contracts * price * (1 - price) * 100)
            # Capped at max_taker_fee * contracts * 100
            contracts = math.ceil(quantity)
            raw_cents = multiplier * contracts * price * (1.0 - price) * 100.0
            total_cents = math.ceil(round(raw_cents, 6))
            max_cents = math.ceil(round(sched.kalshi_max_taker_fee * 100.0 * contracts, 6))
            final_cents = max(1, min(total_cents, max_cents))  # At least 1 cent for non-zero execution
            total = round(final_cents / 100.0, 4)
            per_unit = round(total / quantity, 6) if quantity > 0 else 0.0
            return {
                "per_unit_fee": per_unit,
                "total_fee": total,
                "multiplier": multiplier,
                "rounding": "ceil_whole_cents",
                "version": sched.version
            }
        else:
            # Legacy unrounded v2026.1 formula
            raw_fee = min(multiplier * price * (1.0 - price), sched.kalshi_max_taker_fee)
            per_unit = round(max(raw_fee, sched.kalshi_min_fee), 6)
            total = round(per_unit * quantity, 4)
            return {
                "per_unit_fee": per_unit,
                "total_fee": total,
                "multiplier": multiplier,
                "rounding": "unrounded",
                "version": sched.version
            }

    return {"per_unit_fee": 0.0, "total_fee": 0.0, "version": sched.version}


def evaluate_net_opportunity_costs(
    buy_venue: str,
    sell_venue: str,
    buy_price: float,
    sell_price: float,
    quantity: float,
    is_inverse_mint: bool = False,
    is_inverse_merge: bool = False,
    is_inverse_long_both: bool = False,
    is_inverse_short_both: bool = False,
    slippage_bps: Optional[float] = None,
    version: str = DEFAULT_FEE_VERSION,
    kalshi_ticker: Optional[str] = None,
    poly_taker_bps: Optional[float] = None,
    poly_fees_enabled: Optional[bool] = None,
    poly_category: Optional[str] = None
) -> Dict[str, Any]:
    """
    Evaluates gross spread vs net spread after known trading fees and estimated execution slippage.
    Accounts for both execution legs, series multipliers, official CFTC rounding rules,
    and market-specific Polymarket fee rates.
    """
    sched = get_fee_schedule(version)
    active_slippage = slippage_bps if slippage_bps is not None else sched.default_slippage_bps

    long_both = is_inverse_long_both or is_inverse_mint
    short_both = is_inverse_short_both or is_inverse_merge

    # 1. Gross Spread per contract
    if long_both:
        # Cross-Venue Synthetic Long Complementary Position (Payoff Parity)
        gross_spread_per_unit = round(1.00 - (buy_price + sell_price), 6)
        entry_notional = buy_price + sell_price
    elif short_both:
        # Cross-Venue Synthetic Short Complementary Position
        gross_spread_per_unit = round((buy_price + sell_price) - 1.00, 6)
        entry_notional = 1.00
    else:
        # Direct Cross-Venue Spread (Buy Venue A / Sell Venue B)
        gross_spread_per_unit = round(sell_price - buy_price, 6)
        entry_notional = buy_price

    total_gross_profit = round(gross_spread_per_unit * quantity, 4)

    # 2. Known Trading Fees for Both Execution Legs
    buy_fee_info = calculate_venue_taker_fee(
        buy_venue, buy_price, quantity, version,
        kalshi_ticker=kalshi_ticker,
        poly_taker_bps=poly_taker_bps,
        poly_fees_enabled=poly_fees_enabled,
        poly_category=poly_category
    )
    sell_fee_info = calculate_venue_taker_fee(
        sell_venue, sell_price, quantity, version,
        kalshi_ticker=kalshi_ticker,
        poly_taker_bps=poly_taker_bps,
        poly_fees_enabled=poly_fees_enabled,
        poly_category=poly_category
    )

    total_known_fees = round(buy_fee_info["total_fee"] + sell_fee_info["total_fee"], 4)
    fee_per_unit = round(total_known_fees / quantity, 6) if quantity > 0 else 0.0

    # 3. Net Spread after Known Trading Fees (Deterministic)
    net_spread_per_unit = round(gross_spread_per_unit - fee_per_unit, 6)
    total_net_profit = round(total_gross_profit - total_known_fees, 4)

    # 4. Estimated Execution Slippage Across BOTH Legs
    # Slippage buffer applies per-leg to each leg's respective notional value:
    # Leg 1 (Buy Leg): buy_price * quantity
    # Leg 2 (Sell Leg): sell_price * quantity
    buy_leg_notional = buy_price * quantity
    sell_leg_notional = sell_price * quantity
    total_execution_notional = round(buy_leg_notional + sell_leg_notional, 4)

    buy_leg_slippage = round(buy_price * (active_slippage / 10_000.0) * quantity, 4)
    sell_leg_slippage = round(sell_price * (active_slippage / 10_000.0) * quantity, 4)
    total_slippage_cost = round(buy_leg_slippage + sell_leg_slippage, 4)
    slippage_cost_per_unit = round(total_slippage_cost / quantity, 6) if quantity > 0 else 0.0

    estimated_net_spread_per_unit = round(net_spread_per_unit - slippage_cost_per_unit, 6)
    total_estimated_net_profit = round(total_net_profit - total_slippage_cost, 4)

    gross_spread_bps = round((gross_spread_per_unit / entry_notional) * 10_000.0, 2) if entry_notional > 0 else 0.0
    net_spread_bps = round((net_spread_per_unit / entry_notional) * 10_000.0, 2) if entry_notional > 0 else 0.0

    return {
        "gross_spread": gross_spread_per_unit,
        "gross_spread_bps": gross_spread_bps,
        "total_gross_profit": total_gross_profit,
        "buy_venue_fee": buy_fee_info["total_fee"],
        "sell_venue_fee": sell_fee_info["total_fee"],
        "total_known_fees": total_known_fees,
        "net_spread": net_spread_per_unit,
        "net_spread_bps": net_spread_bps,
        "total_net_profit": total_net_profit,
        "slippage_bps_per_leg": active_slippage,
        "buy_leg_slippage": buy_leg_slippage,
        "sell_leg_slippage": sell_leg_slippage,
        "total_slippage_cost": total_slippage_cost,
        "slippage_application": "per_leg_across_both_legs",
        "total_execution_notional": total_execution_notional,
        "estimated_net_spread": estimated_net_spread_per_unit,
        "total_estimated_net_profit": total_estimated_net_profit,
        "fee_schedule_version": sched.version,
        "fee_schedule_source": sched.source,
        "rounding_rule": sched.kalshi_rounding_rule,
        "is_payoff_parity_strategy": long_both or short_both,
        "is_guaranteed_profit": False,
        "risk_disclosure": "Subject to execution legging delays, partial fills, capital lockup, and oracle settlement divergence."
    }

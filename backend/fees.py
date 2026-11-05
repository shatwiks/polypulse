"""
PolyPulse Phase 6: Versioned Fee & Execution Cost Engine.
Provides official fee schedules, transaction cost modeling, and net spread calculations
for Polymarket and Kalshi binary prediction markets.
"""
from dataclasses import dataclass
from typing import Dict, Any, Optional

DEFAULT_FEE_VERSION = "v2026.1"


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
    default_slippage_bps: float
    notes: str


FEE_SCHEDULES: Dict[str, FeeScheduleVersion] = {
    "v2026.1": FeeScheduleVersion(
        version="v2026.1",
        effective_date="2026-01-01T00:00:00Z",
        source="Kalshi CFTC Rulebook Appendix B / Polymarket CTF Exchange V2 Architecture",
        polymarket_maker_bps=0.0,
        polymarket_taker_bps=0.0,
        polymarket_settlement_fee_pct=2.0,  # 2% on net redemption profits
        kalshi_maker_bps=0.0,
        kalshi_taker_formula="min(0.07 * P * (1 - P), 0.02)",
        kalshi_max_taker_fee=0.02,
        kalshi_min_fee=0.0001,
        default_slippage_bps=10.0,  # 10 bps default slippage assumption
        notes="Official CFTC-regulated Kalshi binary options schedule and zero-taker-fee Polymarket CLOB."
    )
}


def get_fee_schedule(version: str = DEFAULT_FEE_VERSION) -> FeeScheduleVersion:
    return FEE_SCHEDULES.get(version, FEE_SCHEDULES[DEFAULT_FEE_VERSION])


def calculate_venue_taker_fee(
    venue: str,
    price: float,
    quantity: float,
    version: str = DEFAULT_FEE_VERSION
) -> Dict[str, Any]:
    """
    Calculates exact known taker fee for an order.
    Returns per-contract fee and total fee for quantity.
    """
    sched = get_fee_schedule(version)
    if price <= 0.0 or price >= 1.0 or quantity <= 0.0:
        return {"per_unit_fee": 0.0, "total_fee": 0.0, "version": sched.version}

    if venue == "polymarket":
        per_unit = round((sched.polymarket_taker_bps / 10_000.0) * price, 6)
        total = round(per_unit * quantity, 4)
        return {"per_unit_fee": per_unit, "total_fee": total, "version": sched.version}

    elif venue == "kalshi":
        # min(0.07 * P * (1 - P), 0.02)
        raw_fee = min(0.07 * price * (1.0 - price), sched.kalshi_max_taker_fee)
        per_unit = round(max(raw_fee, sched.kalshi_min_fee), 6)
        total = round(per_unit * quantity, 4)
        return {"per_unit_fee": per_unit, "total_fee": total, "version": sched.version}

    return {"per_unit_fee": 0.0, "total_fee": 0.0, "version": sched.version}


def evaluate_net_opportunity_costs(
    buy_venue: str,
    sell_venue: str,
    buy_price: float,
    sell_price: float,
    quantity: float,
    is_inverse_mint: bool = False,
    is_inverse_merge: bool = False,
    slippage_bps: Optional[float] = None,
    version: str = DEFAULT_FEE_VERSION
) -> Dict[str, Any]:
    """
    Evaluates gross spread vs net spread after known trading fees and optional execution slippage.
    """
    sched = get_fee_schedule(version)
    active_slippage = slippage_bps if slippage_bps is not None else sched.default_slippage_bps

    # 1. Gross Spread per contract
    if is_inverse_mint:
        gross_spread_per_unit = round(1.00 - (buy_price + sell_price), 6)
        entry_notional = buy_price + sell_price
    elif is_inverse_merge:
        gross_spread_per_unit = round((buy_price + sell_price) - 1.00, 6)
        entry_notional = 1.00
    else:
        gross_spread_per_unit = round(sell_price - buy_price, 6)
        entry_notional = buy_price

    total_gross_profit = round(gross_spread_per_unit * quantity, 4)

    # 2. Known Trading Fees
    buy_fee_info = calculate_venue_taker_fee(buy_venue, buy_price, quantity, version)
    sell_fee_info = calculate_venue_taker_fee(sell_venue, sell_price, quantity, version)

    total_known_fees = round(buy_fee_info["total_fee"] + sell_fee_info["total_fee"], 4)
    fee_per_unit = round(buy_fee_info["per_unit_fee"] + sell_fee_info["per_unit_fee"], 6)

    # 3. Net Spread after Known Trading Fees
    net_spread_per_unit = round(gross_spread_per_unit - fee_per_unit, 6)
    total_net_profit = round(total_gross_profit - total_known_fees, 4)

    # 4. Estimated Slippage & Execution Costs
    slippage_cost_per_unit = round(entry_notional * (active_slippage / 10_000.0), 6)
    total_slippage_cost = round(slippage_cost_per_unit * quantity, 4)

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
        "slippage_bps": active_slippage,
        "total_slippage_cost": total_slippage_cost,
        "estimated_net_spread": estimated_net_spread_per_unit,
        "total_estimated_net_profit": total_estimated_net_profit,
        "fee_schedule_version": sched.version,
        "fee_schedule_source": sched.source
    }

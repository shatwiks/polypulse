"""
PolyPulse Phase 6: Canonical Price Normalization.
Provides lossless integer-tick (1,000,000 scale) representation of binary prediction
orderbooks across Polymarket and Kalshi, transforming YES/NO bids and asks,
enforcing microstructure invariants, and preserving venue source timestamps.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Optional, Tuple, Dict, Any

PRICE_SCALE = 1_000_000
QUANTITY_SCALE = 1_000_000


def to_price_ticks(value: Any) -> int:
    """Converts price in range [0.0, 1.0] to integer ticks in [0, 1_000_000]."""
    if value is None:
        raise ValueError("Price cannot be None")
    try:
        dec = Decimal(str(value))
    except (InvalidOperation, TypeError) as exc:
        raise ValueError(f"Invalid price value: {value}") from exc
    ticks = int(dec * PRICE_SCALE)
    if not (0 <= ticks <= PRICE_SCALE):
        raise ValueError(f"Price ticks {ticks} outside binary contract range [0, {PRICE_SCALE}]")
    return ticks


def to_quantity_units(value: Any) -> int:
    """Converts quantity to integer micro-units (scale 1_000_000)."""
    if value is None:
        raise ValueError("Quantity cannot be None")
    try:
        dec = Decimal(str(value))
    except (InvalidOperation, TypeError) as exc:
        raise ValueError(f"Invalid quantity value: {value}") from exc
    units = int(dec * QUANTITY_SCALE)
    if units < 0:
        raise ValueError("Quantity cannot be negative")
    return units


def ticks_to_float(ticks: Optional[int]) -> Optional[float]:
    if ticks is None:
        return None
    return round(ticks / PRICE_SCALE, 6)


def units_to_float(units: Optional[int]) -> Optional[float]:
    if units is None:
        return None
    return round(units / QUANTITY_SCALE, 6)


@dataclass(frozen=True)
class CanonicalBookQuote:
    """Canonical representation of a venue's binary prediction book state."""
    venue: str
    market_id: str
    outcome_id: str
    yes_bid_ticks: Optional[int]
    yes_ask_ticks: Optional[int]
    no_bid_ticks: Optional[int]
    no_ask_ticks: Optional[int]
    yes_bid_units: int
    yes_ask_units: int
    no_bid_units: int
    no_ask_units: int
    market_status: str
    exchange_timestamp: Optional[str]
    received_timestamp: str
    valid: bool
    stale: bool
    rejection_reason: Optional[str] = None

    @property
    def yes_bid_price(self) -> Optional[float]:
        return ticks_to_float(self.yes_bid_ticks)

    @property
    def yes_ask_price(self) -> Optional[float]:
        return ticks_to_float(self.yes_ask_ticks)

    @property
    def no_bid_price(self) -> Optional[float]:
        return ticks_to_float(self.no_bid_ticks)

    @property
    def no_ask_price(self) -> Optional[float]:
        return ticks_to_float(self.no_ask_ticks)

    @property
    def yes_bid_qty(self) -> float:
        return units_to_float(self.yes_bid_units) or 0.0

    @property
    def yes_ask_qty(self) -> float:
        return units_to_float(self.yes_ask_units) or 0.0

    @property
    def no_bid_qty(self) -> float:
        return units_to_float(self.no_bid_units) or 0.0

    @property
    def no_ask_qty(self) -> float:
        return units_to_float(self.no_ask_units) or 0.0

    def is_executable(self) -> bool:
        return bool(
            self.valid
            and not self.stale
            and self.market_status == "active"
            and self.yes_bid_ticks is not None
            and self.yes_ask_ticks is not None
            and self.yes_ask_ticks > self.yes_bid_ticks
            and self.yes_bid_units > 0
            and self.yes_ask_units > 0
        )


def normalize_kalshi_book(
    raw_payload: Dict[str, Any],
    received_at: str,
    max_age_seconds: float = 10.0,
    current_time_dt: Optional[datetime] = None
) -> CanonicalBookQuote:
    """
    Transforms Kalshi YES and NO orderbook levels into canonical representation.
    Kalshi presents binary markets where:
      YES ask = 1.0 - NO bid
      NO ask = 1.0 - YES bid
    """
    now = current_time_dt or datetime.now(timezone.utc)
    market_ticker = str(raw_payload.get("market_ticker", ""))
    exchange_ts = raw_payload.get("exchange_timestamp") or raw_payload.get("ts")
    market_status = raw_payload.get("status", "active")

    # Parse raw top of books if provided
    raw_yes_bid = raw_payload.get("yes_bid")
    raw_yes_ask = raw_payload.get("yes_ask")
    raw_no_bid = raw_payload.get("no_bid")
    raw_no_ask = raw_payload.get("no_ask")

    # If raw levels were provided as arrays
    yes_levels = raw_payload.get("yes_dollars_fp") or raw_payload.get("yes_dollars") or []
    no_levels = raw_payload.get("no_dollars_fp") or raw_payload.get("no_dollars") or []

    yes_bid_ticks = None
    yes_bid_units = 0
    if raw_yes_bid is not None:
        yes_bid_ticks = to_price_ticks(raw_yes_bid)
        yes_bid_units = to_quantity_units(raw_payload.get("yes_bid_size", 0))
    elif yes_levels:
        yes_bid_ticks = to_price_ticks(yes_levels[0][0])
        yes_bid_units = to_quantity_units(yes_levels[0][1])

    no_bid_ticks = None
    no_bid_units = 0
    if raw_no_bid is not None:
        no_bid_ticks = to_price_ticks(raw_no_bid)
        no_bid_units = to_quantity_units(raw_payload.get("no_bid_size", 0))
    elif no_levels:
        no_bid_ticks = to_price_ticks(no_levels[0][0])
        no_bid_units = to_quantity_units(no_levels[0][1])

    # Transform NO bid into synthetic YES ask if direct YES ask not specified:
    # YES ask = 1_000_000 - NO bid
    yes_ask_ticks = None
    yes_ask_units = 0
    if raw_yes_ask is not None:
        yes_ask_ticks = to_price_ticks(raw_yes_ask)
        yes_ask_units = to_quantity_units(raw_payload.get("yes_ask_size", 0))
    elif no_bid_ticks is not None:
        yes_ask_ticks = PRICE_SCALE - no_bid_ticks
        yes_ask_units = no_bid_units

    # Transform YES bid into synthetic NO ask:
    # NO ask = 1_000_000 - YES bid
    no_ask_ticks = None
    no_ask_units = 0
    if raw_no_ask is not None:
        no_ask_ticks = to_price_ticks(raw_no_ask)
        no_ask_units = to_quantity_units(raw_payload.get("no_ask_size", 0))
    elif yes_bid_ticks is not None:
        no_ask_ticks = PRICE_SCALE - yes_bid_ticks
        no_ask_units = yes_bid_units

    # Check staleness
    stale = False
    if exchange_ts:
        try:
            ex_dt = datetime.fromisoformat(str(exchange_ts).replace("Z", "+00:00"))
            if abs((now - ex_dt).total_seconds()) > max_age_seconds:
                stale = True
        except Exception:
            pass

    # Check validity
    valid = True
    rejection_reason = None
    if yes_bid_ticks is not None and yes_ask_ticks is not None:
        if yes_ask_ticks <= yes_bid_ticks:
            valid = False
            rejection_reason = f"Crossed or locked YES quotes: ask={yes_ask_ticks} <= bid={yes_bid_ticks}"
    else:
        valid = False
        rejection_reason = "Missing top of book quote"

    return CanonicalBookQuote(
        venue="kalshi",
        market_id=market_ticker,
        outcome_id="YES",
        yes_bid_ticks=yes_bid_ticks,
        yes_ask_ticks=yes_ask_ticks,
        no_bid_ticks=no_bid_ticks,
        no_ask_ticks=no_ask_ticks,
        yes_bid_units=yes_bid_units,
        yes_ask_units=yes_ask_units,
        no_bid_units=no_bid_units,
        no_ask_units=no_ask_units,
        market_status=market_status,
        exchange_timestamp=str(exchange_ts) if exchange_ts else None,
        received_timestamp=received_at,
        valid=valid,
        stale=stale,
        rejection_reason=rejection_reason,
    )


def normalize_polymarket_book(
    raw_payload: Dict[str, Any],
    received_at: str,
    outcome_token_id: str,
    max_age_seconds: float = 10.0,
    current_time_dt: Optional[datetime] = None
) -> CanonicalBookQuote:
    """
    Normalizes Polymarket top of book for a specific outcome token into canonical integer ticks.
    Computes synthetic NO prices:
      NO bid = 1_000_000 - YES ask
      NO ask = 1_000_000 - YES bid
    """
    now = current_time_dt or datetime.now(timezone.utc)
    market_id = str(raw_payload.get("market_id") or raw_payload.get("asset_id", ""))
    exchange_ts = raw_payload.get("exchange_timestamp") or raw_payload.get("timestamp")
    market_status = raw_payload.get("status", "active")

    best_bid = raw_payload.get("best_bid")
    best_ask = raw_payload.get("best_ask")
    bid_depth = raw_payload.get("bid_depth", 0.0)
    ask_depth = raw_payload.get("ask_depth", 0.0)

    # Convert to ticks
    yes_bid_ticks = to_price_ticks(best_bid) if best_bid is not None else None
    yes_ask_ticks = to_price_ticks(best_ask) if best_ask is not None else None
    yes_bid_units = to_quantity_units(bid_depth) if bid_depth else 0
    yes_ask_units = to_quantity_units(ask_depth) if ask_depth else 0

    no_bid_ticks = (PRICE_SCALE - yes_ask_ticks) if yes_ask_ticks is not None else None
    no_ask_ticks = (PRICE_SCALE - yes_bid_ticks) if yes_bid_ticks is not None else None
    no_bid_units = yes_ask_units
    no_ask_units = yes_bid_units

    stale = False
    if exchange_ts:
        try:
            ex_dt = datetime.fromisoformat(str(exchange_ts).replace("Z", "+00:00"))
            if abs((now - ex_dt).total_seconds()) > max_age_seconds:
                stale = True
        except Exception:
            pass

    valid = True
    rejection_reason = None
    if yes_bid_ticks is not None and yes_ask_ticks is not None:
        if yes_ask_ticks <= yes_bid_ticks:
            valid = False
            rejection_reason = f"Crossed or locked YES quotes: ask={yes_ask_ticks} <= bid={yes_bid_ticks}"
    else:
        valid = False
        rejection_reason = "Missing top of book quote"

    return CanonicalBookQuote(
        venue="polymarket",
        market_id=market_id,
        outcome_id=outcome_token_id,
        yes_bid_ticks=yes_bid_ticks,
        yes_ask_ticks=yes_ask_ticks,
        no_bid_ticks=no_bid_ticks,
        no_ask_ticks=no_ask_ticks,
        yes_bid_units=yes_bid_units,
        yes_ask_units=yes_ask_units,
        no_bid_units=no_bid_units,
        no_ask_units=no_ask_units,
        market_status=market_status,
        exchange_timestamp=str(exchange_ts) if exchange_ts else None,
        received_timestamp=received_at,
        valid=valid,
        stale=stale,
        rejection_reason=rejection_reason,
    )

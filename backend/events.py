"""Validated exchange events. Exchange and receive times are distinct UTC values."""
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import uuid

PRICE_SCALE = 1_000_000
QUANTITY_SCALE = 1_000_000


def utc_timestamp(value):
    if value is None:
        return None
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdecimal()):
        number = int(value)
        return datetime.fromtimestamp(number / (1000 if number > 10_000_000_000 else 1), timezone.utc).isoformat(timespec="milliseconds")
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("invalid exchange timestamp") from exc
        if parsed.tzinfo is None:
            raise ValueError("exchange timestamp lacks timezone")
        return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds")
    raise ValueError("invalid exchange timestamp")


def now_utc():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def scaled(value, scale, *, price=False, allow_zero=False, signed=False):
    try:
        number = Decimal(str(value))
        units = number * scale
    except (InvalidOperation, TypeError) as exc:
        raise ValueError("invalid numeric value") from exc
    if not units.is_finite() or units != units.to_integral_value():
        raise ValueError("numeric precision exceeds six decimals")
    result = int(units)
    if price and not 0 <= result <= scale:
        raise ValueError("price outside binary contract range")
    if not signed and (result < 0 or (result == 0 and not allow_zero)):
        raise ValueError("quantity must be positive")
    return result


def price_ticks(value):
    return scaled(value, PRICE_SCALE, price=True, allow_zero=True)


def quantity_units(value, *, allow_zero=False, signed=False):
    return scaled(value, QUANTITY_SCALE, allow_zero=allow_zero, signed=signed)


@dataclass(frozen=True)
class NormalizedEvent:
    venue: str
    market_id: str
    event_type: str
    received_timestamp: str
    raw_payload: dict
    exchange_timestamp: str | None = None
    sequence_number: int | None = None
    price: str | None = None
    quantity: str | None = None
    side: str | None = None
    source_id: str | None = None
    book_sequence: int | None = None
    bids: tuple[tuple[int, int], ...] = ()
    asks: tuple[tuple[int, int], ...] = ()

    def __post_init__(self):
        if not isinstance(self.raw_payload, dict):
            raise ValueError("raw payload must be an object")
        if self.venue not in ("polymarket", "kalshi", "polygon"):
            raise ValueError("unknown venue")
        if not self.market_id or self.event_type not in ("book_snapshot", "book_level", "trade", "fill"):
            raise ValueError("invalid event identity")
        received = utc_timestamp(self.received_timestamp)
        if received is None:
            raise ValueError("received timestamp required")
        object.__setattr__(self, "received_timestamp", received)
        if self.exchange_timestamp is not None:
            object.__setattr__(self, "exchange_timestamp", utc_timestamp(self.exchange_timestamp))
        if self.sequence_number is not None and self.sequence_number < 0:
            raise ValueError("negative sequence")
        if self.price is not None:
            price_ticks(self.price)
        if self.quantity is not None:
            quantity_units(self.quantity, allow_zero=True)
        if self.side is not None and self.side not in ("bid", "ask", "buy", "sell"):
            raise ValueError("invalid side")
        if self.event_type == "book_level" and (self.price is None or self.quantity is None or self.side not in ("bid", "ask")):
            raise ValueError("incomplete book level")
        if self.event_type in ("trade", "fill") and (self.price is None or self.side not in ("buy", "sell")):
            raise ValueError("incomplete trade")
        if self.event_type == "book_snapshot" and any(p < 0 or p > PRICE_SCALE or q <= 0 for p,q in self.bids + self.asks):
            raise ValueError("invalid snapshot level")

    @property
    def event_id(self):
        # A canonical source payload and source identifier make redelivery stable.
        identity = json.dumps([self.venue, self.market_id, self.event_type,
            self.source_id, self.sequence_number, self.exchange_timestamp, self.raw_payload],
            sort_keys=True, separators=(",", ":"), default=str)
        return str(uuid.UUID(bytes=hashlib.sha256(identity.encode()).digest()[:16]))

    def with_book_sequence(self, sequence):
        return replace(self, book_sequence=sequence)

    def as_row(self):
        return {"event_id": self.event_id, "venue": self.venue, "market_id": self.market_id,
            "event_type": self.event_type, "exchange_timestamp": self.exchange_timestamp,
            "received_timestamp": self.received_timestamp, "sequence_number": self.sequence_number,
            "book_sequence": self.book_sequence, "price": self.price, "quantity": self.quantity,
            "side": self.side, "source_id": self.source_id, "raw_payload": self.raw_payload,
            "bids": self.bids, "asks": self.asks}


def _levels(values):
    if not isinstance(values, list):
        raise ValueError("book levels must be a list")
    result = []
    for item in values:
        if isinstance(item, dict):
            price, quantity = item["price"], item["size"]
        else:
            price, quantity = item
        result.append((price_ticks(price), quantity_units(quantity)))
    return tuple(result)


def parse_polymarket(message, received_timestamp=None):
    """Parse the public raw market-channel schemas independently by event type."""
    received = received_timestamp or now_utc()
    result = []
    for item in message if isinstance(message, list) else [message]:
        if not isinstance(item, dict):
            raise ValueError("Polymarket message must be an object")
        payload = item.get("payload", item)
        if not isinstance(payload, dict):
            raise ValueError("Polymarket payload must be an object")
        kind = item.get("event_type", item.get("type"))
        exchange = utc_timestamp(payload.get("timestamp"))
        if kind == "book":
            asset = str(payload.get("asset_id", payload.get("assetId", "")))
            if not asset:
                raise ValueError("book missing asset_id")
            result.append(NormalizedEvent("polymarket", asset, "book_snapshot", received, item,
                exchange, source_id=payload.get("hash"), bids=_levels(payload["bids"]), asks=_levels(payload["asks"])))
        elif kind == "price_change":
            changes = payload.get("price_changes", payload.get("priceChanges"))
            if not isinstance(changes, list):
                raise ValueError("price_change missing changes")
            for index, change in enumerate(changes):
                asset = str(change.get("asset_id", change.get("assetId", "")))
                side = {"BUY": "bid", "SELL": "ask"}.get(str(change.get("side", "")).upper())
                if not asset or side is None:
                    raise ValueError("price_change missing asset or side")
                # The per-change raw payload keeps duplicate IDs stable in multi-asset frames.
                result.append(NormalizedEvent("polymarket", asset, "book_level", received,
                    {"message": item, "index": index},
                    exchange, price=str(change["price"]), quantity=str(change["size"]), side=side,
                    source_id=change.get("hash")))
        elif kind == "last_trade_price":
            asset = str(payload.get("asset_id", payload.get("assetId", "")))
            side = str(payload.get("side", "")).lower()
            if not asset or side not in ("buy", "sell"):
                raise ValueError("trade missing asset or side")
            result.append(NormalizedEvent("polymarket", asset, "trade", received, item, exchange,
                price=str(payload["price"]), quantity=str(payload["size"]) if payload.get("size") is not None else None, side=side,
                source_id=payload.get("transaction_hash", payload.get("transactionHash"))))
        # Other event types have different schemas and are deliberately ignored.
    return result


def parse_kalshi(message, received_timestamp=None):
    received = received_timestamp or now_utc()
    if not isinstance(message, dict) or not isinstance(message.get("msg"), dict):
        raise ValueError("invalid Kalshi message")
    payload = message["msg"]
    market = str(payload.get("market_ticker", ""))
    if not market:
        raise ValueError("Kalshi market ticker missing")
    seq = int(message["seq"])
    exchange = utc_timestamp(payload.get("ts_ms") or payload.get("ts") or message.get("sending_ts_ms"))
    source_id = f"{message.get('sid')}:{seq}:{payload.get('clock', '')}"
    kind = message.get("type")
    if kind == "orderbook_snapshot":
        yes = payload.get("yes_dollars_fp", payload.get("yes_dollars", []))
        no = payload.get("no_dollars_fp", payload.get("no_dollars", []))
        bids = _levels(yes)
        asks = tuple((PRICE_SCALE - p, q) for p,q in _levels(no))
        return NormalizedEvent("kalshi", market, "book_snapshot", received, message,
            exchange, seq, source_id=source_id, bids=bids, asks=asks)
    if kind == "orderbook_delta":
        raw_side = payload.get("side")
        if raw_side not in ("yes", "no"):
            raise ValueError("invalid Kalshi side")
        raw_price = price_ticks(payload["price_dollars"])
        side = "bid" if raw_side == "yes" else "ask"
        price = raw_price if side == "bid" else PRICE_SCALE - raw_price
        # Delta remains signed until applied against the current level.
        delta = quantity_units(payload["delta_fp"], allow_zero=True, signed=True)
        result = NormalizedEvent("kalshi", market, "book_level", received, message,
            exchange, seq, price=str(Decimal(price) / PRICE_SCALE), quantity="0",
            side=side, source_id=source_id)
        return result, delta
    raise ValueError("unsupported Kalshi event type")

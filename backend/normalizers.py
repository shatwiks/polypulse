"""Strict venue payload parsing into integer-tick book operations."""
from decimal import Decimal, InvalidOperation

SCALE = Decimal(10_000)


def units(value, *, price=False):
    try:
        decimal = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("invalid decimal") from exc
    result = decimal * SCALE
    if not result.is_finite() or result != result.to_integral_value():
        raise ValueError("precision exceeds 4 decimals")
    integer = int(result)
    if price and not 0 <= integer <= 10_000:
        raise ValueError("price outside binary contract range")
    return integer


def poly_messages(message):
    """Yield (asset_id, operation, bids, asks, side, price, size)."""
    for item in message if isinstance(message, list) else [message]:
        if not isinstance(item, dict):
            continue
        payload = item.get("payload", item)
        kind = item.get("event_type", item.get("type"))
        if kind == "book":
            asset = str(payload.get("asset_id", payload.get("assetId", "")))
            if asset:
                bids = [(units(x["price"], price=True), units(x["size"])) for x in payload.get("bids", [])]
                asks = [(units(x["price"], price=True), units(x["size"])) for x in payload.get("asks", [])]
                yield (asset, "snapshot", bids, asks, None, None, None)
        elif kind == "price_change":
            for change in payload.get("price_changes", payload.get("priceChanges", [])):
                asset = str(change.get("asset_id", change.get("assetId", "")))
                if asset:
                    raw_side = change.get("side", "").upper()
                    if raw_side not in ("BUY", "SELL"):
                        raise ValueError("unknown Polymarket book side")
                    side = "bid" if raw_side == "BUY" else "ask"
                    yield (asset, "level", None, None, side,
                           units(change["price"], price=True), units(change["size"]))


def kalshi_snapshot(message):
    payload = message.get("msg", message)
    def levels(name):
        return [(units(p, price=True), units(q)) for p, q in payload.get(name, [])]
    yes = levels("yes_dollars_fp") or levels("yes_dollars")
    no = levels("no_dollars_fp") or levels("no_dollars")
    bids = yes
    asks = [(10_000 - price, quantity) for price, quantity in no]
    return str(payload["market_ticker"]), bids, asks


def kalshi_delta(message, current):
    payload = message["msg"]
    price = units(payload["price_dollars"], price=True)
    delta = units(payload["delta_fp"])
    side = "bid" if payload["side"] == "yes" else "ask"
    normalized_price = price if side == "bid" else 10_000 - price
    quantity = current + delta
    if quantity < 0:
        raise ValueError("negative quantity after Kalshi delta")
    return str(payload["market_ticker"]), side, normalized_price, quantity

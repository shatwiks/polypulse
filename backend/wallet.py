"""Wallet attribution model and address normalization for PolyPulse Phase 5.

Guarantees:
- Public Polymarket CLOB feeds and Kalshi feeds NEVER carry wallet addresses.
- Wallet attribution is strictly derived from Polygon CTF V2 OrderFilled logs
  (maker and taker) or explicitly tagged demo/test injections.
- Zero fabrication: no invented addresses.
"""
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re
from typing import Any, Dict, List, Optional

from backend.events import NormalizedEvent, PRICE_SCALE, QUANTITY_SCALE, price_ticks, quantity_units, now_utc

HEX_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


def normalize_address(address: str) -> str:
    """
    Canonically normalizes an EVM address to lowercase 0x hex format.
    Accepts standard 42-char hex (0x...) or 66-char 32-byte padded topic (0x00...addr).
    Raises ValueError on invalid formats.
    """
    if not isinstance(address, str):
        raise ValueError(f"Address must be string, got {type(address)}")
    raw = address.strip().lower()
    if not raw.startswith("0x"):
        raw = "0x" + raw
    
    # Handle 32-byte EVM topic encoding (66 characters: 0x + 64 hex)
    if len(raw) == 66:
        raw = "0x" + raw[-40:]
    
    if len(raw) != 42 or not all(c in "0123456789abcdef" for c in raw[2:]):
        raise ValueError(f"Invalid Ethereum address: {address}")
    
    return raw


@dataclass(frozen=True)
class WalletTrade:
    trade_id: str
    event_id: str
    wallet_address: str
    venue: str
    market_id: str
    attribution_type: str  # "maker", "taker", "synthetic"
    side: str              # "buy", "sell"
    price_ticks: int
    quantity_units: int
    price: str
    quantity: str
    notional: str
    exchange_timestamp: Optional[str]
    received_timestamp: str
    transaction_hash: Optional[str] = None
    block_number: Optional[int] = None
    log_index: Optional[int] = None
    source: str = "polygon_v2"
    contract_id: Optional[str] = None
    outcome: Optional[str] = None

    def __post_init__(self):
        norm = normalize_address(self.wallet_address)
        object.__setattr__(self, "wallet_address", norm)
        if self.side not in ("buy", "sell"):
            raise ValueError(f"Invalid trade side: {self.side}")
        if self.attribution_type not in ("maker", "taker", "synthetic"):
            raise ValueError(f"Invalid attribution type: {self.attribution_type}")
        if self.price_ticks <= 0 or self.price_ticks > PRICE_SCALE:
            raise ValueError(f"Price ticks out of bounds: {self.price_ticks}")
        if self.quantity_units <= 0:
            raise ValueError(f"Quantity units must be positive: {self.quantity_units}")

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def as_clickhouse_row(self) -> Dict[str, Any]:
        return {
            "trade_id": self.trade_id,
            "event_id": self.event_id,
            "wallet_address": self.wallet_address,
            "venue": self.venue,
            "market_id": self.market_id,
            "attribution_type": self.attribution_type,
            "side": self.side,
            "price": self.price,
            "quantity": self.quantity,
            "notional": self.notional,
            "exchange_timestamp": self.exchange_timestamp,
            "received_timestamp": self.received_timestamp,
            "transaction_hash": self.transaction_hash,
            "block_number": self.block_number,
            "log_index": self.log_index,
            "source": self.source
        }


def extract_wallet_trades(event: NormalizedEvent) -> List[WalletTrade]:
    """
    Extracts verified wallet-attributed trades from a NormalizedEvent.
    - Polygon V2 fills: produces both maker and taker trades.
    - Demo trades: produces synthetic wallet trade if wallet field present.
    - Polymarket public market events & Kalshi events: strictly returns [] (zero fabrication).
    """
    if event.venue == "polygon" and event.event_type == "fill":
        raw = event.raw_payload or {}
        decoded = raw.get("decoded", {})
        log = raw.get("log", {})
        maker_raw = decoded.get("maker")
        taker_raw = decoded.get("taker")
        if not maker_raw or not taker_raw:
            return []

        tx_hash = log.get("transactionHash") or (event.source_id.split(":")[0] if event.source_id else "0x0")
        log_index_str = log.get("logIndex")
        if log_index_str is not None:
            log_idx = int(log_index_str, 16) if isinstance(log_index_str, str) and log_index_str.startswith("0x") else int(log_index_str)
        elif event.source_id and ":" in event.source_id:
            try:
                log_idx = int(event.source_id.split(":")[1])
            except ValueError:
                log_idx = 0
        else:
            log_idx = 0

        block_num_str = log.get("blockNumber")
        block_num = (int(block_num_str, 16) if isinstance(block_num_str, str) and block_num_str.startswith("0x") else int(block_num_str)) if block_num_str else None

        # Price and quantity
        p_str = event.price or "0.500000"
        q_str = event.quantity or "1.000000"
        p_ticks = price_ticks(p_str)
        q_units = quantity_units(q_str)
        notional_val = (Decimal(p_str) * Decimal(q_str)).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
        notional_str = str(notional_val)

        # Maker trade
        maker_side = event.side  # in decode_v2_fill, event.side is maker's side ("buy" if side_code==0 else "sell")
        maker_trade_id = f"polygon:{tx_hash}:{log_idx}:maker"
        maker_trade = WalletTrade(
            trade_id=maker_trade_id,
            event_id=event.event_id,
            wallet_address=maker_raw,
            venue="polygon",
            market_id=event.market_id,
            attribution_type="maker",
            side=maker_side,
            price_ticks=p_ticks,
            quantity_units=q_units,
            price=p_str,
            quantity=q_str,
            notional=notional_str,
            exchange_timestamp=event.exchange_timestamp,
            received_timestamp=event.received_timestamp,
            transaction_hash=tx_hash,
            block_number=block_num,
            log_index=log_idx,
            source="polygon_v2",
            contract_id=event.market_id
        )

        # Taker trade
        taker_side = "sell" if maker_side == "buy" else "buy"
        taker_trade_id = f"polygon:{tx_hash}:{log_idx}:taker"
        taker_trade = WalletTrade(
            trade_id=taker_trade_id,
            event_id=event.event_id,
            wallet_address=taker_raw,
            venue="polygon",
            market_id=event.market_id,
            attribution_type="taker",
            side=taker_side,
            price_ticks=p_ticks,
            quantity_units=q_units,
            price=p_str,
            quantity=q_str,
            notional=notional_str,
            exchange_timestamp=event.exchange_timestamp,
            received_timestamp=event.received_timestamp,
            transaction_hash=tx_hash,
            block_number=block_num,
            log_index=log_idx,
            source="polygon_v2",
            contract_id=event.market_id
        )

        return [maker_trade, taker_trade]

    # For public Polymarket or Kalshi trades, do NOT invent wallet addresses
    return []


def create_demo_wallet_trade(trade_dict: Dict[str, Any]) -> Optional[WalletTrade]:
    """Helper to convert a demo trade payload with a wallet address to a verified WalletTrade."""
    wallet = trade_dict.get("wallet")
    if not wallet:
        return None
    try:
        norm_wallet = normalize_address(wallet)
    except ValueError:
        return None

    trade_id = str(trade_dict.get("trade_id", ""))
    if not trade_id:
        return None

    side = str(trade_dict.get("side", "buy")).lower()
    p_val = Decimal(str(trade_dict.get("price", "0.500000"))).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
    q_val = Decimal(str(trade_dict.get("quantity", "1.000000"))).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
    notional = (p_val * q_val).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)

    ts = trade_dict.get("executed_at")
    if isinstance(ts, (int, float)):
        rec_time = datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="milliseconds")
    else:
        rec_time = now_utc()

    event_id = trade_dict.get("event_id") or trade_id
    if len(event_id) != 36:
        # Format as stable UUID
        import uuid
        import hashlib
        event_id = str(uuid.UUID(bytes=hashlib.sha256(trade_id.encode()).digest()[:16]))

    return WalletTrade(
        trade_id=f"demo:{trade_id}" if not trade_id.startswith("demo:") else trade_id,
        event_id=event_id,
        wallet_address=norm_wallet,
        venue=trade_dict.get("venue", "polymarket"),
        market_id=trade_dict.get("market_id", "demo"),
        attribution_type="synthetic",
        side=side,
        price_ticks=price_ticks(str(p_val)),
        quantity_units=quantity_units(str(q_val)),
        price=str(p_val),
        quantity=str(q_val),
        notional=str(notional),
        exchange_timestamp=rec_time,
        received_timestamp=rec_time,
        source="demo"
    )

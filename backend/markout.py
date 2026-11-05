"""Multi-horizon execution markout and adverse selection calculation engine.

Markout evaluates post-trade market movement relative to execution price:
- Buy: future_mid - exec_price
- Sell: exec_price - future_mid
- Basis points (bps): (markout / exec_price) * 10,000

IMPORTANT:
Markout measures adverse selection and market drift post-execution.
It does NOT represent closed portfolio P&L, inventory carrying cost, or settlement payout.
"""
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Dict, List, Optional, Tuple

from backend.events import now_utc, utc_timestamp

CURRENT_CALC_VERSION = "v1.0"
DEFAULT_HORIZONS = (5, 30, 60)


@dataclass(frozen=True)
class TradeMarkout:
    trade_id: str
    wallet_address: str
    venue: str
    market_id: str
    side: str
    horizon_seconds: int
    calc_version: str
    status: str  # "available", "unavailable", "stale", "crossed"
    exec_price: float
    future_mid: Optional[float]
    markout: Optional[float]
    markout_bps: Optional[float]
    observation_time: Optional[str]
    calculated_at: str

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def as_clickhouse_row(self) -> Dict[str, Any]:
        return {
            "trade_id": self.trade_id,
            "wallet_address": self.wallet_address,
            "venue": self.venue,
            "market_id": self.market_id,
            "side": self.side,
            "horizon_seconds": self.horizon_seconds,
            "calc_version": self.calc_version,
            "status": self.status,
            "exec_price": f"{self.exec_price:.6f}",
            "future_mid": f"{self.future_mid:.6f}" if self.future_mid is not None else None,
            "markout": f"{self.markout:.6f}" if self.markout is not None else None,
            "markout_bps": f"{self.markout_bps:.4f}" if self.markout_bps is not None else None,
            "observation_time": self.observation_time,
            "calculated_at": self.calculated_at
        }


def calculate_markout_metrics(
    exec_price: float,
    side: str,
    future_mid: float
) -> Tuple[float, float]:
    """
    Computes (gross_markout, markout_bps) from execution price, trade side, and future mid.
    - Buy: future_mid - exec_price (positive when price increases after buying)
    - Sell: exec_price - future_mid (positive when price decreases after selling)
    - Basis Points: (markout / exec_price) * 10,000
    """
    if exec_price <= 0:
        raise ValueError("exec_price must be positive")
    
    side_lower = side.lower()
    if side_lower == "buy":
        markout = future_mid - exec_price
    elif side_lower == "sell":
        markout = exec_price - future_mid
    else:
        raise ValueError(f"Invalid side: {side}")
    
    bps = (markout / exec_price) * 10_000.0
    return round(markout, 6), round(bps, 4)


def validate_snapshot_quotes(
    best_bid: Optional[float],
    best_ask: Optional[float],
    valid_flag: int = 1
) -> Tuple[str, Optional[float]]:
    """
    Validates top-of-book quotes and calculates the mid price.
    Returns (status, mid_price).
    - If valid_flag == 0 or bids/asks missing: ("unavailable", None)
    - If non-positive: ("crossed", None)
    - If best_ask <= best_bid: ("crossed", None) (reject crossed or locked books)
    - If valid: ("available", (best_bid + best_ask) / 2.0)
    """
    if valid_flag == 0 or best_bid is None or best_ask is None:
        return "unavailable", None
    
    if best_bid <= 0 or best_ask <= 0:
        return "crossed", None
    
    if best_ask <= best_bid:
        # Rejection of crossed or locked books
        return "crossed", None
    
    mid = (best_bid + best_ask) / 2.0
    return "available", round(mid, 6)


def evaluate_markout(
    trade_id: str,
    wallet_address: str,
    venue: str,
    market_id: str,
    side: str,
    exec_price: float,
    exec_timestamp: float,
    horizon_seconds: int,
    snapshots: List[Dict[str, Any]],
    tolerance_seconds: float = 5.0,
    max_staleness_seconds: float = 10.0,
    calc_version: str = CURRENT_CALC_VERSION
) -> TradeMarkout:
    """
    Evaluates markout for a single trade and horizon given a list of candidate snapshot records.
    Candidate snapshots should contain 'snapshot_time' (timestamp or ISO str), 'best_bid', 'best_ask', 'valid'.
    """
    calc_time = now_utc()
    target_time = exec_timestamp + horizon_seconds
    
    # Filter candidates in [target_time, target_time + tolerance_seconds]
    # Also support [target_time - 1.0, target_time + tolerance_seconds] to catch exact boundary
    candidates = []
    for s in snapshots:
        st = s.get("snapshot_time") or s.get("recorded_at")
        if isinstance(st, str):
            try:
                t = datetime.fromisoformat(st.replace("Z", "+00:00")).timestamp()
            except ValueError:
                continue
        elif isinstance(st, (int, float)):
            t = float(st)
        else:
            continue
        
        diff = t - target_time
        # In window [0, tolerance_seconds] or small negative boundary
        if -0.5 <= diff <= tolerance_seconds:
            candidates.append((abs(diff), t, s))
    
    if not candidates:
        return TradeMarkout(
            trade_id=trade_id,
            wallet_address=wallet_address,
            venue=venue,
            market_id=market_id,
            side=side,
            horizon_seconds=horizon_seconds,
            calc_version=calc_version,
            status="unavailable",
            exec_price=exec_price,
            future_mid=None,
            markout=None,
            markout_bps=None,
            observation_time=None,
            calculated_at=calc_time
        )
    
    # Pick closest snapshot to target_time
    candidates.sort(key=lambda x: x[0])
    delta, obs_ts, best_snap = candidates[0]
    
    obs_iso = datetime.fromtimestamp(obs_ts, timezone.utc).isoformat(timespec="milliseconds")
    
    # Check max staleness
    if delta > max_staleness_seconds:
        return TradeMarkout(
            trade_id=trade_id,
            wallet_address=wallet_address,
            venue=venue,
            market_id=market_id,
            side=side,
            horizon_seconds=horizon_seconds,
            calc_version=calc_version,
            status="stale",
            exec_price=exec_price,
            future_mid=None,
            markout=None,
            markout_bps=None,
            observation_time=obs_iso,
            calculated_at=calc_time
        )
    
    bid = float(best_snap["best_bid"]) if best_snap.get("best_bid") is not None else None
    ask = float(best_snap["best_ask"]) if best_snap.get("best_ask") is not None else None
    valid_flag = int(best_snap.get("valid", 1))
    
    status, mid = validate_snapshot_quotes(bid, ask, valid_flag)
    if status != "available" or mid is None:
        return TradeMarkout(
            trade_id=trade_id,
            wallet_address=wallet_address,
            venue=venue,
            market_id=market_id,
            side=side,
            horizon_seconds=horizon_seconds,
            calc_version=calc_version,
            status=status,
            exec_price=exec_price,
            future_mid=None,
            markout=None,
            markout_bps=None,
            observation_time=obs_iso,
            calculated_at=calc_time
        )
    
    markout, markout_bps = calculate_markout_metrics(exec_price, side, mid)
    return TradeMarkout(
        trade_id=trade_id,
        wallet_address=wallet_address,
        venue=venue,
        market_id=market_id,
        side=side,
        horizon_seconds=horizon_seconds,
        calc_version=calc_version,
        status="available",
        exec_price=exec_price,
        future_mid=mid,
        markout=markout,
        markout_bps=markout_bps,
        observation_time=obs_iso,
        calculated_at=calc_time
    )

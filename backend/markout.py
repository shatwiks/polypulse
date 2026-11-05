"""Multi-horizon execution markout and adverse selection calculation engine.

Markout evaluates post-trade market movement relative to execution price:
- Buy: future_mid - exec_price
- Sell: exec_price - future_mid
- Basis points (bps): (markout / exec_price) * 10,000

Timing & Status Architecture:
- Observation Tolerance: candidate snapshot must lie within [T_target - 0.5s, T_target + 5.0s].
- Quote Freshness: candidate snapshot quote age must not exceed max_quote_age_seconds (10s).
- Quote Invariants: bids/asks must be positive with best_ask > best_bid (crossed/locked rejected).
- Pending vs Finalized: for horizon H, wait until at least T_target + 5.0s + 5.0s grace period
  before finalizing an 'unavailable' observation.
- Stable Execution Partitioning: stores immutable trade_timestamp for monthly partition safety.

IMPORTANT:
Markout measures adverse selection and market drift post-execution.
It does NOT represent closed portfolio P&L, inventory carrying cost, or settlement payout.
"""
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import time
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
    status: str  # "available", "unavailable", "stale", "crossed", "pending"
    exec_price: float
    future_mid: Optional[float]
    markout: Optional[float]
    markout_bps: Optional[float]
    observation_time: Optional[str]
    trade_timestamp: Optional[str]
    calculated_at: str
    token_id: Optional[str] = None
    condition_id: Optional[str] = None

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
            "trade_timestamp": self.trade_timestamp or self.calculated_at,
            "calculated_at": self.calculated_at,
            "token_id": self.token_id,
            "condition_id": self.condition_id
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
    tolerance_pre_seconds: float = 0.5,
    tolerance_post_seconds: float = 5.0,
    max_quote_age_seconds: float = 10.0,
    calc_version: str = CURRENT_CALC_VERSION,
    current_time: Optional[float] = None,
    grace_period_seconds: float = 5.0,
    trade_timestamp_iso: Optional[str] = None,
    token_id: Optional[str] = None,
    condition_id: Optional[str] = None
) -> TradeMarkout:
    """
    Evaluates markout for a single trade and horizon given candidate snapshot records.
    Strictly differentiates:
      1. Observation window tolerance: [T_target - tolerance_pre, T_target + tolerance_post]
      2. Quote freshness: quote_age <= max_quote_age_seconds
      3. Quote invariants: best_ask > best_bid > 0
      4. Pending vs Unavailable: do not finalize unavailable until target + post + grace expires.
    """
    calc_time = now_utc()
    target_time = exec_timestamp + horizon_seconds
    now_ts = current_time if current_time is not None else time.time()
    
    trade_iso = trade_timestamp_iso or datetime.fromtimestamp(exec_timestamp, timezone.utc).isoformat(timespec="milliseconds")

    # Filter candidate snapshots within observation window [target_time - tolerance_pre, target_time + tolerance_post]
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
        if -tolerance_pre_seconds <= diff <= tolerance_post_seconds:
            candidates.append((abs(diff), t, s))
    
    # If no candidate snapshot found in observation window:
    if not candidates:
        # Check if the evaluation window has actually closed
        finalize_threshold = target_time + tolerance_post_seconds + grace_period_seconds
        status = "pending" if now_ts < finalize_threshold else "unavailable"
        
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
            observation_time=None,
            trade_timestamp=trade_iso,
            calculated_at=calc_time,
            token_id=token_id,
            condition_id=condition_id
        )
    
    # Pick closest snapshot to target_time
    candidates.sort(key=lambda x: x[0])
    delta, obs_ts, best_snap = candidates[0]
    obs_iso = datetime.fromtimestamp(obs_ts, timezone.utc).isoformat(timespec="milliseconds")

    # Check quote freshness:
    # 1. explicit staleness flag on snapshot
    # 2. quote age / update delay
    is_feed_stale = bool(best_snap.get("is_stale", False))
    quote_age = float(best_snap.get("quote_age_seconds", delta))
    if is_feed_stale or quote_age > max_quote_age_seconds:
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
            trade_timestamp=trade_iso,
            calculated_at=calc_time,
            token_id=token_id,
            condition_id=condition_id
        )

    # Check quote validity / invariants
    bid = float(best_snap["best_bid"]) if best_snap.get("best_bid") is not None else None
    ask = float(best_snap["best_ask"]) if best_snap.get("best_ask") is not None else None
    valid_flag = int(best_snap.get("valid", 1))

    quote_status, mid = validate_snapshot_quotes(bid, ask, valid_flag)
    if quote_status != "available" or mid is None:
        return TradeMarkout(
            trade_id=trade_id,
            wallet_address=wallet_address,
            venue=venue,
            market_id=market_id,
            side=side,
            horizon_seconds=horizon_seconds,
            calc_version=calc_version,
            status=quote_status,
            exec_price=exec_price,
            future_mid=None,
            markout=None,
            markout_bps=None,
            observation_time=obs_iso,
            trade_timestamp=trade_iso,
            calculated_at=calc_time,
            token_id=token_id,
            condition_id=condition_id
        )

    # Valid observation found: compute markout and bps
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
        trade_timestamp=trade_iso,
        calculated_at=calc_time,
        token_id=token_id,
        condition_id=condition_id
    )

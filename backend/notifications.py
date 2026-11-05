"""
PolyPulse Phase 6: Cross-Venue Opportunity Notification Engine.
Supports:
- Per-pair cooldowns (prevents repeated spam for the same unchanged opportunity)
- Configurable thresholds: min net spread bps, min liquidity, max quote age
- Strict contract verification requirement
- Three distinct notification event classes:
  * candidate_opportunity
  * executable_quote
  * invalidated_opportunity
- Pure analytics/monitoring only: Zero live exchange order execution.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import logging
from typing import Dict, Any, List, Optional, Callable

LOG = logging.getLogger(__name__)


@dataclass
class NotificationConfig:
    min_net_spread_bps: float = 20.0
    min_executable_qty: float = 10.0
    max_quote_age_seconds: float = 10.0
    max_venue_delta_seconds: float = 5.0
    require_verified: bool = True
    cooldown_seconds: float = 60.0
    enabled: bool = True


@dataclass
class NotificationEvent:
    event_id: str
    event_type: str  # candidate_opportunity | executable_quote | invalidated_opportunity
    pair_id: str
    canonical_event: str
    status: str
    net_spread: float
    net_spread_bps: float
    executable_qty: float
    buy_venue: str
    sell_venue: str
    timestamp: str
    details: Dict[str, Any] = field(default_factory=dict)


class NotificationEngine:
    def __init__(self, config: Optional[NotificationConfig] = None):
        self.config = config or NotificationConfig()
        self._last_notified: Dict[str, float] = {}  # pair_id -> monotonic or epoch timestamp
        self._last_opportunity_hash: Dict[str, str] = {}  # pair_id -> content hash
        self._last_status: Dict[str, str] = {}  # pair_id -> last opportunity status
        self._history: List[NotificationEvent] = []
        self._subscribers: List[Callable[[NotificationEvent], None]] = []

    def subscribe(self, callback: Callable[[NotificationEvent], None]) -> None:
        self._subscribers.append(callback)

    def evaluate_spread(self, spread_data: Dict[str, Any], current_time: Optional[float] = None) -> Optional[NotificationEvent]:
        """
        Evaluates a cross-venue spread object against notification thresholds,
        state transitions, and per-pair cooldowns.
        """
        if not self.config.enabled or not spread_data:
            return None

        now = current_time if current_time is not None else datetime.now(timezone.utc).timestamp()
        now_iso = datetime.fromtimestamp(now, timezone.utc).isoformat()
        pair_id = spread_data.get("pair_id", "")
        status = spread_data.get("status", "candidate")
        net_spread = float(spread_data.get("net_spread", 0.0) or 0.0)
        net_spread_bps = float(spread_data.get("best_spread_bps", 0.0) or 0.0)
        executable_qty = float(spread_data.get("executable_qty", 0.0) or 0.0)
        canonical_event = spread_data.get("canonical_event", "")
        prev_status = self._last_status.get(pair_id)

        # 1. Check for Invalidation Transition
        if prev_status == "executable" and status != "executable":
            self._last_status[pair_id] = status
            event = NotificationEvent(
                event_id=f"notif_inv_{pair_id}_{int(now)}",
                event_type="invalidated_opportunity",
                pair_id=pair_id,
                canonical_event=canonical_event,
                status=status,
                net_spread=net_spread,
                net_spread_bps=net_spread_bps,
                executable_qty=executable_qty,
                buy_venue=spread_data.get("opportunity_type", ""),
                sell_venue="",
                timestamp=now_iso,
                details={"reason": f"Transitioned from executable to {status}", "spread": spread_data}
            )
            self._record_and_dispatch(event)
            return event

        self._last_status[pair_id] = status

        # 2. Contract Verification Guardrail
        if self.config.require_verified and spread_data.get("polymarket", {}).get("valid") != 1:
            return None

        # 3. Quote Freshness Threshold
        quote_age = spread_data.get("quote_age_seconds", {})
        poly_age = float(quote_age.get("polymarket", 0.0))
        kalshi_age = float(quote_age.get("kalshi", 0.0))
        delta_s = float(quote_age.get("venue_delta", 0.0))
        if poly_age > self.config.max_quote_age_seconds or kalshi_age > self.config.max_quote_age_seconds or delta_s > self.config.max_venue_delta_seconds:
            return None

        # 4. Determine Notification Class
        if status == "executable" and net_spread > 0.0:
            if net_spread_bps < self.config.min_net_spread_bps or executable_qty < self.config.min_executable_qty:
                return None
            event_type = "executable_quote"
        elif spread_data.get("best_spread", 0.0) > 0.0:
            event_type = "candidate_opportunity"
        else:
            return None

        # 5. Check Content Fingerprint (Avoid duplicate notification if quotes are identical)
        content_key = f"{pair_id}:{status}:{round(net_spread, 4)}:{round(executable_qty, 2)}"
        content_hash = hashlib.sha256(content_key.encode("utf-8")).hexdigest()
        last_hash = self._last_opportunity_hash.get(pair_id)

        # 6. Check Per-Pair Cooldown
        last_time = self._last_notified.get(pair_id, 0.0)
        time_elapsed = now - last_time

        if content_hash == last_hash and time_elapsed < self.config.cooldown_seconds:
            # Drop identical notification within cooldown period
            return None

        # Update cache
        self._last_notified[pair_id] = now
        self._last_opportunity_hash[pair_id] = content_hash

        opp_type = spread_data.get("opportunity_type", "")
        buy_v = "polymarket" if "poly" in opp_type and "short_poly" not in opp_type else "kalshi"
        sell_v = "kalshi" if buy_v == "polymarket" else "polymarket"

        event = NotificationEvent(
            event_id=f"notif_{event_type}_{pair_id}_{int(now)}",
            event_type=event_type,
            pair_id=pair_id,
            canonical_event=canonical_event,
            status=status,
            net_spread=net_spread,
            net_spread_bps=net_spread_bps,
            executable_qty=executable_qty,
            buy_venue=buy_v,
            sell_venue=sell_v,
            timestamp=now_iso,
            details=spread_data
        )
        self._record_and_dispatch(event)
        return event

    def _record_and_dispatch(self, event: NotificationEvent) -> None:
        self._history.append(event)
        if len(self._history) > 1000:
            self._history.pop(0)
        for sub in self._subscribers:
            try:
                sub(event)
            except Exception as exc:
                LOG.warning("Notification subscriber error: %s", exc)

    def get_history(self, pair_id: Optional[str] = None, event_type: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        results = []
        for e in reversed(self._history):
            if pair_id and e.pair_id != pair_id:
                continue
            if event_type and e.event_type != event_type:
                continue
            results.append({
                "event_id": e.event_id,
                "event_type": e.event_type,
                "pair_id": e.pair_id,
                "canonical_event": e.canonical_event,
                "status": e.status,
                "net_spread": e.net_spread,
                "net_spread_bps": e.net_spread_bps,
                "executable_qty": e.executable_qty,
                "buy_venue": e.buy_venue,
                "sell_venue": e.sell_venue,
                "timestamp": e.timestamp,
                "details": e.details
            })
            if len(results) >= limit:
                break
        return results

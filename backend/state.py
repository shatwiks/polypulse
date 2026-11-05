import asyncio
import hashlib
import json
import logging
import time
from collections import deque
from dataclasses import replace
from datetime import datetime, timezone

from backend.engine import Book, PRICE_SCALE, QUANTITY_SCALE
from backend.events import price_ticks, quantity_units

LOG = logging.getLogger(__name__)


class MarketState:
    def __init__(self, journal, pairs=None, mode="demo"):
        self.journal = journal
        self.books = {}
        self.listeners = set()
        self.pairs = pairs or []
        self.feed_status = {"polymarket": "unconfigured", "kalshi": "unconfigured"}
        self.counters = {"events": 0, "gaps": 0, "recoveries": 0, "duplicates": 0}
        self.book_status = {}
        self.feed_details = {}
        self.received_times = deque()
        self.processed_times = deque()
        self.processing_ms = deque(maxlen=1000)
        self.started_at = time.time()
        for row in journal.latest_snapshots():
            if mode == "live" and row["market_id"].startswith("demo-"):
                continue
            view = json.loads(row["payload"])
            book = Book()
            book.snapshot([(round(p*PRICE_SCALE), round(q*QUANTITY_SCALE)) for p,q in view["bids"]],
                          [(round(p*PRICE_SCALE), round(q*QUANTITY_SCALE)) for p,q in view["asks"]], row["sequence"])
            self.books[(row["venue"], row["market_id"])] = book
            self.book_status[(row["venue"], row["market_id"])] = {
                "valid": mode == "demo", "recovery_status": "ready" if mode == "demo" else "awaiting_snapshot",
                "last_received_timestamp": None, "last_exchange_timestamp": None}

    def reset_venue(self, venue):
        for key in list(self.books):
            if key[0] == venue:
                self.books.pop(key).close()
                self.book_status.pop(key, None)

    def invalidate_venue(self, venue, reason="disconnected"):
        for key in list(self.books):
            if key[0] == venue:
                self.book_status.setdefault(key, {}).update({"valid": False, "recovery_status": reason})
                self._publish_book(*key)

    def set_feed_status(self, venue, status, detail=None):
        if self.feed_status.get(venue) != status:
            self.feed_status[venue] = status
            if hasattr(self.journal, "log_health"):
                self.journal.log_health(venue, status, detail)
        self.feed_details[venue] = {"status": status, "detail": detail,
            "last_received_timestamp": self.feed_details.get(venue, {}).get("last_received_timestamp")}

    def mark_received(self, venue, when):
        self.received_times.append(time.monotonic())
        self.feed_details.setdefault(venue, {})["last_received_timestamp"] = when

    def touch_book(self, venue, market_id, received_timestamp=None):
        """Update last_received_timestamp when reconciliation confirms the local book matches exchange state."""
        key = (venue, market_id)
        if key in self.book_status:
            ts = received_timestamp or datetime.now(timezone.utc).isoformat(timespec="milliseconds")
            self.book_status[key]["last_received_timestamp"] = ts
            self.book_status[key]["valid"] = True
            self.book_status[key]["recovery_status"] = "ready"
            self.mark_received(venue, ts)
            self._publish_book(*key)

    def rates(self):
        cutoff = time.monotonic() - 10
        for values in (self.received_times, self.processed_times):
            while values and values[0] < cutoff:
                values.popleft()
        return {"events_received_per_second": round(len(self.received_times) / 10, 2),
                "events_processed_per_second": round(len(self.processed_times) / 10, 2),
                "orderbook_processing_latency_ms_p95": sorted(self.processing_ms)[int(.95 * (len(self.processing_ms)-1))] if self.processing_ms else None}

    def _publish_book(self, venue, market_id):
        message = {"type": "book", "data": self.get(venue, market_id)}
        for queue in list(self.listeners):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(message)

    def get(self, venue, market_id):
        book = self.books.get((venue, market_id))
        if not book:
            return None
        metadata = self.book_status.get((venue, market_id), {"valid": True, "recovery_status": "ready"})
        last_received = metadata.get("last_received_timestamp")
        stale = bool(last_received and time.time() - datetime.fromisoformat(last_received).timestamp() > 30)
        view = book.view()
        if stale or not metadata.get("valid", False):
            view.update({"bids": [], "asks": [], "best_bid": None, "best_ask": None,
                         "buy_vwap_10": None, "imbalance": 0})
        return {"venue": venue, "market_id": market_id, **view, **metadata,
                "valid": bool(metadata.get("valid", False)) and not stale,
                "stale": stale, "recovery_status": "stale" if stale else metadata.get("recovery_status", "ready")}

    def markets(self):
        return [self.get(*key) for key in sorted(self.books)]

    def spreads(self):
        result = []
        for pair in self.journal.contract_pairs(verified_only=True):
            if datetime.fromisoformat(pair["expiration"]) <= datetime.now(timezone.utc):
                continue
            left = self.get("polymarket", pair["polymarket_asset_id"])
            right = self.get("kalshi", pair["kalshi_ticker"])
            if not left or not right or not left["valid"] or not right["valid"]:
                continue
            buy_poly = right["best_bid"] - left["best_ask"] if right["best_bid"] is not None and left["best_ask"] is not None else None
            buy_kalshi = left["best_bid"] - right["best_ask"] if left["best_bid"] is not None and right["best_ask"] is not None else None
            result.append({"name": pair["event_description"], "polymarket": left, "kalshi": right,
                           "buy_polymarket_sell_kalshi": buy_poly,
                           "buy_kalshi_sell_polymarket": buy_kalshi,
                           "note": "Gross quote difference before fees, depth and settlement risk"})
        return result

    def _record(self, venue, market_id, kind, sequence, raw, book):
        now = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        self.mark_received(venue, now)
        raw_json = json.dumps(raw, sort_keys=True, separators=(",", ":"))
        identity = hashlib.sha256(f"{venue}|{market_id}|{kind}|{sequence}|{raw_json}".encode()).hexdigest()
        event = {"event_id": f"{identity[:8]}-{identity[8:12]}-{identity[12:16]}-{identity[16:20]}-{identity[20:32]}",
                 "venue": venue, "market_id": market_id, "event_type": kind,
                 "event_time": now, "received_at": now, "sequence": sequence, "payload": raw}
        inserted = self.journal.event(event)
        if not inserted:
            self.counters["duplicates"] += 1
        else:
            self.counters["events"] += 1
            self.processed_times.append(time.monotonic())
        view = book.view(depth=None)
        self.journal.snapshot(venue, market_id, sequence, view)
        message = {"type": "book", "data": {"venue": venue, "market_id": market_id, **book.view()}}
        for queue in list(self.listeners):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(message)

    def snapshot(self, venue, market_id, bids, asks, sequence, raw=None):
        key = (venue, market_id)
        book = self.books.get(key)
        if book is None:
            book = Book()
            self.books[key] = book
        elif sequence < book.view(depth=0)["sequence"]:
            raise ValueError("stale snapshot")
        book.snapshot(bids, asks, sequence)
        self._record(venue, market_id, "snapshot", sequence, raw or {"bids": bids, "asks": asks}, book)

    def level(self, venue, market_id, side, price, quantity, sequence, raw=None):
        book = self.books.get((venue, market_id))
        if not book:
            raise ValueError("update before snapshot")
        book.set_level(side, price, quantity, sequence)
        self._record(venue, market_id, "level", sequence,
                     raw or {"side": side, "price": price, "quantity": quantity}, book)

    def listen(self):
        queue = asyncio.Queue(maxsize=100)
        self.listeners.add(queue)
        return queue

    def unlisten(self, queue):
        self.listeners.discard(queue)

    def process_live_event(self, event, *, replay=False):
        """Journal first, then apply, then atomically record observation and completion."""
        key = (event.venue, event.market_id)
        book = self.books.get(key)
        if event.event_type == "book_level" and (book is None or (not replay and not self.book_status.get(key, {}).get("valid"))):
            raise ValueError("incremental update without a valid snapshot")
        sequence = event.book_sequence
        if sequence is None and event.event_type in ("book_snapshot", "book_level"):
            sequence = (book.view(depth=0)["sequence"] if book else 0) + 1
            event = replace(event, book_sequence=sequence)
        if not replay and not self.journal.append(event):
            self.counters["duplicates"] += 1
            if not self.journal.already_applied(event.event_id):
                return self.process_live_event(event, replay=True)
            if event.event_type == "book_snapshot":
                # An unchanged authoritative snapshot after reconnect is still
                # needed to establish a valid in-memory book for this session.
                if book is None:
                    book = Book()
                    self.books[key] = book
                book.snapshot(event.bids, event.asks, sequence)
                self.book_status[key] = {"valid": True, "recovery_status": "ready",
                    "last_received_timestamp": event.received_timestamp,
                    "last_exchange_timestamp": event.exchange_timestamp}
                self._publish_book(*key)
            return False
        if replay and self.journal.already_applied(event.event_id):
            return False
        started = time.perf_counter()
        try:
            view = None
            if event.event_type == "book_snapshot":
                if book is None:
                    book = Book()
                    self.books[key] = book
                if sequence > book.view(depth=0)["sequence"]:
                    book.snapshot(event.bids, event.asks, sequence)
                self.book_status[key] = {"valid": not replay, "recovery_status": "awaiting_snapshot" if replay else "ready",
                    "last_received_timestamp": event.received_timestamp,
                    "last_exchange_timestamp": event.exchange_timestamp}
                view = book.view(depth=None)
                view["valid"] = not replay
            elif event.event_type == "book_level":
                if sequence > book.view(depth=0)["sequence"]:
                    book.set_level(event.side, price_ticks(event.price),
                                   quantity_units(event.quantity, allow_zero=True), sequence)
                self.book_status[key].update({"last_received_timestamp": event.received_timestamp,
                    "last_exchange_timestamp": event.exchange_timestamp})
                view = book.view(depth=None)
                view["valid"] = self.book_status[key].get("valid", False)
            self.journal.finish(event, view)
        except Exception as exc:
            self.journal.log_error(event.venue, event.market_id, event.event_id, exc)
            LOG.error("normalized event rejected", extra={"venue": event.venue,
                "market_id": event.market_id, "event_id": event.event_id}, exc_info=True)
            self.book_status.setdefault(key, {}).update({"valid": False, "recovery_status": "error"})
            self.journal.finish(event)
            self._publish_book(*key)
            raise
        self.counters["events"] += 1
        self.processed_times.append(time.monotonic())
        LOG.info("normalized event processed", extra={"venue": event.venue,
            "market_id": event.market_id, "event_id": event.event_id})
        if view is not None:
            self.processing_ms.append(round((time.perf_counter() - started) * 1000, 3))
            self._publish_book(*key)
        return True

    def replay_unprocessed(self):
        if not hasattr(self.journal, "unprocessed"):
            return 0
        count = 0
        for event in self.journal.unprocessed():
            try:
                if self.process_live_event(event, replay=True):
                    count += 1
            except Exception:
                continue
        return count

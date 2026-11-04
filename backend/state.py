import asyncio
import hashlib
import json
import time
from datetime import datetime, timezone

from backend.engine import Book


class MarketState:
    def __init__(self, journal, pairs=None):
        self.journal = journal
        self.books = {}
        self.listeners = set()
        self.pairs = pairs or []
        self.feed_status = {"polymarket": "unconfigured", "kalshi": "unconfigured"}
        self.counters = {"events": 0, "gaps": 0, "recoveries": 0, "duplicates": 0}
        self.started_at = time.time()
        for row in journal.latest_snapshots():
            view = json.loads(row["payload"])
            book = Book()
            book.snapshot([(round(p*10000), round(q*10000)) for p,q in view["bids"]],
                          [(round(p*10000), round(q*10000)) for p,q in view["asks"]], row["sequence"])
            self.books[(row["venue"], row["market_id"])] = book

    def reset_venue(self, venue):
        for key in list(self.books):
            if key[0] == venue:
                self.books.pop(key).close()

    def get(self, venue, market_id):
        book = self.books.get((venue, market_id))
        if not book:
            return None
        return {"venue": venue, "market_id": market_id, **book.view()}

    def markets(self):
        return [self.get(*key) for key in sorted(self.books)]

    def spreads(self):
        result = []
        for pair in self.pairs:
            left = self.get("polymarket", pair["polymarket"])
            right = self.get("kalshi", pair["kalshi"])
            if not left or not right:
                continue
            buy_poly = right["best_bid"] - left["best_ask"] if right["best_bid"] is not None and left["best_ask"] is not None else None
            buy_kalshi = left["best_bid"] - right["best_ask"] if left["best_bid"] is not None and right["best_ask"] is not None else None
            result.append({"name": pair["name"], "polymarket": left, "kalshi": right,
                           "buy_polymarket_sell_kalshi": buy_poly,
                           "buy_kalshi_sell_polymarket": buy_kalshi,
                           "note": "Gross quote difference before fees, depth and settlement risk"})
        return result

    def _record(self, venue, market_id, kind, sequence, raw, book):
        now = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
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
        view = book.view(depth=1000)
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

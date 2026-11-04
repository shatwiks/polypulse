"""Durable SQLite journal and best-effort ClickHouse batch exporter."""
import asyncio
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx


class Journal:
    def __init__(self, path):
        self.db = sqlite3.connect(Path(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS events(
          event_id TEXT PRIMARY KEY, venue TEXT NOT NULL, market_id TEXT NOT NULL,
          event_type TEXT NOT NULL, event_time TEXT NOT NULL, received_at TEXT NOT NULL,
          sequence INTEGER NOT NULL, payload TEXT NOT NULL, exported INTEGER NOT NULL DEFAULT 0);
        CREATE INDEX IF NOT EXISTS events_market ON events(venue, market_id, received_at);
        CREATE TABLE IF NOT EXISTS snapshots(
          venue TEXT NOT NULL, market_id TEXT NOT NULL, recorded_at REAL NOT NULL,
          sequence INTEGER NOT NULL, payload TEXT NOT NULL,
          PRIMARY KEY(venue, market_id, sequence));
        CREATE TABLE IF NOT EXISTS book_observations(
          id INTEGER PRIMARY KEY AUTOINCREMENT, venue TEXT NOT NULL, market_id TEXT NOT NULL,
          recorded_at REAL NOT NULL, sequence INTEGER NOT NULL, payload TEXT NOT NULL,
          exported INTEGER NOT NULL DEFAULT 0);
        CREATE INDEX IF NOT EXISTS book_observations_market ON book_observations(venue, market_id, recorded_at);
        CREATE TABLE IF NOT EXISTS trades(
          trade_id TEXT PRIMARY KEY, wallet TEXT NOT NULL, venue TEXT NOT NULL,
          market_id TEXT NOT NULL, side TEXT NOT NULL, price REAL NOT NULL,
          quantity REAL NOT NULL, executed_at REAL NOT NULL);
        """)
        self.db.execute("""INSERT INTO book_observations(venue, market_id, recorded_at, sequence, payload)
            SELECT s.venue, s.market_id, s.recorded_at, s.sequence, s.payload FROM snapshots s
            WHERE NOT EXISTS(SELECT 1 FROM book_observations b WHERE b.venue=s.venue AND b.market_id=s.market_id AND b.recorded_at=s.recorded_at AND b.sequence=s.sequence)""")
        self.db.commit()

    def event(self, event):
        cur = self.db.execute("INSERT OR IGNORE INTO events VALUES(?,?,?,?,?,?,?,?,0)",
            (event["event_id"], event["venue"], event["market_id"], event["event_type"],
             event["event_time"], event["received_at"], event["sequence"],
             json.dumps(event["payload"], separators=(",", ":"))))
        self.db.commit()
        return cur.rowcount == 1

    def snapshot(self, venue, market_id, sequence, view):
        self.db.execute("INSERT INTO book_observations(venue, market_id, recorded_at, sequence, payload) VALUES(?,?,?,?,?)",
            (venue, market_id, time.time(), sequence, json.dumps(view)))
        self.db.commit()

    def latest_snapshots(self):
        rows = self.db.execute("""SELECT s.* FROM book_observations s JOIN
            (SELECT venue, market_id, MAX(id) id FROM book_observations GROUP BY venue, market_id) x
            ON s.id=x.id""").fetchall()
        return [dict(row) for row in rows]

    def pending(self, limit=250):
        return [dict(r) for r in self.db.execute("SELECT * FROM events WHERE exported=0 ORDER BY received_at LIMIT ?", (limit,))]

    def pending_observations(self, limit=250):
        return [dict(r) for r in self.db.execute("SELECT * FROM book_observations WHERE exported=0 ORDER BY id LIMIT ?", (limit,))]

    def mark_exported(self, ids):
        self.db.executemany("UPDATE events SET exported=1 WHERE event_id=?", [(x,) for x in ids])
        self.db.commit()

    def mark_observations_exported(self, ids):
        self.db.executemany("UPDATE book_observations SET exported=1 WHERE id=?", [(x,) for x in ids])
        self.db.commit()

    def add_trade(self, trade):
        cur = self.db.execute("INSERT OR IGNORE INTO trades VALUES(?,?,?,?,?,?,?,?)",
            (trade["trade_id"], trade["wallet"], trade["venue"], trade["market_id"],
             trade["side"], trade["price"], trade["quantity"], trade["executed_at"]))
        self.db.commit()
        return cur.rowcount == 1

    def trades(self, wallet=None):
        if wallet:
            return [dict(r) for r in self.db.execute("SELECT * FROM trades WHERE wallet=? ORDER BY executed_at DESC", (wallet,))]
        return [dict(r) for r in self.db.execute("SELECT * FROM trades ORDER BY executed_at DESC LIMIT 100")]

    def wallets(self):
        return [dict(r) for r in self.db.execute("SELECT wallet, COUNT(*) trade_count FROM trades GROUP BY wallet ORDER BY trade_count DESC")]

    def future_mid(self, venue, market_id, target):
        row = self.db.execute("SELECT payload FROM book_observations WHERE venue=? AND market_id=? AND recorded_at>=? AND recorded_at<=? ORDER BY recorded_at LIMIT 1", (venue, market_id, target, target + 5)).fetchone()
        if not row:
            return None
        view = json.loads(row[0])
        return (view["best_bid"] + view["best_ask"]) / 2 if view.get("best_bid") is not None and view.get("best_ask") is not None else None

    def stats(self):
        return {"events": self.db.execute("SELECT COUNT(*) FROM events").fetchone()[0],
                "pending_export": self.db.execute("SELECT (SELECT COUNT(*) FROM events WHERE exported=0) + (SELECT COUNT(*) FROM book_observations WHERE exported=0)").fetchone()[0],
                "snapshots": self.db.execute("SELECT COUNT(*) FROM book_observations").fetchone()[0]}


async def export_forever(journal, url, status):
    async with httpx.AsyncClient(timeout=10) as client:
        while True:
            batch = journal.pending()
            observations = journal.pending_observations()
            if not batch and not observations:
                await asyncio.sleep(1)
                continue
            try:
                if batch:
                    lines = [json.dumps({k: row[k] for k in ("event_id", "venue", "market_id", "event_type", "event_time", "received_at", "sequence", "payload")}) for row in batch]
                    response = await client.post(url, params={"query": "INSERT INTO polypulse.market_events FORMAT JSONEachRow", "date_time_input_format": "best_effort"}, content="\n".join(lines) + "\n")
                    response.raise_for_status()
                    journal.mark_exported([r["event_id"] for r in batch])
                if observations:
                    lines = []
                    for row in observations:
                        view = json.loads(row["payload"])
                        bids, asks = view["bids"], view["asks"]
                        lines.append(json.dumps({"venue": row["venue"], "market_id": row["market_id"],
                            "snapshot_time": datetime.fromtimestamp(row["recorded_at"], timezone.utc).isoformat(timespec="milliseconds"), "best_bid": view["best_bid"],
                            "best_ask": view["best_ask"],
                            "bid_depth": sum(q for _,q in bids), "ask_depth": sum(q for _,q in asks),
                            "sequence": row["sequence"]}))
                    response = await client.post(url, params={"query": "INSERT INTO polypulse.orderbook_snapshots FORMAT JSONEachRow", "date_time_input_format": "best_effort"}, content="\n".join(lines) + "\n")
                    response.raise_for_status()
                    journal.mark_observations_exported([r["id"] for r in observations])
                status["clickhouse"] = "connected"
            except (httpx.HTTPError, OSError):
                status["clickhouse"] = "retrying"
                await asyncio.sleep(5)

"""SQLite write-ahead journal and at-least-once ClickHouse exporter."""
import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import logging
import time

import httpx

from backend.events import NormalizedEvent, now_utc
from backend.storage import Journal
from backend.wallet import (
    WalletTrade, extract_wallet_trades, create_demo_wallet_trade, normalize_address
)
from backend.markout import (
    TradeMarkout, CURRENT_CALC_VERSION, DEFAULT_HORIZONS,
    calculate_markout_metrics, validate_snapshot_quotes
)

LOG = logging.getLogger(__name__)


class DurableJournal(Journal):
    def __init__(self, path):
        super().__init__(path)
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(events)")}
        additions = {
            "exchange_timestamp": "TEXT", "source_sequence": "INTEGER",
            "book_sequence": "INTEGER", "price": "TEXT", "quantity": "TEXT",
            "side": "TEXT", "source_id": "TEXT", "normalized_json": "TEXT",
            "applied": "INTEGER NOT NULL DEFAULT 1"}
        for column, column_type in additions.items():
            if column not in columns:
                self.db.execute(f"ALTER TABLE events ADD COLUMN {column} {column_type}")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS public_trades(
          event_id TEXT PRIMARY KEY, venue TEXT NOT NULL, market_id TEXT NOT NULL,
          source_id TEXT, side TEXT NOT NULL, price TEXT NOT NULL, quantity TEXT,
          exchange_timestamp TEXT, received_timestamp TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS feed_health(
          id INTEGER PRIMARY KEY AUTOINCREMENT, venue TEXT NOT NULL, status TEXT NOT NULL,
          observed_at TEXT NOT NULL, detail TEXT, exported INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS ingestion_errors(
          id INTEGER PRIMARY KEY AUTOINCREMENT, venue TEXT NOT NULL, market_id TEXT,
          event_id TEXT, error TEXT NOT NULL, observed_at TEXT NOT NULL,
          exported INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS checkpoints(
          source TEXT PRIMARY KEY, value INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS contract_pairs(
          polymarket_asset_id TEXT NOT NULL, kalshi_ticker TEXT NOT NULL,
          event_description TEXT NOT NULL, outcome_equivalence TEXT NOT NULL,
          resolution_source TEXT NOT NULL, expiration TEXT NOT NULL,
          verification_status TEXT NOT NULL CHECK(verification_status IN ('unverified','verified','rejected')),
          updated_at TEXT NOT NULL,
          PRIMARY KEY(polymarket_asset_id, kalshi_ticker));
        CREATE TABLE IF NOT EXISTS export_batches (
          batch_id TEXT PRIMARY KEY,
          target_table TEXT NOT NULL,
          source_table TEXT NOT NULL,
          item_count INTEGER NOT NULL,
          event_ids TEXT NOT NULL,
          payload TEXT NOT NULL,
          dedup_token TEXT NOT NULL,
          created_at TEXT NOT NULL,
          attempts INTEGER NOT NULL DEFAULT 0,
          status TEXT NOT NULL DEFAULT 'pending',
          last_attempt_at TEXT,
          last_error TEXT,
          acknowledged_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_export_batches_status ON export_batches(status, target_table, created_at);
        CREATE TABLE IF NOT EXISTS export_batch_items (
          target_table TEXT NOT NULL,
          source_table TEXT NOT NULL,
          item_id TEXT NOT NULL,
          batch_id TEXT NOT NULL,
          assigned_at TEXT NOT NULL,
          PRIMARY KEY(target_table, source_table, item_id),
          FOREIGN KEY(batch_id) REFERENCES export_batches(batch_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_export_batch_items_batch ON export_batch_items(batch_id);

        CREATE TRIGGER IF NOT EXISTS trg_reopen_batch_on_unexported_event
        AFTER UPDATE OF exported ON events
        WHEN NEW.exported = 0 AND OLD.exported = 1
        BEGIN
            UPDATE export_batches
            SET status = 'pending', acknowledged_at = NULL
            WHERE batch_id IN (SELECT batch_id FROM export_batch_items WHERE source_table = 'events' AND item_id = NEW.event_id);
        END;

        CREATE TRIGGER IF NOT EXISTS trg_reopen_batch_on_unexported_book_obs
        AFTER UPDATE OF exported ON book_observations
        WHEN NEW.exported = 0 AND OLD.exported = 1
        BEGIN
            UPDATE export_batches
            SET status = 'pending', acknowledged_at = NULL
            WHERE batch_id IN (SELECT batch_id FROM export_batch_items WHERE source_table = 'book_observations' AND item_id = CAST(NEW.id AS TEXT));
        END;

        CREATE TABLE IF NOT EXISTS wallet_trades (
          trade_id TEXT PRIMARY KEY,
          event_id TEXT NOT NULL,
          wallet_address TEXT NOT NULL,
          venue TEXT NOT NULL,
          market_id TEXT NOT NULL,
          attribution_type TEXT NOT NULL,
          side TEXT NOT NULL,
          price TEXT NOT NULL,
          quantity TEXT NOT NULL,
          notional TEXT NOT NULL,
          exchange_timestamp TEXT,
          received_timestamp TEXT NOT NULL,
          chain_id INTEGER DEFAULT 137,
          exchange_address TEXT DEFAULT '0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e',
          transaction_hash TEXT,
          block_number INTEGER,
          log_index INTEGER,
          condition_id TEXT,
          token_id TEXT DEFAULT '0',
          outcome_label TEXT,
          timestamp_source TEXT DEFAULT 'block_header',
          match_type TEXT DEFAULT 'complementary',
          source TEXT NOT NULL,
          exported INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_wallet_trades_wallet ON wallet_trades(wallet_address, received_timestamp);
        CREATE INDEX IF NOT EXISTS idx_wallet_trades_market ON wallet_trades(venue, market_id, received_timestamp);

        CREATE TABLE IF NOT EXISTS trade_markouts (
          trade_id TEXT NOT NULL,
          wallet_address TEXT NOT NULL,
          venue TEXT NOT NULL,
          market_id TEXT NOT NULL,
          side TEXT NOT NULL,
          horizon_seconds INTEGER NOT NULL,
          calc_version TEXT NOT NULL,
          status TEXT NOT NULL,
          exec_price REAL NOT NULL,
          future_mid REAL,
          markout REAL,
          markout_bps REAL,
          observation_time TEXT,
          trade_timestamp TEXT,
          calculated_at TEXT NOT NULL,
          token_id TEXT,
          condition_id TEXT,
          exported INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY(trade_id, horizon_seconds, calc_version)
        );
        CREATE INDEX IF NOT EXISTS idx_trade_markouts_wallet ON trade_markouts(wallet_address, horizon_seconds, calc_version);

        CREATE TRIGGER IF NOT EXISTS trg_reopen_batch_on_unexported_wallet_trades
        AFTER UPDATE OF exported ON wallet_trades
        WHEN NEW.exported = 0 AND OLD.exported = 1
        BEGIN
            UPDATE export_batches
            SET status = 'pending', acknowledged_at = NULL
            WHERE batch_id IN (SELECT batch_id FROM export_batch_items WHERE source_table = 'wallet_trades' AND item_id = NEW.trade_id);
        END;

        CREATE TRIGGER IF NOT EXISTS trg_reopen_batch_on_unexported_trade_markouts
        AFTER UPDATE OF exported ON trade_markouts
        WHEN NEW.exported = 0 AND OLD.exported = 1
        BEGIN
            UPDATE export_batches
            SET status = 'pending', acknowledged_at = NULL
            WHERE batch_id IN (SELECT batch_id FROM export_batch_items WHERE source_table = 'trade_markouts' AND item_id = (NEW.trade_id || ':' || NEW.horizon_seconds || ':' || NEW.calc_version));
        END;
        """)

        # Automated schema column migration for existing sqlite journals
        cols_wt = [r[1] for r in self.db.execute("PRAGMA table_info(wallet_trades)").fetchall()]
        for col_name, col_def in [
            ("chain_id", "INTEGER DEFAULT 137"),
            ("exchange_address", "TEXT DEFAULT '0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e'"),
            ("condition_id", "TEXT"),
            ("token_id", "TEXT DEFAULT '0'"),
            ("outcome_label", "TEXT"),
            ("timestamp_source", "TEXT DEFAULT 'block_header'"),
            ("match_type", "TEXT DEFAULT 'complementary'"),
        ]:
            if col_name not in cols_wt:
                self.db.execute(f"ALTER TABLE wallet_trades ADD COLUMN {col_name} {col_def}")

        cols_tm = [r[1] for r in self.db.execute("PRAGMA table_info(trade_markouts)").fetchall()]
        for col_name, col_def in [
            ("trade_timestamp", "TEXT"),
            ("token_id", "TEXT"),
            ("condition_id", "TEXT"),
        ]:
            if col_name not in cols_tm:
                self.db.execute(f"ALTER TABLE trade_markouts ADD COLUMN {col_name} {col_def}")

        # Phase 6: Contract matching and cross-venue intelligence column migrations
        cols_cp = [r[1] for r in self.db.execute("PRAGMA table_info(contract_pairs)").fetchall()]
        for col_name, col_def in [
            ("canonical_event", "TEXT DEFAULT ''"),
            ("canonical_outcome", "TEXT DEFAULT ''"),
            ("equivalence_mapping", "TEXT DEFAULT 'direct'"),
            ("resolution_criteria", "TEXT DEFAULT ''"),
            ("polymarket_expiration", "TEXT DEFAULT ''"),
            ("kalshi_expiration", "TEXT DEFAULT ''"),
            ("settlement_details", "TEXT DEFAULT ''"),
            ("reviewer", "TEXT DEFAULT ''"),
            ("verified_at", "TEXT"),
        ]:
            if col_name not in cols_cp:
                self.db.execute(f"ALTER TABLE contract_pairs ADD COLUMN {col_name} {col_def}")

        self.db.commit()

    def event(self, event):
        """Keep the Phase 1 demo path compatible with migrated SQLite tables."""
        cur = self.db.execute("""INSERT OR IGNORE INTO events
            (event_id, venue, market_id, event_type, event_time, received_at, sequence, payload, exported, applied)
            VALUES(?,?,?,?,?,?,?,?,0,1)""",
            (event["event_id"], event["venue"], event["market_id"], event["event_type"],
             event["event_time"], event["received_at"], event["sequence"],
             json.dumps(event["payload"], separators=(",", ":"))))
        self.db.commit()
        return cur.rowcount == 1

    def append(self, event: NormalizedEvent):
        """Commit the validated event before applying it to the in-memory book."""
        row = event.as_row()
        cur = self.db.execute("""INSERT OR IGNORE INTO events
            (event_id, venue, market_id, event_type, event_time, received_at, sequence,
             payload, exchange_timestamp, source_sequence, book_sequence, price,
             quantity, side, source_id, normalized_json, applied, exported)
             VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,0)""",
            (row["event_id"], row["venue"], row["market_id"], row["event_type"],
             row["exchange_timestamp"] or row["received_timestamp"], row["received_timestamp"],
             row["book_sequence"] or 0, json.dumps(row["raw_payload"], separators=(",", ":")),
             row["exchange_timestamp"], row["sequence_number"], row["book_sequence"],
             row["price"], row["quantity"], row["side"], row["source_id"],
             json.dumps(asdict(event), separators=(",", ":"))))
        self.db.commit()
        return cur.rowcount == 1

    def finish(self, event: NormalizedEvent, view=None):
        """Observation and applied marker commit together for restart replay."""
        self.db.execute("BEGIN")
        try:
            if view is not None:
                self.db.execute("""INSERT INTO book_observations
                    (venue, market_id, recorded_at, sequence, payload)
                    VALUES(?,?,?,?,?)""",
                    (event.venue, event.market_id, time.time(), event.book_sequence,
                     json.dumps(view, separators=(",", ":"))))
            if event.event_type == "trade":
                self.db.execute("""INSERT OR IGNORE INTO public_trades VALUES(?,?,?,?,?,?,?,?,?)""",
                    (event.event_id, event.venue, event.market_id, event.source_id,
                     event.side, event.price, event.quantity, event.exchange_timestamp,
                     event.received_timestamp))
            if event.event_type == "fill" and event.venue == "polygon":
                for wt in extract_wallet_trades(event):
                    self.db.execute("""
                        INSERT OR IGNORE INTO wallet_trades
                        (trade_id, event_id, wallet_address, venue, market_id,
                         attribution_type, side, price, quantity, notional,
                         exchange_timestamp, received_timestamp, chain_id, exchange_address,
                         transaction_hash, block_number, log_index, condition_id,
                         token_id, outcome_label, timestamp_source, match_type, source, exported)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                    """, (
                        wt.trade_id, wt.event_id, wt.wallet_address, wt.venue, wt.market_id,
                        wt.attribution_type, wt.side, wt.price, wt.quantity, wt.notional,
                        wt.exchange_timestamp, wt.received_timestamp, wt.chain_id, wt.exchange_address,
                        wt.transaction_hash, wt.block_number, wt.log_index, wt.condition_id,
                        wt.token_id, wt.outcome_label, wt.timestamp_source, wt.match_type, wt.source
                    ))
            if event.event_type == "fill" and isinstance(event.raw_payload, dict) and "decoded" in event.raw_payload:
                decoded = event.raw_payload["decoded"]
                ts = datetime.fromisoformat(event.exchange_timestamp).timestamp() if event.exchange_timestamp else time.time()
                self.db.execute("""INSERT OR IGNORE INTO trades VALUES(?,?,?,?,?,?,?,?)""",
                    (event.event_id, decoded.get("maker", "0x0"), event.venue, event.market_id,
                     event.side, float(event.price), float(event.quantity), ts))
            self.db.execute("UPDATE events SET applied=1 WHERE event_id=?", (event.event_id,))
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def add_trade(self, trade):
        cur = super().add_trade(trade)
        wt = create_demo_wallet_trade(trade)
        if wt:
            self.db.execute("""
                INSERT OR IGNORE INTO wallet_trades
                (trade_id, event_id, wallet_address, venue, market_id,
                 attribution_type, side, price, quantity, notional,
                 exchange_timestamp, received_timestamp, chain_id, exchange_address,
                 transaction_hash, block_number, log_index, condition_id,
                 token_id, outcome_label, timestamp_source, match_type, source, exported)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            """, (
                wt.trade_id, wt.event_id, wt.wallet_address, wt.venue, wt.market_id,
                wt.attribution_type, wt.side, wt.price, wt.quantity, wt.notional,
                wt.exchange_timestamp, wt.received_timestamp, wt.chain_id, wt.exchange_address,
                wt.transaction_hash, wt.block_number, wt.log_index, wt.condition_id,
                wt.token_id, wt.outcome_label, wt.timestamp_source, wt.match_type, wt.source
            ))
            self.db.commit()
        return cur

    def unprocessed(self):
        rows = self.db.execute("SELECT normalized_json FROM events WHERE applied=0 AND normalized_json IS NOT NULL ORDER BY rowid").fetchall()
        return [NormalizedEvent(**json.loads(row[0])) for row in rows]

    def already_applied(self, event_id):
        row = self.db.execute("SELECT applied FROM events WHERE event_id=?", (event_id,)).fetchone()
        return bool(row and row[0])

    def recent_trades(self, limit=100):
        public = [dict(row) for row in self.db.execute("SELECT * FROM public_trades ORDER BY received_timestamp DESC LIMIT ?", (limit,))]
        wallet = self.trades()
        return sorted([{
            "trade_id": row["event_id"], "wallet": None, "venue": row["venue"],
            "market_id": row["market_id"], "side": row["side"],
            "price": float(row["price"]), "quantity": float(row["quantity"]) if row["quantity"] is not None else None,
            "executed_at": datetime.fromisoformat(row["exchange_timestamp"] or row["received_timestamp"]).timestamp(),
            "source_id": row["source_id"]} for row in public] + wallet,
            key=lambda row: row["executed_at"], reverse=True)[:limit]

    def log_health(self, venue, status, detail=None):
        self.db.execute("INSERT INTO feed_health(venue,status,observed_at,detail) VALUES(?,?,?,?)",
            (venue, status, now_utc(), detail))
        self.db.commit()

    def log_error(self, venue, market_id, event_id, error):
        self.db.execute("INSERT INTO ingestion_errors(venue,market_id,event_id,error,observed_at) VALUES(?,?,?,?,?)",
            (venue, market_id, event_id, str(error)[:1000], now_utc()))
        self.db.commit()

    def pending_rows(self, table, limit=250):
        if table not in ("events", "book_observations", "feed_health", "ingestion_errors"):
            raise ValueError("unknown export table")
        return [dict(row) for row in self.db.execute(f"SELECT * FROM {table} WHERE exported=0 ORDER BY rowid LIMIT ?", (limit,))]

    def mark_rows_exported(self, table, ids):
        key = "event_id" if table == "events" else "id"
        if table not in ("events", "book_observations", "feed_health", "ingestion_errors"):
            raise ValueError("unknown export table")
        self.db.executemany(f"UPDATE {table} SET exported=1 WHERE {key}=?", [(x,) for x in ids])
        self.db.commit()

    def _fetch_unassigned_items(self, target_table: str, limit: int = 250):
        """Fetch items not yet assigned to an export batch for target_table."""
        if target_table == "market_events":
            rows = self.db.execute("""
                SELECT e.* FROM events e
                LEFT JOIN export_batch_items bi
                  ON bi.target_table = 'market_events' AND bi.source_table = 'events' AND bi.item_id = e.event_id
                WHERE bi.item_id IS NULL
                ORDER BY e.rowid ASC
                LIMIT ?
            """, (limit,)).fetchall()
            return "events", [dict(r) for r in rows]
        elif target_table == "trades":
            rows = self.db.execute("""
                SELECT e.* FROM events e
                LEFT JOIN export_batch_items bi
                  ON bi.target_table = 'trades' AND bi.source_table = 'events' AND bi.item_id = e.event_id
                WHERE e.event_type IN ('trade', 'fill') AND bi.item_id IS NULL
                ORDER BY e.rowid ASC
                LIMIT ?
            """, (limit,)).fetchall()
            return "events", [dict(r) for r in rows]
        elif target_table == "orderbook_snapshots":
            rows = self.db.execute("""
                SELECT b.* FROM book_observations b
                LEFT JOIN export_batch_items bi
                  ON bi.target_table = 'orderbook_snapshots' AND bi.source_table = 'book_observations' AND bi.item_id = CAST(b.id AS TEXT)
                WHERE bi.item_id IS NULL
                ORDER BY b.rowid ASC
                LIMIT ?
            """, (limit,)).fetchall()
            return "book_observations", [dict(r) for r in rows]
        elif target_table == "feed_health":
            rows = self.db.execute("""
                SELECT f.* FROM feed_health f
                LEFT JOIN export_batch_items bi
                  ON bi.target_table = 'feed_health' AND bi.source_table = 'feed_health' AND bi.item_id = CAST(f.id AS TEXT)
                WHERE bi.item_id IS NULL
                ORDER BY f.rowid ASC
                LIMIT ?
            """, (limit,)).fetchall()
            return "feed_health", [dict(r) for r in rows]
        elif target_table == "ingestion_errors":
            rows = self.db.execute("""
                SELECT i.* FROM ingestion_errors i
                LEFT JOIN export_batch_items bi
                  ON bi.target_table = 'ingestion_errors' AND bi.source_table = 'ingestion_errors' AND bi.item_id = CAST(i.id AS TEXT)
                WHERE bi.item_id IS NULL
                ORDER BY i.rowid ASC
                LIMIT ?
            """, (limit,)).fetchall()
            return "ingestion_errors", [dict(r) for r in rows]
        elif target_table == "wallet_trades":
            rows = self.db.execute("""
                SELECT w.* FROM wallet_trades w
                LEFT JOIN export_batch_items bi
                  ON bi.target_table = 'wallet_trades' AND bi.source_table = 'wallet_trades' AND bi.item_id = w.trade_id
                WHERE bi.item_id IS NULL
                ORDER BY w.rowid ASC
                LIMIT ?
            """, (limit,)).fetchall()
            return "wallet_trades", [dict(r) for r in rows]
        elif target_table == "trade_markouts":
            rows = self.db.execute("""
                SELECT m.*, (m.trade_id || ':' || m.horizon_seconds || ':' || m.calc_version) AS markout_key
                FROM trade_markouts m
                LEFT JOIN export_batch_items bi
                  ON bi.target_table = 'trade_markouts' AND bi.source_table = 'trade_markouts' AND bi.item_id = (m.trade_id || ':' || m.horizon_seconds || ':' || m.calc_version)
                WHERE bi.item_id IS NULL
                ORDER BY m.rowid ASC
                LIMIT ?
            """, (limit,)).fetchall()
            return "trade_markouts", [dict(r) for r in rows]
        else:
            raise ValueError(f"Unknown target ClickHouse table: {target_table}")

    def _serialize_target_rows(self, target_table: str, items: list):
        if target_table == "market_events":
            return _clickhouse_rows("events", items)
        elif target_table == "trades":
            return [{
                "event_id": row["event_id"],
                "venue": row["venue"],
                "market_id": row["market_id"],
                "source_id": row["source_id"],
                "side": row["side"],
                "price": row["price"],
                "quantity": row["quantity"],
                "exchange_timestamp": row["exchange_timestamp"],
                "received_timestamp": row["received_at"],
                "wallet": (json.loads(row["payload"]).get("decoded", {}).get("maker")
                           if row["event_type"] == "fill" and isinstance(row["payload"], str) and "decoded" in row["payload"]
                           else None)
            } for row in items]
        elif target_table == "orderbook_snapshots":
            return _clickhouse_rows("book_observations", items)
        elif target_table in ("feed_health", "ingestion_errors"):
            return _clickhouse_rows(target_table, items)
        elif target_table == "wallet_trades":
            return [{
                "trade_id": row["trade_id"],
                "event_id": row["event_id"],
                "wallet_address": row["wallet_address"],
                "venue": row["venue"],
                "market_id": row["market_id"],
                "attribution_type": row["attribution_type"],
                "side": row["side"],
                "price": str(row["price"]),
                "quantity": str(row["quantity"]),
                "notional": str(row["notional"]),
                "exchange_timestamp": row["exchange_timestamp"],
                "received_timestamp": row["received_timestamp"],
                "chain_id": row.get("chain_id") or 137,
                "exchange_address": (row.get("exchange_address") or "0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e").lower(),
                "transaction_hash": row["transaction_hash"],
                "block_number": row["block_number"],
                "log_index": row["log_index"],
                "condition_id": row.get("condition_id"),
                "token_id": str(row.get("token_id") or "0"),
                "outcome_label": row.get("outcome_label"),
                "timestamp_source": row.get("timestamp_source") or "block_header",
                "match_type": row.get("match_type") or "complementary",
                "source": row["source"]
            } for row in items]
        elif target_table == "trade_markouts":
            return [{
                "trade_id": row["trade_id"],
                "wallet_address": row["wallet_address"],
                "venue": row["venue"],
                "market_id": row["market_id"],
                "side": row["side"],
                "horizon_seconds": int(row["horizon_seconds"]),
                "calc_version": row["calc_version"],
                "status": row["status"],
                "exec_price": f"{float(row['exec_price']):.6f}",
                "future_mid": f"{float(row['future_mid']):.6f}" if row["future_mid"] is not None else None,
                "markout": f"{float(row['markout']):.6f}" if row["markout"] is not None else None,
                "markout_bps": f"{float(row['markout_bps']):.4f}" if row["markout_bps"] is not None else None,
                "observation_time": row["observation_time"],
                "trade_timestamp": row.get("trade_timestamp") or row["calculated_at"],
                "calculated_at": row["calculated_at"],
                "token_id": row.get("token_id"),
                "condition_id": row.get("condition_id")
            } for row in items]
        raise ValueError(f"Unknown target table: {target_table}")

    def create_export_batch(self, target_table: str, limit: int = 250):
        """Create an immutable persistent export batch inside a SQLite transaction."""
        import uuid
        if self.db.in_transaction:
            self.db.commit()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            source_table, items = self._fetch_unassigned_items(target_table, limit)
            if not items:
                self.db.commit()
                return None

            batch_id = str(uuid.uuid4())
            rows = self._serialize_target_rows(target_table, items)
            payload = "\n".join(json.dumps(r, separators=(",", ":"), sort_keys=True) for r in rows) + "\n"
            dedup_token = hashlib.sha256(f"{target_table}:{payload}".encode("utf-8")).hexdigest()

            if target_table == "wallet_trades":
                item_ids = [str(r["trade_id"]) for r in items]
            elif target_table == "trade_markouts":
                item_ids = [str(r.get("markout_key") or f"{r['trade_id']}:{r['horizon_seconds']}:{r['calc_version']}") for r in items]
            else:
                item_ids = [str(r["event_id"] if "event_id" in r else r["id"]) for r in items]
            created_at = now_utc()

            self.db.execute("""
                INSERT INTO export_batches
                (batch_id, target_table, source_table, item_count, event_ids, payload, dedup_token, created_at, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')
            """, (batch_id, target_table, source_table, len(items), json.dumps(item_ids), payload, dedup_token, created_at))

            self.db.executemany("""
                INSERT INTO export_batch_items
                (target_table, source_table, item_id, batch_id, assigned_at)
                VALUES (?, ?, ?, ?, ?)
            """, [(target_table, source_table, i_id, batch_id, created_at) for i_id in item_ids])

            self.db.commit()
            return self.get_batch(batch_id)
        except Exception:
            self.db.rollback()
            raise

    def get_batch(self, batch_id: str):
        row = self.db.execute("SELECT * FROM export_batches WHERE batch_id = ?", (batch_id,)).fetchone()
        return dict(row) if row else None

    def get_pending_batch(self, target_table: str):
        row = self.db.execute(
            "SELECT * FROM export_batches WHERE target_table = ? AND status = 'pending' ORDER BY created_at ASC LIMIT 1",
            (target_table,)
        ).fetchone()
        return dict(row) if row else None

    def get_or_create_pending_batch(self, target_table: str, limit: int = 250):
        quarantined = self.db.execute(
            "SELECT 1 FROM export_batches WHERE target_table = ? AND status = 'quarantined' LIMIT 1",
            (target_table,)
        ).fetchone()
        if quarantined:
            return None
        pending = self.get_pending_batch(target_table)
        if pending:
            return pending
        return self.create_export_batch(target_table, limit=limit)

    def record_batch_attempt(self, batch_id: str, error: str = None):
        now = now_utc()
        err_str = str(error)[:1000] if error else None
        self.db.execute("""
            UPDATE export_batches
            SET attempts = attempts + 1,
                last_attempt_at = ?,
                last_error = ?
            WHERE batch_id = ?
        """, (now, err_str, batch_id))
        self.db.commit()

    def acknowledge_batch(self, batch_id: str) -> bool:
        """Mark a batch acknowledged only after ClickHouse confirms HTTP 200."""
        now = now_utc()
        if self.db.in_transaction:
            self.db.commit()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            cur = self.db.execute(
                "UPDATE export_batches SET status = 'acknowledged', acknowledged_at = ? WHERE batch_id = ? AND status IN ('pending', 'quarantined')",
                (now, batch_id)
            )
            if cur.rowcount == 0:
                self.db.commit()
                return False

            batch = self.get_batch(batch_id)
            if batch:
                ids = json.loads(batch["event_ids"])
                source = batch["source_table"]
                target = batch["target_table"]
                if source == "events":
                    for x in ids:
                        self.db.execute("""
                            UPDATE events SET exported = 1
                            WHERE event_id = ?
                              AND event_id IN (
                                  SELECT bi.item_id FROM export_batch_items bi
                                  JOIN export_batches b ON bi.batch_id = b.batch_id
                                  WHERE bi.target_table = 'market_events' AND bi.item_id = ? AND b.status = 'acknowledged'
                              )
                              AND (
                                  event_type NOT IN ('trade', 'fill')
                                  OR event_id IN (
                                      SELECT bi.item_id FROM export_batch_items bi
                                      JOIN export_batches b ON bi.batch_id = b.batch_id
                                      WHERE bi.target_table = 'trades' AND bi.item_id = ? AND b.status = 'acknowledged'
                                  )
                              )
                        """, (x, x, x))
                elif source == "book_observations":
                    self.db.executemany("UPDATE book_observations SET exported = 1 WHERE id = ?", [(int(x),) for x in ids])
                elif source == "feed_health":
                    self.db.executemany("UPDATE feed_health SET exported = 1 WHERE id = ?", [(int(x),) for x in ids])
                elif source == "ingestion_errors":
                    self.db.executemany("UPDATE ingestion_errors SET exported = 1 WHERE id = ?", [(int(x),) for x in ids])
                elif source == "wallet_trades":
                    self.db.executemany("UPDATE wallet_trades SET exported = 1 WHERE trade_id = ?", [(x,) for x in ids])
                elif source == "trade_markouts":
                    for key in ids:
                        parts = key.split(":")
                        if len(parts) >= 3:
                            t_id = ":".join(parts[:-2])
                            h_sec = int(parts[-2])
                            c_ver = parts[-1]
                            self.db.execute(
                                "UPDATE trade_markouts SET exported = 1 WHERE trade_id = ? AND horizon_seconds = ? AND calc_version = ?",
                                (t_id, h_sec, c_ver)
                            )

            self.db.commit()
            return True
        except Exception:
            self.db.rollback()
            raise

    def check_dedup_window_risk(self, batch: dict, max_age_seconds: int = 3600, max_attempts: int = 25) -> bool:
        """
        Monitors whether a batch is old enough that eviction from ClickHouse's
        finite deduplication window is a concern.
        Quarantines at-risk batches so they are not silently retried.
        """
        if batch.get("status") != "pending":
            return False
        created = datetime.fromisoformat(batch["created_at"])
        age_seconds = (datetime.now(timezone.utc) - created).total_seconds()
        attempts = int(batch.get("attempts", 0))

        if age_seconds > max_age_seconds or attempts >= max_attempts:
            LOG.error(
                "ClickHouse export batch %s quarantined: deduplication window eviction risk "
                "(target=%s, age=%.1fs, attempts=%d, token=%s). Manual recovery required.",
                batch["batch_id"], batch["target_table"], age_seconds, attempts, batch["dedup_token"]
            )
            self.db.execute(
                "UPDATE export_batches SET status = 'quarantined', last_error = 'DEDUPLICATION_WINDOW_EVICTION_RISK' WHERE batch_id = ?",
                (batch["batch_id"],)
            )
            self.db.commit()
            return True
        return False

    def recover_quarantined_batch(self, batch_id: str, action: str = "verify_and_ack") -> bool:
        batch = self.get_batch(batch_id)
        if not batch or batch["status"] != "quarantined":
            return False
        if action == "verify_and_ack":
            return self.acknowledge_batch(batch_id)
        elif action == "force_retry":
            now = now_utc()
            self.db.execute(
                "UPDATE export_batches SET status = 'pending', attempts = 0, created_at = ?, last_error = NULL WHERE batch_id = ?",
                (now, batch_id)
            )
            self.db.commit()
            return True
        else:
            raise ValueError(f"Unknown recovery action: {action}")

    def pending_batches(self, target_table: str = None):
        if target_table:
            rows = self.db.execute("SELECT * FROM export_batches WHERE status = 'pending' AND target_table = ? ORDER BY created_at ASC", (target_table,)).fetchall()
        else:
            rows = self.db.execute("SELECT * FROM export_batches WHERE status = 'pending' ORDER BY created_at ASC").fetchall()
        return [dict(r) for r in rows]

    def quarantined_batches(self, target_table: str = None):
        if target_table:
            rows = self.db.execute("SELECT * FROM export_batches WHERE status = 'quarantined' AND target_table = ? ORDER BY created_at ASC", (target_table,)).fetchall()
        else:
            rows = self.db.execute("SELECT * FROM export_batches WHERE status = 'quarantined' ORDER BY created_at ASC").fetchall()
        return [dict(r) for r in rows]

    def ingestion_lag_seconds(self):
        row = self.db.execute("SELECT received_at FROM events WHERE exported=0 ORDER BY received_at LIMIT 1").fetchone()
        if not row:
            return 0.0
        return max(0.0, time.time() - datetime.fromisoformat(row[0]).timestamp())

    def checkpoint(self, source):
        row = self.db.execute("SELECT value FROM checkpoints WHERE source=?", (source,)).fetchone()
        return row[0] if row else None

    def set_checkpoint(self, source, value):
        self.db.execute("INSERT INTO checkpoints VALUES(?,?) ON CONFLICT(source) DO UPDATE SET value=excluded.value", (source, value))
        self.db.commit()

    def contract_pairs(self, verified_only=False, status=None):
        query = "SELECT * FROM contract_pairs"
        clauses = []
        params = []
        if verified_only:
            clauses.append("verification_status='verified'")
        elif status and status != "all":
            clauses.append("verification_status=?")
            params.append(status)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY updated_at DESC"
        rows = [dict(row) for row in self.db.execute(query, params)]
        for r in rows:
            if not r.get("canonical_event"):
                r["canonical_event"] = r.get("event_description", "")
            if not r.get("canonical_outcome"):
                r["canonical_outcome"] = r.get("outcome_equivalence", "")
            if not r.get("event_description"):
                r["event_description"] = r.get("canonical_event", "")
            if not r.get("outcome_equivalence"):
                r["outcome_equivalence"] = r.get("canonical_outcome", "")
            if not r.get("equivalence_mapping"):
                r["equivalence_mapping"] = "direct"
        return rows

    def get_contract_pair(self, polymarket_asset_id, kalshi_ticker):
        row = self.db.execute(
            "SELECT * FROM contract_pairs WHERE polymarket_asset_id=? AND kalshi_ticker=?",
            (polymarket_asset_id, kalshi_ticker)
        ).fetchone()
        if not row:
            return None
        r = dict(row)
        if not r.get("canonical_event"):
            r["canonical_event"] = r.get("event_description", "")
        if not r.get("canonical_outcome"):
            r["canonical_outcome"] = r.get("outcome_equivalence", "")
        if not r.get("event_description"):
            r["event_description"] = r.get("canonical_event", "")
        if not r.get("outcome_equivalence"):
            r["outcome_equivalence"] = r.get("canonical_outcome", "")
        if not r.get("equivalence_mapping"):
            r["equivalence_mapping"] = "direct"
        return r

    def save_contract_pair(self, pair):
        canonical_event = pair.get("canonical_event") or pair.get("event_description", "")
        canonical_outcome = pair.get("canonical_outcome") or pair.get("outcome_equivalence", "")
        event_description = pair.get("event_description") or canonical_event
        outcome_equivalence = pair.get("outcome_equivalence") or canonical_outcome
        equivalence_mapping = pair.get("equivalence_mapping", "direct")
        resolution_source = pair.get("resolution_source", "")
        resolution_criteria = pair.get("resolution_criteria", "")
        polymarket_expiration = pair.get("polymarket_expiration") or pair.get("expiration", "")
        kalshi_expiration = pair.get("kalshi_expiration") or pair.get("expiration", "")
        expiration = pair.get("expiration") or min(polymarket_expiration, kalshi_expiration)
        settlement_details = pair.get("settlement_details", "")
        verification_status = pair.get("verification_status", "unverified")
        reviewer = pair.get("reviewer", "")
        verified_at = pair.get("verified_at")
        now_ts = now_utc()

        if verification_status == "verified" and not verified_at:
            verified_at = now_ts
        elif verification_status != "verified":
            verified_at = None

        self.db.execute("""INSERT INTO contract_pairs (
            polymarket_asset_id, kalshi_ticker, canonical_event, canonical_outcome,
            event_description, outcome_equivalence, equivalence_mapping,
            resolution_source, resolution_criteria, polymarket_expiration,
            kalshi_expiration, expiration, settlement_details,
            verification_status, reviewer, verified_at, updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(polymarket_asset_id, kalshi_ticker) DO UPDATE SET
            canonical_event=excluded.canonical_event,
            canonical_outcome=excluded.canonical_outcome,
            event_description=excluded.event_description,
            outcome_equivalence=excluded.outcome_equivalence,
            equivalence_mapping=excluded.equivalence_mapping,
            resolution_source=excluded.resolution_source,
            resolution_criteria=excluded.resolution_criteria,
            polymarket_expiration=excluded.polymarket_expiration,
            kalshi_expiration=excluded.kalshi_expiration,
            expiration=excluded.expiration,
            settlement_details=excluded.settlement_details,
            verification_status=excluded.verification_status,
            reviewer=excluded.reviewer,
            verified_at=excluded.verified_at,
            updated_at=excluded.updated_at""",
            (pair["polymarket_asset_id"], pair["kalshi_ticker"],
             canonical_event, canonical_outcome,
             event_description, outcome_equivalence,
             equivalence_mapping, resolution_source, resolution_criteria,
             polymarket_expiration, kalshi_expiration, expiration,
             settlement_details, verification_status, reviewer,
             verified_at, now_ts))
        self.db.commit()

    def delete_contract_pair(self, polymarket_asset_id, kalshi_ticker):
        cur = self.db.execute("DELETE FROM contract_pairs WHERE polymarket_asset_id=? AND kalshi_ticker=?",
                              (polymarket_asset_id, kalshi_ticker))
        self.db.commit()
        return cur.rowcount > 0

    def future_mid_v2(self, venue, market_id, target, tolerance_seconds=5.0, current_time=None, grace_seconds=5.0):
        """
        Looks up future midpoint within [target - 0.5, target + tolerance_seconds].
        Validates against crossed/locked books and stale observations.
        Differentiates 'pending' from 'unavailable' during the active evaluation grace window.
        Returns (status, mid_price, obs_time).
        """
        now_ts = current_time if current_time is not None else time.time()
        rows = self.db.execute("""
            SELECT recorded_at, payload
            FROM book_observations
            WHERE venue = ? AND market_id = ?
              AND recorded_at >= ? AND recorded_at <= ?
            ORDER BY ABS(recorded_at - ?) ASC
            LIMIT 5
        """, (venue, market_id, target - 0.5, target + tolerance_seconds, target)).fetchall()

        if not rows:
            if now_ts < target + tolerance_seconds + grace_seconds:
                return "pending", None, None
            return "unavailable", None, None

        for r in rows:
            rec_at = float(r["recorded_at"])
            obs_iso = datetime.fromtimestamp(rec_at, timezone.utc).isoformat(timespec="milliseconds")
            try:
                view = json.loads(r["payload"])
            except Exception:
                continue

            # Check quote staleness
            if view.get("is_stale") or (abs(rec_at - target) > 10.0):
                return "stale", None, obs_iso

            bid = view.get("best_bid")
            ask = view.get("best_ask")
            valid = int(view.get("valid", True))
            status, mid = validate_snapshot_quotes(bid, ask, valid)
            if status == "available":
                return "available", mid, obs_iso
            elif status == "crossed":
                return "crossed", None, obs_iso

        return "unavailable", None, None

    def compute_and_store_markouts(self, trade_id: str, horizons=DEFAULT_HORIZONS, calc_version=CURRENT_CALC_VERSION, force=False):
        """Computes and stores markout records for a trade across specified horizons."""
        trade_row = self.db.execute("SELECT * FROM wallet_trades WHERE trade_id = ?", (trade_id,)).fetchone()
        if not trade_row:
            trade_row = self.db.execute("SELECT * FROM trades WHERE trade_id = ?", (trade_id,)).fetchone()
            if not trade_row:
                return []
            trade = {
                "trade_id": trade_row["trade_id"],
                "wallet_address": normalize_address(trade_row["wallet"]) if trade_row["wallet"] else "0x0000000000000000000000000000000000000000",
                "venue": trade_row["venue"],
                "market_id": trade_row["market_id"],
                "side": trade_row["side"],
                "price": float(trade_row["price"]),
                "executed_at": float(trade_row["executed_at"]),
                "trade_timestamp": datetime.fromtimestamp(float(trade_row["executed_at"]), timezone.utc).isoformat(timespec="milliseconds"),
                "token_id": None,
                "condition_id": None
            }
        else:
            exec_ts = None
            if trade_row["exchange_timestamp"]:
                try:
                    exec_ts = datetime.fromisoformat(trade_row["exchange_timestamp"].replace("Z", "+00:00")).timestamp()
                except ValueError:
                    pass
            if exec_ts is None:
                exec_ts = datetime.fromisoformat(trade_row["received_timestamp"].replace("Z", "+00:00")).timestamp()
            trade_ts_str = trade_row["exchange_timestamp"] or trade_row["received_timestamp"]
            tok_id = trade_row["token_id"] if "token_id" in trade_row.keys() else None
            cond_id = trade_row["condition_id"] if "condition_id" in trade_row.keys() else None
            trade = {
                "trade_id": trade_row["trade_id"],
                "wallet_address": trade_row["wallet_address"],
                "venue": trade_row["venue"],
                "market_id": trade_row["market_id"],
                "side": trade_row["side"],
                "price": float(trade_row["price"]),
                "executed_at": exec_ts,
                "trade_timestamp": trade_ts_str,
                "token_id": tok_id,
                "condition_id": cond_id
            }

        calc_time = now_utc()
        results = []
        for h in horizons:
            if not force:
                existing = self.db.execute(
                    "SELECT * FROM trade_markouts WHERE trade_id = ? AND horizon_seconds = ? AND calc_version = ?",
                    (trade["trade_id"], h, calc_version)
                ).fetchone()
                if existing:
                    results.append(dict(existing))
                    continue

            status, mid, obs_time = self.future_mid_v2(trade["venue"], trade["market_id"], trade["executed_at"] + h)
            markout_val = None
            markout_bps_val = None
            if status == "available" and mid is not None:
                markout_val, markout_bps_val = calculate_markout_metrics(trade["price"], trade["side"], mid)

            self.db.execute("""
                INSERT OR REPLACE INTO trade_markouts
                (trade_id, wallet_address, venue, market_id, side, horizon_seconds,
                 calc_version, status, exec_price, future_mid, markout, markout_bps,
                 observation_time, trade_timestamp, calculated_at, token_id, condition_id, exported)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            """, (
                trade["trade_id"], trade["wallet_address"], trade["venue"], trade["market_id"],
                trade["side"], h, calc_version, status, trade["price"], mid,
                markout_val, markout_bps_val, obs_time, trade["trade_timestamp"] or calc_time,
                calc_time, trade.get("token_id"), trade.get("condition_id")
            ))
            results.append({
                "trade_id": trade["trade_id"],
                "wallet_address": trade["wallet_address"],
                "venue": trade["venue"],
                "market_id": trade["market_id"],
                "side": trade["side"],
                "horizon_seconds": h,
                "calc_version": calc_version,
                "status": status,
                "exec_price": trade["price"],
                "future_mid": mid,
                "markout": markout_val,
                "markout_bps": markout_bps_val,
                "observation_time": obs_time,
                "trade_timestamp": trade["trade_timestamp"] or calc_time,
                "calculated_at": calc_time,
                "token_id": trade.get("token_id"),
                "condition_id": trade.get("condition_id"),
                "available": status == "available"
            })
        self.db.commit()
        return results

    def recompute_markouts(self, wallet_address=None, horizons=DEFAULT_HORIZONS, calc_version=CURRENT_CALC_VERSION, force=False):
        """Recomputes markouts across trades in SQLite."""
        if wallet_address:
            norm = normalize_address(wallet_address)
            trades = self.db.execute("SELECT trade_id FROM wallet_trades WHERE wallet_address = ?", (norm,)).fetchall()
            if not trades:
                trades = self.db.execute("SELECT trade_id FROM trades WHERE wallet = ?", (norm,)).fetchall()
        else:
            trades = self.db.execute("SELECT trade_id FROM wallet_trades").fetchall()
            if not trades:
                trades = self.db.execute("SELECT trade_id FROM trades").fetchall()
        
        count = 0
        for t in trades:
            self.compute_and_store_markouts(t["trade_id"], horizons=horizons, calc_version=calc_version, force=force)
            count += 1
        return count

    def wallets(self):
        w_rows = self.db.execute("""
            SELECT wallet_address AS wallet,
                   COUNT(*) AS trade_count,
                   COUNT(DISTINCT market_id) AS active_markets,
                   SUM(CASE WHEN attribution_type = 'maker' THEN 1 ELSE 0 END) AS maker_count,
                   SUM(CASE WHEN attribution_type = 'taker' THEN 1 ELSE 0 END) AS taker_count,
                   SUM(CASE WHEN attribution_type = 'synthetic' THEN 1 ELSE 0 END) AS synthetic_count
            FROM wallet_trades
            GROUP BY wallet_address
            ORDER BY trade_count DESC
        """).fetchall()
        if w_rows:
            return [dict(r) for r in w_rows]
        return super().wallets()

    def get_wallet_trades(self, wallet_address, limit=100, offset=0, venue=None, market_id=None):
        norm = normalize_address(wallet_address)
        filters = ["wallet_address = ?"]
        params = [norm]
        if venue:
            filters.append("venue = ?")
            params.append(venue)
        if market_id:
            filters.append("market_id = ?")
            params.append(market_id)
        where_clause = " WHERE " + " AND ".join(filters)
        params.extend([limit, offset])
        rows = self.db.execute(f"SELECT * FROM wallet_trades {where_clause} ORDER BY received_timestamp DESC LIMIT ? OFFSET ?", params).fetchall()
        if rows:
            return [dict(r) for r in rows]
        legacy = [dict(r) for r in self.db.execute("SELECT * FROM trades WHERE wallet = ? ORDER BY executed_at DESC LIMIT ? OFFSET ?", (norm, limit, offset))]
        return [{
            "trade_id": r["trade_id"], "wallet_address": r["wallet"], "venue": r["venue"],
            "market_id": r["market_id"], "attribution_type": "maker", "side": r["side"],
            "price": str(r["price"]), "quantity": str(r["quantity"]), "notional": str(round(r["price"] * r["quantity"], 4)),
            "received_timestamp": datetime.fromtimestamp(r["executed_at"], timezone.utc).isoformat(timespec="milliseconds"),
            "source": "legacy"
        } for r in legacy]

    def get_wallet_profile(self, wallet_address):
        norm = normalize_address(wallet_address)
        row = self.db.execute("""
            SELECT wallet_address,
                   COUNT(*) AS trade_count,
                   COUNT(DISTINCT market_id) AS market_count,
                   COUNT(DISTINCT venue) AS venue_count,
                   SUM(CAST(quantity AS REAL)) AS total_volume,
                   SUM(CAST(notional AS REAL)) AS total_notional,
                   MIN(received_timestamp) AS first_seen,
                   MAX(received_timestamp) AS last_seen,
                   SUM(CASE WHEN attribution_type = 'maker' THEN 1 ELSE 0 END) AS maker_count,
                   SUM(CASE WHEN attribution_type = 'taker' THEN 1 ELSE 0 END) AS taker_count
            FROM wallet_trades
            WHERE wallet_address = ?
        """, (norm,)).fetchone()
        if row and row["trade_count"] > 0:
            d = dict(row)
            d["total_volume"] = round(d["total_volume"] or 0.0, 4)
            d["total_notional"] = round(d["total_notional"] or 0.0, 2)
            return d
        
        legacy = self.trades(norm)
        if legacy:
            vol = sum(t["quantity"] for t in legacy)
            notional = sum(t["price"] * t["quantity"] for t in legacy)
            return {
                "wallet_address": norm,
                "trade_count": len(legacy),
                "market_count": len(set(t["market_id"] for t in legacy)),
                "venue_count": len(set(t["venue"] for t in legacy)),
                "total_volume": round(vol, 4),
                "total_notional": round(notional, 2),
                "first_seen": datetime.fromtimestamp(min(t["executed_at"] for t in legacy), timezone.utc).isoformat(timespec="milliseconds"),
                "last_seen": datetime.fromtimestamp(max(t["executed_at"] for t in legacy), timezone.utc).isoformat(timespec="milliseconds"),
                "maker_count": len(legacy),
                "taker_count": 0
            }
        return {
            "wallet_address": norm, "trade_count": 0, "market_count": 0, "venue_count": 0,
            "total_volume": 0.0, "total_notional": 0.0, "first_seen": None, "last_seen": None,
            "maker_count": 0, "taker_count": 0
        }

    def get_wallet_markouts(self, wallet_address, horizon=None, venue=None, market_id=None, calc_version=CURRENT_CALC_VERSION):
        norm = normalize_address(wallet_address)
        self.recompute_markouts(norm, calc_version=calc_version)
        
        horizons = [horizon] if horizon else list(DEFAULT_HORIZONS)
        summary = []
        entries = []
        
        for h in horizons:
            filters = ["wallet_address = ?", "horizon_seconds = ?", "calc_version = ?"]
            params = [norm, h, calc_version]
            if venue:
                filters.append("venue = ?")
                params.append(venue)
            if market_id:
                filters.append("market_id = ?")
                params.append(market_id)
            where_sql = " WHERE " + " AND ".join(filters)
            
            rows = [dict(r) for r in self.db.execute(f"SELECT * FROM trade_markouts {where_sql} ORDER BY rowid DESC", params)]
            
            trade_count = len(rows)
            available = [r for r in rows if r["status"] == "available" and r["markout"] is not None]
            avail_count = len(available)
            vals = [r["markout"] for r in available]
            bps_vals = [r["markout_bps"] for r in available if r["markout_bps"] is not None]
            
            avg_m = (sum(vals) / len(vals)) if vals else None
            avg_bps = (sum(bps_vals) / len(bps_vals)) if bps_vals else None
            pos_pct = (100.0 * sum(1 for v in vals if v > 0) / len(vals)) if vals else None
            
            status_counts = {
                "available": avail_count,
                "unavailable": sum(1 for r in rows if r["status"] == "unavailable"),
                "crossed": sum(1 for r in rows if r["status"] == "crossed"),
                "stale": sum(1 for r in rows if r["status"] == "stale")
            }
            
            summary.append({
                "horizon_seconds": h,
                "trade_count": trade_count,
                "available_count": avail_count,
                "average_markout": round(avg_m, 6) if avg_m is not None else None,
                "average_markout_bps": round(avg_bps, 2) if avg_bps is not None else None,
                "positive_percentage": round(pos_pct, 1) if pos_pct is not None else None,
                "status_counts": status_counts
            })
            
            for r in rows:
                entries.append({
                    "trade_id": r["trade_id"],
                    "horizon_seconds": h,
                    "side": r["side"],
                    "exec_price": r["exec_price"],
                    "future_mid": r["future_mid"],
                    "markout": r["markout"],
                    "markout_bps": r["markout_bps"],
                    "status": r["status"],
                    "available": r["status"] == "available",
                    "observation_time": r["observation_time"],
                    "calc_version": r["calc_version"]
                })
                
        return {"wallet": norm, "summary": summary, "entries": entries}

    def get_wallet_performance(self, wallet_address, calc_version=CURRENT_CALC_VERSION):
        norm = normalize_address(wallet_address)
        self.recompute_markouts(norm, calc_version=calc_version)
        prof = self.get_wallet_profile(norm)
        markouts_data = self.get_wallet_markouts(norm, calc_version=calc_version)
        
        by_side = {}
        for side in ("buy", "sell"):
            side_rows = [r for r in self.db.execute(
                "SELECT * FROM trade_markouts WHERE wallet_address = ? AND side = ? AND calc_version = ? AND status = 'available'",
                (norm, side, calc_version)
            )]
            vals = [r["markout"] for r in side_rows if r["markout"] is not None]
            bps_vals = [r["markout_bps"] for r in side_rows if r["markout_bps"] is not None]
            by_side[side] = {
                "trade_count": len(side_rows),
                "avg_markout": round(sum(vals)/len(vals), 6) if vals else None,
                "avg_markout_bps": round(sum(bps_vals)/len(bps_vals), 2) if bps_vals else None,
                "positive_percentage": round(100.0 * sum(1 for v in vals if v > 0)/len(vals), 1) if vals else None
            }
            
        return {
            "wallet": norm,
            "profile": prof,
            "horizons": markouts_data["summary"],
            "by_side": by_side
        }

    def get_wallet_rankings(self, horizon=30, min_trades=5, calc_version=CURRENT_CALC_VERSION, limit=50):
        self.recompute_markouts(calc_version=calc_version)
        wallets_list = self.wallets()
        rankings = []
        
        for w in wallets_list:
            addr = w["wallet"]
            m_data = self.get_wallet_markouts(addr, horizon=horizon, calc_version=calc_version)
            summary = m_data["summary"][0] if m_data["summary"] else {}
            
            avail_count = summary.get("available_count", 0)
            trade_count = w["trade_count"]
            status_counts = summary.get("status_counts", {})
            # Minimum sample-size threshold applied strictly to available observations for this specific horizon
            qualified = avail_count >= min_trades
            
            rankings.append({
                "wallet": addr,
                "total_trades": trade_count,
                "trade_count": trade_count,
                "available_count": avail_count,
                "unavailable_count": status_counts.get("unavailable", 0),
                "stale_count": status_counts.get("stale", 0),
                "crossed_count": status_counts.get("crossed", 0),
                "horizon_seconds": horizon,
                "avg_markout": summary.get("average_markout"),
                "avg_markout_bps": summary.get("average_markout_bps"),
                "positive_percentage": summary.get("positive_percentage"),
                "qualified": qualified,
                "sample_status": "qualified" if qualified else "insufficient_sample",
                "maker_count": w.get("maker_count", 0),
                "taker_count": w.get("taker_count", 0)
            })
            
        rankings.sort(key=lambda r: (1 if r["qualified"] else 0, r["avg_markout_bps"] if r["avg_markout_bps"] is not None else -999999.0), reverse=True)
        return rankings[:limit]


def _clickhouse_rows(table, batch):
    if table == "events":
        return [{
            "event_id": r["event_id"], "venue": r["venue"], "market_id": r["market_id"],
            "event_type": r["event_type"], "exchange_timestamp": r["exchange_timestamp"],
            "received_timestamp": r["received_at"], "sequence_number": r["source_sequence"],
            "price": r["price"], "quantity": r["quantity"], "side": r["side"],
            "source_id": r["source_id"], "raw_payload": r["payload"],
            "event_time": r["event_time"], "received_at": r["received_at"],
            "sequence": r["sequence"], "payload": r["payload"]} for r in batch]
    if table == "book_observations":
        result = []
        for row in batch:
            view = json.loads(row["payload"])
            result.append({"observation_id": row["id"], "venue": row["venue"],
                "market_id": row["market_id"], "snapshot_time": datetime.fromtimestamp(row["recorded_at"], timezone.utc).isoformat(timespec="milliseconds"),
                "best_bid": view["best_bid"], "best_ask": view["best_ask"],
                "bid_depth": sum(q for _,q in view["bids"]),
                "ask_depth": sum(q for _,q in view["asks"]), "sequence": row["sequence"],
                "valid": int(view.get("valid", True))})
        return result
    return [{key: value for key,value in row.items() if key != "exported"} for row in batch]


TARGET_TABLES = ("market_events", "trades", "orderbook_snapshots", "wallet_trades", "trade_markouts", "feed_health", "ingestion_errors")


async def export_live_forever(journal, url, status, client=None, sleep=asyncio.sleep):
    """
    Persistent immutable outbox batch exporter with ClickHouse deduplication token.
    Each batch is created once in SQLite and never re-chunked.
    """
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=10)
    attempt = 0
    try:
        while True:
            did_work = False
            for target in TARGET_TABLES:
                batch = journal.get_or_create_pending_batch(target)
                if not batch:
                    continue

                if journal.check_dedup_window_risk(batch):
                    status["dedup_window_risk"] = True
                    status["quarantined_batches"] = len(journal.quarantined_batches())
                    continue

                did_work = True
                started = time.perf_counter()
                try:
                    response = await client.post(
                        url,
                        params={
                            "query": f"INSERT INTO polypulse.{target} SETTINGS insert_deduplicate=1, deduplicate_blocks_in_dependent_materialized_views=1, insert_deduplication_token='{batch['dedup_token']}' FORMAT JSONEachRow",
                            "date_time_input_format": "best_effort"
                        },
                        content=batch["payload"]
                    )
                    response.raise_for_status()
                except (httpx.HTTPError, OSError) as exc:
                    attempt = min(attempt + 1, 5)
                    journal.record_batch_attempt(batch["batch_id"], error=str(exc))
                    status["clickhouse"] = "degraded" if attempt == 5 else "retrying"
                    status["retry_count"] = status.get("retry_count", 0) + 1
                    status["last_error"] = str(exc)
                    LOG.warning("ClickHouse insert retry: table=%s batch=%s attempt=%d error=%s",
                                target, batch["batch_id"], batch["attempts"] + 1, type(exc).__name__)
                    await sleep(min(30, 2 ** min(attempt - 1, 5)))
                    break

                status["insert_latency_ms"] = round((time.perf_counter() - started) * 1000, 3)
                journal.acknowledge_batch(batch["batch_id"])
                status["clickhouse"] = "connected"
                attempt = 0

            if not did_work:
                await sleep(1)
    finally:
        if owns_client:
            await client.aclose()

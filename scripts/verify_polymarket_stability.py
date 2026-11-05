"""Timed public-feed probe. Writes measurements only; never writes credentials."""
import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv

from backend.durable import DurableJournal, export_live_forever
from backend.live import polymarket
from backend.state import MarketState


async def run(args):
    load_dotenv()
    journal = DurableJournal(args.database)
    state = MarketState(journal, mode="live")
    ids = [value.strip() for value in (args.asset_ids or os.getenv("POLYMARKET_ASSET_IDS", "")).split(",") if value.strip()]
    started = time.monotonic()
    records = []
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    export_status = {}
    url = None
    if args.with_clickhouse:
        url = os.getenv("CLICKHOUSE_URL")
        if not url:
            password = os.getenv("CLICKHOUSE_PASSWORD")
            if not password:
                raise RuntimeError("CLICKHOUSE_URL or CLICKHOUSE_PASSWORD is required")
            url = "http://polypulse:" + quote(password, safe="") + "@localhost:8123"
    task = asyncio.create_task(polymarket(state, ids))
    exporter = asyncio.create_task(export_live_forever(journal, url, export_status)) if url else None
    try:
        with output.open("w", encoding="utf-8") as handle:
            while time.monotonic() - started < args.duration_seconds:
                remaining = args.duration_seconds - (time.monotonic() - started)
                await asyncio.sleep(min(args.interval_seconds, max(0, remaining)))
                valid = sum(bool(state.get(*key)["valid"]) for key in state.books)
                record = {"time_utc": datetime.now(timezone.utc).isoformat(),
                    "elapsed_seconds": round(time.monotonic() - started, 2),
                    "status": state.feed_status.get("polymarket"),
                    "market_count": len(state.books), "valid_books": valid,
                    "events": state.counters["events"],
                    "reconnects": state.counters.get("reconnects", 0),
                    "recoveries": state.counters["recoveries"],
                    "duplicates": state.counters["duplicates"],
                    "reconciliations_matched": state.counters.get("reconciliations_matched", 0),
                    "reconciliations_diverged": state.counters.get("reconciliations_diverged", 0),
                    "oldest_unexported_age_seconds": round(journal.ingestion_lag_seconds(), 2)}
                records.append(record)
                handle.write(json.dumps(record, separators=(",", ":")) + "\n")
                handle.flush()
    finally:
        task.cancel()
        if exporter:
            exporter.cancel()
        await asyncio.gather(*([task, exporter] if exporter else [task]), return_exceptions=True)
        for book in state.books.values():
            book.close()
        journal.db.close()
    summary = {"duration_seconds": round(time.monotonic() - started, 2),
        "samples": len(records), "connected_samples": sum(row["status"] == "connected" for row in records),
        "max_events": max((row["events"] for row in records), default=0),
        "reconnects": state.counters.get("reconnects", 0),
        "recoveries": state.counters["recoveries"],
        "max_valid_books": max((row["valid_books"] for row in records), default=0),
        "reconciliations_matched": state.counters.get("reconciliations_matched", 0),
        "reconciliations_diverged": state.counters.get("reconciliations_diverged", 0),
        "clickhouse_export_enabled": args.with_clickhouse,
        "clickhouse_status": export_status.get("clickhouse", "disabled"),
        "samples_path": str(output)}
    print(json.dumps(summary, separators=(",", ":")), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration-seconds", type=int, default=1800)
    parser.add_argument("--interval-seconds", type=int, default=5)
    parser.add_argument("--database", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--asset-ids", type=str, default="", help="Comma-separated asset IDs to probe")
    parser.add_argument("--with-clickhouse", action="store_true")
    asyncio.run(run(parser.parse_args()))

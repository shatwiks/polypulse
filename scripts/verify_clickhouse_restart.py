"""Stop/start the local PolyPulse ClickHouse container and verify journal replay."""
import asyncio
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import time
from urllib.parse import quote
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
from dotenv import load_dotenv

from backend.durable import DurableJournal, export_live_forever
from backend.events import now_utc, parse_polymarket
from backend.state import MarketState


import shutil

def docker(*args):
    executable = shutil.which("docker") or (Path.home() / "AppData/Local/Programs/DockerDesktop/resources/bin/docker.exe")
    subprocess.run([str(executable), *args], check=True, capture_output=True, timeout=45)


async def flush(journal, url, timeout):
    status = {}
    task = asyncio.create_task(export_live_forever(journal, url, status))
    try:
        async def drained():
            while any(journal.pending_rows(table) for table in
                      ("events", "book_observations", "feed_health", "ingestion_errors")):
                await asyncio.sleep(.2)
        await asyncio.wait_for(drained(), timeout)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    return status


async def main():
    load_dotenv()
    password = os.getenv("CLICKHOUSE_PASSWORD")
    if not password:
        raise RuntimeError("CLICKHOUSE_PASSWORD is not configured")
    url = "http://polypulse:" + quote(password, safe="") + "@localhost:8123"
    container = "polypulse-clickhouse-1"
    market = "restart-check-" + uuid.uuid4().hex
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
        journal = DurableJournal(Path(folder) / "journal.db")
        state = MarketState(journal, mode="live")
        event = parse_polymarket({"event_type":"book", "asset_id":market,
            "timestamp":str(int(time.time()*1000)), "hash":"restart-check",
            "bids":[{"price":"0.49","size":"2"}],
            "asks":[{"price":"0.51","size":"3"}]}, now_utc())[0]
        state.process_live_event(event)
        stopped = False
        try:
            stopped = True
            docker("stop", container)
            offline_started = time.monotonic()
            status = {}
            task = asyncio.create_task(export_live_forever(journal, url, status))
            try:
                for _ in range(30):
                    if status.get("retry_count", 0):
                        break
                    await asyncio.sleep(.5)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            pending_during_outage = journal.stats()["pending_export"]
            retries_during_outage = status.get("retry_count", 0)
            if pending_during_outage < 1 or retries_during_outage < 1:
                raise AssertionError("expected pending rows and an exporter retry while offline")
        except BaseException:
            for book in state.books.values():
                book.close()
            journal.db.close()
            raise
        finally:
            if stopped:
                docker("start", container)
        async with httpx.AsyncClient(timeout=3) as client:
            for _ in range(40):
                try:
                    response = await client.post(url, params={"query":"SELECT 1"})
                    response.raise_for_status()
                    if response.text.strip() == "1":
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(1)
            else:
                raise RuntimeError("ClickHouse did not recover in 40 seconds")
        outage_seconds = round(time.monotonic() - offline_started, 2)
        status = await flush(journal, url, 30)
        quoted = market.replace("'", "''")
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(url, params={"query":
                f"SELECT uniqExact(event_id) FROM polypulse.market_events WHERE market_id='{quoted}'"})
            response.raise_for_status()
            logical_count = int(response.text.strip())
        if logical_count != 1 or status.get("clickhouse") != "connected":
            raise AssertionError("journal replay after restart failed")
        print(f"clickhouse_restart=passed outage_seconds={outage_seconds} "
              f"pending_during_outage={pending_during_outage} retries={retries_during_outage} "
              f"pending_after={journal.stats()['pending_export']} logical_count={logical_count}")
        for book in state.books.values():
            book.close()
        journal.db.close()


if __name__ == "__main__":
    asyncio.run(main())

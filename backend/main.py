"""PolyPulse API. Run with python -m uvicorn backend.main:app --reload."""
import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from backend.connectors import demo, kalshi, polymarket
from backend.state import MarketState
from backend.storage import Journal, export_forever

logging.basicConfig(level=logging.INFO)
LOG = logging.getLogger(__name__)


def settings():
    mode = os.getenv("POLYPULSE_MODE", "demo")
    if mode not in ("demo", "live"):
        raise ValueError("POLYPULSE_MODE must be demo or live")
    pairs = json.loads(os.getenv("MARKET_PAIRS_JSON", "[]"))
    return {"mode": mode, "pairs": pairs,
            "poly": [x for x in os.getenv("POLYMARKET_ASSET_IDS", "").split(",") if x],
            "kalshi": [x for x in os.getenv("KALSHI_MARKET_TICKERS", "").split(",") if x],
            "kalshi_key": os.getenv("KALSHI_API_KEY_ID", ""),
            "kalshi_private_key": os.getenv("KALSHI_PRIVATE_KEY_PATH", ""),
            "clickhouse": os.getenv("CLICKHOUSE_URL", "")}


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = settings()
    journal = Journal(os.getenv("POLYPULSE_DB", "polypulse.db"))
    state = MarketState(journal, config["pairs"])
    app.state.market = state
    app.state.config = config
    app.state.storage_status = {"clickhouse": "disabled"}
    tasks = []
    if config["mode"] == "demo":
        tasks.append(asyncio.create_task(demo(state)))
    else:
        if config["poly"]:
            tasks.append(asyncio.create_task(polymarket(state, config["poly"])))
        if config["kalshi"] and config["kalshi_key"] and config["kalshi_private_key"]:
            tasks.append(asyncio.create_task(kalshi(state, config["kalshi"], config["kalshi_key"], config["kalshi_private_key"])))
    if config["clickhouse"]:
        app.state.storage_status["clickhouse"] = "connecting"
        tasks.append(asyncio.create_task(export_forever(journal, config["clickhouse"], app.state.storage_status)))
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for book in state.books.values():
            book.close()
        journal.db.close()


app = FastAPI(title="PolyPulse API", version="0.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
                   allow_methods=["GET", "POST"], allow_headers=["*"])


def state():
    return app.state.market


@app.get("/api/v1/markets")
async def markets():
    return {"mode": app.state.config["mode"], "markets": state().markets()}


@app.get("/api/v1/markets/{market_id}/orderbook")
async def orderbook(market_id: str, venue: str = Query(..., pattern="^(polymarket|kalshi)$")):
    result = state().get(venue, market_id)
    if not result:
        raise HTTPException(404, "Market book unavailable")
    return result


@app.get("/api/v1/markets/{market_id}/spread")
async def spread(market_id: str):
    result = next((x for x in state().spreads() if market_id in (x["polymarket"]["market_id"], x["kalshi"]["market_id"])), None)
    if not result:
        raise HTTPException(404, "No verified pair or book available")
    return result


@app.get("/api/v1/spreads")
async def spreads():
    return state().spreads()


@app.get("/api/v1/health")
async def health():
    market = state()
    return {"mode": app.state.config["mode"], "feeds": market.feed_status,
            "storage": app.state.storage_status, "uptime_seconds": round(time.time() - market.started_at),
            "journal": market.journal.stats(), "counters": market.counters}


@app.get("/api/v1/metrics")
async def metrics():
    return await health()


class TradeIn(BaseModel):
    trade_id: str = Field(min_length=1)
    wallet: str = Field(min_length=1)
    venue: str = Field(pattern="^(polymarket|kalshi)$")
    market_id: str = Field(min_length=1)
    side: str = Field(pattern="^(buy|sell)$")
    price: float = Field(ge=0, le=1)
    quantity: float = Field(gt=0)
    executed_at: float = Field(gt=0)


@app.post("/api/v1/trades", status_code=201)
async def add_trade(trade: TradeIn):
    if app.state.config["mode"] != "demo":
        raise HTTPException(403, "Trade import endpoint is disabled in live mode")
    inserted = state().journal.add_trade(trade.model_dump())
    return {"inserted": inserted}


@app.get("/api/v1/trades")
async def trades():
    return state().journal.trades()


@app.get("/api/v1/wallets")
async def wallets():
    return state().journal.wallets()


@app.get("/api/v1/wallets/{address}/markouts")
async def markouts(address: str, horizon: int | None = Query(None), venue: str | None = None, market_id: str | None = None):
    if horizon is not None and horizon not in (5, 30, 60):
        raise HTTPException(422, "horizon must be 5, 30, or 60")
    rows = state().journal.trades(address)
    if venue:
        rows = [x for x in rows if x["venue"] == venue]
    if market_id:
        rows = [x for x in rows if x["market_id"] == market_id]
    horizons = [horizon] if horizon else [5, 30, 60]
    entries = []
    summary = []
    for seconds in horizons:
        values = []
        for trade in rows:
            mid = state().journal.future_mid(trade["venue"], trade["market_id"], trade["executed_at"] + seconds)
            value = (1 if trade["side"] == "buy" else -1) * (mid - trade["price"]) if mid is not None else None
            if value is not None:
                values.append(value)
            entries.append({"trade_id": trade["trade_id"], "horizon_seconds": seconds,
                            "future_mid": mid, "markout": value, "available": value is not None})
        summary.append({"horizon_seconds": seconds, "trade_count": len(rows), "available_count": len(values),
                        "average_markout": sum(values)/len(values) if values else None,
                        "positive_percentage": 100*sum(x>0 for x in values)/len(values) if values else None})
    return {"wallet": address, "summary": summary, "entries": entries}


@app.websocket("/ws/markets")
async def market_socket(ws: WebSocket):
    await ws.accept()
    market = state()
    queue = market.listen()
    try:
        await ws.send_json({"type": "initial", "data": {"markets": market.markets(), "mode": app.state.config["mode"]}})
        while True:
            try:
                message = await asyncio.wait_for(queue.get(), timeout=5)
                await ws.send_json(message)
            except asyncio.TimeoutError:
                await ws.send_json({"type": "health", "data": await health()})
    except WebSocketDisconnect:
        pass
    finally:
        market.unlisten(queue)


@app.websocket("/ws/wallets")
async def wallet_socket(ws: WebSocket):
    await ws.accept()
    try:
        while True:
            await ws.send_json({"type": "wallets", "data": await wallets()})
            await asyncio.sleep(5)
    except WebSocketDisconnect:
        pass

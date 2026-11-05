"""Public Polymarket and authenticated Kalshi orderbook feeds."""
import asyncio
import base64
import hashlib
import json
import logging
import random
import time
import uuid
from pathlib import Path
from collections import deque

import websockets
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from backend.normalizers import kalshi_delta, kalshi_snapshot, poly_messages
from backend.engine import PRICE_SCALE, QUANTITY_SCALE

LOG = logging.getLogger(__name__)
POLY_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
KALSHI_URL = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"


async def reconnect(name, state, connect):
    delay = 1
    while True:
        try:
            state.feed_status[name] = "connecting"
            await connect()
            delay = 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOG.warning("%s feed disconnected: %s", name, exc)
            state.feed_status[name] = "retrying"
            await asyncio.sleep(delay + random.random())
            delay = min(30, delay * 2)


async def polymarket(state, asset_ids):
    async def once():
        # Polymarket does not provide a Kalshi-style sequence on this stream.
        # A fresh connection must begin with a full book before deltas are applied.
        state.reset_venue("polymarket")
        seq = {}
        seen = set()
        recent = deque()
        async with websockets.connect(POLY_URL, ping_interval=None, max_size=4_000_000) as ws:
            await ws.send(json.dumps({"assets_ids": asset_ids, "type": "market"}))
            state.feed_status["polymarket"] = "connected"
            last_ping = time.monotonic()
            last_seen = time.monotonic()
            while True:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=10)
                except asyncio.TimeoutError:
                    if time.monotonic() - last_ping >= 10:
                        await ws.send("PING")
                        last_ping = time.monotonic()
                    # A 30-second silence is handled by three receive timeouts.
                    if time.monotonic() - last_seen > 30:
                        raise TimeoutError("Polymarket feed silence")
                    continue
                if raw == "PONG":
                    continue
                last_seen = time.monotonic()
                digest = hashlib.sha256(raw.encode() if isinstance(raw, str) else raw).digest()
                if digest in seen:
                    state.counters["duplicates"] += 1
                    continue
                seen.add(digest)
                recent.append(digest)
                if len(recent) > 10_000:
                    seen.discard(recent.popleft())
                message = json.loads(raw)
                for asset, op, bids, asks, side, price, size in poly_messages(message):
                    seq[asset] = seq.get(asset, 0) + 1
                    if op == "snapshot":
                        state.snapshot("polymarket", asset, bids, asks, seq[asset], message)
                    elif ("polymarket", asset) in state.books:
                        state.level("polymarket", asset, side, price, size, seq[asset], message)
    await reconnect("polymarket", state, once)


def kalshi_headers(key_id, private_key_path):
    stamp = str(int(time.time() * 1000))
    path = "/trade-api/ws/v2"
    key = serialization.load_pem_private_key(Path(private_key_path).read_bytes(), password=None)
    signature = key.sign((stamp + "GET" + path).encode(),
                         padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
                         hashes.SHA256())
    return {"KALSHI-ACCESS-KEY": key_id, "KALSHI-ACCESS-TIMESTAMP": stamp,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode()}


async def kalshi(state, tickers, key_id, private_key_path):
    async def once():
        state.reset_venue("kalshi")
        # Sequence is per subscription SID, not per market.
        sequences = {}
        quantities = {}
        headers = kalshi_headers(key_id, private_key_path)
        async with websockets.connect(KALSHI_URL, additional_headers=headers, ping_interval=10, ping_timeout=10) as ws:
            await ws.send(json.dumps({"id": 1, "cmd": "subscribe", "params": {"channels": ["orderbook_delta"], "market_tickers": tickers}}))
            state.feed_status["kalshi"] = "connected"
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=30)
                message = json.loads(raw)
                kind = message.get("type")
                if kind == "error":
                    raise RuntimeError(f"Kalshi subscription error: {message.get('msg')}")
                if kind not in ("orderbook_snapshot", "orderbook_delta"):
                    continue
                sid = message["sid"]
                seq = int(message["seq"])
                previous = sequences.get(sid)
                if previous is not None and seq <= previous:
                    state.counters["duplicates"] += 1
                    continue
                if previous is not None and seq > previous + 1:
                    state.counters["gaps"] += 1
                    state.counters["recoveries"] += 1
                    raise RuntimeError(f"Kalshi sequence gap: {previous} -> {seq}; resubscribing")
                if kind == "orderbook_snapshot":
                    market, bids, asks = kalshi_snapshot(message)
                    for key in list(quantities):
                        if key[0] == market:
                            quantities.pop(key)
                    for price, quantity in bids:
                        quantities[(market, "bid", price)] = quantity
                    for price, quantity in asks:
                        quantities[(market, "ask", price)] = quantity
                    state.snapshot("kalshi", market, bids, asks, seq, message)
                else:
                    payload = message["msg"]
                    market = str(payload["market_ticker"])
                    side = "bid" if payload["side"] == "yes" else "ask"
                    from backend.normalizers import units
                    price = units(payload["price_dollars"], price=True)
                    price = price if side == "bid" else 10_000 - price
                    current = quantities.get((market, side, price), 0)
                    market, side, price, quantity = kalshi_delta(message, current)
                    quantities[(market, side, price)] = quantity
                    state.level("kalshi", market, side, price, quantity, seq, message)
                sequences[sid] = seq
    await reconnect("kalshi", state, once)


async def demo(state):
    """A labelled synthetic feed for local development and UI inspection."""
    state.reset_venue("polymarket")
    state.reset_venue("kalshi")
    rng = random.Random(42)
    pairs = [("Election outcome · YES", "demo-poly-election", "demo-kalshi-election", 5000),
             ("Rate decision · YES", "demo-poly-rates", "demo-kalshi-rates", 6350),
             ("Space launch · YES", "demo-poly-space", "demo-kalshi-space", 7200)]
    state.pairs = [{"name": name, "polymarket": p, "kalshi": k} for name,p,k,_ in pairs]
    state.feed_status.update({"polymarket": "demo", "kalshi": "demo"})
    seq = {}
    for _, poly, kalshi_id, mid in pairs:
        for venue, market, offset in [("polymarket", poly, -40), ("kalshi", kalshi_id, 30)]:
            key = (venue, market)
            seq[key] = 1
            center = mid + offset
            bids = [((center-50-i*50)*100, (20+i*8)*QUANTITY_SCALE) for i in range(10)]
            asks = [((center+50+i*50)*100, (18+i*7)*QUANTITY_SCALE) for i in range(10)]
            state.snapshot(venue, market, bids, asks, 1)
    for index, (_, poly, kalshi_id, mid) in enumerate(pairs):
        state.journal.add_trade({"trade_id": f"demo-{uuid.uuid4().hex}",
            "wallet": f"demo-wallet-{index+1:02d}", "venue": "polymarket" if index % 2 == 0 else "kalshi",
            "market_id": poly if index % 2 == 0 else kalshi_id,
            "side": "buy" if index % 2 == 0 else "sell", "price": mid / 10_000,
            "quantity": 10 + index * 5, "executed_at": time.time()})
    ticks = 0
    while True:
        await asyncio.sleep(0.8)
        ticks += 1
        _, poly, kalshi_id, _ = rng.choice(pairs)
        venue, market = rng.choice([("polymarket", poly), ("kalshi", kalshi_id)])
        key = (venue, market)
        view = state.get(venue, market)
        side = rng.choice(["bid", "ask"])
        levels = view["bids"] if side == "bid" else view["asks"]
        price = round(rng.choice(levels)[0] * PRICE_SCALE)
        quantity = rng.randint(8, 100) * QUANTITY_SCALE
        seq[key] += 1
        state.level(venue, market, side, price, quantity, seq[key])
        if ticks % 8 == 0:
            mid_price = (view["best_bid"] + view["best_ask"]) / 2 if view["best_bid"] is not None and view["best_ask"] is not None else None
            if mid_price is not None:
                state.journal.add_trade({"trade_id": f"demo-{uuid.uuid4().hex}",
                    "wallet": f"demo-wallet-{rng.randint(1,3):02d}", "venue": venue,
                    "market_id": market, "side": rng.choice(["buy", "sell"]),
                    "price": mid_price, "quantity": rng.randint(5,25), "executed_at": time.time()})

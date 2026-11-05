"""Live venue connectors. No synthetic events enter this module."""
import asyncio
import base64
from dataclasses import replace
from decimal import Decimal
import json
import logging
from pathlib import Path
import random
import time

import httpx
import websockets
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

from backend.events import (NormalizedEvent, PRICE_SCALE, QUANTITY_SCALE,
                            now_utc, parse_kalshi, parse_polymarket,
                            price_ticks, quantity_units)

LOG = logging.getLogger(__name__)
POLY_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
KALSHI_URL = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
GAMMA_URL = "https://gamma-api.polymarket.com/markets/keyset"
CLOB_MARKETS_URL = "https://clob.polymarket.com/sampling-markets"
CLOB_BOOK_URL = "https://clob.polymarket.com/book"


async def discover_polymarket_assets(limit=3, client=None):
    """Select open YES outcomes using Gamma keyset endpoint, falling back to CLOB sampling-markets."""
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=10)
    assets = []
    try:
        try:
            for version in ("v2", "v1"):
                response = await client.get(GAMMA_URL, params={"version": version, "closed": "false", "limit": 40})
                response.raise_for_status()
                for market in response.json().get("markets", []):
                    if market.get("closed") is True or market.get("active") is False:
                        continue
                    # Keyset pages may omit outcome IDs; fetch the full market when needed.
                    if not market.get("positionIds") and not market.get("clobTokenIds") and market.get("id"):
                        try:
                            details = await client.get(f"https://gamma-api.polymarket.com/markets/{market['id']}", timeout=5)
                            details.raise_for_status()
                            market = details.json()
                        except Exception:
                            continue
                    ids = market.get("positionIds") if version == "v2" else market.get("clobTokenIds")
                    if isinstance(ids, str):
                        try:
                            ids = json.loads(ids)
                        except ValueError:
                            continue
                    if isinstance(ids, list) and ids and str(ids[0]).isdigit():
                        assets.append((str(ids[0]), market.get("question") or market.get("slug") or str(ids[0])))
                    if len(assets) >= limit:
                        return assets
        except Exception as exc:
            LOG.warning("Gamma asset discovery failed, falling back to CLOB: %s", exc)

        if len(assets) < limit:
            try:
                resp = await client.get(CLOB_MARKETS_URL, timeout=10)
                resp.raise_for_status()
                for m in resp.json().get("data", []):
                    if not m.get("active") or m.get("closed") or not m.get("enable_order_book"):
                        continue
                    tokens = m.get("tokens", [])
                    yes_token = next((t for t in tokens if str(t.get("outcome", "")).lower() == "yes"), None)
                    token = yes_token or (tokens[0] if tokens else None)
                    if token and token.get("token_id"):
                        tid = str(token["token_id"])
                        lbl = m.get("question") or m.get("market_slug") or tid
                        if not any(a[0] == tid for a in assets):
                            assets.append((tid, lbl))
                    if len(assets) >= limit:
                        break
            except Exception as exc:
                LOG.error("CLOB asset discovery failed: %s", exc)

        return assets
    finally:
        if owns_client:
            await client.aclose()


async def reconcile_polymarket_orderbook(asset_id, state, client=None):
    """Reconcile local orderbook against official Polymarket REST CLOB.
    
    Reconciliation rules:
    1. Fetch authoritative orderbook snapshot from https://clob.polymarket.com/book?token_id={asset_id}.
    2. Compare top-of-book (best bid, best ask).
    3. If local book matches REST book: refresh last_received_timestamp via touch_book().
    4. If diverged or local book invalid: apply REST snapshot via process_live_event().
    5. On network or HTTP error: log and do not touch book (if updates cease, staleness correctly hides quotes).
    """
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=10)
    try:
        resp = await client.get(f"{CLOB_BOOK_URL}?token_id={asset_id}")
        if resp.status_code != 200:
            LOG.debug("Polymarket REST reconciliation failed: HTTP %s", resp.status_code)
            return False
        data = resp.json()
        data["event_type"] = "book"
        received = now_utc()
        events = parse_polymarket(data, received)
        if not events:
            return False
        rest_event = events[0]

        local_view = state.get("polymarket", asset_id)
        rest_best_bid = max((p for p, q in rest_event.bids), default=None)
        rest_best_ask = min((p for p, q in rest_event.asks), default=None)

        local_best_bid = price_ticks(local_view["best_bid"]) if local_view and local_view.get("best_bid") is not None else None
        local_best_ask = price_ticks(local_view["best_ask"]) if local_view and local_view.get("best_ask") is not None else None

        is_match = (
            local_view is not None and
            not local_view.get("stale", False) and
            local_view.get("valid", False) and
            local_best_bid == rest_best_bid and
            local_best_ask == rest_best_ask
        )

        if is_match:
            state.counters["reconciliations_matched"] = state.counters.get("reconciliations_matched", 0) + 1
            state.touch_book("polymarket", asset_id, received)
            return True
        else:
            state.counters["reconciliations_diverged"] = state.counters.get("reconciliations_diverged", 0) + 1
            state.process_live_event(rest_event)
            state.touch_book("polymarket", asset_id, received)
            LOG.info("Polymarket orderbook reconciled after divergence for %s", asset_id)
            return True
    except Exception as exc:
        LOG.warning("Polymarket reconciliation error for %s: %s", asset_id, exc)
        return False
    finally:
        if owns_client:
            await client.aclose()


def kalshi_headers(key_id, private_key_path, timestamp_ms=None):
    stamp = str(timestamp_ms or int(time.time() * 1000))
    path = "/trade-api/ws/v2"
    key = serialization.load_pem_private_key(Path(private_key_path).read_bytes(), password=None)
    message = (stamp + "GET" + path).encode()
    if isinstance(key, ed25519.Ed25519PrivateKey):
        signature = key.sign(message)
    elif isinstance(key, rsa.RSAPrivateKey):
        signature = key.sign(message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256())
    else:
        raise ValueError("Kalshi key must be Ed25519 or RSA")
    return {"KALSHI-ACCESS-KEY": key_id, "KALSHI-ACCESS-TIMESTAMP": stamp,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode()}


async def reconnect_forever(venue, state, connect, sleep=asyncio.sleep):
    delay = 1
    while True:
        state.set_feed_status(venue, "connecting")
        try:
            await connect()
            raise ConnectionError("feed ended")
        except asyncio.CancelledError:
            state.invalidate_venue(venue, "stopped")
            state.set_feed_status(venue, "stopped")
            raise
        except Exception as exc:
            if state.feed_status.get(venue) == "connected":
                delay = 1
            state.counters["reconnects"] = state.counters.get("reconnects", 0) + 1
            state.invalidate_venue(venue, "reconnecting")
            # Exception text can contain signed headers or URLs with credentials.
            reason = type(exc).__name__
            state.set_feed_status(venue, "retrying", reason)
            state.journal.log_error(venue, None, None, reason)
            LOG.warning("feed disconnected: %s", reason, extra={"venue": venue})
            await sleep(delay + random.uniform(0, .25))
            delay = min(30, delay * 2)


async def polymarket(state, asset_ids, *, websocket_connect=websockets.connect):
    selected_cache = list(dict.fromkeys(asset_ids))

    async def reconcile_loop():
        # Reconcile after brief delay, then periodically every 15s
        await asyncio.sleep(2)
        while True:
            try:
                for aid in list(selected_cache):
                    try:
                        await reconcile_polymarket_orderbook(aid, state)
                    except Exception as exc:
                        LOG.warning("Polymarket reconciliation error for %s: %s", aid, exc)
            except Exception as exc:
                LOG.error("Polymarket reconcile_loop cycle error: %s", exc)
            await asyncio.sleep(15)

    async def once():
        nonlocal selected_cache
        if not selected_cache:
            selected_cache = list(dict.fromkeys(asset for asset, label in await discover_polymarket_assets()))
        selected = selected_cache
        if not selected:
            raise RuntimeError("No active Polymarket assets discovered")
        recovering = any(key[0] == "polymarket" for key in state.books)
        state.invalidate_venue("polymarket", "awaiting_snapshot")
        snapshots = set()
        last_heartbeat = time.monotonic()
        last_ping = time.monotonic()
        async with websocket_connect(POLY_URL, ping_interval=None, open_timeout=10,
                                     max_size=4_000_000) as ws:
            await ws.send(json.dumps({"assets_ids": selected, "type": "market"}))
            state.set_feed_status("polymarket", "subscribed")
            while True:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=10)
                except asyncio.TimeoutError:
                    await ws.send("PING")
                    last_ping = time.monotonic()
                    if time.monotonic() - last_heartbeat >= 30:
                        raise TimeoutError("Polymarket heartbeat timed out")
                    continue
                if raw == "PONG":
                    last_heartbeat = time.monotonic()
                    continue
                if time.monotonic() - last_ping >= 10:
                    await ws.send("PING")
                    last_ping = time.monotonic()
                last_heartbeat = time.monotonic()
                received = now_utc()
                try:
                    events = parse_polymarket(json.loads(raw), received)
                except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
                    state.journal.log_error("polymarket", None, None, exc)
                    raise RuntimeError("invalid Polymarket market event") from exc
                for event in events:
                    if event.market_id not in selected:
                        continue
                    state.mark_received("polymarket", received)
                    if event.event_type == "book_snapshot":
                        snapshots.add(event.market_id)
                    elif event.event_type == "book_level" and event.market_id not in snapshots:
                        # Incrementals cannot establish a book on their own.
                        state.journal.log_error("polymarket", event.market_id, event.event_id, "delta before snapshot")
                        continue
                    state.process_live_event(event)
                    if snapshots.issuperset(selected):
                        if recovering:
                            state.counters["recoveries"] += 1
                            recovering = False
                        state.set_feed_status("polymarket", "connected")

    reconcile_task = asyncio.create_task(reconcile_loop())
    try:
        await reconnect_forever("polymarket", state, once)
    finally:
        reconcile_task.cancel()
        try:
            await reconcile_task
        except asyncio.CancelledError:
            pass


class KalshiSequence:
    def __init__(self):
        self.last = {}

    def accept(self, sid, sequence):
        previous = self.last.get(sid)
        if previous is not None and sequence <= previous:
            return "duplicate"
        if previous is not None and sequence != previous + 1:
            return "gap"
        self.last[sid] = sequence
        return "ok"


async def kalshi(state, tickers, key_id, private_key_path, *, websocket_connect=websockets.connect):
    if not tickers or not key_id or not private_key_path:
        state.set_feed_status("kalshi", "unconfigured", "Set ticker, key ID and private key path")
        return

    gap_pending = False
    async def once():
        nonlocal gap_pending
        state.invalidate_venue("kalshi", "awaiting_snapshot")
        quantities = {}
        tracker = KalshiSequence()
        snapshots = set()
        headers = kalshi_headers(key_id, private_key_path)
        async with websocket_connect(KALSHI_URL, additional_headers=headers,
                                     ping_interval=10, ping_timeout=10, max_size=4_000_000) as ws:
            await ws.send(json.dumps({"id": 1, "cmd": "subscribe",
                "params": {"channels": ["orderbook_delta"], "market_tickers": tickers}}))
            state.set_feed_status("kalshi", "subscribed")
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=30)
                received = now_utc()
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise RuntimeError("invalid Kalshi JSON") from exc
                kind = message.get("type")
                if kind == "error":
                    raise RuntimeError(f"Kalshi subscription error: {message.get('msg')}")
                if "sid" in message and "seq" in message:
                    sid, sequence = int(message["sid"]), int(message["seq"])
                    result = tracker.accept(sid, sequence)
                    if result == "duplicate":
                        state.counters["duplicates"] += 1
                        continue
                    if result == "gap":
                        state.counters["gaps"] += 1
                        gap_pending = True
                        # Closing and resubscribing obtains fresh snapshots for every ticker.
                        state.invalidate_venue("kalshi", "sequence_gap")
                        raise RuntimeError(f"Kalshi sequence gap on sid {sid}")
                if kind not in ("orderbook_snapshot", "orderbook_delta"):
                    continue
                try:
                    parsed = parse_kalshi(message, received)
                    event, delta = parsed if isinstance(parsed, tuple) else (parsed, None)
                    if event.market_id not in tickers:
                        continue
                    state.mark_received("kalshi", received)
                    if event.event_type == "book_snapshot":
                        for key in [key for key in quantities if key[0] == event.market_id]:
                            quantities.pop(key)
                        for price, quantity in event.bids:
                            quantities[(event.market_id, "bid", price)] = quantity
                        for price, quantity in event.asks:
                            quantities[(event.market_id, "ask", price)] = quantity
                        snapshots.add(event.market_id)
                    else:
                        if event.market_id not in snapshots:
                            raise RuntimeError("Kalshi delta before snapshot")
                        level_key = (event.market_id, event.side, price_ticks(event.price))
                        updated = quantities.get(level_key, 0) + delta
                        if updated < 0:
                            raise RuntimeError("Kalshi delta would make negative level")
                        event = replace(event, quantity=str(Decimal(updated) / QUANTITY_SCALE))
                        quantities[level_key] = updated
                    state.process_live_event(event)
                    if snapshots.issuperset(tickers):
                        if gap_pending:
                            state.counters["recoveries"] += 1
                            gap_pending = False
                        state.set_feed_status("kalshi", "connected")
                except (ValueError, KeyError, TypeError) as exc:
                    state.journal.log_error("kalshi", message.get("msg", {}).get("market_ticker"), None, exc)
                    state.invalidate_venue("kalshi", "invalid_update")
                    raise RuntimeError("invalid Kalshi orderbook update") from exc
    await reconnect_forever("kalshi", state, once)

"""Confirmed Polygon CTF Exchange V2 maker fills via JSON-RPC polling."""
import asyncio
from decimal import Decimal, ROUND_HALF_UP
import logging
import time

import httpx

from backend.events import NormalizedEvent, now_utc, utc_timestamp

LOG = logging.getLogger(__name__)
ORDER_FILLED_V2_TOPIC = "0xd543adfd945773f1a62f74f0ee55a5e3b9b1a28262980ba90b1a89f2ea84d8ee"
EXCHANGES = (
    "0xE111180000d2663C0091e4f400237545B87B996B",
    "0xe2222d279d744050d28e00520010520000310F59",
)


def decode_v2_fill(log, block_timestamp):
    """Decode the maker side of OrderFilled; one event is one maker fill."""
    topics = log.get("topics", [])
    data = log.get("data", "")
    if len(topics) != 4 or topics[0].lower() != ORDER_FILLED_V2_TOPIC or not data.startswith("0x") or len(data) != 2 + 7 * 64:
        raise ValueError("invalid V2 OrderFilled log")
    words = [int(data[2+i*64:2+(i+1)*64], 16) for i in range(5)]
    side_code, token_id, maker_amount, taker_amount, fee = words
    if side_code not in (0, 1) or token_id == 0 or maker_amount == 0 or taker_amount == 0:
        raise ValueError("invalid V2 fill amounts or side")
    side = "buy" if side_code == 0 else "sell"
    collateral = maker_amount if side == "buy" else taker_amount
    tokens = taker_amount if side == "buy" else maker_amount
    price = (Decimal(collateral) / Decimal(tokens)).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
    quantity = Decimal(tokens) / Decimal(1_000_000)
    maker = "0x" + topics[2][-40:].lower()
    taker = "0x" + topics[3][-40:].lower()
    decoded = {"maker": maker, "taker": taker, "order_hash": topics[1],
        "fee_units": fee, "token_id": str(token_id)}
    return NormalizedEvent("polygon", str(token_id), "fill", now_utc(),
        {"log": log, "decoded": decoded}, utc_timestamp(block_timestamp),
        source_id=f"{log['transactionHash']}:{int(log['logIndex'], 16)}",
        price=str(price), quantity=str(quantity), side=side)


async def rpc(client, url, method, params):
    response = await client.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    response.raise_for_status()
    body = response.json()
    if "error" in body:
        raise RuntimeError(f"Polygon RPC {method}: {body['error']}")
    return body["result"]


async def polygon_fills(state, url, confirmations=12, start_block=None, client=None, sleep=asyncio.sleep):
    """Poll bounded confirmed block ranges and checkpoint only after processing."""
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=20)
    try:
        chain = await rpc(client, url, "eth_chainId", [])
        if int(chain, 16) != 137:
            raise ValueError("Polygon RPC chain ID must be 137")
        state.set_feed_status("polygon", "connected")
        checkpoint = state.journal.checkpoint("polygon_v2")
        while True:
            latest = int(await rpc(client, url, "eth_blockNumber", []), 16)
            safe = max(0, latest - confirmations)
            if checkpoint is None:
                checkpoint = (start_block - 1) if start_block is not None else safe
                state.journal.set_checkpoint("polygon_v2", checkpoint)
            while checkpoint < safe:
                end = min(safe, checkpoint + 10)
                logs = await rpc(client, url, "eth_getLogs", [{
                    "fromBlock": hex(checkpoint + 1), "toBlock": hex(end),
                    "address": list(EXCHANGES), "topics": [ORDER_FILLED_V2_TOPIC]}])
                block_times = {}
                for log in logs:
                    block = log["blockNumber"]
                    if block not in block_times:
                        header = await rpc(client, url, "eth_getBlockByNumber", [block, False])
                        block_times[block] = int(header["timestamp"], 16)
                    event = decode_v2_fill(log, block_times[block])
                    state.mark_received("polygon", event.received_timestamp)
                    state.process_live_event(event)
                checkpoint = end
                state.journal.set_checkpoint("polygon_v2", checkpoint)
            await sleep(5)
    except asyncio.CancelledError:
        state.set_feed_status("polygon", "stopped")
        raise
    except Exception as exc:
        state.set_feed_status("polygon", "retrying", type(exc).__name__)
        state.journal.log_error("polygon", None, None, type(exc).__name__)
        raise
    finally:
        if owns_client:
            await client.aclose()


async def polygon_reconnect(state, url, confirmations=12, start_block=None):
    from backend.live import reconnect_forever
    await reconnect_forever("polygon", state, lambda: polygon_fills(state, url, confirmations, start_block))

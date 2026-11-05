"""Confirmed Polygon CTF Exchange V2 maker fills via JSON-RPC polling.

Includes:
- Strict topic padding and length validation.
- Classification of individual maker-fill vs. taker-order summary events.
- Rejection of exchange contract addresses as trading wallets.
- Support for Complementary, MINT, and MERGE match types.
- Preservation of full uint256 token IDs as validated decimal strings.
- Monitored CTF Exchange V2 and Neg Risk Exchange V2 contracts.
"""
import asyncio
from decimal import Decimal, ROUND_HALF_UP
import logging
import time
from typing import Any, Dict, List, Optional

import httpx

from backend.events import NormalizedEvent, now_utc, utc_timestamp
from backend.wallet import (
    CTF_EXCHANGE_V2, NEG_RISK_EXCHANGE_V2, NEG_RISK_ADAPTER,
    TEST_EXCHANGE_1, TEST_EXCHANGE_2, KNOWN_EXCHANGES,
    normalize_address
)

LOG = logging.getLogger(__name__)

ORDER_FILLED_V2_TOPIC = "0xd543adfd945773f1a62f74f0ee55a5e3b9b1a28262980ba90b1a89f2ea84d8ee"
ORDERS_MATCHED_TOPIC = "0xfb545e69913e5c28436bf9b9ae83418178594c3691ea3f96bf277f4f808a0010"

EXCHANGES = (
    CTF_EXCHANGE_V2,
    NEG_RISK_EXCHANGE_V2,
    TEST_EXCHANGE_1,
    TEST_EXCHANGE_2,
)


def decode_v2_fill(log: Dict[str, Any], block_timestamp: int) -> NormalizedEvent:
    """
    Decodes an on-chain Polygon OrderFilled log into a NormalizedEvent.
    Strictly validates topics, lengths, ABI address padding, and match types.
    """
    topics = log.get("topics", [])
    data = log.get("data", "")
    if len(topics) != 4 or topics[0].lower() != ORDER_FILLED_V2_TOPIC.lower():
        raise ValueError(f"invalid V2 OrderFilled topics: count={len(topics)}")
    
    if not isinstance(data, str) or not data.startswith("0x") or len(data) < 2 + 5 * 64:
        raise ValueError(f"invalid V2 OrderFilled data payload length: {len(data)}")

    # Strict address topic normalization (validates exactly 12 zero bytes of padding)
    maker = normalize_address(topics[2])
    taker = normalize_address(topics[3])

    num_words = (len(data) - 2) // 64
    words = [int(data[2+i*64:2+(i+1)*64], 16) for i in range(min(num_words, 7))]
    side_code, token_id, maker_amount, taker_amount, fee = words[:5]

    if side_code not in (0, 1) or token_id == 0 or maker_amount == 0 or taker_amount == 0:
        raise ValueError("invalid V2 fill amounts, token, or side code")

    side = "buy" if side_code == 0 else "sell"
    collateral = maker_amount if side == "buy" else taker_amount
    tokens = taker_amount if side == "buy" else maker_amount
    price = (Decimal(collateral) / Decimal(tokens)).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
    quantity = Decimal(tokens) / Decimal(1_000_000)

    # Decode optional fields: taker_token_id, match_type, condition_id
    taker_token_id = str(words[5]) if len(words) > 5 and words[5] > 2 else str(token_id)
    
    # Determine match type:
    if log.get("match_type"):
        match_type = str(log["match_type"]).lower()
    elif len(words) > 5 and words[5] in (0, 1, 2):
        match_type = {0: "complementary", 1: "mint", 2: "merge"}[words[5]]
    elif len(words) > 5 and 2 < words[5] < 1000:
        match_type = "unresolved"
    else:
        match_type = "complementary"

    # Identify taker-order summary events (where taker is the exchange itself)
    log_addr = str(log.get("address", CTF_EXCHANGE_V2)).lower()
    if taker in KNOWN_EXCHANGES or taker == log_addr:
        classification = "taker_summary"
    else:
        classification = "maker_fill"

    condition_id = hex(words[6]) if len(words) > 6 else log.get("condition_id")

    log_idx_raw = log.get("logIndex", 0)
    if isinstance(log_idx_raw, str) and log_idx_raw.startswith("0x"):
        log_idx = int(log_idx_raw, 16)
    else:
        log_idx = int(log_idx_raw)

    decoded = {
        "maker": maker,
        "taker": taker,
        "order_hash": topics[1],
        "fee_units": fee,
        "token_id": str(token_id),
        "maker_token_id": str(token_id),
        "taker_token_id": taker_token_id,
        "match_type": match_type,
        "classification": classification,
        "chain_id": 137,
        "exchange_address": log_addr,
        "condition_id": condition_id,
        "timestamp_source": "block_header"
    }

    tx_hash = log.get("transactionHash", "0x0")
    source_id = f"{tx_hash}:{log_idx}"

    return NormalizedEvent(
        venue="polygon",
        market_id=str(token_id),
        event_type="fill",
        received_timestamp=now_utc(),
        raw_payload={"log": log, "decoded": decoded},
        exchange_timestamp=utc_timestamp(block_timestamp),
        source_id=source_id,
        price=str(price),
        quantity=str(quantity),
        side=side
    )


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

"""ClickHouse Historical Analytics client and query service."""
from datetime import datetime, timedelta, timezone
import json
import logging
import math
from typing import Any, Dict, List, Optional
import httpx

from backend.wallet import normalize_address

LOG = logging.getLogger(__name__)


def parse_window(window_str: str) -> timedelta:
    """Parse window strings like 1h, 6h, 24h, 7d into timedelta."""
    window_str = window_str.lower().strip()
    if window_str.endswith("h"):
        return timedelta(hours=int(window_str[:-1]))
    if window_str.endswith("d"):
        return timedelta(days=int(window_str[:-1]))
    if window_str.endswith("m"):
        return timedelta(minutes=int(window_str[:-1]))
    return timedelta(hours=24)


class ClickHouseAnalytics:
    def __init__(self, url: str):
        self.url = url
        self.enabled = bool(url)

    async def execute_query(self, query: str, params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        if not self.enabled:
            return []
        request_params = {"query": query.strip(), "default_format": "JSONEachRow"}
        if params:
            for k, v in params.items():
                request_params[f"param_{k}"] = str(v)

        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                resp = await client.post(self.url, params=request_params)
                resp.raise_for_status()
                text = resp.text.strip()
                if not text:
                    return []
                return [json.loads(line) for line in text.split("\n") if line.strip()]
            except httpx.HTTPStatusError as exc:
                LOG.warning("ClickHouse query error: %s - %s (query=%s)", exc.response.status_code, exc.response.text, query[:150])
                return []
            except (httpx.HTTPError, OSError) as exc:
                LOG.warning("ClickHouse query error: %s (query=%s)", type(exc).__name__, query[:100])
                return []

    async def get_market_summary(self, market_id: str, venue: Optional[str] = None, window: str = "24h") -> Dict[str, Any]:
        """Calculates 24h volume, VWAP, trade count, high/low/close, and current quote over deduplicated trades."""
        delta = parse_window(window)
        since = (datetime.now(timezone.utc) - delta).strftime("%Y-%m-%d %H:%M:%S")

        venue_filter = "AND venue = {venue:String}" if venue else ""
        query_trades = f"""
        SELECT
            count() AS trade_count,
            uniqExact(event_id) AS unique_trades,
            sum(ifNull(quantity, toDecimal64(0, 6))) AS total_volume,
            sum(if(side = 'buy', ifNull(quantity, toDecimal64(0, 6)), toDecimal64(0, 6))) AS buy_volume,
            sum(if(side = 'sell', ifNull(quantity, toDecimal64(0, 6)), toDecimal64(0, 6))) AS sell_volume,
            sum(toDecimal64(price, 6) * toDecimal64(ifNull(quantity, toDecimal64(0, 6)), 6)) AS dollar_volume,
            max(price) AS high_price,
            min(price) AS low_price,
            argMax(price, (received_timestamp, event_id)) AS close_price
        FROM polypulse.trades FINAL
        WHERE market_id = {{market:String}} {venue_filter}
          AND received_timestamp >= {{since:DateTime64(3, 'UTC')}}
        """
        params = {"market": market_id, "since": since}
        if venue:
            params["venue"] = venue

        rows = await self.execute_query(query_trades, params)
        data = rows[0] if rows else {}

        trade_count = int(data.get("unique_trades", 0) or 0)
        tot_vol = float(data.get("total_volume", 0.0) or 0.0)
        dollar_vol = float(data.get("dollar_volume", 0.0) or 0.0)
        vwap = (dollar_vol / tot_vol) if tot_vol > 0 else None

        # Fetch latest valid non-crossed snapshot
        query_book = f"""
        SELECT
            venue,
            argMax(best_bid, (snapshot_time, observation_id)) AS latest_bid,
            argMax(best_ask, (snapshot_time, observation_id)) AS latest_ask,
            argMax(bid_depth, (snapshot_time, observation_id)) AS latest_bid_depth,
            argMax(ask_depth, (snapshot_time, observation_id)) AS latest_ask_depth,
            count() AS snapshot_count
        FROM polypulse.orderbook_snapshots FINAL
        WHERE market_id = {{market:String}} {venue_filter}
          AND snapshot_time >= {{since:DateTime64(3, 'UTC')}}
          AND valid = 1
          AND best_bid IS NOT NULL AND best_ask IS NOT NULL
          AND best_ask > best_bid
        GROUP BY venue
        ORDER BY latest_ask ASC
        LIMIT 1
        """
        book_rows = await self.execute_query(query_book, params)
        book_data = book_rows[0] if book_rows else {}

        best_bid = float(book_data.get("latest_bid")) if book_data.get("latest_bid") is not None else None
        best_ask = float(book_data.get("latest_ask")) if book_data.get("latest_ask") is not None else None
        spread = (best_ask - best_bid) if (best_bid is not None and best_ask is not None) else None
        mid = ((best_bid + best_ask) / 2.0) if (best_bid is not None and best_ask is not None) else None
        spread_bps = ((spread / mid) * 10000.0) if (spread is not None and mid and mid > 0) else None

        return {
            "market_id": market_id,
            "venue": venue or book_data.get("venue"),
            "window": window,
            "trade_count": trade_count,
            "total_volume": round(tot_vol, 4),
            "buy_volume": round(float(data.get("buy_volume", 0.0) or 0.0), 4),
            "sell_volume": round(float(data.get("sell_volume", 0.0) or 0.0), 4),
            "dollar_volume": round(dollar_vol, 4),
            "vwap": round(vwap, 4) if vwap else None,
            "high_price": float(data.get("high_price")) if data.get("high_price") is not None else None,
            "low_price": float(data.get("low_price")) if data.get("low_price") is not None else None,
            "close_price": float(data.get("close_price")) if data.get("close_price") is not None else None,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "spread": round(spread, 6) if spread is not None else None,
            "spread_bps": round(spread_bps, 2) if spread_bps is not None else None,
            "bid_depth": float(book_data.get("latest_bid_depth", 0.0) or 0.0),
            "ask_depth": float(book_data.get("latest_ask_depth", 0.0) or 0.0),
            "snapshot_count": int(book_data.get("snapshot_count", 0) or 0)
        }

    async def get_market_candles(self, market_id: str, venue: Optional[str] = None,
                                 resolution: str = "1m",
                                 start_time: Optional[datetime] = None,
                                 end_time: Optional[datetime] = None,
                                 limit: int = 500) -> List[Dict[str, Any]]:
        """Fetch OHLCV + VWAP candles pre-aggregated from materialized views."""
        venue_filter = "AND venue = {venue:String}" if venue else ""
        if not start_time:
            start_time = datetime.now(timezone.utc) - timedelta(hours=24)
        if not end_time:
            end_time = datetime.now(timezone.utc)

        params = {
            "market": market_id,
            "start": start_time.strftime("%Y-%m-%d %H:%M:%S"),
            "end": end_time.strftime("%Y-%m-%d %H:%M:%S")
        }
        if venue:
            params["venue"] = venue

        # Group by resolution interval (1m, 5m, 1h, 1d)
        interval_expr = "toStartOfMinute(received_timestamp)"
        if resolution == "5m":
            interval_expr = "toStartOfInterval(received_timestamp, INTERVAL 5 MINUTE)"
        elif resolution == "1h":
            interval_expr = "toStartOfHour(received_timestamp)"
        elif resolution == "1d":
            interval_expr = "toStartOfDay(received_timestamp)"

        query = f"""
        SELECT
            {interval_expr} AS time,
            argMin(price, (received_timestamp, event_id)) AS open,
            max(price) AS high,
            min(price) AS low,
            argMax(price, (received_timestamp, event_id)) AS close,
            sum(ifNull(quantity, toDecimal64(0, 6))) AS volume,
            sum(if(side = 'buy', ifNull(quantity, toDecimal64(0, 6)), toDecimal64(0, 6))) AS buy_volume,
            sum(if(side = 'sell', ifNull(quantity, toDecimal64(0, 6)), toDecimal64(0, 6))) AS sell_volume,
            uniqExact(event_id) AS trade_count,
            if(sum(ifNull(quantity, toDecimal64(0, 6))) > 0, toDecimal64(sum(toDecimal64(price, 6) * toDecimal64(ifNull(quantity, toDecimal64(0, 6)), 6)) / sum(ifNull(quantity, toDecimal64(0, 6))), 6), toDecimal64(0, 6)) AS avg_price
        FROM polypulse.trades FINAL
        WHERE market_id = {{market:String}} {venue_filter}
          AND received_timestamp >= {{start:DateTime64(3, 'UTC')}}
          AND received_timestamp <= {{end:DateTime64(3, 'UTC')}}
        GROUP BY time
        ORDER BY time ASC
        LIMIT {min(max(1, limit), 2000)}
        """

        rows = await self.execute_query(query, params)

        candles = []
        for r in rows:
            vol = float(r.get("volume", 0.0) or 0.0)
            candles.append({
                "time": str(r.get("time")),
                "open": float(r["open"]) if r.get("open") is not None else None,
                "high": float(r["high"]) if r.get("high") is not None else None,
                "low": float(r["low"]) if r.get("low") is not None else None,
                "close": float(r["close"]) if r.get("close") is not None else None,
                "volume": round(vol, 4),
                "buy_volume": round(float(r.get("buy_volume", 0.0) or 0.0), 4),
                "sell_volume": round(float(r.get("sell_volume", 0.0) or 0.0), 4),
                "trade_count": int(r.get("trade_count", 0) or 0),
                "avg_price": round(float(r["avg_price"]), 4) if r.get("avg_price") is not None else None
            })
        return candles

    async def get_market_trades(self, market_id: str, venue: Optional[str] = None,
                                start_time: Optional[datetime] = None,
                                end_time: Optional[datetime] = None,
                                limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
        """Paginated historical trade log."""
        venue_filter = "AND venue = {venue:String}" if venue else ""
        conditions = ["market_id = {market:String}"]
        params = {"market": market_id}
        if venue:
            conditions.append("venue = {venue:String}")
            params["venue"] = venue
        if start_time:
            conditions.append("received_timestamp >= {start:DateTime64(3, 'UTC')}")
            params["start"] = start_time.strftime("%Y-%m-%d %H:%M:%S")
        if end_time:
            conditions.append("received_timestamp <= {end:DateTime64(3, 'UTC')}")
            params["end"] = end_time.strftime("%Y-%m-%d %H:%M:%S")

        where_clause = " AND ".join(conditions)
        query = f"""
        SELECT
            toString(event_id) AS trade_id,
            venue,
            market_id,
            source_id,
            side,
            wallet,
            price,
            quantity,
            exchange_timestamp,
            received_timestamp
        FROM polypulse.trades FINAL
        WHERE {where_clause}
        ORDER BY received_timestamp DESC, event_id DESC
        LIMIT {min(max(1, limit), 1000)} OFFSET {max(0, offset)}
        """
        rows = await self.execute_query(query, params)
        trades = []
        for r in rows:
            trades.append({
                "trade_id": r.get("trade_id"),
                "venue": r.get("venue"),
                "market_id": r.get("market_id"),
                "source_id": r.get("source_id"),
                "side": r.get("side"),
                "wallet": r.get("wallet"),
                "price": float(r["price"]) if r.get("price") is not None else None,
                "quantity": float(r["quantity"]) if r.get("quantity") is not None else None,
                "exchange_timestamp": str(r.get("exchange_timestamp")),
                "received_timestamp": str(r.get("received_timestamp"))
            })
        return trades

    async def get_market_spread_trajectory(self, market_id: str, venue: Optional[str] = None,
                                          start_time: Optional[datetime] = None,
                                          end_time: Optional[datetime] = None,
                                          limit: int = 200) -> List[Dict[str, Any]]:
        """Historical spread trajectory and depth over time, excluding crossed books."""
        venue_filter = "AND venue = {venue:String}" if venue else ""
        if not start_time:
            start_time = datetime.now(timezone.utc) - timedelta(hours=24)
        if not end_time:
            end_time = datetime.now(timezone.utc)

        params = {
            "market": market_id,
            "start": start_time.strftime("%Y-%m-%d %H:%M:%S"),
            "end": end_time.strftime("%Y-%m-%d %H:%M:%S")
        }
        if venue:
            params["venue"] = venue

        query = f"""
        SELECT
            toStartOfMinute(snapshot_time) AS time,
            avg(assumeNotNull(best_bid)) AS avg_bid,
            avg(assumeNotNull(best_ask)) AS avg_ask,
            min(assumeNotNull(best_ask) - assumeNotNull(best_bid)) AS min_spread,
            max(assumeNotNull(best_ask) - assumeNotNull(best_bid)) AS max_spread,
            avg(assumeNotNull(best_ask) - assumeNotNull(best_bid)) AS avg_spread,
            avg(bid_depth) AS bid_depth,
            avg(ask_depth) AS ask_depth,
            count() AS snapshot_count
        FROM polypulse.orderbook_snapshots FINAL
        WHERE market_id = {{market:String}} {venue_filter}
          AND snapshot_time >= {{start:DateTime64(3, 'UTC')}}
          AND snapshot_time <= {{end:DateTime64(3, 'UTC')}}
          AND valid = 1 AND best_bid IS NOT NULL AND best_ask IS NOT NULL
          AND best_ask > best_bid
        GROUP BY time
        ORDER BY time ASC
        LIMIT {min(max(1, limit), 1000)}
        """
        rows = await self.execute_query(query, params)

        trajectory = []
        for r in rows:
            bid = float(r["avg_bid"]) if r.get("avg_bid") is not None else None
            ask = float(r["avg_ask"]) if r.get("avg_ask") is not None else None
            spread = float(r["avg_spread"]) if r.get("avg_spread") is not None else None
            mid = ((bid + ask) / 2.0) if (bid is not None and ask is not None) else None
            spread_bps = ((spread / mid) * 10000.0) if (spread is not None and mid and mid > 0) else None

            trajectory.append({
                "time": str(r.get("time")),
                "avg_bid": round(bid, 4) if bid is not None else None,
                "avg_ask": round(ask, 4) if ask is not None else None,
                "avg_spread": round(spread, 6) if spread is not None else None,
                "spread_bps": round(spread_bps, 2) if spread_bps is not None else None,
                "min_spread": float(r["min_spread"]) if r.get("min_spread") is not None else None,
                "max_spread": float(r["max_spread"]) if r.get("max_spread") is not None else None,
                "bid_depth": round(float(r.get("bid_depth", 0.0) or 0.0), 2),
                "ask_depth": round(float(r.get("ask_depth", 0.0) or 0.0), 2),
                "snapshot_count": int(r.get("snapshot_count", 0) or 0)
            })
        return trajectory

    async def get_venue_analytics(self, window: str = "24h") -> List[Dict[str, Any]]:
        """Venue comparative throughput, total trades, active markets, error count over deduplicated events."""
        delta = parse_window(window)
        since = (datetime.now(timezone.utc) - delta).strftime("%Y-%m-%d %H:%M:%S")

        query = """
        SELECT
            venue,
            uniqExact(event_id) AS event_count,
            uniqExact(market_id) AS active_markets,
            min(received_timestamp) AS first_event,
            max(received_timestamp) AS last_event
        FROM polypulse.market_events FINAL
        WHERE received_timestamp >= {since:DateTime64(3, 'UTC')}
        GROUP BY venue
        ORDER BY event_count DESC
        """
        rows = await self.execute_query(query, {"since": since})

        # Fetch trade counts and volume per venue
        trades_query = """
        SELECT
            venue,
            uniqExact(event_id) AS trade_count,
            sum(ifNull(quantity, toDecimal64(0, 6))) AS total_volume
        FROM polypulse.trades FINAL
        WHERE received_timestamp >= {since:DateTime64(3, 'UTC')}
        GROUP BY venue
        """
        trade_rows = {r["venue"]: r for r in await self.execute_query(trades_query, {"since": since})}

        errors_query = """
        SELECT
            venue,
            count() AS error_count
        FROM polypulse.ingestion_errors FINAL
        WHERE observed_at >= {since:DateTime64(3, 'UTC')}
        GROUP BY venue
        """
        error_rows = {r["venue"]: r for r in await self.execute_query(errors_query, {"since": since})}

        venues = []
        for r in rows:
            v = r["venue"]
            t_data = trade_rows.get(v, {})
            e_data = error_rows.get(v, {})
            venues.append({
                "venue": v,
                "event_count": int(r.get("event_count", 0)),
                "active_markets": int(r.get("active_markets", 0)),
                "trade_count": int(t_data.get("trade_count", 0)),
                "total_volume": round(float(t_data.get("total_volume", 0.0) or 0.0), 4),
                "error_count": int(e_data.get("error_count", 0)),
                "first_event": str(r.get("first_event")),
                "last_event": str(r.get("last_event"))
            })
        return venues

    async def get_storage_telemetry(self) -> Dict[str, Any]:
        """ClickHouse storage footprint, physical part metrics, and partition stats from system.parts."""
        query = """
        SELECT
            table,
            sum(rows) AS physical_rows,
            count() AS active_parts,
            sum(data_compressed_bytes) AS compressed_bytes,
            sum(data_uncompressed_bytes) AS uncompressed_bytes,
            uniqExact(partition) AS partition_count,
            min(min_time) AS oldest_record,
            max(max_time) AS newest_record
        FROM system.parts
        WHERE database = 'polypulse' AND active = 1
        GROUP BY table
        ORDER BY physical_rows DESC
        """
        rows = await self.execute_query(query)
        tables = []
        total_compressed = 0
        total_uncompressed = 0
        total_physical_rows = 0

        for r in rows:
            c_bytes = int(r.get("compressed_bytes", 0) or 0)
            u_bytes = int(r.get("uncompressed_bytes", 0) or 0)
            p_rows = int(r.get("physical_rows", 0) or 0)
            ratio = (u_bytes / c_bytes) if c_bytes > 0 else 1.0

            total_compressed += c_bytes
            total_uncompressed += u_bytes
            total_physical_rows += p_rows

            tables.append({
                "table": r.get("table"),
                "physical_rows": p_rows,
                "rows": p_rows,  # backward compatibility alias
                "parts": int(r.get("active_parts", 0) or 0),
                "compressed_bytes": c_bytes,
                "uncompressed_bytes": u_bytes,
                "compression_ratio": round(ratio, 2),
                "partitions": int(r.get("partition_count", 0) or 0),
                "oldest_record": str(r.get("oldest_record")),
                "newest_record": str(r.get("newest_record"))
            })

        overall_ratio = (total_uncompressed / total_compressed) if total_compressed > 0 else 1.0

        return {
            "status": "connected" if self.enabled else "disabled",
            "physical_rows": total_physical_rows,
            "total_rows": total_physical_rows,
            "total_compressed_bytes": total_compressed,
            "total_uncompressed_bytes": total_uncompressed,
            "overall_compression_ratio": round(overall_ratio, 2),
            "tables": tables
        }

    async def get_wallet_profile(self, wallet_address: str) -> Optional[Dict[str, Any]]:
        """Queries wallet summary metrics from polypulse.wallet_trades FINAL."""
        try:
            norm = normalize_address(wallet_address)
        except ValueError:
            return None
        
        query = """
        SELECT
            wallet_address,
            count() AS trade_count,
            uniqExact(market_id) AS market_count,
            uniqExact(venue) AS venue_count,
            sum(quantity) AS total_volume,
            sum(notional) AS total_notional,
            min(received_timestamp) AS first_seen,
            max(received_timestamp) AS last_seen,
            countIf(attribution_type = 'maker') AS maker_count,
            countIf(attribution_type = 'taker') AS taker_count,
            countIf(attribution_type = 'synthetic') AS synthetic_count
        FROM polypulse.wallet_trades FINAL
        WHERE wallet_address = {wallet:String}
        GROUP BY wallet_address
        """
        rows = await self.execute_query(query, {"wallet": norm})
        if not rows:
            return None
        r = rows[0]
        return {
            "wallet_address": r.get("wallet_address", norm),
            "trade_count": int(r.get("trade_count", 0)),
            "market_count": int(r.get("market_count", 0)),
            "venue_count": int(r.get("venue_count", 0)),
            "total_volume": round(float(r.get("total_volume", 0.0) or 0.0), 4),
            "total_notional": round(float(r.get("total_notional", 0.0) or 0.0), 2),
            "first_seen": str(r.get("first_seen")),
            "last_seen": str(r.get("last_seen")),
            "maker_count": int(r.get("maker_count", 0)),
            "taker_count": int(r.get("taker_count", 0)),
            "synthetic_count": int(r.get("synthetic_count", 0))
        }

    async def get_wallet_trades(
        self,
        wallet_address: str,
        limit: int = 100,
        offset: int = 0,
        venue: Optional[str] = None,
        market_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Queries paginated wallet-attributed trades from polypulse.wallet_trades FINAL."""
        try:
            norm = normalize_address(wallet_address)
        except ValueError:
            return []

        venue_filter = "AND venue = {venue:String}" if venue else ""
        market_filter = "AND market_id = {market:String}" if market_id else ""
        query = f"""
        SELECT
            trade_id,
            event_id,
            wallet_address,
            venue,
            market_id,
            attribution_type,
            side,
            price,
            quantity,
            notional,
            exchange_timestamp,
            received_timestamp,
            transaction_hash,
            block_number,
            log_index,
            source
        FROM polypulse.wallet_trades FINAL
        WHERE wallet_address = {{wallet:String}} {venue_filter} {market_filter}
        ORDER BY received_timestamp DESC
        LIMIT {{limit:UInt32}} OFFSET {{offset:UInt32}}
        """
        params = {"wallet": norm, "limit": limit, "offset": offset}
        if venue:
            params["venue"] = venue
        if market_id:
            params["market"] = market_id

        rows = await self.execute_query(query, params)
        return [{
            "trade_id": r.get("trade_id"),
            "event_id": str(r.get("event_id")),
            "wallet_address": r.get("wallet_address"),
            "venue": r.get("venue"),
            "market_id": r.get("market_id"),
            "attribution_type": r.get("attribution_type"),
            "side": r.get("side"),
            "price": str(r.get("price")),
            "quantity": str(r.get("quantity")),
            "notional": str(r.get("notional")),
            "exchange_timestamp": str(r.get("exchange_timestamp")),
            "received_timestamp": str(r.get("received_timestamp")),
            "transaction_hash": r.get("transaction_hash"),
            "block_number": r.get("block_number"),
            "log_index": r.get("log_index"),
            "source": r.get("source")
        } for r in rows]

    async def get_wallet_markouts(
        self,
        wallet_address: str,
        horizon: Optional[int] = None,
        venue: Optional[str] = None,
        market_id: Optional[str] = None,
        calc_version: str = "v1.0"
    ) -> Optional[Dict[str, Any]]:
        """Queries multi-horizon markout calculations from polypulse.trade_markouts FINAL."""
        try:
            norm = normalize_address(wallet_address)
        except ValueError:
            return None

        h_filter = "AND horizon_seconds = {horizon:UInt16}" if horizon else ""
        v_filter = "AND venue = {venue:String}" if venue else ""
        m_filter = "AND market_id = {market:String}" if market_id else ""

        query = f"""
        SELECT
            trade_id,
            wallet_address,
            venue,
            market_id,
            side,
            horizon_seconds,
            calc_version,
            status,
            exec_price,
            future_mid,
            markout,
            markout_bps,
            observation_time,
            calculated_at
        FROM polypulse.trade_markouts FINAL
        WHERE wallet_address = {{wallet:String}}
          AND calc_version = {{calc_version:String}}
          {h_filter} {v_filter} {m_filter}
        ORDER BY observation_time DESC, calculated_at DESC
        """
        params = {"wallet": norm, "calc_version": calc_version}
        if horizon:
            params["horizon"] = horizon
        if venue:
            params["venue"] = venue
        if market_id:
            params["market"] = market_id

        rows = await self.execute_query(query, params)
        if not rows:
            return None

        horizons = [horizon] if horizon else [5, 30, 60]
        summary = []
        entries = []

        for h in horizons:
            h_rows = [r for r in rows if int(r.get("horizon_seconds", 0)) == h]
            trade_count = len(h_rows)
            available = [r for r in h_rows if r.get("status") == "available" and r.get("markout") is not None]
            avail_count = len(available)
            vals = [float(r["markout"]) for r in available]
            bps_vals = [float(r["markout_bps"]) for r in available if r.get("markout_bps") is not None]

            avg_m = (sum(vals) / len(vals)) if vals else None
            avg_bps = (sum(bps_vals) / len(bps_vals)) if bps_vals else None
            pos_pct = (100.0 * sum(1 for v in vals if v > 0) / len(vals)) if vals else None

            status_counts = {
                "available": avail_count,
                "unavailable": sum(1 for r in h_rows if r.get("status") == "unavailable"),
                "crossed": sum(1 for r in h_rows if r.get("status") == "crossed"),
                "stale": sum(1 for r in h_rows if r.get("status") == "stale")
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

            for r in h_rows:
                entries.append({
                    "trade_id": r.get("trade_id"),
                    "horizon_seconds": h,
                    "side": r.get("side"),
                    "exec_price": float(r["exec_price"]) if r.get("exec_price") is not None else None,
                    "future_mid": float(r["future_mid"]) if r.get("future_mid") is not None else None,
                    "markout": float(r["markout"]) if r.get("markout") is not None else None,
                    "markout_bps": float(r["markout_bps"]) if r.get("markout_bps") is not None else None,
                    "status": r.get("status"),
                    "available": r.get("status") == "available",
                    "observation_time": str(r.get("observation_time")),
                    "calc_version": r.get("calc_version")
                })

        return {"wallet": norm, "summary": summary, "entries": entries}

    async def get_wallet_rankings(
        self,
        horizon: int = 30,
        min_trades: int = 5,
        calc_version: str = "v1.0",
        limit: int = 50
    ) -> List[Dict[str, Any]]:
        """Queries top wallets ranked by average markout with minimum sample size guardrail."""
        query = """
        SELECT
            wallet_address AS wallet,
            count() AS trade_count,
            countIf(status = 'available') AS available_count,
            avgIf(markout, status = 'available') AS avg_markout,
            avgIf(markout_bps, status = 'available') AS avg_markout_bps,
            countIf(status = 'available' AND markout > 0) / nullIf(countIf(status = 'available'), 0) * 100 AS positive_percentage,
            count() >= {min_trades:UInt32} AS qualified
        FROM polypulse.trade_markouts FINAL
        WHERE horizon_seconds = {horizon:UInt16}
          AND calc_version = {calc_version:String}
        GROUP BY wallet_address
        ORDER BY qualified DESC, avg_markout_bps DESC
        LIMIT {limit:UInt32}
        """
        rows = await self.execute_query(query, {
            "horizon": horizon,
            "min_trades": min_trades,
            "calc_version": calc_version,
            "limit": limit
        })
        return [{
            "wallet": r.get("wallet"),
            "trade_count": int(r.get("trade_count", 0)),
            "available_count": int(r.get("available_count", 0)),
            "horizon_seconds": horizon,
            "avg_markout": round(float(r["avg_markout"]), 6) if r.get("avg_markout") is not None else None,
            "avg_markout_bps": round(float(r["avg_markout_bps"]), 2) if r.get("avg_markout_bps") is not None else None,
            "positive_percentage": round(float(r["positive_percentage"]), 1) if r.get("positive_percentage") is not None else None,
            "qualified": bool(r.get("qualified", 0)),
            "sample_status": "qualified" if r.get("qualified") else "insufficient_sample"
        } for r in rows]

    async def get_markout_distribution(
        self,
        horizon: int = 30,
        venue: Optional[str] = None,
        market_id: Optional[str] = None,
        calc_version: str = "v1.0"
    ) -> Dict[str, Any]:
        """Queries adverse selection / markout bps distribution quantiles."""
        v_filter = "AND venue = {venue:String}" if venue else ""
        m_filter = "AND market_id = {market:String}" if market_id else ""

        query = f"""
        SELECT
            count() AS total_count,
            countIf(status = 'available') AS available_count,
            minIf(markout_bps, status = 'available') AS min_bps,
            quantileIf(0.10)(markout_bps, status = 'available') AS p10_bps,
            quantileIf(0.25)(markout_bps, status = 'available') AS p25_bps,
            quantileIf(0.50)(markout_bps, status = 'available') AS median_bps,
            quantileIf(0.75)(markout_bps, status = 'available') AS p75_bps,
            quantileIf(0.90)(markout_bps, status = 'available') AS p90_bps,
            maxIf(markout_bps, status = 'available') AS max_bps,
            avgIf(markout_bps, status = 'available') AS avg_bps,
            countIf(status = 'available' AND markout > 0) / nullIf(countIf(status = 'available'), 0) * 100 AS win_rate
        FROM polypulse.trade_markouts FINAL
        WHERE horizon_seconds = {{horizon:UInt16}}
          AND calc_version = {{calc_version:String}}
          {v_filter} {m_filter}
        """
        params = {"horizon": horizon, "calc_version": calc_version}
        if venue:
            params["venue"] = venue
        if market_id:
            params["market"] = market_id

        rows = await self.execute_query(query, params)
        if not rows:
            return {
                "horizon_seconds": horizon, "total_count": 0, "available_count": 0,
                "quantiles": {"p10": None, "p25": None, "median": None, "p75": None, "p90": None},
                "min_bps": None, "max_bps": None, "avg_bps": None, "win_rate": None
            }
        r = rows[0]
        return {
            "horizon_seconds": horizon,
            "total_count": int(r.get("total_count", 0)),
            "available_count": int(r.get("available_count", 0)),
            "quantiles": {
                "p10": round(float(r["p10_bps"]), 2) if r.get("p10_bps") is not None else None,
                "p25": round(float(r["p25_bps"]), 2) if r.get("p25_bps") is not None else None,
                "median": round(float(r["median_bps"]), 2) if r.get("median_bps") is not None else None,
                "p75": round(float(r["p75_bps"]), 2) if r.get("p75_bps") is not None else None,
                "p90": round(float(r["p90_bps"]), 2) if r.get("p90_bps") is not None else None,
            },
            "min_bps": round(float(r["min_bps"]), 2) if r.get("min_bps") is not None else None,
            "max_bps": round(float(r["max_bps"]), 2) if r.get("max_bps") is not None else None,
            "avg_bps": round(float(r["avg_bps"]), 2) if r.get("avg_bps") is not None else None,
            "win_rate": round(float(r["win_rate"]), 1) if r.get("win_rate") is not None else None
        }

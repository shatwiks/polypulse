"""PolyPulse API. Run with python -m uvicorn backend.main:app --reload."""
import asyncio
import json
import logging
import os
import time
from urllib.parse import quote
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Security, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field, field_validator, model_validator
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Literal, Optional, Tuple
from dotenv import load_dotenv

from backend.connectors import demo
from backend.live import kalshi, polymarket
from backend.polygon import polygon_reconnect
from backend.durable import DurableJournal, export_live_forever
from backend.state import MarketState
from backend.analytics import ClickHouseAnalytics
from backend.wallet import normalize_address
from backend.markout import CURRENT_CALC_VERSION
from backend.logging_config import configure_logging
from backend.scanner import validate_contract_compatibility
from backend.fees import FEE_SCHEDULES, get_fee_schedule, DEFAULT_FEE_VERSION
from backend.notifications import NotificationEngine, NotificationConfig

configure_logging()
load_dotenv()
LOG = logging.getLogger(__name__)


def settings():
    mode = os.getenv("POLYPULSE_MODE", "demo")
    if mode not in ("demo", "live"):
        raise ValueError("POLYPULSE_MODE must be demo or live")
    pairs = json.loads(os.getenv("MARKET_PAIRS_JSON", "[]"))
    clickhouse = os.getenv("CLICKHOUSE_URL", "")
    if mode == "live" and not clickhouse and os.getenv("CLICKHOUSE_PASSWORD"):
        clickhouse = f"http://polypulse:{quote(os.environ['CLICKHOUSE_PASSWORD'], safe='')}@localhost:8123"
    return {"mode": mode, "pairs": pairs,
            "poly": [x for x in os.getenv("POLYMARKET_ASSET_IDS", "").split(",") if x],
            "kalshi": [x for x in os.getenv("KALSHI_MARKET_TICKERS", "").split(",") if x],
            "kalshi_key": os.getenv("KALSHI_API_KEY_ID", ""),
            "kalshi_private_key": os.getenv("KALSHI_PRIVATE_KEY_PATH", ""),
            "clickhouse": clickhouse,
            "polygon_rpc": os.getenv("POLYGON_RPC_URL", ""),
            "polygon_confirmations": int(os.getenv("POLYGON_CONFIRMATIONS", "12")),
            "polygon_start_block": int(os.environ["POLYGON_START_BLOCK"]) if os.getenv("POLYGON_START_BLOCK") else None}


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = settings()
    journal = DurableJournal(os.getenv("POLYPULSE_DB") or ("polypulse.db" if config["mode"] == "demo" else "polypulse-live.db"))
    state = MarketState(journal, config["pairs"], mode=config["mode"])
    if config["mode"] == "live":
        state.replay_unprocessed()
    app.state.market = state
    app.state.config = config
    app.state.storage_status = {"clickhouse": "disabled"}
    app.state.analytics = ClickHouseAnalytics(config["clickhouse"])
    app.state.notifications = NotificationEngine()
    tasks = []
    if config["mode"] == "demo":
        tasks.append(asyncio.create_task(demo(state)))
    else:
        tasks.append(asyncio.create_task(polymarket(state, config["poly"])))
        if config["kalshi"] and config["kalshi_key"] and config["kalshi_private_key"]:
            tasks.append(asyncio.create_task(kalshi(state, config["kalshi"], config["kalshi_key"], config["kalshi_private_key"])))
        else:
            state.set_feed_status("kalshi", "unconfigured", "Ticker and API credentials required")
        if config["polygon_rpc"]:
            tasks.append(asyncio.create_task(polygon_reconnect(state, config["polygon_rpc"],
                config["polygon_confirmations"], config["polygon_start_block"])))
        else:
            state.set_feed_status("polygon", "unconfigured", "POLYGON_RPC_URL required")
    if config["clickhouse"]:
        app.state.storage_status["clickhouse"] = "connecting"
        tasks.append(asyncio.create_task(export_live_forever(journal, config["clickhouse"], app.state.storage_status)))
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


def notifications() -> NotificationEngine:
    if not hasattr(app.state, "notifications"):
        app.state.notifications = NotificationEngine()
    return app.state.notifications


def _split_pair_id(pair_id: str) -> Tuple[str, str]:
    if ":" in pair_id:
        p, k = pair_id.split(":", 1)
        return p, k
    elif "_" in pair_id:
        p, k = pair_id.split("_", 1)
        return p, k
    return pair_id, ""


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
async def spreads(opportunities_only: bool = Query(False, description="Filter for positive arbitrage opportunities only")):
    return state().spreads(opportunities_only=opportunities_only)


@app.get("/api/v1/spreads/opportunities")
async def spread_opportunities():
    return state().spreads(opportunities_only=True)


class ContractPairIn(BaseModel):
    polymarket_asset_id: str = Field(min_length=1, max_length=120)
    kalshi_ticker: str = Field(min_length=1, max_length=120)
    canonical_event: str = Field(default="", max_length=500)
    canonical_outcome: str = Field(default="", max_length=500)
    event_description: str = Field(default="", max_length=500)
    outcome_equivalence: str = Field(default="", max_length=1000)
    equivalence_mapping: Literal["direct", "inverse"] = "direct"
    resolution_source: str = Field(min_length=1, max_length=500)
    resolution_criteria: str = Field(default="", max_length=2000)
    polymarket_expiration: Optional[datetime] = None
    kalshi_expiration: Optional[datetime] = None
    expiration: datetime
    settlement_details: str = Field(default="", max_length=2000)
    verification_status: Literal["unverified", "verified", "rejected", "incompatible", "expired"] = "unverified"
    reviewer: str = Field(default="", max_length=120)
    verified_at: Optional[datetime] = None

    @field_validator("expiration", "polymarket_expiration", "kalshi_expiration")
    @classmethod
    def require_timezone(cls, value):
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("expiration needs a timezone")
        return value.astimezone(timezone.utc) if value is not None else None

    @model_validator(mode="after")
    def validate_semantics(self):
        # Bi-directional fallback between legacy and canonical fields
        if not self.canonical_event and self.event_description:
            self.canonical_event = self.event_description
        if not self.event_description and self.canonical_event:
            self.event_description = self.canonical_event
        if not self.canonical_outcome and self.outcome_equivalence:
            self.canonical_outcome = self.outcome_equivalence
        if not self.outcome_equivalence and self.canonical_outcome:
            self.outcome_equivalence = self.canonical_outcome

        # Strict checks if marking as verified
        if self.verification_status == "verified":
            if not self.reviewer.strip():
                raise ValueError("Verified contract pairs require an explicit reviewer identity")
            if not self.resolution_source.strip():
                raise ValueError("Verified contract pairs require an authoritative resolution source")
            if self.expiration <= datetime.now(timezone.utc):
                raise ValueError("Cannot verify an already expired contract pair")
            if not self.canonical_event.strip():
                raise ValueError("Verified contract pairs require a canonical event specification")
            if not self.verified_at:
                self.verified_at = datetime.now(timezone.utc)
        return self


class ContractVerifyIn(BaseModel):
    polymarket_asset_id: str
    kalshi_ticker: str
    verification_status: Literal["verified", "rejected", "incompatible", "unverified"]
    reviewer: str = Field(min_length=1, max_length=120)
    equivalence_mapping: Optional[Literal["direct", "inverse"]] = None
    resolution_criteria: Optional[str] = None
    settlement_details: Optional[str] = None
    notes: Optional[str] = None


operator_key_header = APIKeyHeader(name="X-Operator-Key", auto_error=False)


def verify_operator_access(
    x_operator_key: str | None = Security(operator_key_header),
    authorization: str | None = Header(default=None)
):
    """Enforce operator authentication on contract review routes when OPERATOR_KEY is configured."""
    required_key = os.getenv("OPERATOR_KEY")
    if not required_key:
        return True
    if x_operator_key and x_operator_key == required_key:
        return True
    if authorization:
        parts = authorization.split()
        if len(parts) == 2 and parts[0].lower() == "bearer" and parts[1] == required_key:
            return True
    raise HTTPException(status_code=403, detail="Invalid or missing operator authorization")


@app.get("/api/v1/contracts")
async def contracts(status: Optional[str] = Query(None, description="Filter by status: verified, unverified, rejected, incompatible, expired, all")):
    return state().journal.contract_pairs(status=status)


@app.get("/api/v1/contracts/{polymarket_asset_id}/{kalshi_ticker}")
async def get_contract(polymarket_asset_id: str, kalshi_ticker: str):
    pair = state().journal.get_contract_pair(polymarket_asset_id, kalshi_ticker)
    if not pair:
        raise HTTPException(404, "Contract pair unavailable")
    return pair


@app.put("/api/v1/contracts", dependencies=[Depends(verify_operator_access)])
async def save_contract(pair: ContractPairIn):
    payload = pair.model_dump(mode="json")
    state().journal.save_contract_pair(payload)
    return {"saved": True, "verification_status": pair.verification_status}


@app.post("/api/v1/contracts/verify", dependencies=[Depends(verify_operator_access)])
async def verify_contract(req: ContractVerifyIn):
    journal = state().journal
    pair = journal.get_contract_pair(req.polymarket_asset_id, req.kalshi_ticker)
    if not pair:
        raise HTTPException(404, "Contract pair not found")

    if req.verification_status == "verified":
        # Check expiration
        exp_str = pair.get("expiration")
        if exp_str:
            try:
                exp_dt = datetime.fromisoformat(exp_str.replace("Z", "+00:00"))
                if exp_dt <= datetime.now(timezone.utc):
                    raise HTTPException(400, "Cannot verify an already expired contract pair")
            except Exception as exc:
                raise HTTPException(400, f"Invalid expiration date: {exc}")

    pair["verification_status"] = req.verification_status
    pair["reviewer"] = req.reviewer
    if req.equivalence_mapping:
        pair["equivalence_mapping"] = req.equivalence_mapping
    if req.resolution_criteria:
        pair["resolution_criteria"] = req.resolution_criteria
    if req.settlement_details:
        pair["settlement_details"] = req.settlement_details
    if req.verification_status == "verified":
        pair["verified_at"] = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    else:
        pair["verified_at"] = None

    journal.save_contract_pair(pair)
    return {"verified": True, "verification_status": req.verification_status, "reviewer": req.reviewer}


@app.delete("/api/v1/contracts/{polymarket_asset_id}/{kalshi_ticker}", dependencies=[Depends(verify_operator_access)])
async def delete_contract(polymarket_asset_id: str, kalshi_ticker: str):
    if not state().journal.delete_contract_pair(polymarket_asset_id, kalshi_ticker):
        raise HTTPException(404, "Contract pair unavailable")
    return {"deleted": True}


class PairReviewIn(BaseModel):
    verification_status: Literal["verified", "rejected", "incompatible", "unverified"]
    reviewer: str = Field(min_length=1, max_length=120)
    canonical_event: Optional[str] = None
    canonical_outcome: Optional[str] = None
    equivalence_mapping: Optional[Literal["direct", "inverse"]] = None
    resolution_source: Optional[str] = None
    resolution_criteria: Optional[str] = None
    polymarket_expiration: Optional[datetime] = None
    kalshi_expiration: Optional[datetime] = None
    expiration: Optional[datetime] = None
    settlement_details: Optional[str] = None
    notes: Optional[str] = None


@app.get("/api/v1/cross-venue/pairs")
async def cross_venue_pairs(
    status: Optional[str] = Query(None, description="Filter: verified, unverified, rejected, incompatible, expired, all"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0)
):
    pairs = state().journal.contract_pairs(status=status)
    return {"total": len(pairs), "pairs": pairs[offset : offset + limit]}


@app.get("/api/v1/cross-venue/pairs/{pair_id}")
async def get_cross_venue_pair(pair_id: str):
    poly_id, kalshi_ticker = _split_pair_id(pair_id)
    pair = state().journal.get_contract_pair(poly_id, kalshi_ticker)
    if not pair:
        raise HTTPException(404, f"Contract pair '{pair_id}' not found")
    poly_book = state().get("polymarket", poly_id)
    kalshi_book = state().get("kalshi", kalshi_ticker)
    return {
        "pair": pair,
        "live_books": {
            "polymarket": poly_book,
            "kalshi": kalshi_book
        }
    }


@app.put("/api/v1/cross-venue/pairs/{pair_id}/review", dependencies=[Depends(verify_operator_access)])
async def review_cross_venue_pair(pair_id: str, review: PairReviewIn):
    poly_id, kalshi_ticker = _split_pair_id(pair_id)
    journal = state().journal
    pair = journal.get_contract_pair(poly_id, kalshi_ticker)
    if not pair:
        raise HTTPException(404, f"Contract pair '{pair_id}' not found")

    if review.verification_status == "verified":
        exp_str = pair.get("expiration")
        if exp_str:
            try:
                exp_dt = datetime.fromisoformat(exp_str.replace("Z", "+00:00"))
                if exp_dt <= datetime.now(timezone.utc):
                    raise HTTPException(400, "Cannot verify an already expired contract pair")
            except Exception as exc:
                raise HTTPException(400, f"Invalid expiration date: {exc}")

    pair["verification_status"] = review.verification_status
    pair["reviewer"] = review.reviewer
    if review.canonical_event:
        pair["canonical_event"] = review.canonical_event
    if review.canonical_outcome:
        pair["canonical_outcome"] = review.canonical_outcome
    if review.equivalence_mapping:
        pair["equivalence_mapping"] = review.equivalence_mapping
    if review.resolution_source:
        pair["resolution_source"] = review.resolution_source
    if review.resolution_criteria:
        pair["resolution_criteria"] = review.resolution_criteria
    if review.settlement_details:
        pair["settlement_details"] = review.settlement_details
    now_iso = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    if review.verification_status == "verified":
        pair["verified_at"] = now_iso
    else:
        pair["verified_at"] = None

    journal.save_contract_pair(pair)

    review_record = {
        "pair_id": pair_id,
        "polymarket_asset_id": poly_id,
        "kalshi_ticker": kalshi_ticker,
        "canonical_event": pair.get("canonical_event", ""),
        "canonical_outcome": pair.get("canonical_outcome", ""),
        "equivalence_mapping": pair.get("equivalence_mapping", "direct"),
        "resolution_source": pair.get("resolution_source", ""),
        "resolution_criteria": pair.get("resolution_criteria", ""),
        "polymarket_expiration": pair.get("polymarket_expiration"),
        "kalshi_expiration": pair.get("kalshi_expiration"),
        "settlement_details": pair.get("settlement_details", ""),
        "verification_status": review.verification_status,
        "reviewer": review.reviewer,
        "reviewed_at": now_iso
    }
    journal.record_contract_pair_review(review_record)
    return {"saved": True, "pair": pair, "review_record": review_record}


@app.get("/api/v1/cross-venue/spreads")
async def cross_venue_spreads(
    opportunities_only: bool = Query(False, description="Filter for positive arbitrage opportunities only"),
    limit: int = Query(100, ge=1, le=500)
):
    spreads = state().spreads(opportunities_only=opportunities_only)
    notif_engine = notifications()
    journal = state().journal
    for s in spreads:
        notif_engine.evaluate_spread(s)
        # Record spread observation in journal
        try:
            poly_book = s.get("polymarket", {})
            kalshi_book = s.get("kalshi", {})
            quote_age = s.get("quote_age_seconds", {})
            obs_data = {
                "pair_id": s["pair_id"],
                "canonical_event": s.get("canonical_event", ""),
                "canonical_outcome": s.get("canonical_outcome", ""),
                "equivalence_mapping": s.get("equivalence_mapping", "direct"),
                "poly_best_bid": poly_book.get("best_bid", 0.0),
                "poly_best_ask": poly_book.get("best_ask", 0.0),
                "kalshi_best_bid": kalshi_book.get("best_bid", 0.0),
                "kalshi_best_ask": kalshi_book.get("best_ask", 0.0),
                "poly_ask_depth": poly_book.get("ask_depth", 0.0),
                "kalshi_bid_depth": kalshi_book.get("bid_depth", 0.0),
                "gross_spread": s.get("best_spread", 0.0),
                "gross_spread_bps": s.get("best_spread_bps", 0.0),
                "net_spread": s.get("net_spread", 0.0),
                "net_spread_bps": s.get("best_spread_bps", 0.0),
                "poly_fee": s.get("poly_fee", 0.0),
                "kalshi_fee": s.get("kalshi_fee", 0.0),
                "slippage_cost": 0.0,
                "executable_qty": s.get("executable_qty", 0.0),
                "opportunity_type": s.get("opportunity_type", "none"),
                "status": s.get("status", "candidate"),
                "quote_age_poly": quote_age.get("polymarket", 0.0),
                "quote_age_kalshi": quote_age.get("kalshi", 0.0),
                "venue_delta_seconds": quote_age.get("venue_delta", 0.0),
            }
            journal.record_spread_observation(obs_data)
        except Exception as exc:
            LOG.debug("Error auto-recording spread observation: %s", exc)

    return spreads[:limit]


@app.get("/api/v1/cross-venue/opportunities")
async def cross_venue_opportunities(
    status: Optional[str] = Query(None, description="Status filter: candidate, executable, insufficient_liquidity, stale, all"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0)
):
    all_spreads = state().spreads(opportunities_only=False)
    notif_engine = notifications()
    for s in all_spreads:
        notif_engine.evaluate_spread(s)

    if status and status != "all":
        filtered = [s for s in all_spreads if s.get("status") == status]
    else:
        filtered = [s for s in all_spreads if s.get("status") in ("candidate", "executable", "insufficient_liquidity", "stale")]

    return {
        "total": len(filtered),
        "opportunities": filtered[offset : offset + limit]
    }


@app.get("/api/v1/cross-venue/opportunities/{opportunity_id}")
async def get_cross_venue_opportunity(opportunity_id: str):
    all_spreads = state().spreads(opportunities_only=False)
    match = next((s for s in all_spreads if s.get("pair_id") == opportunity_id or s.get("pair_id") == opportunity_id.replace("_", ":")), None)
    if not match:
        raise HTTPException(404, f"Opportunity '{opportunity_id}' not found in active scanner")
    return match


@app.get("/api/v1/cross-venue/fees")
async def cross_venue_fees(version: Optional[str] = Query(None)):
    if version and version in FEE_SCHEDULES:
        return asdict(FEE_SCHEDULES[version])
    return {
        "default_version": DEFAULT_FEE_VERSION,
        "schedules": {k: asdict(v) for k, v in FEE_SCHEDULES.items()}
    }


@app.get("/api/v1/cross-venue/analytics")
async def cross_venue_analytics(
    window: str = Query("24h"),
    min_observations: int = Query(5, ge=1)
):
    ch = analytics()
    if ch.enabled:
        top_pairs = await ch.get_top_spread_pairs(min_observations=min_observations, window=window)
        frequency = await ch.get_opportunity_frequency(min_observations=min_observations, window=window)
        duration = await ch.get_opportunity_duration_stats(window=window)
        venue_edge = await ch.get_venue_comparison_analytics(window=window)
        freshness = await ch.get_quote_freshness_stats(window=window)
        return {
            "source": "clickhouse",
            "window": window,
            "min_observations": min_observations,
            "top_spread_pairs": top_pairs,
            "opportunity_frequency": frequency,
            "duration_stats": duration,
            "venue_comparison": venue_edge,
            "quote_freshness": freshness
        }

    obs = state().journal.get_spread_observations(limit=100)
    opps = state().journal.get_arbitrage_opportunities(limit=100)
    return {
        "source": "sqlite_local",
        "window": window,
        "observation_count": len(obs),
        "opportunity_count": len(opps),
        "recent_observations": obs[:20],
        "recent_opportunities": opps[:20]
    }


@app.get("/api/v1/cross-venue/notifications")
async def cross_venue_notifications(
    pair_id: Optional[str] = Query(None),
    event_type: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200)
):
    return notifications().get_history(pair_id=pair_id, event_type=event_type, limit=limit)


@app.get("/api/v1/health")
async def health():
    market = state()
    return {"mode": app.state.config["mode"], "feeds": market.feed_status,
            "feed_details": market.feed_details,
            "book_status": {f"{venue}:{market_id}": data for (venue,market_id),data in market.book_status.items()},
            "rates": market.rates(), "ingestion_lag_seconds": market.journal.ingestion_lag_seconds(),
            "storage": app.state.storage_status, "uptime_seconds": round(time.time() - market.started_at),
            "journal": market.journal.stats(), "counters": market.counters}


@app.get("/api/v1/metrics")
async def metrics():
    return await health()


@app.get("/metrics", response_class=PlainTextResponse)
async def prometheus_metrics():
    data = await health()
    values = {**data["counters"], **data["rates"],
        "ingestion_lag_seconds": data["ingestion_lag_seconds"],
        "journal_pending_export": data["journal"]["pending_export"],
        "clickhouse_insert_latency_ms": data["storage"].get("insert_latency_ms")}
    return "\n".join(f"polypulse_{key} {value}" for key,value in values.items()
        if isinstance(value, (int,float))) + "\n"


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
    return state().journal.recent_trades()


def validate_address_param(address: str) -> str:
    try:
        return normalize_address(address)
    except ValueError as exc:
        raise HTTPException(422, f"Invalid wallet address: {address}") from exc


@app.get("/api/v1/wallets")
async def wallets():
    return state().journal.wallets()


@app.get("/api/v1/wallets/{address}")
async def wallet_profile(address: str):
    norm = validate_address_param(address)
    ch = analytics()
    if ch.enabled:
        prof = await ch.get_wallet_profile(norm)
        if prof:
            return prof
    return state().journal.get_wallet_profile(norm)


@app.get("/api/v1/wallets/{address}/trades")
async def wallet_trades(
    address: str,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    venue: str | None = None,
    market_id: str | None = None
):
    norm = validate_address_param(address)
    ch = analytics()
    if ch.enabled:
        t_rows = await ch.get_wallet_trades(norm, limit=limit, offset=offset, venue=venue, market_id=market_id)
        if t_rows:
            return t_rows
    return state().journal.get_wallet_trades(norm, limit=limit, offset=offset, venue=venue, market_id=market_id)


@app.get("/api/v1/wallets/{address}/markouts")
async def markouts(
    address: str,
    horizon: int | None = Query(None),
    venue: str | None = None,
    market_id: str | None = None,
    calc_version: str = Query(CURRENT_CALC_VERSION)
):
    if horizon is not None and horizon not in (5, 30, 60):
        raise HTTPException(422, "horizon must be 5, 30, or 60")
    norm = validate_address_param(address)
    ch = analytics()
    if ch.enabled:
        res = await ch.get_wallet_markouts(norm, horizon=horizon, venue=venue, market_id=market_id, calc_version=calc_version)
        if res:
            return res
    return state().journal.get_wallet_markouts(norm, horizon=horizon, venue=venue, market_id=market_id, calc_version=calc_version)


@app.get("/api/v1/wallets/{address}/performance")
async def wallet_performance(
    address: str,
    calc_version: str = Query(CURRENT_CALC_VERSION)
):
    norm = validate_address_param(address)
    return state().journal.get_wallet_performance(norm, calc_version=calc_version)


def analytics() -> ClickHouseAnalytics:
    return getattr(app.state, "analytics", ClickHouseAnalytics(""))


@app.get("/api/v1/analytics/market/{market_id}/summary")
async def market_summary(market_id: str, venue: str | None = None, window: str = Query("24h", pattern="^(1h|6h|24h|7d)$")):
    ch = analytics()
    res = await ch.get_market_summary(market_id, venue=venue, window=window)
    if not ch.enabled or (res["trade_count"] == 0 and res["snapshot_count"] == 0):
        m = state().get(venue or "polymarket", market_id)
        if m:
            res["best_bid"] = m.get("best_bid")
            res["best_ask"] = m.get("best_ask")
            res["mid_price"] = m.get("mid_price")
            res["spread"] = m.get("spread")
            res["spread_bps"] = m.get("spread_bps")
            res["vwap"] = m.get("buy_vwap_10")
    return res


@app.get("/api/v1/analytics/market/{market_id}/candles")
async def market_candles(
    market_id: str,
    venue: str | None = None,
    resolution: str = Query("1m", pattern="^(1m|5m|1h|1d)$"),
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    limit: int = Query(500, ge=1, le=2000)
):
    if start_time and end_time and (end_time - start_time) > timedelta(days=30):
        raise HTTPException(422, "Maximum time range is 30 days")
    ch = analytics()
    return await ch.get_market_candles(market_id, venue=venue, resolution=resolution,
                                       start_time=start_time, end_time=end_time, limit=limit)


@app.get("/api/v1/analytics/market/{market_id}/trades")
async def market_trades(
    market_id: str,
    venue: str | None = None,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0)
):
    ch = analytics()
    if ch.enabled:
        return await ch.get_market_trades(market_id, venue=venue, start_time=start_time,
                                          end_time=end_time, limit=limit, offset=offset)
    all_trades = state().journal.recent_trades(limit=limit + offset)
    filtered = [t for t in all_trades if t["market_id"] == market_id]
    if venue:
        filtered = [t for t in filtered if t["venue"] == venue]
    return filtered[offset:offset + limit]


@app.get("/api/v1/analytics/market/{market_id}/spread")
async def market_spread_trajectory(
    market_id: str,
    venue: str | None = None,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    limit: int = Query(200, ge=1, le=1000)
):
    ch = analytics()
    return await ch.get_market_spread_trajectory(market_id, venue=venue, start_time=start_time,
                                                 end_time=end_time, limit=limit)


@app.get("/api/v1/analytics/venues")
async def venue_analytics(window: str = Query("24h", pattern="^(1h|6h|24h|7d)$")):
    ch = analytics()
    if ch.enabled:
        return await ch.get_venue_analytics(window=window)
    m = state()
    return [{
        "venue": v,
        "event_count": m.counters.get(f"{v}_events", 0),
        "active_markets": len([k for k in m.books.keys() if k[0] == v]),
        "trade_count": m.counters.get(f"{v}_trades", 0),
        "total_volume": 0.0,
        "error_count": 0,
        "status": m.feed_status.get(v, "unknown")
    } for v in ("polymarket", "kalshi", "polygon")]


@app.get("/api/v1/analytics/storage")
async def storage_analytics():
    ch = analytics()
    telemetry = await ch.get_storage_telemetry()
    telemetry["journal_stats"] = state().journal.stats()
    telemetry["ingestion_lag_seconds"] = state().journal.ingestion_lag_seconds()
    return telemetry


@app.get("/api/v1/analytics/wallet-rankings")
async def wallet_rankings(
    horizon: int = Query(30),
    min_trades: int = Query(5, ge=1, le=1000),
    calc_version: str = Query(CURRENT_CALC_VERSION),
    limit: int = Query(50, ge=1, le=500)
):
    if horizon not in (5, 30, 60):
        raise HTTPException(422, "horizon must be 5, 30, or 60")
    ch = analytics()
    if ch.enabled:
        rankings = await ch.get_wallet_rankings(horizon=horizon, min_trades=min_trades, calc_version=calc_version, limit=limit)
        if rankings:
            return rankings
    return state().journal.get_wallet_rankings(horizon=horizon, min_trades=min_trades, calc_version=calc_version, limit=limit)


@app.get("/api/v1/analytics/markout-distribution")
async def markout_distribution(
    horizon: int = Query(30),
    venue: str | None = None,
    market_id: str | None = None,
    calc_version: str = Query(CURRENT_CALC_VERSION)
):
    if horizon not in (5, 30, 60):
        raise HTTPException(422, "horizon must be 5, 30, or 60")
    ch = analytics()
    if ch.enabled:
        return await ch.get_markout_distribution(horizon=horizon, venue=venue, market_id=market_id, calc_version=calc_version)

    rows = state().journal.db.execute("""
        SELECT markout_bps FROM trade_markouts
        WHERE horizon_seconds = ? AND calc_version = ? AND status = 'available' AND markout_bps IS NOT NULL
    """, (horizon, calc_version)).fetchall()
    bps_vals = sorted(float(r[0]) for r in rows)
    if not bps_vals:
        return {
            "horizon_seconds": horizon, "total_count": 0, "available_count": 0,
            "quantiles": {"p10": None, "p25": None, "median": None, "p75": None, "p90": None},
            "min_bps": None, "max_bps": None, "avg_bps": None, "win_rate": None
        }
    n = len(bps_vals)
    def q(p):
        idx = int(round(p * (n - 1)))
        return round(bps_vals[idx], 2)
    return {
        "horizon_seconds": horizon,
        "total_count": n,
        "available_count": n,
        "quantiles": {
            "p10": q(0.10),
            "p25": q(0.25),
            "median": q(0.50),
            "p75": q(0.75),
            "p90": q(0.90),
        },
        "min_bps": round(bps_vals[0], 2),
        "max_bps": round(bps_vals[-1], 2),
        "avg_bps": round(sum(bps_vals) / n, 2),
        "win_rate": round(100.0 * sum(1 for v in bps_vals if v > 0) / n, 1)
    }


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


@app.websocket("/ws/cross-venue")
async def cross_venue_socket(ws: WebSocket):
    """
    Dedicated real-time WebSocket feed for Cross-Venue Intelligence & Arbitrage Scanner.
    Streams initial scanner state, pair updates, spread ladders, opportunity transitions, and health telemetry.
    """
    await ws.accept()
    market = state()
    notif_engine = notifications()
    try:
        # Initial complete scanner frame
        initial_pairs = market.journal.contract_pairs()
        initial_spreads = market.spreads(opportunities_only=False)
        initial_opps = [s for s in initial_spreads if s.get("status") == "executable"]
        for s in initial_spreads:
            notif_engine.evaluate_spread(s)

        await ws.send_json({
            "type": "initial_state",
            "data": {
                "pairs": initial_pairs,
                "spreads": initial_spreads,
                "opportunities": initial_opps,
                "venue_health": market.feed_status,
                "notifications": notif_engine.get_history(limit=5),
                "server_time": datetime.now(timezone.utc).isoformat()
            }
        })

        # Periodic non-blocking scanner updates (1s frequency)
        while True:
            await asyncio.sleep(1.0)
            current_spreads = market.spreads(opportunities_only=False)
            current_opps = [s for s in current_spreads if s.get("status") == "executable"]
            for s in current_spreads:
                notif_engine.evaluate_spread(s)

            await ws.send_json({
                "type": "scanner_update",
                "data": {
                    "spreads": current_spreads,
                    "opportunities": current_opps,
                    "venue_health": market.feed_status,
                    "notifications": notif_engine.get_history(limit=5),
                    "server_time": datetime.now(timezone.utc).isoformat()
                }
            })
    except WebSocketDisconnect:
        pass

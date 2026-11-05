# PolyPulse — Phase 6: Cross-Venue Intelligence and Arbitrage Scanner

You are the senior quantitative trading systems engineer working on PolyPulse.

Phases 1–5 have been implemented.

The existing application includes:

- Polymarket live market ingestion.
- Kalshi connector implementation.
- Polygon V2 blockchain event ingestion.
- SQLite WAL persistence and immutable export batches.
- ClickHouse analytical infrastructure.
- C++20 high-performance orderbook.
- VWAP, OFI and spread analytics.
- Wallet attribution and markout analytics.
- FastAPI backend.
- React trading terminal.

Your task is to implement Phase 6: Cross-Venue Intelligence.

The objective is to compare equivalent prediction-market contracts across Polymarket and Kalshi, identify pricing inefficiencies, calculate executable spreads and provide reliable real-time market intelligence.

Do not rewrite existing components.

## 1. Repository inspection

Before coding, inspect:

- `backend/live.py`
- `backend/polygon.py`
- `backend/engine.py`
- `backend/analytics.py`
- `backend/wallet.py`
- `backend/markout.py`
- Existing market-pair contract registry.
- Existing C++ spread and VWAP calculations.
- Existing REST and WebSocket interfaces.
- ClickHouse schemas and materialized views.
- Current React terminal.

Read documentation from Phases 1–5.

Create `docs/phase6.md` with the architecture and implementation plan.

## 2. Cross-venue contract matching

Build a rigorous cross-venue contract mapping system.

For every proposed pair, store:

- Polymarket asset ID.
- Kalshi market ticker.
- Event description.
- Canonical question.
- Outcome mapping.
- Resolution source.
- Resolution criteria.
- Expiration timestamp.
- Settlement date.
- Market status.
- Verification status.
- Reviewer identity.
- Review timestamp.
- Notes and evidence.

Requirements:

- Do not match contracts using titles alone.
- Compare settlement conditions.
- Compare event expiration and resolution sources.
- Verify binary outcome equivalence.
- Identify differences in wording, cutoff times and settlement rules.
- Support inverse outcome mapping where appropriate.
- Reject ambiguous contracts.
- Reject expired or closed markets.

Only manually verified equivalent contracts may enter the executable spread scanner.

Preserve existing Phase 2 contract registry functionality.

## 3. Canonical price normalization

Create a canonical representation for both venues.

Normalize:

- YES bid.
- YES ask.
- NO bid.
- NO ask.
- Price ticks.
- Quantity units.
- Market status.
- Exchange timestamp.
- Receive timestamp.
- Quote validity.

For Kalshi, correctly transform NO-side quotes into their YES-equivalent prices.

For Polymarket, preserve outcome token identity.

Requirements:

- Use six-decimal fixed-point arithmetic.
- Avoid floating-point operations in critical spread calculations.
- Never compare different outcome tokens unless the contract registry explicitly maps them.
- Reject malformed or crossed orderbooks.
- Reject stale quotes.
- Preserve venue-specific source timestamps.

## 4. Executable cross-venue spread calculations

Implement both trading directions.

Direction A:
- Buy equivalent outcome on Polymarket.
- Sell equivalent outcome on Kalshi.

Direction B:
- Buy equivalent outcome on Kalshi.
- Sell equivalent outcome on Polymarket.

Calculate:

- Best executable entry price.
- Best executable exit price.
- Gross spread.
- Spread in basis points.
- Available liquidity.
- Maximum executable quantity.
- VWAP for requested order sizes.
- Depth-weighted executable spread.

Use the existing C++20 orderbook and VWAP engine wherever appropriate.

Do not calculate profitability using only the displayed top-of-book price when sufficient execution depth is unavailable.

Support multiple quantity sizes.

## 5. Fee and execution-cost engine

Build a configurable venue fee engine.

Support:

- Polymarket taker fees.
- Kalshi trading fees.
- Applicable minimum fees or rounding rules.
- Estimated transaction costs where relevant.
- Optional execution slippage assumptions.
- Configurable fee schedule versions.

All fee calculations must be versioned.

Requirements:

- Store fee configuration and effective dates.
- Do not invent fee schedules.
- Use verified official sources or operator-provided configuration.
- Separate known fees from estimated costs.
- Display the source and version of fee assumptions.

Calculate:

Gross spread.

Net spread after known trading fees.

Estimated net spread after configurable additional execution costs.

Clearly label each result.

Do not call a price difference risk-free arbitrage.

## 6. Opportunity intelligence engine

Build a scanner that evaluates verified market pairs continuously.

For each opportunity, record:

- Pair ID.
- Buy venue.
- Sell venue.
- Contract and outcome.
- Gross spread.
- Net spread.
- Spread bps.
- Available execution quantity.
- VWAP entry cost.
- VWAP exit value.
- Fee assumptions.
- Quote timestamps.
- Quote validity.
- Quote age.
- Observation time.
- Opportunity status.

Opportunity statuses:

- candidate
- executable
- insufficient_liquidity
- stale
- invalid_book
- unverified_contract
- expired
- closed

Only opportunities with verified contract equivalence, valid quotes, sufficient depth and configured positive net spread may be classified as executable.

## 7. Reliability and synchronization

A spread comparison is only meaningful when both venue prices are sufficiently current.

Requirements:

- Configurable maximum quote age.
- Configurable maximum timestamp difference between venues.
- Independent venue health monitoring.
- Immediate invalidation when either orderbook becomes invalid.
- Correct handling of disconnects and sequence gaps.
- No synthetic quote refreshes.
- No mixing demo and live data.
- Re-evaluate opportunities after every relevant book update.
- Prevent duplicate opportunity notifications.

Document timestamp synchronization assumptions and clock skew.

## 8. ClickHouse storage and historical analytics

Add appropriate schemas for:

- `contract_pair_reviews`
- `cross_venue_quotes`
- `spread_observations`
- `arbitrage_opportunities`
- `fee_schedule_versions`

Support:

- Historical gross spread.
- Historical net spread.
- Opportunity duration.
- Maximum available depth.
- Time spent executable.
- Venue comparison.
- Opportunity frequency.
- Quote freshness statistics.

Integrate with the existing immutable SQLite export-batch mechanism.

Do not create duplicate-inflated analytical aggregates.

## 9. Historical cross-venue analytics

Implement queries that answer:

- Which verified pairs have the largest average spread?
- Which pairs have the most frequent executable opportunities?
- How long do opportunities remain available?
- Which venue usually has better prices?
- How does available depth affect spread quality?
- How often do gross opportunities disappear after fees?
- How often are apparent opportunities rejected because of stale quotes?

Do not rank pairs with insufficient observations as established opportunities.

## 10. FastAPI API endpoints

Add:

- `GET /api/v1/cross-venue/pairs`
- `GET /api/v1/cross-venue/pairs/{pair_id}`
- `GET /api/v1/cross-venue/spreads`
- `GET /api/v1/cross-venue/opportunities`
- `GET /api/v1/cross-venue/opportunities/{opportunity_id}`
- `GET /api/v1/cross-venue/analytics`
- `GET /api/v1/cross-venue/fees`
- `PUT /api/v1/cross-venue/pairs/{pair_id}/review`

Requirements:

- Parameterized queries.
- Input validation.
- Pagination.
- Bounded historical queries.
- Operator authorization for contract verification.
- Stable response schemas.
- Clear error handling.

Never permit an unverified contract to enter the executable scanner.

## 11. Real-time WebSocket updates

Add a dedicated cross-venue WebSocket feed.

Suggested endpoint:

`WS /ws/cross-venue`

Send:

- initial scanner state
- pair updates
- spread updates
- opportunity detected
- opportunity invalidated
- venue health changes
- contract verification changes

Use bounded queues and avoid blocking the ingestion event loop.

## 12. React cross-venue terminal

Create a dedicated Cross-Venue Intelligence workspace.

Include:

### Opportunity scanner

- Contract name.
- Outcome.
- Buy venue.
- Sell venue.
- Gross spread.
- Net spread.
- Spread bps.
- Maximum executable quantity.
- Quote freshness.
- Contract verification status.

### Market comparison

- Polymarket orderbook.
- Kalshi orderbook.
- Equivalent outcome mapping.
- Best bid and ask.
- Available depth.
- VWAP at selected quantities.

### Opportunity details

- Gross and net calculations.
- Fee breakdown.
- Estimated slippage.
- Timestamp synchronization.
- Contract settlement evidence.
- Reason for rejection where not executable.

### Historical analytics

- Spread history.
- Opportunity duration.
- Net spread distribution.
- Frequency by market pair.

Use actual backend data.

Do not invent live opportunities or fabricate profit metrics.

## 13. Notifications

Implement a configurable notification engine.

Support:

- Positive net spread threshold.
- Minimum liquidity threshold.
- Maximum quote age.
- Contract verification requirement.
- Per-pair notification cooldown.

Prevent repeated notifications for the same unchanged opportunity.

Notifications must distinguish:
- Candidate opportunities.
- Confirmed executable quotes.
- Invalidated opportunities.

Do not submit real exchange orders. This phase is analytics and monitoring only.

## 14. Testing

Add deterministic tests for:

- Contract equivalence validation.
- Inverse outcome mappings.
- YES/NO price normalization.
- Gross spread calculations.
- Fee calculations.
- Net spread calculations.
- VWAP at multiple execution sizes.
- Insufficient liquidity.
- Crossed books.
- Stale quotes.
- Timestamp mismatch.
- Market expiration.
- Invalid contract pairs.
- Opportunity state transitions.
- Duplicate notification prevention.
- ClickHouse duplicate delivery.
- WebSocket recovery.
- Operator authorization.

Run actual live verification only where credentials and market availability permit.

Never fabricate successful Kalshi live testing if credentials are unavailable.

## 15. Performance and benchmarking

Benchmark:

- Pair evaluation throughput.
- Spread calculation latency.
- Depth-aware VWAP latency.
- Number of pairs scanned per second.
- WebSocket update processing.
- Historical query latency.
- Opportunity detection delay.

Record hardware and methodology.

Compare C++ engine calculations with independent reference calculations.

Do not invent benchmark figures.

## 16. Strict engineering boundaries

- Do not execute live trades.
- Do not expose credentials.
- Do not infer contract equivalence from titles.
- Do not use invalid or stale books.
- Do not treat gross spreads as guaranteed profits.
- Do not fabricate fee information.
- Do not rewrite the existing C++ engine.
- Preserve Phases 1–5.
- Preserve demo/live isolation.
- Keep the SQLite outbox durability boundary.

## 17. Final deliverables

Create:

- Contract equivalence and review service.
- Canonical cross-venue price normalization.
- Depth-aware spread scanner.
- Fee and execution-cost engine.
- Opportunity state machine.
- ClickHouse cross-venue schemas.
- FastAPI endpoints.
- Real-time WebSocket.
- React cross-venue workspace.
- Historical analytics.
- Automated tests.
- Reproducible benchmarks.
- `docs/phase6.md`.
- Updated README.

## 18. Final verification report

Provide:

1. Contract matching methodology.
2. Number of manually verified market pairs.
3. Number of rejected or unresolved pairs.
4. Actual live venue connectivity.
5. Number of valid cross-venue books.
6. Spread and fee calculation examples.
7. Depth-aware VWAP results.
8. Actual opportunity counts.
9. Historical analytics benchmarks.
10. Full Python, C++ and frontend test results.
11. Known limitations.
12. Phase 6 completion verdict.

Start by inspecting the existing repository. Implement the smallest reliable version first, then extend it without breaking existing phases.
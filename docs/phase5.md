# PolyPulse Phase 5: Wallet Intelligence and Markout Analytics Engine

## 1. Overview & Objectives

Phase 5 introduces high-fidelity wallet intelligence and execution markout analytics to PolyPulse. Prediction market participants (liquidity providers, directional takers, and automated arbitrageurs) require quantitative evaluation of execution quality and adverse selection.

The primary objective of Phase 5 is to analyze wallet-attributed trading activity across multiple post-trade observation horizons (5s, 30s, and 60s) to evaluate post-trade price movement.

### Core Guarantees & Constraints

1. **Zero Fabrication Guarantee**:
   Public Polymarket CLOB (`last_trade_price`) and Kalshi WebSocket market data feeds do **not** carry wallet addresses. PolyPulse **never** invents, predicts, or fabricates wallet addresses for public market trades.
2. **Deterministic Wallet Attribution**:
   Wallet attribution is strictly derived from:
   - On-chain Polygon CTF Exchange V2 `OrderFilled` log events (`0xd543adfd...`), decoding both the `maker` (`topics[2]`) and `taker` (`topics[3]`) addresses.
   - Demo-mode synthetic injections explicitly designated for testing and simulated execution.
3. **Execution Quality vs. Realized P&L**:
   Markout strictly measures **post-trade price trajectory / adverse selection** relative to trade execution price. It does **not** represent closed portfolio P&L, inventory carrying cost, funding, or settlement payout.

---

## 2. Wallet Attribution Model

### 2.1 Address Normalization
All wallet addresses are canonically normalized to lower-case EIP-55 hex strings:
- Format: `0x` followed by 40 hexadecimal characters (`^0x[0-9a-f]{40}$`).
- Input padding: Ethereum log topics provide 32-byte (64 hex character) words; the leading 12 zero-bytes (`24` hex chars) are stripped to produce the standard 20-byte address (`topics[n][-40:].lower()`).

### 2.2 Dual Attribution for Polygon CTF Fills
Each on-chain V2 `OrderFilled` log captures a bilateral trade between a passive limit maker and an aggressive taker:
- `side_code == 0` (Maker Buy):
  - **Maker**: `side = buy`, `price = collateral / tokens`, `quantity = tokens`.
  - **Taker**: `side = sell`, `price = collateral / tokens`, `quantity = tokens`.
- `side_code == 1` (Maker Sell):
  - **Maker**: `side = sell`, `price = collateral / tokens`, `quantity = tokens`.
  - **Taker**: `side = buy`, `price = collateral / tokens`, `quantity = tokens`.

To ensure idempotency and non-collision, deterministic trade identifiers are generated:
$$\text{trade\_id} = \text{"polygon:"} + \text{tx\_hash} + \text{":"} + \text{log\_index} + \text{":"} + \text{attribution\_type}$$
where $\text{attribution\_type} \in \{\text{"maker"}, \text{"taker"}\}$.

For demo mode:
$$\text{trade\_id} = \text{"demo:"} + \text{synthetic\_id}$$

---

## 3. Markout Calculation Mathematics

### 3.1 Horizon & Post-Trade Midpoint Lookup
For each trade executed at UTC timestamp $t_{\text{exec}}$, markouts are evaluated at three standardized horizons:
$$H \in \{5, 30, 60\} \text{ seconds}$$

The target observation time is:
$$t_{\text{target}} = t_{\text{exec}} + H$$

The engine queries top-of-book orderbook snapshots recorded in the observation tolerance window:
$$t \in [t_{\text{target}}, t_{\text{target}} + 5\text{s}]$$

The closest observation in time within this window is evaluated.

### 3.2 Orderbook Validity & Status Classification
Before calculating a midpoint, the orderbook snapshot is validated against structural microstructure invariants:
1. `valid == 1` (snapshot passes engine checksum and sequence ordering).
2. `best_bid > 0` and `best_ask > 0`.
3. `best_ask > best_bid` (rejection of crossed or locked books).

Status classification:
- `available`: Valid orderbook observation found within tolerance window. Midpoint is defined as $P_{\text{mid}} = \frac{P_{\text{best\_bid}} + P_{\text{best\_ask}}}{2}$.
- `unavailable`: No observation exists in the window (e.g., target horizon is in the future, or feed was paused).
- `stale`: Observation exists but latency between $t_{\text{target}}$ and observation timestamp exceeds $10\text{s}$.
- `crossed`: Observation exists but $P_{\text{best\_ask}} \le P_{\text{best\_bid}}$ or non-positive quotes.

### 3.3 Markout Formulation
Given executed price $P_{\text{exec}}$ and valid future midpoint $P_{\text{future\_mid}}$:

#### Gross Markout ($)
- **Buy Side**:
  $$\text{markout}_{\text{buy}} = P_{\text{future\_mid}} - P_{\text{exec}}$$
- **Sell Side**:
  $$\text{markout}_{\text{sell}} = P_{\text{exec}} - P_{\text{future\_mid}}$$

A positive markout indicates favorable post-trade movement (price rose after buy, or fell after sell). A negative markout indicates adverse selection.

#### Basis Points ($\text{bps}$)
$$\text{markout\_bps} = \left( \frac{\text{markout}}{P_{\text{exec}}} \right) \times 10{,}000 \quad (P_{\text{exec}} > 0)$$

### 3.4 Calculation Versioning
Markout calculations are tagged with `calc_version` (e.g. `v1.0`). Any calibration of tolerance windows, rounding conventions, or microstructure adjustments will increment `calc_version`, allowing side-by-side verification and idempotent backfill.

---

## 4. ClickHouse Analytical Architecture

### 4.1 Schema: `polypulse.wallet_trades`
Stores deduplicated, wallet-attributed trades:
```sql
CREATE TABLE IF NOT EXISTS polypulse.wallet_trades (
  trade_id String,
  event_id UUID,
  wallet_address LowCardinality(String),
  venue LowCardinality(String),
  market_id String,
  attribution_type LowCardinality(String), -- 'maker' or 'taker'
  side LowCardinality(String),             -- 'buy' or 'sell'
  price Decimal64(6) CODEC(T64, ZSTD(1)),
  quantity Decimal64(6) CODEC(T64, ZSTD(1)),
  notional Decimal64(6) CODEC(T64, ZSTD(1)),
  exchange_timestamp Nullable(DateTime64(3, 'UTC')) CODEC(DoubleDelta, ZSTD(1)),
  received_timestamp DateTime64(3, 'UTC') CODEC(DoubleDelta, ZSTD(1)),
  transaction_hash Nullable(String),
  block_number Nullable(UInt64),
  log_index Nullable(UInt32),
  source LowCardinality(String)
) ENGINE = ReplacingMergeTree(received_timestamp)
PARTITION BY toYYYYMM(received_timestamp)
ORDER BY (wallet_address, venue, market_id, trade_id)
TTL received_timestamp + INTERVAL 180 DAY
SETTINGS non_replicated_deduplication_window = 1000;
```

### 4.2 Schema: `polypulse.trade_markouts`
Stores multi-horizon post-trade markout calculations:
```sql
CREATE TABLE IF NOT EXISTS polypulse.trade_markouts (
  trade_id String,
  wallet_address LowCardinality(String),
  venue LowCardinality(String),
  market_id String,
  side LowCardinality(String),
  horizon_seconds UInt16,
  calc_version LowCardinality(String),
  status LowCardinality(String), -- 'available', 'unavailable', 'stale', 'crossed'
  exec_price Decimal64(6) CODEC(T64, ZSTD(1)),
  future_mid Nullable(Decimal64(6)) CODEC(T64, ZSTD(1)),
  markout Nullable(Decimal64(6)) CODEC(T64, ZSTD(1)),
  markout_bps Nullable(Decimal64(4)) CODEC(T64, ZSTD(1)),
  observation_time Nullable(DateTime64(3, 'UTC')) CODEC(DoubleDelta, ZSTD(1)),
  calculated_at DateTime64(3, 'UTC') CODEC(DoubleDelta, ZSTD(1))
) ENGINE = ReplacingMergeTree(calculated_at)
PARTITION BY toYYYYMM(calculated_at)
ORDER BY (wallet_address, horizon_seconds, calc_version, trade_id)
TTL calculated_at + INTERVAL 180 DAY
SETTINGS non_replicated_deduplication_window = 1000;
```

---

## 5. Wallet Ranking & Scoring Methodology

### 5.1 Minimum Sample Size Guardrail
Leaderboards can be distorted by wallets with 1 or 2 lucky trades. PolyPulse enforces a minimum sample size filter:
$$N_{\text{trades}} \ge N_{\text{min}} \quad (\text{default } N_{\text{min}} = 5)$$
Wallets below this threshold are marked as `insufficient_sample` in leaderboard rankings.

### 5.2 Performance Metrics
For each wallet $W$ and horizon $H$:
1. **Mean Markout**:
   $$\mu_{H} = \frac{1}{|A_H|} \sum_{i \in A_H} \text{markout}_i$$
   where $A_H$ is the set of trades with status `available`.
2. **Mean Markout in BPS**:
   $$\mu_{H,\text{bps}} = \frac{1}{|A_H|} \sum_{i \in A_H} \text{markout\_bps}_i$$
3. **Win Rate (Positive Markout %)**:
   $$\text{WinRate}_H = \frac{|\{i \in A_H : \text{markout}_i > 0\}|}{|A_H|} \times 100\%$$
4. **Side Asymmetry**:
   Separate metrics for $\text{buy}$ vs. $\text{sell}$ executions to detect structural directional alpha or maker inventory skew.

---

## 6. Historical Recomputation & Backfill Pipeline

1. **Incremental Processing**:
   As new snapshots arrive, pending trades awaiting horizon maturation ($t_{\text{now}} > t_{\text{exec}} + 60\text{s}$) are processed in micro-batches.
2. **Recomputation Engine**:
   Recomputation accepts parameters `(calc_version, force=True/False)`. Existing records with matching `calc_version` are overwritten idempotently in ClickHouse via `ReplacingMergeTree(calculated_at)`.

---

## 7. FastAPI Endpoint Reference

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/v1/wallets` | Returns list of tracked wallets with trade counts, active markets, and maker/taker attribution split. |
| `GET` | `/api/v1/wallets/{address}` | Returns wallet profile summary (first/last seen, total volume, total notional, maker/taker count). |
| `GET` | `/api/v1/wallets/{address}/trades` | Paginated list of wallet-attributed executions with trade IDs, prices, quantities, and sides. |
| `GET` | `/api/v1/wallets/{address}/markouts` | Multi-horizon markout summaries (5s, 30s, 60s) with average bps, win rates, and observation breakdown. |
| `GET` | `/api/v1/wallets/{address}/performance` | Detailed performance breakdown comparing Buy vs. Sell side markouts and horizon drift. |
| `GET` | `/api/v1/analytics/wallet-rankings` | Leaderboard ranked by average markout bps with minimum sample size ($N \ge 5$) guardrail. |
| `GET` | `/api/v1/analytics/markout-distribution` | Microstructure adverse selection distribution across 10th, 25th, 50th, 75th, and 90th percentiles. |

---

## 8. Frontend Terminal Workspace

The PolyPulse Terminal (`frontend/src/main.tsx`) features a dedicated "Wallet Intelligence" workspace tab:
- **Leaderboard View**: Displays ranked wallets, total executions, 5s/30s/60s markout in bps and dollars, positive markout percentage, and qualification status pill (`QUALIFIED` vs `N < 5`).
- **Selected Wallet Intelligence**: Detailed profile panel displaying cumulative units traded, dollar notional, maker/taker badge, multi-horizon markout cards with status badges (`available`, `crossed`, `stale`, `unavailable`), and recent execution log.
- **Drift Distribution Panel**: Adverse selection quantile analysis across $P_{10}$, $P_{25}$, $P_{50}$ (median), $P_{75}$, and $P_{90}$ alpha tail.

---

## 9. Verification & Audit Results

### 9.1 Test Suite Verification
- **Wallet & Markout Unit Suite**: `tests/test_wallet_markout.py`
  - 23 tests covering address normalization, maker/taker dual attribution, zero-fabrication invariant on public feeds, markout mathematics, tolerance windows, crossed orderbook rejection, minimum sample size guardrails, outbox batching, and FastAPI endpoints.
  - Result: **23/23 PASSED** in 3.48s.
- **Full Repository Test Suite**: `python -m unittest discover -s tests`
  - Result: **72/72 PASSED** (1 skipped) in 96.82s with zero regressions across Phase 1, 2, 3, 4, and 5.
- **C++ Engine Assertion Suite**: `python scripts/build_engine.py --test`
  - Result: **PASSED** (all microstructure, tick arithmetic, and Cont-Kukanov-Stoikov OFI assertions verified).
- **Vite & TypeScript Frontend Build**: `npm --prefix frontend run build`
  - Result: **PASSED** (0 errors, 1,578 modules bundled in 20.93s).


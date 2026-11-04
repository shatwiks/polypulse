# API Reference & WebSocket Protocols

The PolyPulse API is served by FastAPI on port 8000. It provides REST endpoints for orderbook state, cross-venue spreads, wallet analytics, and system diagnostics, alongside WebSocket feeds for real-time streaming.

Interactive OpenAPI/Swagger documentation is accessible at `http://localhost:8000/docs`.

---

## 1. REST Endpoints

### 1.1 List Markets
Retrieves all currently tracked prediction markets and their active quotes.

- **URL**: `/api/v1/markets`
- **Method**: `GET`
- **Response**:
```json
{
  "mode": "demo",
  "markets": [
    {
      "venue": "polymarket",
      "market_id": "demo-poly-election",
      "bids": [[0.4910, 20.0], [0.4860, 28.0]],
      "asks": [[0.5010, 18.0], [0.5060, 25.0]],
      "best_bid": 0.4910,
      "best_ask": 0.5010,
      "sequence": 142,
      "buy_vwap_10": 0.5010,
      "imbalance": 0.0526
    }
  ]
}
```

---

### 1.2 Get Market Orderbook
Fetches the full sorted price-level depth for a specific venue and market.

- **URL**: `/api/v1/markets/{market_id}/orderbook`
- **Method**: `GET`
- **Query Parameters**:
  - `venue` (required, string): `"polymarket"` or `"kalshi"`
- **Response (`200 OK`)**:
```json
{
  "venue": "polymarket",
  "market_id": "demo-poly-election",
  "bids": [
    [0.4910, 20.0],
    [0.4860, 28.0]
  ],
  "asks": [
    [0.5010, 18.0],
    [0.5060, 25.0]
  ],
  "best_bid": 0.4910,
  "best_ask": 0.5010,
  "sequence": 142,
  "buy_vwap_10": 0.5010,
  "imbalance": 0.0526
}
```
- **Error (`404 Not Found`)**:
```json
{
  "detail": "Market book unavailable"
}
```

---

### 1.3 Cross-Venue Spreads
Calculates gross quote differences between verified equivalent contract pairs.

- **URL**: `/api/v1/spreads`
- **Method**: `GET`
- **Response**:
```json
[
  {
    "name": "Election outcome · YES",
    "polymarket": { "venue": "polymarket", "best_bid": 0.4910, "best_ask": 0.5010 },
    "kalshi": { "venue": "kalshi", "best_bid": 0.4980, "best_ask": 0.5080 },
    "buy_polymarket_sell_kalshi": -0.0030,
    "buy_kalshi_sell_polymarket": -0.0170,
    "note": "Gross quote difference before fees, depth and settlement risk"
  }
]
```

---

### 1.4 System Health & Pipeline Counters
Provides feed statuses, journal statistics, sequence gaps, and recovery metrics.

- **URL**: `/api/v1/health` (also aliased at `/api/v1/metrics`)
- **Method**: `GET`
- **Response**:
```json
{
  "mode": "demo",
  "feeds": {
    "polymarket": "demo",
    "kalshi": "demo"
  },
  "storage": {
    "clickhouse": "connected"
  },
  "uptime_seconds": 1284,
  "journal": {
    "events": 24890,
    "pending_export": 0,
    "snapshots": 12445
  },
  "counters": {
    "events": 24890,
    "gaps": 0,
    "recoveries": 0,
    "duplicates": 12
  }
}
```

---

### 1.5 Ingest Trade (Demo Mode Only)
Imports an observed trade execution into the local journal. Disabled in live mode.

- **URL**: `/api/v1/trades`
- **Method**: `POST`
- **Request Body**:
```json
{
  "trade_id": "trade-001",
  "wallet": "0x71c...a29",
  "venue": "polymarket",
  "market_id": "demo-poly-election",
  "side": "buy",
  "price": 0.4950,
  "quantity": 25.0,
  "executed_at": 1730721600.0
}
```
- **Response (`201 Created`)**:
```json
{
  "inserted": true
}
```
- **Error (`403 Forbidden`)**: Raised if `POLYPULSE_MODE=live`.

---

### 1.6 Wallet Markout Scoring
Calculates post-trade markout trajectory for a given wallet address.

- **URL**: `/api/v1/wallets/{address}/markouts`
- **Method**: `GET`
- **Query Parameters**:
  - `horizon` (optional, integer): `5`, `30`, or `60`
  - `venue` (optional, string): `"polymarket"` or `"kalshi"`
  - `market_id` (optional, string): contract identifier
- **Response**:
```json
{
  "wallet": "demo-wallet-01",
  "summary": [
    {
      "horizon_seconds": 5,
      "trade_count": 12,
      "available_count": 11,
      "average_markout": 0.0082,
      "positive_percentage": 72.7
    },
    {
      "horizon_seconds": 30,
      "trade_count": 12,
      "available_count": 9,
      "average_markout": 0.0145,
      "positive_percentage": 66.7
    },
    {
      "horizon_seconds": 60,
      "trade_count": 12,
      "available_count": 7,
      "average_markout": -0.0021,
      "positive_percentage": 42.9
    }
  ],
  "entries": [
    {
      "trade_id": "demo-8f2e9...",
      "horizon_seconds": 5,
      "future_mid": 0.5050,
      "markout": 0.0100,
      "available": true
    }
  ]
}
```

---

## 2. WebSocket Streaming Protocols

### 2.1 Market Stream (`/ws/markets`)
Streams live orderbook updates and periodic heartbeat health diagnostics.

- **URL**: `ws://localhost:8000/ws/markets`
- **Connection Handshake**:
  Immediately upon connection, the server sends an `initial` frame with all current orderbooks:
  ```json
  {
    "type": "initial",
    "data": {
      "mode": "demo",
      "markets": [ ... ]
    }
  }
  ```
- **Delta Broadcast Frame**:
  Sent whenever an orderbook level or snapshot updates:
  ```json
  {
    "type": "book",
    "data": {
      "venue": "polymarket",
      "market_id": "demo-poly-election",
      "bids": [[0.4910, 20.0]],
      "asks": [[0.5010, 18.0]],
      "best_bid": 0.4910,
      "best_ask": 0.5010,
      "sequence": 143,
      "buy_vwap_10": 0.5010,
      "imbalance": 0.0526
    }
  }
  ```
- **Heartbeat / Health Frame**:
  Sent every 5 seconds of channel idle time:
  ```json
  {
    "type": "health",
    "data": { ... }
  }
  ```

---

### 2.2 Wallet Stream (`/ws/wallets`)
Streams periodic summaries of tracked wallet counts every 5 seconds.

- **URL**: `ws://localhost:8000/ws/wallets`
- **Payload**:
  ```json
  {
    "type": "wallets",
    "data": [
      { "wallet": "demo-wallet-01", "trade_count": 28 },
      { "wallet": "demo-wallet-02", "trade_count": 14 }
    ]
  }
  ```

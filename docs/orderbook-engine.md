# C++20 Orderbook Engine

The core orderbook calculation engine is implemented in modern C++20. It provides ultra-low latency, deterministic tick-level orderbook tracking, top-of-book quote extraction, volume-weighted average price (VWAP) calculation, and order-flow imbalance (OFI) evaluation.

![Orderbook Flow](assets/orderbook-flow.svg)

---

## 1. Memory Layout & Cache Locality

### Structure Definitions
```cpp
namespace polypulse {
struct PriceLevel { 
    std::int64_t price_ticks;    // 8 bytes (0 .. 10,000)
    std::int64_t quantity_units; // 8 bytes (scaled by 10,000)
};

struct Quote { 
    std::int64_t bid_ticks; 
    std::int64_t ask_ticks; 
    std::int64_t bid_units; 
    std::int64_t ask_units; 
    std::uint64_t sequence; 
};

class OrderBook {
 public:
  bool snapshot(const PriceLevel* bids, std::size_t bid_count, 
                const PriceLevel* asks, std::size_t ask_count, std::uint64_t sequence);
  bool set_level(bool bid, std::int64_t price_ticks, std::int64_t quantity_units, std::uint64_t sequence);
  Quote quote() const;
  std::size_t levels(bool bid, PriceLevel* out, std::size_t capacity) const;
  double vwap(bool buy, std::int64_t quantity_units) const;
  double imbalance() const;
 private:
  std::vector<PriceLevel> bids_, asks_;
  std::uint64_t sequence_ = 0;
};
}
```

### Contiguous Vector vs. Node-Based Trees
Traditional financial matching engines with tens of thousands of active price levels often use trees (such as red-black trees in `std::map`) or radix trees to achieve $O(\log N)$ insertions without shifting memory.

In prediction markets:
1. Binary outcome contracts have a strict price range: $[0.00, 1.00]$ dollars, represented as $[0, 10\,000]$ integer ticks.
2. The number of populated price levels rarely exceeds 15–30 distinct price points on either side.
3. Each `PriceLevel` instance is exactly **16 bytes**.
4. Modern CPU architectures utilize a **64-byte L1 cache line**. A single cache line fetch pulls 4 contiguous price levels into L1 cache simultaneously.
5. In contrast, `std::map` incurs a 32-to-48 byte overhead per node (left child, right child, parent pointer, color flag), scattering nodes across dynamic heap memory and triggering cache misses on every branch traversal.

---

## 2. Level Update Mechanics (`set_level`)

Incremental level updates use `std::lower_bound` with a custom ordering lambda:

```cpp
bool OrderBook::set_level(bool bid, std::int64_t price, std::int64_t quantity, std::uint64_t seq) {
  // Sequence Monotonicity Invariant
  if (seq <= sequence_ || price < 0 || price > 10000 || quantity < 0) return false;

  auto& levels = bid ? bids_ : asks_;
  auto before = [bid](PriceLevel level, std::int64_t target) {
    return bid ? level.price_ticks > target : level.price_ticks < target;
  };

  auto it = std::lower_bound(levels.begin(), levels.end(), price, before);

  if (it != levels.end() && it->price_ticks == price) {
    if (quantity == 0) {
      levels.erase(it); // Level cleared
    } else {
      it->quantity_units = quantity; // Level updated in-place
    }
  } else if (quantity > 0) {
    levels.insert(it, {price, quantity}); // New level inserted in order
  }

  sequence_ = seq;
  return true;
}
```

### Invariants Enforced
- **Strict Sequence Monotonicity**: Any update where $\text{seq} \le \text{current\_seq}$ is rejected immediately.
- **Bound Checking**: Prices must lie within $[0, 10\,000]$ ticks.
- **Deletion by Zero Size**: A quantity of zero removes the price level from the orderbook.

---

## 3. Analytical Kernels

### Volume-Weighted Average Price (VWAP)
Calculates the exact average price required to execute a specified volume against the available orderbook depth:

```cpp
double OrderBook::vwap(bool buy, std::int64_t quantity) const {
  if (quantity <= 0) return NAN;
  const auto& levels = buy ? asks_ : bids_;
  std::int64_t remaining = quantity;
  long double total = 0;

  for (auto level : levels) {
    auto filled = std::min(remaining, level.quantity_units);
    total += static_cast<long double>(filled) * level.price_ticks;
    remaining -= filled;
    if (!remaining) break;
  }

  // Returns NaN if the available liquidity cannot satisfy the requested quantity
  return remaining ? NAN : static_cast<double>(total / quantity / 10000.0L);
}
```

- **Overflow Prevention**: Accumulator `total` uses 80-bit `long double` to eliminate the risk of 64-bit integer multiplication overflow when processing high-volume contract orders.
- **Liquidity Guard**: Returns `NAN` when cumulative depth is insufficient.

### Order-Flow Imbalance (OFI)
Measures prevailing quote pressure between bids and asks:

$$\text{OFI} = \frac{\sum Q_{\text{bids}} - \sum Q_{\text{asks}}}{\sum Q_{\text{bids}} + \sum Q_{\text{asks}}}$$

```cpp
double OrderBook::imbalance() const {
  long double bid = 0, ask = 0;
  for (auto p : bids_) bid += p.quantity_units;
  for (auto p : asks_) ask += p.quantity_units;
  return bid + ask == 0 ? 0 : static_cast<double>((bid - ask) / (bid + ask));
}
```
- A score of `+1.0` denotes a 100% bid-heavy book (buying pressure).
- A score of `-1.0` denotes a 100% ask-heavy book (selling pressure).

---

## 4. C Foreign Function Interface (FFI)

The engine compiles into a native shared library (`polypulse.dll` on Windows, `polypulse.so` on Linux, `polypulse.dylib` on macOS) with clean C ABI export bindings:

| Exported C Function | Signature | Description |
| :--- | :--- | :--- |
| `pp_create` | `void* pp_create()` | Allocates a new heap instance of `OrderBook` |
| `pp_destroy` | `void pp_destroy(void* book)` | Safely deallocates book memory |
| `pp_snapshot` | `int pp_snapshot(void*, const PriceLevel*, size_t, const PriceLevel*, size_t, uint64_t)` | Ingests full snapshot |
| `pp_set_level` | `int pp_set_level(void*, int bid, int64_t price, int64_t qty, uint64_t seq)` | Applies delta update |
| `pp_quote` | `Quote pp_quote(void*)` | Returns zero-alloc top-of-book quote |
| `pp_levels` | `size_t pp_levels(void*, int bid, PriceLevel* out, size_t cap)` | Copies sorted levels to buffer |
| `pp_vwap` | `double pp_vwap(void*, int buy, int64_t qty)` | Computes VWAP across depth |
| `pp_imbalance` | `double pp_imbalance(void*)` | Computes OFI metric |

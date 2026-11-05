#pragma once
#include <cstddef>
#include <cstdint>
#include <vector>

namespace polypulse {

constexpr std::int64_t kPriceScale = 1'000'000;      // 6 decimals: 1.0 = 1,000,000 ticks
constexpr std::int64_t kQuantityScale = 1'000'000;   // 6 decimals: 1.0 = 1,000,000 units

struct alignas(16) PriceLevel {
    std::int64_t price_ticks;
    std::int64_t quantity_units;
};

struct Quote {
    std::int64_t bid_ticks;
    std::int64_t ask_ticks;
    std::int64_t bid_units;
    std::int64_t ask_units;
    std::uint64_t sequence;
};

struct SpreadAnalytics {
    std::int64_t best_bid;
    std::int64_t best_ask;
    std::int64_t mid_price;
    std::int64_t spread_ticks;
    double spread_bps;
    int is_crossed;
};

struct CrossSpreadAnalytics {
    std::int64_t buy_1_sell_2_ticks;  // venue 2 bid - venue 1 ask
    std::int64_t buy_2_sell_1_ticks;  // venue 1 bid - venue 2 ask
    double buy_1_sell_2_bps;
    double buy_2_sell_1_bps;
    int executable_buy_1_sell_2;
    int executable_buy_2_sell_1;
};

bool has_avx2();

class OrderBook {
 public:
  OrderBook();

  bool snapshot(const PriceLevel* bids, std::size_t bid_count,
                const PriceLevel* asks, std::size_t ask_count,
                std::uint64_t sequence);

  bool set_level(bool bid, std::int64_t price_ticks, std::int64_t quantity_units, std::uint64_t sequence);

  Quote quote() const;
  std::size_t levels(bool bid, PriceLevel* out, std::size_t capacity) const;
  std::size_t capacity(bool bid) const;

  double vwap(bool buy, std::int64_t quantity_units) const;
  std::int64_t vwap_ticks(bool buy, std::int64_t quantity_units) const;
  double imbalance() const;

  std::int64_t ofi() const { return last_ofi_; }
  std::int64_t cumulative_ofi() const { return cumulative_ofi_; }

  SpreadAnalytics spread_analytics() const;
  static CrossSpreadAnalytics cross_spread(const OrderBook& book1, const OrderBook& book2);

  void batch_vwap(bool buy, const std::int64_t* quantities, double* out_vwap, std::size_t count, bool use_avx2 = true) const;

  void clear();

 private:
  Quote compute_quote(std::uint64_t seq) const;

  std::vector<PriceLevel> bids_;
  std::vector<PriceLevel> asks_;
  std::uint64_t sequence_ = 0;
  Quote prev_quote_{-1, -1, 0, 0, 0};
  std::int64_t last_ofi_ = 0;
  std::int64_t cumulative_ofi_ = 0;
};

}  // namespace polypulse

extern "C" {
// Phase 1 / Phase 2 ABI (100% Backward Compatible)
void* pp_create();
void pp_destroy(void* book);
int pp_snapshot(void* book, const polypulse::PriceLevel* bids, std::size_t bid_count,
                const polypulse::PriceLevel* asks, std::size_t ask_count, std::uint64_t sequence);
int pp_set_level(void* book, int bid, std::int64_t price, std::int64_t quantity, std::uint64_t sequence);
polypulse::Quote pp_quote(void* book);
std::size_t pp_levels(void* book, int bid, polypulse::PriceLevel* out, std::size_t capacity);
double pp_vwap(void* book, int buy, std::int64_t quantity);
double pp_imbalance(void* book);

// Phase 3 Extended Microstructure & AVX2 ABI
std::int64_t pp_vwap_ticks(void* book, int buy, std::int64_t quantity);
std::int64_t pp_ofi(void* book);
std::int64_t pp_cumulative_ofi(void* book);
void pp_spread_analytics(void* book, polypulse::SpreadAnalytics* out);
int pp_cross_spread_analytics(void* book1, void* book2, polypulse::CrossSpreadAnalytics* out);
int pp_batch_vwap(void* book, int buy, const std::int64_t* quantities, double* out_vwap, std::size_t count);
int pp_batch_vwap_scalar(void* book, int buy, const std::int64_t* quantities, double* out_vwap, std::size_t count);
int pp_batch_vwap_avx2(void* book, int buy, const std::int64_t* quantities, double* out_vwap, std::size_t count);
int pp_has_avx2();
std::size_t pp_capacity(void* book, int bid);
}

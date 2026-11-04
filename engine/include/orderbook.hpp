#pragma once
#include <cstddef>
#include <cstdint>
#include <vector>

namespace polypulse {
struct PriceLevel { std::int64_t price_ticks; std::int64_t quantity_units; };
struct Quote { std::int64_t bid_ticks; std::int64_t ask_ticks; std::int64_t bid_units; std::int64_t ask_units; std::uint64_t sequence; };

class OrderBook {
 public:
  bool snapshot(const PriceLevel* bids, std::size_t bid_count, const PriceLevel* asks, std::size_t ask_count, std::uint64_t sequence);
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

extern "C" {
void* pp_create();
void pp_destroy(void* book);
int pp_snapshot(void* book, const polypulse::PriceLevel* bids, std::size_t bid_count,
                const polypulse::PriceLevel* asks, std::size_t ask_count, std::uint64_t sequence);
int pp_set_level(void* book, int bid, std::int64_t price, std::int64_t quantity, std::uint64_t sequence);
polypulse::Quote pp_quote(void* book);
std::size_t pp_levels(void* book, int bid, polypulse::PriceLevel* out, std::size_t capacity);
double pp_vwap(void* book, int buy, std::int64_t quantity);
double pp_imbalance(void* book);
}

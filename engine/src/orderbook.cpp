#include "orderbook.hpp"
#include <algorithm>
#include <cmath>
#include <new>

namespace polypulse {
namespace {
bool valid(PriceLevel p) { return p.price_ticks >= 0 && p.price_ticks <= 10000 && p.quantity_units > 0; }
void normalize(std::vector<PriceLevel>& levels, bool bid) {
  std::sort(levels.begin(), levels.end(), [bid](auto a, auto b) {
    return bid ? a.price_ticks > b.price_ticks : a.price_ticks < b.price_ticks;
  });
  auto it = std::unique(levels.begin(), levels.end(), [](auto a, auto b) { return a.price_ticks == b.price_ticks; });
  levels.erase(it, levels.end());
}
}
bool OrderBook::snapshot(const PriceLevel* bids, std::size_t bn, const PriceLevel* asks, std::size_t an, std::uint64_t seq) {
  if ((bn && !bids) || (an && !asks) || seq < sequence_) return false;
  for (std::size_t i=0; i<bn; ++i) if (!valid(bids[i])) return false;
  for (std::size_t i=0; i<an; ++i) if (!valid(asks[i])) return false;
  if (bn) bids_.assign(bids, bids+bn); else bids_.clear();
  if (an) asks_.assign(asks, asks+an); else asks_.clear();
  normalize(bids_, true); normalize(asks_, false); sequence_ = seq;
  return true;
}
bool OrderBook::set_level(bool bid, std::int64_t price, std::int64_t quantity, std::uint64_t seq) {
  if (seq <= sequence_ || price < 0 || price > 10000 || quantity < 0) return false;
  auto& levels = bid ? bids_ : asks_;
  auto before = [bid](PriceLevel level, std::int64_t target) { return bid ? level.price_ticks > target : level.price_ticks < target; };
  auto it = std::lower_bound(levels.begin(), levels.end(), price, before);
  if (it != levels.end() && it->price_ticks == price) {
    if (quantity == 0) levels.erase(it); else it->quantity_units = quantity;
  } else if (quantity > 0) levels.insert(it, {price, quantity});
  sequence_ = seq;
  return true;
}
Quote OrderBook::quote() const {
  return {bids_.empty() ? -1 : bids_.front().price_ticks,
          asks_.empty() ? -1 : asks_.front().price_ticks,
          bids_.empty() ? 0 : bids_.front().quantity_units,
          asks_.empty() ? 0 : asks_.front().quantity_units, sequence_};
}
std::size_t OrderBook::levels(bool bid, PriceLevel* out, std::size_t cap) const {
  const auto& levels = bid ? bids_ : asks_;
  if (out) std::copy_n(levels.begin(), std::min(cap, levels.size()), out);
  return levels.size();
}
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
  return remaining ? NAN : static_cast<double>(total / quantity / 10000.0L);
}
double OrderBook::imbalance() const {
  long double bid=0, ask=0;
  for (auto p : bids_) bid += p.quantity_units;
  for (auto p : asks_) ask += p.quantity_units;
  return bid+ask == 0 ? 0 : static_cast<double>((bid-ask)/(bid+ask));
}
}

extern "C" {
void* pp_create() { return new (std::nothrow) polypulse::OrderBook(); }
void pp_destroy(void* p) { delete static_cast<polypulse::OrderBook*>(p); }
int pp_snapshot(void* p, const polypulse::PriceLevel* b, std::size_t bn, const polypulse::PriceLevel* a, std::size_t an, std::uint64_t s) { return p && static_cast<polypulse::OrderBook*>(p)->snapshot(b,bn,a,an,s); }
int pp_set_level(void* p, int b, std::int64_t pr, std::int64_t q, std::uint64_t s) { return p && static_cast<polypulse::OrderBook*>(p)->set_level(b != 0,pr,q,s); }
polypulse::Quote pp_quote(void* p) { return p ? static_cast<polypulse::OrderBook*>(p)->quote() : polypulse::Quote{-1,-1,0,0,0}; }
std::size_t pp_levels(void* p, int b, polypulse::PriceLevel* out, std::size_t cap) { return p ? static_cast<polypulse::OrderBook*>(p)->levels(b != 0,out,cap) : 0; }
double pp_vwap(void* p, int b, std::int64_t q) { return p ? static_cast<polypulse::OrderBook*>(p)->vwap(b != 0,q) : NAN; }
double pp_imbalance(void* p) { return p ? static_cast<polypulse::OrderBook*>(p)->imbalance() : NAN; }
}

#include "orderbook.hpp"
#include <algorithm>
#include <cmath>
#include <cstring>
#include <new>
#include <immintrin.h>

namespace polypulse {

namespace {

inline bool valid(const PriceLevel& p) {
  return p.price_ticks >= 0 && p.price_ticks <= kPriceScale && p.quantity_units > 0;
}

std::int64_t compute_ofi_delta(const Quote& prev, const Quote& curr) {
  if (prev.sequence == 0 || curr.sequence == 0) return 0;

  // Cont, Kukanov and Stoikov (2014) definition:
  // Bid contribution:
  // - Bid price improves: current bid quantity.
  // - Bid price unchanged: current quantity minus previous quantity.
  // - Bid price declines: negative previous bid quantity.
  std::int64_t bid_contrib = 0;
  bool prev_has_bid = (prev.bid_ticks >= 0 && prev.bid_units > 0);
  bool curr_has_bid = (curr.bid_ticks >= 0 && curr.bid_units > 0);

  if (prev_has_bid && curr_has_bid) {
    if (curr.bid_ticks > prev.bid_ticks) {
      bid_contrib = curr.bid_units;
    } else if (curr.bid_ticks == prev.bid_ticks) {
      bid_contrib = curr.bid_units - prev.bid_units;
    } else {
      bid_contrib = -prev.bid_units;
    }
  } else if (curr_has_bid) {
    bid_contrib = curr.bid_units;
  } else if (prev_has_bid) {
    bid_contrib = -prev.bid_units;
  }

  // Ask contribution:
  // - Ask price improves (moves lower): negative current ask quantity.
  // - Ask price unchanged: previous ask quantity minus current ask quantity.
  // - Ask price worsens (moves higher): previous ask quantity.
  std::int64_t ask_contrib = 0;
  bool prev_has_ask = (prev.ask_ticks >= 0 && prev.ask_units > 0);
  bool curr_has_ask = (curr.ask_ticks >= 0 && curr.ask_units > 0);

  if (prev_has_ask && curr_has_ask) {
    if (curr.ask_ticks < prev.ask_ticks) {
      ask_contrib = -curr.ask_units;
    } else if (curr.ask_ticks == prev.ask_ticks) {
      ask_contrib = prev.ask_units - curr.ask_units;
    } else {
      ask_contrib = prev.ask_units;
    }
  } else if (curr_has_ask) {
    ask_contrib = -curr.ask_units;
  } else if (prev_has_ask) {
    ask_contrib = prev.ask_units;
  }

  // Total OFI = bid contribution + ask contribution
  return bid_contrib + ask_contrib;
}

}  // namespace

bool has_avx2() {
#if defined(__x86_64__) || defined(_M_X64)
  __builtin_cpu_init();
  return __builtin_cpu_supports("avx2");
#else
  return false;
#endif
}

__attribute__((target("avx2")))
void batch_vwap_avx2_impl(const PriceLevel* levels, std::size_t level_count,
                          const std::int64_t* quantities, double* out_vwap, std::size_t count) {
  std::size_t i = 0;
  for (; i + 4 <= count; i += 4) {
    __m256i rem = _mm256_loadu_si256(reinterpret_cast<const __m256i*>(quantities + i));
    __m256i total_val = _mm256_setzero_si256();

    for (std::size_t l = 0; l < level_count; ++l) {
      __m256i lvl_q = _mm256_set1_epi64x(levels[l].quantity_units);
      __m256i lvl_p = _mm256_set1_epi64x(levels[l].price_ticks);

      __m256i cmp = _mm256_cmpgt_epi64(rem, lvl_q);
      __m256i fill = _mm256_blendv_epi8(rem, lvl_q, cmp);

      // 64-bit multiply of fill by lvl_p:
      // fill = (fill_hi * 2^32) + fill_lo
      // fill * lvl_p = (fill_lo * lvl_p) + ((fill_hi * lvl_p) << 32)
      __m256i prod_lo = _mm256_mul_epu32(fill, lvl_p);
      __m256i fill_hi = _mm256_srli_epi64(fill, 32);
      __m256i prod_hi = _mm256_mul_epu32(fill_hi, lvl_p);
      __m256i prod_hi_shifted = _mm256_slli_epi64(prod_hi, 32);
      __m256i prod = _mm256_add_epi64(prod_lo, prod_hi_shifted);

      total_val = _mm256_add_epi64(total_val, prod);
      rem = _mm256_sub_epi64(rem, fill);

      if (_mm256_testz_si256(rem, rem)) break;
    }

    alignas(32) std::int64_t rem_arr[4];
    alignas(32) std::int64_t val_arr[4];
    _mm256_storeu_si256(reinterpret_cast<__m256i*>(rem_arr), rem);
    _mm256_storeu_si256(reinterpret_cast<__m256i*>(val_arr), total_val);

    for (int k = 0; k < 4; ++k) {
      if (rem_arr[k] == 0 && quantities[i + k] > 0) {
        out_vwap[i + k] = static_cast<double>(val_arr[k]) /
                          (static_cast<double>(quantities[i + k]) * kPriceScale);
      } else {
        out_vwap[i + k] = NAN;
      }
    }
  }

  // Scalar tail
  for (; i < count; ++i) {
    std::int64_t q = quantities[i];
    if (q <= 0) { out_vwap[i] = NAN; continue; }
    std::int64_t rem_s = q;
    unsigned __int128 tot = 0;
    for (std::size_t l = 0; l < level_count; ++l) {
      std::int64_t filled = std::min(rem_s, levels[l].quantity_units);
      tot += static_cast<unsigned __int128>(filled) * levels[l].price_ticks;
      rem_s -= filled;
      if (!rem_s) break;
    }
    out_vwap[i] = (rem_s == 0)
        ? (static_cast<double>(tot) / (static_cast<double>(q) * kPriceScale))
        : NAN;
  }
}

void batch_vwap_scalar_impl(const PriceLevel* levels, std::size_t level_count,
                            const std::int64_t* quantities, double* out_vwap, std::size_t count) {
  for (std::size_t i = 0; i < count; ++i) {
    std::int64_t q = quantities[i];
    if (q <= 0) { out_vwap[i] = NAN; continue; }
    std::int64_t rem_s = q;
    unsigned __int128 tot = 0;
    for (std::size_t l = 0; l < level_count; ++l) {
      std::int64_t filled = std::min(rem_s, levels[l].quantity_units);
      tot += static_cast<unsigned __int128>(filled) * levels[l].price_ticks;
      rem_s -= filled;
      if (!rem_s) break;
    }
    out_vwap[i] = (rem_s == 0)
        ? (static_cast<double>(tot) / (static_cast<double>(q) * kPriceScale))
        : NAN;
  }
}

OrderBook::OrderBook() {
  bids_.reserve(128);
  asks_.reserve(128);
}

void OrderBook::clear() {
  bids_.clear();
  asks_.clear();
  sequence_ = 0;
  prev_quote_ = Quote{-1, -1, 0, 0, 0};
  last_ofi_ = 0;
  cumulative_ofi_ = 0;
}

bool OrderBook::snapshot(const PriceLevel* bids, std::size_t bn,
                        const PriceLevel* asks, std::size_t an,
                        std::uint64_t seq) {
  if ((bn && !bids) || (an && !asks) || (sequence_ && seq <= sequence_)) return false;
  for (std::size_t i = 0; i < bn; ++i) if (!valid(bids[i])) return false;
  for (std::size_t i = 0; i < an; ++i) if (!valid(asks[i])) return false;

  bids_.assign(bids, bids + bn);
  asks_.assign(asks, asks + an);

  std::sort(bids_.begin(), bids_.end(), [](const PriceLevel& a, const PriceLevel& b) {
    return a.price_ticks > b.price_ticks;
  });
  std::sort(asks_.begin(), asks_.end(), [](const PriceLevel& a, const PriceLevel& b) {
    return a.price_ticks < b.price_ticks;
  });

  if (std::adjacent_find(bids_.begin(), bids_.end(), [](const auto& a, const auto& b) {
        return a.price_ticks == b.price_ticks;
      }) != bids_.end()) {
    bids_.clear();
    asks_.clear();
    return false;
  }

  if (std::adjacent_find(asks_.begin(), asks_.end(), [](const auto& a, const auto& b) {
        return a.price_ticks == b.price_ticks;
      }) != asks_.end()) {
    bids_.clear();
    asks_.clear();
    return false;
  }

  Quote new_q = compute_quote(seq);
  if (prev_quote_.sequence > 0) {
    last_ofi_ = compute_ofi_delta(prev_quote_, new_q);
    cumulative_ofi_ += last_ofi_;
  } else {
    last_ofi_ = 0;
  }
  prev_quote_ = new_q;
  sequence_ = seq;
  return true;
}

bool OrderBook::set_level(bool bid, std::int64_t price, std::int64_t quantity, std::uint64_t seq) {
  if (seq <= sequence_ || price < 0 || price > kPriceScale || quantity < 0) return false;
  auto& levels = bid ? bids_ : asks_;

  auto before = [bid](const PriceLevel& level, std::int64_t target) {
    return bid ? level.price_ticks > target : level.price_ticks < target;
  };
  auto it = std::lower_bound(levels.begin(), levels.end(), price, before);

  if (it != levels.end() && it->price_ticks == price) {
    if (quantity == 0) {
      levels.erase(it);
    } else {
      it->quantity_units = quantity;
    }
  } else if (quantity > 0) {
    levels.insert(it, PriceLevel{price, quantity});
  }

  Quote new_q = compute_quote(seq);
  if (prev_quote_.sequence > 0) {
    last_ofi_ = compute_ofi_delta(prev_quote_, new_q);
    cumulative_ofi_ += last_ofi_;
  } else {
    last_ofi_ = 0;
  }
  prev_quote_ = new_q;
  sequence_ = seq;
  return true;
}

Quote OrderBook::quote() const {
  return compute_quote(sequence_);
}

Quote OrderBook::compute_quote(std::uint64_t seq) const {
  return {
    bids_.empty() ? -1 : bids_.front().price_ticks,
    asks_.empty() ? -1 : asks_.front().price_ticks,
    bids_.empty() ? 0 : bids_.front().quantity_units,
    asks_.empty() ? 0 : asks_.front().quantity_units,
    seq
  };
}

std::size_t OrderBook::levels(bool bid, PriceLevel* out, std::size_t cap) const {
  const auto& levels = bid ? bids_ : asks_;
  if (out && cap > 0) {
    std::copy_n(levels.begin(), std::min(cap, levels.size()), out);
  }
  return levels.size();
}

std::size_t OrderBook::capacity(bool bid) const {
  return bid ? bids_.capacity() : asks_.capacity();
}

double OrderBook::vwap(bool buy, std::int64_t quantity) const {
  if (quantity <= 0) return NAN;
  const auto& levels = buy ? asks_ : bids_;
  std::int64_t remaining = quantity;
  unsigned __int128 total_val = 0;

  for (const auto& level : levels) {
    std::int64_t filled = std::min(remaining, level.quantity_units);
    total_val += static_cast<unsigned __int128>(filled) * level.price_ticks;
    remaining -= filled;
    if (!remaining) break;
  }

  if (remaining > 0) return NAN;
  return static_cast<double>(total_val) / (static_cast<double>(quantity) * kPriceScale);
}

std::int64_t OrderBook::vwap_ticks(bool buy, std::int64_t quantity) const {
  if (quantity <= 0) return -1;
  const auto& levels = buy ? asks_ : bids_;
  std::int64_t remaining = quantity;
  unsigned __int128 total_val = 0;

  for (const auto& level : levels) {
    std::int64_t filled = std::min(remaining, level.quantity_units);
    total_val += static_cast<unsigned __int128>(filled) * level.price_ticks;
    remaining -= filled;
    if (!remaining) break;
  }

  if (remaining > 0) return -1;
  return static_cast<std::int64_t>(total_val / quantity);
}

double OrderBook::imbalance() const {
  std::int64_t bid_total = 0, ask_total = 0;
  for (const auto& p : bids_) bid_total += p.quantity_units;
  for (const auto& p : asks_) ask_total += p.quantity_units;
  std::int64_t denom = bid_total + ask_total;
  if (denom == 0) return 0.0;
  return static_cast<double>(bid_total - ask_total) / static_cast<double>(denom);
}

SpreadAnalytics OrderBook::spread_analytics() const {
  SpreadAnalytics sa{};
  sa.best_bid = bids_.empty() ? -1 : bids_.front().price_ticks;
  sa.best_ask = asks_.empty() ? -1 : asks_.front().price_ticks;

  if (sa.best_bid < 0 || sa.best_ask < 0) {
    sa.mid_price = -1;
    sa.spread_ticks = -1;
    sa.spread_bps = 0.0;
    sa.is_crossed = 0;
  } else {
    sa.mid_price = (sa.best_bid + sa.best_ask) / 2;
    sa.spread_ticks = sa.best_ask - sa.best_bid;
    sa.is_crossed = (sa.best_bid > sa.best_ask) ? 1 : 0;
    sa.spread_bps = (sa.mid_price > 0)
        ? (static_cast<double>(sa.spread_ticks) / static_cast<double>(sa.mid_price) * 10000.0)
        : 0.0;
  }
  return sa;
}

CrossSpreadAnalytics OrderBook::cross_spread(const OrderBook& book1, const OrderBook& book2) {
  CrossSpreadAnalytics res{};
  Quote q1 = book1.quote();
  Quote q2 = book2.quote();

  // Validate that books have populated quotes and are NOT internally crossed
  bool q1_valid = (q1.bid_ticks >= 0 && q1.ask_ticks >= 0 && q1.bid_ticks <= q1.ask_ticks);
  bool q2_valid = (q2.bid_ticks >= 0 && q2.ask_ticks >= 0 && q2.bid_ticks <= q2.ask_ticks);

  // Buy on Venue 1 (at ask_1), Sell on Venue 2 (at bid_2)
  if (q1.ask_ticks >= 0 && q1.ask_units > 0 && q2.bid_ticks >= 0 && q2.bid_units > 0) {
    res.buy_1_sell_2_ticks = q2.bid_ticks - q1.ask_ticks;
    res.executable_buy_1_sell_2 = (res.buy_1_sell_2_ticks > 0 && q1_valid && q2_valid) ? 1 : 0;
    double mid1 = (q1.bid_ticks >= 0) ? (q1.bid_ticks + q1.ask_ticks) / 2.0 : static_cast<double>(q1.ask_ticks);
    res.buy_1_sell_2_bps = (mid1 > 0) ? (static_cast<double>(res.buy_1_sell_2_ticks) / mid1 * 10000.0) : 0.0;
  } else {
    res.buy_1_sell_2_ticks = 0;
    res.executable_buy_1_sell_2 = 0;
    res.buy_1_sell_2_bps = 0.0;
  }

  // Buy on Venue 2 (at ask_2), Sell on Venue 1 (at bid_1)
  if (q2.ask_ticks >= 0 && q2.ask_units > 0 && q1.bid_ticks >= 0 && q1.bid_units > 0) {
    res.buy_2_sell_1_ticks = q1.bid_ticks - q2.ask_ticks;
    res.executable_buy_2_sell_1 = (res.buy_2_sell_1_ticks > 0 && q1_valid && q2_valid) ? 1 : 0;
    double mid2 = (q2.bid_ticks >= 0) ? (q2.bid_ticks + q2.ask_ticks) / 2.0 : static_cast<double>(q2.ask_ticks);
    res.buy_2_sell_1_bps = (mid2 > 0) ? (static_cast<double>(res.buy_2_sell_1_ticks) / mid2 * 10000.0) : 0.0;
  } else {
    res.buy_2_sell_1_ticks = 0;
    res.executable_buy_2_sell_1 = 0;
    res.buy_2_sell_1_bps = 0.0;
  }

  return res;
}

void OrderBook::batch_vwap(bool buy, const std::int64_t* quantities,
                           double* out_vwap, std::size_t count, bool use_avx2) const {
  if (!quantities || !out_vwap || count == 0) return;
  const auto& levels = buy ? asks_ : bids_;

  // Intermediate overflow guard: if any quantity exceeds 10^12 units (1M contracts),
  // dispatch to the 128-bit unsigned scalar accumulator to eliminate 64-bit integer overflow risk.
  bool safe_for_simd = true;
  for (std::size_t i = 0; i < count; ++i) {
    if (quantities[i] > 1'000'000'000'000LL) {
      safe_for_simd = false;
      break;
    }
  }

  if (use_avx2 && has_avx2() && safe_for_simd) {
    batch_vwap_avx2_impl(levels.data(), levels.size(), quantities, out_vwap, count);
  } else {
    batch_vwap_scalar_impl(levels.data(), levels.size(), quantities, out_vwap, count);
  }
}

}  // namespace polypulse

extern "C" {

void* pp_create() {
  return new (std::nothrow) polypulse::OrderBook();
}

void pp_destroy(void* p) {
  delete static_cast<polypulse::OrderBook*>(p);
}

int pp_snapshot(void* p, const polypulse::PriceLevel* b, std::size_t bn,
                const polypulse::PriceLevel* a, std::size_t an, std::uint64_t s) {
  return p && static_cast<polypulse::OrderBook*>(p)->snapshot(b, bn, a, an, s);
}

int pp_set_level(void* p, int b, std::int64_t pr, std::int64_t q, std::uint64_t s) {
  return p && static_cast<polypulse::OrderBook*>(p)->set_level(b != 0, pr, q, s);
}

polypulse::Quote pp_quote(void* p) {
  return p ? static_cast<polypulse::OrderBook*>(p)->quote() : polypulse::Quote{-1, -1, 0, 0, 0};
}

std::size_t pp_levels(void* p, int b, polypulse::PriceLevel* out, std::size_t cap) {
  return p ? static_cast<polypulse::OrderBook*>(p)->levels(b != 0, out, cap) : 0;
}

double pp_vwap(void* p, int b, std::int64_t q) {
  return p ? static_cast<polypulse::OrderBook*>(p)->vwap(b != 0, q) : NAN;
}

std::int64_t pp_vwap_ticks(void* p, int b, std::int64_t q) {
  return p ? static_cast<polypulse::OrderBook*>(p)->vwap_ticks(b != 0, q) : -1;
}

double pp_imbalance(void* p) {
  return p ? static_cast<polypulse::OrderBook*>(p)->imbalance() : NAN;
}

std::int64_t pp_ofi(void* p) {
  return p ? static_cast<polypulse::OrderBook*>(p)->ofi() : 0;
}

std::int64_t pp_cumulative_ofi(void* p) {
  return p ? static_cast<polypulse::OrderBook*>(p)->cumulative_ofi() : 0;
}

void pp_spread_analytics(void* p, polypulse::SpreadAnalytics* out) {
  if (p && out) {
    *out = static_cast<polypulse::OrderBook*>(p)->spread_analytics();
  }
}

int pp_cross_spread_analytics(void* p1, void* p2, polypulse::CrossSpreadAnalytics* out) {
  if (p1 && p2 && out) {
    *out = polypulse::OrderBook::cross_spread(*static_cast<polypulse::OrderBook*>(p1),
                                              *static_cast<polypulse::OrderBook*>(p2));
    return 1;
  }
  return 0;
}

int pp_batch_vwap(void* p, int buy, const std::int64_t* quantities, double* out_vwap, std::size_t count) {
  if (!p || !quantities || !out_vwap || count == 0) return 0;
  static_cast<polypulse::OrderBook*>(p)->batch_vwap(buy != 0, quantities, out_vwap, count, true);
  return 1;
}

int pp_batch_vwap_scalar(void* p, int buy, const std::int64_t* quantities, double* out_vwap, std::size_t count) {
  if (!p || !quantities || !out_vwap || count == 0) return 0;
  static_cast<polypulse::OrderBook*>(p)->batch_vwap(buy != 0, quantities, out_vwap, count, false);
  return 1;
}

int pp_batch_vwap_avx2(void* p, int buy, const std::int64_t* quantities, double* out_vwap, std::size_t count) {
  if (!p || !quantities || !out_vwap || count == 0) return 0;
  if (!polypulse::has_avx2()) return 0;
  static_cast<polypulse::OrderBook*>(p)->batch_vwap(buy != 0, quantities, out_vwap, count, true);
  return 1;
}

int pp_has_avx2() {
  return polypulse::has_avx2() ? 1 : 0;
}

std::size_t pp_capacity(void* p, int bid) {
  if (!p) return 0;
  return static_cast<polypulse::OrderBook*>(p)->capacity(bid != 0);
}

}  // extern "C"

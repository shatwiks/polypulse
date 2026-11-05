#include "orderbook.hpp"
#include <cassert>
#include <cmath>
#include <iostream>
#include <vector>

using namespace polypulse;

void test_baseline_and_lifecycle() {
  OrderBook b;
  PriceLevel bids[] = {{450000, 20000000}, {500000, 10000000}};
  PriceLevel asks[] = {{550000, 10000000}, {600000, 20000000}};

  // 1. Snapshot creation
  assert(b.snapshot(bids, 2, asks, 2, 1));
  assert(b.quote().bid_ticks == 500000 && b.quote().ask_ticks == 550000);
  assert(b.quote().bid_units == 10000000 && b.quote().ask_units == 10000000);

  // 2. VWAP calculation & liquidity limit
  assert(std::abs(b.vwap(true, 20000000) - 0.575) < 1e-9);
  assert(std::isnan(b.vwap(true, 40000000)));
  assert(b.vwap_ticks(true, 20000000) == 575000);
  assert(b.vwap_ticks(true, 40000000) == -1);

  // 3. Level insertions and rejections
  assert(b.set_level(true, 520000, 30000000, 2));
  assert(!b.set_level(true, 530000, 10000000, 2)); // Stale sequence rejected
  assert(b.quote().bid_ticks == 520000);

  // 4. Level deletion
  assert(b.set_level(true, 520000, 0, 3));
  assert(b.quote().bid_ticks == 500000);

  assert(b.set_level(false, 540000, 5000000, 4));
  assert(b.quote().ask_ticks == 540000);
  assert(b.set_level(false, 540000, 0, 5));
  assert(b.quote().ask_ticks == 550000);

  // 5. Invalid price and quantity rejection
  assert(!b.set_level(false, 1000001, 100, 6)); // Out of bounds price (>1.0)
  assert(!b.set_level(false, -1, 100, 6));      // Negative price
  assert(!b.set_level(false, 500000, -1, 6));   // Negative quantity
  assert(!b.snapshot(bids, 2, asks, 2, 4));     // Stale sequence

  // 6. Duplicate price level rejection in snapshot
  PriceLevel duplicate[] = {{450000, 1000000}, {450000, 2000000}};
  assert(!b.snapshot(duplicate, 2, asks, 2, 6));

  // 7. Successful replacement snapshot
  assert(b.snapshot(bids, 2, asks, 2, 6));
  assert(b.quote().bid_ticks == 500000 && b.quote().ask_ticks == 550000);
}

void test_empty_and_single_level_books() {
  OrderBook b;
  assert(b.quote().bid_ticks == -1);
  assert(b.quote().ask_ticks == -1);
  assert(b.imbalance() == 0.0);
  assert(std::isnan(b.vwap(true, 1000000)));
  assert(b.vwap_ticks(true, 1000000) == -1);

  auto sa_empty = b.spread_analytics();
  assert(sa_empty.best_bid == -1);
  assert(sa_empty.best_ask == -1);
  assert(sa_empty.mid_price == -1);
  assert(sa_empty.spread_ticks == -1);
  assert(sa_empty.spread_bps == 0.0);

  // Single bid
  PriceLevel bid_only[] = {{480000, 5000000}};
  assert(b.snapshot(bid_only, 1, nullptr, 0, 10));
  assert(b.quote().bid_ticks == 480000);
  assert(b.quote().ask_ticks == -1);
  assert(b.imbalance() == 1.0);
  assert(std::abs(b.vwap(false, 5000000) - 0.48) < 1e-9);
  assert(std::isnan(b.vwap(true, 1000000))); // No asks available

  // Single ask
  PriceLevel ask_only[] = {{520000, 8000000}};
  assert(b.snapshot(nullptr, 0, ask_only, 1, 11));
  assert(b.quote().bid_ticks == -1);
  assert(b.quote().ask_ticks == 520000);
  assert(b.imbalance() == -1.0);
  assert(std::abs(b.vwap(true, 8000000) - 0.52) < 1e-9);
  assert(std::isnan(b.vwap(false, 1000000))); // No bids available
}

void test_ofi_mathematics_and_sign_conventions() {
  OrderBook b;
  PriceLevel b1[] = {{500000, 10000000}}; // Bid 0.50 Q=10
  PriceLevel a1[] = {{520000, 10000000}}; // Ask 0.52 Q=10
  b.snapshot(b1, 1, a1, 1, 100);
  assert(b.ofi() == 0); // Initial snapshot has no previous reference
  assert(b.cumulative_ofi() == 0);

  // 1. Bid price rises from 500000 to 510000 (aggressive buying interest arrives)
  // New bid arrives at higher price: delta_b = curr_q = 5000000, ask unchanged -> OFI = +5000000
  b.set_level(true, 510000, 5000000, 101);
  assert(b.ofi() == 5000000);
  assert(b.cumulative_ofi() == 5000000);

  // 2. Bid quantity increases at same price (510000) from 5000000 to 8000000
  // delta_b = 8000000 - 5000000 = +3000000 -> OFI = +3000000
  b.set_level(true, 510000, 8000000, 102);
  assert(b.ofi() == 3000000);
  assert(b.cumulative_ofi() == 8000000);

  // 3. Bid level at 510000 is canceled (quantity = 0), best bid falls back to 500000 (Q=10000000)
  // Bid price fell from 510000 to 500000: delta_b = -prev_q = -8000000 -> OFI = -8000000
  b.set_level(true, 510000, 0, 103);
  assert(b.ofi() == -8000000);
  assert(b.cumulative_ofi() == 0);

  // 4. Ask price improves (drops from 520000 to 515000, aggressive selling arrives)
  // Ask price fell: delta_a = curr_q = 4000000 -> OFI = 0 - (+4000000) = -4000000
  b.set_level(false, 515000, 4000000, 104);
  assert(b.ofi() == -4000000);
  assert(b.cumulative_ofi() == -4000000);

  // 5. Ask level at 515000 is filled/canceled, ask retreats back to 520000 (Q=10000000)
  // Ask price rose from 515000 to 520000: delta_a = -prev_q = -4000000 -> OFI = 0 - (-4000000) = +4000000
  b.set_level(false, 515000, 0, 105);
  assert(b.ofi() == 4000000);
  assert(b.cumulative_ofi() == 0);
}

void test_spread_and_cross_venue_analytics() {
  OrderBook poly;
  PriceLevel poly_b[] = {{490000, 10000000}};
  PriceLevel poly_a[] = {{510000, 10000000}};
  poly.snapshot(poly_b, 1, poly_a, 1, 1);

  auto sa = poly.spread_analytics();
  assert(sa.best_bid == 490000);
  assert(sa.best_ask == 510000);
  assert(sa.mid_price == 500000);
  assert(sa.spread_ticks == 20000); // 0.02
  assert(std::abs(sa.spread_bps - 400.0) < 1e-6); // 20000 / 500000 * 10000 = 400 bps
  assert(sa.is_crossed == 0);

  // Crossed book detection
  OrderBook crossed;
  PriceLevel cross_b[] = {{530000, 1000000}};
  PriceLevel cross_a[] = {{510000, 1000000}};
  crossed.snapshot(cross_b, 1, cross_a, 1, 1);
  auto sa_crossed = crossed.spread_analytics();
  assert(sa_crossed.is_crossed == 1);
  assert(sa_crossed.spread_ticks == -20000);

  // Cross-venue spread test: Kalshi vs Polymarket
  OrderBook kalshi;
  PriceLevel kalshi_b[] = {{520000, 15000000}}; // Kalshi Bid: 0.52 (higher than Poly ask 0.51 -> Arb!)
  PriceLevel kalshi_a[] = {{540000, 15000000}}; // Kalshi Ask: 0.54
  kalshi.snapshot(kalshi_b, 1, kalshi_a, 1, 1);

  // Buy Poly (at 0.51), Sell Kalshi (at 0.52) -> +0.01 profit
  auto cross = OrderBook::cross_spread(poly, kalshi);
  assert(cross.buy_1_sell_2_ticks == 10000); // 520000 - 510000
  assert(cross.executable_buy_1_sell_2 == 1);
  assert(cross.buy_1_sell_2_bps > 0);

  // Buy Kalshi (at 0.54), Sell Poly (at 0.49) -> negative (not executable)
  assert(cross.buy_2_sell_1_ticks == -50000);
  assert(cross.executable_buy_2_sell_1 == 0);
}

void test_scalar_vs_avx2_equivalence() {
  OrderBook b;
  PriceLevel bids[8] = {
    {500000, 10000000}, {490000, 15000000}, {480000, 20000000}, {470000, 25000000},
    {460000, 30000000}, {450000, 35000000}, {440000, 40000000}, {430000, 45000000}
  };
  PriceLevel asks[8] = {
    {510000, 10000000}, {520000, 15000000}, {530000, 20000000}, {540000, 25000000},
    {550000, 30000000}, {560000, 35000000}, {570000, 40000000}, {580000, 45000000}
  };
  b.snapshot(bids, 8, asks, 8, 1);

  const std::size_t count = 8;
  std::int64_t quantities[count] = {
    5000000,   // 5 units (level 1 only)
    15000000,  // 15 units (level 1 + level 2)
    30000000,  // 30 units (level 1 + 2 + 3)
    60000000,  // 60 units
    100000000, // 100 units
    150000000, // 150 units
    220000000, // 220 units (exact total ask units)
    300000000  // 300 units (insufficient liquidity -> NAN)
  };

  double vwap_scalar[count];
  double vwap_avx2[count];

  b.batch_vwap(true, quantities, vwap_scalar, count, false); // Force scalar
  b.batch_vwap(true, quantities, vwap_avx2, count, true);   // Use AVX2 if supported

  for (std::size_t i = 0; i < count; ++i) {
    if (std::isnan(vwap_scalar[i])) {
      assert(std::isnan(vwap_avx2[i]));
    } else {
      assert(!std::isnan(vwap_avx2[i]));
      assert(std::abs(vwap_scalar[i] - vwap_avx2[i]) < 1e-9);
    }
  }

  // Also verify on sell side (bids)
  double vwap_bid_scalar[count];
  double vwap_bid_avx2[count];
  b.batch_vwap(false, quantities, vwap_bid_scalar, count, false);
  b.batch_vwap(false, quantities, vwap_bid_avx2, count, true);

  for (std::size_t i = 0; i < count; ++i) {
    if (std::isnan(vwap_bid_scalar[i])) {
      assert(std::isnan(vwap_bid_avx2[i]));
    } else {
      assert(!std::isnan(vwap_bid_avx2[i]));
      assert(std::abs(vwap_bid_scalar[i] - vwap_bid_avx2[i]) < 1e-9);
    }
  }
}

// Independent Reference Model for OFI (Cont, Kukanov & Stoikov 2014)
struct ReferenceQuote {
  std::int64_t bid_p;
  std::int64_t bid_q;
  std::int64_t ask_p;
  std::int64_t ask_q;
};

std::int64_t calculate_reference_ofi(const ReferenceQuote& prev, const ReferenceQuote& curr) {
  // Bid contribution:
  // - Bid price improves: current bid quantity
  // - Bid price unchanged: current qty - previous qty
  // - Bid price declines: -previous bid quantity
  std::int64_t bid_contrib = 0;
  bool prev_has_b = (prev.bid_p >= 0 && prev.bid_q > 0);
  bool curr_has_b = (curr.bid_p >= 0 && curr.bid_q > 0);
  if (prev_has_b && curr_has_b) {
    if (curr.bid_p > prev.bid_p) bid_contrib = curr.bid_q;
    else if (curr.bid_p == prev.bid_p) bid_contrib = curr.bid_q - prev.bid_q;
    else bid_contrib = -prev.bid_q;
  } else if (curr_has_b) {
    bid_contrib = curr.bid_q;
  } else if (prev_has_b) {
    bid_contrib = -prev.bid_q;
  }

  // Ask contribution:
  // - Ask price improves (moves lower): -current ask quantity
  // - Ask price unchanged: previous ask qty - current ask qty
  // - Ask price worsens (moves higher): previous ask quantity
  std::int64_t ask_contrib = 0;
  bool prev_has_a = (prev.ask_p >= 0 && prev.ask_q > 0);
  bool curr_has_a = (curr.ask_p >= 0 && curr.ask_q > 0);
  if (prev_has_a && curr_has_a) {
    if (curr.ask_p < prev.ask_p) ask_contrib = -curr.ask_q;
    else if (curr.ask_p == prev.ask_p) ask_contrib = prev.ask_q - curr.ask_q;
    else ask_contrib = prev.ask_q;
  } else if (curr_has_a) {
    ask_contrib = -curr.ask_q;
  } else if (prev_has_a) {
    ask_contrib = prev.ask_q;
  }

  return bid_contrib + ask_contrib;
}

void test_independent_ofi_reference_audit() {
  OrderBook b;
  std::int64_t cum_ref = 0;

  // 1. Initial observation -> 0
  PriceLevel init_b[] = {{490000, 1000000}};
  PriceLevel init_a[] = {{520000, 2000000}};
  b.snapshot(init_b, 1, init_a, 1, 1);
  assert(b.ofi() == 0);
  assert(b.cumulative_ofi() == 0);

  ReferenceQuote prev = {490000, 1000000, 520000, 2000000};

  // Case A: Bid improvement with different old (1000) and new (2500) quantities
  // New bid at 500000, Q=2500000. Expected: +2500000
  b.set_level(true, 500000, 2500000, 2);
  ReferenceQuote curr1 = {500000, 2500000, 520000, 2000000};
  std::int64_t ref_ofi1 = calculate_reference_ofi(prev, curr1);
  assert(ref_ofi1 == 2500000);
  assert(b.ofi() == ref_ofi1);
  cum_ref += ref_ofi1;
  assert(b.cumulative_ofi() == cum_ref);
  prev = curr1;

  // Case B: Bid retreat with different old (2500) and new (1000) quantities
  // Cancel 500000 level -> top bid retreats to 490000 (Q=1000000). Expected: -2500000
  b.set_level(true, 500000, 0, 3);
  ReferenceQuote curr2 = {490000, 1000000, 520000, 2000000};
  std::int64_t ref_ofi2 = calculate_reference_ofi(prev, curr2);
  assert(ref_ofi2 == -2500000);
  assert(b.ofi() == ref_ofi2);
  cum_ref += ref_ofi2;
  assert(b.cumulative_ofi() == cum_ref);
  prev = curr2;

  // Case C: Ask improvement with different old (2000) and new (4500) quantities
  // New ask at 510000, Q=4500000. Expected: -4500000
  b.set_level(false, 510000, 4500000, 4);
  ReferenceQuote curr3 = {490000, 1000000, 510000, 4500000};
  std::int64_t ref_ofi3 = calculate_reference_ofi(prev, curr3);
  assert(ref_ofi3 == -4500000);
  assert(b.ofi() == ref_ofi3);
  cum_ref += ref_ofi3;
  assert(b.cumulative_ofi() == cum_ref);
  prev = curr3;

  // Case D: Ask retreat with different old (4500) and new (2000) quantities
  // Cancel 510000 level -> top ask retreats to 520000 (Q=2000000). Expected: +4500000
  b.set_level(false, 510000, 0, 5);
  ReferenceQuote curr4 = {490000, 1000000, 520000, 2000000};
  std::int64_t ref_ofi4 = calculate_reference_ofi(prev, curr4);
  assert(ref_ofi4 == 4500000);
  assert(b.ofi() == ref_ofi4);
  cum_ref += ref_ofi4;
  assert(b.cumulative_ofi() == cum_ref);
  prev = curr4;

  // Case E: Unchanged bid and ask prices with quantity additions
  // Bid 490000: 1000000 -> 1800000 (+800000)
  // Ask 520000: 2000000 -> 2600000 (-600000)
  // Total expected: +800000 - 600000 = +200000
  b.set_level(true, 490000, 1800000, 6);
  ReferenceQuote curr5 = {490000, 1800000, 520000, 2000000};
  std::int64_t ref_ofi5 = calculate_reference_ofi(prev, curr5);
  assert(ref_ofi5 == 800000);
  assert(b.ofi() == ref_ofi5);
  cum_ref += ref_ofi5;
  prev = curr5;

  b.set_level(false, 520000, 2600000, 7);
  ReferenceQuote curr6 = {490000, 1800000, 520000, 2600000};
  std::int64_t ref_ofi6 = calculate_reference_ofi(prev, curr6);
  assert(ref_ofi6 == -600000);
  assert(b.ofi() == ref_ofi6);
  cum_ref += ref_ofi6;
  prev = curr6;

  // Case F: Unchanged bid and ask prices with quantity removals
  // Bid 490000: 1800000 -> 1100000 (-700000)
  // Ask 520000: 2600000 -> 1900000 (+700000)
  // Total expected across two updates: -700000 + 700000 = 0
  b.set_level(true, 490000, 1100000, 8);
  ReferenceQuote curr7 = {490000, 1100000, 520000, 2600000};
  std::int64_t ref_ofi7 = calculate_reference_ofi(prev, curr7);
  assert(ref_ofi7 == -700000);
  assert(b.ofi() == ref_ofi7);
  cum_ref += ref_ofi7;
  prev = curr7;

  b.set_level(false, 520000, 1900000, 9);
  ReferenceQuote curr8 = {490000, 1100000, 520000, 1900000};
  std::int64_t ref_ofi8 = calculate_reference_ofi(prev, curr8);
  assert(ref_ofi8 == 700000);
  assert(b.ofi() == ref_ofi8);
  cum_ref += ref_ofi8;
  prev = curr8;

  // Case G: Bid clearing to empty book
  b.set_level(true, 490000, 0, 10);
  ReferenceQuote curr9 = {-1, 0, 520000, 1900000};
  std::int64_t ref_ofi9 = calculate_reference_ofi(prev, curr9);
  assert(ref_ofi9 == -1100000);
  assert(b.ofi() == ref_ofi9);
  cum_ref += ref_ofi9;
  assert(b.cumulative_ofi() == cum_ref);
}

void test_memory_and_capacity_invariants() {
  assert(sizeof(OrderBook) == 112);
  assert(sizeof(PriceLevel) == 16);
  assert(sizeof(Quote) == 40);
  assert(sizeof(SpreadAnalytics) == 48);
  assert(sizeof(CrossSpreadAnalytics) == 40);

  OrderBook b;
  // Check initial preallocated capacity
  assert(b.capacity(true) == 128);
  assert(b.capacity(false) == 128);

  // Insert 128 levels on the bid side
  for (std::size_t i = 0; i < 128; ++i) {
    int64_t p = 100000 + static_cast<int64_t>(i) * 1000;
    assert(b.set_level(true, p, 1000000, i + 1));
  }
  // Capacity must NOT grow, remaining exactly 128 (zero dynamic allocations)
  assert(b.capacity(true) == 128);
  assert(b.levels(true, nullptr, 0) == 128);

  // Insert 129th level: causes vector capacity growth
  assert(b.set_level(true, 90000, 1000000, 129));
  assert(b.capacity(true) > 128); // Capacity grown via vector growth factor
  assert(b.levels(true, nullptr, 0) == 129);
}

void test_128bit_vwap_accumulator_and_crossed_books() {
  OrderBook b;
  // 100,000,000 contracts (10^14 units) at 0.50 (500,000 ticks)
  // Total value in units*ticks = 5 * 10^19. Exceeds INT64_MAX (9.22 * 10^18).
  PriceLevel deep_ask[] = {{500000, 100000000000000LL}};
  assert(b.snapshot(nullptr, 0, deep_ask, 1, 1));
  std::int64_t v_ticks = b.vwap_ticks(true, 100000000000000LL);
  assert(v_ticks == 500000);
  double v_dbl = b.vwap(true, 100000000000000LL);
  assert(std::abs(v_dbl - 0.50) < 1e-9);

  // Crossed book rejection in cross_spread
  OrderBook poly_crossed;
  PriceLevel b_poly[] = {{530000, 1000000}};
  PriceLevel a_poly[] = {{510000, 1000000}}; // Crossed internally (53 > 51)
  poly_crossed.snapshot(b_poly, 1, a_poly, 1, 1);

  OrderBook kalshi;
  PriceLevel b_kal[] = {{520000, 1000000}};
  PriceLevel a_kal[] = {{540000, 1000000}};
  kalshi.snapshot(b_kal, 1, a_kal, 1, 1);

  auto cs = OrderBook::cross_spread(poly_crossed, kalshi);
  assert(cs.executable_buy_1_sell_2 == 0); // Must NOT be marked executable if internally crossed
  assert(cs.executable_buy_2_sell_1 == 0);
}

int main() {
  std::cout << "[TEST] Running OrderBook unit assertions...\n";
  test_baseline_and_lifecycle();
  test_empty_and_single_level_books();
  test_ofi_mathematics_and_sign_conventions();
  test_independent_ofi_reference_audit();
  test_memory_and_capacity_invariants();
  test_128bit_vwap_accumulator_and_crossed_books();
  test_spread_and_cross_venue_analytics();
  test_scalar_vs_avx2_equivalence();
  std::cout << "[PASS] All C++20 OrderBook and Microstructure tests PASSED.\n";
  return 0;
}

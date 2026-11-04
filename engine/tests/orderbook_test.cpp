#include "orderbook.hpp"
#include <cassert>
#include <cmath>
using namespace polypulse;
int main() {
  OrderBook b;
  PriceLevel bids[]={{4500,20000},{5000,10000}}, asks[]={{5500,10000},{6000,20000}};
  assert(b.snapshot(bids,2,asks,2,1));
  assert(b.quote().bid_ticks==5000 && b.quote().ask_ticks==5500);
  assert(std::abs(b.vwap(true,20000)-0.575)<1e-9);
  assert(std::isnan(b.vwap(true,40000)));
  assert(b.set_level(true,5200,30000,2));
  assert(!b.set_level(true,5300,10000,2));
  assert(b.quote().bid_ticks==5200);
  assert(b.set_level(true,5200,0,3));
  assert(b.quote().bid_ticks==5000);
  assert(!b.set_level(false,10001,100,4));
  return 0;
}

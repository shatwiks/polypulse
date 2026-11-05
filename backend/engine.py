"""ctypes bridge to the C++20 orderbook. Price and size use 1/1,000,000 units."""
import ctypes as C
import math
import pathlib
import sys

PRICE_SCALE = 1_000_000
QUANTITY_SCALE = 1_000_000


class Level(C.Structure):
    _fields_ = [("price_ticks", C.c_int64), ("quantity_units", C.c_int64)]


class Quote(C.Structure):
    _fields_ = [("bid_ticks", C.c_int64), ("ask_ticks", C.c_int64),
                ("bid_units", C.c_int64), ("ask_units", C.c_int64),
                ("sequence", C.c_uint64)]


class SpreadAnalytics(C.Structure):
    _fields_ = [("best_bid", C.c_int64), ("best_ask", C.c_int64),
                ("mid_price", C.c_int64), ("spread_ticks", C.c_int64),
                ("spread_bps", C.c_double), ("is_crossed", C.c_int)]


class CrossSpreadAnalytics(C.Structure):
    _fields_ = [("buy_1_sell_2_ticks", C.c_int64), ("buy_2_sell_1_ticks", C.c_int64),
                ("buy_1_sell_2_bps", C.c_double), ("buy_2_sell_1_bps", C.c_double),
                ("executable_buy_1_sell_2", C.c_int), ("executable_buy_2_sell_1", C.c_int)]


def load_library():
    suffix = ".dll" if sys.platform == "win32" else ".dylib" if sys.platform == "darwin" else ".so"
    path = pathlib.Path(__file__).resolve().parents[1] / "engine" / "build" / ("polypulse" + suffix)
    if not path.exists():
        raise RuntimeError(f"C++ engine missing: run python scripts/build_engine.py ({path})")
    lib = C.CDLL(str(path))

    # Phase 1 / Phase 2 ABI
    lib.pp_create.restype = C.c_void_p
    lib.pp_destroy.argtypes = [C.c_void_p]
    lib.pp_snapshot.argtypes = [C.c_void_p, C.POINTER(Level), C.c_size_t, C.POINTER(Level), C.c_size_t, C.c_uint64]
    lib.pp_snapshot.restype = C.c_int
    lib.pp_set_level.argtypes = [C.c_void_p, C.c_int, C.c_int64, C.c_int64, C.c_uint64]
    lib.pp_set_level.restype = C.c_int
    lib.pp_quote.argtypes = [C.c_void_p]
    lib.pp_quote.restype = Quote
    lib.pp_levels.argtypes = [C.c_void_p, C.c_int, C.POINTER(Level), C.c_size_t]
    lib.pp_levels.restype = C.c_size_t
    lib.pp_vwap.argtypes = [C.c_void_p, C.c_int, C.c_int64]
    lib.pp_vwap.restype = C.c_double
    lib.pp_imbalance.argtypes = [C.c_void_p]
    lib.pp_imbalance.restype = C.c_double

    # Phase 3 Microstructure & SIMD ABI
    lib.pp_vwap_ticks.argtypes = [C.c_void_p, C.c_int, C.c_int64]
    lib.pp_vwap_ticks.restype = C.c_int64
    lib.pp_ofi.argtypes = [C.c_void_p]
    lib.pp_ofi.restype = C.c_int64
    lib.pp_cumulative_ofi.argtypes = [C.c_void_p]
    lib.pp_cumulative_ofi.restype = C.c_int64
    lib.pp_spread_analytics.argtypes = [C.c_void_p, C.POINTER(SpreadAnalytics)]
    lib.pp_spread_analytics.restype = None
    lib.pp_cross_spread_analytics.argtypes = [C.c_void_p, C.c_void_p, C.POINTER(CrossSpreadAnalytics)]
    lib.pp_cross_spread_analytics.restype = C.c_int
    lib.pp_batch_vwap.argtypes = [C.c_void_p, C.c_int, C.POINTER(C.c_int64), C.POINTER(C.c_double), C.c_size_t]
    lib.pp_batch_vwap.restype = C.c_int
    lib.pp_batch_vwap_scalar.argtypes = [C.c_void_p, C.c_int, C.POINTER(C.c_int64), C.POINTER(C.c_double), C.c_size_t]
    lib.pp_batch_vwap_scalar.restype = C.c_int
    lib.pp_batch_vwap_avx2.argtypes = [C.c_void_p, C.c_int, C.POINTER(C.c_int64), C.POINTER(C.c_double), C.c_size_t]
    lib.pp_batch_vwap_avx2.restype = C.c_int
    lib.pp_has_avx2.argtypes = []
    lib.pp_has_avx2.restype = C.c_int
    lib.pp_capacity.argtypes = [C.c_void_p, C.c_int]
    lib.pp_capacity.restype = C.c_size_t

    return lib


class Book:
    def __init__(self, library=None):
        self.lib = library or load_library()
        self.handle = self.lib.pp_create()
        if not self.handle:
            raise MemoryError("cannot allocate orderbook")

    def close(self):
        if getattr(self, "handle", None):
            self.lib.pp_destroy(self.handle)
            self.handle = None

    def __del__(self):
        self.close()

    def snapshot(self, bids, asks, sequence):
        b = (Level * len(bids))(*(Level(*item) for item in bids))
        a = (Level * len(asks))(*(Level(*item) for item in asks))
        if not self.lib.pp_snapshot(self.handle, b, len(bids), a, len(asks), sequence):
            raise ValueError("invalid or stale snapshot")

    def set_level(self, side, price, quantity, sequence):
        if not self.lib.pp_set_level(self.handle, side == "bid", price, quantity, sequence):
            raise ValueError("invalid or stale book update")

    def vwap(self, buy=True, quantity=10.0):
        qty_units = int(round(quantity * QUANTITY_SCALE))
        val = self.lib.pp_vwap(self.handle, 1 if buy else 0, qty_units)
        return None if math.isnan(val) else val

    def vwap_ticks(self, buy=True, quantity=10.0):
        qty_units = int(round(quantity * QUANTITY_SCALE))
        ticks = self.lib.pp_vwap_ticks(self.handle, 1 if buy else 0, qty_units)
        return None if ticks < 0 else ticks

    def imbalance(self):
        return self.lib.pp_imbalance(self.handle)

    def ofi(self):
        """Latest step Order Flow Imbalance in scaled units."""
        return self.lib.pp_ofi(self.handle) / QUANTITY_SCALE

    def cumulative_ofi(self):
        """Cumulative Order Flow Imbalance since book initialization."""
        return self.lib.pp_cumulative_ofi(self.handle) / QUANTITY_SCALE

    def spread_analytics(self):
        sa = SpreadAnalytics()
        self.lib.pp_spread_analytics(self.handle, C.byref(sa))
        has_both = sa.best_bid >= 0 and sa.best_ask >= 0
        return {
            "best_bid": None if sa.best_bid < 0 else sa.best_bid / PRICE_SCALE,
            "best_ask": None if sa.best_ask < 0 else sa.best_ask / PRICE_SCALE,
            "mid_price": (sa.mid_price / PRICE_SCALE) if has_both else None,
            "spread": (sa.spread_ticks / PRICE_SCALE) if has_both else None,
            "spread_bps": sa.spread_bps if has_both else None,
            "is_crossed": bool(sa.is_crossed)
        }

    @staticmethod
    def cross_spread(book1, book2):
        ca = CrossSpreadAnalytics()
        res = book1.lib.pp_cross_spread_analytics(book1.handle, book2.handle, C.byref(ca))
        if not res:
            return None
        return {
            "buy_1_sell_2_spread": ca.buy_1_sell_2_ticks / PRICE_SCALE,
            "buy_2_sell_1_spread": ca.buy_2_sell_1_ticks / PRICE_SCALE,
            "buy_1_sell_2_bps": ca.buy_1_sell_2_bps,
            "buy_2_sell_1_bps": ca.buy_2_sell_1_bps,
            "executable_buy_1_sell_2": bool(ca.executable_buy_1_sell_2),
            "executable_buy_2_sell_1": bool(ca.executable_buy_2_sell_1)
        }

    def batch_vwap(self, buy=True, quantities=None, use_avx2=True):
        quantities = quantities or [10.0, 50.0, 100.0, 500.0]
        n = len(quantities)
        q_arr = (C.c_int64 * n)(*(int(round(q * QUANTITY_SCALE)) for q in quantities))
        out_arr = (C.c_double * n)()
        func = self.lib.pp_batch_vwap if use_avx2 else self.lib.pp_batch_vwap_scalar
        func(self.handle, 1 if buy else 0, q_arr, out_arr, n)
        return [None if math.isnan(out_arr[i]) else out_arr[i] for i in range(n)]

    def has_avx2(self):
        return bool(self.lib.pp_has_avx2())

    def capacity(self, bid=True):
        return self.lib.pp_capacity(self.handle, 1 if bid else 0)

    def view(self, depth=12):
        q = self.lib.pp_quote(self.handle)
        def levels(bid):
            capacity = depth if depth is not None else self.lib.pp_levels(self.handle, int(bid), None, 0)
            out = (Level * capacity)()
            count = self.lib.pp_levels(self.handle, int(bid), out, capacity)
            return [[out[i].price_ticks / PRICE_SCALE, out[i].quantity_units / QUANTITY_SCALE]
                    for i in range(min(count, capacity))]
        vwap = self.lib.pp_vwap(self.handle, 1, QUANTITY_SCALE * 10)
        sa = SpreadAnalytics()
        self.lib.pp_spread_analytics(self.handle, C.byref(sa))
        has_both = sa.best_bid >= 0 and sa.best_ask >= 0
        return {"bids": levels(True), "asks": levels(False),
                "best_bid": None if q.bid_ticks < 0 else q.bid_ticks / PRICE_SCALE,
                "best_ask": None if q.ask_ticks < 0 else q.ask_ticks / PRICE_SCALE,
                "sequence": q.sequence, "buy_vwap_10": None if math.isnan(vwap) else vwap,
                "imbalance": self.lib.pp_imbalance(self.handle),
                "ofi": self.lib.pp_ofi(self.handle) / QUANTITY_SCALE,
                "mid_price": (sa.mid_price / PRICE_SCALE) if has_both else None,
                "spread": (sa.spread_ticks / PRICE_SCALE) if has_both else None,
                "spread_bps": sa.spread_bps if has_both else None}

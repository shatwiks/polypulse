"""ctypes bridge to the C++20 orderbook. Price and size use 1/10,000 units."""
import ctypes as C
import math
import pathlib
import sys

SCALE = 10_000


class Level(C.Structure):
    _fields_ = [("price_ticks", C.c_int64), ("quantity_units", C.c_int64)]


class Quote(C.Structure):
    _fields_ = [("bid_ticks", C.c_int64), ("ask_ticks", C.c_int64),
                ("bid_units", C.c_int64), ("ask_units", C.c_int64),
                ("sequence", C.c_uint64)]


def load_library():
    suffix = ".dll" if sys.platform == "win32" else ".dylib" if sys.platform == "darwin" else ".so"
    path = pathlib.Path(__file__).resolve().parents[1] / "engine" / "build" / ("polypulse" + suffix)
    if not path.exists():
        raise RuntimeError(f"C++ engine missing: run python scripts/build_engine.py ({path})")
    lib = C.CDLL(str(path))
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

    def view(self, depth=12):
        q = self.lib.pp_quote(self.handle)
        def levels(bid):
            out = (Level * depth)()
            count = self.lib.pp_levels(self.handle, int(bid), out, depth)
            return [[out[i].price_ticks / SCALE, out[i].quantity_units / SCALE]
                    for i in range(min(count, depth))]
        vwap = self.lib.pp_vwap(self.handle, 1, SCALE * 10)
        return {"bids": levels(True), "asks": levels(False),
                "best_bid": None if q.bid_ticks < 0 else q.bid_ticks / SCALE,
                "best_ask": None if q.ask_ticks < 0 else q.ask_ticks / SCALE,
                "sequence": q.sequence, "buy_vwap_10": None if math.isnan(vwap) else vwap,
                "imbalance": self.lib.pp_imbalance(self.handle)}

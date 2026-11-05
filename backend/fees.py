"""
PolyPulse Phase 6: Versioned Fee & Execution Cost Engine.
Provides official fee schedules, series-specific multipliers, regulatory rounding rules,
official Polymarket non-linear taker fee curves (C * r * [P * (1 - P)] ** e),
token-specific API fee configuration normalization, and multi-leg net spread calculations
for Polymarket and Kalshi binary prediction markets.
"""
import json
import logging
import math
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_HALF_UP, localcontext
from typing import Dict, Any, Optional
from urllib.parse import quote
import httpx

LOG = logging.getLogger(__name__)
FEE_QUANTUM = Decimal("0.00001")


def _decimal(value: Any) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite():
        raise InvalidOperation("non-finite fee input")
    return result

DEFAULT_FEE_VERSION = "v2026.2"

# Official Polymarket API endpoints for token-specific market fee inspection
POLYMARKET_GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
POLYMARKET_CLOB_FEE_RATE_URL = "https://clob.polymarket.com/fee-rate"
POLYMARKET_CLOB_MARKETS_URL = "https://clob.polymarket.com/clob-markets"

# In-memory cache of resolved token-specific Polymarket fee configurations
_TOKEN_FEE_CACHE: Dict[str, Dict[str, Any]] = {}

# Series multipliers for Kalshi CFTC-regulated binary option markets
KALSHI_SERIES_MULTIPLIERS = {
    "INTRATE": 0.07,
    "FED": 0.07,
    "CPI": 0.07,
    "REC": 0.07,
    "ECON": 0.07,
    "POL": 0.07,
    "PRES": 0.07,
    "INDEX": 0.035,
    "SPX": 0.035,
    "NASDAQ": 0.035,
    "DEFAULT": 0.07
}

# Default Polymarket category taker fee curve (e = 1):
#   fee = round(C * feeRate * [P * (1 - P)] ** e, 5)
# where feeRate is a dimensionless formula coefficient (NOT a flat percentage of contract notional).
# Official Category Coefficients (per Polymarket Fee Schedule):
#   - Finance     : 0.04  (finance_prices_fees)
#   - Politics    : 0.04  (politics_fees)
#   - Economics   : 0.05  (economics_fees)
#   - Geopolitics : 0.00  (fee-free)
#   - Crypto      : 0.07  (crypto_fees_v2)
#   - Sports      : 0.05  (sports_fees_v2)
#   - Tech        : 0.04  (tech_fees)
#   - Culture     : 0.05  (culture_fees)
#   - Weather     : 0.05  (weather_fees)
#   - General     : 0.05  (general_fees)
POLYMARKET_CATEGORY_FEES: Dict[str, Dict[str, Any]] = {
    "finance": {
        "fees_enabled": True,
        "fee_rate": 0.04,
        "taker_bps": 400.0,
        "fee_type": "finance_prices_fees",
        "description": "Finance / Rate / Price markets (feeRate = 0.04)"
    },
    "politics": {
        "fees_enabled": True,
        "fee_rate": 0.04,
        "taker_bps": 400.0,
        "fee_type": "politics_fees",
        "description": "Elections / Political nomination markets (feeRate = 0.04)"
    },
    "economics": {
        "fees_enabled": True,
        "fee_rate": 0.05,
        "taker_bps": 500.0,
        "fee_type": "economics_fees",
        "description": "Macroeconomic Indicator markets (feeRate = 0.05)"
    },
    "geopolitics": {
        "fees_enabled": False,
        "fee_rate": 0.00,
        "taker_bps": 0.0,
        "fee_type": None,
        "description": "Geopolitical events (fee-free, feeRate = 0.00)"
    },
    "crypto": {
        "fees_enabled": True,
        "fee_rate": 0.07,
        "taker_bps": 700.0,
        "fee_type": "crypto_fees_v2",
        "description": "Crypto markets (feeRate = 0.07)"
    },
    "sports": {
        "fees_enabled": True,
        "fee_rate": 0.05,
        "taker_bps": 500.0,
        "fee_type": "sports_fees_v2",
        "description": "Sports markets (feeRate = 0.05)"
    },
    "tech": {
        "fees_enabled": True,
        "fee_rate": 0.04,
        "taker_bps": 400.0,
        "fee_type": "tech_fees",
        "description": "Technology markets (feeRate = 0.04)"
    },
    "culture": {
        "fees_enabled": True,
        "fee_rate": 0.05,
        "taker_bps": 500.0,
        "fee_type": "culture_fees",
        "description": "Culture / Entertainment markets (feeRate = 0.05)"
    },
    "weather": {
        "fees_enabled": True,
        "fee_rate": 0.05,
        "taker_bps": 500.0,
        "fee_type": "weather_fees",
        "description": "Weather / Climate markets (feeRate = 0.05)"
    },
    "general": {
        "fees_enabled": True,
        "fee_rate": 0.05,
        "taker_bps": 500.0,
        "fee_type": "general_fees",
        "description": "General markets (feeRate = 0.05)"
    },
    "other": {
        "fees_enabled": True,
        "fee_rate": 0.05,
        "taker_bps": 500.0,
        "fee_type": "general_fees",
        "description": "Other standard fee-enabled markets (feeRate = 0.05)"
    },
    "fee_free": {
        "fees_enabled": False,
        "fee_rate": 0.00,
        "taker_bps": 0.0,
        "fee_type": None,
        "description": "Explicitly fee-disabled market (feeRate = 0.00)"
    },
    "none": {
        "fees_enabled": False,
        "fee_rate": 0.00,
        "taker_bps": 0.0,
        "fee_type": None,
        "description": "Explicitly fee-disabled market (feeRate = 0.00)"
    }
}

# Mapping from Polymarket API `feeType` identifier to canonical category key
POLYMARKET_FEE_TYPE_MAP: Dict[str, str] = {
    "finance_prices_fees": "finance",
    "finance_fees": "finance",
    "politics_fees": "politics",
    "economics_fees": "economics",
    "geopolitics_fees": "geopolitics",
    "crypto_fees_v2": "crypto",
    "crypto_fees": "crypto",
    "sports_fees_v2": "sports",
    "sports_fees": "sports",
    "tech_fees": "tech",
    "culture_fees": "culture",
    "weather_fees": "weather",
    "general_fees": "general",
}


@dataclass(frozen=True)
class FeeScheduleVersion:
    version: str
    effective_date: str
    source: str
    polymarket_maker_bps: float
    polymarket_taker_bps: float
    polymarket_taker_formula: str
    polymarket_rounding_decimals: int
    polymarket_settlement_fee_pct: float
    kalshi_maker_bps: float
    kalshi_taker_formula: str
    kalshi_max_taker_fee: float
    kalshi_min_fee: float
    kalshi_rounding_rule: str
    default_slippage_bps: float
    notes: str


FEE_SCHEDULES: Dict[str, FeeScheduleVersion] = {
    "v2026.1": FeeScheduleVersion(
        version="v2026.1",
        effective_date="2026-01-01T00:00:00Z",
        source="Kalshi CFTC Rulebook Chapter 3 / Polymarket Official Taker Fee Schedule",
        polymarket_maker_bps=0.0,
        polymarket_taker_bps=0.0,
        polymarket_taker_formula="round(C * feeRate * P * (1 - P), 5)",
        polymarket_rounding_decimals=5,
        polymarket_settlement_fee_pct=0.0,
        kalshi_maker_bps=0.0,
        kalshi_taker_formula="min(0.07 * P * (1 - P), 0.02)",
        kalshi_max_taker_fee=0.02,
        kalshi_min_fee=0.0001,
        kalshi_rounding_rule="unrounded",
        default_slippage_bps=10.0,
        notes="Legacy Kalshi unrounded schedule with Polymarket non-linear C * feeRate * P * (1 - P) formula."
    ),
    "v2026.2": FeeScheduleVersion(
        version="v2026.2",
        effective_date="2026-11-01T00:00:00Z",
        source="Kalshi Official CFTC Fee Schedule (Appendix B) / Polymarket Official Category Fee Schedule",
        polymarket_maker_bps=0.0,
        polymarket_taker_bps=0.0,
        polymarket_taker_formula="round(C * r * (P * (1 - P)) ** e, 5)",
        polymarket_rounding_decimals=5,
        polymarket_settlement_fee_pct=0.0,
        kalshi_maker_bps=0.0,
        kalshi_taker_formula="ceil(multiplier * contracts * P * (1 - P) * 100) / 100",
        kalshi_max_taker_fee=0.02,  # $0.02 max fee per contract
        kalshi_min_fee=0.01,        # $0.01 minimum fee per executed order
        kalshi_rounding_rule="ceil_whole_cents",  # CFTC official order-level cent ceiling
        default_slippage_bps=10.0,
        notes="Official Kalshi CFTC series multipliers & integer cent ceiling + Polymarket market-specific r/e/taker-only fee curve rounded to 5 decimals."
    )
}


def get_fee_schedule(version: str = DEFAULT_FEE_VERSION) -> FeeScheduleVersion:
    return FEE_SCHEDULES.get(version, FEE_SCHEDULES[DEFAULT_FEE_VERSION])


def get_kalshi_series_multiplier(ticker: Optional[str]) -> float:
    if not ticker:
        return KALSHI_SERIES_MULTIPLIERS["DEFAULT"]
    ticker_upper = ticker.upper()
    for prefix, mult in KALSHI_SERIES_MULTIPLIERS.items():
        if prefix != "DEFAULT" and prefix in ticker_upper:
            return mult
    return KALSHI_SERIES_MULTIPLIERS["DEFAULT"]


def normalize_polymarket_fee_rate(raw_value: Any, unit: str = "auto") -> Optional[float]:
    """
    Normalizes a Polymarket fee rate parameter into the dimensionless formula coefficient
    `feeRate` in [0.0, 1.0] used in `fee = C * feeRate * P * (1 - P)`.

    Unit Conversion Rules:
    1. `unit="coefficient"` (or `"decimal"`):
       - Used by Polymarket Gamma `feeSchedule.rate` and `POLYMARKET_CATEGORY_FEES["fee_rate"]`
         (e.g., Finance = 0.04, Politics = 0.04, Economics = 0.05, Geopolitics = 0.00).
       - Valid range: 0.0 <= raw_value <= 1.0.
    2. `unit="bps"` (or `"basis_points"`):
       - Used to interpret Polymarket CLOB `/fee-rate?token_id=<id>` basis-point metadata
         (`base_fee`, `taker_base_fee`, `fee_rate_bps`). This metadata is not a complete fee curve.
       - Numeric conversion, when separately needed, is:
           feeRate = raw_value_bps / 10_000.0
         (e.g., 400 bps -> 0.04, 500 bps -> 0.05, 0 bps -> 0.00).
       - Valid range: 0.0 <= raw_value <= 10_000.0.
    3. `unit="auto"`:
       - Values in [0.0, 1.0] are treated as dimensionless coefficients.
       - Values in (1.0, 10_000.0] are treated as basis points and divided by 10,000.0.

    Returns None if `raw_value` is None, non-numeric, NaN, infinite, negative, or out of bounds.
    """
    if raw_value is None or isinstance(raw_value, bool):
        return None
    try:
        val = _decimal(raw_value)
    except (InvalidOperation, ValueError, TypeError):
        return None

    if val < 0:
        return None

    u = (unit or "auto").strip().lower()
    if u in ("coefficient", "decimal", "rate"):
        if val > 1.0:
            return None
        return float(val)
    elif u in ("bps", "basis_points"):
        if val > 10_000.0:
            return None
        return float(val / Decimal(10_000))
    else:
        # Auto detection with explicit range validation
        if val <= 1.0:
            return float(val)
        elif val <= 10_000.0:
            return float(val / Decimal(10_000))
        return None


def normalize_polymarket_api_fee_config(api_payload: Any) -> Dict[str, Any]:
    """
    Normalizes a raw Polymarket CLOB or Gamma API token/market fee configuration object.

    Supported API payload structures:
    1. Gamma Market Payload (`/markets?clob_token_ids=<token_id>`):
       `{"feesEnabled": True, "feeType": "finance_prices_fees", "feeSchedule": {"rate": 0.04, "exponent": 1}}`
       or `{"feesEnabled": False, "feeType": None, "feeSchedule": None}`
    2. CLOB Market Payload (`/clob-markets/{condition_id}`):
       `{"fd": {"r": 0.05, "e": 1, "to": true}}`
    3. CLOB Fee-Rate Payload (`/fee-rate?token_id=<token_id>`):
       `{"base_fee": 400}` is basis-point metadata, not a complete fee curve.
    4. Normalized / Direct Token Config:
       `{"fees_enabled": True, "fee_rate": 0.04}`

    If the payload is missing, malformed, or contains invalid values, returns `fee_status="unknown"`.
    """
    if not isinstance(api_payload, dict) or not api_payload:
        return {
            "fee_status": "unknown",
            "fees_enabled": None,
            "fee_rate": None,
            "taker_bps": None,
            "category": "unknown",
            "unit_conversion": "none",
            "source": "invalid_or_empty_api_payload"
        }

    # Preserve an earlier failed resolution when a normalized config is passed back
    # through the scanner. A category fallback must not make it executable.
    if api_payload.get("fee_status") == "unknown":
        return dict(api_payload)

    # Check explicit feesEnabled / fees_enabled boolean
    fees_enabled_raw = api_payload.get("feesEnabled")
    if fees_enabled_raw is None:
        fees_enabled_raw = api_payload.get("fees_enabled")

    if fees_enabled_raw is not None and not isinstance(fees_enabled_raw, bool):
        return {
            "fee_status": "unknown",
            "fees_enabled": None,
            "fee_rate": None,
            "taker_bps": None,
            "category": "unknown",
            "unit_conversion": "none",
            "source": "invalid_fees_enabled_flag"
        }

    # CLOB market fee details: r is the curve coefficient, e is the exponent
    # on p*(1-p), and to records whether the curve is taker-only.
    if "fd" in api_payload:
        fd = api_payload["fd"]
        if fd is None:
            if fees_enabled_raw is False:
                return {"fee_status": "known", "fees_enabled": False, "fee_rate": 0.0,
                        "fee_exponent": None, "taker_only": None, "fd": None,
                        "taker_bps": 0.0, "category": "fee_free",
                        "unit_conversion": "fee_disabled_zero",
                        "source": api_payload.get("fee_disabled_corroboration", "clob_market_fees_enabled_false")}
            return {"fee_status": "unknown", "fees_enabled": None, "fee_rate": None,
                    "fee_exponent": None, "taker_only": None, "fd": None,
                    "source": "uncorroborated_null_clob_fd"}
        if not isinstance(fd, dict) or not all(k in fd for k in ("r", "e", "to")):
            return {"fee_status": "unknown", "fees_enabled": None, "fee_rate": None,
                    "fee_exponent": None, "taker_only": None, "source": "invalid_clob_fd"}
        rate = normalize_polymarket_fee_rate(fd["r"], unit="coefficient")
        try:
            exponent = float(fd["e"]) if not isinstance(fd["e"], bool) else float("nan")
        except (TypeError, ValueError):
            exponent = float("nan")
        taker_only = fd["to"]
        if (rate is None or not math.isfinite(exponent) or exponent < 0
                or not isinstance(taker_only, bool)
                or (fees_enabled_raw is False and rate > 0)):
            return {"fee_status": "unknown", "fees_enabled": None, "fee_rate": None,
                    "fee_exponent": None, "taker_only": None, "source": "invalid_clob_fd"}
        enabled = rate > 0 and fees_enabled_raw is not False
        return {"fee_status": "known", "fees_enabled": enabled,
                "fee_rate": rate if enabled else 0.0,
                "fee_exponent": exponent, "taker_only": taker_only,
                "fd": {"r": fd["r"], "e": fd["e"], "to": taker_only},
                "fee_rate_exact": str(_decimal(fd["r"])),
                "fee_exponent_exact": str(_decimal(fd["e"])),
                "condition_id": api_payload.get("condition_id"),
                "taker_bps": round(rate * 10_000, 4),
                "category": str(api_payload.get("category") or "clob_market").lower(),
                "unit_conversion": "coefficient_direct", "source": "polymarket_clob_market_fd"}

    # A /fee-rate base_fee alone remains metadata even if the payload also
    # happens to carry a fee-disabled flag or fee-type label.
    if "base_fee" in api_payload and "feeSchedule" not in api_payload and "fee_schedule" not in api_payload:
        rate = normalize_polymarket_fee_rate(api_payload["base_fee"], unit="bps")
        return {"fee_status": "unknown", "fees_enabled": None, "fee_rate": None,
                "fee_exponent": None, "taker_only": None,
                "base_fee_bps": float(api_payload["base_fee"]) if rate is not None else None,
                "base_fee_unit": "basis_points", "taker_bps": None,
                "category": "unknown", "unit_conversion": "basis_points_only",
                "source": "polymarket_clob_base_fee_incomplete_curve"}

    # If explicitly fee-disabled on the market
    if fees_enabled_raw is False:
        return {
            "fee_status": "known",
            "fees_enabled": False,
            "fee_rate": 0.0,
            "taker_bps": 0.0,
            "category": str(api_payload.get("category") or api_payload.get("feeType") or "fee_free").lower(),
            "unit_conversion": "fee_disabled_zero",
            "source": "polymarket_api_token_config"
        }

    # 1. Check nested `feeSchedule` / `fee_schedule` from Gamma / Market API
    fee_schedule = api_payload.get("feeSchedule") or api_payload.get("fee_schedule")
    if fee_schedule is not None:
        if not isinstance(fee_schedule, dict) or "rate" not in fee_schedule:
            return {"fee_status": "unknown", "fees_enabled": None, "fee_rate": None,
                    "source": "invalid_api_fee_schedule"}
        rate = normalize_polymarket_fee_rate(fee_schedule.get("rate"), unit="coefficient")
        try:
            exponent = float(fee_schedule.get("exponent")) if not isinstance(fee_schedule.get("exponent"), bool) else float("nan")
        except (TypeError, ValueError):
            exponent = float("nan")
        taker_only = fee_schedule.get("takerOnly")
        if rate is None or not math.isfinite(exponent) or exponent < 0 or not isinstance(taker_only, bool):
            return {
                "fee_status": "unknown",
                "fees_enabled": None,
                "fee_rate": None,
                "taker_bps": None,
                "category": "unknown",
                "unit_conversion": "invalid_schedule_rate",
                "source": "invalid_api_fee_schedule"
            }
        enabled = bool(rate > 0.0) if fees_enabled_raw is None else bool(fees_enabled_raw and rate > 0.0)
        active_rate = rate if enabled else 0.0
        fee_type = str(api_payload.get("feeType") or api_payload.get("fee_type") or "token_schedule").lower()
        cat = POLYMARKET_FEE_TYPE_MAP.get(fee_type, fee_type)
        return {
            "fee_status": "known",
            "fees_enabled": enabled,
            "fee_rate": active_rate,
            "fee_rate_exact": str(_decimal(fee_schedule["rate"])),
            "fee_exponent": exponent,
            "fee_exponent_exact": str(_decimal(fee_schedule["exponent"])),
            "taker_only": taker_only,
            "taker_bps": round(active_rate * 10_000.0, 4),
            "category": cat,
            "unit_conversion": "coefficient_direct",
            "source": "polymarket_api_fee_schedule"
        }

    # 2. Check direct `fee_rate` or `rate` field on payload
    if "fee_rate" in api_payload or "rate" in api_payload:
        raw_r = api_payload.get("fee_rate") if "fee_rate" in api_payload else api_payload.get("rate")
        unit_hint = str(api_payload.get("unit") or "coefficient")
        rate = normalize_polymarket_fee_rate(raw_r, unit=unit_hint)
        try:
            exponent = float(api_payload.get("fee_exponent", 1))
        except (TypeError, ValueError):
            exponent = float("nan")
        taker_only = api_payload.get("taker_only", True)
        if rate is None or not math.isfinite(exponent) or exponent < 0 or not isinstance(taker_only, bool):
            return {
                "fee_status": "unknown",
                "fees_enabled": None,
                "fee_rate": None,
                "taker_bps": None,
                "category": "unknown",
                "unit_conversion": "invalid_direct_rate",
                "source": "invalid_api_fee_rate"
            }
        enabled = bool(rate > 0.0) if fees_enabled_raw is None else bool(fees_enabled_raw and rate > 0.0)
        active_rate = rate if enabled else 0.0
        return {
            "fee_status": "known",
            "fees_enabled": enabled,
            "fee_rate": active_rate,
            "fee_rate_exact": (str(_decimal(raw_r) / Decimal(10_000))
                               if unit_hint.lower() in ("bps", "basis_points")
                               or (unit_hint.lower() == "auto" and _decimal(raw_r) > 1)
                               else str(_decimal(raw_r))),
            "fee_exponent": exponent,
            "taker_only": taker_only,
            "taker_bps": round(active_rate * 10_000.0, 4),
            "category": str(api_payload.get("category") or "token_config").lower(),
            "unit_conversion": f"normalized_from_{unit_hint}",
            "source": "polymarket_api_token_config"
        }

    # 3. Check `feeType` / `fee_type` from Gamma API
    fee_type_raw = api_payload.get("feeType") or api_payload.get("fee_type")
    if isinstance(fee_type_raw, str) and fee_type_raw.strip():
        ft_key = fee_type_raw.strip().lower()
        mapped_cat = POLYMARKET_FEE_TYPE_MAP.get(ft_key, ft_key)
        if mapped_cat in POLYMARKET_CATEGORY_FEES:
            cat_info = POLYMARKET_CATEGORY_FEES[mapped_cat]
            enabled = cat_info["fees_enabled"] if fees_enabled_raw is None else bool(fees_enabled_raw)
            active_rate = float(cat_info["fee_rate"]) if enabled else 0.0
            return {
                "fee_status": "known",
                "fees_enabled": enabled,
                "fee_rate": active_rate,
                "taker_bps": round(active_rate * 10_000.0, 4),
                "category": mapped_cat,
                "unit_conversion": "fee_type_schedule_lookup",
                "source": "polymarket_api_fee_type"
            }

    # 4. /fee-rate supplies basis points only. It cannot establish the fee-curve
    # exponent or taker-only status and must never authorize execution alone.
    for bps_key in ("fee_rate_bps", "taker_bps", "base_fee", "taker_base_fee", "takerBaseFee"):
        if bps_key in api_payload and api_payload[bps_key] is not None:
            bps = api_payload[bps_key]
            rate = normalize_polymarket_fee_rate(bps, unit="bps")
            return {
                "fee_status": "unknown", "fees_enabled": None, "fee_rate": None,
                "fee_exponent": None, "taker_only": None,
                "base_fee_bps": float(bps) if rate is not None else None,
                "base_fee_unit": "basis_points", "taker_bps": None,
                "category": "unknown", "unit_conversion": "basis_points_only",
                "source": f"polymarket_clob_{bps_key}_incomplete_curve"
            }

    return {
        "fee_status": "unknown",
        "fees_enabled": None,
        "fee_rate": None,
        "taker_bps": None,
        "category": "unknown",
        "unit_conversion": "none",
        "source": "missing_api_fee_fields"
    }


def fetch_polymarket_token_fee_config(
    token_id: str,
    client: Optional[httpx.Client] = None,
    timeout: float = 5.0,
    use_cache: bool = True,
    condition_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Fetches the actual token-specific market fee configuration from the official
    Polymarket Gamma / CLOB APIs and retains the complete market fee curve.
    Caches resolved configurations by token_id.
    """
    if not token_id or not str(token_id).strip():
        return normalize_polymarket_api_fee_config({})

    tid = str(token_id).strip()
    if use_cache and tid in _TOKEN_FEE_CACHE:
        return _TOKEN_FEE_CACHE[tid]

    owns_client = client is None
    http_client = client or httpx.Client(timeout=timeout)
    try:
        gamma_market = None
        if not condition_id:
            resp_gamma = http_client.get(POLYMARKET_GAMMA_MARKETS_URL, params={"clob_token_ids": tid})
            if resp_gamma.status_code == 200:
                data = resp_gamma.json()
                if isinstance(data, list):
                    def matches_token(market: Dict[str, Any]) -> bool:
                        token_ids = market.get("clobTokenIds", market.get("clob_token_ids"))
                        if isinstance(token_ids, str):
                            try:
                                token_ids = json.loads(token_ids)
                            except json.JSONDecodeError:
                                token_ids = [token_ids]
                        return isinstance(token_ids, list) and tid in [str(value) for value in token_ids]

                    gamma_market = next((m for m in data if isinstance(m, dict) and matches_token(m)), None)
                    # Some Gamma responses omit clobTokenIds after filtering.
                    if (gamma_market is None and len(data) == 1 and isinstance(data[0], dict)
                            and "clobTokenIds" not in data[0] and "clob_token_ids" not in data[0]):
                        gamma_market = data[0]
                    condition_id = (gamma_market or {}).get("conditionId") or (gamma_market or {}).get("condition_id")

        # The CLOB market response carries the authoritative fee curve.
        if condition_id:
            resp_market = http_client.get(f"{POLYMARKET_CLOB_MARKETS_URL}/{quote(str(condition_id), safe='')}")
            if resp_market.status_code == 200:
                market_data = resp_market.json()
                if not isinstance(market_data, dict) or "fd" not in market_data:
                    norm = {"fee_status": "unknown", "fees_enabled": None,
                            "fee_rate": None, "source": "missing_clob_fd"}
                else:
                    if (market_data["fd"] is None and "feesEnabled" not in market_data
                            and gamma_market is not None and gamma_market.get("feesEnabled") is False):
                        market_data = {**market_data, "feesEnabled": False,
                                       "fee_disabled_corroboration": "gamma_fees_enabled_false"}
                    norm = normalize_polymarket_api_fee_config(market_data)
                norm["condition_id"] = condition_id
                if norm["fee_status"] == "known":
                    _TOKEN_FEE_CACHE[tid] = norm
                return norm

        # Gamma's complete feeSchedule is a usable fallback when CLOB is unavailable.
        if gamma_market is not None:
            norm = normalize_polymarket_api_fee_config(gamma_market)
            if norm["fee_status"] == "known":
                _TOKEN_FEE_CACHE[tid] = norm
                return norm

        # /fee-rate is retained as basis-point metadata only.
        resp_clob = http_client.get(POLYMARKET_CLOB_FEE_RATE_URL, params={"token_id": tid})
        if resp_clob.status_code == 200:
            clob_data = resp_clob.json()
            if isinstance(clob_data, dict):
                return normalize_polymarket_api_fee_config(clob_data)
    except Exception as exc:
        LOG.warning("Failed to fetch Polymarket fee config for token %s: %s", tid, type(exc).__name__)
    finally:
        if owns_client:
            http_client.close()

    return {
        "fee_status": "unknown",
        "fees_enabled": None,
        "fee_rate": None,
        "taker_bps": None,
        "category": "unknown",
        "unit_conversion": "none",
        "source": "api_unreachable_or_unconfigured"
    }


def get_polymarket_market_fee_rate(
    market_category: Optional[str] = None,
    fees_enabled: Optional[bool] = None,
    custom_taker_bps: Optional[float] = None,
    poly_fee_rate: Optional[float] = None,
    token_fee_config: Optional[Dict[str, Any]] = None,
    version: str = DEFAULT_FEE_VERSION
) -> Dict[str, Any]:
    """
    Determines the active Polymarket taker fee coefficient (`fee_rate`) and whether fees are enabled.

    Resolution Precedence:
    1. Actual token-specific market fee configuration (`token_fee_config`) from the Polymarket API.
    2. Explicit token-level `poly_fee_rate` (dimensionless coefficient) or `custom_taker_bps` (normalized via bps / 10,000).
    3. Explicit `fees_enabled=False` (fee-disabled market -> feeRate = 0.0, fee_status = "known").
    4. Official category schedule fallback (`market_category` in `POLYMARKET_CATEGORY_FEES`).
    5. If fee configuration is missing or invalid, classifies `fee_status = "unknown"` and `fee_rate = None`.
    """
    # 1. Prefer token-specific market fee config from Polymarket API when provided
    if token_fee_config is not None:
        return normalize_polymarket_api_fee_config(token_fee_config)

    # Validate fees_enabled if provided
    if fees_enabled is not None and not isinstance(fees_enabled, bool):
        return {
            "fee_status": "unknown",
            "fees_enabled": None,
            "fee_rate": None,
            "taker_bps": None,
            "category": "unknown",
            "unit_conversion": "none",
            "source": "invalid_fees_enabled"
        }

    # 2a. Explicit dimensionless coefficient `poly_fee_rate`
    if poly_fee_rate is not None:
        norm_rate = normalize_polymarket_fee_rate(poly_fee_rate, unit="coefficient")
        if norm_rate is None:
            return {
                "fee_status": "unknown",
                "fees_enabled": None,
                "fee_rate": None,
                "taker_bps": None,
                "category": "unknown",
                "unit_conversion": "invalid_coefficient",
                "source": "invalid_poly_fee_rate"
            }
        enabled = fees_enabled if fees_enabled is not None else (norm_rate > 0.0)
        active_rate = norm_rate if enabled else 0.0
        return {
            "fee_status": "known",
            "fees_enabled": enabled,
            "fee_rate": active_rate,
            "taker_bps": round(active_rate * 10_000.0, 4),
            "category": (market_category or "custom").lower(),
            "unit_conversion": "coefficient_direct",
            "source": "explicit_fee_rate"
        }

    # 2b. Explicit basis-points override `custom_taker_bps` (normalized via bps / 10,000)
    if custom_taker_bps is not None:
        norm_rate = normalize_polymarket_fee_rate(custom_taker_bps, unit="bps")
        if norm_rate is None:
            return {
                "fee_status": "unknown",
                "fees_enabled": None,
                "fee_rate": None,
                "taker_bps": None,
                "category": "unknown",
                "unit_conversion": "invalid_bps",
                "source": "invalid_custom_taker_bps"
            }
        enabled = fees_enabled if fees_enabled is not None else (norm_rate > 0.0)
        active_rate = norm_rate if enabled else 0.0
        return {
            "fee_status": "known",
            "fees_enabled": enabled,
            "fee_rate": active_rate,
            "taker_bps": round(active_rate * 10_000.0, 4),
            "category": (market_category or "custom").lower(),
            "unit_conversion": "bps_div_10000",
            "source": "explicit_taker_bps"
        }

    # 3. Explicit fee-disabled market (`fees_enabled=False`)
    if fees_enabled is False:
        return {
            "fee_status": "known",
            "fees_enabled": False,
            "fee_rate": 0.0,
            "taker_bps": 0.0,
            "category": (market_category or "fee_free").strip().lower(),
            "unit_conversion": "fee_disabled_zero",
            "source": "explicit_fee_disabled"
        }

    # 4. Category fallback from `POLYMARKET_CATEGORY_FEES`
    if market_category is not None and isinstance(market_category, str) and market_category.strip():
        raw_cat = market_category.strip().lower()
        cat_key = POLYMARKET_FEE_TYPE_MAP.get(raw_cat, raw_cat)
        if cat_key in POLYMARKET_CATEGORY_FEES:
            cat_info = POLYMARKET_CATEGORY_FEES[cat_key]
            enabled = fees_enabled if fees_enabled is not None else cat_info["fees_enabled"]
            active_rate = float(cat_info["fee_rate"]) if enabled else 0.0
            return {
                "fee_status": "known",
                "fees_enabled": enabled,
                "fee_rate": active_rate,
                "taker_bps": round(active_rate * 10_000.0, 4),
                "category": cat_key,
                "unit_conversion": "category_coefficient",
                "source": "category_schedule"
            }
        # Unrecognized category -> unknown fee configuration
        return {
            "fee_status": "unknown",
            "fees_enabled": None,
            "fee_rate": None,
            "taker_bps": None,
            "category": raw_cat,
            "unit_conversion": "none",
            "source": "unrecognized_category"
        }

    # 5. Missing fee configuration (neither token config, rate, fees_enabled=False, nor category provided)
    return {
        "fee_status": "unknown",
        "fees_enabled": None,
        "fee_rate": None,
        "taker_bps": None,
        "category": "unknown",
        "unit_conversion": "none",
        "source": "missing_fee_configuration"
    }


def calculate_venue_taker_fee(
    venue: str,
    price: float,
    quantity: float,
    version: str = DEFAULT_FEE_VERSION,
    kalshi_ticker: Optional[str] = None,
    poly_taker_bps: Optional[float] = None,
    poly_fee_rate: Optional[float] = None,
    poly_fees_enabled: Optional[bool] = None,
    poly_category: Optional[str] = None,
    poly_token_fee_config: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    Calculates exact known taker fee for an order leg.
    - Kalshi: Official CFTC cent-rounding rules (`ceil_whole_cents`) and series multipliers.
    - Polymarket: Official market curve `fee = round(C * r * [P * (1-P)] ** e, 5)`.
      If the Polymarket fee configuration is missing or invalid, returns `fee_status="unknown"`
      and `total_fee=None`.
    """
    sched = get_fee_schedule(version)
    if price <= 0.0 or price >= 1.0 or quantity <= 0.0:
        return {
            "per_unit_fee": 0.0,
            "total_fee": 0.0,
            "fee_status": "known",
            "version": sched.version
        }

    if venue == "polymarket":
        poly_rate_info = get_polymarket_market_fee_rate(
            market_category=poly_category,
            fees_enabled=poly_fees_enabled,
            custom_taker_bps=poly_taker_bps,
            poly_fee_rate=poly_fee_rate,
            token_fee_config=poly_token_fee_config,
            version=version
        )
        if poly_rate_info.get("fee_status") == "unknown":
            return {
                "per_unit_fee": None,
                "total_fee": None,
                "fee_rate": None,
                "taker_bps": None,
                "fees_enabled": None,
                "fee_status": "unknown",
                "category": poly_rate_info.get("category", "unknown"),
                "source": poly_rate_info.get("source", "missing_fee_configuration"),
                "version": sched.version
            }

        if not poly_rate_info["fees_enabled"] or poly_rate_info["fee_rate"] == 0.0:
            return {
                "per_unit_fee": 0.0,
                "total_fee": 0.0,
                "fee_rate": 0.0,
                "taker_bps": 0.0,
                "fees_enabled": False,
                "fee_status": "known",
                "category": poly_rate_info.get("category", "fee_free"),
                "source": poly_rate_info.get("source", "fee_disabled"),
                "rounding": "round_5_decimals",
                "version": sched.version
            }

        # Official Polymarket taker fee curve; category fallback has e = 1.
        active_fee_rate = float(poly_rate_info["fee_rate"])
        exponent = poly_rate_info.get("fee_exponent", 1.0)
        try:
            with localcontext() as ctx:
                ctx.prec = 40
                price_d, quantity_d = _decimal(price), _decimal(quantity)
                rate_d = _decimal(poly_rate_info.get("fee_rate_exact", active_fee_rate))
                exponent_d = _decimal(poly_rate_info.get("fee_exponent_exact", exponent))
                raw_total = quantity_d * rate_d * (price_d * (1 - price_d)) ** exponent_d
                total_d = raw_total.quantize(FEE_QUANTUM, rounding=ROUND_HALF_UP)
                per_unit_d = (total_d / quantity_d).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
        except (InvalidOperation, ValueError, OverflowError, ZeroDivisionError):
            return {"per_unit_fee": None, "total_fee": None, "fee_status": "unknown",
                    "source": "invalid_decimal_fee_curve", "version": sched.version}
        return {
            "per_unit_fee": float(per_unit_d),
            "total_fee": float(total_d),
            "fee_rate": active_fee_rate,
            "fee_exponent": exponent,
            "taker_only": poly_rate_info.get("taker_only"),
            "taker_bps": poly_rate_info["taker_bps"],
            "fees_enabled": True,
            "fee_status": "known",
            "category": poly_rate_info.get("category", "custom"),
            "source": poly_rate_info.get("source", "schedule"),
            "rounding": "round_5_decimals",
            "version": sched.version
        }

    elif venue == "kalshi":
        multiplier = get_kalshi_series_multiplier(kalshi_ticker)
        if sched.kalshi_rounding_rule == "ceil_whole_cents":
            # Official CFTC order-level formula:
            # fee_cents = ceil(multiplier * contracts * price * (1 - price) * 100)
            # Capped at max_taker_fee * contracts * 100
            with localcontext() as ctx:
                ctx.prec = 40
                price_d, quantity_d = _decimal(price), _decimal(quantity)
                contracts = quantity_d.to_integral_value(rounding=ROUND_CEILING)
                raw_cents = _decimal(multiplier) * contracts * price_d * (1 - price_d) * 100
                total_cents = raw_cents.to_integral_value(rounding=ROUND_CEILING)
                max_cents = (_decimal(sched.kalshi_max_taker_fee) * 100 * contracts).to_integral_value(rounding=ROUND_CEILING)
                final_cents = max(Decimal(1), min(total_cents, max_cents))
                total_d = final_cents / 100
                per_unit_d = (total_d / quantity_d).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
            return {
                "per_unit_fee": float(per_unit_d),
                "total_fee": float(total_d),
                "multiplier": multiplier,
                "fee_status": "known",
                "rounding": "ceil_whole_cents",
                "version": sched.version
            }
        else:
            # Legacy unrounded v2026.1 formula
            with localcontext() as ctx:
                ctx.prec = 40
                price_d, quantity_d = _decimal(price), _decimal(quantity)
                raw_fee = min(_decimal(multiplier) * price_d * (1 - price_d),
                              _decimal(sched.kalshi_max_taker_fee))
                per_unit_d = max(raw_fee, _decimal(sched.kalshi_min_fee)).quantize(
                    Decimal("0.000001"), rounding=ROUND_HALF_UP)
                total_d = (per_unit_d * quantity_d).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
            return {
                "per_unit_fee": float(per_unit_d),
                "total_fee": float(total_d),
                "multiplier": multiplier,
                "fee_status": "known",
                "rounding": "unrounded",
                "version": sched.version
            }

    return {
        "per_unit_fee": None,
        "total_fee": None,
        "fee_status": "unknown",
        "version": sched.version
    }


def calculate_venue_taker_fee_for_fills(
    venue: str,
    fills: list[tuple[Any, Any]],
    version: str = DEFAULT_FEE_VERSION,
    **fee_options: Any,
) -> Dict[str, Any]:
    """Price each executed level, then aggregate one order's fees.

    Polymarket applies five-decimal rounding to each fill. Kalshi v2026.2
    sums the unrounded level fees and applies its cent ceiling once per order.
    """
    try:
        with localcontext() as ctx:
            ctx.prec = 40
            parsed = [(_decimal(price), _decimal(quantity)) for price, quantity in fills]
            if not parsed or any(not (0 < price < 1 and quantity > 0) for price, quantity in parsed):
                raise ValueError("invalid fill")
            quantity = sum((item[1] for item in parsed), Decimal(0))
            schedule = get_fee_schedule(version)
            if venue == "kalshi" and schedule.kalshi_rounding_rule == "ceil_whole_cents":
                multiplier = _decimal(get_kalshi_series_multiplier(fee_options.get("kalshi_ticker")))
                raw_cents = sum((multiplier * qty * price * (1 - price) * 100
                                 for price, qty in parsed), Decimal(0))
                contracts = quantity.to_integral_value(rounding=ROUND_CEILING)
                max_cents = (_decimal(schedule.kalshi_max_taker_fee) * contracts * 100).to_integral_value(rounding=ROUND_CEILING)
                total = max(Decimal(1), min(raw_cents.to_integral_value(rounding=ROUND_CEILING), max_cents)) / 100
            else:
                costs = [calculate_venue_taker_fee(venue, price, qty, version, **fee_options)
                         for price, qty in parsed]
                if any(cost.get("fee_status") != "known" for cost in costs):
                    return {"fee_status": "unknown", "total_fee": None, "per_unit_fee": None,
                            "version": schedule.version}
                total = sum((_decimal(cost["total_fee"]) for cost in costs), Decimal(0))
            return {"fee_status": "known", "total_fee": float(total),
                    "per_unit_fee": float((total / quantity).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)),
                    "version": schedule.version}
    except (InvalidOperation, ValueError, OverflowError, ZeroDivisionError, TypeError):
        return {"fee_status": "unknown", "total_fee": None, "per_unit_fee": None,
                "version": get_fee_schedule(version).version}


def evaluate_net_opportunity_costs(
    buy_venue: str,
    sell_venue: str,
    buy_price: float,
    sell_price: float,
    quantity: float,
    is_inverse_mint: bool = False,
    is_inverse_merge: bool = False,
    is_inverse_long_both: bool = False,
    is_inverse_short_both: bool = False,
    slippage_bps: Optional[float] = None,
    version: str = DEFAULT_FEE_VERSION,
    kalshi_ticker: Optional[str] = None,
    poly_taker_bps: Optional[float] = None,
    poly_fee_rate: Optional[float] = None,
    poly_fees_enabled: Optional[bool] = None,
    poly_category: Optional[str] = None,
    poly_token_fee_config: Optional[Dict[str, Any]] = None,
    buy_fills: Optional[list[tuple[Any, Any]]] = None,
    sell_fills: Optional[list[tuple[Any, Any]]] = None,
) -> Dict[str, Any]:
    """
    Evaluates gross spread vs net spread after known trading fees and estimated execution slippage.
    Applies fees independently to both execution legs (`buy_venue` at `buy_price`, `sell_venue` at `sell_price`).
    If either leg's fee configuration is missing or invalid, classifies `fee_status="unknown"`
    and does not claim a positive executable net spread (`net_spread=None`, `is_executable=False`).
    """
    sched = get_fee_schedule(version)
    active_slippage = slippage_bps if slippage_bps is not None else sched.default_slippage_bps

    long_both = is_inverse_long_both or is_inverse_mint
    short_both = is_inverse_short_both or is_inverse_merge

    # 1. Gross Spread per contract
    if long_both:
        # Cross-Venue Synthetic Long Complementary Position (Payoff Parity)
        gross_spread_per_unit = round(1.00 - (buy_price + sell_price), 6)
        entry_notional = buy_price + sell_price
    elif short_both:
        # Cross-Venue Synthetic Short Complementary Position
        gross_spread_per_unit = round((buy_price + sell_price) - 1.00, 6)
        entry_notional = 1.00
    else:
        # Direct Cross-Venue Spread (Buy Venue A / Sell Venue B)
        gross_spread_per_unit = round(sell_price - buy_price, 6)
        entry_notional = buy_price

    total_gross_profit = round(gross_spread_per_unit * quantity, 5)
    gross_spread_bps = round((gross_spread_per_unit / entry_notional) * 10_000.0, 2) if entry_notional > 0 else 0.0

    # 2. Known Trading Fees Applied Independently to Both Execution Legs
    fee_options = dict(kalshi_ticker=kalshi_ticker, poly_taker_bps=poly_taker_bps,
                       poly_fee_rate=poly_fee_rate, poly_fees_enabled=poly_fees_enabled,
                       poly_category=poly_category, poly_token_fee_config=poly_token_fee_config)
    buy_fee_info = (calculate_venue_taker_fee_for_fills(buy_venue, buy_fills, version, **fee_options)
                    if buy_fills is not None else
                    calculate_venue_taker_fee(buy_venue, buy_price, quantity, version, **fee_options))
    sell_fee_info = (calculate_venue_taker_fee_for_fills(sell_venue, sell_fills, version, **fee_options)
                     if sell_fills is not None else
                     calculate_venue_taker_fee(sell_venue, sell_price, quantity, version, **fee_options))
    for fills, info in ((buy_fills, buy_fee_info), (sell_fills, sell_fee_info)):
        if fills is not None:
            try:
                if sum((_decimal(qty) for _, qty in fills), Decimal(0)) != _decimal(quantity):
                    info.update({"fee_status": "unknown", "total_fee": None})
            except (InvalidOperation, ValueError, TypeError):
                info.update({"fee_status": "unknown", "total_fee": None})

    # 3. Two-Leg Slippage Calculation (Independent of Fee Status)
    buy_leg_notional = buy_price * quantity
    sell_leg_notional = sell_price * quantity
    total_execution_notional = round(buy_leg_notional + sell_leg_notional, 5)

    buy_leg_slippage = round(buy_price * (active_slippage / 10_000.0) * quantity, 5)
    sell_leg_slippage = round(sell_price * (active_slippage / 10_000.0) * quantity, 5)
    total_slippage_cost = round(buy_leg_slippage + sell_leg_slippage, 5)
    slippage_cost_per_unit = round(total_slippage_cost / quantity, 6) if quantity > 0 else 0.0

    # 4. Check for Missing or Invalid Fee Configuration on Either Leg
    if buy_fee_info.get("fee_status") == "unknown" or sell_fee_info.get("fee_status") == "unknown":
        return {
            "fee_status": "unknown",
            "is_executable": False,
            "gross_spread": gross_spread_per_unit,
            "gross_spread_bps": gross_spread_bps,
            "total_gross_profit": total_gross_profit,
            "buy_venue_fee": buy_fee_info.get("total_fee"),
            "sell_venue_fee": sell_fee_info.get("total_fee"),
            "total_known_fees": None,
            "net_spread": None,
            "net_spread_bps": None,
            "total_net_profit": None,
            "slippage_bps_per_leg": active_slippage,
            "buy_leg_slippage": buy_leg_slippage,
            "sell_leg_slippage": sell_leg_slippage,
            "total_slippage_cost": total_slippage_cost,
            "slippage_application": "per_leg_across_both_legs",
            "total_execution_notional": total_execution_notional,
            "estimated_net_spread": None,
            "total_estimated_net_profit": None,
            "fee_schedule_version": sched.version,
            "fee_schedule_source": sched.source,
            "rounding_rule": sched.kalshi_rounding_rule,
            "is_payoff_parity_strategy": long_both or short_both,
            "is_guaranteed_profit": False,
            "risk_disclosure": "Fee configuration unknown or invalid; positive executable net spread cannot be claimed."
        }

    # 5. Deterministic Net Spread & Estimated Net Profit (Both Legs Known)
    with localcontext() as ctx:
        ctx.prec = 40
        total_known_fees_d = _decimal(buy_fee_info["total_fee"]) + _decimal(sell_fee_info["total_fee"])
        total_known_fees = float(total_known_fees_d.quantize(FEE_QUANTUM, rounding=ROUND_HALF_UP))
        fee_per_unit = float((total_known_fees_d / _decimal(quantity)).quantize(
            Decimal("0.000001"), rounding=ROUND_HALF_UP)) if quantity > 0 else 0.0

    net_spread_per_unit = round(gross_spread_per_unit - fee_per_unit, 6)
    total_net_profit = round(total_gross_profit - total_known_fees, 5)
    net_spread_bps = round((net_spread_per_unit / entry_notional) * 10_000.0, 2) if entry_notional > 0 else 0.0

    estimated_net_spread_per_unit = round(net_spread_per_unit - slippage_cost_per_unit, 6)
    total_estimated_net_profit = round(total_net_profit - total_slippage_cost, 5)

    return {
        "fee_status": "known",
        "is_executable": bool(estimated_net_spread_per_unit > 0.0 and gross_spread_per_unit > 0.0),
        "gross_spread": gross_spread_per_unit,
        "gross_spread_bps": gross_spread_bps,
        "total_gross_profit": total_gross_profit,
        "buy_venue_fee": buy_fee_info["total_fee"],
        "sell_venue_fee": sell_fee_info["total_fee"],
        "total_known_fees": total_known_fees,
        "net_spread": net_spread_per_unit,
        "net_spread_bps": net_spread_bps,
        "total_net_profit": total_net_profit,
        "slippage_bps_per_leg": active_slippage,
        "buy_leg_slippage": buy_leg_slippage,
        "sell_leg_slippage": sell_leg_slippage,
        "total_slippage_cost": total_slippage_cost,
        "slippage_application": "per_leg_across_both_legs",
        "total_execution_notional": total_execution_notional,
        "estimated_net_spread": estimated_net_spread_per_unit,
        "total_estimated_net_profit": total_estimated_net_profit,
        "fee_schedule_version": sched.version,
        "fee_schedule_source": sched.source,
        "rounding_rule": sched.kalshi_rounding_rule,
        "is_payoff_parity_strategy": long_both or short_both,
        "is_guaranteed_profit": False,
        "risk_disclosure": "Subject to execution legging delays, partial fills, capital lockup, and oracle settlement divergence."
    }

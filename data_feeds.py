"""
data_feeds.py — Free external data sources for SMC Pro

Sources:
  FRED API     — US macro indicators (Fed rate, CPI, GDP, unemployment) — free unlimited

All functions return empty defaults gracefully if API key not configured.
"""

import os
import json
import logging
from datetime import datetime, timezone

import db as _db

_log = logging.getLogger(__name__)

_FRED_KEY   = os.environ.get("FRED_API_KEY", "8f68a4232ff18468959baa71aaa124de").strip()

_FRED_TTL   = 3600 * 4   # 4h — macro data changes slowly


def _get_requests():
    try:
        import requests
        return requests
    except ImportError:
        return None


def _cache_get(key: str) -> dict | None:
    """Read cached data from app_settings."""
    try:
        raw = _db.get_setting(f"cache_{key}")
        if not raw:
            return None
        data = json.loads(raw)
        ts = datetime.fromisoformat(data.get("_ts", "2000-01-01"))
        ttl = data.get("_ttl", 3600)
        if (datetime.now(timezone.utc) - ts.replace(tzinfo=timezone.utc)).total_seconds() < ttl:
            return data.get("payload")
    except Exception:
        pass
    return None


def _cache_set(key: str, payload: dict, ttl: int) -> None:
    try:
        _db.set_setting(f"cache_{key}", json.dumps({
            "_ts": datetime.now(timezone.utc).isoformat(),
            "_ttl": ttl,
            "payload": payload,
        }))
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# FRED — US Macro Indicators
# ─────────────────────────────────────────────────────────────────────────────

_FRED_SERIES = {
    "fed_rate":        "FEDFUNDS",      # Federal Funds Rate
    "cpi_yoy":         "CPIAUCSL",      # CPI (proxy YoY with transform)
    "unemployment":    "UNRATE",        # Unemployment rate
    "gdp_growth":      "A191RL1Q225SBEA",  # Real GDP growth QoQ
    "dxy_index":       "DTWEXBGS",      # Trade-weighted USD index
    "10y_yield":       "DGS10",         # 10-year treasury yield
    "2y_yield":        "DGS2",          # 2-year treasury yield (yield curve)
    "pce_inflation":   "PCEPI",         # PCE inflation (Fed preferred)
}

_FRED_LABELS = {
    "fed_rate":      "Tasa Fed",
    "cpi_yoy":       "CPI",
    "unemployment":  "Desempleo",
    "gdp_growth":    "PIB (QoQ)",
    "dxy_index":     "DXY (FRED)",
    "10y_yield":     "Bono 10Y",
    "2y_yield":      "Bono 2Y",
    "pce_inflation": "PCE Inflación",
}


def get_fred_indicators() -> dict:
    """
    Fetch latest values for key US macro indicators from FRED.
    Returns dict: {series_name: {"value": float, "date": str, "label": str}}
    Falls back to empty dict if FRED_API_KEY not set.
    """
    if not _FRED_KEY:
        return {}

    cached = _cache_get("fred_indicators")
    if cached:
        return cached

    req = _get_requests()
    if not req:
        return {}

    result = {}
    for name, series_id in _FRED_SERIES.items():
        try:
            url = (f"https://api.stlouisfed.org/fred/series/observations"
                   f"?series_id={series_id}&api_key={_FRED_KEY}"
                   f"&file_type=json&sort_order=desc&limit=2")
            resp = req.get(url, timeout=8)
            if resp.status_code != 200:
                continue
            obs = resp.json().get("observations", [])
            # Find latest non-missing value
            for ob in obs:
                if ob.get("value") and ob["value"] != ".":
                    result[name] = {
                        "value": float(ob["value"]),
                        "date":  ob.get("realtime_start", ob.get("date", "")),
                        "label": _FRED_LABELS.get(name, name),
                        "series_id": series_id,
                    }
                    break
        except Exception as e:
            _log.debug("FRED %s error: %s", series_id, e)
            continue

    if result:
        _cache_set("fred_indicators", result, _FRED_TTL)
    return result


def get_yield_curve_signal(fred_data: dict) -> str:
    """
    Compute yield curve spread (10Y - 2Y). Inversion = bearish USD context.
    Returns: 'NORMAL' | 'INVERTED' | 'FLAT' | 'UNKNOWN'
    """
    y10 = (fred_data.get("10y_yield") or {}).get("value")
    y2  = (fred_data.get("2y_yield") or {}).get("value")
    if y10 is None or y2 is None:
        return "UNKNOWN"
    spread = y10 - y2
    if spread > 0.5:  return "NORMAL"
    if spread < -0.1: return "INVERTED"
    return "FLAT"


def get_macro_bias(fred_data: dict) -> str:
    """
    Derive a simple USD bias from macro data.
    Returns: 'BULLISH_USD' | 'BEARISH_USD' | 'NEUTRAL'
    """
    if not fred_data:
        return "NEUTRAL"
    signals = 0
    fed = (fred_data.get("fed_rate") or {}).get("value", 0)
    if fed > 5.0:   signals += 1   # high rates = bullish USD
    elif fed < 2.0: signals -= 1
    ump = (fred_data.get("unemployment") or {}).get("value", 5)
    if ump < 4.0:   signals += 1   # low unemployment = bullish USD
    elif ump > 6.0: signals -= 1
    yc = get_yield_curve_signal(fred_data)
    if yc == "INVERTED": signals -= 1  # inversion = recession fear = bearish USD
    if signals > 0:  return "BULLISH_USD"
    if signals < 0:  return "BEARISH_USD"
    return "NEUTRAL"


# ─────────────────────────────────────────────────────────────────────────────
# COMBINED MACRO CONTEXT (for signal enrichment)
# ─────────────────────────────────────────────────────────────────────────────

def get_full_macro_context() -> dict:
    """
    Aggregate FRED macro data into a single context dict.
    Safe to call every analysis cycle — internally cached.
    """
    fred       = get_fred_indicators()
    yc_signal  = get_yield_curve_signal(fred)
    macro_bias = get_macro_bias(fred)

    return {
        "fred":           fred,
        "yield_curve":    yc_signal,
        "macro_bias":     macro_bias,
        "fed_rate":       (fred.get("fed_rate") or {}).get("value"),
        "unemployment":   (fred.get("unemployment") or {}).get("value"),
        "cpi":            (fred.get("cpi_yoy") or {}).get("value"),
        "sentiment_score": 0.0,
        "hi_impact_soon":  0,
        "ts":             datetime.utcnow().isoformat(),
    }


def macro_context_to_score_bonus(macro: dict) -> tuple[int, list[str]]:
    """
    Convert macro context into a score adjustment for confluence scoring.
    Returns (adjustment: int, reasons: list[str])
    """
    if not macro:
        return 0, []

    adj = 0
    reasons = []

    bias = macro.get("macro_bias", "NEUTRAL")
    if bias == "BULLISH_USD":
        adj -= 4
        reasons.append("🏦 Macro: USD fuerte (FRED) → presión bajista EUR (-4)")
    elif bias == "BEARISH_USD":
        adj += 4
        reasons.append("🏦 Macro: USD débil (FRED) → impulso alcista EUR (+4)")

    yc = macro.get("yield_curve", "UNKNOWN")
    if yc == "INVERTED":
        adj -= 3
        reasons.append("🏦 Yield curve invertida → precaución (-3)")
    elif yc == "NORMAL":
        adj += 2
        reasons.append("🏦 Yield curve normal → ambiente favorable (+2)")


    return adj, reasons

"""
data_feeds.py — Free external data sources for SMC Pro

Sources:
  FRED API     — US macro indicators (Fed rate, CPI, GDP, unemployment) — free unlimited
  Finnhub      — Forex sentiment, economic calendar, real-time quotes — 60 RPM free
  Alpha Vantage — Forex historical data backup — 25 calls/day free

All functions return empty defaults gracefully if API key not configured.
"""

import os
import json
import logging
from datetime import datetime, timezone, timedelta
from functools import lru_cache

import db as _db

_log = logging.getLogger(__name__)

_FRED_KEY         = os.environ.get("FRED_API_KEY", "").strip()
_FINNHUB_KEY      = os.environ.get("FINNHUB_API_KEY", "").strip()
_AV_KEY           = os.environ.get("ALPHAVANTAGE_API_KEY", "").strip()
_TWELVEDATA_KEY   = os.environ.get("TWELVEDATA_API_KEY", "").strip()

# Cache TTLs (seconds)
_FRED_TTL     = 3600 * 4   # 4h — macro data changes slowly
_FINNHUB_TTL  = 300        # 5m — sentiment updates more often
_AV_TTL       = 900        # 15m


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
# FINNHUB — Sentiment + Economic Calendar + Forex quotes
# ─────────────────────────────────────────────────────────────────────────────

def get_finnhub_news_sentiment(symbol: str = "EURUSD") -> dict:
    """
    Fetch news sentiment for EUR/USD from Finnhub.
    Returns: {"sentiment": float (-1..1), "articles": int, "label": str}
    """
    if not _FINNHUB_KEY:
        return {}

    cached = _cache_get(f"finnhub_sent_{symbol}")
    if cached:
        return cached

    req = _get_requests()
    if not req:
        return {}

    try:
        # Finnhub uses company news endpoint; for forex use general finance news
        url = (f"https://finnhub.io/api/v1/news?category=forex"
               f"&token={_FINNHUB_KEY}")
        resp = req.get(url, timeout=8)
        if resp.status_code != 200:
            return {}

        articles = resp.json() or []
        # Simple sentiment from headlines containing EUR or USD
        pos = neg = 0
        relevant = 0
        eur_kw = ["eur", "euro", "ecb", "european"]
        usd_kw = ["usd", "dollar", "fed", "federal reserve", "fomc"]
        for art in articles[:30]:
            headline = (art.get("headline") or "").lower()
            summary  = (art.get("summary") or "").lower()
            text = headline + " " + summary
            if any(k in text for k in eur_kw + usd_kw):
                relevant += 1
                pos_words = ["rise", "gain", "rally", "strong", "high", "surge", "beat", "exceed"]
                neg_words = ["fall", "drop", "weak", "low", "miss", "decline", "concern", "risk"]
                pos += sum(1 for w in pos_words if w in text)
                neg += sum(1 for w in neg_words if w in text)

        total = pos + neg
        sentiment = round((pos - neg) / total, 3) if total > 0 else 0.0
        label = "Positivo" if sentiment > 0.1 else "Negativo" if sentiment < -0.1 else "Neutral"

        result = {"sentiment": sentiment, "articles": relevant,
                  "label": label, "source": "Finnhub"}
        _cache_set(f"finnhub_sent_{symbol}", result, _FINNHUB_TTL)
        return result
    except Exception as e:
        _log.debug("Finnhub sentiment error: %s", e)
        return {}


def get_finnhub_economic_calendar() -> list[dict]:
    """
    Fetch upcoming economic events from Finnhub.
    Returns list of events with date, country, event, impact.
    """
    if not _FINNHUB_KEY:
        return []

    cached = _cache_get("finnhub_calendar")
    if cached:
        return cached

    req = _get_requests()
    if not req:
        return []

    try:
        now  = datetime.now(timezone.utc)
        frm  = now.strftime("%Y-%m-%d")
        to   = (now + timedelta(days=7)).strftime("%Y-%m-%d")
        url  = (f"https://finnhub.io/api/v1/calendar/economic"
                f"?from={frm}&to={to}&token={_FINNHUB_KEY}")
        resp = req.get(url, timeout=8)
        if resp.status_code != 200:
            return []
        events_raw = resp.json().get("economicCalendar", []) or []
        events = []
        for ev in events_raw[:20]:
            country = (ev.get("country") or "").upper()
            if country not in ("US", "EU", "EUR", "DE", "FR"):
                continue
            events.append({
                "date":    ev.get("time", ""),
                "country": country,
                "event":   ev.get("event", ""),
                "impact":  ev.get("impact", "low"),
                "actual":  ev.get("actual"),
                "forecast":ev.get("estimate"),
                "previous":ev.get("prev"),
            })
        if events:
            _cache_set("finnhub_calendar", events, _FINNHUB_TTL)
        return events
    except Exception as e:
        _log.debug("Finnhub calendar error: %s", e)
        return []


def get_finnhub_forex_quote(symbol: str = "OANDA:EUR_USD") -> dict:
    """
    Get real-time forex quote from Finnhub.
    Returns: {"bid": float, "ask": float, "price": float}
    """
    if not _FINNHUB_KEY:
        return {}

    req = _get_requests()
    if not req:
        return {}

    try:
        url = f"https://finnhub.io/api/v1/quote?symbol={symbol}&token={_FINNHUB_KEY}"
        resp = req.get(url, timeout=5)
        if resp.status_code != 200:
            return {}
        data = resp.json()
        price = data.get("c") or data.get("l")
        return {"price": price, "open": data.get("o"), "high": data.get("h"),
                "low": data.get("l"), "prev_close": data.get("pc"), "source": "Finnhub"}
    except Exception as e:
        _log.debug("Finnhub quote error: %s", e)
        return {}


# ─────────────────────────────────────────────────────────────────────────────
# ALPHA VANTAGE — Forex backup data
# ─────────────────────────────────────────────────────────────────────────────

def get_av_forex_rate(from_sym: str = "EUR", to_sym: str = "USD") -> dict:
    """Get current exchange rate from Alpha Vantage (25 calls/day free)."""
    if not _AV_KEY:
        return {}

    req = _get_requests()
    if not req:
        return {}

    try:
        url = (f"https://www.alphavantage.co/query"
               f"?function=CURRENCY_EXCHANGE_RATE"
               f"&from_currency={from_sym}&to_currency={to_sym}"
               f"&apikey={_AV_KEY}")
        resp = req.get(url, timeout=8)
        data = resp.json().get("Realtime Currency Exchange Rate", {})
        return {
            "price":     float(data.get("5. Exchange Rate", 0)),
            "bid":       float(data.get("8. Bid Price", 0)),
            "ask":       float(data.get("9. Ask Price", 0)),
            "timestamp": data.get("6. Last Refreshed", ""),
            "source":    "AlphaVantage",
        }
    except Exception as e:
        _log.debug("Alpha Vantage forex error: %s", e)
        return {}


# ─────────────────────────────────────────────────────────────────────────────
# COMBINED MACRO CONTEXT (for signal enrichment)
# ─────────────────────────────────────────────────────────────────────────────

def get_full_macro_context() -> dict:
    """
    Aggregate all external data sources into a single macro context dict.
    Safe to call every analysis cycle — internally cached.
    """
    fred      = get_fred_indicators()
    sentiment = get_finnhub_news_sentiment()
    calendar  = get_finnhub_economic_calendar()

    yc_signal  = get_yield_curve_signal(fred)
    macro_bias = get_macro_bias(fred)

    # Count high-impact events in next 24h
    hi_impact_count = 0
    now_str = datetime.now(timezone.utc).isoformat()[:10]
    for ev in calendar:
        ev_date = str(ev.get("date", ""))[:10]
        if ev_date >= now_str and (ev.get("impact") or "").lower() in ("high", "medium"):
            hi_impact_count += 1

    return {
        "fred":           fred,
        "sentiment":      sentiment,
        "calendar_count": len(calendar),
        "hi_impact_soon": hi_impact_count,
        "yield_curve":    yc_signal,
        "macro_bias":     macro_bias,
        "fed_rate":       (fred.get("fed_rate") or {}).get("value"),
        "unemployment":   (fred.get("unemployment") or {}).get("value"),
        "cpi":            (fred.get("cpi_yoy") or {}).get("value"),
        "sentiment_score": sentiment.get("sentiment", 0.0),
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

    sent = macro.get("sentiment_score", 0.0)
    if sent > 0.2:
        adj += 3; reasons.append(f"📰 Sentimiento Finnhub positivo (+3)")
    elif sent < -0.2:
        adj -= 3; reasons.append(f"📰 Sentimiento Finnhub negativo (-3)")

    if macro.get("hi_impact_soon", 0) >= 2:
        adj -= 5
        reasons.append(f"📅 {macro['hi_impact_soon']} eventos alto impacto próximas horas (-5)")

    return adj, reasons

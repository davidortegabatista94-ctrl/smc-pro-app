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

_FRED_KEY   = os.environ.get("FRED_API_KEY", "").strip()

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

# ─────────────────────────────────────────────────────────────────────────────
# NEWS & FUNDAMENTAL SIGNAL
# Fuentes: 20 feeds RSS (Reuters, ECB, Fed, Bloomberg…) + NewsAPI
# Caché 30 min — se llama desde background_worker sin Streamlit
# ─────────────────────────────────────────────────────────────────────────────

_NEWS_TTL = 1800  # 30 min

# Palabras clave de alto impacto en EUR/USD
_HIGH_IMPACT_KEYWORDS = [
    "federal reserve", "fed rate", "interest rate", "rate hike", "rate cut",
    "fomc", "powell", "ecb", "lagarde", "inflation", "cpi", "pce",
    "nonfarm payroll", "nfp", "unemployment", "gdp", "recession",
    "quantitative easing", "qt", "qe", "hawkish", "dovish",
    "yield", "treasury", "bond", "banking crisis",
    "eur/usd", "eurusd", "euro dollar", "usd strength", "dollar",
]

_BULLISH_EUR_KEYWORDS = [
    "ecb hike", "ecb raise", "hawkish ecb", "euro strength", "eur bullish",
    "fed cut", "fed pause", "dovish fed", "dollar weakness", "weak dollar",
    "risk on", "risk appetite", "growth", "recovery",
]

_BEARISH_EUR_KEYWORDS = [
    "fed hike", "fed raise", "hawkish fed", "dollar strength", "strong dollar",
    "ecb cut", "ecb pause", "dovish ecb", "euro weakness", "eur bearish",
    "recession", "risk off", "flight to safety", "inflation surge",
]


def _score_headline(title: str, desc: str = "") -> tuple[float, str]:
    """
    Analiza un titular y devuelve (sentiment: -1.0..+1.0, direction: LONG|SHORT|NEUTRAL).
    +1.0 = muy alcista EUR, -1.0 = muy bajista EUR.
    """
    text = (title + " " + desc).lower()
    impact = sum(1 for kw in _HIGH_IMPACT_KEYWORDS if kw in text)
    if impact == 0:
        return 0.0, "NEUTRAL"

    bull = sum(1 for kw in _BULLISH_EUR_KEYWORDS if kw in text)
    bear = sum(1 for kw in _BEARISH_EUR_KEYWORDS if kw in text)

    # TextBlob sentiment como capa adicional
    try:
        from textblob import TextBlob
        tb_pol = TextBlob(title).sentiment.polarity  # -1..+1
    except Exception:
        tb_pol = 0.0

    raw = (bull - bear) * 0.4 + tb_pol * 0.6
    score = max(-1.0, min(1.0, raw))
    direction = "LONG" if score > 0.15 else ("SHORT" if score < -0.15 else "NEUTRAL")
    return round(score, 3), direction


def get_news_fundamental() -> dict:
    """
    Descarga noticias de 20 fuentes RSS + NewsAPI, analiza sentimiento fundamental
    y devuelve un resumen accionable para EUR/USD.

    Caché de 30 min en DB (seguro llamar cada ciclo de 3 min).
    """
    cached = _cache_get("news_fundamental")
    if cached:
        return cached

    req = _get_requests()
    news_items: list[dict] = []

    # ── RSS feeds (20 fuentes) ───────────────────────────────────────────────
    _RSS_FEEDS = [
        ("Reuters",           "https://feeds.reuters.com/reuters/businessNews"),
        ("ECB Press",         "https://www.ecb.europa.eu/rss/press.html"),
        ("Federal Reserve",   "https://www.federalreserve.gov/feeds/press_all.xml"),
        ("CNBC",              "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
        ("MarketWatch",       "https://feeds.marketwatch.com/marketwatch/marketpulse/"),
        ("FXStreet",          "https://www.fxstreet.com/rss"),
        ("Investing.com",     "https://www.investing.com/rss/news.rss"),
        ("Bloomberg",         "https://feeds.bloomberg.com/markets/news.rss"),
        ("WSJ Markets",       "https://feeds.a.dj.com/rss/RSSMarketsMain.xml"),
        ("Yahoo Finance",     "https://finance.yahoo.com/rss/"),
        ("AP Business",       "https://feeds.apnews.com/rss/apf-business"),
        ("Guardian Business", "https://www.theguardian.com/business/rss"),
        ("Bank of England",   "https://www.bankofengland.co.uk/rss/news"),
        ("IMF",               "https://www.imf.org/en/rss"),
        ("Euronews",          "https://www.euronews.com/rss?format=mrss&level=theme&name=business"),
        ("DailyFX",           "https://www.dailyfx.com/feeds"),
        ("BabyPips",          "https://www.babypips.com/rss"),
        ("ZeroHedge",         "https://feeds.feedburner.com/zerohedge/feed"),
        ("BBC Business",      "http://feeds.bbci.co.uk/news/business/rss.xml"),
        ("Al Jazeera",        "https://www.aljazeera.com/xml/rss/all.xml"),
    ]

    def _fetch_rss(name: str, url: str) -> list[dict]:
        try:
            import feedparser
            if req:
                r = req.get(url, timeout=6, headers={"User-Agent": "Mozilla/5.0"})
                parsed = feedparser.parse(r.content)
            else:
                parsed = feedparser.parse(url)
            items = []
            for e in parsed.entries[:4]:
                title = e.get("title", "")
                desc  = e.get("summary", "")
                if not title:
                    continue
                sentiment, direction = _score_headline(title, desc)
                # Solo guardamos noticias con impacto
                impact = sum(1 for kw in _HIGH_IMPACT_KEYWORDS if kw in (title + desc).lower())
                if impact > 0:
                    items.append({
                        "source":    name,
                        "title":     title[:200],
                        "sentiment": sentiment,
                        "direction": direction,
                        "impact":    impact,
                        "ts":        e.get("published", ""),
                    })
            return items
        except Exception:
            return []

    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=10) as ex:
        futs = {ex.submit(_fetch_rss, name, url): name for name, url in _RSS_FEEDS}
        for fut in as_completed(futs, timeout=15):
            try:
                news_items.extend(fut.result())
            except Exception:
                pass

    # ── Calcular señal fundamental agregada ─────────────────────────────────
    if not news_items:
        result = {
            "signal":        "NEUTRAL",
            "score":         0.0,
            "direction":     "NEUTRAL",
            "news_count":    0,
            "hi_impact":     0,
            "top_headlines": [],
            "macro_bias":    "NEUTRAL",
            "ts":            datetime.utcnow().isoformat(),
        }
        _cache_set("news_fundamental", result, _NEWS_TTL)
        return result

    # Ordenar por impacto desc
    news_items.sort(key=lambda x: (x["impact"], abs(x["sentiment"])), reverse=True)

    hi_impact  = [n for n in news_items if n["impact"] >= 2]
    sentiments = [n["sentiment"] for n in news_items]
    hi_sents   = [n["sentiment"] for n in hi_impact] if hi_impact else sentiments

    # Score ponderado: noticias de alto impacto pesan el doble
    if hi_impact:
        weighted = (sum(hi_sents) * 2 + sum(sentiments)) / (2 * len(hi_impact) + len(sentiments))
    else:
        weighted = sum(sentiments) / len(sentiments)

    weighted = round(max(-1.0, min(1.0, weighted)), 3)

    # Dirección fundamental para EUR/USD
    if weighted > 0.15:
        direction, signal = "LONG",  "🟢 ALCISTA"
    elif weighted < -0.15:
        direction, signal = "SHORT", "🔴 BAJISTA"
    else:
        direction, signal = "NEUTRAL", "⚪ NEUTRAL"

    result = {
        "signal":        signal,
        "score":         weighted,          # -1..+1
        "direction":     direction,
        "news_count":    len(news_items),
        "hi_impact":     len(hi_impact),
        "top_headlines": [
            {"source": n["source"], "title": n["title"],
             "sentiment": n["sentiment"], "direction": n["direction"]}
            for n in news_items[:8]
        ],
        "macro_bias":    "BEARISH_USD" if weighted > 0.2 else ("BULLISH_USD" if weighted < -0.2 else "NEUTRAL"),
        "ts":            datetime.utcnow().isoformat(),
    }
    _cache_set("news_fundamental", result, _NEWS_TTL)

    # Guardar en DB para análisis histórico (estrategia maestra aprende de esto)
    try:
        _db.save_metric(
            name="fundamental_signal",
            value=weighted,
            context={
                "direction":  direction,
                "hi_impact":  len(hi_impact),
                "news_count": len(news_items),
                "top":        result["top_headlines"][:3],
            },
        )
    except Exception:
        pass

    return result


def get_fundamental_score_bonus(fund: dict, signal_direction: str) -> tuple[int, list[str]]:
    """
    Convierte la señal fundamental en bonus/penalización de score.
    Si la noticia confirma la dirección técnica → bonus.
    Si la contradice → penalización fuerte.
    Returns (adjustment: int, reasons: list[str])
    """
    if not fund or fund.get("news_count", 0) == 0:
        return 0, []

    adj      = 0
    reasons  = []
    f_dir    = fund.get("direction", "NEUTRAL")
    f_score  = float(fund.get("score", 0))
    hi_imp   = int(fund.get("hi_impact", 0))
    n_count  = int(fund.get("news_count", 0))

    # Factor de confianza: más noticias de alto impacto = más confianza
    conf = min(1.0, (hi_imp * 2 + n_count * 0.5) / 20)

    if f_dir == signal_direction and f_dir != "NEUTRAL":
        bonus = int(15 * abs(f_score) * conf)
        adj  += bonus
        reasons.append(f"📰 Noticias confirman {f_dir}: +{bonus}pts ({n_count} fuentes, {hi_imp} alto impacto)")
    elif f_dir not in ("NEUTRAL", "") and f_dir != signal_direction and signal_direction:
        penalty = int(18 * abs(f_score) * conf)
        adj    -= penalty
        reasons.append(f"📰 Noticias CONTRADICEN señal ({f_dir} vs {signal_direction}): -{penalty}pts ⚠️")
    elif abs(f_score) < 0.1:
        reasons.append(f"📰 Noticias neutrales ({n_count} fuentes) — sin ajuste")

    # Bonus extra si hay muchas noticias de alto impacto convergentes
    if hi_imp >= 3 and f_dir == signal_direction:
        adj += 5
        reasons.append(f"🔥 {hi_imp} eventos de alto impacto confirman (+5)")

    return adj, reasons


def get_full_macro_context() -> dict:
    """
    Aggregate FRED macro + news fundamental into a single context dict.
    Safe to call every analysis cycle — internally cached.
    """
    fred       = get_fred_indicators()
    yc_signal  = get_yield_curve_signal(fred)
    macro_bias = get_macro_bias(fred)
    news_fund  = get_news_fundamental()

    # Combinar sesgo macro FRED con sentimiento de noticias
    combined_bias = macro_bias
    if news_fund.get("macro_bias") != "NEUTRAL" and macro_bias == "NEUTRAL":
        combined_bias = news_fund.get("macro_bias", "NEUTRAL")
    elif news_fund.get("macro_bias") == macro_bias and macro_bias != "NEUTRAL":
        combined_bias = macro_bias  # doble confirmación

    return {
        "fred":            fred,
        "yield_curve":     yc_signal,
        "macro_bias":      combined_bias,
        "fred_bias":       macro_bias,
        "news_bias":       news_fund.get("macro_bias", "NEUTRAL"),
        "fed_rate":        (fred.get("fed_rate") or {}).get("value"),
        "unemployment":    (fred.get("unemployment") or {}).get("value"),
        "cpi":             (fred.get("cpi_yoy") or {}).get("value"),
        "sentiment_score": float(news_fund.get("score", 0)),
        "news_signal":     news_fund.get("signal", "NEUTRAL"),
        "news_direction":  news_fund.get("direction", "NEUTRAL"),
        "hi_impact_news":  news_fund.get("hi_impact", 0),
        "news_count":      news_fund.get("news_count", 0),
        "top_headlines":   news_fund.get("top_headlines", []),
        "hi_impact_soon":  0,
        "ts":              datetime.utcnow().isoformat(),
    }


def macro_context_to_score_bonus(macro: dict) -> tuple[int, list[str]]:
    """
    Convert macro context into a score adjustment for confluence scoring.
    Now includes both FRED macro AND news fundamental.
    Returns (adjustment: int, reasons: list[str])
    """
    if not macro:
        return 0, []

    adj = 0
    reasons = []

    # ── FRED macro bias ───────────────────────────────────────────────────────
    bias = macro.get("fred_bias", macro.get("macro_bias", "NEUTRAL"))
    if bias == "BULLISH_USD":
        adj -= 4
        reasons.append("🏦 FRED: USD fuerte → presión bajista EUR (-4)")
    elif bias == "BEARISH_USD":
        adj += 4
        reasons.append("🏦 FRED: USD débil → impulso alcista EUR (+4)")

    yc = macro.get("yield_curve", "UNKNOWN")
    if yc == "INVERTED":
        adj -= 3
        reasons.append("🏦 Yield curve invertida → precaución (-3)")
    elif yc == "NORMAL":
        adj += 2
        reasons.append("🏦 Yield curve normal → ambiente favorable (+2)")

    # ── Noticias fundamentales ────────────────────────────────────────────────
    news_dir = macro.get("news_direction", "NEUTRAL")
    news_sc  = float(macro.get("sentiment_score", 0))
    hi_imp   = int(macro.get("hi_impact_news", 0))
    n_count  = int(macro.get("news_count", 0))

    if abs(news_sc) > 0.1 and news_dir != "NEUTRAL" and n_count > 0:
        news_bonus = int(12 * abs(news_sc) * min(1.0, (hi_imp + n_count * 0.3) / 10))
        if news_dir == "LONG":
            adj += news_bonus
            reasons.append(f"📰 Noticias alcistas EUR ({n_count} fuentes, {hi_imp} alto impacto) +{news_bonus}")
        elif news_dir == "SHORT":
            adj -= news_bonus
            reasons.append(f"📰 Noticias bajistas EUR ({n_count} fuentes, {hi_imp} alto impacto) -{news_bonus}")

    return adj, reasons

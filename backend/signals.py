"""
backend/signals.py — Data fetchers (yfinance), news analysis, indicators on OHLC frames.
No Streamlit dependencies. MT5-specific paths remain in smc_pro_app.py.
"""
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, date

import feedparser
import numpy as np
import pandas as pd

from backend.config import (
    NEWS_API_KEY, SYMBOL, CACHE_FILE, CACHE_DURATION, PIP,
)
from backend.indicators import (
    estimate_impact, last_scalar, scalar, flatten_columns,
    interpret_dxy_signal,
)

# ── In-memory OHLC cache (30-second TTL) ──────────────────────────────────────
_EURUSD_CACHE: dict = {}
_EURUSD_CACHE_TTL = timedelta(seconds=30)

# yfinance period/interval map
_TF_MAP_YF = {
    "15m": ("5d",  "15m"),
    "1h":  ("5d",  "1h"),
    "4h":  ("30d", "1h"),
    "1d":  ("90d", "1d"),
}


# ============================================================
# NEWS CACHE (JSON file)
# ============================================================

def load_cache():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r") as f:
                data = json.load(f)
                ts = datetime.fromisoformat(data["timestamp"])
                if datetime.now() - ts < CACHE_DURATION:
                    return data["news"]
        except Exception:
            pass
    return None


def save_cache(news):
    try:
        with open(CACHE_FILE, "w") as f:
            json.dump({"timestamp": datetime.now().isoformat(), "news": news}, f)
    except Exception as e:
        logging.warning(f"save_cache: {e}")


# ============================================================
# OHLC DATA — yfinance (Railway/Linux fallback)
# ============================================================

def get_eurusd_data_yf(tf="1h", extended=False):
    """
    Obtiene datos EUR/USD via yfinance. Pure Python, works on Railway.
    For MT5-first version (Windows local), use get_eurusd_data() in smc_pro_app.py.
    """
    key = (tf, bool(extended))
    cache_entry = _EURUSD_CACHE.get(key)
    if cache_entry:
        ts, df = cache_entry
        if datetime.now() - ts < _EURUSD_CACHE_TTL:
            return df
    try:
        import yfinance as yf
        if extended:
            period, interval = {
                "1h": ("3mo", "1h"),
                "4h": ("6mo", "1h"),
                "1d": ("2y",  "1d"),
            }.get(tf, ("1y", "1d"))
        else:
            period, interval = _TF_MAP_YF.get(tf, ("5d", "1h"))

        df = yf.download(SYMBOL + "=X", period=period, interval=interval,
                         progress=False, auto_adjust=True)
        df = flatten_columns(df)
        if df.empty:
            return pd.DataFrame()
        if tf == "4h":
            df = df.resample("4h").agg({
                "Open": "first", "High": "max",
                "Low": "min", "Close": "last", "Volume": "sum"
            }).dropna()
        _EURUSD_CACHE[key] = (datetime.now(), df)
        return df
    except Exception as e:
        logging.warning(f"get_eurusd_data_yf {tf}: {e}")
        return pd.DataFrame()


def get_backtest_data(tf="1h"):
    """Descarga datos para backtest. Intenta períodos largos con fallback a cortos."""
    try:
        import yfinance as yf
    except ImportError:
        return pd.DataFrame()

    if tf in ("1h", "4h"):
        frames = []
        end = datetime.now()
        for chunk in range(6):  # 6 bloques de ~60d = ~1 año
            start = end - timedelta(days=59)
            try:
                df_chunk = yf.download(
                    "EURUSD=X",
                    start=start.strftime("%Y-%m-%d"),
                    end=end.strftime("%Y-%m-%d"),
                    interval="1h",
                    progress=False, auto_adjust=True,
                )
                df_chunk = flatten_columns(df_chunk)
                if not df_chunk.empty:
                    frames.append(df_chunk)
            except Exception as e:
                logging.warning(f"Backtest chunk {chunk}: {e}")
            end = start - timedelta(days=1)
        if not frames:
            return pd.DataFrame()
        df = pd.concat(frames).sort_index()
        df = df[~df.index.duplicated(keep="first")]
        if tf == "4h":
            df = df.resample("4h").agg({
                "Open": "first", "High": "max",
                "Low": "min", "Close": "last", "Volume": "sum",
            }).dropna()
        return df
    else:
        for period in ["2y", "1y", "6mo"]:
            try:
                df = yf.download("EURUSD=X", period=period, interval="1d",
                                 progress=False, auto_adjust=True)
                df = flatten_columns(df)
                if not df.empty and len(df) > 50:
                    return df
            except Exception as e:
                logging.warning(f"Backtest daily {period}: {e}")
    return pd.DataFrame()


def get_longterm_data_2008():
    """Datos diarios EUR/USD desde 2008+ via yfinance."""
    try:
        import yfinance as yf
    except ImportError:
        return pd.DataFrame()
    for attempt in ["2008-01-01", "2010-01-01"]:
        try:
            df = yf.download(
                "EURUSD=X", start=attempt, interval="1d",
                progress=False, auto_adjust=True,
            )
            df = flatten_columns(df)
            df.dropna(subset=["Close", "High", "Low"], inplace=True)
            df = df[df["Close"] > 0]
            if len(df) > 500:
                return df
        except Exception as e:
            logging.warning(f"Long-term data ({attempt}): {e}")
    return pd.DataFrame()


# ============================================================
# DXY DATA (yfinance)
# ============================================================

def get_dxy_yf(tf="15m"):
    """DXY/dollar index data via yfinance (UUP ETF or DX futures)."""
    try:
        import yfinance as yf
    except ImportError:
        return None
    _yf_dxy_period_map = {
        "5m":  ("1d",  "5m"),
        "15m": ("5d",  "15m"),
        "1h":  ("5d",  "1h"),
        "4h":  ("30d", "1h"),
        "1d":  ("90d", "1d"),
    }
    yf_period, yf_interval = _yf_dxy_period_map.get(tf, ("5d", "15m"))
    for ticker in ["UUP", "DX=F", "DX-Y.NYB"]:
        try:
            df = yf.download(ticker, period=yf_period, interval=yf_interval,
                             progress=False, auto_adjust=True)
            df = flatten_columns(df)
            if df.empty or "Close" not in df.columns:
                continue
            close = df["Close"].dropna()
            if len(close) < 3:
                continue
            current  = scalar(close.iloc[-1])
            open_day = scalar(close.iloc[0])
            if not current or not open_day:
                continue
            change = ((current - open_day) / open_day) * 100
            recent_change = None
            if len(close) >= 5:
                recent_start  = close.iloc[-5]
                recent_change = ((current - recent_start) / recent_start) * 100
            direction, trend, e8, e21 = interpret_dxy_signal(close)
            return {
                "tf": tf, "source": ticker,
                "price": current,
                "chg": round(change, 2),
                "recent_chg": round(recent_change, 2) if recent_change is not None else None,
                "direction": direction,
                "trend": trend,
                "ema8": e8, "ema21": e21,
                "close": close,
            }
        except Exception as e:
            logging.warning(f"DXY yf {ticker} ({tf}): {e}")
    return None


def get_dxy_combined():
    """Combines 5m and 15m DXY signals for a more reliable direction."""
    dxy_15m = get_dxy_yf("15m")
    dxy_5m  = get_dxy_yf("5m")
    if not dxy_15m and not dxy_5m:
        return {
            "dxy_dir": "NO DATA", "dxy_price": None, "dxy_chg": None,
            "dxy_trend": "NO DATA", "dxy_src": None,
            "dxy_ema8": None, "dxy_ema21": None,
            "dxy_15m_dir": None, "dxy_15m_trend": None, "dxy_15m_price": None,
            "dxy_15m_chg": None, "dxy_5m_dir": None, "dxy_5m_trend": None,
            "dxy_5m_price": None, "dxy_5m_chg": None,
        }
    if dxy_5m and dxy_15m:
        if dxy_5m["direction"] == dxy_15m["direction"]:
            combined_dir = dxy_5m["direction"]
        elif dxy_5m["direction"] == "LATERAL":
            combined_dir = dxy_15m["direction"]
        elif dxy_15m["direction"] == "LATERAL":
            combined_dir = dxy_5m["direction"]
        else:
            combined_dir = "LATERAL"
    else:
        combined_dir = (dxy_5m or dxy_15m)["direction"]

    main_data = dxy_5m or dxy_15m
    return {
        "dxy_dir": combined_dir,
        "dxy_price": main_data["price"],
        "dxy_chg": main_data["chg"],
        "dxy_trend": f"15m {dxy_15m['direction'] if dxy_15m else '??'} / 5m {dxy_5m['direction'] if dxy_5m else '??'}",
        "dxy_src": main_data["source"],
        "dxy_ema8": main_data["ema8"],
        "dxy_ema21": main_data["ema21"],
        "dxy_15m_dir": dxy_15m["direction"] if dxy_15m else None,
        "dxy_15m_trend": dxy_15m["trend"] if dxy_15m else None,
        "dxy_15m_price": dxy_15m["price"] if dxy_15m else None,
        "dxy_15m_chg": dxy_15m["chg"] if dxy_15m else None,
        "dxy_5m_dir": dxy_5m["direction"] if dxy_5m else None,
        "dxy_5m_trend": dxy_5m["trend"] if dxy_5m else None,
        "dxy_5m_price": dxy_5m["price"] if dxy_5m else None,
        "dxy_5m_chg": dxy_5m["chg"] if dxy_5m else None,
    }


# ============================================================
# NEWS FETCHING
# ============================================================

def get_rss_news():
    feeds = [
        {"name": "Reuters",           "url": "https://feeds.reuters.com/reuters/topNews"},
        {"name": "BBC Business",      "url": "http://feeds.bbci.co.uk/news/business/rss.xml"},
        {"name": "CNBC",              "url": "https://www.cnbc.com/id/100003114/device/rss/rss.html"},
        {"name": "MarketWatch",       "url": "https://feeds.marketwatch.com/marketwatch/marketpulse/"},
        {"name": "FXStreet",          "url": "https://www.fxstreet.com/rss"},
        {"name": "DailyFX",           "url": "https://www.dailyfx.com/feeds"},
        {"name": "ECB Press",         "url": "https://www.ecb.europa.eu/rss/press.html"},
        {"name": "Federal Reserve",   "url": "https://www.federalreserve.gov/feeds/press_all.xml"},
        {"name": "BabyPips",          "url": "https://www.babypips.com/rss"},
        {"name": "Bloomberg",         "url": "https://feeds.bloomberg.com/markets/news.rss"},
        {"name": "WSJ Markets",       "url": "https://feeds.a.dj.com/rss/RSSMarketsMain.xml"},
        {"name": "Yahoo Finance",     "url": "https://finance.yahoo.com/rss/"},
        {"name": "AP Business",       "url": "https://feeds.apnews.com/rss/apf-business"},
        {"name": "Guardian Business", "url": "https://www.theguardian.com/business/rss"},
        {"name": "Al Jazeera",        "url": "https://www.aljazeera.com/xml/rss/all.xml"},
        {"name": "Bank of England",   "url": "https://www.bankofengland.co.uk/rss/news"},
        {"name": "IMF",               "url": "https://www.imf.org/en/rss"},
        {"name": "Euronews",          "url": "https://www.euronews.com/rss?format=mrss&level=theme&name=business"},
        {"name": "ZeroHedge",         "url": "https://feeds.feedburner.com/zerohedge/feed"},
        {"name": "Investing.com",     "url": "https://www.investing.com/rss/news.rss"},
    ]
    HEADERS = {"User-Agent": "Mozilla/5.0"}

    def fetch(feed):
        try:
            import requests
            r = requests.get(feed["url"], timeout=8, headers=HEADERS)
            r.raise_for_status()
            parsed = feedparser.parse(r.content)
            arts = []
            for e in parsed.entries[:3]:
                title = e.get("title", "")
                desc  = e.get("summary", "")
                imp, lbl, emoji, kws = estimate_impact(title, desc)
                arts.append({
                    "title": title, "description": desc, "url": e.get("link", ""),
                    "source": {"name": feed["name"]},
                    "publishedAt": e.get("published") or e.get("updated") or "",
                    "impact_score": imp, "impact_label": lbl,
                    "impact_emoji": emoji, "keywords": kws,
                })
            return arts
        except Exception:
            return []

    all_news = []
    with ThreadPoolExecutor(max_workers=10) as ex:
        for fut in as_completed([ex.submit(fetch, f) for f in feeds]):
            try:
                all_news.extend(fut.result())
            except Exception:
                pass

    def pdt(a):
        try:
            dt = datetime.fromisoformat(a["publishedAt"].replace("Z", "+00:00"))
            return dt.replace(tzinfo=None) if dt.tzinfo else dt
        except Exception:
            return datetime.min

    all_news.sort(key=pdt, reverse=True)
    return all_news


def get_news(n=25):
    cached = load_cache()
    if cached:
        return cached[:n]
    try:
        import requests
        url = (
            f"https://newsapi.org/v2/everything?q=EUR+USD+Fed+ECB+inflation"
            f"&language=en&sortBy=publishedAt&apiKey={NEWS_API_KEY}"
        )
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        api = []
        for a in r.json().get("articles", [])[:n // 3]:
            imp, lbl, emoji, kws = estimate_impact(
                a.get("title", ""), a.get("description", ""))
            a.update({"impact_score": imp, "impact_label": lbl,
                      "impact_emoji": emoji, "keywords": kws})
            api.append(a)
    except Exception:
        api = []
    rss = get_rss_news()[:2 * n // 3]
    all_a = api + rss

    def sk(a):
        try:
            dt = datetime.fromisoformat(a.get("publishedAt", "").replace("Z", "+00:00"))
            dt = dt.replace(tzinfo=None) if dt.tzinfo else dt
        except Exception:
            dt = datetime(1970, 1, 1)
        try:
            ts = dt.timestamp()
        except Exception:
            ts = 0
        return (-a.get("impact_score", 0), -ts)

    all_a.sort(key=sk)
    save_cache(all_a)
    return all_a[:n]


def analyze_consensus(news):
    if not news:
        return {"consensus": "Sin datos", "details": [], "avg_impact_score": 0,
                "weighted_sentiment": 0, "total_sources": 0}
    themes = {k: [] for k in ["FED/ECB", "INFLATION", "GDP/ECONOMY",
                                "INTEREST_RATES", "EURUSD", "BANKING/FINANCIAL",
                                "TRADE/WAR", "ENERGY/OIL", "GEOPOLITICS"]}
    total_imp = 0
    w_sent = 0
    try:
        from textblob import TextBlob as _TB
        tb_cls = _TB
    except Exception:
        tb_cls = None

    for a in news:
        text = (a.get("title", "") + " " + a.get("description", "")).upper()
        src  = a.get("source", {}).get("name", "Unknown")
        try:
            sent = tb_cls(text).sentiment.polarity if tb_cls else 0
        except Exception:
            sent = 0
        imp   = a.get("impact_score", 0)
        imp_n = imp / 100.0
        total_imp += imp_n
        w_sent    += sent * imp_n
        entry = {"source": src, "sentiment": sent, "impact": imp, "weighted": sent * imp_n}
        if any(k in text for k in ["FED", "ECB", "POWELL", "LAGARDE"]):
            themes["FED/ECB"].append(entry)
        if "INFLATION" in text:
            themes["INFLATION"].append(entry)
        if any(k in text for k in ["GDP", "ECONOMY", "ECONOMIC"]):
            themes["GDP/ECONOMY"].append(entry)
        if any(k in text for k in ["INTEREST RATE", "RATE HIKE", "RATE CUT"]):
            themes["INTEREST_RATES"].append(entry)
        if "EUR" in text and "USD" in text:
            themes["EURUSD"].append(entry)
        if any(k in text for k in ["BANK", "FINANCIAL", "CREDIT"]):
            themes["BANKING/FINANCIAL"].append(entry)
        if any(k in text for k in ["TRADE", "WAR", "SANCTION", "TARIFF"]):
            themes["TRADE/WAR"].append(entry)
        if any(k in text for k in ["OIL", "ENERGY", "GAS", "CRUDE"]):
            themes["ENERGY/OIL"].append(entry)
        if any(k in text for k in ["GEOPOLITIC", "POLITIC", "ELECTION", "GOVERNMENT"]):
            themes["GEOPOLITICS"].append(entry)

    details = []
    pos_w = neg_w = 0
    for theme, srcs in themes.items():
        if not srcs:
            continue
        avg_s = sum(s["sentiment"] for s in srcs) / len(srcs)
        avg_i = sum(s["impact"]    for s in srcs) / len(srcs)
        avg_w = sum(s["weighted"]  for s in srcs) / len(srcs)
        details.append({"theme": theme, "avg_sentiment": avg_s,
                         "avg_impact": avg_i, "avg_weighted": avg_w,
                         "sources_count": len(srcs)})
        if avg_w >  0.05: pos_w += avg_w * len(srcs)
        if avg_w < -0.05: neg_w += abs(avg_w) * len(srcs)

    if   pos_w > neg_w * 1.5: cons = "Bullish (más fuentes positivas con mayor impacto)"
    elif neg_w > pos_w * 1.5: cons = "Bearish (más fuentes negativas con mayor impacto)"
    else:                      cons = "Mixed (consenso dividido)"

    total_sources = sum(len(v) for v in themes.values())
    return {
        "consensus": cons, "details": details, "total_sources": total_sources,
        "avg_impact_score": total_imp / len(news) * 100 if news else 0,
        "weighted_sentiment": w_sent,
    }


def analyze_news():
    """Returns (usd_score, eur_score, news_list, consensus_dict)."""
    usd_score = eur_score = 0.0
    news = get_news()
    try:
        from textblob import TextBlob as _TB
        tb_cls = _TB
    except Exception:
        tb_cls = None

    for a in news:
        text = (a.get("title", "") + " " + a.get("description", "")).upper()
        try:
            sent = tb_cls(text).sentiment if tb_cls else type("S", (), {"polarity": 0, "subjectivity": 0})()
            pol  = sent.polarity
            subj = sent.subjectivity
        except Exception:
            pol = 0
            subj = 0
        imp_m  = a.get("impact_score", 0) / 100.0 * (0.5 if subj > 0.7 else 1.0)
        is_usd = any(k in text for k in ["FED", "DOLLAR", "USD", "USA", "TRUMP",
                                          "POWELL", "FOMC", "FEDERAL RESERVE"])
        is_eur = any(k in text for k in ["ECB", "EURO", "EUR", "LAGARDE",
                                          "EUROZONE", "EUROPEAN CENTRAL"])
        if is_usd and abs(pol) > 0.1:
            usd_score += pol * imp_m * 2
            eur_score -= pol * imp_m * 1.5
        elif is_eur and abs(pol) > 0.1:
            eur_score += pol * imp_m * 2
            usd_score -= pol * imp_m * 1.5
    return usd_score, eur_score, news, analyze_consensus(news)


# ============================================================
# TECHNICAL INDICATORS ON OHLC DATAFRAME
# ============================================================

def calculate_indicators(df):
    if df.empty:
        return {}
    close, high, low = df["Close"], df["High"], df["Low"]
    ind = {}
    for n in [9, 20, 21, 50]:
        if len(close) >= n:
            ind[f"SMA{n}"] = close.rolling(n).mean()
            ind[f"EMA{n}"] = close.ewm(span=n, adjust=False).mean()
    if len(close) >= 15:
        delta = close.diff()
        gain  = delta.clip(lower=0).rolling(14).mean()
        loss  = (-delta.clip(upper=0)).rolling(14).mean()
        rs    = gain / loss.replace(0, np.nan)
        ind["RSI"] = 100 - (100 / (1 + rs))
    if len(close) >= 26:
        m12  = close.ewm(span=12, adjust=False).mean()
        m26  = close.ewm(span=26, adjust=False).mean()
        macd = m12 - m26
        sig  = macd.ewm(span=9, adjust=False).mean()
        ind["MACD"] = macd
        ind["Signal"] = sig
        ind["Histogram"] = macd - sig
    if "SMA20" in ind:
        s20 = close.rolling(20).std()
        ind["BB_upper"] = ind["SMA20"] + s20 * 2
        ind["BB_lower"] = ind["SMA20"] - s20 * 2
    if len(df) >= 15:
        tr = pd.concat([
            high - low,
            (high - close.shift()).abs(),
            (low  - close.shift()).abs(),
        ], axis=1).max(axis=1)
        ind["ATR"] = tr.rolling(14).mean()
    return ind


def analyze_timeframe(tf_name, df):
    if df.empty:
        return {"timeframe": tf_name, "error": "Sin datos", "signal": "NEUTRAL"}
    ind   = calculate_indicators(df)
    close = last_scalar(df["Close"])
    if close is None:
        return {"timeframe": tf_name, "error": "Sin precio", "signal": "NEUTRAL"}
    r = {
        "timeframe": tf_name, "price": close, "trend": "NINGUNO",
        "signal": "NEUTRAL", "rsi": None, "rsi_status": "NEUTRAL",
        "macd_signal": None, "ema_cross": None, "bb_position": None, "atr": None,
    }
    atr = last_scalar(ind.get("ATR"))
    if atr:
        r["atr"] = round(atr / PIP, 1)
    rsi = last_scalar(ind.get("RSI"))
    if rsi is not None:
        r["rsi"] = rsi
        r["rsi_status"] = ("SOBRECOMPRADO" if rsi > 70
                           else "SOBREVENDIDO" if rsi < 30 else "NEUTRAL")
    sma20 = last_scalar(ind.get("SMA20"))
    sma50 = last_scalar(ind.get("SMA50"))
    if sma20 and sma50:
        if close > sma20 > sma50:
            r["trend"] = "ALCISTA"
        elif close < sma20 < sma50:
            r["trend"] = "BAJISTA"
    hist = last_scalar(ind.get("Histogram"))
    if hist is not None:
        r["macd_signal"] = "COMPRA" if hist > 0 else "VENTA"
    e9s  = ind.get("EMA9")
    e21s = ind.get("EMA21")
    if e9s is not None and e21s is not None and len(e9s) > 1:
        e9n, e21n = last_scalar(e9s), last_scalar(e21s)
        e9p, e21p = scalar(e9s.iloc[-2]), scalar(e21s.iloc[-2])
        if all(v is not None for v in [e9n, e21n, e9p, e21p]):
            if e9n > e21n and e9p <= e21p:
                r["ema_cross"] = "ALCISTA"
            elif e9n < e21n and e9p >= e21p:
                r["ema_cross"] = "BAJISTA"
    bbu = last_scalar(ind.get("BB_upper"))
    bbl = last_scalar(ind.get("BB_lower"))
    if bbu and bbl:
        if close > bbu * 0.998:
            r["bb_position"] = "SUPERIOR"
        elif close < bbl * 1.002:
            r["bb_position"] = "INFERIOR"
        else:
            r["bb_position"] = "MEDIO"
    buys  = sum([r["rsi_status"] == "SOBREVENDIDO",
                 r["macd_signal"] == "COMPRA",
                 r["ema_cross"] == "ALCISTA"])
    sells = sum([r["rsi_status"] == "SOBRECOMPRADO",
                 r["macd_signal"] == "VENTA",
                 r["ema_cross"] == "BAJISTA"])
    if buys > sells:
        r["signal"] = "COMPRA"
    elif sells > buys:
        r["signal"] = "VENTA"
    return r

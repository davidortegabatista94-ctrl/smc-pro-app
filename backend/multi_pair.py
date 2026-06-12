"""
backend/multi_pair.py — Independent analysis engine for the 7 major FX pairs.

Each pair is analyzed completely separately:
  - Its own price data (yfinance)
  - Its own news sentiment (currency-keyword filtering)
  - Its own technical indicators (TF analysis)
  - Its own weighted-vote decision (DXY direction adjusted per pair)

No shared state between pairs.
"""
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import pandas as pd
import numpy as np

_log = logging.getLogger(__name__)

# ── In-memory OHLC cache ─────────────────────────────────────────────────────
_OHLC_CACHE: dict = {}
_OHLC_TTL = timedelta(minutes=5)

# ── Pair configuration ────────────────────────────────────────────────────────
PAIRS: dict[str, dict] = {
    "EURUSD": {
        "name": "EUR/USD", "base": "EUR", "quote": "USD",
        "yf": "EURUSD=X", "pip": 0.0001,
        "dxy_mode": "inverse",   # DXY up → pair falls
        "color": "#3d8ef5", "flag_base": "🇪🇺", "flag_quote": "🇺🇸",
        "base_kw":  ["euro","EUR","ECB","European Central Bank","eurozone","lagarde","europe"],
        "quote_kw": ["dollar","USD","fed","federal reserve","fomc","powell","treasury"],
        "desc": "Par más líquido del mundo. Sensible a diferenciales de tipos BCE/Fed.",
    },
    "GBPUSD": {
        "name": "GBP/USD", "base": "GBP", "quote": "USD",
        "yf": "GBPUSD=X", "pip": 0.0001,
        "dxy_mode": "inverse",
        "color": "#c97d0a", "flag_base": "🇬🇧", "flag_quote": "🇺🇸",
        "base_kw":  ["pound","GBP","BOE","Bank of England","sterling","UK","britain","bailey"],
        "quote_kw": ["dollar","USD","fed","federal reserve","fomc","powell"],
        "desc": "Cable. Alta volatilidad. Afectado por Brexit, BOE y datos UK.",
    },
    "USDJPY": {
        "name": "USD/JPY", "base": "USD", "quote": "JPY",
        "yf": "USDJPY=X", "pip": 0.01,
        "dxy_mode": "direct",    # DXY up → pair rises
        "color": "#e03c50", "flag_base": "🇺🇸", "flag_quote": "🇯🇵",
        "base_kw":  ["dollar","USD","fed","federal reserve","fomc","powell"],
        "quote_kw": ["yen","JPY","BOJ","Bank of Japan","ueda","intervention"],
        "desc": "Par carry trade. Sensible a diferenciales de tipos y risk-on/risk-off.",
    },
    "USDCHF": {
        "name": "USD/CHF", "base": "USD", "quote": "CHF",
        "yf": "USDCHF=X", "pip": 0.0001,
        "dxy_mode": "direct",
        "color": "#57697c", "flag_base": "🇺🇸", "flag_quote": "🇨🇭",
        "base_kw":  ["dollar","USD","fed","federal reserve","fomc"],
        "quote_kw": ["franc","CHF","SNB","Swiss National Bank","switzerland","jordan"],
        "desc": "Safe haven suizo. Correlación inversa con riesgo global.",
    },
    "AUDUSD": {
        "name": "AUD/USD", "base": "AUD", "quote": "USD",
        "yf": "AUDUSD=X", "pip": 0.0001,
        "dxy_mode": "inverse",
        "color": "#00b87c", "flag_base": "🇦🇺", "flag_quote": "🇺🇸",
        "base_kw":  ["aussie","AUD","RBA","Reserve Bank of Australia","australia","bullock","iron ore","commodity"],
        "quote_kw": ["dollar","USD","fed","federal reserve","fomc"],
        "desc": "Commodity currency. Ligado a China, materias primas y risk appetite.",
    },
    "USDCAD": {
        "name": "USD/CAD", "base": "USD", "quote": "CAD",
        "yf": "USDCAD=X", "pip": 0.0001,
        "dxy_mode": "direct",
        "color": "#c97d0a", "flag_base": "🇺🇸", "flag_quote": "🇨🇦",
        "base_kw":  ["dollar","USD","fed","federal reserve"],
        "quote_kw": ["loonie","CAD","BOC","Bank of Canada","canada","oil","crude","macklem"],
        "desc": "Loonie. Correlación alta con precio del crudo WTI.",
    },
    "NZDUSD": {
        "name": "NZD/USD", "base": "NZD", "quote": "USD",
        "yf": "NZDUSD=X", "pip": 0.0001,
        "dxy_mode": "inverse",
        "color": "#00c98a", "flag_base": "🇳🇿", "flag_quote": "🇺🇸",
        "base_kw":  ["kiwi","NZD","RBNZ","Reserve Bank of New Zealand","new zealand","orr","dairy"],
        "quote_kw": ["dollar","USD","fed","federal reserve","fomc"],
        "desc": "Kiwi. Commodity currency con alta sensibilidad a risk appetite.",
    },
}

PAIR_LIST: list[str] = list(PAIRS.keys())


# ── OHLC data fetcher ─────────────────────────────────────────────────────────

def get_pair_ohlc(symbol: str, period: str = "5d", interval: str = "1h") -> pd.DataFrame:
    """Fetch OHLCV for any FX pair from yfinance with 5-min in-memory cache."""
    cache_key = (symbol, period, interval)
    entry = _OHLC_CACHE.get(cache_key)
    if entry:
        ts, df = entry
        if datetime.now() - ts < _OHLC_TTL:
            return df

    cfg = PAIRS.get(symbol, {})
    yf_sym = cfg.get("yf", f"{symbol}=X")
    try:
        import yfinance as yf
        df = yf.download(yf_sym, period=period, interval=interval,
                         auto_adjust=True, progress=False)
        # Flatten multi-level columns if present
        if hasattr(df.columns, "levels"):
            df.columns = df.columns.get_level_values(0)
        df.dropna(inplace=True)
        # Resample 1h → 4h if needed
        if interval == "1h" and period in ("30d", "60d"):
            df = df.resample("4h").agg({
                "Open": "first", "High": "max",
                "Low": "min", "Close": "last", "Volume": "sum",
            }).dropna()
        if not df.empty:
            _OHLC_CACHE[cache_key] = (datetime.now(), df)
        return df
    except Exception as e:
        _log.warning("get_pair_ohlc %s %s/%s: %s", symbol, period, interval, e)
        return pd.DataFrame()


# ── News sentiment per pair ───────────────────────────────────────────────────

def score_news_for_pair(symbol: str, news: list) -> dict:
    """
    Score news sentiment specifically for a pair's two currencies.
    Returns: {base_score, quote_score, direction, base_count, quote_count}
    """
    cfg = PAIRS.get(symbol, {})
    base_kw  = cfg.get("base_kw",  [])
    quote_kw = cfg.get("quote_kw", [])
    dxy_mode = cfg.get("dxy_mode", "inverse")

    base_score = quote_score = 0.0
    base_count = quote_count = 0

    try:
        from textblob import TextBlob as _TB
        _tb = _TB
    except Exception:
        _tb = None

    for item in news:
        headline = item.get("title", "") or item.get("headline", "")
        summary  = item.get("description", "") or item.get("summary", "")
        text = (headline + " " + summary).lower()

        # Compute sentiment
        pol = 0.0
        if _tb:
            try:
                pol = _tb(text).sentiment.polarity
            except Exception:
                pass
        else:
            pos_words = ["rises","gains","strong","bullish","surges","rally","beat","above"]
            neg_words = ["falls","drops","weak","bearish","slumps","miss","below","deficit"]
            pol = (sum(1 for w in pos_words if w in text) -
                   sum(1 for w in neg_words if w in text)) * 0.15

        impact = item.get("impact_score", 50) / 100.0

        is_base  = any(k in text for k in base_kw)
        is_quote = any(k in text for k in quote_kw)

        if is_base and not is_quote and abs(pol) > 0.05:
            base_score  += pol * impact
            base_count  += 1
        elif is_quote and not is_base and abs(pol) > 0.05:
            quote_score += pol * impact
            quote_count += 1

    avg_base  = base_score  / base_count  if base_count  else 0.0
    avg_quote = quote_score / quote_count if quote_count else 0.0

    # For inverse pairs (EUR/USD): base strong → LONG
    # For direct pairs (USD/JPY): quote strong → SHORT pair (USD gaining is LONG)
    diff = avg_base - avg_quote
    direction = "NEUTRAL"
    if diff > 0.08:
        direction = "LONG"
    elif diff < -0.08:
        direction = "SHORT"

    return {
        "base_score":  round(avg_base, 3),
        "quote_score": round(avg_quote, 3),
        "direction":   direction,
        "base_count":  base_count,
        "quote_count": quote_count,
    }


# ── Single-pair full analysis ─────────────────────────────────────────────────

def analyze_pair(
    symbol: str,
    dxy_dir: str = "",
    news: list | None = None,
    mode: str = "intraday",
) -> dict:
    """
    Complete independent technical + news + decision analysis for one FX pair.
    No external state — works with any of the 7 majors.
    """
    from backend.signals import analyze_timeframe, calculate_indicators
    from backend.indicators import (
        detect_liquidity_levels, calc_smart_tp_sl,
        detect_market_structure, calculate_trend_strength,
        detect_volume_spikes, scalar, last_scalar,
    )

    cfg = PAIRS.get(symbol, {})
    pip_size = cfg.get("pip", 0.0001)
    dxy_mode = cfg.get("dxy_mode", "inverse")

    result: dict = {
        "symbol": symbol, "name": cfg.get("name", symbol),
        "color": cfg.get("color", "#3d8ef5"),
        "flag_base": cfg.get("flag_base", ""), "flag_quote": cfg.get("flag_quote", ""),
        "desc": cfg.get("desc", ""),
        "price": None, "change_pct": None,
        "direction": None, "score": 0, "confidence": 0,
        "buy_signals": 0, "sell_signals": 0,
        "timeframes": {},
        "rsi": None, "ema20": None, "ema50": None, "atr_pips": None,
        "tp1": None, "tp2": None, "sl": None, "rr": None,
        "news_sentiment": {},
        "vote_log": [],
        "votes_long": 0, "votes_short": 0,
        "dxy_signal_dir": "",
        "confluence": False, "conflict": False, "setup_grade": "normal",
        "calendar": {"calendar_available": False},
        "error": None,
    }

    try:
        # 1. Fetch OHLCV per timeframe
        tf_map = {
            "scalping": [("15m", "2d", "15m"), ("1h", "5d", "1h")],
            "intraday": [("15m", "2d", "15m"), ("1h", "5d", "1h"),
                         ("4h", "30d", "1h"),  ("1d", "2y",  "1d")],
            "swing":    [("1h", "5d", "1h"),   ("4h", "30d", "1h"), ("1d", "2y", "1d")],
        }.get(mode, [("15m", "2d", "15m"), ("1h", "5d", "1h"),
                     ("4h", "30d", "1h"), ("1d", "2y", "1d")])

        dfs: dict[str, pd.DataFrame] = {}
        for tf_label, period, interval in tf_map:
            df = get_pair_ohlc(symbol, period, interval)
            # Resample 1h data to 4h
            if tf_label == "4h" and interval == "1h" and not df.empty:
                df = df.resample("4h").agg({
                    "Open": "first", "High": "max",
                    "Low": "min", "Close": "last", "Volume": "sum",
                }).dropna()
            dfs[tf_label] = df

        df_1h = dfs.get("1h", pd.DataFrame())
        df_15 = dfs.get("15m", pd.DataFrame())

        # Price from 1h
        if not df_1h.empty:
            result["price"] = float(last_scalar(df_1h["Close"]) or 0) or None
            if len(df_1h) >= 2:
                prev = float(last_scalar(df_1h["Close"].iloc[:-1]) or 0)
                cur  = result["price"] or 0
                if prev:
                    result["change_pct"] = round((cur - prev) / prev * 100, 4)

        # 2. TF analysis — mode-aware weights
        tf_weights = {
            "scalping": {"15m": 3, "1h": 2, "4h": 1, "1d": 0},
            "intraday": {"15m": 1, "1h": 3, "4h": 2, "1d": 1},
            "swing":    {"15m": 0, "1h": 1, "4h": 3, "1d": 3},
        }.get(mode, {"15m": 1, "1h": 3, "4h": 2, "1d": 1})

        buy_sigs = sell_sigs = 0
        vote_log: list[str] = []
        vl = vs = 0  # weighted votes

        for tf_label, df in dfs.items():
            if df.empty:
                continue
            w = tf_weights.get(tf_label, 1)
            if w == 0:
                continue
            try:
                ta = analyze_timeframe(tf_label, df)
                # Recalculate ATR in pips with correct pip size
                if ta.get("atr") is not None:
                    try:
                        ind = calculate_indicators(df)
                        _atr_raw = last_scalar(ind.get("ATR"))
                        if _atr_raw:
                            ta["atr"] = round(_atr_raw / pip_size, 1)
                    except Exception:
                        pass
                result["timeframes"][tf_label] = ta
                sig = ta.get("signal", "NEUTRAL")
                if sig == "COMPRA":
                    buy_sigs += 1; vl += w
                    vote_log.append(f"+{w} LONG — TF {tf_label} alcista")
                elif sig == "VENTA":
                    sell_sigs += 1; vs += w
                    vote_log.append(f"+{w} SHORT — TF {tf_label} bajista")
                # Grab 1h indicators for display
                if tf_label == "1h":
                    result["rsi"]   = ta.get("rsi")
                    result["atr_pips"] = ta.get("atr")
                    # EMA from 1h indicators
                    try:
                        ind1h = calculate_indicators(df_1h)
                        result["ema20"] = last_scalar(ind1h.get("EMA20"))
                        result["ema50"] = last_scalar(ind1h.get("EMA50"))
                    except Exception:
                        pass
            except Exception as _e:
                _log.debug("TF %s %s: %s", tf_label, symbol, _e)

        result["buy_signals"]  = buy_sigs
        result["sell_signals"] = sell_sigs

        # Snapshot técnico puro (antes de DXY/noticias) para detectar confluencia
        tech_vl, tech_vs = vl, vs
        tech_dir = "LONG" if tech_vl > tech_vs else ("SHORT" if tech_vs > tech_vl else "")

        # 3. DXY — adjusted direction per pair type
        dxy_signal_dir = ""
        if dxy_dir == "DOWN":
            dxy_signal_dir = "LONG"  if dxy_mode == "inverse" else "SHORT"
        elif dxy_dir == "UP":
            dxy_signal_dir = "SHORT" if dxy_mode == "inverse" else "LONG"
        result["dxy_signal_dir"] = dxy_signal_dir

        if dxy_signal_dir:
            w = 2
            if dxy_signal_dir == "LONG":
                vl += w; vote_log.append(f"+{w} LONG — DXY {dxy_dir.lower()} ({dxy_mode})")
            else:
                vs += w; vote_log.append(f"+{w} SHORT — DXY {dxy_dir.lower()} ({dxy_mode})")

        # 4. News sentiment per pair — peso escalado por fuerza e impacto
        news_dir = "NEUTRAL"
        news_w   = 0
        if news:
            ns = score_news_for_pair(symbol, news)
            result["news_sentiment"] = ns
            news_dir = ns.get("direction", "NEUTRAL")
            # Fuerza del sesgo de noticias = |diff base-quote|; nº de titulares relevantes
            _strength = abs(ns.get("base_score", 0.0) - ns.get("quote_score", 0.0))
            _ncount   = ns.get("base_count", 0) + ns.get("quote_count", 0)
            if news_dir in ("LONG", "SHORT"):
                # 1 base; +1 si sesgo fuerte (>0.20); +1 si 2+ titulares confirman
                news_w = 1 + (1 if _strength > 0.20 else 0) + (1 if _ncount >= 2 else 0)
                if news_dir == "LONG":
                    vl += news_w
                    vote_log.append(f"+{news_w} LONG — Noticias (fuerza {_strength:.2f}, {_ncount} titulares)")
                else:
                    vs += news_w
                    vote_log.append(f"+{news_w} SHORT — Noticias (fuerza {_strength:.2f}, {_ncount} titulares)")

        # 4b. Calendario económico — el "porqué" programado de las velas
        cal_block = False
        cal_dir   = ""
        try:
            from backend.econ_calendar import calendar_context
            cal = calendar_context(symbol)
            result["calendar"] = cal
            cal_block = bool(cal.get("block"))
            cal_dir   = cal.get("bias", "") or ""
            if cal.get("reason"):
                vote_log.append(f"📅 {cal['reason']}")
            # Sesgo direccional de una sorpresa macro reciente = señal fuerte (peso 2)
            if cal_dir in ("LONG", "SHORT"):
                if cal_dir == "LONG":
                    vl += 2; vote_log.append(f"+2 LONG — {cal.get('bias_reason','')}")
                else:
                    vs += 2; vote_log.append(f"+2 SHORT — {cal.get('bias_reason','')}")
        except Exception as _e:
            _log.debug("calendar %s: %s", symbol, _e)
            result["calendar"] = {"calendar_available": False}

        # 5. TP/SL from 1h data
        if not df_1h.empty and result["price"]:
            try:
                liq = detect_liquidity_levels(df_1h)
                ms  = detect_market_structure(df_1h)
                # Determine direction for TP/SL calc
                _dir = None
                if vl > vs:   _dir = "LONG"
                elif vs > vl: _dir = "SHORT"
                if _dir:
                    tp1, tp2, tp3, sl, rr, risk_pips, _atr_v, _warns = calc_smart_tp_sl(
                        result["price"], _dir, df_1h, liq, ms, result.get("atr_pips"))
                    result.update({"tp1": tp1, "tp2": tp2, "sl": sl, "rr": rr})
            except Exception as _e:
                _log.debug("TP/SL %s: %s", symbol, _e)

        # 6. Confluencia / conflicto noticias-técnico (coordinación inteligente)
        #    - Confluencia: noticias fuertes + técnico en la MISMA dirección → alta convicción
        #    - Conflicto:  noticias FUERTES (peso 3) contra técnico claro → VETO (fail-closed)
        confluence = False
        conflict   = False
        setup_grade = "normal"
        if news_dir in ("LONG", "SHORT") and tech_dir:
            if news_dir == tech_dir:
                confluence = True
                setup_grade = "alta_conviccion"
                vote_log.append(f"★ CONFLUENCIA — Noticias y técnico coinciden ({news_dir})")
            elif news_w >= 3 and abs(tech_vl - tech_vs) >= 2:
                # Noticias fuertes empujan en contra de un técnico claro → no operamos
                conflict = True
                vote_log.append(f"⚠ CONFLICTO — Noticias fuertes ({news_dir}) vs técnico ({tech_dir}) → VETO")
        # Calendario manda como filtro de riesgo: si hay evento de alto impacto
        # demasiado cerca, NO abrimos (el precio salta sin estructura = casino).
        if cal_block:
            conflict = True
            setup_grade = "evento_proximo"
            vote_log.append("⏸ VETO calendario — evento macro inminente, esperar")

        result["confluence"]  = confluence
        result["conflict"]    = conflict
        result["setup_grade"] = setup_grade

        # 7. Compute net direction + confidence
        result["votes_long"]  = vl
        result["votes_short"] = vs
        result["vote_log"]    = vote_log
        total = vl + vs
        if conflict:
            # Fail-closed: en conflicto fuerte o evento inminente no operamos
            result["direction"]  = None
            result["confidence"] = 0
        elif total == 0:
            result["direction"]  = None
            result["confidence"] = 0
        elif vl > vs:
            result["direction"]  = "LONG"
            result["confidence"] = min(95, round(vl / total * 100))
        elif vs > vl:
            result["direction"]  = "SHORT"
            result["confidence"] = min(95, round(vs / total * 100))
        else:
            result["direction"]  = None
            result["confidence"] = 50

        # 8. Score (0-100 scale for consistency with EUR/USD display)
        if conflict:
            result["score"] = 0
        else:
            _base = 40
            if total > 0:
                _base += int((max(vl, vs) / total) * 30)
            if dxy_signal_dir == result["direction"]:
                _base += 10
            if result["news_sentiment"].get("direction") == result["direction"]:
                _base += 8
            if confluence:
                _base += 12   # bonus de convicción cuando noticias + técnico alinean
            result["score"] = min(98, _base)

    except Exception as e:
        result["error"] = str(e)
        _log.warning("analyze_pair %s: %s", symbol, e)

    return result


# ── Parallel analysis of all 7 pairs ─────────────────────────────────────────

def analyze_all_pairs(
    dxy_dir: str = "",
    news: list | None = None,
    mode: str = "intraday",
    max_workers: int = 4,
) -> dict[str, dict]:
    """
    Analyze all 7 major pairs in parallel.
    Returns {symbol: analysis_dict}
    """
    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(analyze_pair, sym, dxy_dir, news, mode): sym
            for sym in PAIR_LIST
        }
        for fut in as_completed(futures, timeout=60):
            sym = futures[fut]
            try:
                results[sym] = fut.result()
            except Exception as e:
                _log.warning("analyze_all_pairs %s: %s", sym, e)
                results[sym] = {
                    "symbol": sym, "name": PAIRS[sym]["name"],
                    "error": str(e), "direction": None, "confidence": 0,
                    "price": None, "score": 0,
                }
    # Return in canonical order
    return {sym: results.get(sym, {"symbol": sym, "name": PAIRS[sym]["name"],
                                    "error": "no result"})
            for sym in PAIR_LIST}

"""
backend/market_context.py — Market regime, session, trading window, calendar, COT.
No Streamlit dependencies.
"""
import logging
import numpy as np
import pandas as pd
from datetime import datetime, timedelta

from backend.config import NEWS_API_KEY, UTC_OFFSET_SPAIN, PIP

# ── In-memory caches ──────────────────────────────────────────────────────────
_CALENDAR_CACHE = None
_CALENDAR_TTL   = timedelta(hours=4)
_COT_CACHE      = None
_COT_CACHE_TTL  = timedelta(hours=12)


# ============================================================
# MARKET SESSION
# ============================================================

def get_market_session():
    h = datetime.utcnow().hour
    if h >= 22 or h < 7: return "Sydney",      "BAJA",        "⚪"
    if 8  <= h < 13:      return "Londres",     "ALTA",        "🟢"
    if 13 <= h < 17:      return "Londres+NY",  "MUY ALTA ⚡", "🟡"
    if 17 <= h < 22:      return "Nueva York",  "ALTA",        "🟢"
    return                       "Tokio",       "MEDIA",       "🟡"


# ============================================================
# TRADING WINDOW (Spain time)
# ============================================================

def get_spain_hour():
    return (datetime.utcnow().hour + UTC_OFFSET_SPAIN) % 24


def is_trading_window():
    """True si hora España está en 07:00-20:00"""
    h = get_spain_hour()
    return 7 <= h < 20


def get_trading_window_info():
    h = get_spain_hour()
    if 7 <= h < 20:
        return True, "VENTANA TRADING (07:00-20:00)", f"Cierra en ~{20 - h}h | España aprox. {h:02d}:xx"
    elif h < 7:
        return False, "CERRADO (noche)", f"Abre en ~{7 - h}h"
    else:
        return False, "CERRADO (noche)", f"Abre mañana en ~{24 - h + 7}h"


# ============================================================
# ECONOMIC CALENDAR
# ============================================================

def get_economic_calendar():
    global _CALENDAR_CACHE
    if _CALENDAR_CACHE:
        ts, data = _CALENDAR_CACHE
        if datetime.now() - ts < _CALENDAR_TTL:
            return data
    try:
        import requests
        r = requests.get(
            "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
            timeout=10, headers={"User-Agent": "Mozilla/5.0 SMC-Bot/1.0"}
        )
        r.raise_for_status()
        all_ev = r.json()
        relevant = [e for e in all_ev
                    if e.get("impact", "").upper() in ("HIGH", "MEDIUM")
                    and e.get("currency", "") in ("EUR", "USD")]
        _CALENDAR_CACHE = (datetime.now(), relevant)
        return relevant
    except Exception as e:
        logging.warning(f"Calendar: {e}")
        return []


# ============================================================
# COT DATA
# ============================================================

def get_cot_data():
    """Obtiene COT (Commitment of Traders) para EUR FX Futures desde CFTC."""
    global _COT_CACHE
    if _COT_CACHE:
        ts, data = _COT_CACHE
        if datetime.now() - ts < _COT_CACHE_TTL:
            return data
    try:
        import requests
        url = (
            "https://publicreporting.cftc.gov/api/odata/v1/MarketsAndPositions"
            "?$filter=MarketAndExchangeNames eq 'EURO FX - CHICAGO MERCANTILE EXCHANGE'"
            "&$top=2&$orderby=ReportDate desc"
        )
        r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        values = r.json().get("value", [])
        if not values:
            return None
        latest = values[0]
        prev   = values[1] if len(values) > 1 else None
        nc_long  = int(latest.get("NonCommercialLong",  0) or 0)
        nc_short = int(latest.get("NonCommercialShort", 0) or 0)
        net      = nc_long - nc_short
        prev_net = 0
        if prev:
            prev_net = int(prev.get("NonCommercialLong", 0) or 0) - int(prev.get("NonCommercialShort", 0) or 0)
        change = net - prev_net
        result = {
            "date":           (latest.get("ReportDate") or "")[:10],
            "nc_long":        nc_long,
            "nc_short":       nc_short,
            "net":            net,
            "prev_net":       prev_net,
            "change":         change,
            "bias":           "ALCISTA (EUR)" if net > 0 else "BAJISTA (EUR)",
            "bias_direction": "LONG" if net > 0 else "SHORT",
            "change_lbl":     "Aumentando longs" if change > 0 else "Reduciendo longs",
        }
        _COT_CACHE = (datetime.now(), result)
        return result
    except Exception as e:
        logging.warning(f"COT data error: {e}")
        return None


def interpret_cot_for_signal(cot):
    """Convierte COT en sesgo direccional para el score de confluencia."""
    if not cot:
        return None, 0
    net = cot["net"]
    change = cot["change"]
    if net > 50000 and change > 0:
        return "LONG", 15
    elif net > 20000:
        return "LONG", 8
    elif net < -50000 and change < 0:
        return "SHORT", 15
    elif net < -20000:
        return "SHORT", 8
    return "NEUTRAL", 0


# ============================================================
# MARKET REGIME DETECTION
# ============================================================

def detect_market_regime(df, calendar=None):
    """
    Clasifica el mercado actual: trending_bull, trending_bear, ranging,
    volatile, volatile_trend, pre_news.
    Devuelve (regime_key, regime_label, details_dict).
    """
    if df.empty or len(df) < 50:
        return "unknown", "Desconocido", {}

    close = df["Close"]
    high  = df["High"]
    low   = df["Low"]
    c     = float(close.iloc[-1])
    _pip  = 0.0001

    e9  = float(close.ewm(span=9,  adjust=False).mean().iloc[-1])
    e21 = float(close.ewm(span=21, adjust=False).mean().iloc[-1])
    e50 = float(close.ewm(span=50, adjust=False).mean().iloc[-1])

    dc   = close.diff()
    gain = dc.clip(lower=0).rolling(14).mean()
    loss = (-dc.clip(upper=0)).rolling(14).mean()
    rsi  = float((100 - 100 / (1 + gain / loss.replace(0, np.nan))).iloc[-1])
    if np.isnan(rsi):
        rsi = 50.0

    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low  - close.shift()).abs(),
    ], axis=1).max(axis=1)
    atr14  = float(tr.rolling(14).mean().iloc[-1]) / _pip
    atr_avg = float(tr.rolling(50).mean().iloc[-1]) / _pip if len(tr) >= 50 else atr14
    high_vol = atr14 > atr_avg * 1.3

    ema_spread = abs(e9 - e50) / (_pip * 10)
    trending   = ema_spread > 3.0
    bull       = e9 > e21 > e50
    bear       = e9 < e21 < e50

    news_risk       = "low"
    minutes_to_news = None
    if calendar:
        now_utc = datetime.utcnow()
        high_ev = [e for e in calendar
                   if e.get("impact", "").upper() == "HIGH"
                   and e.get("currency", "") in ("EUR", "USD")]
        best_delta = None
        for ev in high_ev:
            try:
                ev_dt = datetime.strptime(str(ev.get("date", ""))[:16], "%Y-%m-%dT%H:%M")
                dm = (ev_dt - now_utc).total_seconds() / 60
                if -30 <= dm <= 120:
                    if best_delta is None or abs(dm) < abs(best_delta):
                        best_delta = dm
            except Exception:
                pass
        if best_delta is not None:
            minutes_to_news = int(best_delta)
            news_risk = "high" if -30 <= best_delta <= 60 else "medium"

    if news_risk == "high":
        regime, lbl = "pre_news",       "Riesgo Noticias — Precaución"
    elif high_vol and trending:
        regime, lbl = "volatile_trend", "Tendencia Explosiva (alta volatilidad)"
    elif trending and bull:
        regime, lbl = "trending_bull",  "Tendencia Alcista"
    elif trending and bear:
        regime, lbl = "trending_bear",  "Tendencia Bajista"
    elif high_vol:
        regime, lbl = "volatile",       "Volatilidad Alta (sin tendencia clara)"
    else:
        regime, lbl = "ranging",        "Mercado Lateral / Rango"

    details = {
        "regime":     regime,
        "rsi":        round(rsi, 1),
        "atr_pips":   round(atr14, 1),
        "atr_avg":    round(atr_avg, 1),
        "high_vol":   high_vol,
        "trending":   trending,
        "bull":       bull,
        "bear":       bear,
        "news_risk":  news_risk,
        "ema_spread": round(ema_spread, 1),
    }
    if minutes_to_news is not None:
        details["minutes_to_news"] = minutes_to_news

    return regime, lbl, details


# ============================================================
# MARKET CONTEXT — WHY IS EUR/USD HERE?
# ============================================================

def explain_market_context(df, cot=None, calendar=None, news=None):
    """Devuelve lista de cadenas explicando por qué el EUR/USD está donde está."""
    if df.empty or len(df) < 55:
        return ["Sin datos suficientes para contexto."]

    close = df["Close"]
    c     = float(close.iloc[-1])
    ema21 = close.ewm(span=21,  adjust=False).mean()
    ema50 = close.ewm(span=50,  adjust=False).mean()
    ema200= close.ewm(span=200, adjust=False).mean() if len(close) >= 200 else None
    e21   = float(ema21.iloc[-1])
    e50   = float(ema50.iloc[-1])

    dc   = close.diff()
    gain = dc.clip(lower=0).rolling(14).mean()
    loss = (-dc.clip(upper=0)).rolling(14).mean()
    rsi_v = float((100 - (100 / (1 + gain / loss.replace(0, np.nan)))).iloc[-1])

    macd_l = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
    hist_v = float((macd_l - macd_l.ewm(span=9, adjust=False).mean()).iloc[-1])

    reasons = []

    trend = "ALCISTA" if c > e50 else "BAJISTA"
    reasons.append(
        f"📈 TENDENCIA ({trend}): Precio {c:.5f} está "
        f"{'SOBRE' if c > e50 else 'BAJO'} la EMA50 ({e50:.5f}). "
        f"El mercado de corto plazo favorece posiciones {'LONG' if c > e50 else 'SHORT'}."
    )

    if ema200 is not None:
        e200 = float(ema200.iloc[-1])
        macro = "ALCISTA" if c > e200 else "BAJISTA"
        reasons.append(
            f"🗺️ MACRO (EMA200): Tendencia institucional {macro}. "
            f"EUR/USD {'por encima' if c > e200 else 'por debajo'} de {e200:.5f}. "
            + (
                "Los grandes fondos mantienen posición neta LARGA en EUR."
                if c > e200 else
                "Los grandes fondos mantienen posición neta CORTA en EUR."
            )
        )

    if not np.isnan(rsi_v):
        if rsi_v > 70:
            reasons.append(f"⚠️ RSI={rsi_v:.0f} — SOBRECOMPRA. Alta probabilidad de pausa o retroceso técnico a EMA21 ({e21:.5f}).")
        elif rsi_v < 30:
            reasons.append(f"⚠️ RSI={rsi_v:.0f} — SOBREVENTA. Alta probabilidad de rebote técnico hacia EMA21 ({e21:.5f}).")
        elif rsi_v > 55:
            reasons.append(f"✅ RSI={rsi_v:.0f} — Compradores en control. Momentum alcista confirmado.")
        else:
            reasons.append(f"🔻 RSI={rsi_v:.0f} — Vendedores en control. Momentum bajista activo.")

    reasons.append(
        f"{'✅' if hist_v > 0 else '🔻'} MACD histogram {'positivo' if hist_v > 0 else 'negativo'} — "
        f"la fuerza del movimiento apunta {'ARRIBA (compradores)' if hist_v > 0 else 'ABAJO (vendedores)'}."
    )

    if cot:
        net    = cot.get("net", 0)
        change = cot.get("change", 0)
        if abs(net) > 50000:
            bias_lbl = "MUY ALCISTA" if net > 0 else "MUY BAJISTA"
        elif abs(net) > 20000:
            bias_lbl = "ALCISTA" if net > 0 else "BAJISTA"
        else:
            bias_lbl = "NEUTRAL"
        reasons.append(
            f"🏦 INVERSORES INSTITUCIONALES (CFTC COT): {bias_lbl} en EUR. "
            f"Posición neta especuladores: {net:+,.0f} contratos. "
            f"Cambio esta semana: {change:+,.0f}. "
            + (
                "Los hedge funds llevan semanas COMPRANDO EUR → fuerza alcista estructural."
                if net > 50000 else
                "Los hedge funds llevan semanas VENDIENDO EUR → presión bajista estructural."
                if net < -50000 else
                "Posicionamiento institucional neutro — el mercado espera un catalizador."
            )
        )

    if calendar:
        high_ev = [e for e in calendar if e.get("impact", "").upper() == "HIGH"]
        if high_ev:
            reasons.append(f"📅 CALENDARIO ({len(high_ev)} eventos ALTO impacto esta semana):")
            for ev in high_ev[:4]:
                cur   = ev.get("currency", "")
                title = ev.get("title", "")
                prev  = ev.get("previous", "?")
                fore  = ev.get("forecast", "?")
                date  = str(ev.get("date", ""))[:10]
                effect = (
                    f"Si dato > pronóstico → USD sube → EUR/USD BAJA."
                    if cur == "USD" else
                    f"Si dato > pronóstico → EUR sube → EUR/USD SUBE."
                )
                reasons.append(f"  → [{cur}] {title} | Anterior:{prev} Pronóstico:{fore} | {date} — {effect}")
        med_ev = [e for e in calendar if e.get("impact", "").upper() == "MEDIUM"]
        if med_ev:
            reasons.append(f"  ({len(med_ev)} eventos de impacto MEDIO esta semana — monitorear.)")

    if news:
        top = sorted(
            [n for n in news if n.get("impact_score", 0) >= 6],
            key=lambda x: x.get("impact_score", 0), reverse=True
        )[:3]
        for n in top:
            reasons.append(
                f"📰 NOTICIA ({n.get('impact_label','ALTA')}): "
                f"{n.get('title','')[:85]} "
                f"[{n.get('source',{}).get('name','')}]"
            )

    return reasons

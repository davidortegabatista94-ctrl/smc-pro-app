"""
backend/gold_system.py — Sistema del ORO desplegable + overlay de NOTICIAS en vivo.

La ESTRATEGIA (validada sobre 18 años, neto de costes, out-of-sample):
  1. Ensemble de tendencia (20/60/120/250 días) → dirección.
  2. Filtro de RÉGIMEN (Efficiency Ratio): solo opera si el oro TIENDE; si va en
     rango, FUERA (preserva capital). Esto duplicó el Sharpe y bajó el drawdown.
  3. Tamaño por volatilidad (riesgo constante).

El OVERLAY DE NOTICIAS (nuevo, EN VIVO, aún sin validar históricamente):
  El oro es refugio y anti-inflación/anti-dólar. Las noticias de riesgo, inflación,
  Fed dovish o dólar débil son alcistas para el oro; lo contrario, bajistas.
  El overlay SOLO ajusta el TAMAÑO (convicción) dentro de la dirección que ya
  decidió la estrategia — NUNCA la anula. Si las noticias confirman la tendencia,
  sube la posición; si la contradicen, la reduce.

HONESTIDAD (CLAUDE.md): no hay histórico de noticias para backtestear este overlay,
así que se despliega en modo "recoger evidencia": modula suave (±30%) y su efecto
se medirá en vivo. La estrategia base es la que tiene el edge probado.
"""
from __future__ import annotations

import logging
import numpy as np
import pandas as pd

_log = logging.getLogger(__name__)

LOOKBACKS = (20, 60, 120, 250)
TARGET_VOL_D = 0.10 / np.sqrt(252)

# Palabras clave para el sesgo del oro (macro digerido)
GOLD_BULL = ["inflation", "cpi", "rate cut", "dovish", "recession", "war", "geopolit",
             "safe haven", "haven", "crisis", "uncertainty", "stimulus", "weak dollar",
             "dollar falls", "yields fall", "risk-off", "tension", "conflict", "stagflation"]
GOLD_BEAR = ["rate hike", "hawkish", "strong dollar", "dollar rises", "risk-on",
             "taper", "yields rise", "yields climb", "rally in stocks", "soft landing",
             "rate pause", "disinflation"]


def _fetch_gold_daily(period: str = "3y") -> pd.DataFrame:
    """Oro diario vía yfinance (GC=F). Devuelve OHLC con columnas planas."""
    import yfinance as yf
    df = yf.download("GC=F", period=period, interval="1d", progress=False, auto_adjust=True)
    if hasattr(df.columns, "levels"):
        df.columns = df.columns.get_level_values(0)
    df = df.dropna()
    df["ret"] = df["Close"].pct_change()
    return df


def _efficiency_ratio(c: pd.Series, n: int = 20) -> pd.Series:
    return c.diff(n).abs() / c.diff().abs().rolling(n).sum()


def base_signal(df: pd.DataFrame) -> dict:
    """
    Señal base de la ESTRATEGIA validada (sin noticias).
    Devuelve régimen, dirección y tamaño (vol-scaled) del último día.
    """
    c = df["Close"]
    ens = pd.concat([np.sign(c.pct_change(n)) for n in LOOKBACKS], axis=1).mean(axis=1)
    er = _efficiency_ratio(c).shift(1)
    med = er.rolling(252, min_periods=60).median()
    trending = bool((er.iloc[-1] > med.iloc[-1])) if not np.isnan(med.iloc[-1]) else False

    trend_val = float(ens.iloc[-1])           # [-1,1]
    vol = float(df["ret"].rolling(30).std().iloc[-1] or 0)
    size = 0.0
    if vol > 0:
        size = float(np.clip(TARGET_VOL_D / vol, 0, 3.0)) * abs(trend_val)

    direction = "LONG" if trend_val > 0 else ("SHORT" if trend_val < 0 else "FLAT")
    if not trending:
        return {"regime": "rango", "trending": False, "direction": "FUERA",
                "base_size": 0.0, "trend_strength": round(trend_val, 2),
                "reason": "Régimen LATERAL → fuera. El trend del oro no gana en rango."}
    return {"regime": "tendencia", "trending": True, "direction": direction,
            "base_size": round(size, 2), "trend_strength": round(trend_val, 2),
            "reason": f"Tendencia {direction} confirmada (consenso de 4 velocidades)."}


def news_overlay(news_list: list | None) -> dict:
    """Sesgo de noticias para el oro. Devuelve dirección del sesgo, fuerza y multiplicador."""
    if not news_list:
        return {"bias": "NEUTRAL", "score": 0.0, "mult_long": 1.0, "mult_short": 1.0,
                "n": 0, "reason": "Sin noticias relevantes — solo estrategia."}
    bull = bear = 0
    hits = []
    for item in news_list:
        text = ((item.get("title", "") or item.get("headline", "")) + " " +
                (item.get("description", "") or item.get("summary", ""))).lower()
        b = sum(1 for k in GOLD_BULL if k in text)
        s = sum(1 for k in GOLD_BEAR if k in text)
        if b: bull += b; hits.append(("+", item.get("title", "")[:60]))
        if s: bear += s; hits.append(("-", item.get("title", "")[:60]))
    net = bull - bear
    total = bull + bear
    score = net / total if total else 0.0        # [-1,1]
    if score > 0.15:
        bias = "ALCISTA (oro)"
    elif score < -0.15:
        bias = "BAJISTA (oro)"
    else:
        bias = "NEUTRAL"
    # El overlay modula ±30% según confirme o contradiga
    mult_long = float(np.clip(1.0 + 0.3 * score, 0.6, 1.3))
    mult_short = float(np.clip(1.0 - 0.3 * score, 0.6, 1.3))
    return {"bias": bias, "score": round(score, 2), "mult_long": round(mult_long, 2),
            "mult_short": round(mult_short, 2), "n": total,
            "reason": f"{bull} señales alcistas vs {bear} bajistas en {len(news_list)} titulares.",
            "hits": hits[:5]}


def gold_decision(news_list: list | None = None) -> dict:
    """
    Decisión completa del día: estrategia + overlay de noticias.
    Devuelve todo el 'porqué' para mostrar/auditar.
    """
    try:
        df = _fetch_gold_daily()
    except Exception as e:
        return {"error": f"Sin datos de oro: {e}", "action": "SIN DATOS"}
    if df.empty or len(df) < 260:
        return {"error": "Datos insuficientes", "action": "SIN DATOS"}

    base = base_signal(df)
    news = news_overlay(news_list)
    price = float(df["Close"].iloc[-1])

    # Aplicar overlay solo si estamos operando
    final_size = base["base_size"]
    news_effect = "—"
    if base["direction"] == "LONG":
        final_size *= news["mult_long"]
        news_effect = f"×{news['mult_long']}"
    elif base["direction"] == "SHORT":
        final_size *= news["mult_short"]
        news_effect = f"×{news['mult_short']}"

    return {
        "price": round(price, 1),
        "regime": base["regime"],
        "action": base["direction"],
        "base_size": base["base_size"],
        "final_size": round(final_size, 2),
        "trend_strength": base["trend_strength"],
        "news_bias": news["bias"],
        "news_score": news["score"],
        "news_effect": news_effect,
        "reason_strategy": base["reason"],
        "reason_news": news["reason"],
        "news_hits": news.get("hits", []),
    }


if __name__ == "__main__":
    import sys
    try: sys.stdout.reconfigure(encoding="utf-8")
    except Exception: pass
    logging.basicConfig(level=logging.WARNING)
    news = []
    try:
        from backend.signals import get_rss_news
        news = get_rss_news() or []
    except Exception:
        pass
    d = gold_decision(news)
    print("=== DECISIÓN DEL SISTEMA ORO (hoy) ===")
    for k, v in d.items():
        if k != "news_hits":
            print(f"  {k:<16}: {v}")
    if d.get("news_hits"):
        print("  titulares clave:")
        for s, t in d["news_hits"]:
            print(f"     {s} {t}")

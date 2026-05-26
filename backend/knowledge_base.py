"""
backend/knowledge_base.py — KB persistence + online learning.
No Streamlit dependencies. Uses JSON file for storage.
"""
import json
import os
import logging
from datetime import datetime

from backend.config import PIP

_KB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "knowledge_base.json")

# ── Strategy metadata (labels + descriptions for UI + scoring) ───────────────
_STRATEGY_REGIME_AFFINITY = {
    "ema_trend":           ["trending_bull", "trending_bear", "volatile_trend"],
    "ema_crossover":       ["trending_bull", "trending_bear", "volatile_trend"],
    "triple_ema":          ["volatile_trend", "trending_bull", "trending_bear"],
    "ema_ribbon":          ["trending_bull", "trending_bear"],
    "macd_cross":          ["trending_bull", "trending_bear", "volatile_trend"],
    "rsi_reversion":       ["trending_bull", "trending_bear"],
    "rsi_50_cross":        ["ranging", "trending_bull", "trending_bear"],
    "stochastic_trend":    ["ranging", "trending_bull"],
    "bb_touch":            ["ranging"],
    "keltner_touch":       ["ranging"],
    "donchian_breakout":   ["volatile_trend", "trending_bull", "trending_bear"],
    "supertrend":          ["trending_bull", "trending_bear", "volatile_trend"],
    "market_structure_bo": ["trending_bull", "trending_bear", "volatile_trend"],
    "momentum_breakout":   ["volatile_trend", "volatile"],
    "aggressive_momentum": ["volatile_trend", "volatile"],
    "meta_composite":      ["trending_bull", "trending_bear", "ranging", "volatile_trend"],
    "precision_be":        ["trending_bull", "trending_bear"],
}

_STRATEGY_META = {
    "ema_trend": {
        "label": "EMA Trend (9/21/50 + MACD + RSI)",
        "why":   "Las 3 EMAs alineadas en los 3 horizontes + confirmación MACD y RSI.",
        "pros":  "Bajo drawdown · Alta selectividad",
        "cons":  "Pocas señales en rangos",
    },
    "ema_crossover": {
        "label": "EMA Crossover 9/21 + EMA50 filtro",
        "why":   "EMA9 cruza EMA21 con precio al lado correcto de EMA50.",
        "pros":  "Entrada temprana en impulsos · Buena frecuencia",
        "cons":  "Whipsaws en rangos laterales",
    },
    "triple_ema": {
        "label": "Triple EMA 3/8/21 (sistema rápido)",
        "why":   "EMAs 3/8/21 alineadas con momentum fuerte.",
        "pros":  "Muy sensible · Muchas señales en tendencias",
        "cons":  "Alta frecuencia de señales falsas en laterales",
    },
    "ema_ribbon": {
        "label": "EMA Ribbon 5/10/20/50 (multi-marco)",
        "why":   "5 EMAs alineadas confirman tendencia en 5 horizontes.",
        "pros":  "Señales muy robustas · Bajo drawdown",
        "cons":  "Muy pocas señales — solo en tendencias limpias",
    },
    "macd_cross": {
        "label": "MACD Crossover (hist cruza cero) + EMA50",
        "why":   "Cruce del histograma MACD señala cambio de momentum.",
        "pros":  "Entra pronto en tendencias · Buena frecuencia",
        "cons":  "Señales falsas en laterales",
    },
    "rsi_reversion": {
        "label": "RSI Reversion en Tendencia (pullback a 45)",
        "why":   "En tendencia, espera pullback RSI 40-48 y rebote.",
        "pros":  "Win rate alta · Entradas en mínimos de corrección",
        "cons":  "Requiere tendencia clara previa",
    },
    "rsi_50_cross": {
        "label": "RSI cruza nivel 50 + MACD + EMA50",
        "why":   "RSI cruza 50 con precio al lado correcto de EMA50 y MACD confirmando.",
        "pros":  "Simple · Frecuencia moderada · Buenas confirmaciones",
        "cons":  "RSI puede oscilar alrededor del 50 en rangos",
    },
    "stochastic_trend": {
        "label": "Estocástico (14,3) reversión en tendencia",
        "why":   "%K cruza %D saliendo de oversold (< 25) en tendencia alcista.",
        "pros":  "Entradas muy precisas en correcciones · Clásico probado",
        "cons":  "Puede señalizar early en tendencias muy fuertes",
    },
    "bb_touch": {
        "label": "Bollinger Band Touch (−2σ) + RSI",
        "why":   "Toca la banda inferior en tendencia alcista con RSI < 45.",
        "pros":  "Entradas muy precisas · Funciona bien en EUR/USD",
        "cons":  "Precio puede pegarse a la banda en tendencias fuertes",
    },
    "keltner_touch": {
        "label": "Keltner Channel Touch (EMA20 ± 2.5×ATR)",
        "why":   "Keltner filtra mejor la volatilidad que Bollinger.",
        "pros":  "Menos falsas señales que BB · Usa volatilidad real (ATR)",
        "cons":  "Señales poco frecuentes en mercados de baja volatilidad",
    },
    "donchian_break": {
        "label": "Donchian Breakout 20 períodos + EMA50",
        "why":   "Romper el máximo de 20 velas con precio sobre EMA50.",
        "pros":  "Captura movimientos grandes · Sin indicadores rezagados",
        "cons":  "Falsas rupturas frecuentes sin filtros adicionales",
    },
    "momentum_break": {
        "label": "ATR Momentum Breakout (máx/mín 10 velas)",
        "why":   "Precio rompe máximo de 10 velas con momentum confirmado (RSI > 50).",
        "pros":  "Captura impulsos fuertes · R:R favorable",
        "cons":  "Puede entrar tarde en el movimiento",
    },
    "supertrend": {
        "label": "SuperTrend (EMA(H+L/2) ± 3×ATR10)",
        "why":   "Soporte/resistencia dinámico basado en ATR.",
        "pros":  "Muy visual · Pocas señales pero de alta calidad",
        "cons":  "Rezagado por naturaleza — entra tarde en reversiones",
    },
    "engulfing": {
        "label": "Engulfing Pattern (velas envolventes) + EMA50",
        "why":   "Vela envolvente en zona de soporte/EMA50 señala rechazo.",
        "pros":  "Señal de acción del precio pura · Sin indicadores",
        "cons":  "Necesita contexto (nivel de soporte/tendencia)",
    },
    "aggressive_momentum": {
        "label": "AGRESIVA: Momentum Explosivo (ATR alto + vela fuerte)",
        "why":   "Vela fuerte + ATR ≥ 6 pips + EMAs alineadas. Sin filtro RSI.",
        "pros":  "Captura impulsos explosivos · Más operaciones en tendencias fuertes",
        "cons":  "Mayor drawdown · Requiere volatilidad alta",
    },
    "meta_composite": {
        "label": "META-Composite: Consenso Inteligente (6 estrategias)",
        "why":   "Vota entre 6 estrategias. Entra solo cuando ≥3 coinciden.",
        "pros":  "Señales de altísima calidad · Drawdown mínimo",
        "cons":  "Pocas señales — solo en confluencia perfecta",
    },
    "precision_be": {
        "label": "Precisión BE: Pullback EMA21 con Break-Even Automático",
        "why":   "Pullbacks exactos a EMA21 en tendencia. BE automático tras 1×SL.",
        "pros":  "Capital protegido · Win rate efectiva alta",
        "cons":  "Requiere tendencia + pullback exacto",
    },
}

_REGIME_LABELS = {
    "trending_bull":  "Tendencia Alcista",
    "trending_bear":  "Tendencia Bajista",
    "volatile_trend": "Tendencia Explosiva",
    "volatile":       "Alta Volatilidad",
    "ranging":        "Mercado Lateral",
    "pre_news":       "Riesgo Noticias",
    "unknown":        "Desconocido",
}
_REGIME_ICONS = {
    "trending_bull":  "📈",
    "trending_bear":  "📉",
    "volatile_trend": "⚡",
    "volatile":       "🌪️",
    "ranging":        "↔️",
    "pre_news":       "⚠️",
    "unknown":        "❓",
}


# ── KB CRUD ──────────────────────────────────────────────────────────────────

def load_knowledge_base():
    try:
        if os.path.exists(_KB_FILE):
            with open(_KB_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {"runs": [], "best_strategy": None, "strategy_wins": {}}


def save_knowledge_base(kb):
    try:
        with open(_KB_FILE, "w", encoding="utf-8") as f:
            json.dump(kb, f, indent=2, ensure_ascii=False, default=str)
    except Exception as e:
        logging.warning(f"KB save: {e}")


def update_kb(comparison_result, cot=None, calendar=None, market_ctx=None):
    kb = load_knowledge_base()
    best = comparison_result["best"]
    entry = {
        "ts":       datetime.now().isoformat()[:16],
        "best":     best["strategy"],
        "pf":       best["profit_factor"],
        "wr":       best["winrate"],
        "total":    best["total"],
        "net_pips": best["net_pips"],
        "strategies": [
            {"n": r["strategy"], "pf": r["profit_factor"],
             "wr": r["winrate"], "total": r["total"]}
            for r in comparison_result["results"]
        ],
        "cot_bias":    cot.get("bias") if cot else None,
        "events_high": sum(1 for e in (calendar or []) if e.get("impact", "").upper() == "HIGH"),
        "market_ctx":  (market_ctx or [])[:6],
    }
    kb["runs"] = (kb.get("runs", []) + [entry])[-50:]
    wins = kb.get("strategy_wins", {})
    wins[best["strategy"]] = wins.get(best["strategy"], 0) + 1
    kb["strategy_wins"] = wins
    recent = kb["runs"][-5:]
    votes = {}
    for r in recent:
        votes[r["best"]] = votes.get(r["best"], 0) + 1
    kb["best_strategy"] = max(votes, key=votes.get) if votes else best["strategy"]
    save_knowledge_base(kb)
    return kb


def kb_record_pending_signal(direction, price, strategy, reason, df=None, cot=None, calendar=None):
    """Guarda la señal actual con contexto técnico+fundamental para evaluarla después."""
    kb = load_knowledge_base()
    context = {}
    if df is not None and not df.empty:
        try:
            from backend.market_context import detect_market_regime
            regime, regime_lbl, regime_details = detect_market_regime(df, calendar)
            context["regime"]     = regime
            context["regime_lbl"] = regime_lbl
            context["rsi"]        = regime_details.get("rsi")
            context["atr_pips"]   = regime_details.get("atr_pips")
            context["high_vol"]   = regime_details.get("high_vol", False)
            context["news_risk"]  = regime_details.get("news_risk", "low")
            if "minutes_to_news" in regime_details:
                context["minutes_to_news"] = regime_details["minutes_to_news"]
        except Exception:
            pass
    if cot:
        context["cot_bias"] = cot.get("bias", "neutral")

    kb["pending_signal"] = {
        "ts":        datetime.now().isoformat()[:19],
        "direction": direction,
        "price":     price,
        "strategy":  strategy,
        "reason":    reason,
        "context":   context,
    }
    save_knowledge_base(kb)


def kb_evaluate_and_learn(current_price):
    """Compara señal pendiente con precio actual y actualiza estadísticas con contexto."""
    kb = load_knowledge_base()
    pending = kb.get("pending_signal")
    if not pending or pending.get("direction") == "NO TRADE":
        return kb
    direction   = pending["direction"]
    entry_price = pending.get("price")
    strategy    = pending.get("strategy", "unknown")
    context     = pending.get("context", {})
    if entry_price is None or current_price is None:
        return kb
    move_pips = (current_price - entry_price) / 0.0001
    if direction == "LONG":
        correct = move_pips > 3
    elif direction == "SHORT":
        correct = move_pips < -3
    else:
        return kb
    outcome_key = "correct" if correct else "wrong"

    stats = kb.get("signal_stats", {})
    s = stats.get(strategy, {"correct": 0, "wrong": 0, "by_regime": {}, "by_news_risk": {}})
    s[outcome_key] = s.get(outcome_key, 0) + 1

    regime = context.get("regime", "unknown")
    by_regime = s.get("by_regime", {})
    r_s = by_regime.get(regime, {"correct": 0, "wrong": 0})
    r_s[outcome_key] = r_s.get(outcome_key, 0) + 1
    by_regime[regime] = r_s
    s["by_regime"] = by_regime

    news_risk = context.get("news_risk", "low")
    by_news = s.get("by_news_risk", {})
    n_s = by_news.get(news_risk, {"correct": 0, "wrong": 0})
    n_s[outcome_key] = n_s.get(outcome_key, 0) + 1
    by_news[news_risk] = n_s
    s["by_news_risk"] = by_news

    stats[strategy] = s
    kb["signal_stats"] = stats
    kb.pop("pending_signal", None)
    save_knowledge_base(kb)
    return kb


def kb_best_strategy_for_conditions(df, cot=None, calendar=None):
    """
    Selecciona la mejor estrategia según régimen de mercado actual.
    Devuelve (strategy_key, regime_key, regime_label, regime_details, explanation_why).
    """
    from backend.market_context import detect_market_regime
    kb     = load_knowledge_base()
    regime, regime_lbl, regime_details = detect_market_regime(df, calendar)
    stats  = kb.get("signal_stats", {})
    wins   = kb.get("strategy_wins", {})
    total_runs = len(kb.get("runs", []))

    scores = {}
    explanations = {}

    for strat in _STRATEGY_META.keys():
        score = 0.0
        parts = []

        s   = stats.get(strat, {})
        ok  = s.get("correct", 0)
        ko  = s.get("wrong", 0)
        tot = ok + ko
        if tot > 0:
            wr = ok / tot
            score += wr * 40
            parts.append(f"{ok}/{tot} señales correctas ({wr*100:.0f}%)")

        by_regime = s.get("by_regime", {})
        r_s  = by_regime.get(regime, {})
        r_ok = r_s.get("correct", 0)
        r_ko = r_s.get("wrong", 0)
        if r_ok + r_ko >= 2:
            r_wr = r_ok / (r_ok + r_ko)
            score += r_wr * 35
            parts.append(f"En {regime_lbl}: {r_ok}/{r_ok+r_ko} ({r_wr*100:.0f}%)")
        elif regime in _STRATEGY_REGIME_AFFINITY.get(strat, []):
            score += 15
            parts.append(f"Diseñada para {regime_lbl}")

        if total_runs > 0:
            score += (wins.get(strat, 0) / total_runs) * 15

        if cot:
            cot_bias = cot.get("bias", "neutral")
            if cot_bias == "bullish" and regime in ("trending_bull", "volatile_trend"):
                score += 10
                parts.append("COT institucional alcista confirma dirección")
            elif cot_bias == "bearish" and regime in ("trending_bear", "volatile_trend"):
                score += 10
                parts.append("COT institucional bajista confirma dirección")

        scores[strat]       = score
        explanations[strat] = parts

    if any(v > 0 for v in scores.values()):
        best = max(scores, key=scores.get)
    else:
        best = kb.get("best_strategy")

    if best is None:
        best = next(iter(_STRATEGY_META))

    why_parts = explanations.get(best, [])
    why = f"Seleccionada por régimen actual ({regime_lbl})"
    if why_parts:
        why += " — " + " · ".join(why_parts[:3])

    return best, regime, regime_lbl, regime_details, why

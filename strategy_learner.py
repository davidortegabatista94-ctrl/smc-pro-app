"""
strategy_learner.py — Meta-aprendizaje y estrategia maestra adaptativa.

Cada 6 horas analiza TODA la información disponible:
  · Observaciones de mercado (sesión, régimen, DXY, score, señal)
  · Datos macro FRED (tipos de interés, inflación, empleo)
  · Errores del sistema y ciclos de auto-mejora previos
  · Versiones anteriores del DNA y su rendimiento

Con todo eso, la IA sintetiza una ESTRATEGIA MAESTRA que:
  · Pondera cada fuente de señal (técnico, DXY, sentimiento, volumen, macro)
  · Ajusta umbrales por régimen y sesión
  · Se actualiza automáticamente cada 6 horas
  · Aprende qué combinación de condiciones produce las mejores señales
"""

import json
import logging
from datetime import datetime, timezone, timedelta
from collections import defaultdict

import db as _db
import ai_engine as _ai

_log = logging.getLogger("smc.learner")

_LEARN_INTERVAL_HOURS = 6
_LAST_LEARN_KEY       = "last_learn_ts"

# Pesos por defecto del DNA maestro (se sobreescriben al aprender)
DEFAULT_MASTER_DNA = {
    "version":        1,
    "source":         "default",
    "signal_weights": {
        "technical":   0.40,
        "dxy":         0.20,
        "volume":      0.20,
        "sentiment":   0.10,
        "fundamental": 0.10,
    },
    "session_weights": {
        "London": 1.00,
        "NY":     0.90,
        "Asia":   0.55,
        "Off":    0.25,
    },
    "regime_thresholds": {
        "trending_up":   65,
        "trending_down": 65,
        "ranging":       72,
        "neutral":       70,
        "unknown":       68,
    },
    "best_combos":  [],   # [(session, regime, dxy_dir, avg_score)]
    "worst_combos": [],   # condiciones a evitar
    "ai_insight":   "DNA inicial — sin datos suficientes aún",
    "evolved_at":   None,
}


# ─────────────────────────────────────────────────────────────────────────────
# CONTROL DE TIEMPO
# ─────────────────────────────────────────────────────────────────────────────

def should_run_learning() -> bool:
    try:
        last = _db.get_setting(_LAST_LEARN_KEY)
        if not last:
            return True
        last_dt = datetime.fromisoformat(last)
        if not last_dt.tzinfo:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - last_dt).total_seconds() > _LEARN_INTERVAL_HOURS * 3600
    except Exception:
        return True


def mark_learning_done() -> None:
    _db.set_setting(_LAST_LEARN_KEY, datetime.now(timezone.utc).isoformat())


# ─────────────────────────────────────────────────────────────────────────────
# ANÁLISIS DE OBSERVACIONES
# ─────────────────────────────────────────────────────────────────────────────

def _analyze_observations(observations: list[dict]) -> dict:
    """
    Agrupa observaciones por (sesión, régimen, dxy_dir) y calcula
    el score medio y la distribución de señales en cada combo.
    """
    groups: dict = defaultdict(list)
    for obs in observations:
        ctx = obs.get("context") or {}
        session = ctx.get("session") or obs.get("session", "Unknown")
        regime  = ctx.get("regime")  or obs.get("regime",  "unknown")
        dxy_dir = ctx.get("dxy_dir") or obs.get("dxy_dir", "")
        score   = float(ctx.get("score") or obs.get("value") or 0)
        signal  = ctx.get("signal") or obs.get("signal", "NEUTRAL")
        key = (session, regime, dxy_dir or "N/A")
        groups[key].append({"score": score, "signal": signal})

    stats = {}
    for (session, regime, dxy), items in groups.items():
        scores = [i["score"] for i in items]
        buys   = sum(1 for i in items if "COMPRA" in i["signal"] or "BUY" in i["signal"])
        sells  = sum(1 for i in items if "VENTA" in i["signal"] or "SELL" in i["signal"])
        avg_sc = round(sum(scores) / len(scores), 1) if scores else 0
        max_sc = max(scores) if scores else 0
        stats[f"{session}|{regime}|{dxy}"] = {
            "session": session, "regime": regime, "dxy_dir": dxy,
            "count": len(items), "avg_score": avg_sc, "max_score": max_sc,
            "buy_pct": round(buys / len(items) * 100, 1) if items else 0,
            "sell_pct": round(sells / len(items) * 100, 1) if items else 0,
        }
    return stats


def _get_session_stats(obs_stats: dict) -> dict:
    """Score medio y conteo por sesión."""
    sessions: dict = defaultdict(list)
    for v in obs_stats.values():
        sessions[v["session"]].append(v["avg_score"])
    return {
        s: {"avg": round(sum(sc) / len(sc), 1), "count": len(sc)}
        for s, sc in sessions.items()
    }


def _get_regime_stats(obs_stats: dict) -> dict:
    """Score medio y conteo por régimen."""
    regimes: dict = defaultdict(list)
    for v in obs_stats.values():
        regimes[v["regime"]].append(v["avg_score"])
    return {
        r: {"avg": round(sum(sc) / len(sc), 1), "count": len(sc)}
        for r, sc in regimes.items()
    }


def _best_worst_combos(obs_stats: dict, top_n: int = 5) -> tuple[list, list]:
    """Devuelve los N mejores y peores combos (sesión × régimen × DXY)."""
    sorted_stats = sorted(
        [v for v in obs_stats.values() if v["count"] >= 3],
        key=lambda x: x["avg_score"], reverse=True,
    )
    best  = sorted_stats[:top_n]
    worst = sorted_stats[-top_n:]
    return best, worst


# ─────────────────────────────────────────────────────────────────────────────
# ANÁLISIS MACRO (FRED)
# ─────────────────────────────────────────────────────────────────────────────

def _get_macro_context() -> dict:
    """Obtiene contexto macro reciente de FRED."""
    try:
        import data_feeds as _df
        fred = _df.get_fred_indicators()
        return {k: v for k, v in (fred or {}).items() if v is not None}
    except Exception:
        return {}


# ─────────────────────────────────────────────────────────────────────────────
# CONSTRUCCIÓN DEL PROMPT MAESTRO
# ─────────────────────────────────────────────────────────────────────────────

def _build_master_prompt(
    obs_count: int,
    obs_stats: dict,
    session_stats: dict,
    regime_stats: dict,
    best_combos: list,
    worst_combos: list,
    macro: dict,
    prev_dna: dict,
    prev_versions: int,
) -> str:
    return f"""Eres el motor de meta-aprendizaje de SMC Pro, un bot de trading EUR/USD.

Tu misión: analizar TODOS los datos disponibles y crear la ESTRATEGIA MAESTRA más rentable posible.
La estrategia debe funcionar en el mayor número de condiciones posible, adaptándose a cada contexto.

═══════════════════════════════════════════════
DATOS ACUMULADOS ({obs_count} observaciones de mercado)
═══════════════════════════════════════════════

RENDIMIENTO POR SESIÓN:
{json.dumps(session_stats, ensure_ascii=False, indent=2)}

RENDIMIENTO POR RÉGIMEN DE MERCADO:
{json.dumps(regime_stats, ensure_ascii=False, indent=2)}

MEJORES COMBINACIONES (sesión × régimen × DXY):
{json.dumps(best_combos, ensure_ascii=False, indent=2)}

PEORES COMBINACIONES (evitar o requerir score muy alto):
{json.dumps(worst_combos, ensure_ascii=False, indent=2)}

CONTEXTO MACRO ACTUAL (FRED):
{json.dumps(macro, ensure_ascii=False, indent=2) if macro else "Sin datos macro disponibles"}

DNA PREVIO (v{prev_dna.get("version", 1)}, {prev_versions} versiones evolucionadas):
{json.dumps(prev_dna, ensure_ascii=False, indent=2)}

═══════════════════════════════════════════════
ANÁLISIS REQUERIDO
═══════════════════════════════════════════════

1. ¿Qué sesión y régimen producen las señales de mayor calidad (score más alto)?
2. ¿Cuándo NO operar? (condiciones donde el score es sistemáticamente bajo)
3. ¿Qué peso dar a cada fuente de señal? (técnico, DXY, volumen, sentimiento, fundamental)
4. ¿Cómo ajustar el umbral mínimo de score según el contexto?
5. ¿Qué insight clave se extrae de todos los datos para mejorar la estrategia?

REGLAS DE ORO que NUNCA puedes violar:
- min_score nunca menor de 55 (protección capital)
- No operar en sesión "Off" con score < 75
- Si DXY contradice la señal, aumentar el umbral mínimo en +8 puntos

Responde SOLO con JSON (sin markdown, sin texto extra):
{{
  "master_strategy_version": {prev_dna.get("version", 1) + 1},
  "ai_insight": "<insight principal en 2-3 frases>",
  "signal_weights": {{
    "technical":   <float 0.0-1.0>,
    "dxy":         <float 0.0-1.0>,
    "volume":      <float 0.0-1.0>,
    "sentiment":   <float 0.0-1.0>,
    "fundamental": <float 0.0-1.0>
  }},
  "session_weights": {{
    "London": <float 0.0-1.0>,
    "NY":     <float 0.0-1.0>,
    "Asia":   <float 0.0-1.0>,
    "Off":    <float 0.0-1.0>
  }},
  "regime_thresholds": {{
    "trending_up":   <int 55-88>,
    "trending_down": <int 55-88>,
    "ranging":       <int 55-88>,
    "neutral":       <int 55-88>,
    "unknown":       <int 55-88>
  }},
  "best_conditions": ["<condición 1>", "<condición 2>", "<condición 3>"],
  "avoid_conditions": ["<condición a evitar 1>", "<condición a evitar 2>"],
  "macro_outlook": "<cómo el contexto macro afecta la estrategia>",
  "improvement_vs_prev": "<qué mejora respecto al DNA anterior>"
}}"""


# ─────────────────────────────────────────────────────────────────────────────
# CICLO PRINCIPAL DE APRENDIZAJE
# ─────────────────────────────────────────────────────────────────────────────

def run_learning_cycle() -> dict | None:
    """
    Ejecuta el ciclo de meta-aprendizaje.
    Retorna el nuevo DNA maestro, o None si se saltó.
    """
    if not should_run_learning():
        return None

    mark_learning_done()
    _log.info("StrategyLearner: iniciando ciclo de aprendizaje")

    # ── 1. Recopilar observaciones ──────────────────────────────────────────
    try:
        raw_obs = _db.get_metrics(name="market_observation", limit=500) or []
    except Exception as e:
        _log.warning("StrategyLearner: no se pudieron cargar observaciones: %s", e)
        raw_obs = []

    if len(raw_obs) < 20:
        _log.info("StrategyLearner: solo %d observaciones — necesito al menos 20, saltando", len(raw_obs))
        return None

    # ── 2. Cargar DNA actual ────────────────────────────────────────────────
    try:
        prev_dna = _db.load_active_strategy() or DEFAULT_MASTER_DNA.copy()
    except Exception:
        prev_dna = DEFAULT_MASTER_DNA.copy()

    # ── 3. Analizar observaciones ───────────────────────────────────────────
    obs_stats     = _analyze_observations(raw_obs)
    session_stats = _get_session_stats(obs_stats)
    regime_stats  = _get_regime_stats(obs_stats)
    best, worst   = _best_worst_combos(obs_stats)

    # ── 4. Contexto macro ───────────────────────────────────────────────────
    macro = _get_macro_context()

    # ── 5. Versiones previas ────────────────────────────────────────────────
    try:
        all_dnas = _db.get_strategy_dna_history(limit=10) or []
        prev_versions = len(all_dnas)
    except Exception:
        prev_versions = 1

    # ── 6. Llamar a la IA ───────────────────────────────────────────────────
    prompt = _build_master_prompt(
        obs_count=len(raw_obs),
        obs_stats=obs_stats,
        session_stats=session_stats,
        regime_stats=regime_stats,
        best_combos=best,
        worst_combos=worst,
        macro=macro,
        prev_dna=prev_dna,
        prev_versions=prev_versions,
    )

    try:
        response = _ai.call_ai(
            [{"role": "user", "content": prompt}],
            max_tokens=900, temperature=0.15, prefer_quality=True,
        )
        if response.startswith("⚠️ Todos los proveedores"):
            _log.warning("StrategyLearner: AI no disponible")
            return None

        new_dna_ai = _ai._parse_json(response)
        if not new_dna_ai:
            _log.warning("StrategyLearner: no se pudo parsear respuesta IA")
            return None

    except Exception as e:
        _log.warning("StrategyLearner: error en llamada IA: %s", e)
        return None

    # ── 7. Construir el nuevo DNA maestro ───────────────────────────────────
    new_version = int(prev_dna.get("version") or 1) + 1
    new_dna = {
        "version":          new_version,
        "source":           "meta_learner",
        "evolved_at":       datetime.utcnow().isoformat(),
        "obs_analyzed":     len(raw_obs),
        "ai_insight":       new_dna_ai.get("ai_insight", "")[:300],
        "macro_outlook":    new_dna_ai.get("macro_outlook", "")[:200],
        "improvement":      new_dna_ai.get("improvement_vs_prev", "")[:200],
        "signal_weights":   _safe_weights(new_dna_ai.get("signal_weights") or {}),
        "session_weights":  _safe_session_weights(new_dna_ai.get("session_weights") or {}),
        "regime_thresholds": _safe_thresholds(new_dna_ai.get("regime_thresholds") or {}),
        "best_conditions":  new_dna_ai.get("best_conditions", [])[:5],
        "avoid_conditions": new_dna_ai.get("avoid_conditions", [])[:5],
        "best_combos":      best[:3],
        "worst_combos":     worst[:3],
        # Compatibilidad con el sistema de heal
        "min_score":        new_dna_ai.get("regime_thresholds", {}).get("neutral", 68),
        "dxy_filter_strength": new_dna_ai.get("signal_weights", {}).get("dxy", 1.0),
    }

    # ── 8. Guardar en DB ────────────────────────────────────────────────────
    insight = new_dna.get("ai_insight", "")
    try:
        _db.save_strategy_dna(
            version=new_version,
            rules=new_dna,
            fitness=_compute_fitness(obs_stats),
            trades_evaluated=len(raw_obs),
            winrate=0.0,   # sin trades reales aún
            net_pips=0.0,
            key_insight=insight[:200],
        )
        _log.info("StrategyLearner: DNA maestro v%d guardado — %s", new_version, insight[:80])
    except Exception as e:
        _log.warning("StrategyLearner: error guardando DNA: %s", e)

    # ── 9. Registrar como auto-mejora ───────────────────────────────────────
    try:
        _db.save_self_improvement(
            improvement_type="strategy_evolution",
            before={"version": prev_dna.get("version", 1), "obs_prev": prev_versions},
            after={"version": new_version, "obs_analyzed": len(raw_obs)},
            reason=f"Estrategia maestra v{new_version}: {insight[:200]}",
            applied=True,
        )
    except Exception:
        pass

    return new_dna


# ─────────────────────────────────────────────────────────────────────────────
# APLICAR DNA MAESTRO A UNA SEÑAL
# ─────────────────────────────────────────────────────────────────────────────

def apply_master_dna(signal: dict, score: int, session: str,
                     regime: str, dxy_dir: str,
                     master_dna: dict | None = None) -> dict:
    """
    Aplica los pesos del DNA maestro a una señal y devuelve:
      - adjusted_score: score ajustado según condiciones
      - min_threshold:  umbral mínimo para esta condición
      - should_signal:  True si la señal supera el umbral ajustado
      - context_boost:  puntos añadidos por condiciones favorables
    """
    dna = master_dna or DEFAULT_MASTER_DNA

    # Umbral por régimen
    thresholds = dna.get("regime_thresholds", {})
    min_thr = int(thresholds.get(regime, thresholds.get("neutral", 68)))

    # Peso de sesión
    sess_w = dna.get("session_weights", {})
    session_mult = float(sess_w.get(session, 0.7))

    # Boost por DXY
    dxy_weight  = float((dna.get("signal_weights") or {}).get("dxy", 0.20))
    dxy_aligned = (
        (signal.get("direction") == "LONG"  and dxy_dir == "DOWN") or
        (signal.get("direction") == "SHORT" and dxy_dir == "UP")
    )
    dxy_boost = int(dxy_weight * 15) if dxy_aligned else -int(dxy_weight * 8)

    # Score ajustado
    context_boost  = dxy_boost + (5 if session_mult >= 0.9 else 0)
    adjusted_score = int(score * session_mult + context_boost)
    adjusted_score = max(0, min(100, adjusted_score))

    return {
        "adjusted_score":  adjusted_score,
        "min_threshold":   min_thr,
        "should_signal":   adjusted_score >= min_thr,
        "context_boost":   context_boost,
        "session_weight":  session_mult,
        "dxy_aligned":     dxy_aligned,
    }


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _safe_weights(w: dict) -> dict:
    """Normaliza pesos de señal (suma ≈ 1.0, cada uno entre 0 y 1)."""
    keys = ["technical", "dxy", "volume", "sentiment", "fundamental"]
    defaults = DEFAULT_MASTER_DNA["signal_weights"]
    result = {}
    for k in keys:
        val = float(w.get(k, defaults[k]))
        result[k] = round(max(0.0, min(1.0, val)), 3)
    total = sum(result.values()) or 1.0
    return {k: round(v / total, 3) for k, v in result.items()}


def _safe_session_weights(w: dict) -> dict:
    """Pesos de sesión entre 0 y 1."""
    defaults = DEFAULT_MASTER_DNA["session_weights"]
    return {
        s: round(max(0.1, min(1.0, float(w.get(s, defaults.get(s, 0.5))))), 2)
        for s in ["London", "NY", "Asia", "Off"]
    }


def _safe_thresholds(t: dict) -> dict:
    """Umbrales entre 55 y 88."""
    defaults = DEFAULT_MASTER_DNA["regime_thresholds"]
    return {
        r: max(55, min(88, int(t.get(r, defaults.get(r, 68)))))
        for r in ["trending_up", "trending_down", "ranging", "neutral", "unknown"]
    }


def _compute_fitness(obs_stats: dict) -> float:
    """Fitness proxy: score medio ponderado por cantidad de observaciones."""
    total_w, total_sc = 0.0, 0.0
    for v in obs_stats.values():
        w = float(v["count"])
        total_w  += w
        total_sc += v["avg_score"] * w
    return round(total_sc / total_w, 2) if total_w > 0 else 0.0


def get_master_dna() -> dict:
    """Carga el DNA maestro activo desde DB, o devuelve el default."""
    try:
        dna = _db.load_active_strategy()
        if dna and dna.get("source") in ("meta_learner", "default"):
            return dna
        # Si el DNA activo no es del learner, devuelve el default
        return DEFAULT_MASTER_DNA.copy()
    except Exception:
        return DEFAULT_MASTER_DNA.copy()

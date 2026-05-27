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
        "technical":   0.30,   # indicadores técnicos (EMA, RSI, MACD…)
        "fundamental": 0.30,   # noticias + FRED macro (explica el movimiento real)
        "dxy":         0.20,   # correlación inversa con el dólar
        "volume":      0.12,   # volumen y delta
        "sentiment":   0.08,   # sentimiento de noticias (complementa fundamental)
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
# HELPERS DE ANÁLISIS — construyen contexto rico para el prompt
# ─────────────────────────────────────────────────────────────────────────────

def _fmt_strategy_table(all_res: list[dict], lt_dict: dict,
                         cert: set, winners60: set, lt_winners: set) -> str:
    """Tabla compacta de las 17 estrategias con sus stats 60d y 2008."""
    if not all_res:
        return "Sin datos de backtest disponibles aún."
    lines = ["Estrategia            | WR60d | PF60d | Ops60d | WR2008 | PF2008 | Ops2008 | Estado"]
    lines.append("-" * 100)
    for r in all_res:
        name  = r["_name"]
        lt    = lt_dict.get(name, {})
        badge = "🏆CERT" if name in cert else ("✅60d" if name in winners60 else ("📅2008" if name in lt_winners else "❌"))
        lines.append(
            f"{name:<22}| {r.get('winrate',0):>5.1f}%| {r.get('profit_factor',0):>5.2f} | "
            f"{r.get('total',0):>6} | {lt.get('winrate',0):>6.1f}%| {lt.get('profit_factor',0):>6.2f} | "
            f"{lt.get('total',0):>7} | {badge}"
        )
    return "\n".join(lines)


def _fmt_regime_matrix(cert: set, winners60: set) -> str:
    """Para cada régimen: qué estrategias certificadas aplican y el nivel de confianza."""
    from strategy_selector import REGIME_STRATEGY_MAP
    lines = []
    for regime, candidates in REGIME_STRATEGY_MAP.items():
        cert_here = [c for c in candidates if c in cert]
        w60_here  = [c for c in candidates if c in winners60 and c not in cert]
        pct = round(len(cert_here) / len(candidates) * 100) if candidates else 0
        confidence = "MUY ALTA" if pct >= 75 else "ALTA" if pct >= 50 else "MEDIA" if pct >= 25 else "BAJA"
        threshold  = 60 if pct >= 75 else 65 if pct >= 50 else 70 if pct >= 25 else 77
        lines.append(
            f"  {regime:<15} | confianza {confidence} ({pct}%) | threshold recomendado: {threshold} | "
            f"certificadas: {cert_here or 'ninguna'} | solo-60d: {w60_here or 'ninguna'}"
        )
    return "\n".join(lines)


def _summarize_fund_history(fund_rows: list[dict]) -> dict:
    """Extrae estadísticas de tendencia del historial de noticias."""
    if not fund_rows:
        return {"status": "sin_datos"}
    scores  = [float(r.get("value", 0)) for r in fund_rows]
    dirs    = [((r.get("context") or {}).get("direction", "NEUTRAL")) for r in fund_rows]
    hi_imps = [int((r.get("context") or {}).get("hi_impact", 0)) for r in fund_rows]
    n = len(scores)
    n_bull = sum(1 for d in dirs if d in ("LONG", "UP", "COMPRA"))
    n_bear = sum(1 for d in dirs if d in ("SHORT", "DOWN", "VENTA"))
    avg_sc = round(sum(scores) / n, 3) if n else 0
    trend  = "ALCISTA" if n_bull > n_bear * 1.3 else "BAJISTA" if n_bear > n_bull * 1.3 else "NEUTRAL"
    hi_pct = round(sum(1 for h in hi_imps if h >= 3) / n * 100) if n else 0
    # Últimas 10 vs primeras 10 (tendencia reciente)
    recent_bull = sum(1 for d in dirs[-10:] if d in ("LONG", "UP", "COMPRA"))
    older_bull  = sum(1 for d in dirs[:10]  if d in ("LONG", "UP", "COMPRA"))
    momentum = "acelerando alcista" if recent_bull > older_bull else \
               "acelerando bajista" if recent_bull < older_bull else "estable"
    return {
        "lecturas": n,
        "tendencia_general": trend,
        "pct_bullish": round(n_bull / n * 100) if n else 0,
        "pct_bearish": round(n_bear / n * 100) if n else 0,
        "score_medio": avg_sc,
        "pct_alto_impacto": hi_pct,
        "momentum_reciente": momentum,
        "top_headlines": [
            (r.get("context") or {}).get("top_headlines", [])[:2]
            for r in fund_rows[-5:]
        ],
    }


# ─────────────────────────────────────────────────────────────────────────────
# CONSTRUCCIÓN DEL PROMPT MAESTRO (versión completa con todos los datos)
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
    strategy_ranking: list | None = None,
    strategy_winners: list | None = None,
    certified: list | None = None,
    fund_history: list | None = None,
    # Nuevos argumentos con toda la evidencia
    strategy_table: str = "",
    regime_matrix: str = "",
    fund_summary: dict | None = None,
    lt_results_count: int = 0,
    baseline_dna: dict | None = None,
) -> str:
    next_v = int(prev_dna.get("_version") or prev_dna.get("version") or 1) + 1
    cert_list = json.dumps(certified or [], ensure_ascii=False)

    _lt_note = (
        f"{lt_results_count} estrategias probadas en datos DIARIOS EUR/USD DESDE 2008 (~4400+ barras, 15+ años)"
        if lt_results_count > 0
        else "Backtest 2008 aún en progreso — disponible pronto"
    )

    _baseline_note = (
        f"DNA DERIVADO AUTOMÁTICAMENTE DE LOS DATOS (úsalo como punto de partida y mejóralo con tu análisis):\n"
        f"{json.dumps(baseline_dna, ensure_ascii=False, indent=2)}"
        if baseline_dna
        else "Sin baseline previo — crea la estrategia desde cero."
    )

    _fund_note = (
        f"ANÁLISIS ESTADÍSTICO DE {fund_summary.get('lecturas', 0)} LECTURAS DE NOTICIAS:\n"
        f"  Tendencia general: {fund_summary.get('tendencia_general', 'N/A')}\n"
        f"  Bullish/Bearish: {fund_summary.get('pct_bullish', 0)}% / {fund_summary.get('pct_bearish', 0)}%\n"
        f"  Score medio fundamental: {fund_summary.get('score_medio', 0)}\n"
        f"  Alto impacto (NFP/CPI/Fed): {fund_summary.get('pct_alto_impacto', 0)}% de las lecturas\n"
        f"  Momentum reciente: {fund_summary.get('momentum_reciente', 'N/A')}"
        if fund_summary and fund_summary.get("lecturas", 0) > 0
        else "Historial de noticias acumulando — disponible en próximas horas."
    )

    return f"""Eres el motor de estrategia maestra de SMC Pro, un bot algorítmico de trading EUR/USD.

CONTEXTO: Tienes acceso a {lt_results_count} estrategias probadas sobre 15+ años de datos
diarios (desde 2008 — crisis financiera, recuperación, Brexit, COVID, subidas de tipos Fed).
Esto te da evidencia ESTADÍSTICAMENTE SÓLIDA para ser DECISIVO y PRECISO.

Tu misión: sintetizar TODO lo que sabes en el DNA óptimo que maximice la calidad de señales
en el mayor número de condiciones posibles. No seas conservador por falta de datos — TIENES los datos.

══════════════════════════════════════════════════════════════════
SECCIÓN 1 — EVIDENCIA HISTÓRICA 15 AÑOS ({_lt_note})
══════════════════════════════════════════════════════════════════

TABLA COMPLETA DE RENDIMIENTO (60d 1H + 2008+ Daily):
{strategy_table if strategy_table else json.dumps(strategy_ranking or [], indent=2)}

ESTRATEGIAS CERTIFICADAS (pasan AMBOS filtros — son tu arsenal principal):
{cert_list}

MATRIZ DE RÉGIMEN × ESTRATEGIAS CERTIFICADAS:
(Cuántas estrategias certificadas aplican a cada régimen → determina la confianza y el threshold)
{regime_matrix}

══════════════════════════════════════════════════════════════════
SECCIÓN 2 — OBSERVACIONES DE MERCADO EN VIVO ({obs_count} señales registradas)
══════════════════════════════════════════════════════════════════

RENDIMIENTO POR SESIÓN (score promedio y distribución de señales):
{json.dumps(session_stats, ensure_ascii=False, indent=2)}

RENDIMIENTO POR RÉGIMEN:
{json.dumps(regime_stats, ensure_ascii=False, indent=2)}

MEJORES COMBINACIONES (sesión × régimen × DXY — opera aquí):
{json.dumps(best_combos[:5], ensure_ascii=False, indent=2)}

PEORES COMBINACIONES (evitar o exigir score muy alto):
{json.dumps(worst_combos[:5], ensure_ascii=False, indent=2)}

══════════════════════════════════════════════════════════════════
SECCIÓN 3 — ANÁLISIS FUNDAMENTAL (RSS 20 fuentes + FRED)
══════════════════════════════════════════════════════════════════

{_fund_note}

CONTEXTO MACRO ACTUAL (Fed rate, CPI, desempleo, yield curve, GDP):
{json.dumps(macro, ensure_ascii=False, indent=2) if macro else "FRED no disponible en este momento"}

══════════════════════════════════════════════════════════════════
SECCIÓN 4 — DNA ACTUAL Y BASELINE DE DATOS
══════════════════════════════════════════════════════════════════

DNA ACTIVO (v{prev_dna.get("_version") or prev_dna.get("version", 1)}, {prev_versions} versiones):
signal_weights actuales: {json.dumps((prev_dna.get("signal_weights") or {}), ensure_ascii=False)}
session_weights actuales: {json.dumps((prev_dna.get("session_weights") or {}), ensure_ascii=False)}
regime_thresholds actuales: {json.dumps((prev_dna.get("regime_thresholds") or {}), ensure_ascii=False)}

{_baseline_note}

══════════════════════════════════════════════════════════════════
TU ANÁLISIS — SÉ DECISIVO, TIENES 15 AÑOS DE EVIDENCIA
══════════════════════════════════════════════════════════════════

Analiza TODA la evidencia anterior y determina:

1. SIGNAL WEIGHTS: ¿Qué importancia relativa tiene cada fuente?
   - ¿Las noticias macro (NFP, CPI, Fed) mueven el EUR/USD? → peso fundamental
   - ¿El DXY predice la dirección del EUR? → peso dxy
   - ¿Los indicadores técnicos dan buen timing de entrada? → peso technical
   - ¿El volumen confirma las señales? → peso volume
   - ¿El sentimiento de titulares anticipa el movimiento? → peso sentiment

2. SESSION WEIGHTS: ¿En qué sesiones las señales son más fiables?
   - London/NY overlap: típicamente el mejor momento para EUR/USD
   - Asia: mercado más tranquilo, señales de menor calidad
   - Off: evitar salvo señal muy fuerte

3. REGIME THRESHOLDS: ¿Cuándo ser más/menos exigente?
   - Si tienes muchas estrategias certificadas para ese régimen → threshold bajo
   - Si las estrategias fallan en ese régimen → threshold alto

4. MACRO+FUNDAMENTAL: ¿Qué dice el contexto actual?
   - Política Fed vs ECB → dirección estructural EUR/USD
   - Inflación y empleo → expectativas de tipos
   - Tendencia reciente de noticias → momentum fundamental

REGLAS INVIOLABLES (el sistema las aplica automáticamente, no hace falta que seas conservador):
✓ fundamental MÍNIMO 0.20 — las noticias CAUSAN el movimiento
✓ sentiment   MÍNIMO 0.05 — el tono de titulares anticipa el precio
✓ dxy         MÍNIMO 0.10 — correlación inversa EUR/USD es matemática
✓ technical   MÍNIMO 0.15 — necesario para el timing preciso de entrada
✓ volume      MÍNIMO 0.03 — confirma o desmiente señales
✓ regime thresholds entre 55 y 88
✓ session Off siempre ≤ 0.30

Responde SOLO con JSON válido (sin markdown, sin texto antes ni después):
{{
  "master_strategy_version": {next_v},
  "ai_insight": "<2-3 frases con el hallazgo más importante de los 15 años de datos>",
  "macro_outlook": "<situación macro actual Fed/ECB y su impacto en EUR/USD>",
  "signal_weights": {{
    "technical":   <0.15-0.45>,
    "dxy":         <0.10-0.35>,
    "volume":      <0.03-0.20>,
    "sentiment":   <0.05-0.20>,
    "fundamental": <0.20-0.45>
  }},
  "session_weights": {{
    "London": <0.70-1.00>,
    "NY":     <0.65-1.00>,
    "Asia":   <0.30-0.80>,
    "Off":    <0.10-0.30>
  }},
  "regime_thresholds": {{
    "trending_up":   <55-85>,
    "trending_down": <55-85>,
    "ranging":       <60-88>,
    "neutral":       <60-85>,
    "unknown":       <65-88>
  }},
  "best_conditions": [
    "<sesión+régimen+condición que históricamente da las mejores señales>",
    "<segunda mejor condición>",
    "<tercera mejor condición>"
  ],
  "avoid_conditions": [
    "<condición donde las señales fallan consistentemente>",
    "<segunda condición a evitar>"
  ],
  "improvement_vs_prev": "<qué cambia respecto al DNA anterior y por qué lo justifican los datos>"
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

    # ── 3b. Datos completos de estrategias (60d + 2008) ────────────────────
    strategy_ranking: list[dict] = []
    strategy_winners: list[str]  = []
    certified: list[str]         = []
    strategy_table:  str         = ""
    regime_matrix:   str         = ""
    lt_results_count: int        = 0
    baseline_dna: dict | None    = None
    try:
        import strategy_selector as _ss
        _ss.ensure_ready()
        all_res, winners   = _ss.get_cached_results()
        lt_res, lt_winners = _ss.get_lt_cached_results()
        cert_set           = _ss.certified_winners()

        strategy_winners  = list(winners)
        certified         = list(cert_set)
        lt_results_count  = len(lt_res)
        lt_dict           = {r["_name"]: r for r in lt_res}

        # Tabla completa de todas las estrategias (para el prompt de IA)
        strategy_table = _fmt_strategy_table(all_res, lt_dict, cert_set, winners, lt_winners)

        # Matriz régimen × estrategias certificadas
        regime_matrix = _fmt_regime_matrix(cert_set, winners)

        # Ranking compacto para compatibilidad
        strategy_ranking = [
            {
                "name":       r["_name"],
                "label":      r.get("_label", r["_name"]),
                "winrate":    round(r.get("winrate", 0), 1),
                "pf":         round(r.get("profit_factor", 0), 2),
                "trades_60d": r.get("total", 0),
                "lt_winrate": round(lt_dict.get(r["_name"], {}).get("winrate", 0), 1),
                "lt_pf":      round(lt_dict.get(r["_name"], {}).get("profit_factor", 0), 2),
                "lt_trades":  lt_dict.get(r["_name"], {}).get("total", 0),
                "winner_60d": r["_name"] in winners,
                "winner_lt":  r["_name"] in lt_winners,
                "certified":  r["_name"] in cert_set,
            }
            for r in all_res
        ]

        # DNA derivado de datos como baseline para la IA
        try:
            baseline_dna = _ss.auto_derive_master_dna(source="learner_baseline")
        except Exception:
            baseline_dna = None

    except Exception as _se:
        _log.debug("StrategyLearner: error cargando selector: %s", _se)

    # ── 3c. Historial de señales fundamentales (último 100 + resumen) ──────
    fund_history: list[dict] = []
    fund_summary: dict       = {}
    try:
        fund_rows = _db.get_metrics(name="fundamental_signal", limit=200) or []
        fund_summary = _summarize_fund_history(fund_rows)
        # Para el prompt: últimas 30 lecturas con detalle
        fund_history = [
            {
                "score":     round(float(r.get("value", 0)), 3),
                "direction": (r.get("context") or {}).get("direction", ""),
                "hi_impact": int((r.get("context") or {}).get("hi_impact", 0)),
                "ts":        str(r.get("created_at", ""))[:16],
                "headlines": ((r.get("context") or {}).get("top_headlines") or [])[:2],
            }
            for r in fund_rows[-30:]
        ]
    except Exception:
        pass

    # ── 4. Contexto macro FRED ──────────────────────────────────────────────
    macro = _get_macro_context()

    # ── 5. Versiones previas ────────────────────────────────────────────────
    try:
        all_dnas      = _db.get_strategy_dna_history(limit=10) or []
        prev_versions = len(all_dnas)
    except Exception:
        prev_versions = 1

    # ── 6. Llamar a la IA con TODO el contexto ──────────────────────────────
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
        strategy_ranking=strategy_ranking,
        strategy_winners=strategy_winners,
        certified=certified,
        fund_history=fund_history,
        strategy_table=strategy_table,
        regime_matrix=regime_matrix,
        fund_summary=fund_summary,
        lt_results_count=lt_results_count,
        baseline_dna=baseline_dna,
    )

    _log.info("StrategyLearner: prompt construido (%d chars) — llamando IA", len(prompt))

    try:
        response = _ai.call_ai(
            [{"role": "user", "content": prompt}],
            max_tokens=1400, temperature=0.12, prefer_quality=True,
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
    new_version = int(
        prev_dna.get("_version") or prev_dna.get("version") or 1
    ) + 1

    # Aplicar mínimos duros SIEMPRE — la IA puede sugerir, el sistema garantiza
    safe_sw  = _safe_weights(new_dna_ai.get("signal_weights") or {})
    safe_ses = _safe_session_weights(new_dna_ai.get("session_weights") or {})
    safe_thr = _safe_thresholds(new_dna_ai.get("regime_thresholds") or {})

    _log.info(
        "StrategyLearner v%d — signal_weights FINALES: %s",
        new_version,
        {k: f"{v:.0%}" for k, v in safe_sw.items()}
    )

    new_dna = {
        "version":              new_version,
        "source":               "meta_learner_ai",
        "evolved_at":           datetime.utcnow().isoformat(),
        "obs_analyzed":         len(raw_obs),
        "lt_strategies_tested": lt_results_count,
        "certified_count":      len(certified),
        "ai_insight":           new_dna_ai.get("ai_insight", "")[:400],
        "macro_outlook":        new_dna_ai.get("macro_outlook", "")[:250],
        "improvement":          new_dna_ai.get("improvement_vs_prev", "")[:250],
        "signal_weights":       safe_sw,
        "session_weights":      safe_ses,
        "regime_thresholds":    safe_thr,
        "best_conditions":      new_dna_ai.get("best_conditions", [])[:5],
        "avoid_conditions":     new_dna_ai.get("avoid_conditions", [])[:5],
        "best_combos":          best[:5],
        "worst_combos":         worst[:5],
        "certified_strategies": certified,
        "fund_trend":           fund_summary.get("tendencia_general", "N/A") if fund_summary else "N/A",
        "fund_pct_bullish":     fund_summary.get("pct_bullish", 0) if fund_summary else 0,
        # Compatibilidad con el sistema de heal/score
        "min_score":            safe_thr.get("neutral", 68),
        "dxy_filter_strength":  safe_sw.get("dxy", 0.20),
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
    """
    Normaliza pesos de señal (suma = 1.0).
    Impone mínimos DUROS basados en evidencia económica:
      - fundamental ≥ 20%  (NFP, CPI, Fed → CAUSAN el movimiento)
      - sentiment   ≥  5%  (tono de titulares complementa el fundamental)
      - dxy         ≥ 10%  (correlación inversa EUR/USD documentada)
      - technical   ≥ 15%  (señal de timing, no puede ser 0)
      - volume      ≥  3%  (confirma breakouts)
    La IA puede sugerir pesos distintos pero NUNCA por debajo de estos mínimos.
    """
    # Mínimos económicamente justificados — imposibles de violar
    HARD_MINS = {
        "fundamental": 0.20,
        "sentiment":   0.05,
        "dxy":         0.10,
        "technical":   0.15,
        "volume":      0.03,
    }
    keys = ["technical", "dxy", "volume", "sentiment", "fundamental"]
    defaults = DEFAULT_MASTER_DNA["signal_weights"]
    result = {}
    for k in keys:
        raw = float(w.get(k, defaults[k]))
        # Primero clamp 0..1, luego aplicar mínimo duro
        clamped = max(0.0, min(1.0, raw))
        result[k] = max(HARD_MINS.get(k, 0.0), clamped)
    # Normalizar a suma=1
    total = sum(result.values()) or 1.0
    normalized = {k: round(v / total, 3) for k, v in result.items()}
    # Verificación final — nunca puede quedar debajo del mínimo tras normalización
    # (si la suma previa es enorme, la normalización podría bajar alguno)
    for k, mn in HARD_MINS.items():
        if normalized.get(k, 0) < mn * 0.90:   # margen 10% por redondeo
            normalized[k] = round(mn, 3)
    return normalized


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

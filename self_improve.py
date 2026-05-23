"""
self_improve.py — Autonomous self-improvement engine for SMC Pro

Runs automatically every N analysis cycles. Components:

  ErrorMonitor   — catches, classifies and stores all app exceptions
  PerfMonitor    — tracks per-indicator and per-session accuracy over time
  HealEngine     — AI reviews error + perf logs → proposes + applies parameter fixes
  ToolScout      — tests free API endpoints, registers new data sources
  MarketLearner  — stores rich market observations for long-term pattern mining
"""

import os
import json
import logging
import traceback
from datetime import datetime, timezone, timedelta

import db as _db
import ai_engine as _ai

_log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# ERROR MONITOR
# ─────────────────────────────────────────────────────────────────────────────

def log_error(component: str, error: Exception, context: dict | None = None,
              severity: str = "warning") -> None:
    """Store an exception to the error_log table for AI review."""
    try:
        msg = str(error)[:500]
        tb  = traceback.format_exc()[:1000]
        _db.log_app_error(component=component, severity=severity,
                          message=msg, traceback=tb,
                          context=context or {})
    except Exception:
        pass


def get_recent_errors(hours: int = 6) -> list[dict]:
    """Return error log entries from the last N hours."""
    try:
        return _db.get_error_log(hours=hours, limit=50)
    except Exception:
        return []


# ─────────────────────────────────────────────────────────────────────────────
# PERFORMANCE MONITOR
# ─────────────────────────────────────────────────────────────────────────────

def record_metric(name: str, value: float, context: dict | None = None) -> None:
    """Store a scalar metric to performance_metrics table."""
    try:
        _db.save_metric(name=name, value=value, context=context or {})
    except Exception:
        pass


def compute_indicator_accuracy(trades: list[dict]) -> dict:
    """
    From closed trades with market_snapshot, compute per-indicator accuracy.
    Returns dict: {indicator_name: {"correct": int, "total": int, "accuracy": float}}
    """
    stats: dict = {}
    for t in trades:
        snap  = t.get("market_snapshot") or {}
        pips  = t.get("pips", 0) or 0
        win   = pips > 0
        d     = t.get("direction", "")

        # DXY alignment
        dxy = snap.get("dxy_dir", "")
        if dxy and d:
            correct = (d == "LONG" and dxy == "DOWN") or (d == "SHORT" and dxy == "UP")
            s = stats.setdefault("dxy_alignment", {"correct": 0, "total": 0})
            s["total"] += 1
            if correct == win: s["correct"] += 1

        # Volume spike
        vs = snap.get("vol_spike", False)
        if vs is not None:
            s = stats.setdefault("vol_spike", {"correct": 0, "total": 0})
            s["total"] += 1
            if win: s["correct"] += 1  # spike present + win = correct

        # Session
        sess = snap.get("session", "")
        if sess:
            key = f"session_{sess}"
            s = stats.setdefault(key, {"correct": 0, "total": 0})
            s["total"] += 1
            if win: s["correct"] += 1

        # Score bucket
        score = snap.get("score", 0) or 0
        bucket = f"score_{(score // 10) * 10}"
        s = stats.setdefault(bucket, {"correct": 0, "total": 0})
        s["total"] += 1
        if win: s["correct"] += 1

    for k, v in stats.items():
        v["accuracy"] = round(v["correct"] / v["total"] * 100, 1) if v["total"] > 0 else 0.0
    return stats


# ─────────────────────────────────────────────────────────────────────────────
# HEAL ENGINE — AI-driven self-correction
# ─────────────────────────────────────────────────────────────────────────────

_HEAL_INTERVAL_MINUTES = 60   # run at most once per hour
_LAST_HEAL_KEY         = "last_heal_ts"


def should_run_heal() -> bool:
    """Check if enough time has passed since last heal cycle."""
    try:
        last = _db.get_setting(_LAST_HEAL_KEY)
        if not last:
            return True
        last_dt = datetime.fromisoformat(last)
        return (datetime.now(timezone.utc) - last_dt).total_seconds() > _HEAL_INTERVAL_MINUTES * 60
    except Exception:
        return True


def run_heal_cycle(active_dna: dict, current_user: str = "system") -> dict | None:
    """
    Core self-improvement cycle. Returns a dict with the assessment and
    any parameter changes applied. Returns None if skipped.
    """
    if not should_run_heal():
        return None

    _db.set_setting(_LAST_HEAL_KEY, datetime.now(timezone.utc).isoformat())

    # ── Gather evidence ───────────────────────────────────────────────────────
    errors        = get_recent_errors(hours=6)
    recent_trades = _db.get_trades_for_evolution(limit=40) or []
    ind_accuracy  = compute_indicator_accuracy(recent_trades) if recent_trades else {}
    dna_version   = (active_dna or {}).get("version", 1)

    wins  = [t for t in recent_trades if (t.get("pips") or 0) > 0]
    wr    = round(len(wins) / len(recent_trades) * 100, 1) if recent_trades else 0
    net_p = round(sum(t.get("pips") or 0 for t in recent_trades), 1) if recent_trades else 0

    error_summary = []
    for e in errors[-10:]:
        error_summary.append({"component": e.get("component", "?"),
                               "message":   str(e.get("message", ""))[:120],
                               "severity":  e.get("severity", "warning"),
                               "count":     1})

    prompt = f"""Eres el motor de auto-mejora de SMC Pro, un bot de trading EUR/USD.

ESTADO ACTUAL (DNA v{dna_version}):
- Trades recientes: {len(recent_trades)} | Win Rate: {wr}% | Pips netos: {net_p:+.1f}
- Precisión por indicador: {json.dumps(ind_accuracy, ensure_ascii=False)}
- Errores recientes (últimas 6h): {json.dumps(error_summary, ensure_ascii=False)}

DNA ACTIVO:
{json.dumps(active_dna or {}, indent=2, ensure_ascii=False)}

ANÁLISIS REQUERIDO:
1. ERRORES: ¿hay patrones en los errores? ¿alguno crítico que afecte al trading?
2. INDICADORES: ¿qué indicadores tienen baja precisión y deberían ponderarse menos?
3. SESIONES: ¿hay sesiones con win rate < 40%? ¿habría que blacklistearlas?
4. PARÁMETROS: ¿qué parámetros del DNA mejorarían el rendimiento?
5. ACCIONES: lista exacta de cambios a aplicar con justificación

Responde SOLO con JSON (sin markdown):
{{
  "health_status": "ok|warning|critical",
  "summary": "<2 frases: estado general del sistema>",
  "error_patterns": "<1 frase sobre errores>",
  "top_finding": "<hallazgo más importante>",
  "parameter_changes": {{
    "min_score": <int o null>,
    "blacklist_hours_utc": [<ints>] o null,
    "dxy_filter_strength": <float o null>,
    "volume_spike_bonus": <int o null>
  }},
  "actions_taken": ["<acción 1>", "<acción 2>"]
}}"""

    try:
        response = _ai.call_ai(
            [{"role": "user", "content": prompt}],
            max_tokens=600, temperature=0.2, prefer_reasoning=True,
        )
        assessment = _ai._parse_json(response)
        if not assessment:
            assessment = {"health_status": "unknown", "summary": response[:200],
                          "error_patterns": "", "top_finding": "", "parameter_changes": {},
                          "actions_taken": []}
    except Exception as e:
        _log.warning("HealEngine AI call failed: %s", e)
        assessment = {"health_status": "unknown", "summary": str(e)[:200],
                      "error_patterns": "", "top_finding": "", "parameter_changes": {},
                      "actions_taken": []}

    # ── Apply safe parameter changes ──────────────────────────────────────────
    changes     = assessment.get("parameter_changes") or {}
    applied     = {}
    new_dna     = dict(active_dna or {})
    dna_changed = False

    if changes.get("min_score") is not None:
        val = max(55, min(88, int(changes["min_score"])))
        if val != new_dna.get("min_score"):
            new_dna["min_score"] = val
            applied["min_score"] = val
            dna_changed = True

    if changes.get("dxy_filter_strength") is not None:
        val = max(0.0, min(2.0, float(changes["dxy_filter_strength"])))
        if abs(val - float(new_dna.get("dxy_filter_strength") or 1.0)) > 0.05:
            new_dna["dxy_filter_strength"] = val
            applied["dxy_filter_strength"] = val
            dna_changed = True

    if changes.get("volume_spike_bonus") is not None:
        val = max(0, min(20, int(changes["volume_spike_bonus"])))
        if val != new_dna.get("volume_spike_bonus"):
            new_dna["volume_spike_bonus"] = val
            applied["volume_spike_bonus"] = val
            dna_changed = True

    if changes.get("blacklist_hours_utc") is not None:
        blist = [h for h in changes["blacklist_hours_utc"] if isinstance(h, int) and 0 <= h <= 23]
        if blist != (new_dna.get("blacklist_hours_utc") or []):
            new_dna["blacklist_hours_utc"] = blist
            applied["blacklist_hours_utc"] = blist
            dna_changed = True

    # Persist if anything changed
    if dna_changed:
        new_dna["version"] = int(new_dna.get("version") or 1) + 1
        new_dna["evolved_at"] = datetime.utcnow().isoformat()
        new_dna["explanation"] = f"Auto-heal v{new_dna['version']}: {assessment.get('top_finding','')[:100]}"
        try:
            _db.save_strategy_dna(
                version=new_dna["version"],
                rules=new_dna,
                fitness=float(new_dna.get("fitness") or wr),
                trades_evaluated=len(recent_trades),
                winrate=float(wr),
                net_pips=float(net_p),
                key_insight=assessment.get("top_finding", "")[:200],
            )
        except Exception as ex:
            _log.warning("HealEngine DNA save failed: %s", ex)

    # Store the heal record
    try:
        _db.save_self_improvement(
            improvement_type="heal_cycle",
            before={"dna_version": dna_version, "winrate": wr, "net_pips": net_p},
            after=applied,
            reason=assessment.get("summary", "")[:300],
            applied=bool(applied),
        )
    except Exception:
        pass

    assessment["applied_changes"] = applied
    assessment["dna_updated"]     = dna_changed
    assessment["new_dna"]         = new_dna if dna_changed else None
    assessment["ts"]              = datetime.utcnow().isoformat()
    return assessment


# ─────────────────────────────────────────────────────────────────────────────
# TOOL SCOUT — discover and test free API endpoints
# ─────────────────────────────────────────────────────────────────────────────

_FREE_APIS = [
    {"name": "Groq",       "env": "GROQ_API_KEY",       "url": "https://console.groq.com",       "type": "ai"},
    {"name": "Cerebras",   "env": "CEREBRAS_API_KEY",   "url": "https://www.cerebras.ai",         "type": "ai"},
    {"name": "Gemini",     "env": "GEMINI_API_KEY",     "url": "https://ai.google.dev",           "type": "ai"},
    {"name": "Mistral",    "env": "MISTRAL_API_KEY",    "url": "https://console.mistral.ai",      "type": "ai"},
    {"name": "Zhipu GLM",  "env": "ZHIPU_API_KEY",      "url": "https://open.bigmodel.cn",        "type": "ai"},
    {"name": "OpenRouter", "env": "OPENROUTER_API_KEY", "url": "https://openrouter.ai",           "type": "ai"},
    {"name": "Finnhub",    "env": "FINNHUB_API_KEY",    "url": "https://finnhub.io",              "type": "data"},
    {"name": "FRED",       "env": "FRED_API_KEY",       "url": "https://fredaccount.stlouisfed.org/apikeys", "type": "data"},
    {"name": "Alpha Vantage", "env": "ALPHAVANTAGE_API_KEY", "url": "https://www.alphavantage.co", "type": "data"},
    {"name": "TwelveData", "env": "TWELVEDATA_API_KEY", "url": "https://twelvedata.com",          "type": "data"},
]


def get_missing_apis() -> list[dict]:
    """Return list of free APIs that have no key configured yet."""
    missing = []
    for api in _FREE_APIS:
        if not os.environ.get(api["env"], "").strip():
            missing.append(api)
    return missing


def get_configured_apis() -> list[dict]:
    """Return list of APIs that have keys configured."""
    active = []
    for api in _FREE_APIS:
        if os.environ.get(api["env"], "").strip():
            active.append(api)
    return active


# ─────────────────────────────────────────────────────────────────────────────
# MARKET LEARNER — rich observation storage
# ─────────────────────────────────────────────────────────────────────────────

def store_market_observation(signal: dict, score: int, session: str,
                              dxy_dir: str, economic_data: dict | None = None,
                              sentiment: float = 0.0) -> None:
    """
    Store a rich market observation. Called on each analysis refresh.
    Builds the long-term dataset used for AI pattern mining.
    """
    obs = {
        "score":       score,
        "session":     session,
        "dxy_dir":     dxy_dir,
        "regime":      signal.get("kb_regime_label") or signal.get("regime", ""),
        "signal":      signal.get("final_signal", "NEUTRAL"),
        "direction":   signal.get("direction", ""),
        "price":       signal.get("price"),
        "buy_sigs":    signal.get("buy_signals", 0),
        "sell_sigs":   signal.get("sell_signals", 0),
        "sentiment":   round(sentiment, 4),
        "econ":        economic_data or {},
        "ts":          datetime.utcnow().isoformat(),
    }
    try:
        _db.save_metric(
            name="market_observation",
            value=float(score),
            context=obs,
        )
    except Exception:
        pass


def get_pattern_report(limit: int = 200) -> str:
    """
    Ask AI to find patterns in recent market observations.
    Returns a short human-readable report.
    """
    try:
        observations = _db.get_metrics(name="market_observation", limit=limit)
    except Exception:
        return "Sin datos de observaciones disponibles."

    if len(observations) < 10:
        return f"Solo {len(observations)} observaciones — necesito más datos para detectar patrones."

    # Sample to avoid token overflow
    sample = observations[-50:]
    prompt = f"""Analiza {len(sample)} observaciones de mercado EUR/USD y detecta patrones.

OBSERVACIONES (últimas {len(sample)}):
{json.dumps(sample, ensure_ascii=False, default=str)[:3000]}

Responde en español con:
1. PATRÓN PRINCIPAL detectado (sesión, régimen, DXY más exitosos)
2. HORAS/SESIONES a evitar (las que tienen señales de baja calidad)
3. CORRELACIÓN DXY (¿cuándo confirma vs contradice más?)
4. RECOMENDACIÓN: 1 ajuste concreto al DNA para mejorar

Máximo 150 palabras."""

    try:
        return _ai.call_ai(
            [{"role": "user", "content": prompt}],
            max_tokens=300, temperature=0.3,
        )
    except Exception as e:
        return f"Error en análisis de patrones: {e}"

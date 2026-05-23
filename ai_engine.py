"""
ai_engine.py — Multi-provider AI engine + Self-learning Strategy DNA

Providers (priority order):
  1. Groq (free) — llama-3.3-70b-versatile
  2. Anthropic Claude Haiku (ANTHROPIC_API_KEY) — claude-haiku-4-5-20251001
  3. OpenAI-compatible (OPENAI_API_KEY, optional)

Strategy DNA lifecycle:
  new_trade → save_market_snapshot → [N trades] → evolve_strategy →
  save_dna → apply_dna → adjusted_score → trade → repeat
"""

import os
import json
import logging
from datetime import datetime

_log = logging.getLogger(__name__)

# ── Provider detection ────────────────────────────────────────────────────────

def _get_providers() -> list[tuple]:
    """Return available AI providers in priority order as (name, key, model) tuples."""
    providers = []
    groq_key = os.environ.get("GROQ_API_KEY", "").strip()
    if groq_key:
        providers.append(("groq", groq_key, "llama-3.3-70b-versatile"))
    ant_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if ant_key:
        providers.append(("anthropic", ant_key, "claude-haiku-4-5-20251001"))
    oai_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if oai_key:
        providers.append(("openai", oai_key, "gpt-4o-mini"))
    return providers


def get_active_providers() -> list[str]:
    """Return list of active provider names for UI display."""
    return [p[0] for p in _get_providers()]


def call_ai(messages: list, max_tokens: int = 1200, temperature: float = 0.4,
            prefer_quality: bool = False) -> str:
    """
    Send messages to the best available AI provider with automatic fallback.
    messages: list of {role: system|user|assistant, content: str}.
    prefer_quality: if True, try Anthropic/OpenAI before Groq for complex tasks.
    Returns response text or error string.
    """
    providers = _get_providers()
    if not providers:
        return "⚠️ Sin API key. Añade GROQ_API_KEY (gratis) o ANTHROPIC_API_KEY en Railway → Variables."

    if prefer_quality:
        quality_order = ["anthropic", "openai", "groq"]
        providers = sorted(providers, key=lambda p: quality_order.index(p[0])
                           if p[0] in quality_order else 99)

    system_msg = next((m["content"] for m in messages if m["role"] == "system"), "")
    chat_msgs  = [m for m in messages if m["role"] != "system"]

    for provider, key, model in providers:
        try:
            if provider == "groq":
                from groq import Groq
                all_msgs = ([{"role": "system", "content": system_msg}]
                            if system_msg else []) + chat_msgs
                resp = Groq(api_key=key).chat.completions.create(
                    model=model, messages=all_msgs,
                    max_tokens=max_tokens, temperature=temperature,
                )
                return resp.choices[0].message.content

            elif provider == "anthropic":
                import anthropic
                resp = anthropic.Anthropic(api_key=key).messages.create(
                    model=model, max_tokens=max_tokens,
                    system=system_msg or "You are a helpful assistant.",
                    messages=chat_msgs,
                )
                return resp.content[0].text

            elif provider == "openai":
                from openai import OpenAI
                all_msgs = ([{"role": "system", "content": system_msg}]
                            if system_msg else []) + chat_msgs
                resp = OpenAI(api_key=key).chat.completions.create(
                    model=model, messages=all_msgs,
                    max_tokens=max_tokens, temperature=temperature,
                )
                return resp.choices[0].message.content

        except Exception as e:
            _log.warning("AI provider %s failed: %s", provider, e)
            continue

    return "⚠️ Todos los proveedores AI fallaron. Revisa las API keys en Railway → Variables."


def _parse_json(text: str) -> dict | None:
    """Strip markdown fences and parse JSON. Returns None on failure."""
    text = text.strip()
    if text.startswith("```"):
        parts = text.split("```")
        text = parts[1] if len(parts) > 1 else text
        if text.startswith("json"):
            text = text[4:]
    try:
        return json.loads(text.strip())
    except Exception:
        return None


# ── Strategy DNA ──────────────────────────────────────────────────────────────

DEFAULT_DNA: dict = {
    "version": 1,
    "min_score": 70,
    "preferred_sessions": ["London", "NY"],
    "regime_weights": {
        "trending_up": 1.2,
        "trending_down": 1.2,
        "ranging": 0.8,
        "volatile": 0.9,
        "neutral": 1.0,
    },
    "dxy_filter_strength": 1.0,
    "volume_spike_bonus": 5,
    "delta_threshold_pct": 10,
    "score_boost_strong_regime": 8,
    "blacklist_hours_utc": [22, 23, 0, 1],
    "min_confluences": 3,
    "explanation": "Estrategia inicial base — pendiente evolución con datos reales",
    "key_insight": "Empezando a aprender del mercado",
    "fitness": 0.0,
    "trades_evaluated": 0,
    "winrate": 0.0,
    "net_pips": 0.0,
    "evolved_at": None,
}


def apply_dna_to_signal(signal: dict, score: int, dna: dict,
                        session: str, dxy_dir: str) -> tuple[int, list[str]]:
    """
    Apply strategy DNA rules to adjust a confluence score.
    Returns (adjusted_score, list_of_reason_strings).
    The reasons are appended to the score breakdown display.
    """
    if not dna or not isinstance(dna, dict):
        return score, []

    adj = 0
    reasons = []
    final_sig = signal.get("final_signal", "NEUTRAL")

    # Session filter
    preferred = dna.get("preferred_sessions") or []
    if preferred and session:
        if session in preferred:
            adj += 5
            reasons.append(f"🧬 DNA sesión {session} óptima (+5)")
        else:
            adj -= 12
            reasons.append(f"🧬 DNA sesión {session} fuera de ventana óptima (-12)")

    # Regime weight
    regime_raw = (signal.get("regime") or signal.get("kb_regime_label") or "").lower()
    regime_weights = dna.get("regime_weights") or {}
    _matched = next((k for k in regime_weights if k.lower() in regime_raw), None)
    if _matched:
        w = regime_weights[_matched]
        regime_adj = int((w - 1.0) * 16)
        if regime_adj != 0:
            adj += regime_adj
            reasons.append(f"🧬 DNA régimen {_matched} × {w} ({regime_adj:+d})")

    # DXY filter
    dxy_strength = float(dna.get("dxy_filter_strength") or 1.0)
    if dxy_strength > 0:
        confirms = (
            (final_sig == "COMPRA" and dxy_dir == "DOWN") or
            (final_sig == "VENTA" and dxy_dir == "UP")
        )
        contradicts = (
            (final_sig == "COMPRA" and dxy_dir == "UP") or
            (final_sig == "VENTA" and dxy_dir == "DOWN")
        )
        dxy_val = int(dxy_strength * 8)
        if confirms:
            adj += dxy_val
            reasons.append(f"🧬 DNA DXY confirma dirección (+{dxy_val})")
        elif contradicts:
            adj -= dxy_val
            reasons.append(f"🧬 DNA DXY contradice dirección (-{dxy_val})")

    # Volume spike bonus
    spike_bonus = int(dna.get("volume_spike_bonus") or 0)
    if spike_bonus and signal.get("volume_spike"):
        adj += spike_bonus
        reasons.append(f"🧬 DNA spike volumen confirmado (+{spike_bonus})")

    # Strong regime boost
    strong_boost = int(dna.get("score_boost_strong_regime") or 0)
    if strong_boost and ("strong" in regime_raw or "fuerte" in regime_raw):
        adj += strong_boost
        reasons.append(f"🧬 DNA régimen fuerte (+{strong_boost})")

    # Hour blacklist
    blacklist = dna.get("blacklist_hours_utc") or []
    _utc_hour = datetime.utcnow().hour
    if blacklist and _utc_hour in blacklist:
        adj -= 20
        reasons.append(f"🧬 DNA hora {_utc_hour}h UTC en lista negra (-20)")

    final_score = max(0, min(100, score + adj))
    return final_score, reasons


# ── Evolution cycle ───────────────────────────────────────────────────────────

def evolve_strategy(recent_trades: list, current_dna: dict) -> dict | None:
    """
    Analyze recent trades using AI and generate an improved Strategy DNA.
    Returns updated DNA dict, or None if evolution fails (< 5 trades or AI error).
    """
    if len(recent_trades) < 5:
        return None

    wins   = [t for t in recent_trades if (t.get("pips") or 0) > 0]
    losses = [t for t in recent_trades if (t.get("pips") or 0) <= 0]
    winrate  = round(len(wins) / len(recent_trades) * 100, 1)
    net_pips = round(sum(t.get("pips") or 0 for t in recent_trades), 1)

    # Summarise by strategy, session, regime
    strat_stats: dict = {}
    for t in recent_trades:
        s = t.get("strategy") or "unknown"
        strat_stats.setdefault(s, {"wins": 0, "losses": 0, "pips": 0.0})
        if (t.get("pips") or 0) > 0:
            strat_stats[s]["wins"] += 1
        else:
            strat_stats[s]["losses"] += 1
        strat_stats[s]["pips"] = round(strat_stats[s]["pips"] + (t.get("pips") or 0), 1)

    trade_digest = []
    for t in recent_trades[-25:]:
        snap = t.get("market_snapshot") or {}
        trade_digest.append({
            "direction": t.get("direction", "?"),
            "outcome":   t.get("outcome", "?"),
            "pips":      round(t.get("pips") or 0, 1),
            "score":     t.get("score", 0),
            "strategy":  t.get("strategy", "?"),
            "session":   snap.get("session", "?"),
            "regime":    snap.get("regime", "?"),
            "dxy_dir":   snap.get("dxy_dir", "?"),
            "vol_spike": snap.get("vol_spike", False),
        })

    prompt = f"""Eres un optimizador cuantitativo de estrategias de trading para EUR/USD.

DNA ACTUAL (v{current_dna.get('version', 1)}):
{json.dumps(current_dna, indent=2, ensure_ascii=False)}

RENDIMIENTO RECIENTE ({len(recent_trades)} operaciones):
- Win Rate: {winrate}%  |  Pips netos: {net_pips:+.1f}
- Wins: {len(wins)}  |  Losses: {len(losses)}
- Por estrategia: {json.dumps(strat_stats, ensure_ascii=False)}

DETALLE ÚLTIMOS {len(trade_digest)} TRADES:
{json.dumps(trade_digest, indent=2, ensure_ascii=False)}

TAREA: Analiza los tres pilares:
1. TÉCNICO: ¿qué regímenes/scores/estrategias correlacionan con wins vs losses?
2. FUNDAMENTAL: ¿DXY dirección tiene impacto? ¿algún patrón en sesiones?
3. SENTIMIENTO/VOLUMEN: ¿los spikes de volumen mejoran o empeoran resultados?

Genera un DNA mejorado. Reglas:
- Incrementa "version" en 1
- Ajusta SOLO los campos que el análisis justifica cambiar
- "explanation" (≤150 chars) explica los cambios en español
- "key_insight" (≤80 chars) el hallazgo más valioso
- Mantén JSON válido con EXACTAMENTE los mismos campos que el DNA actual
- No inventes campos nuevos

Responde SOLO con el JSON del nuevo DNA (sin markdown ni explicaciones extra):"""

    response = call_ai(
        [{"role": "user", "content": prompt}],
        max_tokens=1100,
        temperature=0.25,
        prefer_quality=True,
    )

    new_dna = _parse_json(response)
    if not new_dna or not isinstance(new_dna, dict):
        _log.warning("Evolution: failed to parse AI response as JSON")
        return None

    # Sanitize and enrich
    new_dna["fitness"]          = winrate
    new_dna["trades_evaluated"] = len(recent_trades)
    new_dna["winrate"]          = winrate
    new_dna["net_pips"]         = net_pips
    new_dna["evolved_at"]       = datetime.utcnow().isoformat()
    new_dna.setdefault("version", (current_dna.get("version") or 1) + 1)
    new_dna.setdefault("key_insight", "")
    new_dna.setdefault("explanation", "")

    # Clamp numeric values to sane ranges
    new_dna["min_score"]    = max(50, min(90, int(new_dna.get("min_score") or 70)))
    new_dna["dxy_filter_strength"] = max(0.0, min(2.0, float(new_dna.get("dxy_filter_strength") or 1.0)))

    return new_dna


# ── Post-mortem analysis ──────────────────────────────────────────────────────

def analyze_trade_postmortem(
    direction: str, outcome: str, pips: float,
    entry_price: float, exit_price: float | None,
    market_snapshot: dict,
) -> tuple[str, list[str]]:
    """
    Brief AI analysis of why a trade won or lost.
    Returns (one_sentence_analysis, [lesson, avoid_if, seek_if]).
    """
    emoji = "✅" if pips > 0 else "❌"
    snap_summary = []
    if market_snapshot:
        for k in ("session", "regime", "dxy_dir", "score", "signal", "strategy", "vol_spike"):
            v = market_snapshot.get(k)
            if v is not None:
                snap_summary.append(f"{k}: {v}")

    prompt = f"""{emoji} Trade EUR/USD {direction} → {outcome} ({pips:+.1f} pips)
Entrada: {entry_price:.5f} | Salida: {exit_price:.5f if exit_price else 'N/A'}
Contexto: {' | '.join(snap_summary) or 'no disponible'}

Responde SOLO con JSON (sin markdown):
{{"analysis":"<1 frase: qué ocurrió y factor clave>",
  "lesson":"<qué aprender de este trade>",
  "avoid_if":"<cuándo evitar esta operación>",
  "seek_if":"<cuándo buscar esta operación>"}}"""

    response = call_ai(
        [{"role": "user", "content": prompt}],
        max_tokens=250, temperature=0.2,
    )
    data = _parse_json(response)
    if not data:
        return f"{'Ganancia' if pips > 0 else 'Pérdida'} de {abs(pips):.1f}p en {direction}", []

    analysis = data.get("analysis", "")
    lessons  = [v for k, v in data.items() if k != "analysis" and isinstance(v, str) and v]
    return analysis, lessons


# ── Market snapshot builder ───────────────────────────────────────────────────

def build_market_snapshot(signal: dict, score: int, session: str, dxy_dir: str,
                           vol_spikes: list, delta: dict | None,
                           context_reasons: list | None,
                           dna_version: int = 1) -> dict:
    """Build a compact market snapshot dict to save with a trade."""
    return {
        "signal":    signal.get("final_signal", "NEUTRAL"),
        "score":     score,
        "session":   session,
        "regime":    signal.get("regime") or signal.get("kb_regime_label", ""),
        "strategy":  signal.get("strategy") or signal.get("kb_best_strategy", ""),
        "dxy_dir":   dxy_dir,
        "buy_sigs":  signal.get("buy_signals", 0),
        "sell_sigs": signal.get("sell_signals", 0),
        "vol_spike": bool(vol_spikes),
        "delta_pct": round(delta.get("delta_pct", 0), 1) if delta else 0,
        "price":     signal.get("price"),
        "top_reasons": (context_reasons or [])[:5],
        "dna_version": dna_version,
        "ts": datetime.utcnow().isoformat(),
    }

"""
ai_engine.py — Multi-provider AI engine + Self-learning Strategy DNA

Active providers (priority order):
  1. Groq          — llama-3.3-70b-versatile          (30 RPM, 1000 RPD)
  2. Cerebras      — llama-3.3-70b                    (1M tok/day, 30 RPM)
  3. Zhipu GLM     — glm-4-flash / glm-z1-flash        (free, OpenAI-compatible)
  4. Anthropic     — claude-haiku-4-5-20251001         (if key present)
  5. OpenAI        — gpt-4o-mini                       (if key present)

Strategy DNA lifecycle:
  new_trade → save_market_snapshot → [N trades] → evolve_strategy →
  save_dna → apply_dna → adjusted_score → trade → repeat
"""

import os
import re
import json
import logging
from datetime import datetime

_log = logging.getLogger(__name__)


def _strip_thinking(text: str) -> str:
    """Strip <think>...</think> reasoning blocks emitted by models like glm-z1-flash."""
    cleaned = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
    return cleaned.strip()

# ── Provider detection ────────────────────────────────────────────────────────

def _get_providers() -> list[tuple]:
    """Return all available AI providers in priority order as (name, key, model) tuples."""
    providers = []

    groq_key = os.environ.get("GROQ_API_KEY", "").strip()
    if groq_key:
        providers.append(("groq", groq_key, "llama-3.3-70b-versatile"))

    cerebras_key = os.environ.get("CEREBRAS_API_KEY", "").strip()
    if cerebras_key:
        providers.append(("cerebras", cerebras_key, "llama-3.3-70b"))

    zhipu_key = os.environ.get("ZHIPU_API_KEY", "").strip()
    if zhipu_key:
        providers.append(("zhipu", zhipu_key, "glm-4-flash"))

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


def get_providers_status() -> list[dict]:
    """Return detailed status of all possible providers."""
    checks = [
        ("groq",      "GROQ_API_KEY",      "llama-3.3-70b-versatile",    "30 RPM · 1000 RPD · gratuito"),
        ("cerebras",  "CEREBRAS_API_KEY",  "llama-3.3-70b",              "1M tok/día · 30 RPM · gratuito"),
        ("zhipu",     "ZHIPU_API_KEY",     "glm-4-flash / glm-z1-flash", "gratuito · Zhipu AI"),
        ("anthropic", "ANTHROPIC_API_KEY", "claude-haiku-4-5-20251001",  "pago"),
        ("openai",    "OPENAI_API_KEY",    "gpt-4o-mini",                "pago"),
    ]
    result = []
    for name, env_var, model, limits in checks:
        active = bool(os.environ.get(env_var, "").strip())
        result.append({"name": name, "model": model, "limits": limits,
                        "active": active, "env_var": env_var})
    return result


# ── Core call_ai ──────────────────────────────────────────────────────────────

def call_ai(messages: list, max_tokens: int = 1200, temperature: float = 0.4,
            prefer_quality: bool = False, prefer_reasoning: bool = False) -> str:
    """
    Send messages to the best available free AI provider with automatic fallback.
    prefer_quality: try Anthropic/OpenAI before speed-optimised providers.
    prefer_reasoning: use OpenRouter DeepSeek-R1 (reasoning model) first.
    """
    providers = _get_providers()
    if not providers:
        return ("⚠️ Sin API key activa. Proveedores configurados: GROQ_API_KEY, "
                "CEREBRAS_API_KEY, ZHIPU_API_KEY — revisa Railway → Variables.")

    if prefer_reasoning:
        order = ["zhipu", "groq", "cerebras", "anthropic", "openai"]
        providers = sorted(providers, key=lambda p: order.index(p[0]) if p[0] in order else 99)
    elif prefer_quality:
        order = ["anthropic", "openai", "groq", "zhipu", "cerebras"]
        providers = sorted(providers, key=lambda p: order.index(p[0]) if p[0] in order else 99)

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
                return _strip_thinking(resp.choices[0].message.content)

            elif provider == "cerebras":
                from openai import OpenAI
                all_msgs = ([{"role": "system", "content": system_msg}]
                            if system_msg else []) + chat_msgs
                resp = OpenAI(
                    api_key=key,
                    base_url="https://api.cerebras.ai/v1"
                ).chat.completions.create(
                    model=model, messages=all_msgs,
                    max_tokens=min(max_tokens, 8192),  # Cerebras free cap
                    temperature=temperature,
                )
                return _strip_thinking(resp.choices[0].message.content)

            elif provider == "zhipu":
                from openai import OpenAI
                all_msgs = ([{"role": "system", "content": system_msg}]
                            if system_msg else []) + chat_msgs
                # Use glm-z1-flash (reasoning) when prefer_reasoning was set
                zhipu_model = "glm-z1-flash" if prefer_reasoning else "glm-4-flash"
                resp = OpenAI(
                    api_key=key,
                    base_url="https://open.bigmodel.cn/api/paas/v4/",
                ).chat.completions.create(
                    model=zhipu_model, messages=all_msgs,
                    max_tokens=max_tokens, temperature=temperature,
                )
                return _strip_thinking(resp.choices[0].message.content)

            elif provider == "anthropic":
                import anthropic
                resp = anthropic.Anthropic(api_key=key).messages.create(
                    model=model, max_tokens=max_tokens,
                    system=system_msg or "You are a helpful assistant.",
                    messages=chat_msgs,
                )
                return _strip_thinking(resp.content[0].text)

            elif provider == "openai":
                from openai import OpenAI
                all_msgs = ([{"role": "system", "content": system_msg}]
                            if system_msg else []) + chat_msgs
                resp = OpenAI(api_key=key).chat.completions.create(
                    model=model, messages=all_msgs,
                    max_tokens=max_tokens, temperature=temperature,
                )
                return _strip_thinking(resp.choices[0].message.content)

        except Exception as e:
            _log.warning("AI provider %s failed: %s", provider, e)
            continue

    return "⚠️ Todos los proveedores AI fallaron. Revisa las API keys en Railway → Variables."


def _parse_json(text: str) -> dict | None:
    """Strip markdown fences and parse JSON. Returns None on failure."""
    import re as _re
    text = text.strip()
    # Strip markdown code fences
    if text.startswith("```"):
        parts = text.split("```")
        text = parts[1] if len(parts) > 1 else text
        if text.startswith("json"):
            text = text[4:]
    # Try direct parse
    try:
        return json.loads(text.strip())
    except Exception:
        pass
    # Try to find the first {...} block (handles preamble / postamble text)
    m = _re.search(r'\{[\s\S]*\}', text)
    if m:
        try:
            return json.loads(m.group())
        except Exception:
            pass
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
    """Apply strategy DNA rules to adjust a confluence score."""
    if not dna or not isinstance(dna, dict):
        return score, []

    adj = 0
    reasons = []
    final_sig = signal.get("final_signal", "NEUTRAL")

    preferred = dna.get("preferred_sessions") or []
    if preferred and session:
        if session in preferred:
            adj += 5; reasons.append(f"🧬 DNA sesión {session} óptima (+5)")
        else:
            adj -= 12; reasons.append(f"🧬 DNA sesión {session} fuera de ventana óptima (-12)")

    regime_raw = (signal.get("regime") or signal.get("kb_regime_label") or "").lower()
    regime_weights = dna.get("regime_weights") or {}
    _matched = next((k for k in regime_weights if k.lower() in regime_raw), None)
    if _matched:
        w = regime_weights[_matched]
        regime_adj = int((w - 1.0) * 16)
        if regime_adj != 0:
            adj += regime_adj; reasons.append(f"🧬 DNA régimen {_matched} × {w} ({regime_adj:+d})")

    dxy_strength = float(dna.get("dxy_filter_strength") or 1.0)
    if dxy_strength > 0:
        confirms = ((final_sig == "COMPRA" and dxy_dir == "DOWN") or
                    (final_sig == "VENTA" and dxy_dir == "UP"))
        contradicts = ((final_sig == "COMPRA" and dxy_dir == "UP") or
                       (final_sig == "VENTA" and dxy_dir == "DOWN"))
        dxy_val = int(dxy_strength * 8)
        if confirms:   adj += dxy_val; reasons.append(f"🧬 DNA DXY confirma (+{dxy_val})")
        elif contradicts: adj -= dxy_val; reasons.append(f"🧬 DNA DXY contradice (-{dxy_val})")

    spike_bonus = int(dna.get("volume_spike_bonus") or 0)
    if spike_bonus and signal.get("volume_spike"):
        adj += spike_bonus; reasons.append(f"🧬 DNA spike volumen (+{spike_bonus})")

    strong_boost = int(dna.get("score_boost_strong_regime") or 0)
    if strong_boost and ("strong" in regime_raw or "fuerte" in regime_raw):
        adj += strong_boost; reasons.append(f"🧬 DNA régimen fuerte (+{strong_boost})")

    blacklist = dna.get("blacklist_hours_utc") or []
    _utc_hour = datetime.utcnow().hour
    if blacklist and _utc_hour in blacklist:
        adj -= 20; reasons.append(f"🧬 DNA hora {_utc_hour}h UTC en lista negra (-20)")

    return max(0, min(100, score + adj)), reasons


# ── Evolution cycle ───────────────────────────────────────────────────────────

def evolve_strategy(recent_trades: list, current_dna: dict) -> dict | None:
    """Analyze recent trades using AI and generate an improved Strategy DNA."""
    if len(recent_trades) < 5:
        return None

    wins    = [t for t in recent_trades if (t.get("pips") or 0) > 0]
    losses  = [t for t in recent_trades if (t.get("pips") or 0) <= 0]
    winrate = round(len(wins) / len(recent_trades) * 100, 1)
    net_pips = round(sum(t.get("pips") or 0 for t in recent_trades), 1)

    strat_stats: dict = {}
    for t in recent_trades:
        s = t.get("strategy") or "unknown"
        strat_stats.setdefault(s, {"wins": 0, "losses": 0, "pips": 0.0})
        if (t.get("pips") or 0) > 0: strat_stats[s]["wins"] += 1
        else: strat_stats[s]["losses"] += 1
        strat_stats[s]["pips"] = round(strat_stats[s]["pips"] + (t.get("pips") or 0), 1)

    trade_digest = []
    for t in recent_trades[-25:]:
        snap = t.get("market_snapshot") or {}
        trade_digest.append({
            "direction": t.get("direction", "?"), "outcome": t.get("outcome", "?"),
            "pips": round(t.get("pips") or 0, 1), "score": t.get("score", 0),
            "strategy": t.get("strategy", "?"), "session": snap.get("session", "?"),
            "regime": snap.get("regime", "?"), "dxy_dir": snap.get("dxy_dir", "?"),
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

Responde SOLO con el JSON del nuevo DNA (sin markdown ni explicaciones extra):"""

    response = call_ai(
        [{"role": "user", "content": prompt}],
        max_tokens=1100, temperature=0.25, prefer_quality=True,
    )

    new_dna = _parse_json(response)
    if not new_dna or not isinstance(new_dna, dict):
        return None

    new_dna["fitness"]          = winrate
    new_dna["trades_evaluated"] = len(recent_trades)
    new_dna["winrate"]          = winrate
    new_dna["net_pips"]         = net_pips
    new_dna["evolved_at"]       = datetime.utcnow().isoformat()
    new_dna.setdefault("version", (current_dna.get("version") or 1) + 1)
    new_dna.setdefault("key_insight", "")
    new_dna.setdefault("explanation", "")
    new_dna["min_score"]            = max(50, min(90, int(new_dna.get("min_score") or 70)))
    new_dna["dxy_filter_strength"]  = max(0.0, min(2.0, float(new_dna.get("dxy_filter_strength") or 1.0)))
    return new_dna


# ── Post-mortem analysis ──────────────────────────────────────────────────────

def analyze_trade_postmortem(
    direction: str, outcome: str, pips: float,
    entry_price: float, exit_price: float | None,
    market_snapshot: dict,
) -> tuple[str, list[str]]:
    """Brief AI analysis of why a trade won or lost."""
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

    response = call_ai([{"role": "user", "content": prompt}],
                       max_tokens=250, temperature=0.2)
    data = _parse_json(response)
    if not data:
        return f"{'Ganancia' if pips > 0 else 'Pérdida'} de {abs(pips):.1f}p en {direction}", []
    analysis = data.get("analysis", "")
    lessons  = [v for k, v in data.items() if k != "analysis" and isinstance(v, str) and v]
    return analysis, lessons


# ── Market snapshot builder ───────────────────────────────────────────────────

def synthesize_trading_decision(ctx: dict) -> dict:
    """
    Weighted-vote synthesis of ALL trading signals → single decisive recommendation.
    Resolves contradictions via institutional hierarchy, then calls AI for the
    natural-language verdict (3 reasons + 1 verdict sentence).

    Resolution hierarchy (highest weight wins):
      DXY + COT (institutional) > KB backtested strategy > TF alignment > Score > AI bias > Macro
    """
    import json as _j

    mode = ctx.get("mode", "intraday")

    # Mode-aware TF weights (scalping ignores daily, swing ignores 15m)
    tf_w = {
        "scalping": {"15m": 3, "1h": 2, "4h": 1, "1d": 0},
        "intraday": {"15m": 1, "1h": 3, "4h": 2, "1d": 1},
        "swing":    {"15m": 0, "1h": 1, "4h": 3, "1d": 3},
    }.get(mode, {"15m": 1, "1h": 3, "4h": 2, "1d": 1})

    vl = vs = 0
    log: list[str] = []

    def _vote(direction: str, weight: int, label: str) -> None:
        nonlocal vl, vs
        if direction == "LONG":
            vl += weight; log.append(f"+{weight} LONG — {label}")
        elif direction == "SHORT":
            vs += weight; log.append(f"+{weight} SHORT — {label}")

    # 1. Technical timeframes
    for tf, data in ctx.get("timeframes", {}).items():
        w = tf_w.get(tf, 1)
        if w == 0:
            continue
        s = data.get("signal", "")
        if   s == "COMPRA": _vote("LONG",  w, f"TF {tf} alcista")
        elif s == "VENTA":  _vote("SHORT", w, f"TF {tf} bajista")

    # 2. DXY — institutional (weight 2)
    dxy = ctx.get("dxy_dir", "")
    if   dxy == "DOWN": _vote("LONG",  2, "DXY bajista → EUR sube")
    elif dxy == "UP":   _vote("SHORT", 2, "DXY alcista → EUR baja")

    # 3. COT — institutional positioning (weight 2)
    cot = ctx.get("cot_dir", "")
    if   cot == "LONG":  _vote("LONG",  2, "COT: institucionales largos")
    elif cot == "SHORT": _vote("SHORT", 2, "COT: institucionales cortos")

    # 4. KB backtested strategy (weight 2)
    kb = ctx.get("kb_dir", "")
    if   kb == "LONG":  _vote("LONG",  2, "Estrategia KB certificada")
    elif kb == "SHORT": _vote("SHORT", 2, "Estrategia KB certificada")

    # 5. Confluence score direction (weight 1, only if ≥65)
    sc = ctx.get("score", 0)
    sd = ctx.get("score_dir", "")
    if sc >= 65 and sd:
        _vote(sd, 1, f"Score confluencia {sc}/100")

    # 6. AI structural market bias (weight 1)
    ab = ctx.get("ai_bias_dir", "")
    if ab: _vote(ab, 1, "Bias estructural IA")

    # 7. Macro / fundamental signal (weight 1)
    md = ctx.get("macro_dir", "")
    if md: _vote(md, 1, "Contexto macro/fundamental")

    # Net result
    total = vl + vs
    if total == 0:
        direction, confidence = "WAIT", 0
    elif vl > vs:
        direction  = "LONG"
        confidence = min(95, round(vl / total * 100))
    elif vs > vl:
        direction  = "SHORT"
        confidence = min(95, round(vs / total * 100))
    else:
        direction, confidence = "WAIT", 50

    # Fallback (no AI)
    reasons   = (log[:3] if log else ["Sin datos suficientes para síntesis"])
    verdict   = f"{direction} — {confidence}% confluencia ({vl}L vs {vs}S votos ponderados)"
    risk_note = None

    # AI natural-language verdict — fast, low-token call
    system_prompt = (
        "Eres analista institucional senior de FX. Responde SOLO con JSON válido, sin texto extra.\n"
        "Formato exacto: {\"reasons\":[\"frase1\",\"frase2\",\"frase3\"],"
        "\"verdict\":\"oración\",\"risk_note\":null}\n"
        "Reglas:\n"
        "- reasons: exactamente 3 strings en español, máx 12 palabras cada uno\n"
        "- Selecciona los 3 factores MÁS IMPORTANTES que justifican 'direction'\n"
        "- verdict: 1 oración ≤20 palabras explicando la decisión tomada\n"
        "- risk_note: null o advertencia específica ≤10 palabras si hay riesgo real\n"
        "- NO contradigas el 'direction' ya calculado por el sistema de votos"
    )
    payload = {
        "direction": direction, "confidence": confidence,
        "votes_long": vl, "votes_short": vs,
        "vote_log": log[:8],
        "price": ctx.get("price"), "session": ctx.get("session", ""),
        "regime": ctx.get("regime", ""), "score": sc,
        "dxy": dxy, "cot": cot, "mode": mode,
        "atr_pips": ctx.get("atr_pips"),
        "context_reasons": ctx.get("context_reasons", [])[:4],
    }
    try:
        raw = call_ai([
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": _j.dumps(payload, ensure_ascii=False, default=str)},
        ], max_tokens=350, temperature=0.15)
        p = _parse_json(raw)
        if p and isinstance(p.get("reasons"), list) and len(p["reasons"]) >= 1:
            reasons   = p["reasons"][:3]
            verdict   = p.get("verdict", verdict)
            risk_note = p.get("risk_note") or None
    except Exception as _e:
        _log.warning("synthesize_trading_decision AI: %s", _e)

    return {
        "direction":  direction,
        "confidence": confidence,
        "votes_long": vl,
        "votes_short": vs,
        "vote_log":   log,
        "reasons":    reasons,
        "verdict":    verdict,
        "risk_note":  risk_note,
        "entry": ctx.get("price"),
        "tp1":   ctx.get("tp1"),
        "tp2":   ctx.get("tp2"),
        "sl":    ctx.get("sl"),
        "rr":    ctx.get("rr"),
        "mode":  mode,
    }


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

"""
ai_engine.py — Multi-provider AI engine + Self-learning Strategy DNA

Free providers (priority order):
  1. Groq          — llama-3.3-70b-versatile          (30 RPM, 1000 RPD)
  2. Cerebras      — llama-3.3-70b                    (1M tok/day, 30 RPM)
  3. Gemini Flash  — gemini-2.0-flash-lite             (15 RPM, 1000 RPD)
  4. Mistral       — mistral-small-latest              (1B tok/month)
  5. OpenRouter    — deepseek/deepseek-r1:free         (reasoning, free)
  6. Anthropic     — claude-haiku-4-5-20251001         (if key present)
  7. OpenAI        — gpt-4o-mini                       (if key present)

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
    """Return all available AI providers in priority order as (name, key, model) tuples."""
    providers = []

    groq_key = os.environ.get("GROQ_API_KEY", "").strip()
    if groq_key:
        providers.append(("groq", groq_key, "llama-3.3-70b-versatile"))

    cerebras_key = os.environ.get("CEREBRAS_API_KEY", "").strip()
    if cerebras_key:
        providers.append(("cerebras", cerebras_key, "llama-3.3-70b"))

    gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if gemini_key:
        providers.append(("gemini", gemini_key, "gemini-2.0-flash-lite"))

    mistral_key = os.environ.get("MISTRAL_API_KEY", "").strip()
    if mistral_key:
        providers.append(("mistral", mistral_key, "mistral-small-latest"))

    openrouter_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if openrouter_key:
        providers.append(("openrouter", openrouter_key, "deepseek/deepseek-r1:free"))

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
        ("groq",        "GROQ_API_KEY",        "llama-3.3-70b-versatile",    "30 RPM · 1000 RPD · gratuito"),
        ("cerebras",    "CEREBRAS_API_KEY",     "llama-3.3-70b",              "1M tok/día · 30 RPM · gratuito"),
        ("gemini",      "GEMINI_API_KEY",       "gemini-2.0-flash-lite",      "15 RPM · 1000 RPD · gratuito"),
        ("mistral",     "MISTRAL_API_KEY",      "mistral-small-latest",       "1B tok/mes · gratuito"),
        ("openrouter",  "OPENROUTER_API_KEY",   "deepseek-r1:free",           "reasoning · gratuito"),
        ("anthropic",   "ANTHROPIC_API_KEY",    "claude-haiku-4-5-20251001",  "pago"),
        ("openai",      "OPENAI_API_KEY",       "gpt-4o-mini",                "pago"),
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
        return ("⚠️ Sin API key. Añade GROQ_API_KEY (gratis) en Railway → Variables. "
                "También puedes añadir CEREBRAS_API_KEY, GEMINI_API_KEY, MISTRAL_API_KEY, "
                "OPENROUTER_API_KEY — todos gratuitos.")

    if prefer_reasoning:
        order = ["openrouter", "groq", "gemini", "cerebras", "mistral", "anthropic", "openai"]
        providers = sorted(providers, key=lambda p: order.index(p[0]) if p[0] in order else 99)
    elif prefer_quality:
        order = ["anthropic", "openai", "groq", "gemini", "cerebras", "mistral", "openrouter"]
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
                return resp.choices[0].message.content

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
                return resp.choices[0].message.content

            elif provider == "gemini":
                import google.generativeai as genai
                genai.configure(api_key=key)
                mdl = genai.GenerativeModel(model)
                full_prompt = (f"[SYSTEM]\n{system_msg}\n\n" if system_msg else "") + \
                              "\n".join(f"[{m['role'].upper()}]\n{m['content']}" for m in chat_msgs)
                resp = mdl.generate_content(
                    full_prompt,
                    generation_config={"max_output_tokens": max_tokens,
                                       "temperature": temperature}
                )
                return resp.text

            elif provider == "mistral":
                from mistralai import Mistral
                all_msgs = ([{"role": "system", "content": system_msg}]
                            if system_msg else []) + chat_msgs
                resp = Mistral(api_key=key).chat.complete(
                    model=model, messages=all_msgs,
                    max_tokens=max_tokens, temperature=temperature,
                )
                return resp.choices[0].message.content

            elif provider == "openrouter":
                from openai import OpenAI
                all_msgs = ([{"role": "system", "content": system_msg}]
                            if system_msg else []) + chat_msgs
                resp = OpenAI(
                    api_key=key,
                    base_url="https://openrouter.ai/api/v1",
                    default_headers={"HTTP-Referer": "https://smc-pro-app.up.railway.app",
                                     "X-Title": "SMC Pro Trading Bot"}
                ).chat.completions.create(
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

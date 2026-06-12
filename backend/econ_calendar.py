"""
backend/econ_calendar.py — Calendario económico GRATUITO (el "porqué" de las velas).

Fuente: feed JSON público de faireconomy.media (mirror del calendario de ForexFactory).
Da, por divisa: evento, hora exacta (con zona), impacto (High/Medium/Low),
previsión (forecast) y dato anterior (previous), y 'actual' una vez publicado.

POR QUÉ EXISTE este módulo (hipótesis económica, no técnica):
    El mercado FX no se mueve al azar — se mueve cuando llega información nueva.
    El 80% de la volatilidad intradía de un par concentra alrededor de sus
    publicaciones macro programadas (CPI, NFP, decisiones de tipos, GDP).
    Saber QUÉ evento viene, CUÁNDO y para QUÉ divisa convierte "adivinar" en
    "entender". Y saberlo nos deja hacer lo correcto en gestión de riesgo:
    NO abrir posiciones nuevas justo antes de un evento de alto impacto
    (el spread se dispara y el precio salta sin estructura → casino).

CÓMO se valida:
    - Cada decisión del bot queda con su 'why' (evento próximo o ausencia de él).
    - Comparando win rate de entradas "limpias" (sin evento) vs entradas cerca
      de eventos sabremos si el guard ayuda. Si no ayuda, se quita. Sin fe ciega.

QUÉ pasa si falla en producción:
    - Fail-closed: si el feed no responde, devolvemos contexto vacío que NO
      bloquea (no inventamos eventos) pero marca 'calendar_available=False'
      para que el resto del sistema sepa que va a ciegas en lo macro.
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
from datetime import datetime, timezone

_log = logging.getLogger(__name__)

_FEED_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
_CACHE_TTL = 3600            # 1 hora — el calendario semanal no cambia rápido
_CACHE_FILE = os.path.join(os.path.dirname(__file__), "_econ_cache.json")

# Ventanas del "guard" alrededor de eventos de alto impacto (en minutos)
_BLOCK_BEFORE_HIGH = 20      # no abrir 20 min antes de un evento High
_BLOCK_AFTER_HIGH  = 25      # ni 25 min después (el latigazo post-dato)
_BLOCK_BEFORE_MED  = 8       # margen menor para impacto Medium
_BLOCK_AFTER_MED   = 10

# Cache en memoria
_MEM: dict = {"ts": 0.0, "events": None}


# ── Descarga + cache ──────────────────────────────────────────────────────────

def _load_cache_file() -> list | None:
    try:
        if os.path.exists(_CACHE_FILE):
            with open(_CACHE_FILE, "r", encoding="utf-8") as f:
                blob = json.load(f)
            if time.time() - blob.get("ts", 0) < _CACHE_TTL:
                return blob.get("events")
    except Exception as e:
        _log.debug("econ cache read: %s", e)
    return None


def _save_cache_file(events: list) -> None:
    try:
        with open(_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({"ts": time.time(), "events": events}, f)
    except Exception as e:
        _log.debug("econ cache write: %s", e)


def fetch_calendar(force: bool = False) -> list[dict]:
    """
    Devuelve la lista de eventos de la semana. Cachea en memoria y en disco 1h.
    Cada evento: {title, country (divisa), date (ISO con tz), impact,
                  forecast, previous, actual?}
    """
    now = time.time()
    if not force and _MEM["events"] is not None and now - _MEM["ts"] < _CACHE_TTL:
        return _MEM["events"]

    if not force:
        cached = _load_cache_file()
        if cached is not None:
            _MEM["events"] = cached
            _MEM["ts"] = now
            return cached

    try:
        req = urllib.request.Request(_FEED_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            events = json.loads(resp.read())
        if not isinstance(events, list):
            raise ValueError("formato inesperado del feed")
        _MEM["events"] = events
        _MEM["ts"] = now
        _save_cache_file(events)
        return events
    except Exception as e:
        _log.warning("fetch_calendar: %s", e)
        # Fail-closed: devolver lo último que tengamos, o lista vacía
        return _MEM["events"] or []


# ── Parsing de tiempos ────────────────────────────────────────────────────────

def _parse_dt(date_str: str) -> datetime | None:
    """ISO '2026-06-10T08:30:00-04:00' → datetime UTC-aware. Robusto entre versiones."""
    if not date_str:
        return None
    try:
        dt = datetime.fromisoformat(date_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _surprise_direction(ev: dict) -> str:
    """
    Si hay 'actual' y 'forecast', deriva si el dato salió a favor o en contra
    de la divisa. Convención FX: dato MEJOR de lo esperado → divisa SUBE.
    (Excepción notable: desempleo, donde mayor = peor — se invierte.)
    Devuelve 'UP', 'DOWN' o '' (sin señal).
    """
    actual = _to_num(ev.get("actual"))
    fc     = _to_num(ev.get("forecast"))
    if actual is None or fc is None:
        return ""
    title = (ev.get("title") or "").lower()
    inverted = any(k in title for k in ("unemployment", "jobless", "claims"))
    better = actual > fc
    if inverted:
        better = not better
    if abs(actual - fc) < 1e-9:
        return ""
    return "UP" if better else "DOWN"


def _to_num(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        s = str(v).strip().replace("%", "").replace(",", "")
        # Sufijos K/M/B
        mult = 1.0
        if s and s[-1] in "KkMmBb":
            mult = {"k": 1e3, "m": 1e6, "b": 1e9}[s[-1].lower()]
            s = s[:-1]
        return float(s) * mult
    except Exception:
        return None


# ── Contexto por divisa / par ─────────────────────────────────────────────────

def events_for_currency(currency: str, now: datetime | None = None) -> list[dict]:
    """Eventos futuros y recientes (±12h) para una divisa, ordenados por hora."""
    now = now or datetime.now(timezone.utc)
    out = []
    for ev in fetch_calendar():
        if (ev.get("country") or "").upper() != currency.upper():
            continue
        dt = _parse_dt(ev.get("date", ""))
        if dt is None:
            continue
        mins = (dt - now).total_seconds() / 60.0
        if -12 * 60 <= mins <= 12 * 60:
            out.append({**ev, "_dt": dt, "_mins": round(mins, 1)})
    out.sort(key=lambda e: e["_dt"])
    return out


def calendar_context(symbol: str, now: datetime | None = None) -> dict:
    """
    Contexto macro para un par. Mira AMBAS divisas (base y quote).

    Devuelve:
      calendar_available : bool  — ¿pudimos leer el feed?
      block              : bool  — ¿hay evento de alto/medio impacto demasiado cerca?
      reason             : str   — el "porqué" legible (qué evento y cuándo)
      next_event         : dict  — el próximo evento relevante (o {})
      bias               : str   — 'LONG'/'SHORT'/'' sesgo derivado de sorpresas recientes
      bias_reason        : str   — explicación del sesgo
    """
    from backend.multi_pair import PAIRS
    cfg   = PAIRS.get(symbol, {})
    base  = cfg.get("base", "")
    quote = cfg.get("quote", "")
    now = now or datetime.now(timezone.utc)

    ctx = {
        "calendar_available": False,
        "block": False, "reason": "",
        "next_event": {}, "bias": "", "bias_reason": "",
    }

    events = fetch_calendar()
    if not events:
        ctx["reason"] = "Calendario no disponible — operando a ciegas en macro"
        return ctx
    ctx["calendar_available"] = True

    block_reason = ""
    soonest_block_mins = 1e9
    next_ev = {}
    soonest_future = 1e9
    bias = ""
    bias_reason = ""

    for ccy in (base, quote):
        if not ccy:
            continue
        for ev in events_for_currency(ccy, now):
            mins   = ev["_mins"]
            impact = (ev.get("impact") or "").lower()
            title  = ev.get("title") or ""

            # Próximo evento futuro relevante (para mostrar el "porqué")
            if mins >= 0 and impact in ("high", "medium") and mins < soonest_future:
                soonest_future = mins
                next_ev = {
                    "currency": ccy, "title": title, "impact": ev.get("impact"),
                    "in_minutes": round(mins), "forecast": ev.get("forecast"),
                    "previous": ev.get("previous"),
                }

            # ¿Bloquea? Evento alto/medio dentro de su ventana
            if impact == "high":
                if -_BLOCK_AFTER_HIGH <= mins <= _BLOCK_BEFORE_HIGH:
                    if abs(mins) < soonest_block_mins:
                        soonest_block_mins = abs(mins)
                        when = f"en {round(mins)} min" if mins >= 0 else f"hace {abs(round(mins))} min"
                        block_reason = f"{ccy} {title} (alto impacto) {when}"
            elif impact == "medium":
                if -_BLOCK_AFTER_MED <= mins <= _BLOCK_BEFORE_MED:
                    if abs(mins) < soonest_block_mins:
                        soonest_block_mins = abs(mins)
                        when = f"en {round(mins)} min" if mins >= 0 else f"hace {abs(round(mins))} min"
                        block_reason = f"{ccy} {title} (impacto medio) {when}"

            # Sesgo direccional por sorpresa reciente (dato ya publicado, <6h)
            if -360 <= mins < 0 and impact in ("high", "medium"):
                surp = _surprise_direction(ev)
                if surp:
                    # surp se refiere a la DIVISA ccy. Traducir al PAR:
                    ccy_up = (surp == "UP")
                    if ccy == base:
                        pair_dir = "LONG" if ccy_up else "SHORT"
                    else:  # ccy == quote: quote fuerte → par baja
                        pair_dir = "SHORT" if ccy_up else "LONG"
                    bias = pair_dir
                    arrow = "fuerte" if ccy_up else "débil"
                    bias_reason = f"{ccy} {title}: dato salió {arrow} vs previsión → {pair_dir}"

    if block_reason:
        ctx["block"] = True
        ctx["reason"] = f"⏸ Evento próximo: {block_reason} → no abrir nuevas"
    elif next_ev:
        ctx["reason"] = f"Próximo evento: {next_ev['currency']} {next_ev['title']} en {next_ev['in_minutes']} min"
    else:
        ctx["reason"] = "Sin eventos macro relevantes en la ventana — entrada técnica limpia"

    ctx["next_event"]  = next_ev
    ctx["bias"]        = bias
    ctx["bias_reason"] = bias_reason
    return ctx


# ── Resumen global (para el dashboard) ────────────────────────────────────────

def todays_high_impact(now: datetime | None = None) -> list[dict]:
    """Lista de eventos de alto impacto de hoy en adelante (para mostrar al usuario)."""
    now = now or datetime.now(timezone.utc)
    out = []
    for ev in fetch_calendar():
        if (ev.get("impact") or "").lower() != "high":
            continue
        dt = _parse_dt(ev.get("date", ""))
        if dt is None or dt < now:
            continue
        out.append({
            "currency": ev.get("country"), "title": ev.get("title"),
            "when": dt, "in_hours": round((dt - now).total_seconds() / 3600, 1),
            "forecast": ev.get("forecast"), "previous": ev.get("previous"),
        })
    out.sort(key=lambda e: e["when"])
    return out

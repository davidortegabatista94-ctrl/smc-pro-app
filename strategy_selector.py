"""
strategy_selector.py — Selector dinámico de estrategias ganadoras.

Cada 8 horas ejecuta las 17 estrategias sobre datos reales de EURUSD (60 días, 1H).
Solo activa las que han GANADO (winrate ≥ 52% + profit_factor ≥ 1.1 + ≥ 10 trades).

Para cada señal en vivo:
  1. Identifica el régimen actual (trending/ranging/neutral)
  2. Intersecta con las estrategias que GANAN en ese régimen
  3. Calcula si el consenso apoya la señal
  4. Devuelve un boost de score y las estrategias validadoras
"""

import json
import logging
import threading
from datetime import datetime, timezone, timedelta

_log = logging.getLogger("smc.selector")

# ─────────────────────────────────────────────────────────────────────────────
# MAPA: régimen → qué estrategias funcionan mejor en ese contexto
# (basado en las propiedades matemáticas de cada estrategia)
# ─────────────────────────────────────────────────────────────────────────────

REGIME_STRATEGY_MAP: dict[str, list[str]] = {
    "trending_up": [
        "ema_trend", "macd_cross", "supertrend",
        "momentum_break", "donchian_break", "precision_be",
        "aggressive_momentum", "meta_composite",
    ],
    "trending_down": [
        "ema_trend", "macd_cross", "supertrend",
        "momentum_break", "donchian_break", "precision_be",
        "aggressive_momentum", "meta_composite",
    ],
    "ranging": [
        "bb_touch", "keltner_touch", "rsi_50_cross",
        "stochastic_trend", "engulfing", "meta_composite",
    ],
    "neutral": [
        "rsi_50_cross", "stochastic_trend", "bb_touch",
        "macd_cross", "meta_composite", "precision_be",
    ],
    "unknown": [
        "meta_composite", "ema_trend", "macd_cross",
        "rsi_50_cross", "precision_be",
    ],
}

# Mínimos de calidad — si una estrategia no cumple esto, NO se usa aunque sea ganadora
MIN_WINRATE      = 52.0   # win rate mínimo %
MIN_PROFIT_FACTOR = 1.10  # profit factor mínimo
MIN_TRADES       = 10     # operaciones mínimas en backtest

# ─────────────────────────────────────────────────────────────────────────────
# CACHÉ EN MEMORIA (compartida con el hilo de UI)
# ─────────────────────────────────────────────────────────────────────────────

_cache_lock       = threading.Lock()
_cached_results: list[dict] = []      # lista de dicts por estrategia
_cached_winners:  set[str]  = set()   # estrategias que pasan el filtro de calidad
_cache_ts: datetime | None  = None
_CACHE_TTL_HOURS  = 8


def _cache_valid() -> bool:
    if _cache_ts is None or not _cached_results:
        return False
    return (datetime.now(timezone.utc) - _cache_ts).total_seconds() < _CACHE_TTL_HOURS * 3600


# ─────────────────────────────────────────────────────────────────────────────
# BACKTEST DE TODAS LAS ESTRATEGIAS
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_data():
    """Descarga 60 días de EURUSD 1H para el backtest."""
    try:
        import yfinance as yf
        df = yf.download(
            "EURUSD=X", period="60d", interval="1h",
            progress=False, auto_adjust=True,
        )
        if df is None or df.empty or len(df) < 110:
            return None
        return df
    except Exception as e:
        _log.warning("strategy_selector: descarga datos: %s", e)
        return None


def run_all_backtests() -> list[dict]:
    """
    Ejecuta las 17 estrategias sobre datos reales. Devuelve lista de resultados
    ordenada por score compuesto (winrate × profit_factor × sqrt(trades)).
    """
    df = _fetch_data()
    if df is None:
        _log.warning("strategy_selector: sin datos — backtest omitido")
        return []

    try:
        from backend.strategies import _run_single_strategy, _ALL_STRATEGIES, _STRATEGY_META
    except ImportError as e:
        _log.warning("strategy_selector: import error: %s", e)
        return []

    results = []
    for name in _ALL_STRATEGIES:
        try:
            r = _run_single_strategy(df, strategy=name, use_windows=False)
            if r:
                r["_name"] = name
                r["_label"] = (_STRATEGY_META.get(name) or {}).get("label", name)
                # Score compuesto: equilibra win rate, profit factor y cantidad de trades
                r["_composite"] = round(
                    (r.get("winrate", 0) / 100)
                    * r.get("profit_factor", 0)
                    * (r.get("total", 0) ** 0.4),
                    3,
                )
                results.append(r)
        except Exception as e:
            _log.debug("strategy_selector %s error: %s", name, e)

    results.sort(key=lambda x: x["_composite"], reverse=True)
    return results


def refresh_cache(force: bool = False) -> None:
    """Refresca el caché de backtests si ha expirado (o si force=True)."""
    global _cached_results, _cached_winners, _cache_ts
    with _cache_lock:
        if not force and _cache_valid():
            return
        _log.info("strategy_selector: ejecutando backtests de las 17 estrategias...")
        results = run_all_backtests()
        if results:
            _cached_results = results
            _cached_winners = {
                r["_name"] for r in results
                if r.get("winrate", 0)       >= MIN_WINRATE
                and r.get("profit_factor", 0) >= MIN_PROFIT_FACTOR
                and r.get("total", 0)         >= MIN_TRADES
            }
            _cache_ts = datetime.now(timezone.utc)
            _log.info(
                "strategy_selector: %d estrategias ganadoras de %d: %s",
                len(_cached_winners), len(results), _cached_winners,
            )


def get_cached_results() -> tuple[list[dict], set[str]]:
    """Devuelve (todos los resultados, set de nombres ganadores)."""
    if not _cache_valid():
        refresh_cache()
    return _cached_results, _cached_winners


# ─────────────────────────────────────────────────────────────────────────────
# SELECTOR PARA SEÑAL EN VIVO
# ─────────────────────────────────────────────────────────────────────────────

def select_for_signal(
    regime: str,
    direction: str,
    score: int,
    session: str = "",
    dxy_dir: str = "",
) -> dict:
    """
    Para las condiciones actuales devuelve:
      - recommended:    nombre de la mejor estrategia para este régimen/condición
      - supporting:     lista de estrategias ganadoras que apoyan esta condición
      - score_boost:    puntos extra al score por consenso estratégico
      - consensus_pct:  % de estrategias de régimen que son ganadoras
      - veto:           True si ninguna estrategia ganadora aplica (no operar)
      - detail:         texto para Telegram/UI
    """
    # Asegurarse de que el caché está actualizado (en background si hace falta)
    if not _cache_valid():
        try:
            t = threading.Thread(target=refresh_cache, daemon=True)
            t.start()
        except Exception:
            pass

    all_res, winners = _cached_results, _cached_winners

    # Estrategias candidatas para este régimen
    candidates = REGIME_STRATEGY_MAP.get(regime, REGIME_STRATEGY_MAP["unknown"])

    # Intersección: candidatas que además son ganadoras en backtest reciente
    supporting = [c for c in candidates if c in winners]

    # Estrategia recomendada: la primera candidata que sea ganadora, o la primera candidata
    recommended = supporting[0] if supporting else (candidates[0] if candidates else "meta_composite")

    # % de consenso
    consensus_pct = round(len(supporting) / len(candidates) * 100) if candidates else 0

    # Boost de score: +5 si hay consenso parcial, +12 si alto consenso, +0 si ninguna gana
    if len(supporting) == 0:
        score_boost = -5   # ninguna estrategia ganadora apoya → reducir confianza
    elif consensus_pct >= 60:
        score_boost = 12
    elif consensus_pct >= 30:
        score_boost = 5
    else:
        score_boost = 0

    # Veto: si hay ganadoras pero ninguna aplica al régimen actual
    # (solo vetamos si tenemos datos suficientes)
    veto = (len(winners) > 0 and len(supporting) == 0 and score < 70)

    # Obtener win rate de la estrategia recomendada
    rec_stats = next((r for r in all_res if r.get("_name") == recommended), {})
    rec_wr    = rec_stats.get("winrate", 0)
    rec_pf    = rec_stats.get("profit_factor", 0)
    rec_label = rec_stats.get("_label", recommended)

    # Texto detallado
    sup_labels = []
    for s in supporting[:3]:
        st = next((r for r in all_res if r.get("_name") == s), {})
        sup_labels.append(f"{st.get('_label', s)} ({st.get('winrate', 0):.0f}%)")

    if supporting:
        detail = (
            f"✅ {len(supporting)}/{len(candidates)} estrategias ganadoras en régimen {regime}. "
            f"Mejor: {rec_label} ({rec_wr:.0f}% WR, PF {rec_pf:.2f})"
        )
    else:
        detail = f"⚠️ Ninguna estrategia ganadora detectada para régimen {regime} — señal con cautela"

    return {
        "recommended":   recommended,
        "rec_label":     rec_label,
        "rec_winrate":   rec_wr,
        "rec_pf":        rec_pf,
        "supporting":    supporting,
        "sup_labels":    sup_labels,
        "score_boost":   score_boost,
        "consensus_pct": consensus_pct,
        "veto":          veto,
        "detail":        detail,
        "winners_total": len(winners),
    }


# ─────────────────────────────────────────────────────────────────────────────
# PERSISTENCIA EN DB (guardar ranking para UI)
# ─────────────────────────────────────────────────────────────────────────────

def save_ranking_to_db(results: list[dict]) -> None:
    """Guarda el ranking de estrategias en DB como métrica."""
    try:
        import db as _db
        ranking = [
            {
                "name":          r["_name"],
                "label":         r["_label"],
                "winrate":       r.get("winrate", 0),
                "profit_factor": r.get("profit_factor", 0),
                "net_pips":      r.get("net_pips", 0),
                "total":         r.get("total", 0),
                "composite":     r.get("_composite", 0),
                "is_winner":     r["_name"] in _cached_winners,
            }
            for r in results[:17]
        ]
        _db.save_metric(
            name="strategy_ranking",
            value=float(len(_cached_winners)),
            context={"ranking": ranking, "ts": datetime.utcnow().isoformat()},
        )
    except Exception as e:
        _log.debug("save_ranking_to_db: %s", e)


def get_latest_ranking() -> list[dict]:
    """Carga el último ranking guardado en DB."""
    try:
        import db as _db
        rows = _db.get_metrics(name="strategy_ranking", limit=1)
        if rows:
            return (rows[0].get("context") or {}).get("ranking", [])
    except Exception:
        pass
    return []


# ─────────────────────────────────────────────────────────────────────────────
# INICIALIZACIÓN (llamada desde background_worker)
# ─────────────────────────────────────────────────────────────────────────────

def ensure_ready() -> None:
    """Refresca el caché si ha expirado. Llamar al inicio del worker."""
    if not _cache_valid():
        refresh_cache()
        if _cached_results:
            save_ranking_to_db(_cached_results)

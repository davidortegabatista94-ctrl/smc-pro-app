"""
strategy_selector.py — Selector dinámico de estrategias ganadoras (doble filtro).

FILTRO CORTO PLAZO (cada 8h):
  - 60 días de EURUSD 1H
  - Gana: winrate ≥ 52% + profit_factor ≥ 1.1 + ≥ 10 trades

FILTRO LARGO PLAZO — "BACKTEST 2008" (cada 24h):
  - Datos diarios EUR/USD desde 2008 (~4400 barras)
  - Gana: winrate ≥ 52% + profit_factor ≥ 1.1 + ≥ 30 trades

ESTRATEGIA CERTIFICADA (🏆): pasa AMBOS filtros.
Solo las estrategias certificadas se usan para boost/veto en señales en vivo.

  ❌ no gana ningún filtro
  ✅ solo gana 60d (reciente)
  🏆 gana 60d + 2008 (certificada — la bot la usa)
"""

import logging
import threading
from datetime import datetime, timezone

_log = logging.getLogger("smc.selector")

# ─────────────────────────────────────────────────────────────────────────────
# MAPA: régimen → qué estrategias funcionan mejor en ese contexto
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

# ─────────────────────────────────────────────────────────────────────────────
# UMBRALES DE CALIDAD
# ─────────────────────────────────────────────────────────────────────────────

# Corto plazo (60d 1H)
MIN_WINRATE       = 52.0
MIN_PROFIT_FACTOR = 1.10
MIN_TRADES        = 10

# Largo plazo (2008+ diario) — exigimos más operaciones porque 15 años de daily
LT_MIN_WINRATE       = 52.0
LT_MIN_PROFIT_FACTOR = 1.10
LT_MIN_TRADES        = 30      # dailies: 30 operaciones en 15 años es exigente pero viable

# ─────────────────────────────────────────────────────────────────────────────
# CACHÉ EN MEMORIA
# ─────────────────────────────────────────────────────────────────────────────

_cache_lock = threading.Lock()

# ── Corto plazo (60d 1H) ──────────────────────────────────────────────────
_cached_results: list[dict] = []
_cached_winners: set[str]   = set()
_cache_ts: datetime | None  = None
_CACHE_TTL_HOURS = 8

# ── Largo plazo (2008+ daily) ─────────────────────────────────────────────
_cached_lt_results: list[dict] = []
_cached_lt_winners: set[str]   = set()
_lt_cache_ts: datetime | None  = None
_LT_CACHE_TTL_HOURS = 24        # el 2008-backtest es pesado — refrescamos 1x/día


def _cache_valid() -> bool:
    if _cache_ts is None or not _cached_results:
        return False
    return (datetime.now(timezone.utc) - _cache_ts).total_seconds() < _CACHE_TTL_HOURS * 3600


def _lt_cache_valid() -> bool:
    if _lt_cache_ts is None or not _cached_lt_results:
        return False
    return (datetime.now(timezone.utc) - _lt_cache_ts).total_seconds() < _LT_CACHE_TTL_HOURS * 3600


def certified_winners() -> set[str]:
    """Estrategias que ganan en AMBOS filtros (60d + 2008). Son las únicas que usa la señal."""
    return _cached_winners & _cached_lt_winners   # intersección


# ─────────────────────────────────────────────────────────────────────────────
# BACKTEST CORTO PLAZO (60 días 1H)
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
        _log.warning("strategy_selector: descarga datos 60d: %s", e)
        return None


def run_all_backtests() -> list[dict]:
    """
    Ejecuta las 17 estrategias sobre 60d 1H.
    Devuelve lista ordenada por score compuesto.
    """
    df = _fetch_data()
    if df is None:
        _log.warning("strategy_selector: sin datos 60d — backtest corto omitido")
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
                r["_name"]  = name
                r["_label"] = (_STRATEGY_META.get(name) or {}).get("label", name)
                r["_composite"] = round(
                    (r.get("winrate", 0) / 100)
                    * r.get("profit_factor", 0)
                    * (r.get("total", 0) ** 0.4),
                    3,
                )
                results.append(r)
        except Exception as e:
            _log.debug("strategy_selector 60d %s: %s", name, e)

    results.sort(key=lambda x: x["_composite"], reverse=True)
    return results


def refresh_cache(force: bool = False) -> None:
    """Refresca el caché corto plazo si ha expirado (o si force=True)."""
    global _cached_results, _cached_winners, _cache_ts
    with _cache_lock:
        if not force and _cache_valid():
            return
        _log.info("strategy_selector: backtests 60d × 17 estrategias...")
        results = run_all_backtests()
        if results:
            _cached_results = results
            _cached_winners = {
                r["_name"] for r in results
                if r.get("winrate", 0)        >= MIN_WINRATE
                and r.get("profit_factor", 0) >= MIN_PROFIT_FACTOR
                and r.get("total", 0)          >= MIN_TRADES
            }
            _cache_ts = datetime.now(timezone.utc)
            _log.info(
                "strategy_selector 60d: %d ganadoras de %d: %s",
                len(_cached_winners), len(results), _cached_winners,
            )


def get_cached_results() -> tuple[list[dict], set[str]]:
    """Devuelve (resultados 60d, ganadores 60d). Refresca si necesario."""
    if not _cache_valid():
        refresh_cache()
    return _cached_results, _cached_winners


# ─────────────────────────────────────────────────────────────────────────────
# BACKTEST LARGO PLAZO (2008+ daily)
# ─────────────────────────────────────────────────────────────────────────────

def run_longterm_backtests() -> list[dict]:
    """
    Ejecuta las 17 estrategias sobre datos diarios EURUSD desde 2008.
    Puede tardar 30-90 s — siempre llamar desde hilo daemon.
    Devuelve lista ordenada por score compuesto (igual que corto plazo).
    """
    try:
        from backend.strategies import (
            get_longterm_data_2008,
            run_longterm_comparison,
            _STRATEGY_META,
        )
    except ImportError as e:
        _log.warning("strategy_selector LT: import error: %s", e)
        return []

    _log.info("strategy_selector: descargando datos 2008+ (diario)...")
    df_daily = get_longterm_data_2008()
    if df_daily is None or df_daily.empty or len(df_daily) < 200:
        _log.warning("strategy_selector LT: datos insuficientes (%s barras)",
                     0 if df_daily is None else len(df_daily))
        return []

    _log.info("strategy_selector LT: %d barras diarias — ejecutando 17 estrategias...", len(df_daily))
    comparison = run_longterm_comparison(df_daily)
    if not comparison:
        return []

    results = []
    for r in comparison.get("results", []):
        name = r.get("strategy") or r.get("_name", "")
        if not name:
            continue
        r["_name"]  = name
        r["_label"] = (_STRATEGY_META.get(name) or {}).get("label", name)
        r["_composite_lt"] = round(
            (r.get("winrate", 0) / 100)
            * r.get("profit_factor", 0)
            * (r.get("total", 0) ** 0.4),
            3,
        )
        results.append(r)

    results.sort(key=lambda x: x["_composite_lt"], reverse=True)
    return results


def refresh_lt_cache(force: bool = False) -> None:
    """Refresca el caché largo plazo si ha expirado (o si force=True)."""
    global _cached_lt_results, _cached_lt_winners, _lt_cache_ts
    with _cache_lock:
        if not force and _lt_cache_valid():
            return
        _log.info("strategy_selector: iniciando backtest 2008+ (puede tardar ~60s)...")
        lt_results = run_longterm_backtests()
        if lt_results:
            _cached_lt_results = lt_results
            _cached_lt_winners = {
                r["_name"] for r in lt_results
                if r.get("winrate", 0)        >= LT_MIN_WINRATE
                and r.get("profit_factor", 0) >= LT_MIN_PROFIT_FACTOR
                and r.get("total", 0)          >= LT_MIN_TRADES
            }
            _lt_cache_ts = datetime.now(timezone.utc)
            cert = _cached_winners & _cached_lt_winners
            _log.info(
                "strategy_selector 2008+: %d ganadoras LT | %d certificadas (ambos filtros): %s",
                len(_cached_lt_winners), len(cert), cert,
            )
        else:
            _log.warning("strategy_selector LT: backtest 2008 sin resultados — mantiene caché anterior")


def get_lt_cached_results() -> tuple[list[dict], set[str]]:
    """Devuelve (resultados 2008, ganadores 2008). Refresca en bg si necesario."""
    if not _lt_cache_valid():
        t = threading.Thread(target=refresh_lt_cache, daemon=True, name="lt-backtest")
        t.start()
    return _cached_lt_results, _cached_lt_winners


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
      - recommended:    mejor estrategia certificada (o ganadora reciente si no hay cert.)
      - supporting:     lista de estrategias CERTIFICADAS que apoyan este régimen
      - score_boost:    puntos extra/restados al score
      - consensus_pct:  % candidatas del régimen que son certificadas
      - veto:           True si no hay ninguna certificada y score < 70
      - detail:         texto para Telegram/UI
      - certified_total: cuántas estrategias son certificadas (ambos filtros)
    """
    # Disparar refresco en background si hace falta
    if not _cache_valid():
        threading.Thread(target=refresh_cache, daemon=True).start()
    if not _lt_cache_valid():
        threading.Thread(target=refresh_lt_cache, daemon=True, name="lt-bg").start()

    all_res   = _cached_results
    cert      = certified_winners()   # intersección 60d ∩ 2008+
    winners60 = _cached_winners       # solo 60d (yreferencia para UI)

    # Candidatas para este régimen
    candidates = REGIME_STRATEGY_MAP.get(regime, REGIME_STRATEGY_MAP["unknown"])

    # Certificadas que aplican a este régimen
    supporting = [c for c in candidates if c in cert]

    # Si no hay certificadas, usamos las que solo ganan 60d (fallback parcial)
    supporting_fallback = [c for c in candidates if c in winners60]

    # Estrategia recomendada: primero certificada, luego solo-60d, luego candidata pura
    if supporting:
        recommended = supporting[0]
    elif supporting_fallback:
        recommended = supporting_fallback[0]
    else:
        recommended = candidates[0] if candidates else "meta_composite"

    # % de consenso sobre certificadas
    consensus_pct = round(len(supporting) / len(candidates) * 100) if candidates else 0

    # Boost de score
    # Solo las certificadas (ambos filtros) dan boost máximo
    # Fallback 60d da boost menor
    if len(supporting) == 0 and len(supporting_fallback) == 0:
        score_boost = -5    # ninguna gana → reducir confianza
    elif len(supporting) == 0:
        # Solo hay ganadoras 60d, no certificadas — boost conservador
        score_boost = 3
    elif consensus_pct >= 60:
        score_boost = 15    # alto consenso de certificadas → máximo boost
    elif consensus_pct >= 30:
        score_boost = 8
    else:
        score_boost = 3

    # Veto: solo si tenemos datos suficientes, no hay certificadas y score bajo
    have_lt_data = len(_cached_lt_results) > 0
    veto = (have_lt_data and len(cert) > 0 and len(supporting) == 0
            and len(supporting_fallback) == 0 and score < 70)

    # Estadísticas de la estrategia recomendada
    rec_stats = next((r for r in all_res if r.get("_name") == recommended), {})
    rec_wr    = rec_stats.get("winrate", 0)
    rec_pf    = rec_stats.get("profit_factor", 0)
    rec_label = rec_stats.get("_label", recommended)

    # Etiquetas de las estrategias certificadas de apoyo
    sup_labels = []
    for s in supporting[:3]:
        st = next((r for r in all_res if r.get("_name") == s), {})
        sup_labels.append(f"🏆 {st.get('_label', s)} ({st.get('winrate', 0):.0f}%)")
    # Si no hay certificadas, usar fallback 60d con etiqueta diferente
    if not sup_labels:
        for s in supporting_fallback[:3]:
            st = next((r for r in all_res if r.get("_name") == s), {})
            sup_labels.append(f"✅ {st.get('_label', s)} ({st.get('winrate', 0):.0f}%)")

    # Texto detallado
    if supporting:
        detail = (
            f"🏆 {len(supporting)}/{len(candidates)} estrategias CERTIFICADAS (60d+2008) "
            f"en régimen {regime}. Mejor: {rec_label} ({rec_wr:.0f}% WR, PF {rec_pf:.2f})"
        )
    elif supporting_fallback:
        detail = (
            f"✅ {len(supporting_fallback)}/{len(candidates)} ganadoras recientes (60d) "
            f"en régimen {regime} — sin certificación 2008 aún. "
            f"Mejor: {rec_label} ({rec_wr:.0f}% WR, PF {rec_pf:.2f})"
        )
    else:
        detail = (
            f"⚠️ Ninguna estrategia ganadora para régimen {regime} — señal con cautela máxima"
        )

    return {
        "recommended":      recommended,
        "rec_label":        rec_label,
        "rec_winrate":      rec_wr,
        "rec_pf":           rec_pf,
        "supporting":       supporting,           # certificadas
        "supporting_60d":   supporting_fallback,  # solo 60d
        "sup_labels":       sup_labels,
        "score_boost":      score_boost,
        "consensus_pct":    consensus_pct,
        "veto":             veto,
        "detail":           detail,
        "winners_total":    len(winners60),
        "lt_winners_total": len(_cached_lt_winners),
        "certified_total":  len(cert),
    }


# ─────────────────────────────────────────────────────────────────────────────
# PERSISTENCIA EN DB (guardar ranking para UI)
# ─────────────────────────────────────────────────────────────────────────────

def save_ranking_to_db(results: list[dict]) -> None:
    """
    Guarda el ranking de estrategias en DB.
    Incluye: resultados 60d, resultados 2008, nivel de certificación.
    """
    try:
        import db as _db
        cert    = certified_winners()
        lt_dict = {r["_name"]: r for r in _cached_lt_results}

        ranking = []
        for r in results[:17]:
            name   = r["_name"]
            lt_r   = lt_dict.get(name, {})
            is_cert = name in cert
            is_60d  = name in _cached_winners
            is_lt   = name in _cached_lt_winners

            ranking.append({
                "name":           name,
                "label":          r["_label"],
                # 60d stats
                "winrate":        r.get("winrate", 0),
                "profit_factor":  r.get("profit_factor", 0),
                "net_pips":       r.get("net_pips", 0),
                "total":          r.get("total", 0),
                "composite":      r.get("_composite", 0),
                # 2008 stats
                "lt_winrate":     lt_r.get("winrate", 0),
                "lt_profit_factor": lt_r.get("profit_factor", 0),
                "lt_total":       lt_r.get("total", 0),
                "lt_composite":   lt_r.get("_composite_lt", 0),
                # flags
                "is_winner_60d":  is_60d,
                "is_winner_lt":   is_lt,
                "is_certified":   is_cert,   # 🏆 ambos filtros
                # badge para UI
                "badge": "🏆" if is_cert else ("✅" if is_60d else "❌"),
            })

        _db.save_metric(
            name="strategy_ranking",
            value=float(len(cert)),
            context={
                "ranking":         ranking,
                "ts":              datetime.utcnow().isoformat(),
                "winners_60d":     len(_cached_winners),
                "winners_lt":      len(_cached_lt_winners),
                "certified":       len(cert),
            },
        )
        _log.info(
            "save_ranking_to_db: %d estrategias | 60d:%d | 2008:%d | cert:%d",
            len(ranking), len(_cached_winners), len(_cached_lt_winners), len(cert),
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
    """
    Refresca el caché corto plazo (síncrono) y lanza el largo plazo en background.
    Llamar al arranque del worker — puede tardar hasta ~90s por el backtest 2008.
    """
    # Corto plazo: síncrono (rápido, ~5s)
    if not _cache_valid():
        refresh_cache()
        if _cached_results:
            save_ranking_to_db(_cached_results)

    # Largo plazo: en background (puede tardar 30-90s)
    if not _lt_cache_valid():
        t = threading.Thread(
            target=_refresh_lt_and_save,
            daemon=True,
            name="lt-backtest-init",
        )
        t.start()
        _log.info("strategy_selector: backtest 2008 lanzado en background...")


def _refresh_lt_and_save() -> None:
    """Refresca caché largo plazo y guarda ranking completo en DB."""
    try:
        refresh_lt_cache()
        if _cached_results:
            save_ranking_to_db(_cached_results)
    except Exception as e:
        _log.warning("strategy_selector LT background: %s", e)

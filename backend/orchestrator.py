"""
backend/orchestrator.py — Bot orquestador que conecta todos los módulos existentes.

NO duplica ningún módulo. Usa directamente:
  - backend.multi_pair.analyze_pair()        → análisis completo por par
  - backend.strategies.run_full_backtest()   → backtest con parámetros ajustables
  - backend.strategies.get_longterm_data_2008()
  - backend.strategies.get_backtest_data()
  - backend.signals.get_dxy_yf()             → dirección DXY
  - backend.market_context.get_cot_data()   → posicionamiento institucional
  - backend.market_context.get_economic_calendar()

Principios (CLAUDE.md):
  - Fail closed: sin datos válidos → WAIT, no se opera
  - Cada decisión se loguea con todas sus razones
  - Gestión de riesgo siempre presente (SL obligatorio)
"""
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

_log = logging.getLogger(__name__)

# ── Ruta del log de decisiones ────────────────────────────────────────────────
_BASE_DIR = Path(__file__).parent.parent
DECISIONS_LOG = _BASE_DIR / "orchestrator_decisions.jsonl"

# ── Pares configurados ────────────────────────────────────────────────────────
ALL_PAIRS = ["EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "USDCAD", "NZDUSD"]


# ─────────────────────────────────────────────────────────────────────────────
# LOG DE DECISIONES
# ─────────────────────────────────────────────────────────────────────────────

def log_decision(decision: dict) -> None:
    """Appends one decision to the JSONL decision log."""
    try:
        entry = {**decision, "logged_at": datetime.now(timezone.utc).isoformat()}
        with open(DECISIONS_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception as e:
        _log.warning("log_decision error: %s", e)


def load_decisions_log(last_n: int = 200) -> list[dict]:
    """Reads last N decisions from the JSONL log, newest first."""
    if not DECISIONS_LOG.exists():
        return []
    try:
        lines = DECISIONS_LOG.read_text(encoding="utf-8").strip().splitlines()
        parsed = []
        for ln in lines:
            try:
                parsed.append(json.loads(ln))
            except Exception:
                pass
        return list(reversed(parsed[-last_n:]))
    except Exception as e:
        _log.warning("load_decisions_log error: %s", e)
        return []


# ─────────────────────────────────────────────────────────────────────────────
# ANÁLISIS DE UN PAR (usa multi_pair.analyze_pair — sin duplicar lógica)
# ─────────────────────────────────────────────────────────────────────────────

def analyze_pair_full(
    symbol: str,
    analysis_mode: str = "intraday",
    dxy_dir: str = "",
    news_list: Optional[list] = None,
) -> dict:
    """
    Full independent analysis for one FX pair.
    Delegates entirely to backend.multi_pair.analyze_pair().
    Returns enriched dict with direction, confidence, score, tp, sl, rr, vote_log.
    On any failure → returns WAIT signal (fail-closed principle).
    """
    try:
        from backend.multi_pair import analyze_pair
        result = analyze_pair(
            symbol=symbol,
            dxy_dir=dxy_dir,
            news=news_list,
            mode=analysis_mode,
        )
        result["symbol"] = symbol
        result["analysis_mode"] = analysis_mode
        result["analyzed_at"] = datetime.now(timezone.utc).isoformat()
        return result
    except Exception as e:
        _log.warning("analyze_pair_full(%s) error: %s", symbol, e)
        return {
            "symbol": symbol, "direction": "WAIT", "confidence": 0,
            "score": 0, "error": str(e),
            "analyzed_at": datetime.now(timezone.utc).isoformat(),
        }


# ─────────────────────────────────────────────────────────────────────────────
# ANÁLISIS PARALELO DE TODOS LOS PARES SELECCIONADOS
# ─────────────────────────────────────────────────────────────────────────────

def run_all_pairs_analysis(
    pairs: list[str],
    analysis_mode: str = "intraday",
    dxy_dir: str = "",
    news_list: Optional[list] = None,
    max_workers: int = 4,
    timeout_secs: int = 90,
) -> dict[str, dict]:
    """
    Runs analyze_pair_full() in parallel for all selected pairs.
    Returns {symbol: result_dict} in the order of `pairs`.
    """
    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(analyze_pair_full, sym, analysis_mode, dxy_dir, news_list): sym
            for sym in pairs
        }
        for fut in as_completed(futures, timeout=timeout_secs):
            sym = futures[fut]
            try:
                results[sym] = fut.result()
            except Exception as e:
                results[sym] = {
                    "symbol": sym, "direction": "WAIT", "confidence": 0,
                    "score": 0, "error": str(e),
                }
    # Preserve requested order
    return {sym: results.get(sym, {"symbol": sym, "direction": "WAIT", "confidence": 0, "score": 0})
            for sym in pairs}


# ─────────────────────────────────────────────────────────────────────────────
# DATOS DE SOPORTE (thin wrappers — no duplican lógica, solo llaman módulos)
# ─────────────────────────────────────────────────────────────────────────────

def fetch_dxy_direction() -> str:
    """Returns 'UP' | 'DOWN' | '' from existing DXY module."""
    try:
        from backend.signals import get_dxy_yf
        dxy = get_dxy_yf("1h")
        return dxy.get("direction", "") or dxy.get("dir", "")
    except Exception as e:
        _log.warning("fetch_dxy_direction error: %s", e)
        return ""


def fetch_news_list() -> list:
    """Returns list of news articles from existing news module."""
    try:
        from backend.signals import get_rss_news
        return get_rss_news() or []
    except Exception as e:
        _log.warning("fetch_news_list error: %s", e)
        return []


# ─────────────────────────────────────────────────────────────────────────────
# BARRIDO DE COOLDOWN — ¿cuántas ops/día según cooldown?
# ─────────────────────────────────────────────────────────────────────────────

def cooldown_sweep(
    df: pd.DataFrame,
    cooldowns: Optional[list[int]] = None,
    use_windows: bool = True,
    utc_offset: int = 2,
    news_score: float = 0.0,
    news_dir: str = "",
    news_available: bool = False,
    cot_dir: str = "",
    cot_available: bool = False,
) -> pd.DataFrame:
    """
    Runs run_adaptive_backtest() with different cooldown values on 15m data.

    Muestra la tabla: cooldown → ops/día, WR, PF, pips.
    df debe ser datos 15m (usa get_hf_data() para descargarlo).

    Si se pasan news/cot, se usan como filtros primarios (igual que en live).
    """
    from backend.strategies import run_adaptive_backtest

    if cooldowns is None:
        cooldowns = [1, 2, 3, 4, 6, 8, 10, 12, 16]

    if df.empty or len(df) < 60:
        return pd.DataFrame()

    idx = df.index
    try:
        n_days = max(1, (idx[-1].date() - idx[0].date()).days)
    except Exception:
        n_days = max(1, len(df) // (13 * 4))

    rows = []
    for cd in cooldowns:
        try:
            result = run_adaptive_backtest(
                df,
                news_score=news_score,
                news_dir=news_dir,
                news_available=news_available,
                cot_dir=cot_dir,
                cot_available=cot_available,
                cooldown=cd,
                use_windows=use_windows,
                utc_offset=utc_offset,
            )
            if result is None:
                continue
            ops_day = round(result["total"] / n_days, 1)
            rows.append({
                "Cooldown (velas 15m)": cd,
                "= cada (min)":         cd * 15,
                "Ops totales":          result["total"],
                "Ops/día":              ops_day,
                "Win Rate %":           result["winrate"],
                "Profit Factor":        result["profit_factor"],
                "Pips netos":           result["net_pips"],
                "Max DD %":             result["max_dd"],
                "Régimen % trending":   round(
                    100 * sum(v for k, v in result.get("regime_dist", {}).items()
                              if "trending" in k) / max(result["total"], 1), 0
                ),
            })
        except Exception as e:
            _log.warning("cooldown_sweep cd=%d error: %s", cd, e)

    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)


# ─────────────────────────────────────────────────────────────────────────────
# BACKTEST MULTI-PERIODO
# ─────────────────────────────────────────────────────────────────────────────

# Descripción de cada periodo para la UI
PERIOD_NOTES = {
    "2008 — Crisis financiera": (
        "Mercado extremadamente volátil. EUR/USD cayó de 1.60 a 1.25 en meses. "
        "Solo datos diarios disponibles. Sin noticias en tiempo real — análisis puramente técnico."
    ),
    "2020 — COVID crash": (
        "Flash crash de marzo 2020, seguido de recuperación histórica. "
        "Alta volatilidad. Solo datos diarios. Sin noticias históricas."
    ),
    "2022 — Subidas Fed agresivas": (
        "EUR/USD rompió la paridad. Año de tendencia bajista sostenida. "
        "Solo datos diarios."
    ),
    "Último año (1h intraday)": (
        "Datos horarios reales ~1 año. Máxima granularidad. "
        "Sin noticias históricas — solo técnico."
    ),
}


def backtest_multiperiod(live_news_score: float = 0.0,
                         live_news_dir: str = "",
                         live_cot_dir: str = "") -> dict[str, dict]:
    """
    Backtest multi-periodo usando run_adaptive_backtest() — estrategia variable.

    Señal primaria: NOTICIAS (si disponibles) + COT / posiciones reales
    Señal secundaria: régimen de mercado → estrategia técnica adaptativa

    Periodos históricos (2008/2020/2022): datos diarios, sin noticias reales →
      usa COT proxy (posicionamiento implícito en precio) + régimen variable
    Último año (1h): sin noticias históricas → COT proxy + régimen
    Último mes (15m): PUEDE usar noticias ACTUALES si se pasan como parámetro
      → muestra diferencia de rendimiento cuando las noticias están disponibles

    Returns {period_name: result_dict | error_dict}
    """
    from backend.strategies import (
        run_adaptive_backtest,
        get_longterm_data_2008, get_backtest_data, get_hf_data,
    )

    results: dict[str, dict] = {}

    # ── Datos diarios históricos ──────────────────────────────────────────────
    _log.info("Descargando datos EUR/USD diarios desde 2008...")
    df_all_daily = pd.DataFrame()
    try:
        df_all_daily = get_longterm_data_2008()
        _log.info("Long-term data: %d barras diarias", len(df_all_daily))
    except Exception as e:
        _log.warning("get_longterm_data_2008 error: %s", e)

    daily_periods = {
        "2008 — Crisis financiera":     ("2008-01-01", "2009-12-31"),
        "2020 — COVID crash":           ("2020-01-01", "2021-06-30"),
        "2022 — Subidas Fed agresivas": ("2022-01-01", "2022-12-31"),
    }

    for name, (s, e) in daily_periods.items():
        if df_all_daily.empty:
            results[name] = {"error": "No se pudieron descargar datos diarios históricos.",
                             "note": PERIOD_NOTES.get(name, "")}
            continue
        try:
            df_p = df_all_daily[(df_all_daily.index >= s) & (df_all_daily.index <= e)].copy()
            if df_p.empty or len(df_p) < 60:
                results[name] = {"error": f"Datos insuficientes ({len(df_p)} barras).",
                                 "note": PERIOD_NOTES.get(name, "")}
                continue

            # Sin noticias históricas, sin COT histórico real →
            # run_adaptive_backtest usará COT proxy (precio vs EMA50/EMA200)
            r = run_adaptive_backtest(
                df_p,
                news_available=False,
                cot_available=False,
                cooldown=2,
                use_windows=False,
                utc_offset=0,
            )
            if r is None:
                results[name] = {"error": "Backtest retornó None.", "note": PERIOD_NOTES.get(name, "")}
                continue

            r["bars"]      = len(df_p)
            r["note"]      = PERIOD_NOTES.get(name, "")
            r["tf"]        = "1d diario"
            r["signals"]   = "Régimen variable + COT proxy (sin noticias históricas)"
            results[name]  = r

        except Exception as ex:
            results[name] = {"error": str(ex), "note": PERIOD_NOTES.get(name, "")}

    # ── Último año con datos 1h ───────────────────────────────────────────────
    name_1h = "Último año (1h)"
    try:
        _log.info("Descargando datos EUR/USD 1h...")
        df_1h = get_backtest_data("1h")
        if df_1h.empty or len(df_1h) < 100:
            results[name_1h] = {"error": f"Datos 1h insuficientes ({len(df_1h)} barras)."}
        else:
            r = run_adaptive_backtest(
                df_1h,
                news_available=False,
                cot_available=False,
                cooldown=3,
                use_windows=True,
                utc_offset=2,
            )
            if r is None:
                results[name_1h] = {"error": "Backtest 1h retornó None."}
            else:
                r["bars"]    = len(df_1h)
                r["note"]    = PERIOD_NOTES.get(name_1h, "~1 año de datos horarios reales.")
                r["tf"]      = "1h intraday"
                r["signals"] = "Régimen variable + COT proxy (sin noticias históricas)"
                results[name_1h] = r
    except Exception as ex:
        results[name_1h] = {"error": str(ex)}

    # ── Último mes con datos 15m — CON noticias si están disponibles ──────────
    name_15m_base = "Último mes (15m) — sin noticias"
    name_15m_news = "Último mes (15m) — CON noticias actuales"
    try:
        _log.info("Descargando datos EUR/USD 15m...")
        df_15m = get_hf_data("EURUSD=X", days=55)

        if df_15m.empty or len(df_15m) < 100:
            results[name_15m_base] = {"error": f"Datos 15m insuficientes ({len(df_15m)} barras)."}
        else:
            # Versión sin noticias (base de comparación)
            r_base = run_adaptive_backtest(
                df_15m,
                news_available=False,
                cot_available=False,
                cooldown=2,
                use_windows=True,
                utc_offset=2,
            )
            if r_base:
                r_base["bars"]    = len(df_15m)
                r_base["note"]    = "Solo régimen + COT proxy. Sin noticias."
                r_base["tf"]      = "15m intraday"
                r_base["signals"] = "Régimen variable + COT proxy"
                results[name_15m_base] = r_base

            # Versión CON noticias actuales (si se pasaron)
            if live_news_dir and live_news_dir != "NEUTRAL":
                r_news = run_adaptive_backtest(
                    df_15m,
                    news_score=live_news_score,
                    news_dir=live_news_dir,
                    news_available=True,
                    cot_dir=live_cot_dir,
                    cot_available=bool(live_cot_dir),
                    cooldown=2,
                    use_windows=True,
                    utc_offset=2,
                )
                if r_news:
                    r_news["bars"]    = len(df_15m)
                    r_news["note"]    = (
                        f"Noticias: {live_news_dir} (score {live_news_score:+.2f}) | "
                        f"COT: {live_cot_dir or 'N/A'}. "
                        "Nota: noticias ACTUALES aplicadas a barras históricas — "
                        "no refleja las noticias de cada momento histórico."
                    )
                    r_news["tf"]      = "15m intraday"
                    r_news["signals"] = f"Régimen + Noticias ({live_news_dir}) + COT ({live_cot_dir or 'N/A'})"
                    results[name_15m_news] = r_news
            else:
                results[name_15m_news] = {
                    "error": "Noticias no disponibles en este momento. Pulsa 'Analizar todos los pares' primero.",
                    "note": "El orquestador recoge noticias en tiempo real antes de cada análisis.",
                }

    except Exception as ex:
        results[name_15m_base] = {"error": str(ex)}

    # ── Proyección multi-par (7 pares × ops/día del 15m base) ────────────────
    # Si el backtest 15m base tiene resultados, calculamos la proyección total
    base_15m = results.get(name_15m_base, {})
    if "ops_per_day" in base_15m and not base_15m.get("error"):
        ops_1 = float(base_15m["ops_per_day"])
        wr_1  = float(base_15m["winrate"])
        pf_1  = float(base_15m["profit_factor"])
        for n_pairs in [3, 5, 7]:
            key = f"Proyección {n_pairs} pares × 15m"
            results[key] = {
                "total":         "—",
                "wins":          "—",
                "losses":        "—",
                "winrate":       wr_1,
                "profit_factor": pf_1,
                "net_pips":      "—",
                "max_dd":        base_15m.get("max_dd", "—"),
                "ops_per_day":   round(ops_1 * n_pairs, 1),
                "n_days":        base_15m.get("n_days", "—"),
                "bars":          "—",
                "tf":            f"15m × {n_pairs} pares",
                "signals":       "Régimen + EMA21 Bounce × N pares",
                "note":          (
                    f"Estimación: {ops_1:.1f} ops/día/par × {n_pairs} pares = "
                    f"{ops_1*n_pairs:.1f} ops/día total del sistema. "
                    f"WR y PF = promedio del par base (EURUSD). "
                    f"Pares con menos liquidez pueden tener WR ligeramente diferente."
                ),
                "is_projection": True,
            }

    # ── Aprendizaje walk-forward sobre el periodo con más operaciones (15m) ───
    # Mide, SIN look-ahead, cuánto mejora el sistema al aprender de su propio
    # pasado. Clave con prefijo '_' para que la tabla la ignore.
    try:
        from backend.learning import walkforward_learning
        _wf_trades = base_15m.get("trades", []) if isinstance(base_15m, dict) else []
        if _wf_trades:
            results["_walkforward"] = walkforward_learning(_wf_trades, warmup=20)
    except Exception as _wfe:
        _log.warning("walkforward_learning: %s", _wfe)

    return results

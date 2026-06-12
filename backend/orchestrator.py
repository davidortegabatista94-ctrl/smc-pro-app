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
) -> pd.DataFrame:
    """
    Runs run_hf_backtest() (15m pullback strategy) with different cooldown values.
    Shows the ops/day vs quality tradeoff clearly.

    df should be 15m data (use get_hf_data() to download it).
    Cooldown = number of 15m bars to skip after each entry.
    cooldown=1 → ~1 op each 15min (aggressive)
    cooldown=4 → ~1 op each hour (balanced)
    cooldown=8 → ~1 op each 2h (conservative)

    Returns DataFrame with ops/day, winrate, profit_factor per cooldown.
    """
    from backend.strategies import run_hf_backtest

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
            result = run_hf_backtest(
                df, cooldown=cd, use_windows=use_windows, utc_offset=utc_offset
            )
            if result is None:
                continue
            rows.append({
                "Cooldown (velas 15m)": cd,
                "= cada (min)":         cd * 15,
                "Ops totales":          result["total"],
                "Ops/día":              round(result["total"] / n_days, 1),
                "Win Rate %":           result["winrate"],
                "Profit Factor":        result["profit_factor"],
                "Pips netos":           result["net_pips"],
                "Max DD %":             result["max_dd"],
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


def backtest_multiperiod() -> dict[str, dict]:
    """
    Backtest multi-periodo con dos estrategias:
    - Periodos históricos (2008/2020/2022): datos diarios + run_full_backtest() v2
      (SL escalado por ATR sin cap de pips + filtro EMA200)
    - Último mes (15m): run_hf_backtest() — EMA21 pullback, objetivo 30 ops/día

    Nota honesta: sin spread/slippage real. Resultados indicativos.
    Los 30 ops/día son un objetivo para datos 15m intraday.
    En periodos históricos solo hay datos diarios → ops/día siempre < 1.

    Returns {period_name: result_dict | error_dict}
    """
    from backend.strategies import (
        run_full_backtest, run_hf_backtest,
        get_longterm_data_2008, get_backtest_data, get_hf_data,
    )

    results: dict[str, dict] = {}

    # ── Datos diarios históricos (descarga única) ─────────────────────────────
    _log.info("Descargando datos EUR/USD diarios desde 2008...")
    df_all_daily = pd.DataFrame()
    try:
        df_all_daily = get_longterm_data_2008()
        _log.info("Long-term data: %d barras diarias", len(df_all_daily))
    except Exception as e:
        _log.warning("get_longterm_data_2008 error: %s", e)

    daily_periods = {
        "2008 — Crisis financiera":    ("2008-01-01", "2009-12-31"),
        "2020 — COVID crash":          ("2020-01-01", "2021-06-30"),
        "2022 — Subidas Fed agresivas":("2022-01-01", "2022-12-31"),
    }

    for name, (s, e) in daily_periods.items():
        if df_all_daily.empty:
            results[name] = {"error": "No se pudieron descargar datos diarios históricos.",
                             "note": PERIOD_NOTES.get(name, "")}
            continue
        try:
            df_p = df_all_daily[(df_all_daily.index >= s) & (df_all_daily.index <= e)].copy()
            if df_p.empty or len(df_p) < 60:
                results[name] = {"error": f"Datos insuficientes ({len(df_p)} barras para {s}→{e}).",
                                 "note": PERIOD_NOTES.get(name, "")}
                continue

            # v2: SL sin cap fijo + filtro EMA200 + cooldown=2 días
            r = run_full_backtest(df_p, use_windows=False, utc_offset=0, cooldown=2)
            if r is None:
                results[name] = {"error": "Backtest retornó None.", "note": PERIOD_NOTES.get(name, "")}
                continue

            idx    = df_p.index
            n_days = max(1, (idx[-1].date() - idx[0].date()).days) if hasattr(idx[0], "date") else max(1, len(df_p))
            r["ops_per_day"] = round(r["total"] / n_days, 2)
            r["n_days"]      = n_days
            r["bars"]        = len(df_p)
            r["note"]        = PERIOD_NOTES.get(name, "")
            r["tf"]          = "1d diario — estrategia EMA+EMA200+MACD"
            r["freq_note"]   = "Datos diarios: max ~1 op/día. Los 30 ops/día requieren datos 15m (solo disponibles últimos 60 días)"
            results[name]    = r

        except Exception as ex:
            results[name] = {"error": str(ex), "note": PERIOD_NOTES.get(name, "")}

    # ── Último año con datos 1h ───────────────────────────────────────────────
    name_1h = "Último año (1h intraday)"
    try:
        _log.info("Descargando datos EUR/USD 1h (último año)...")
        df_1h = get_backtest_data("1h")
        if df_1h.empty or len(df_1h) < 100:
            results[name_1h] = {"error": f"Datos 1h insuficientes ({len(df_1h)} barras).",
                                "note": PERIOD_NOTES.get(name_1h, "")}
        else:
            r = run_full_backtest(df_1h, use_windows=True, utc_offset=2, cooldown=4)
            if r is None:
                results[name_1h] = {"error": "Backtest 1h retornó None.", "note": PERIOD_NOTES.get(name_1h, "")}
            else:
                idx    = df_1h.index
                n_days = max(1, (idx[-1].date() - idx[0].date()).days) if hasattr(idx[0], "date") else max(1, len(df_1h) // 24)
                r["ops_per_day"] = round(r["total"] / n_days, 2)
                r["n_days"]      = n_days
                r["bars"]        = len(df_1h)
                r["note"]        = PERIOD_NOTES.get(name_1h, "")
                r["tf"]          = "1h intraday — EMA+EMA200+MACD"
                r["freq_note"]   = "1h: objetivo ~1-3 ops/día con calidad alta"
                results[name_1h] = r
    except Exception as ex:
        results[name_1h] = {"error": str(ex), "note": PERIOD_NOTES.get(name_1h, "")}

    # ── Último mes con datos 15m (estrategia HF pullback) ────────────────────
    name_hf = "Último mes — 15m HF Pullback"
    try:
        _log.info("Descargando datos EUR/USD 15m (último mes)...")
        df_15m = get_hf_data("EURUSD=X", days=55)
        if df_15m.empty or len(df_15m) < 100:
            results[name_hf] = {"error": f"Datos 15m insuficientes ({len(df_15m)} barras).",
                                "note": "Estrategia EMA21 Pullback alta frecuencia"}
        else:
            r = run_hf_backtest(df_15m, cooldown=2, use_windows=True, utc_offset=2)
            if r is None:
                results[name_hf] = {"error": "HF backtest retornó None.",
                                    "note": "Estrategia EMA21 Pullback alta frecuencia"}
            else:
                r["note"]      = "EMA21 Pullback en 15m | SL=1×ATR | TP=3×SL | Cooldown=2 barras (30 min)"
                r["tf"]        = "15m intraday — EMA21 Pullback"
                r["freq_note"] = "Objetivo ~15-25 ops/día por par | ~30 ops/día cruzando 2 pares"
                results[name_hf] = r
    except Exception as ex:
        results[name_hf] = {"error": str(ex), "note": "EMA21 Pullback HF"}

    return results

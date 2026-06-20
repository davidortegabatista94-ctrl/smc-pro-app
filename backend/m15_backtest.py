"""
backend/m15_backtest.py — Harness de validación sobre M15 real (18 años).

POR QUÉ EXISTE: el backtest de la app usa yfinance, que solo da 60 días de 15m.
Eso es muestra insuficiente para confiar en nada (CLAUDE.md exige 200+ ops y
out-of-sample). La carpeta /api trajo un tesoro: M15 real desde 2007-2008 para
múltiples pares. Este módulo lo conecta como FUENTE DE VALIDACIÓN seria.

Qué hace: carga el M15 cacheado (parquet) y valida una táctica con:
  - Costes reales (slippage + spread)
  - Partición OUT-OF-SAMPLE (entreno vs test nunca tocado)
  - Métricas honestas: WR, expectancy, retorno anual capital-weighted, DD

NOTA DE DESPLIEGUE: los parquet (cientos de MB) NO se suben a git ni a Railway.
Este harness es para VALIDACIÓN LOCAL. El resultado (qué tácticas sobreviven)
es lo que se lleva a producción, no los datos.
"""
from __future__ import annotations

import os
import numpy as np
import pandas as pd

# Ruta por defecto al cache M15 de la carpeta /api (ajustable por env)
DEFAULT_CACHE = os.environ.get(
    "M15_CACHE_DIR",
    r"c:\Users\david\Downloads\api\api\data_cache",
)
DEFAULT_PAIRS = ["EURUSD", "GBPUSD", "USDJPY", "XAUUSD", "AUDUSD"]


def load_m15(symbol: str, date_from: str = "2008-01-01",
             date_to: str = "2026-06-10", cache_dir: str | None = None) -> pd.DataFrame | None:
    """Carga M15 desde el parquet cacheado. Devuelve df con índice UTC o None."""
    cache_dir = cache_dir or DEFAULT_CACHE
    fname = f"{symbol}_{date_from}_{date_to}_M15.parquet"
    fpath = os.path.join(cache_dir, fname)
    if not os.path.exists(fpath):
        return None
    try:
        df = pd.read_parquet(fpath)
        df.index = pd.to_datetime(df.index, utc=True)
        return df
    except Exception:
        return None


def _portfolio_yearly(all_trades: dict, balance0: float = 10000.0) -> list[float]:
    """Retornos anuales capital-weighted (pnl_año / capital_total_inicio_año)."""
    by = {s: {} for s in all_trades}
    for s, trs in all_trades.items():
        for t in trs:
            by[s].setdefault(int(str(t["fecha"])[:4]), []).append(t)
    bal = {s: balance0 for s in all_trades}
    out = []
    for yr in range(2008, 2027):
        start = sum(bal.values())
        pnl = 0.0; n = 0
        for s in all_trades:
            for t in by[s].get(yr, []):
                bal[s] += t["pnl"]; pnl += t["pnl"]; n += 1
        if n == 0:
            continue
        out.append((yr, round(pnl / start * 100, 1), n))
    return out


def validate_ict(pairs: list[str] | None = None, risk_pct: float = 1.0,
                 slip_mult: float = 1.0, cache_dir: str | None = None,
                 oos_split: str = "2019-01-01") -> dict:
    """
    Valida la táctica London-Sweep+FVG sobre M15 real, con costes y OOS.
    Devuelve métricas por par + cartera in-sample / out-of-sample.
    """
    from backend.ict_strategy import backtest_ict
    pairs = pairs or DEFAULT_PAIRS

    def run(window) -> dict:
        allt = {}
        per_pair = {}
        for sym in pairs:
            df = load_m15(sym, cache_dir=cache_dir)
            if df is None:
                continue
            if window == "is":
                df = df[df.index < oos_split]
            elif window == "oos":
                df = df[df.index >= oos_split]
            r = backtest_ict(df, sym, risk_pct=risk_pct, balance=10000, slip_mult=slip_mult)
            if r:
                allt[sym] = r["trades"]
                per_pair[sym] = {k: r[k] for k in
                                 ("n", "win_rate", "expectancy_r", "return_pct", "max_dd_pct")}
        yearly = _portfolio_yearly(allt)
        rets = [y[1] for y in yearly]
        summary = {
            "per_pair": per_pair,
            "years": yearly,
            "avg_annual": round(float(np.mean(rets)), 1) if rets else 0.0,
            "median_annual": round(float(np.median(rets)), 1) if rets else 0.0,
            "worst_year": round(min(rets), 1) if rets else 0.0,
            "positive_years": f"{sum(1 for r in rets if r > 0)}/{len(rets)}" if rets else "0/0",
        }
        return summary

    return {
        "full": run("full"),
        "in_sample": run("is"),
        "out_sample": run("oos"),
        "config": {"risk_pct": risk_pct, "slip_mult": slip_mult, "oos_split": oos_split},
    }


# ── CLI de validación ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    print("VALIDACIÓN London-Sweep+FVG sobre M15 real (18 años) — con costes + OOS")
    print("=" * 72)
    for slip, label in [(0.0, "SIN coste (bruto)"), (1.0, "CON slippage realista")]:
        res = validate_ict(slip_mult=slip)
        print(f"\n── {label} ──")
        for win in ("full", "in_sample", "out_sample"):
            s = res[win]
            print(f"  {win:<11}: media={s['avg_annual']:+.1f}%/año  "
                  f"mediana={s['median_annual']:+.1f}%  peor={s['worst_year']:+.1f}%  "
                  f"pos={s['positive_years']}")
    print("\nVeredicto: si 'CON slippage' no es positivo y consistente IS/OOS, NO hay edge.")

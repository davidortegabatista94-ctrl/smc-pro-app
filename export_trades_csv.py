"""
export_trades_csv.py — Exporta TODOS los trades que la estrategia del bot produce
sobre los 18 años de M15 real (carpeta /api), a un CSV para analizar.

Fuentes de trades:
  - Estrategia adaptativa (run_adaptive_backtest): tácticas pullback_trend y
    meanrev_range, con régimen/sesión/resultado, NETO de costes.
  - Táctica ICT (backtest_ict): london_sweep_fvg, con sesión/hora/resultado.
  - Trades en vivo (paper_trades del store), si existen.

Cada fila = un trade con: pair, fecha, source, tactic, direction, regime, session,
outcome (TP/SL/MKT), r_multiple, pips, sl_pips, pnl.
"""
import os, sys, warnings
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import glob
import pandas as pd

CACHE = r"c:\Users\david\Downloads\api\api\data_cache"
OUT   = r"c:\Users\david\Downloads\bot_trades_analisis.csv"


def _pairs_full_range():
    """Pares que tienen el parquet de rango completo 2008→2026."""
    out = []
    for f in glob.glob(os.path.join(CACHE, "*_2008-01-01_2026-06-10_M15.parquet")):
        out.append(os.path.basename(f).split("_")[0])
    return sorted(set(out))


def _load(sym):
    f = os.path.join(CACHE, f"{sym}_2008-01-01_2026-06-10_M15.parquet")
    df = pd.read_parquet(f)
    df.index = pd.to_datetime(df.index, utc=True)
    return df


def run():
    from backend.strategies import run_adaptive_backtest
    from backend.ict_strategy import backtest_ict

    pairs = _pairs_full_range()
    print(f"Pares con 18 años M15: {pairs}")
    rows = []

    for sym in pairs:
        df = _load(sym)
        # ── Estrategia adaptativa (pullback_trend / meanrev_range) ──
        try:
            dfa = df.rename(columns={"O": "Open", "H": "High", "L": "Low",
                                     "C": "Close", "V": "Volume"})
            res = run_adaptive_backtest(dfa, news_available=False, cot_available=False,
                                        cooldown=2, use_windows=True, utc_offset=2, cost_pips=2.0)
            trades = (res or {}).get("trades", []) if res else []
            for t in trades:
                if t.get("outcome") == "OPEN":
                    continue
                ft = t.get("feats", {}) or {}
                sl_p = t.get("sl_pips")
                pips = t.get("pips")
                r = (pips / sl_p) if (sl_p and pips is not None and sl_p > 0) else None
                rows.append({
                    "pair": sym, "fecha": t.get("time"), "source": "adaptativa",
                    "tactic": ft.get("tactic", ""), "direction": ft.get("dir", t.get("dir")),
                    "regime": ft.get("regime", ""), "session": ft.get("session", ""),
                    "hour": "", "outcome": t.get("outcome"),
                    "r_multiple": round(r, 3) if r is not None else "",
                    "pips": t.get("pips"), "sl_pips": sl_p, "pnl": t.get("pnl"),
                })
            print(f"  {sym} adaptativa: {sum(1 for t in trades if t.get('outcome')!='OPEN')} trades")
        except Exception as e:
            print(f"  {sym} adaptativa ERROR: {e}")

        # ── Táctica ICT (london_sweep_fvg) ──
        try:
            r_ict = backtest_ict(df, sym, risk_pct=1.0, balance=10000, slip_mult=1.0)
            for t in (r_ict or {}).get("trades", []):
                ft = t.get("feats", {}) or {}
                rows.append({
                    "pair": sym, "fecha": t.get("fecha"), "source": "ict",
                    "tactic": "london_sweep_fvg", "direction": ft.get("dir", t.get("dir")),
                    "regime": "", "session": ft.get("session", ""),
                    "hour": ft.get("hour", ""), "outcome": t.get("outcome"),
                    "r_multiple": round(t.get("r", 0), 3), "pips": "",
                    "sl_pips": "", "pnl": t.get("pnl"),
                })
            print(f"  {sym} ICT: {len((r_ict or {}).get('trades', []))} trades")
        except Exception as e:
            print(f"  {sym} ICT ERROR: {e}")

    # ── Trades EN VIVO (si hay) ──
    try:
        from backend.store import trades_all
        for t in trades_all():
            ft = t.get("features", {}) or {}
            rows.append({
                "pair": t.get("symbol"), "fecha": t.get("opened_at"), "source": "vivo",
                "tactic": ft.get("tactic", ""), "direction": t.get("direction"),
                "regime": "", "session": ft.get("session", ""), "hour": "",
                "outcome": t.get("outcome") or t.get("status"),
                "r_multiple": t.get("r_multiple", ""), "pips": "", "sl_pips": "",
                "pnl": "",
            })
    except Exception:
        pass

    df_out = pd.DataFrame(rows)
    df_out.to_csv(OUT, index=False, encoding="utf-8-sig")
    print(f"\n✓ CSV escrito: {OUT}")
    print(f"  TOTAL trades: {len(df_out)}")
    if len(df_out):
        print("\n  Por táctica × resultado:")
        print(df_out.groupby(["tactic", "outcome"]).size().to_string())
        print("\n  Win rate por táctica (TP / (TP+SL)):")
        for tac in df_out["tactic"].unique():
            sub = df_out[df_out["tactic"] == tac]
            tp = (sub["outcome"] == "TP").sum(); sl = (sub["outcome"] == "SL").sum()
            wr = tp / (tp + sl) * 100 if (tp + sl) else 0
            print(f"    {tac:<20} {len(sub):>6} trades  WR={wr:.1f}%")


if __name__ == "__main__":
    run()

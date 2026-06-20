"""
backend/ict_strategy.py — Táctica London Sweep + FVG Fill (ICT), bien hecha.

ORIGEN: portada (corregida) de una estrategia de la carpeta /api que afirmaba
~76%/año. Auditoría demostró que ese número era ILUSIÓN (look-ahead + fills
perfectos + sin slippage; drawdown de -1% imposible). Esta versión arregla los
dos fallos y añade costes:
  1. La entrada ESPERA a que el precio vuelva al FVG en una vela POSTERIOR
     (el original entraba en la misma vela que formaba el gap, a su mínimo exacto).
  2. Gestión intrabar SL-PRIMERO (el original asumía TP si una vela tocaba ambos).
  3. Slippage + spread restados a cada operación.

HIPÓTESIS ECONÓMICA (el porqué, no la técnica):
  En la apertura de Nueva York, el precio suele BARRER la liquidez acumulada en
  los extremos del rango de Londres (stops de minoristas) y luego REVERTIR. Ese
  barrido deja un Fair Value Gap (desequilibrio de órdenes) que el precio tiende
  a rellenar antes de continuar. Entrar en el relleno del FVG, en la dirección de
  la reversión y alineado con la tendencia macro (D1 EMA50), captura ese repricing.

CÓMO SE VALIDA: backtest con costes sobre M15 real (18 años) + walk-forward. Si
no sobrevive costes, NO es edge. La táctica queda gateada por el motor de
selección como cualquier otra — la evidencia decide, no la fe.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# Ventanas horarias (UTC)
LON_START, LON_END = 8, 13     # 08:00–12:59 rango de Londres
NY_START,  NY_END  = 13, 17    # 13:00–16:59 ventana de barrido NY

RR_DEFAULT = 2.0

# Parámetros por par: (spread, min_sweep, min_fvg) en precio absoluto.
# Conservadores. min_sweep/min_fvg son umbrales de microestructura (2-3 params).
PAIR_PARAMS = {
    "EURUSD": (0.00010, 0.0003, 0.0002),
    "GBPUSD": (0.00015, 0.0004, 0.0002),
    "USDJPY": (0.015,   0.05,   0.03),
    "AUDUSD": (0.00010, 0.0003, 0.0002),
    "USDCHF": (0.00012, 0.0003, 0.0002),
    "USDCAD": (0.00012, 0.0003, 0.0002),
    "NZDUSD": (0.00012, 0.0003, 0.0002),
    "XAUUSD": (0.30,    1.00,   0.50),
}


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    """Acepta formato app (Open/High/Low/Close) o api (O/H/L/C). Devuelve H/L/C."""
    cols = {c.lower(): c for c in df.columns}
    def pick(*names):
        for n in names:
            if n in df.columns:        return df[n]
            if n.lower() in cols:      return df[cols[n.lower()]]
        return None
    h = pick("High", "H"); l = pick("Low", "L"); c = pick("Close", "C")
    if h is None or l is None or c is None:
        raise ValueError("df sin columnas OHLC reconocibles")
    out = pd.DataFrame({"H": h, "L": l, "C": c})
    out.index = pd.to_datetime(df.index, utc=True)
    return out.dropna()


def backtest_ict(df_m15: pd.DataFrame, symbol: str,
                 risk_pct: float = 1.0, balance: float = 10000.0,
                 rr: float = RR_DEFAULT, slip_mult: float = 1.0) -> dict | None:
    """
    Backtest HONESTO de London Sweep + FVG sobre M15.
      - Entrada: espera el fill del FVG en una vela posterior a su formación.
      - Salida:  SL-primero intrabar (conservador). Cierre a mercado al final de NY.
      - Coste:   spread + slippage (slip_mult × spread por lado) restado en R.
    Devuelve dict con métricas + lista de trades (cada uno con feats para el motor).
    """
    spread, min_sweep, min_fvg = PAIR_PARAMS.get(symbol, (0.0001, 0.0003, 0.0002))
    slip = spread * slip_mult

    df = _normalize(df_m15)
    if len(df) < 500:
        return None

    c = df["C"]
    d1 = (c.resample("1D").last().dropna()
            .ewm(span=50, adjust=False).mean()
            .reindex(df.index, method="ffill").ffill().bfill())

    H = df["H"].values; L = df["L"].values; C = df["C"].values
    D1 = d1.values
    hours = df.index.hour.values
    wd    = df.index.dayofweek.values
    dates = np.array([str(t.date()) for t in df.index])

    day_marker = df.index.floor("D").values
    changes = np.where(np.diff(day_marker.astype("int64")) > 0)[0] + 1
    starts  = np.concatenate([[0], changes])
    ends    = np.concatenate([changes, [len(df)]])

    trades = []
    bal = balance

    for d_s, d_e in zip(starts, ends):
        if wd[d_s] >= 5:
            continue
        sh = hours[d_s:d_e]; Cd = C[d_s:d_e]; Hd = H[d_s:d_e]; Ld = L[d_s:d_e]
        dd = dates[d_s:d_e]; D1d = D1[d_s:d_e]

        lon = (sh >= LON_START) & (sh < LON_END)
        if lon.sum() < 4:
            continue
        lon_hi = Hd[lon].max(); lon_lo = Ld[lon].min()
        if lon_hi <= lon_lo:
            continue

        ny = (sh >= NY_START) & (sh < NY_END)
        nyi = np.where(ny)[0]
        if len(nyi) < 4:
            continue

        state = 0; sw = 0; swk = -1
        fe = fsl = ftp = 0.0; hour_entry = 0

        for ki in range(len(nyi)):
            k = nyi[ki]; bH = Hd[k]; bL = Ld[k]; bC = Cd[k]; d1e = D1d[k]

            if state == 0:
                if bL < (lon_lo - min_sweep) and bC >= lon_lo and bC > d1e:
                    sw = 1; swk = ki; state = 1
                elif bH > (lon_hi + min_sweep) and bC <= lon_hi and bC < d1e:
                    sw = -1; swk = ki; state = 1

            elif state == 1 and ki >= swk + 2:
                kA = nyi[ki - 2]
                AH = Hd[kA]; AL = Ld[kA]; CbH = Hd[k]; CbL = Ld[k]
                if sw == 1 and (CbL - AH) >= min_fvg:
                    fe = CbL; fsl = AH - spread
                    if fe - fsl > 0:
                        ftp = fe + rr * (fe - fsl); state = 2; hour_entry = sh[k]
                elif sw == -1 and (AL - CbH) >= min_fvg:
                    fe = CbH; fsl = AL + spread
                    if fsl - fe > 0:
                        ftp = fe - rr * (fsl - fe); state = 2; hour_entry = sh[k]

            elif state == 2:
                # Esperar el fill en una vela POSTERIOR a la formación del FVG
                filled = (sw == 1 and bL <= fe) or (sw == -1 and bH >= fe)
                if not filled:
                    continue
                # Gestión desde la vela de fill: SL PRIMERO (conservador)
                res = None; r = None
                for kj in range(ki, len(nyi)):
                    kk = nyi[kj]; hH = Hd[kk]; hL = Ld[kk]
                    if sw == 1:
                        if hL <= fsl: res, r = "SL", -1.0; break
                        if hH >= ftp: res, r = "TP", rr;  break
                    else:
                        if hH >= fsl: res, r = "SL", -1.0; break
                        if hL <= ftp: res, r = "TP", rr;  break
                if res is None:
                    lc = Cd[nyi[-1]]; sld = abs(fe - fsl)
                    r = (max(min((lc - fe) / sld if sw == 1 else (fe - lc) / sld, rr), -1.0)
                         if sld > 0 else -1.0)
                    res = "MKT"
                # Coste: slippage de ida y vuelta en múltiplos de R
                sld = abs(fe - fsl)
                r -= (2 * slip) / sld if sld > 0 else 0.0
                pnl = (bal * risk_pct / 100) * r
                bal += pnl
                trades.append({
                    "fecha": dd[k], "dir": "BUY" if sw == 1 else "SELL",
                    "outcome": "TP" if res == "TP" else ("SL" if res == "SL" else "MKT"),
                    "r": round(r, 3), "pnl": round(pnl, 2),
                    "feats": {"tactic": "london_sweep_fvg",
                              "dir": "LONG" if sw == 1 else "SHORT",
                              "session": "overlap", "hour": int(hour_entry)},
                })
                break

    if not trades:
        return None

    rs = [t["r"] for t in trades]
    wins = sum(1 for t in trades if t["r"] > 0)
    n = len(trades)
    eq = np.cumsum([balance] + [t["pnl"] for t in trades])
    peak = np.maximum.accumulate(eq)
    dd = ((eq - peak) / peak * 100).min()
    return {
        "symbol": symbol, "trades": trades, "n": n,
        "win_rate": round(wins / n * 100, 1),
        "expectancy_r": round(float(np.mean(rs)), 3),
        "total_r": round(float(np.sum(rs)), 1),
        "balance_final": round(bal, 0),
        "return_pct": round((bal - balance) / balance * 100, 1),
        "max_dd_pct": round(float(dd), 1),
    }


# ── Detector EN VIVO (para el orquestador / worker) ──────────────────────────

def detect_live_setup(df_m15: pd.DataFrame, symbol: str,
                      rr: float = RR_DEFAULT) -> dict | None:
    """
    Mira el día ACTUAL (UTC): ¿hay un setup London-Sweep+FVG activo ahora mismo?
    Devuelve {direction, entry, sl, tp, reason} o None.
    Pensado para datos 15m en vivo (yfinance da 60 días, suficiente para hoy).
    """
    try:
        df = _normalize(df_m15)
    except Exception:
        return None
    if df.empty:
        return None

    today = df.index[-1].date()
    day = df[df.index.date == today]
    if len(day) < 8:
        return None

    H = day["H"].values; L = day["L"].values; C = day["C"].values
    hours = day.index.hour.values

    spread, min_sweep, min_fvg = PAIR_PARAMS.get(symbol, (0.0001, 0.0003, 0.0002))

    lon = (hours >= LON_START) & (hours < LON_END)
    if lon.sum() < 4:
        return None
    lon_hi = H[lon].max(); lon_lo = L[lon].min()

    ny = (hours >= NY_START) & (hours < NY_END)
    nyi = np.where(ny)[0]
    if len(nyi) < 3:
        return None

    # Recorrer la sesión NY de hoy buscando sweep → FVG (mismo orden causal)
    state = 0; sw = 0; swk = -1
    for ki in range(len(nyi)):
        k = nyi[ki]; bH = H[k]; bL = L[k]; bC = C[k]
        if state == 0:
            if bL < (lon_lo - min_sweep) and bC >= lon_lo:
                sw = 1; swk = ki; state = 1
            elif bH > (lon_hi + min_sweep) and bC <= lon_hi:
                sw = -1; swk = ki; state = 1
        elif state == 1 and ki >= swk + 2:
            kA = nyi[ki - 2]
            AH = H[kA]; AL = L[kA]; CbH = H[k]; CbL = L[k]
            if sw == 1 and (CbL - AH) >= min_fvg:
                entry = CbL; sl = AH - spread
                if entry - sl > 0:
                    return {"direction": "LONG", "entry": round(entry, 5),
                            "sl": round(sl, 5), "tp": round(entry + rr * (entry - sl), 5),
                            "reason": "ICT: barrido London Low + FVG alcista en NY"}
            elif sw == -1 and (AL - CbH) >= min_fvg:
                entry = CbH; sl = AL + spread
                if sl - entry > 0:
                    return {"direction": "SHORT", "entry": round(entry, 5),
                            "sl": round(sl, 5), "tp": round(entry - rr * (sl - entry), 5),
                            "reason": "ICT: barrido London High + FVG bajista en NY"}
    return None

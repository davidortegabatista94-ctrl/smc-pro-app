"""
backend/strategies.py — Backtest engine + all 17 strategy implementations.

No Streamlit calls. Pure Python / pandas / numpy.
"""
import logging
import numpy as np
import pandas as pd
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

from backend.config import PIP, SCALP_MAX_HOLD
from backend.indicators import flatten_columns, last_scalar, scalar

# ── Lazy yfinance loader (mirrors smc_pro_app pattern) ───────────────────────
_yf = None

def _get_yf():
    global _yf
    if _yf is None:
        try:
            import yfinance as yf_module
            _yf = yf_module
        except Exception:
            _yf = False
    return _yf

# ============================================
# BACKTEST SIMPLE
# ============================================
def run_backtest(df, direction="LONG", sl_pips=17, tp_pips=34, max_candles=20):
    if df.empty or len(df) < max_candles + 5:
        return None
    results = []
    for i in range(len(df) - max_candles - 1):
        entry = float(df["Close"].iloc[i])
        tp = entry + tp_pips * PIP if direction == "LONG" else entry - tp_pips * PIP
        sl = entry - sl_pips * PIP if direction == "LONG" else entry + sl_pips * PIP
        outcome = "TIMEOUT"
        for j in range(1, max_candles + 1):
            h = float(df["High"].iloc[i + j])
            l = float(df["Low"].iloc[i + j])
            if direction == "LONG":
                if l <= sl: outcome = "LOSS"; break
                if h >= tp: outcome = "WIN";  break
            else:
                if h >= sl: outcome = "LOSS"; break
                if l <= tp: outcome = "WIN";  break
        results.append(outcome)
    wins   = results.count("WIN")
    losses = results.count("LOSS")
    total  = len(results)
    winrate    = wins / total * 100 if total > 0 else 0
    expectancy = (wins * tp_pips - losses * sl_pips) / total if total > 0 else 0
    return {
        "total": total, "wins": wins, "losses": losses,
        "timeouts": results.count("TIMEOUT"),
        "winrate": round(winrate, 1),
        "expectancy": round(expectancy, 2),
        "net_pips": wins * tp_pips - losses * sl_pips
    }

# ============================================
# BACKTEST COMPLETO — AÑO ANTERIOR
# ============================================
def get_backtest_data(tf="1h"):
    """Descarga datos para backtest. Intenta períodos largos con fallback a cortos."""
    yf = _get_yf()
    if not yf:
        return pd.DataFrame()
    # Para 1h yfinance soporta hasta ~60 días de forma fiable en la API gratuita.
    # Descargamos varios bloques de 60d y los concatenamos para obtener hasta ~1 año.
    if tf in ("1h", "4h"):
        from datetime import timedelta as _td
        frames = []
        end = datetime.now()
        for chunk in range(6):  # 6 bloques de ~60d = ~1 año
            start = end - _td(days=59)
            try:
                df_chunk = yf.download(
                    "EURUSD=X",
                    start=start.strftime("%Y-%m-%d"),
                    end=end.strftime("%Y-%m-%d"),
                    interval="1h",
                    progress=False, auto_adjust=True
                )
                df_chunk = flatten_columns(df_chunk)
                if not df_chunk.empty:
                    frames.append(df_chunk)
            except Exception as e:
                logging.warning(f"Backtest chunk {chunk}: {e}")
            end = start - _td(days=1)
        if not frames:
            return pd.DataFrame()
        df = pd.concat(frames).sort_index()
        df = df[~df.index.duplicated(keep="first")]
        if tf == "4h":
            df = df.resample("4h").agg({
                "Open": "first", "High": "max",
                "Low": "min", "Close": "last", "Volume": "sum"
            }).dropna()
        return df
    else:
        for period in ["2y", "1y", "6mo"]:
            try:
                df = yf.download("EURUSD=X", period=period, interval="1d",
                                 progress=False, auto_adjust=True)
                df = flatten_columns(df)
                if not df.empty and len(df) > 50:
                    return df
            except Exception as e:
                logging.warning(f"Backtest daily {period}: {e}")
    return pd.DataFrame()


def get_longterm_data_2008():
    """
    Descarga datos diarios EUR/USD desde 2008 hasta hoy via yfinance.
    Los datos diarios están disponibles desde 1999 sin límite de período.
    Devuelve DataFrame con columnas Open/High/Low/Close/Volume.
    """
    yf_mod = _get_yf()
    if not yf_mod:
        return pd.DataFrame()
    for attempt in ["2008-01-01", "2010-01-01"]:
        try:
            df = yf_mod.download(
                "EURUSD=X",
                start=attempt,
                interval="1d",
                progress=False,
                auto_adjust=True,
            )
            df = flatten_columns(df)
            df.dropna(subset=["Close", "High", "Low"], inplace=True)
            df = df[df["Close"] > 0]
            if len(df) > 500:
                logging.info(f"Long-term data: {len(df)} bars desde {attempt}")
                return df
        except Exception as e:
            logging.warning(f"Long-term data ({attempt}): {e}")
    return pd.DataFrame()


def run_full_backtest(df, sl_pips=None, use_windows=True, utc_offset=2):
    """
    Estrategia multi-confluencia equilibrada — EUR/USD 1h.
    Objetivo: 3-5 entradas/semana, 40%+ win rate, R:R 1:3.

    LONG:  EMA9>EMA21>EMA50, MACD+, RSI 42-73, vela alcista
    SHORT: EMA9<EMA21<EMA50, MACD-, RSI 27-58, vela bajista
    SL=1.2xATR (6-20p) | TP=3.0xSL | Cooldown 6 velas entre entradas.
    Eliminados: near_EMA21 y ADX>20 (eran los principales cuellos de botella).
    """
    if df.empty or len(df) < 60:
        return None

    close = df["Close"].copy()
    high  = df["High"].copy()
    low   = df["Low"].copy()

    # EMAs para tendencia
    ema9  = close.ewm(span=9,  adjust=False).mean()
    ema21 = close.ewm(span=21, adjust=False).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()

    # RSI(14)
    dc   = close.diff()
    gain = dc.clip(lower=0).rolling(14).mean()
    loss = (-dc.clip(upper=0)).rolling(14).mean()
    rsi  = 100 - (100 / (1 + gain / loss.replace(0, np.nan)))

    # MACD histogram
    macd_line = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
    macd_sig  = macd_line.ewm(span=9, adjust=False).mean()
    hist      = macd_line - macd_sig

    # ATR(14)
    tr  = pd.concat([high - low,
                     (high - close.shift()).abs(),
                     (low  - close.shift()).abs()], axis=1).max(axis=1)
    atr = tr.rolling(14).mean()

    RR      = 3.0
    pip_val = 1.0

    trades       = []
    equity       = [10000.0]
    in_trade     = False
    ep = dr = tp_p = sl_p = ei = None
    last_entry_i = -999   # cooldown: mínimo 6 velas entre entradas

    for i in range(55, len(df) - 1):
        # Filtro ventana horaria
        if use_windows and hasattr(df.index[i], "hour"):
            hs = (df.index[i].hour + utc_offset) % 24
            if not (7 <= hs < 20) and not in_trade:
                continue

        c      = float(close.iloc[i])
        e9     = float(ema9.iloc[i]);  e21 = float(ema21.iloc[i])
        e50    = float(ema50.iloc[i])
        r      = float(rsi.iloc[i])   if not np.isnan(rsi.iloc[i])  else 50.0
        hv     = float(hist.iloc[i])  if not np.isnan(hist.iloc[i]) else 0.0
        av     = float(atr.iloc[i])   if not np.isnan(atr.iloc[i])  else PIP * 12
        prev_c = float(close.iloc[i - 1])

        # SL dinámico: 1.2x ATR, mínimo 6p, máximo 20p
        sl_d = max(min(av * 1.2, PIP * 20), PIP * 6)
        tp_d = sl_d * RR

        # ── Gestionar trade abierto ─────────────────────────────────────────
        if in_trade:
            hc = float(high.iloc[i]); lc = float(low.iloc[i])
            sl_pips_real = sl_d / PIP; tp_pips_real = tp_d / PIP
            if dr == "LONG":
                if lc <= sl_p:
                    pnl = -sl_pips_real * pip_val
                    equity.append(equity[-1] + pnl)
                    trades.append({"dir": "LONG", "outcome": "SL",
                                   "pips": round(-sl_pips_real, 1), "pnl": round(pnl, 2),
                                   "time": str(df.index[ei])[:16]})
                    in_trade = False
                elif hc >= tp_p:
                    pnl = tp_pips_real * pip_val
                    equity.append(equity[-1] + pnl)
                    trades.append({"dir": "LONG", "outcome": "TP",
                                   "pips": round(tp_pips_real, 1), "pnl": round(pnl, 2),
                                   "time": str(df.index[ei])[:16]})
                    in_trade = False
            else:
                if hc >= sl_p:
                    pnl = -sl_pips_real * pip_val
                    equity.append(equity[-1] + pnl)
                    trades.append({"dir": "SHORT", "outcome": "SL",
                                   "pips": round(-sl_pips_real, 1), "pnl": round(pnl, 2),
                                   "time": str(df.index[ei])[:16]})
                    in_trade = False
                elif lc <= tp_p:
                    pnl = tp_pips_real * pip_val
                    equity.append(equity[-1] + pnl)
                    trades.append({"dir": "SHORT", "outcome": "TP",
                                   "pips": round(tp_pips_real, 1), "pnl": round(pnl, 2),
                                   "time": str(df.index[ei])[:16]})
                    in_trade = False
            continue

        # ── Condiciones de entrada ──────────────────────────────────────────
        cooldown_ok = (i - last_entry_i) >= 6   # no entrar dos veces en 6 velas
        min_atr     = av > PIP * 4              # volatilidad mínima

        bull_align  = e9 > e21 > e50            # tendencia alcista confirmada
        bear_align  = e9 < e21 < e50            # tendencia bajista confirmada
        macd_long   = hv > 0                    # momentum alcista
        macd_short  = hv < 0                    # momentum bajista
        long_rsi    = 42 <= r <= 73             # RSI saludable alcista (no sobrecomprado)
        short_rsi   = 27 <= r <= 58             # RSI saludable bajista (no sobrevendido)
        bull_candle = c > prev_c                # vela alcista confirma entrada
        bear_candle = c < prev_c                # vela bajista confirma entrada

        # LONG: tendencia alcista + MACD+ + RSI saludable + vela alcista
        if (bull_align and macd_long and long_rsi and
                min_atr and bull_candle and cooldown_ok):
            ep = c;  dr = "LONG"
            tp_p = c + tp_d;  sl_p = c - sl_d
            in_trade = True; ei = i; last_entry_i = i

        # SHORT: tendencia bajista + MACD- + RSI saludable + vela bajista
        elif (bear_align and macd_short and short_rsi and
              min_atr and bear_candle and cooldown_ok):
            ep = c;  dr = "SHORT"
            tp_p = c - tp_d;  sl_p = c + sl_d
            in_trade = True; ei = i; last_entry_i = i

    # Cerrar trade abierto al final
    if in_trade and ep is not None:
        lp   = float(close.iloc[-1])
        pcl  = (lp - ep) / PIP if dr == "LONG" else (ep - lp) / PIP
        pnlc = pcl * pip_val
        equity.append(equity[-1] + pnlc)
        trades.append({"dir": dr, "outcome": "OPEN",
                       "pips": round(pcl, 1), "pnl": round(pnlc, 2),
                       "time": str(df.index[ei])[:16]})

    if not trades:
        return None

    wins   = [t for t in trades if t["outcome"] == "TP"]
    losses = [t for t in trades if t["outcome"] == "SL"]
    total  = len(trades)
    wr     = len(wins) / total * 100 if total > 0 else 0
    np_    = sum(t["pips"] for t in trades)
    npnl   = sum(t["pnl"]  for t in trades)

    peak = equity[0]; max_dd = 0.0
    for e in equity:
        if e > peak: peak = e
        dd = (peak - e) / peak * 100
        if dd > max_dd: max_dd = dd

    gw = sum(t["pnl"] for t in wins)         if wins   else 0.0
    gl = abs(sum(t["pnl"] for t in losses))  if losses else 1.0
    pf = round(gw / max(gl, 0.01), 2)

    be_winrate = round(1 / (1 + RR) * 100, 1)

    return {
        "total":         total,
        "wins":          len(wins),
        "losses":        len(losses),
        "winrate":       round(wr, 1),
        "be_winrate":    be_winrate,
        "net_pips":      round(np_, 1),
        "net_pnl":       round(npnl, 2),
        "max_dd":        round(max_dd, 1),
        "profit_factor": pf,
        "rr_ratio":      RR,
        "equity":        equity,
        "trades":        trades[-300:],
    }

# ── Afinidad de estrategias por régimen de mercado ───────────────────────────
_STRATEGY_REGIME_AFFINITY = {
    "ema_trend":           ["trending_bull", "trending_bear", "volatile_trend"],
    "ema_crossover":       ["trending_bull", "trending_bear", "volatile_trend"],
    "triple_ema":          ["volatile_trend", "trending_bull", "trending_bear"],
    "ema_ribbon":          ["trending_bull", "trending_bear"],
    "macd_cross":          ["trending_bull", "trending_bear", "volatile_trend"],
    "rsi_reversion":       ["trending_bull", "trending_bear"],
    "rsi_50_cross":        ["ranging", "trending_bull", "trending_bear"],
    "stochastic_trend":    ["ranging", "trending_bull"],
    "bb_touch":            ["ranging"],
    "keltner_touch":       ["ranging"],
    "donchian_breakout":   ["volatile_trend", "trending_bull", "trending_bear"],
    "supertrend":          ["trending_bull", "trending_bear", "volatile_trend"],
    "market_structure_bo": ["trending_bull", "trending_bear", "volatile_trend"],
    "momentum_breakout":   ["volatile_trend", "volatile"],
    "aggressive_momentum": ["volatile_trend", "volatile"],
    "meta_composite":      ["trending_bull", "trending_bear", "ranging", "volatile_trend"],
    "precision_be":        ["trending_bull", "trending_bear"],
}

# Etiquetas de régimen en español para UI
_REGIME_LABELS = {
    "trending_bull":  "Tendencia Alcista",
    "trending_bear":  "Tendencia Bajista",
    "volatile_trend": "Tendencia Explosiva",
    "volatile":       "Alta Volatilidad",
    "ranging":        "Mercado Lateral",
    "pre_news":       "Riesgo Noticias",
    "unknown":        "Desconocido",
}
_REGIME_ICONS = {
    "trending_bull":  "📈",
    "trending_bear":  "📉",
    "volatile_trend": "⚡",
    "volatile":       "🌪️",
    "ranging":        "↔️",
    "pre_news":       "⚠️",
    "unknown":        "❓",
}

# ── Metadatos de estrategias ─────────────────────────────────────────────────
_STRATEGY_META = {
    # ── Tendencia con EMAs ──────────────────────────────────────────────────
    "ema_trend": {
        "label": "EMA Trend (9/21/50 + MACD + RSI)",
        "why":   "Las 3 EMAs alineadas en los 3 horizontes + confirmación MACD y RSI. Alta selectividad.",
        "pros":  "Bajo drawdown · Alta selectividad",
        "cons":  "Pocas señales en rangos",
    },
    "ema_crossover": {
        "label": "EMA Crossover 9/21 + EMA50 filtro",
        "why":   "Cuando la EMA9 cruza la EMA21 (golden/death cross corto plazo), con precio al lado correcto de EMA50. Captura el inicio de cada impulso.",
        "pros":  "Entrada temprana en impulsos · Buena frecuencia",
        "cons":  "Whipsaws en rangos laterales",
    },
    "triple_ema": {
        "label": "Triple EMA 3/8/21 (sistema rápido)",
        "why":   "Las EMA 3/8/21 son un sistema clásico de seguimiento de tendencia a corto plazo. Cuando las 3 están alineadas y en dirección creciente, el momentum es muy fuerte.",
        "pros":  "Muy sensible · Muchas señales en tendencias",
        "cons":  "Alta frecuencia de señales falsas en laterales",
    },
    "ema_ribbon": {
        "label": "EMA Ribbon 5/10/20/50 (multi-marco)",
        "why":   "5 EMAs alineadas confirman tendencia en 5 horizontes diferentes. Señal muy fiable aunque poco frecuente.",
        "pros":  "Señales muy robustas · Bajo drawdown",
        "cons":  "Muy pocas señales — solo en tendencias limpias",
    },
    # ── Momentum / Osciladores ──────────────────────────────────────────────
    "macd_cross": {
        "label": "MACD Crossover (hist cruza cero) + EMA50",
        "why":   "El cruce del histograma MACD de negativo a positivo señala cambio de momentum. EMA50 da la dirección macro.",
        "pros":  "Entra pronto en tendencias · Buena frecuencia",
        "cons":  "Señales falsas en laterales",
    },
    "rsi_reversion": {
        "label": "RSI Reversion en Tendencia (pullback a 45)",
        "why":   "En tendencia (EMA21 > EMA50), espera pullback RSI 40-48 y rebote. Entrada en el punto exacto de menor riesgo.",
        "pros":  "Win rate alta · Entradas en mínimos de corrección",
        "cons":  "Requiere tendencia clara previa",
    },
    "rsi_50_cross": {
        "label": "RSI cruza nivel 50 + MACD + EMA50",
        "why":   "Cuando RSI cruza el nivel 50 (de territorio bajista a alcista o viceversa) con precio al lado correcto de EMA50 y MACD en misma dirección, confirma cambio de momentum.",
        "pros":  "Simple · Frecuencia moderada · Buenas confirmaciones",
        "cons":  "RSI puede oscilar alrededor del 50 en rangos",
    },
    "stochastic_trend": {
        "label": "Estocástico (14,3) reversión en tendencia",
        "why":   "El estocástico mide posición del precio en su rango reciente. Cuando el %K cruza al %D saliendo de zona oversold (< 25) en tendencia alcista, alta probabilidad de rebote.",
        "pros":  "Entradas muy precisas en correcciones · Clásico probado",
        "cons":  "Puede señalizar early en tendencias muy fuertes",
    },
    # ── Volatilidad / Bandas ────────────────────────────────────────────────
    "bb_touch": {
        "label": "Bollinger Band Touch (−2σ) + RSI",
        "why":   "Toca la banda inferior (−2σ) en tendencia alcista con RSI < 45. Corrección estadísticamente extrema con alta probabilidad de rebote al centro.",
        "pros":  "Entradas muy precisas · Funciona bien en EUR/USD",
        "cons":  "Precio puede pegarse a la banda en tendencias fuertes",
    },
    "keltner_touch": {
        "label": "Keltner Channel Touch (EMA20 ± 2.5×ATR)",
        "why":   "El canal Keltner (EMA20 ± 2.5×ATR14) filtra mejor la volatilidad que Bollinger. Tocar el canal inferior en tendencia alcista es una señal de compra clásica.",
        "pros":  "Menos falsas señales que BB · Usa volatilidad real (ATR)",
        "cons":  "Señales poco frecuentes en mercados de baja volatilidad",
    },
    # ── Ruptura / Breakout ──────────────────────────────────────────────────
    "donchian_break": {
        "label": "Donchian Breakout 20 períodos + EMA50",
        "why":   "Romper el máximo de 20 velas (canal Donchian) con precio sobre EMA50 señala una ruptura de resistencia clave. Sistema de seguimiento de tendencia puro.",
        "pros":  "Captura movimientos grandes · Sin indicadores rezagados",
        "cons":  "Falsas rupturas frecuentes sin filtros adicionales",
    },
    "momentum_break": {
        "label": "ATR Momentum Breakout (máx/mín 10 velas)",
        "why":   "Cuando el precio rompe el máximo de las últimas 10 velas con momentum confirmado (RSI > 50), indica un impulso real con fuerza suficiente para continuar.",
        "pros":  "Captura impulsos fuertes · R:R favorable",
        "cons":  "Puede entrar tarde en el movimiento",
    },
    "supertrend": {
        "label": "SuperTrend (EMA(H+L/2) ± 3×ATR10)",
        "why":   "El SuperTrend es un indicador de seguimiento de tendencia basado en ATR que traza soporte/resistencia dinámico. Cuando el precio cruza el nivel, señala cambio de tendencia.",
        "pros":  "Muy visual · Pocas señales pero de alta calidad",
        "cons":  "Rezagado por naturaleza — entra tarde en reversiones",
    },
    # ── Acción del precio ───────────────────────────────────────────────────
    "engulfing": {
        "label": "Engulfing Pattern (velas envolventes) + EMA50",
        "why":   "Una vela envolvente alcista (el cuerpo actual cubre el cuerpo anterior) en zona de soporte/EMA50 señala rechazo fuerte de los vendedores y entrada de compradores institucionales.",
        "pros":  "Señal de acción del precio pura · Sin indicadores",
        "cons":  "Necesita contexto (nivel de soporte/tendencia)",
    },
    # ── ESPECIALES ──────────────────────────────────────────────────────────
    "aggressive_momentum": {
        "label": "AGRESIVA: Momentum Explosivo (ATR alto + vela fuerte)",
        "why":   "Entra en movimientos explosivos: vela fuerte (cuerpo > 55% del rango) + ATR ≥ 6 pips + EMA alineadas. Sin filtro RSI — diseñada para capturas rápidas en mercado en movimiento. Cooldown de 4 velas para mayor frecuencia.",
        "pros":  "Captura impulsos explosivos · Más operaciones en tendencias fuertes · Sin filtro RSI restrictivo",
        "cons":  "Mayor drawdown que filtradas · Requiere volatilidad alta · Más stop losses consecutivos",
    },
    "meta_composite": {
        "label": "META-Composite: Consenso Inteligente (6 estrategias)",
        "why":   "Vota entre 6 estrategias diversas (EMA Trend, MACD, SuperTrend, RSI-50, Momentum Breakout, Estocástico). Entra solo cuando ≥3 coinciden en la misma dirección. La señal de mayor calidad posible — identifica los factores comunes que hacen ganar al resto de estrategias.",
        "pros":  "Señales de altísima calidad · Drawdown mínimo · Confluencia total multi-sistema",
        "cons":  "Pocas señales — solo en confluencia perfecta · Puede perderse impulsos rápidos",
    },
    "precision_be": {
        "label": "Precisión BE: Pullback EMA21 con Break-Even Automático",
        "why":   "Entra en pullbacks exactos a la EMA21 dentro de tendencia (EMA 9>21>50), confirmados por RSI saludable + MACD creciente + Estocástico girando. Una vez el precio avanza 1× SL en favor, el stop se mueve automáticamente al punto de entrada (break-even) — el trade no puede perder dinero después de ese punto.",
        "pros":  "Capital protegido tras activar BE · Win rate efectiva alta · Entradas de alta precisión en pullbacks",
        "cons":  "Requiere tendencia + pullback exacto · Algunas operaciones cierran en BE sin ganancia",
    },
}

_ALL_STRATEGIES = list(_STRATEGY_META.keys())
_RANK_EMOJI = ["🥇","🥈","🥉","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟",
               "⓫","⓬","⓭","⓮","⓯","⓰","⓱"]


def _run_single_strategy(df, strategy="ema_trend", use_windows=True, utc_offset=2, daily_mode=False):
    min_bars = 200 if daily_mode else 110
    if df.empty or len(df) < min_bars:
        return None
    RR = 3.0  # default risk/reward; overridden per-strategy below
    # Escala de umbrales ATR según timeframe
    if daily_mode:
        use_windows  = False   # sin filtro de horario en datos diarios
        _atr_min     = PIP * 15   # ATR mínimo: 15 pips (permite entradas en baja volatilidad)
        _sl_min      = PIP * 30   # SL mínimo
        _sl_max      = PIP * 120  # SL máximo
        _cd_base     = 2          # cooldown: 2 días
        _agg_atr_min = PIP * 30   # aggressive_momentum mínimo ATR
        _be_atr_min  = PIP * 30   # precision_be mínimo ATR
        _be_near_pip = PIP * 150  # pullback EMA21 ± 150 pips (más amplio en diario)
        _sl_mult     = 1.0        # multiplicador SL: 1×ATR diario (~51 pips)
        _entry_rr    = 2.0        # RR diario: TP = 2×SL (~102 pips)
        _max_bars    = 20         # máximo días en operación
        _rsi_mul     = 15         # ampliar rangos RSI en ±15 para datos diarios
    else:
        _atr_min     = PIP * 4
        _sl_min      = PIP * 6
        _sl_max      = PIP * 20
        _cd_base     = 6
        _agg_atr_min = PIP * 6
        _be_atr_min  = PIP * 5
        _be_near_pip = PIP * 10
        _sl_mult     = 1.2
        _entry_rr    = RR
        _max_bars    = 9999
        _rsi_mul     = 0          # sin ajuste para datos horarios

    close = df["Close"].copy(); high = df["High"].copy(); low = df["Low"].copy()
    opn   = df["Open"].copy() if "Open" in df.columns else close.shift(1)

    ema3  = close.ewm(span=3,   adjust=False).mean()
    ema5  = close.ewm(span=5,   adjust=False).mean()
    ema8  = close.ewm(span=8,   adjust=False).mean()
    ema9  = close.ewm(span=9,   adjust=False).mean()
    ema10 = close.ewm(span=10,  adjust=False).mean()
    ema20 = close.ewm(span=20,  adjust=False).mean()
    ema21 = close.ewm(span=21,  adjust=False).mean()
    ema50 = close.ewm(span=50,  adjust=False).mean()
    ema100= close.ewm(span=100, adjust=False).mean()

    dc   = close.diff()
    gain = dc.clip(lower=0).rolling(14).mean()
    loss = (-dc.clip(upper=0)).rolling(14).mean()
    rsi  = 100 - (100 / (1 + gain / loss.replace(0, np.nan)))

    macd_line = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
    macd_sig  = macd_line.ewm(span=9, adjust=False).mean()
    hist      = macd_line - macd_sig

    tr    = pd.concat([high - low, (high - close.shift()).abs(),
                       (low - close.shift()).abs()], axis=1).max(axis=1)
    atr14 = tr.rolling(14).mean()
    atr10 = tr.rolling(10).mean()

    sma20 = close.rolling(20).mean()
    std20 = close.rolling(20).std()
    bb_lo = sma20 - 2 * std20
    bb_up = sma20 + 2 * std20

    kc_lo = ema20 - 2.5 * atr14
    kc_up = ema20 + 2.5 * atr14

    dc_hi = high.rolling(20).max().shift(1)
    dc_lo = low.rolling(20).min().shift(1)
    mb_hi = high.rolling(10).max().shift(1)
    mb_lo = low.rolling(10).min().shift(1)

    lo14    = low.rolling(14).min()
    hi14    = high.rolling(14).max()
    stoch_k = 100 * (close - lo14) / (hi14 - lo14).replace(0, np.nan)
    stoch_d = stoch_k.rolling(3).mean()

    hl2    = (high + low) / 2
    st_up  = hl2 - 3 * atr10
    st_dn  = hl2 + 3 * atr10
    st_dir_list = []
    prev_st = float(st_up.iloc[0]) if not np.isnan(st_up.iloc[0]) else 0.0
    prev_dir = 1
    for idx in range(len(close)):
        cu = float(st_up.iloc[idx]) if not np.isnan(st_up.iloc[idx]) else prev_st
        cd = float(st_dn.iloc[idx]) if not np.isnan(st_dn.iloc[idx]) else prev_st
        cv = float(close.iloc[idx])
        if prev_dir == 1:
            cur_st = max(cu, prev_st) if cv > prev_st else cd
            prev_dir = 1 if cv > cur_st else -1
        else:
            cur_st = min(cd, prev_st) if cv < prev_st else cu
            prev_dir = -1 if cv < cur_st else 1
        st_dir_list.append(prev_dir)
        prev_st = cur_st
    st_dir = pd.Series(st_dir_list, index=close.index)

    RR = 3.0; pip_val = 1.0
    trades = []; equity = [10000.0]
    in_trade = False; ep = dr = tp_p = sl_p = ei = None
    last_entry_i = -999; be_activated = False

    for i in range(110, len(df) - 1):
        if use_windows and hasattr(df.index[i], "hour"):
            hs = (df.index[i].hour + utc_offset) % 24
            if not (7 <= hs < 20) and not in_trade:
                continue

        c      = float(close.iloc[i]);   prev_c = float(close.iloc[i-1])
        h_c    = float(high.iloc[i]);    l_c    = float(low.iloc[i])
        o_c    = float(opn.iloc[i])  if not np.isnan(opn.iloc[i]) else prev_c
        prev_h = float(high.iloc[i-1]); prev_l = float(low.iloc[i-1])
        prev_o = float(opn.iloc[i-1]) if not np.isnan(opn.iloc[i-1]) else float(close.iloc[i-2])
        e3  = float(ema3.iloc[i]);   e5  = float(ema5.iloc[i]);   e8  = float(ema8.iloc[i])
        e9  = float(ema9.iloc[i]);   e10 = float(ema10.iloc[i]);  e20 = float(ema20.iloc[i])
        e21 = float(ema21.iloc[i]);  e50 = float(ema50.iloc[i]);  e100= float(ema100.iloc[i])
        e9p = float(ema9.iloc[i-1]); e21p= float(ema21.iloc[i-1])
        e3p = float(ema3.iloc[i-1]); e8p = float(ema8.iloc[i-1])
        r   = float(rsi.iloc[i])    if not np.isnan(rsi.iloc[i])    else 50.0
        r_p = float(rsi.iloc[i-1])  if not np.isnan(rsi.iloc[i-1])  else 50.0
        hv  = float(hist.iloc[i])   if not np.isnan(hist.iloc[i])   else 0.0
        hv_p= float(hist.iloc[i-1]) if not np.isnan(hist.iloc[i-1]) else 0.0
        av  = float(atr14.iloc[i])  if not np.isnan(atr14.iloc[i])  else PIP * 12
        sk  = float(stoch_k.iloc[i])  if not np.isnan(stoch_k.iloc[i])  else 50.0
        sd  = float(stoch_d.iloc[i])  if not np.isnan(stoch_d.iloc[i])  else 50.0
        sk_p= float(stoch_k.iloc[i-1])if not np.isnan(stoch_k.iloc[i-1]) else 50.0
        sd_p= float(stoch_d.iloc[i-1])if not np.isnan(stoch_d.iloc[i-1]) else 50.0
        bbl = float(bb_lo.iloc[i])  if not np.isnan(bb_lo.iloc[i])  else c - av*2
        bbu = float(bb_up.iloc[i])  if not np.isnan(bb_up.iloc[i])  else c + av*2
        kcl = float(kc_lo.iloc[i])  if not np.isnan(kc_lo.iloc[i])  else c - av*2.5
        kcu = float(kc_up.iloc[i])  if not np.isnan(kc_up.iloc[i])  else c + av*2.5
        dchi= float(dc_hi.iloc[i])  if not np.isnan(dc_hi.iloc[i])  else c + av*3
        dclo= float(dc_lo.iloc[i])  if not np.isnan(dc_lo.iloc[i])  else c - av*3
        mbhi= float(mb_hi.iloc[i])  if not np.isnan(mb_hi.iloc[i])  else c + av*2
        mblo= float(mb_lo.iloc[i])  if not np.isnan(mb_lo.iloc[i])  else c - av*2
        sdir= int(st_dir.iloc[i]);  sdir_p = int(st_dir.iloc[i-1])

        sl_d = max(min(av * _sl_mult, _sl_max), _sl_min)
        tp_d = sl_d * _entry_rr

        if in_trade:
            spr = sl_d / PIP; tpr = tp_d / PIP
            # Break-even: cuando precio avanza 1× SL en favor, mover SL a entrada
            if strategy == "precision_be" and not be_activated and ep is not None:
                _be_dist = abs(ep - sl_p)
                if dr == "LONG"  and h_c >= ep + _be_dist:
                    sl_p = ep; be_activated = True
                elif dr == "SHORT" and l_c <= ep - _be_dist:
                    sl_p = ep; be_activated = True
            if dr == "LONG":
                if l_c <= sl_p:
                    if strategy == "precision_be" and be_activated:
                        equity.append(equity[-1])
                        trades.append({"dir":"LONG","outcome":"BE","pips":0.0,"pnl":0.0,"time":str(df.index[ei])[:16]})
                    else:
                        pnl = -spr*pip_val; equity.append(equity[-1]+pnl)
                        trades.append({"dir":"LONG","outcome":"SL","pips":round(-spr,1),"pnl":round(pnl,2),"time":str(df.index[ei])[:16]})
                    in_trade = False; be_activated = False
                elif h_c >= tp_p:
                    pnl = tpr*pip_val; equity.append(equity[-1]+pnl)
                    trades.append({"dir":"LONG","outcome":"TP","pips":round(tpr,1),"pnl":round(pnl,2),"time":str(df.index[ei])[:16]})
                    in_trade = False; be_activated = False
            else:
                if h_c >= sl_p:
                    if strategy == "precision_be" and be_activated:
                        equity.append(equity[-1])
                        trades.append({"dir":"SHORT","outcome":"BE","pips":0.0,"pnl":0.0,"time":str(df.index[ei])[:16]})
                    else:
                        pnl = -spr*pip_val; equity.append(equity[-1]+pnl)
                        trades.append({"dir":"SHORT","outcome":"SL","pips":round(-spr,1),"pnl":round(pnl,2),"time":str(df.index[ei])[:16]})
                    in_trade = False; be_activated = False
                elif l_c <= tp_p:
                    pnl = tpr*pip_val; equity.append(equity[-1]+pnl)
                    trades.append({"dir":"SHORT","outcome":"TP","pips":round(tpr,1),"pnl":round(pnl,2),"time":str(df.index[ei])[:16]})
                    in_trade = False; be_activated = False
            # Forzar cierre si la operación lleva demasiadas barras (solo modo diario)
            if in_trade and ei is not None and (i - ei) >= _max_bars:
                cur_pips = ((c - ep) / PIP) if dr == "LONG" else ((ep - c) / PIP)
                pnl = cur_pips * pip_val; equity.append(equity[-1] + pnl)
                trades.append({"dir": dr, "outcome": "MAX", "pips": round(cur_pips, 1), "pnl": round(pnl, 2), "time": str(df.index[ei])[:16]})
                in_trade = False; be_activated = False
            continue

        cd_ok = (i - last_entry_i) >= _cd_base
        matr  = av > _atr_min
        long_sig = short_sig = False

        if strategy == "ema_trend":
            long_sig  = (e9>e21>e50) and hv>0 and (42-_rsi_mul)<=r<=(73+_rsi_mul) and c>prev_c and matr and cd_ok
            short_sig = (e9<e21<e50) and hv<0 and (27-_rsi_mul)<=r<=(58+_rsi_mul) and c<prev_c and matr and cd_ok
        elif strategy == "ema_crossover":
            long_sig  = (e9>e21 and e9p<=e21p) and c>e50 and (45-_rsi_mul)<=r<=(72+_rsi_mul) and matr and cd_ok
            short_sig = (e9<e21 and e9p>=e21p) and c<e50 and (28-_rsi_mul)<=r<=(55+_rsi_mul) and matr and cd_ok
        elif strategy == "triple_ema":
            long_sig  = (e3>e8>e21) and e3>e3p and e8>e8p and (44-_rsi_mul)<=r<=(75+_rsi_mul) and matr and cd_ok
            short_sig = (e3<e8<e21) and e3<e3p and e8<e8p and (25-_rsi_mul)<=r<=(56+_rsi_mul) and matr and cd_ok
        elif strategy == "ema_ribbon":
            long_sig  = (e5>e10>e20>e50) and c>e100 and (44-_rsi_mul)<=r<=(74+_rsi_mul) and matr and cd_ok
            short_sig = (e5<e10<e20<e50) and c<e100 and (26-_rsi_mul)<=r<=(56+_rsi_mul) and matr and cd_ok
        elif strategy == "macd_cross":
            long_sig  = (c>e50) and (hv>0 and hv_p<=0) and (30-_rsi_mul)<=r<=(72+_rsi_mul) and matr and cd_ok
            short_sig = (c<e50) and (hv<0 and hv_p>=0) and (28-_rsi_mul)<=r<=(70+_rsi_mul) and matr and cd_ok
        elif strategy == "rsi_reversion":
            long_sig  = (e21>e50) and (r_p<=48 and r>r_p) and r<(62+_rsi_mul) and c>e50 and matr and cd_ok
            short_sig = (e21<e50) and (r_p>=52 and r<r_p) and r>(38-_rsi_mul) and c<e50 and matr and cd_ok
        elif strategy == "rsi_50_cross":
            long_sig  = (r>50 and r_p<=50) and hv>0 and c>e50 and matr and cd_ok
            short_sig = (r<50 and r_p>=50) and hv<0 and c<e50 and matr and cd_ok
        elif strategy == "stochastic_trend":
            _sk_lev    = 35 + _rsi_mul   # 35 en 1h, 50 en diario
            stoch_xu   = sk>sd and sk_p<=sd_p and sk<_sk_lev
            stoch_xd   = sk<sd and sk_p>=sd_p and sk>(100-_sk_lev)
            long_sig   = (e21>e50) and stoch_xu and c>e50 and matr and cd_ok
            short_sig  = (e21<e50) and stoch_xd and c<e50 and matr and cd_ok
        elif strategy == "bb_touch":
            _dir_ok_l  = True if daily_mode else c>prev_c
            _dir_ok_s  = True if daily_mode else c<prev_c
            long_sig   = (e21>e50) and (c<=bbl*1.002) and r<(45+_rsi_mul) and _dir_ok_l and matr and cd_ok
            short_sig  = (e21<e50) and (c>=bbu*0.998) and r>(55-_rsi_mul) and _dir_ok_s and matr and cd_ok
        elif strategy == "keltner_touch":
            _dir_ok_l  = True if daily_mode else c>prev_c
            _dir_ok_s  = True if daily_mode else c<prev_c
            long_sig   = (e21>e50) and (c<=kcl*1.002) and r<(45+_rsi_mul) and _dir_ok_l and matr and cd_ok
            short_sig  = (e21<e50) and (c>=kcu*0.998) and r>(55-_rsi_mul) and _dir_ok_s and matr and cd_ok
        elif strategy == "donchian_break":
            long_sig  = (c>dchi) and c>e50 and (50-_rsi_mul)<=r<=(76+_rsi_mul) and matr and cd_ok
            short_sig = (c<dclo) and c<e50 and (24-_rsi_mul)<=r<=(50+_rsi_mul) and matr and cd_ok
        elif strategy == "momentum_break":
            long_sig  = (c>mbhi) and r>(50-_rsi_mul) and hv>0 and matr and cd_ok
            short_sig = (c<mblo) and r<(50+_rsi_mul) and hv<0 and matr and cd_ok
        elif strategy == "supertrend":
            long_sig  = (sdir==1  and sdir_p==-1) and matr and cd_ok
            short_sig = (sdir==-1 and sdir_p==1)  and matr and cd_ok
        elif strategy == "engulfing":
            bull_eng = (o_c<prev_c) and (c>prev_o) and (c>o_c) and (prev_c<prev_o)
            bear_eng = (o_c>prev_c) and (c<prev_o) and (c<o_c) and (prev_c>prev_o)
            long_sig  = bull_eng and c>e50 and r<(65+_rsi_mul) and matr and cd_ok
            short_sig = bear_eng and c<e50 and r>(35-_rsi_mul) and matr and cd_ok

        elif strategy == "aggressive_momentum":
            body       = abs(c - o_c)
            rng        = (h_c - l_c) if (h_c - l_c) > 1e-9 else 1e-9
            strong     = (body / rng) > 0.55
            high_atr   = av > _agg_atr_min
            cd_ok_agg  = (i - last_entry_i) >= max(4, _cd_base - 2)
            long_sig   = (e9>e21>e50) and c>prev_c and strong and high_atr and cd_ok_agg
            short_sig  = (e9<e21<e50) and c<prev_c and strong and high_atr and cd_ok_agg

        elif strategy == "meta_composite":
            # CONSENSO: ≥3 de 6 estrategias diversas deben coincidir en la misma dirección
            lv = sv_ = 0
            # 1) EMA trend (RSI ampliado en diario)
            if (e9>e21>e50) and hv>0 and (42-_rsi_mul)<=r<=(73+_rsi_mul) and c>prev_c: lv  += 1
            elif (e9<e21<e50) and hv<0 and (27-_rsi_mul)<=r<=(58+_rsi_mul) and c<prev_c: sv_ += 1
            # 2) MACD cross
            if (c>e50) and (hv>0 and hv_p<=0):                            lv  += 1
            elif (c<e50) and (hv<0 and hv_p>=0):                          sv_ += 1
            # 3) SuperTrend dirección
            if sdir == 1:                                                   lv  += 1
            elif sdir == -1:                                                sv_ += 1
            # 4) RSI cruza 50
            if (r>50 and r_p<=50) and c>e50:                               lv  += 1
            elif (r<50 and r_p>=50) and c<e50:                             sv_ += 1
            # 5) Momentum breakout (umbral RSI ampliado)
            if c>mbhi and r>(50-_rsi_mul) and hv>0:                        lv  += 1
            elif c<mblo and r<(50+_rsi_mul) and hv<0:                      sv_ += 1
            # 6) Estocástico en tendencia (nivel ampliado en diario)
            _sk_lv = 35 + _rsi_mul
            stx_u = sk>sd and sk_p<=sd_p and sk<_sk_lv
            stx_d = sk<sd and sk_p>=sd_p and sk>(100-_sk_lv)
            if stx_u and e21>e50:                                          lv  += 1
            elif stx_d and e21<e50:                                        sv_ += 1
            long_sig  = lv  >= 3 and matr and cd_ok
            short_sig = sv_ >= 3 and matr and cd_ok

        elif strategy == "precision_be":
            near_e21_long  = e50 < c <= e21 + _be_near_pip
            near_e21_short = e50 > c >= e21 - _be_near_pip
            macd_growing   = hv > hv_p and hv > 0
            macd_turning   = hv > 0 and hv_p <= 0
            macd_ok_l      = macd_growing or macd_turning
            macd_ok_s      = (hv < hv_p and hv < 0) or (hv < 0 and hv_p >= 0)
            stoch_turn_up  = sk > sd and sk_p <= sd_p and sk < (50 + _rsi_mul)
            stoch_turn_dn  = sk < sd and sk_p >= sd_p and sk > (50 - _rsi_mul)
            bull_candle    = c > o_c
            bear_candle    = c < o_c
            cd_ok_be       = (i - last_entry_i) >= max(4, _cd_base + 2)
            atr_ok_be      = av > _be_atr_min
            if daily_mode:
                # En diario: condiciones simplificadas (pullback EMA21 + MACD + RSI no extremo)
                # El stochastic crossover es demasiado raro en datos diarios para usarlo como filtro
                long_sig  = (e9>e21>e50) and near_e21_long  and macd_ok_l and 20<=r<=75 and atr_ok_be and cd_ok_be
                short_sig = (e9<e21<e50) and near_e21_short and macd_ok_s and 25<=r<=80 and atr_ok_be and cd_ok_be
            else:
                long_sig  = (e9>e21>e50) and near_e21_long  and 38<=r<=58 and macd_ok_l and stoch_turn_up and bull_candle and atr_ok_be and cd_ok_be
                short_sig = (e9<e21<e50) and near_e21_short and 42<=r<=62 and macd_ok_s and stoch_turn_dn and bear_candle and atr_ok_be and cd_ok_be

        # Entrada — precision_be usa RR=2.5; en modo diario _entry_rr=2.0; en horario RR=3.0
        _tp_d = sl_d * (2.5 if strategy == "precision_be" else _entry_rr)
        if long_sig:
            ep=c; dr="LONG";  tp_p=c+_tp_d; sl_p=c-sl_d; in_trade=True; ei=i; last_entry_i=i
        elif short_sig:
            ep=c; dr="SHORT"; tp_p=c-_tp_d; sl_p=c+sl_d; in_trade=True; ei=i; last_entry_i=i

    if in_trade and ep is not None:
        lp   = float(close.iloc[-1])
        pcl  = (lp - ep) / PIP if dr == "LONG" else (ep - lp) / PIP
        pnlc = pcl * pip_val
        equity.append(equity[-1] + pnlc)
        trades.append({"dir":dr,"outcome":"OPEN","pips":round(pcl,1),"pnl":round(pnlc,2),"time":str(df.index[ei])[:16]})

    if not trades:
        return None

    wins   = [t for t in trades if t["outcome"] == "TP" or (t["outcome"] == "MAX" and t["pips"] > 0)]
    losses = [t for t in trades if t["outcome"] == "SL" or (t["outcome"] == "MAX" and t["pips"] <= 0)]
    bes    = [t for t in trades if t["outcome"] == "BE"]
    total  = len(trades)
    wr     = len(wins) / total * 100 if total > 0 else 0
    # be_winrate: TP/(TP+SL) excluyendo trades cerrados en BE (0 pips)
    _decisive = len(wins) + len(losses)
    be_wr  = round(len(wins) / _decisive * 100, 1) if _decisive > 0 else 0.0
    np_    = sum(t["pips"] for t in trades)
    npnl   = sum(t["pnl"]  for t in trades)
    peak   = equity[0]; max_dd = 0.0
    for e in equity:
        if e > peak: peak = e
        dd = (peak - e) / peak * 100
        if dd > max_dd: max_dd = dd
    gw = sum(t["pnl"] for t in wins)        if wins   else 0.0
    gl = abs(sum(t["pnl"] for t in losses)) if losses else 1.0
    pf = round(gw / max(gl, 0.01), 2)
    meta = _STRATEGY_META.get(strategy, {})
    return {
        "strategy":   strategy,
        "label":      meta.get("label", strategy),
        "why":        meta.get("why",   ""),
        "pros":       meta.get("pros",  ""),
        "cons":       meta.get("cons",  ""),
        "total":      total, "wins": len(wins), "losses": len(losses), "be_count": len(bes),
        "winrate":    round(wr, 1), "be_winrate": be_wr,
        "net_pips":   round(np_, 1), "net_pnl": round(npnl, 2),
        "max_dd":     round(max_dd, 1), "profit_factor": pf,
        "rr_ratio":   RR, "equity": equity, "trades": trades[-300:],
        "daily_mode": daily_mode,
    }


def run_strategy_comparison(df, use_windows=True, utc_offset=2):
    """Ejecuta las 17 estrategias sobre los mismos datos. Devuelve ranking + ganadora."""
    results = []
    for name in _ALL_STRATEGIES:
        r = _run_single_strategy(df, strategy=name, use_windows=use_windows, utc_offset=utc_offset)
        if r:
            results.append(r)
    if not results:
        return None
    results.sort(
        key=lambda r: r["profit_factor"] * (r["winrate"] / 100) * (r["total"] ** 0.5),
        reverse=True
    )
    return {"results": results, "best": results[0]}


def run_longterm_comparison(df_daily):
    """
    Ejecuta las 17 estrategias sobre datos diarios desde 2008.
    Usa daily_mode=True: ATR escalado, sin filtro de horario, cooldown en días.
    Devuelve mismo formato que run_strategy_comparison.
    """
    if df_daily.empty or len(df_daily) < 200:
        return None

    results = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {
            pool.submit(
                _run_single_strategy,
                df_daily, name, False, 2, True   # daily_mode=True
            ): name
            for name in _ALL_STRATEGIES
        }
        for fut in as_completed(futures):
            r = fut.result()
            if r:
                results.append(r)

    if not results:
        return None

    results.sort(
        key=lambda r: r["profit_factor"] * (r["winrate"] / 100) * (r["total"] ** 0.5),
        reverse=True,
    )
    return {"results": results, "best": results[0]}


def _live_strategy_signal(df, strategy):
    """Aplica las reglas de entrada de la estrategia al estado ACTUAL del mercado.
    Devuelve: ("LONG"|"SHORT"|"NO TRADE", explicacion_str)
    """
    if df is None or df.empty or len(df) < 115:
        return "NO TRADE", "Sin datos suficientes"
    try:
        close = df["Close"]
        high  = df["High"]
        low   = df["Low"]
        opn   = df["Open"] if "Open" in df.columns else close.shift(1)
        PIP   = 0.0001

        # ── Indicadores base ───────────────────────────────────────────────
        e3  = close.ewm(span=3,   adjust=False).mean()
        e5  = close.ewm(span=5,   adjust=False).mean()
        e8  = close.ewm(span=8,   adjust=False).mean()
        e9  = close.ewm(span=9,   adjust=False).mean()
        e10 = close.ewm(span=10,  adjust=False).mean()
        e20 = close.ewm(span=20,  adjust=False).mean()
        e21 = close.ewm(span=21,  adjust=False).mean()
        e50 = close.ewm(span=50,  adjust=False).mean()
        e100= close.ewm(span=100, adjust=False).mean()

        # RSI(14)
        d = close.diff()
        gain = d.clip(lower=0).ewm(com=13, adjust=False).mean()
        loss = (-d.clip(upper=0)).ewm(com=13, adjust=False).mean()
        rs   = gain / loss.replace(0, 1e-9)
        rsi  = 100 - 100 / (1 + rs)

        # MACD histogram
        macd_line   = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
        macd_signal = macd_line.ewm(span=9, adjust=False).mean()
        macd_hist   = macd_line - macd_signal

        # ATR(14) and ATR(10)
        tr14 = pd.concat([high - low,
                          (high - close.shift(1)).abs(),
                          (low  - close.shift(1)).abs()], axis=1).max(axis=1)
        atr14 = tr14.rolling(14).mean()
        tr10  = tr14.copy()
        atr10 = tr10.rolling(10).mean()

        # Bollinger(20,2)
        bb_mid = close.rolling(20).mean()
        bb_std = close.rolling(20).std()
        bb_lo  = bb_mid - 2 * bb_std
        bb_hi  = bb_mid + 2 * bb_std

        # Keltner(EMA20 ± 2.5×ATR14)
        kc_lo = e20 - 2.5 * atr14
        kc_hi = e20 + 2.5 * atr14

        # Donchian(20, shift 1 para no lookahead)
        don_hi = high.shift(1).rolling(20).max()
        don_lo = low.shift(1).rolling(20).min()

        # Momentum windows(10, shift 1)
        mom_hi = high.shift(1).rolling(10).max()
        mom_lo = low.shift(1).rolling(10).min()

        # Stochastic(14,3)
        lo14  = low.rolling(14).min()
        hi14  = high.rolling(14).max()
        stk   = (close - lo14) / (hi14 - lo14 + 1e-9) * 100
        std   = stk.rolling(3).mean()

        # SuperTrend(3×ATR10) — loop para estado correcto
        mult   = 3.0
        hl2    = (high + low) / 2.0
        up_b   = hl2 + mult * atr10
        dn_b   = hl2 - mult * atr10
        n      = len(df)
        st_dir = pd.Series(index=df.index, dtype=float)
        final_up = up_b.copy(); final_dn = dn_b.copy()
        for j in range(1, n):
            c_prev = close.iloc[j-1]
            fu_prev = final_up.iloc[j-1]
            fd_prev = final_dn.iloc[j-1]
            final_up.iloc[j] = min(up_b.iloc[j], fu_prev) if c_prev <= fu_prev else up_b.iloc[j]
            final_dn.iloc[j] = max(dn_b.iloc[j], fd_prev) if c_prev >= fd_prev else dn_b.iloc[j]
            d_prev = st_dir.iloc[j-1] if j > 1 else 1
            if c_prev <= fu_prev:
                st_dir.iloc[j] = -1 if close.iloc[j] <= final_up.iloc[j] else 1
            else:
                st_dir.iloc[j] = 1 if close.iloc[j] >= final_dn.iloc[j] else -1

        # ── Leer valores del ÚLTIMO candle ──────────────────────────────────
        i = len(df) - 1   # índice actual
        c    = close.iloc[i];  c1 = close.iloc[i-1]
        h    = high.iloc[i];   l  = low.iloc[i]
        o    = opn.iloc[i];    o1 = opn.iloc[i-1]
        h1   = high.iloc[i-1]; l1 = low.iloc[i-1]
        av   = atr14.iloc[i]
        r    = rsi.iloc[i];    r1 = rsi.iloc[i-1]
        hv   = macd_hist.iloc[i]; hv1 = macd_hist.iloc[i-1]
        sv   = e9.iloc[i]; sv21 = e21.iloc[i]; sv50 = e50.iloc[i]
        stk_v = stk.iloc[i]; std_v = std.iloc[i]
        stk1  = stk.iloc[i-1]; std1  = std.iloc[i-1]
        st_d  = st_dir.iloc[i]; st_d1 = st_dir.iloc[i-1]
        bb_l  = bb_lo.iloc[i]; bb_h = bb_hi.iloc[i]
        kc_l  = kc_lo.iloc[i]; kc_h = kc_hi.iloc[i]
        d_hi  = don_hi.iloc[i]; d_lo = don_lo.iloc[i]
        m_hi  = mom_hi.iloc[i]; m_lo = mom_lo.iloc[i]
        min_atr = av > PIP * 4

        bull_align  = sv > sv21 > sv50
        bear_align  = sv < sv21 < sv50
        macd_long   = hv > 0
        macd_short  = hv < 0
        bull_candle = c > c1
        bear_candle = c < c1
        abv_e50     = c > sv50
        blw_e50     = c < sv50

        direction = "NO TRADE"
        reason    = "Sin setup en este momento"

        if strategy == "ema_trend":
            if bull_align and macd_long  and 42<=r<=73 and min_atr and bull_candle:
                direction, reason = "LONG",  "EMA 9>21>50, MACD+, RSI saludable, vela alcista"
            elif bear_align and macd_short and 27<=r<=58 and min_atr and bear_candle:
                direction, reason = "SHORT", "EMA 9<21<50, MACD−, RSI saludable, vela bajista"

        elif strategy == "ema_crossover":
            cross_up   = e9.iloc[i] > e21.iloc[i] and e9.iloc[i-1] <= e21.iloc[i-1]
            cross_down = e9.iloc[i] < e21.iloc[i] and e9.iloc[i-1] >= e21.iloc[i-1]
            if cross_up   and abv_e50 and min_atr: direction, reason = "LONG",  "EMA9 cruza EMA21 al alza + precio>EMA50"
            elif cross_down and blw_e50 and min_atr: direction, reason = "SHORT", "EMA9 cruza EMA21 a la baja + precio<EMA50"

        elif strategy == "triple_ema":
            v3=e3.iloc[i]; v8=e8.iloc[i]; v3p=e3.iloc[i-1]; v8p=e8.iloc[i-1]
            bull3 = v3 > v8 > e21.iloc[i] and v3>v3p and v8>v8p
            bear3 = v3 < v8 < e21.iloc[i] and v3<v3p and v8<v8p
            if bull3 and min_atr: direction, reason = "LONG",  "Triple EMA 3/8/21 alcista y subiendo"
            elif bear3 and min_atr: direction, reason = "SHORT", "Triple EMA 3/8/21 bajista y bajando"

        elif strategy == "ema_ribbon":
            v5=e5.iloc[i]; v10=e10.iloc[i]; v20=e20.iloc[i]
            ribbon_bull = v5>v10>v20>sv50 and c>e100.iloc[i]
            ribbon_bear = v5<v10<v20<sv50 and c<e100.iloc[i]
            if ribbon_bull and min_atr: direction, reason = "LONG",  "Ribbon EMA 5>10>20>50, precio>EMA100"
            elif ribbon_bear and min_atr: direction, reason = "SHORT", "Ribbon EMA 5<10<20<50, precio<EMA100"

        elif strategy == "macd_cross":
            cross_bull = hv > 0 and hv1 <= 0
            cross_bear = hv < 0 and hv1 >= 0
            if cross_bull and abv_e50 and min_atr: direction, reason = "LONG",  "MACD hist cruza cero al alza + precio>EMA50"
            elif cross_bear and blw_e50 and min_atr: direction, reason = "SHORT", "MACD hist cruza cero a la baja + precio<EMA50"

        elif strategy == "rsi_reversion":
            pull_bull = bull_align and 40<=r<=48 and r>r1
            pull_bear = bear_align and 52<=r<=60 and r<r1
            if pull_bull and min_atr: direction, reason = "LONG",  "Pullback RSI 40–48 en tendencia alcista, rebotando"
            elif pull_bear and min_atr: direction, reason = "SHORT", "Pullback RSI 52–60 en tendencia bajista, rebotando"

        elif strategy == "rsi_50_cross":
            r50_bull = r > 50 and r1 <= 50
            r50_bear = r < 50 and r1 >= 50
            if r50_bull and macd_long  and abv_e50 and min_atr: direction, reason = "LONG",  "RSI cruza 50 al alza + MACD+ + precio>EMA50"
            elif r50_bear and macd_short and blw_e50 and min_atr: direction, reason = "SHORT", "RSI cruza 50 a la baja + MACD− + precio<EMA50"

        elif strategy == "stochastic_trend":
            stoch_bull = stk_v > std_v and stk1 <= std1 and stk_v < 80
            stoch_bear = stk_v < std_v and stk1 >= std1 and stk_v > 20
            if stoch_bull and abv_e50 and min_atr: direction, reason = f"LONG",  f"Estocástico %K cruza %D al alza ({stk_v:.0f}), en tendencia alcista"
            elif stoch_bear and blw_e50 and min_atr: direction, reason = f"SHORT", f"Estocástico %K cruza %D a la baja ({stk_v:.0f}), en tendencia bajista"

        elif strategy == "bb_touch":
            touch_lo = l <= bb_l and c > bb_l
            touch_hi = h >= bb_h and c < bb_h
            if touch_lo and abv_e50 and r < 45 and min_atr: direction, reason = "LONG",  f"Toca Bollinger inferior, RSI={r:.0f}, en tendencia alcista"
            elif touch_hi and blw_e50 and r > 55 and min_atr: direction, reason = "SHORT", f"Toca Bollinger superior, RSI={r:.0f}, en tendencia bajista"

        elif strategy == "keltner_touch":
            touch_lo = l <= kc_l and c > kc_l
            touch_hi = h >= kc_h and c < kc_h
            if touch_lo and r < 48 and min_atr: direction, reason = "LONG",  f"Toca Keltner inferior, RSI={r:.0f}"
            elif touch_hi and r > 52 and min_atr: direction, reason = "SHORT", f"Toca Keltner superior, RSI={r:.0f}"

        elif strategy == "donchian_break":
            break_up   = c > d_hi
            break_down = c < d_lo
            if break_up   and abv_e50 and min_atr: direction, reason = "LONG",  f"Rompe máximo Donchian 20 ({d_hi:.5f}) + precio>EMA50"
            elif break_down and blw_e50 and min_atr: direction, reason = "SHORT", f"Rompe mínimo Donchian 20 ({d_lo:.5f}) + precio<EMA50"

        elif strategy == "momentum_break":
            break_up   = c > m_hi
            break_down = c < m_lo
            if break_up   and 45<=r<=70 and macd_long  and min_atr: direction, reason = "LONG",  "Breakout momentum 10 barras al alza + RSI + MACD+"
            elif break_down and 30<=r<=55 and macd_short and min_atr: direction, reason = "SHORT", "Breakout momentum 10 barras a la baja + RSI + MACD−"

        elif strategy == "supertrend":
            flip_bull = st_d == 1  and st_d1 == -1
            flip_bear = st_d == -1 and st_d1 == 1
            if st_d == 1  and min_atr: direction, reason = "LONG",  "SuperTrend alcista" + (" — FLIP reciente" if flip_bull else "")
            elif st_d == -1 and min_atr: direction, reason = "SHORT", "SuperTrend bajista" + (" — FLIP reciente" if flip_bear else "")

        elif strategy == "engulfing":
            bull_eng = c > o and o <= c1 and c >= o1 and (c - o) > (o1 - c1) * 0.8
            bear_eng = c < o and o >= c1 and c <= o1 and (o - c) > (c1 - o1) * 0.8
            if bull_eng and abv_e50 and min_atr: direction, reason = "LONG",  "Vela envolvente alcista sobre EMA50"
            elif bear_eng and blw_e50 and min_atr: direction, reason = "SHORT", "Vela envolvente bajista bajo EMA50"

        elif strategy == "aggressive_momentum":
            body  = abs(c - o);  rng = (h - l) if (h - l) > 1e-9 else 1e-9
            strong   = (body / rng) > 0.55
            high_atr = av > PIP * 6
            if bull_align and bull_candle and strong and high_atr:
                direction, reason = "LONG",  f"AGRESIVA: tendencia alcista + vela fuerte ({body/rng*100:.0f}% rango) + ATR {av/PIP:.1f}p — sin filtro RSI"
            elif bear_align and bear_candle and strong and high_atr:
                direction, reason = "SHORT", f"AGRESIVA: tendencia bajista + vela fuerte ({body/rng*100:.0f}% rango) + ATR {av/PIP:.1f}p — sin filtro RSI"
            else:
                _why = []
                if not bull_align and not bear_align: _why.append("EMAs no alineadas")
                if not strong: _why.append(f"vela débil ({body/rng*100:.0f}% rango, necesita >55%)")
                if not high_atr: _why.append(f"ATR bajo ({av/PIP:.1f}p, necesita >6p)")
                reason = "AGRESIVA sin setup: " + " · ".join(_why)

        elif strategy == "meta_composite":
            # Voto entre 6 sistemas distintos — entra con ≥3 de acuerdo
            lv = 0; sv_ = 0
            names_l = []; names_s = []
            # 1) EMA trend
            if bull_align and macd_long  and 42<=r<=73 and bull_candle: lv+=1;  names_l.append("EMA-Trend")
            elif bear_align and macd_short and 27<=r<=58 and bear_candle: sv_+=1; names_s.append("EMA-Trend")
            # 2) MACD cross cero
            if hv>0 and hv1<=0 and abv_e50: lv+=1;  names_l.append("MACD-cross")
            elif hv<0 and hv1>=0 and blw_e50: sv_+=1; names_s.append("MACD-cross")
            # 3) SuperTrend
            if st_d == 1:  lv+=1;  names_l.append("SuperTrend")
            elif st_d == -1: sv_+=1; names_s.append("SuperTrend")
            # 4) RSI cruza 50
            if r>50 and r1<=50 and macd_long  and abv_e50: lv+=1;  names_l.append("RSI-50↑")
            elif r<50 and r1>=50 and macd_short and blw_e50: sv_+=1; names_s.append("RSI-50↓")
            # 5) Momentum breakout
            if c>m_hi and r>50 and macd_long:  lv+=1;  names_l.append("Breakout")
            elif c<m_lo and r<50 and macd_short: sv_+=1; names_s.append("Breakout")
            # 6) Estocástico
            stx_u = stk_v>std_v and stk1<=std1 and stk_v<35
            stx_d = stk_v<std_v and stk1>=std1 and stk_v>65
            if stx_u and abv_e50: lv+=1;  names_l.append("Estoc")
            elif stx_d and blw_e50: sv_+=1; names_s.append("Estoc")
            if lv >= 3 and min_atr:
                direction = "LONG"
                reason = f"META {lv}/6: {', '.join(names_l)} — máxima confluencia"
            elif sv_ >= 3 and min_atr:
                direction = "SHORT"
                reason = f"META {sv_}/6: {', '.join(names_s)} — máxima confluencia"
            else:
                reason = f"META sin consenso suficiente (LONG={lv}/6, SHORT={sv_}/6 — necesita ≥3)"

        elif strategy == "precision_be":
            # Pullback exacto a EMA21 en tendencia + MACD creciente + Estocástico girando
            sv21v  = float(e21.iloc[i])
            near_l = sv50 < float(c) <= sv21v + PIP * 10
            near_s = sv50 > float(c) >= sv21v - PIP * 10
            macd_ok_l = (hv > hv1 and hv > 0) or (hv > 0 and hv1 <= 0)
            macd_ok_s = (hv < hv1 and hv < 0) or (hv < 0 and hv1 >= 0)
            stoch_up  = stk_v > std_v and stk1 <= std1 and stk_v < 50
            stoch_dn  = stk_v < std_v and stk1 >= std1 and stk_v > 50
            if bull_align and near_l and 38<=r<=58 and macd_ok_l and stoch_up and bull_candle and min_atr:
                direction = "LONG"
                reason = (f"PRECISIÓN BE: pullback a EMA21 ({sv21v:.5f}) en tendencia alcista · "
                          f"MACD creciente · Estoc {stk_v:.0f} girando · RSI {r:.0f} — "
                          f"BE automático al avanzar 1×SL")
            elif bear_align and near_s and 42<=r<=62 and macd_ok_s and stoch_dn and bear_candle and min_atr:
                direction = "SHORT"
                reason = (f"PRECISIÓN BE: pullback a EMA21 ({sv21v:.5f}) en tendencia bajista · "
                          f"MACD decreciente · Estoc {stk_v:.0f} girando · RSI {r:.0f} — "
                          f"BE automático al retroceder 1×SL")
            else:
                _why = []
                if not bull_align and not bear_align: _why.append("EMAs sin tendencia")
                if not (near_l or near_s):            _why.append("precio no en pullback EMA21")
                if not (stoch_up or stoch_dn):        _why.append(f"Estoc sin giro ({stk_v:.0f})")
                if not (macd_ok_l or macd_ok_s):      _why.append("MACD no creciente")
                reason = "BE sin setup: " + (" · ".join(_why) if _why else "condiciones no alineadas")

        return direction, reason
    except Exception as ex:
        logging.warning(f"_live_strategy_signal {strategy}: {ex}")
        return "NO TRADE", "Error de cálculo"


# ============================================
# INDICADORES PARA UI (calculate_indicators + analyze_timeframe)
# ============================================
def calculate_indicators(df):
    if df.empty:
        return {}
    close, high, low = df["Close"], df["High"], df["Low"]
    ind = {}
    for n in [9, 20, 21, 50]:
        if len(close) >= n:
            ind[f"SMA{n}"] = close.rolling(n).mean()
            ind[f"EMA{n}"] = close.ewm(span=n, adjust=False).mean()
    if len(close) >= 15:
        delta = close.diff()
        gain  = delta.clip(lower=0).rolling(14).mean()
        loss  = (-delta.clip(upper=0)).rolling(14).mean()
        rs    = gain / loss.replace(0, np.nan)
        ind["RSI"] = 100 - (100 / (1 + rs))
    if len(close) >= 26:
        m12  = close.ewm(span=12, adjust=False).mean()
        m26  = close.ewm(span=26, adjust=False).mean()
        macd = m12 - m26
        sig  = macd.ewm(span=9, adjust=False).mean()
        ind["MACD"] = macd; ind["Signal"] = sig; ind["Histogram"] = macd - sig
    if "SMA20" in ind:
        s20 = close.rolling(20).std()
        ind["BB_upper"] = ind["SMA20"] + s20 * 2
        ind["BB_lower"] = ind["SMA20"] - s20 * 2
    if len(df) >= 15:
        tr = pd.concat([
            high - low,
            (high - close.shift()).abs(),
            (low  - close.shift()).abs()
        ], axis=1).max(axis=1)
        ind["ATR"] = tr.rolling(14).mean()
    return ind

def analyze_timeframe(tf_name, df):
    if df.empty:
        return {"timeframe": tf_name, "error": "Sin datos", "signal": "NEUTRAL"}
    ind   = calculate_indicators(df)
    close = last_scalar(df["Close"])
    if close is None:
        return {"timeframe": tf_name, "error": "Sin precio", "signal": "NEUTRAL"}
    r = {
        "timeframe": tf_name, "price": close, "trend": "NINGUNO",
        "signal": "NEUTRAL", "rsi": None, "rsi_status": "NEUTRAL",
        "macd_signal": None, "ema_cross": None, "bb_position": None, "atr": None
    }
    atr = last_scalar(ind.get("ATR"))
    if atr: r["atr"] = round(atr / PIP, 1)
    rsi = last_scalar(ind.get("RSI"))
    if rsi is not None:
        r["rsi"] = rsi
        r["rsi_status"] = ("SOBRECOMPRADO" if rsi > 70
                           else "SOBREVENDIDO" if rsi < 30 else "NEUTRAL")
    sma20 = last_scalar(ind.get("SMA20"))
    sma50 = last_scalar(ind.get("SMA50"))
    if sma20 and sma50:
        if close > sma20 > sma50:   r["trend"] = "ALCISTA"
        elif close < sma20 < sma50: r["trend"] = "BAJISTA"
    hist = last_scalar(ind.get("Histogram"))
    if hist is not None:
        r["macd_signal"] = "COMPRA" if hist > 0 else "VENTA"
    e9s  = ind.get("EMA9"); e21s = ind.get("EMA21")
    if e9s is not None and e21s is not None and len(e9s) > 1:
        e9n, e21n = last_scalar(e9s), last_scalar(e21s)
        e9p, e21p = scalar(e9s.iloc[-2]), scalar(e21s.iloc[-2])
        if all(v is not None for v in [e9n, e21n, e9p, e21p]):
            if e9n > e21n and e9p <= e21p:   r["ema_cross"] = "ALCISTA"
            elif e9n < e21n and e9p >= e21p: r["ema_cross"] = "BAJISTA"
    bbu = last_scalar(ind.get("BB_upper"))
    bbl = last_scalar(ind.get("BB_lower"))
    if bbu and bbl:
        if close > bbu * 0.998:   r["bb_position"] = "SUPERIOR"
        elif close < bbl * 1.002: r["bb_position"] = "INFERIOR"
        else:                     r["bb_position"] = "MEDIO"
    buys  = sum([r["rsi_status"] == "SOBREVENDIDO",
                 r["macd_signal"] == "COMPRA", r["ema_cross"] == "ALCISTA"])
    sells = sum([r["rsi_status"] == "SOBRECOMPRADO",
                 r["macd_signal"] == "VENTA",  r["ema_cross"] == "BAJISTA"])
    if buys > sells:   r["signal"] = "COMPRA"
    elif sells > buys: r["signal"] = "VENTA"
    return r

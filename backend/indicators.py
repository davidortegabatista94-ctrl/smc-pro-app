"""
backend/indicators.py — Pure math / indicator functions (no Streamlit, no I/O).

All functions are deterministic given their inputs. No st.* calls allowed here.
"""
import pandas as pd
import numpy as np
import logging

from backend.config import PIP, SCALP_TP_PIPS, SCALP_SL_PIPS, SCALP_MAX_HOLD

# ============================================
# UTILIDADES
# ============================================
def scalar(val):
    if val is None:
        return None
    if isinstance(val, pd.Series):
        if val.empty:
            return None
        val = val.iloc[0]
    if hasattr(val, "item"):
        val = val.item()
    try:
        f = float(val)
        return f if not np.isnan(f) else None
    except (TypeError, ValueError):
        return None

def last_scalar(series):
    if series is None or (isinstance(series, pd.Series) and series.empty):
        return None
    return scalar(series.iloc[-1])

def flatten_columns(df):
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.loc[:, ~df.columns.duplicated()]
    return df

# ============================================
# DXY — INTERPRETACIÓN
# ============================================
def interpret_dxy_signal(close):
    if close.empty:
        return None, None, None, None

    current = scalar(close.iloc[-1])
    ema8 = close.ewm(span=8, adjust=False).mean()
    ema21 = close.ewm(span=21, adjust=False).mean()
    e8, e21 = last_scalar(ema8), last_scalar(ema21)

    trend = "LATERAL"
    direction = "LATERAL"
    if e8 is not None and e21 is not None:
        if current > e8 > e21:
            trend, direction = "ALCISTA", "UP"
        elif current < e8 < e21:
            trend, direction = "BAJISTA", "DOWN"
        elif current > e8:
            trend, direction = "ALCISTA", "UP"
        elif current < e8:
            trend, direction = "BAJISTA", "DOWN"

    momentum_positive = False
    if len(close) >= 4:
        momentum_positive = close.iloc[-1] > close.iloc[-4]
    if direction == "LATERAL" and momentum_positive:
        direction, trend = "UP", "ALCISTA"
    if direction == "LATERAL" and not momentum_positive and len(close) >= 4:
        direction, trend = "DOWN", "BAJISTA"

    return direction, trend, e8, e21

# ============================================
# COT — INTERPRETACIÓN
# ============================================
def interpret_cot_for_signal(cot):
    """Convierte COT en sesgo direccional para el score de confluencia."""
    if not cot:
        return None, 0
    net = cot["net"]
    change = cot["change"]
    if net > 50000 and change > 0:
        return "LONG", 15
    elif net > 20000:
        return "LONG", 8
    elif net < -50000 and change < 0:
        return "SHORT", 15
    elif net < -20000:
        return "SHORT", 8
    return "NEUTRAL", 0

# ============================================
# VOLUMEN — ANÁLISIS COMPLETO
# ============================================

def _ensure_volume(df: pd.DataFrame) -> pd.Series:
    """
    Composite multi-fuente de volumen para forex.

    Prioridad de fuentes:
      1. Volume_oanda  — tick count real de OANDA (mejor para forex)
      2. Volume        — columna real si sum > 0 (MT5 tick volume)
      3. Composite sintético de 3 proxies técnicos ponderados:
           a) Rango H-L en pips  (mide actividad bruta)
           b) Body efficiency    (rango × conviction direccional)
           c) ATR-normalizado    (rango relativo a volatilidad media)

    Todos se normalizan [0,1] antes de combinar y se re-escalan al
    rango del proxy de rango para mantener unidades comparables.
    """
    # ── Fuente 1: OANDA tick volume (la más precisa para forex) ──────────────
    if "Volume_oanda" in df.columns:
        v = df["Volume_oanda"].fillna(0).astype(float)
        if v.sum() > 0:
            return v

    # ── Fuente 2: volumen real si existe y es no-cero ─────────────────────────
    if "Volume" in df.columns:
        v = df["Volume"].fillna(0).astype(float)
        if v.sum() > 0:
            return v

    # ── Fuente 3: composite sintético 3 proxies ───────────────────────────────
    rng  = (df["High"] - df["Low"]).clip(lower=1e-7)
    body = (df["Close"] - df["Open"]).abs()

    # a) Rango de vela en pips × 1000  (base proxy)
    v_range = (rng / 0.0001 * 1000).clip(lower=1.0)

    # b) Body efficiency: velas con más cuerpo relativo = más volumen real
    body_ratio = (body / rng).clip(upper=1.0)
    v_body = (v_range * (0.5 + body_ratio * 0.5)).clip(lower=1.0)

    # c) ATR-normalizado: rango vs volatilidad media de 14 velas
    atr14    = rng.rolling(14, min_periods=3).mean().clip(lower=1e-8)
    atr_mult = (rng / atr14).clip(upper=3.0, lower=0.1)
    v_atr    = (v_range * atr_mult).clip(lower=1.0)

    # Normalizar cada proxy a [0,1] y combinar con pesos
    def _norm(s: pd.Series) -> pd.Series:
        mx = s.max()
        return s / mx if mx > 0 else s

    composite = (
        _norm(v_range) * 0.40 +
        _norm(v_body)  * 0.35 +
        _norm(v_atr)   * 0.25
    )
    # Re-escalar al mismo orden de magnitud que el proxy de rango
    scale = float(v_range.mean()) if v_range.mean() > 0 else 1.0
    return (composite * scale).clip(lower=1.0).round()


def detect_volume_spikes(df, threshold=2.0):
    """Detecta picos de volumen. Usa volumen sintético si el real es cero (forex)."""
    if df.empty or "Close" not in df.columns:
        return []
    vol = _ensure_volume(df)
    if len(vol) < 20:
        return []
    avg_vol = vol.rolling(20).mean().iloc[-1]
    cur_vol = vol.iloc[-1]
    if avg_vol and avg_vol > 0:
        ratio = cur_vol / avg_vol
        if ratio >= threshold:
            return [{
                "tipo":    "SPIKE DE VOLUMEN",
                "ratio":   round(ratio, 2),
                "emoji":   "⚡",
                "mensaje": f"Actividad {ratio:.1f}x sobre la media — posible movimiento institucional"
            }]
    return []


def detect_volume_trend(df):
    """Detecta si el volumen confirma la tendencia de precio."""
    if df.empty or "Close" not in df.columns or len(df) < 5:
        return "Sin datos"
    vol = _ensure_volume(df)
    price_up  = float(df["Close"].iloc[-1]) > float(df["Close"].iloc[-5])
    volume_up = float(vol.iloc[-1]) > float(vol.iloc[-5])
    if price_up and volume_up:
        return "✅ Actividad confirma tendencia ALCISTA"
    elif not price_up and volume_up:
        return "✅ Actividad confirma tendencia BAJISTA"
    elif price_up and not volume_up:
        return "⚠️ Precio sube pero actividad cae — posible debilidad alcista"
    else:
        return "⚠️ Precio baja pero actividad cae — posible agotamiento bajista"


def analyze_volume_profile(df, n_levels=10):
    """
    Perfil de volumen simplificado por niveles de precio.
    Usa volumen sintético si el real es cero.
    """
    if df.empty or "Close" not in df.columns or len(df) < 10:
        return [], None
    vol = _ensure_volume(df)
    price_min = float(df["Low"].min())
    price_max = float(df["High"].max())
    step = (price_max - price_min) / n_levels
    if step == 0:
        return [], None
    levels = []
    for i in range(n_levels):
        low_lvl  = price_min + i * step
        high_lvl = low_lvl + step
        mask = (df["Low"] <= high_lvl) & (df["High"] >= low_lvl)
        vol_at_level = float(vol[mask].sum())
        levels.append({
            "precio":  round((low_lvl + high_lvl) / 2, 5),
            "volumen": int(vol_at_level),
        })
    if not levels:
        return [], None
    max_vol = max(l["volumen"] for l in levels)
    for l in levels:
        l["pct"] = round(l["volumen"] / max_vol * 100, 1) if max_vol > 0 else 0.0
    poc = max(levels, key=lambda x: x["volumen"]) if max_vol > 0 else levels[0]
    return sorted(levels, key=lambda x: x["precio"], reverse=True), poc


def get_volume_delta(df):
    """
    Delta de volumen: diferencia entre presión compradora y vendedora.
    Usa volumen sintético si el real es cero.
    """
    if df.empty or "Close" not in df.columns or len(df) < 5:
        return None
    vol    = _ensure_volume(df)
    recent = df.tail(20).copy()
    rvol   = vol.iloc[-20:]
    bull_vol = rvol[recent["Close"] >= recent["Open"]].sum()
    bear_vol = rvol[recent["Close"] <  recent["Open"]].sum()
    total    = bull_vol + bear_vol
    if total == 0:
        return None
    delta     = bull_vol - bear_vol
    delta_pct = delta / total * 100
    return {
        "bull_vol":  int(bull_vol),
        "bear_vol":  int(bear_vol),
        "delta":     int(delta),
        "delta_pct": round(delta_pct, 1),
        "bias":      "COMPRADORES" if delta > 0 else "VENDEDORES",
    }


def get_cvd(df):
    """
    CVD (Cumulative Volume Delta): presión acumulada de compra/venta.
    Usa volumen sintético si el real es cero.
    """
    if df.empty or "Close" not in df.columns or len(df) < 5:
        return []
    vol    = _ensure_volume(df)
    recent = df.tail(30).copy()
    rvol   = vol.iloc[-30:].reset_index(drop=True)
    rc     = recent["Close"].reset_index(drop=True)
    ro     = recent["Open"].reset_index(drop=True)
    deltas = [float(rvol.iloc[i]) if rc.iloc[i] >= ro.iloc[i]
              else -float(rvol.iloc[i])
              for i in range(len(recent))]
    return pd.Series(deltas).cumsum().tolist()

# ============================================
# LIQUIDEZ
# ============================================
def detect_liquidity_levels(df):
    if df.empty:
        return []
    price = last_scalar(df["Close"])
    if price is None:
        return []
    levels = []
    for offset in [-0.0050, -0.0025, 0, 0.0025, 0.0050]:
        lvl  = round(round(price, 2) + offset, 4)
        dist = abs(price - lvl) / PIP
        if dist < 30:
            levels.append({"nivel": lvl, "tipo": "NÚMERO REDONDO",
                           "dist": round(dist, 1),
                           "fuerza": "ALTA" if dist < 10 else "MEDIA"})
    highs = df["High"].tail(50)
    for i in range(len(highs) - 1):
        for j in range(i + 1, len(highs)):
            if abs(highs.iloc[i] - highs.iloc[j]) < 0.0003:
                dist = abs(price - float(highs.iloc[i])) / PIP
                if dist < 50:
                    levels.append({"nivel": round(float(highs.iloc[i]), 5),
                                   "tipo": "EQUAL HIGH (EQH)",
                                   "dist": round(dist, 1), "fuerza": "MUY ALTA"})
                break
    lows = df["Low"].tail(50)
    for i in range(len(lows) - 1):
        for j in range(i + 1, len(lows)):
            if abs(lows.iloc[i] - lows.iloc[j]) < 0.0003:
                dist = abs(price - float(lows.iloc[i])) / PIP
                if dist < 50:
                    levels.append({"nivel": round(float(lows.iloc[i]), 5),
                                   "tipo": "EQUAL LOW (EQL)",
                                   "dist": round(dist, 1), "fuerza": "MUY ALTA"})
                break
    seen, unique = [], []
    for l in levels:
        if not any(abs(l["nivel"] - s) < 0.0005 for s in seen):
            seen.append(l["nivel"]); unique.append(l)
    return sorted(unique, key=lambda x: x["dist"])[:8]

# ============================================
# SCORE DE CONFLUENCIA
# ============================================
def calculate_confluence_score(signal, consensus, dxy_dir, session,
                                vol_spikes, liq_levels, delta=None,
                                cot=None, trend_strength=None):
    score = 0
    reasons = []
    direction = signal.get("direction")

    # ── Ventana horaria: requisito duro ──────────────────────────────────
    in_window = signal.get("in_trading_window", True)
    if not in_window:
        win_lbl = signal.get("window_label", "fuera de horario")
        reasons.append(f"⛔ FUERA DE HORARIO — {win_lbl} (NO OPERAR)")
        return 0, reasons   # Score 0 fuerza si estamos fuera de ventana

    # ── Técnico multi-TF: 30 pts ─────────────────────────────────────────
    tfs = signal.get("timeframes", {})
    aligned = sum(
        1 for a in tfs.values()
        if (direction == "LONG"  and a.get("signal") == "COMPRA") or
           (direction == "SHORT" and a.get("signal") == "VENTA")
    )
    tf_score = min(int(aligned / max(len(tfs), 1) * 30), 30)
    score += tf_score
    reasons.append(f"📊 Técnico: {aligned}/{len(tfs)} TF alineados (+{tf_score})")

    # ── ADX/Fuerza de tendencia: 10 pts ──────────────────────────────────
    if trend_strength:
        adx_v = trend_strength.get("adx", 0)
        ts_dir = trend_strength.get("tendencia", "")
        if adx_v >= 30:
            score += 10; reasons.append(f"📈 ADX {adx_v:.0f} — tendencia FUERTE (+10)")
        elif adx_v >= 20:
            score += 6;  reasons.append(f"📈 ADX {adx_v:.0f} — tendencia moderada (+6)")
        else:
            reasons.append(f"📈 ADX {adx_v:.0f} — mercado LATERAL sin tendencia (+0)")
        # Confirmación de dirección por ADX
        if (direction == "LONG"  and ts_dir == "ALCISTA") or \
           (direction == "SHORT" and ts_dir == "BAJISTA"):
            score += 5; reasons.append(f"📈 ADX confirma dirección {ts_dir} (+5)")

    # ── DXY: 12 pts ──────────────────────────────────────────────────────
    if (direction == "SHORT" and dxy_dir == "UP") or \
       (direction == "LONG"  and dxy_dir == "DOWN"):
        score += 12; reasons.append("💵 DXY confirma dirección (+12)")
    elif dxy_dir == "LATERAL":
        score += 4;  reasons.append("💵 DXY neutral (+4)")
    else:
        reasons.append("💵 DXY en contra (+0)")

    # ── COT institucional: 10 pts ─────────────────────────────────────────
    if cot:
        cot_dir, cot_pts = interpret_cot_for_signal(cot)
        if cot_dir == direction:
            score += cot_pts
            reasons.append(f"🏦 COT institucional confirma {direction} (+{cot_pts})")
        elif cot_dir == "NEUTRAL":
            reasons.append("🏦 COT neutral (+0)")
        else:
            reasons.append(f"🏦 COT en contra de {direction} (+0)")

    # ── Fundamental: 8 pts ───────────────────────────────────────────────
    cons = consensus.get("consensus", "")
    if (direction == "LONG"  and "Bullish" in cons) or \
       (direction == "SHORT" and "Bearish" in cons):
        score += 8; reasons.append("📰 Fundamental confirma (+8)")
    elif "Mixed" in cons:
        score += 2; reasons.append("📰 Fundamental mixto (+2)")

    # ── Sesión óptima: 8 pts ─────────────────────────────────────────────
    if "Londres" in session or "NY" in session:
        score += 8; reasons.append(f"🕐 Sesión óptima: {session} (+8)")
    elif "Tokio" in session:
        score += 3; reasons.append(f"🕐 Sesión Tokio — volatilidad media (+3)")

    # ── Volumen spike: 8 pts ─────────────────────────────────────────────
    if vol_spikes:
        score += 8; reasons.append("⚡ Spike de volumen institucional (+8)")

    # ── Delta volumen: 8 pts ─────────────────────────────────────────────
    if delta:
        if (direction == "LONG"  and delta["delta"] > 0) or \
           (direction == "SHORT" and delta["delta"] < 0):
            score += 8
            reasons.append(f"📦 Delta volumen confirma: {delta['bias']} ({delta['delta_pct']:+.1f}%) (+8)")
        else:
            reasons.append(f"📦 Delta volumen en contra (+0)")

    # ── Liquidez estructural: 9 pts ──────────────────────────────────────
    close_liq = [l for l in liq_levels if l["dist"] < 15]
    if close_liq:
        fuerza = close_liq[0].get("fuerza", "MEDIA")
        pts = 9 if fuerza == "MUY ALTA" else 6 if fuerza == "ALTA" else 4
        score += pts
        reasons.append(f"🎯 Liquidez {fuerza}: {close_liq[0]['tipo']} (+{pts})")

    return min(score, 100), reasons

def score_label(score):
    if score >= 80:   return "🔥 SEÑAL FUERTE",  "green"
    elif score >= 65: return "✅ SEÑAL VÁLIDA",   "lightgreen"
    elif score >= 50: return "⚠️ SEÑAL DÉBIL",    "orange"
    else:             return "❌ NO OPERAR",       "red"

# ============================================
# NIVELES SCALPING
# ============================================
def find_support_resistance(df, lookback=20):
    if df.empty or len(df) < lookback: return None, None
    recent = df.tail(lookback)
    return scalar(recent["Low"].min()), scalar(recent["High"].max())

def calc_scalp_levels(price, direction, df=None, atr_pips=None, liq_levels=None):
    if price is None or direction not in ("LONG", "SHORT"):
        return None, None, None, None, None, []
    # Ratio mínimo 1:2.8 (objetivo 1:3) — necesario para ser rentable con 40% WR
    MIN_RATIO = 3.0  # Ratio fijo 1:3 (2.8 era variable/random — causaba inconsistencia)
    MAX_SL = 12  # SL máximo 12 pips
    support, resistance = None, None
    liquidity_warnings = []
    strong_levels_near_tp = 0

    # Buscar niveles de liquidez cercanos primero
    if liq_levels:
        strong_levels_near_tp = 0
        for level in liq_levels[:8]:  # Revisar más niveles
            lvl_price = level.get("nivel")
            fuerza = level.get("fuerza", "MEDIA")
            if lvl_price and abs(lvl_price - price) / PIP <= 30:  # Dentro de 30 pips
                if lvl_price < price and (support is None or lvl_price > support):
                    support = lvl_price
                elif lvl_price > price and (resistance is None or lvl_price < resistance):
                    resistance = lvl_price

                # Contar niveles fuertes cerca del área objetivo
                if fuerza in ["ALTA", "MUY ALTA"]:
                    if direction == "LONG" and lvl_price > price:
                        # Estimar área objetivo aproximada
                        sl_estimate = support or (price - MAX_SL * PIP)
                        risk_estimate = price - sl_estimate
                        target_area = price + risk_estimate * MIN_RATIO
                        if abs(lvl_price - target_area) / PIP <= 20:
                            strong_levels_near_tp += 1
                    elif direction == "SHORT" and lvl_price < price:
                        sl_estimate = resistance or (price + MAX_SL * PIP)
                        risk_estimate = sl_estimate - price
                        target_area = price - risk_estimate * MIN_RATIO
                        if abs(lvl_price - target_area) / PIP <= 20:
                            strong_levels_near_tp += 1

        # Generar advertencias basadas en liquidez
        if strong_levels_near_tp >= 2:
            liquidity_warnings.append("⚠️ Alta liquidez cerca del TP - posible resistencia fuerte")
        elif strong_levels_near_tp == 1:
            liquidity_warnings.append("⚠️ Liquidez moderada cerca del TP")

    # Si no hay liquidez buena, usar soporte/resistencia del precio
    if support is None or resistance is None:
        if df is not None and not df.empty:
            support, resistance = find_support_resistance(df)

    if direction == "LONG":
        # SL: usar soporte cercano o máximo 17 pips
        sl = max(
            support * 0.998 if support and support < price else price - MAX_SL * PIP,
            price - MAX_SL * PIP
        )
        risk = price - sl
        tp   = price + risk * MIN_RATIO

        # Ajustar TP si hay mucha liquidez fuerte cerca
        if strong_levels_near_tp >= 2 and resistance and resistance > price:
            # Ser más conservador - usar un TP más cercano (entre 1.5:1 y 2:1)
            conservative_ratio = 1.5 + np.random.random() * 0.5
            conservative_tp = price + risk * conservative_ratio
            if conservative_tp < resistance:
                tp = conservative_tp
                liquidity_warnings.append("🎯 TP ajustado por alta liquidez (más conservador)")

        # Ajustar TP a resistencia cercana si está dentro del rango objetivo
        elif resistance and resistance > price:
            target_tp = price + risk * MIN_RATIO
            if abs(resistance - target_tp) / PIP <= 15:  # Dentro de 15 pips del target
                tp = resistance * 1.0002
    else:  # SHORT
        # SL: usar resistencia cercana o máximo 17 pips
        sl = min(
            resistance * 1.002 if resistance and resistance > price else price + MAX_SL * PIP,
            price + MAX_SL * PIP
        )
        risk = sl - price
        tp   = price - risk * MIN_RATIO

        # Ajustar TP si hay mucha liquidez fuerte cerca
        if strong_levels_near_tp >= 2 and support and support < price:
            # Ser más conservador - usar un TP más cercano (entre 1.5:1 y 2:1)
            conservative_ratio = 1.5 + np.random.random() * 0.5
            conservative_tp = price - risk * conservative_ratio
            if conservative_tp > support:
                tp = conservative_tp
                liquidity_warnings.append("🎯 TP ajustado por alta liquidez (más conservador)")

        # Ajustar TP a soporte cercano si está dentro del rango objetivo
        elif support and support < price:
            target_tp = price - risk * MIN_RATIO
            if abs(support - target_tp) / PIP <= 15:  # Dentro de 15 pips del target
                tp = support * 0.9998

    risk_pips   = abs(price - sl) / PIP
    profit_pips = abs(tp - price) / PIP
    rr     = profit_pips / risk_pips if risk_pips > 0 else None
    viable = (atr_pips >= risk_pips / SCALP_MAX_HOLD) if atr_pips else None
    return tp, sl, rr, viable, risk_pips, liquidity_warnings

# ============================================
# ESTRUCTURA DE MERCADO Y MANIPULACIÓN (SMC)
# ============================================
def detect_stop_hunt(df, lookback=20):
    """Detecta stop hunts: barridos de liquidez con reversión rápida."""
    if df.empty or len(df) < lookback + 3:
        return []
    events = []
    recent = df.tail(lookback + 3)
    for i in range(2, len(recent) - 1):
        candle = recent.iloc[i]
        prev   = recent.iloc[max(0, i - lookback):i]
        if prev.empty:
            continue
        key_high = float(prev["High"].max())
        key_low  = float(prev["Low"].min())
        body     = abs(float(candle["Close"]) - float(candle["Open"]))
        rng      = float(candle["High"]) - float(candle["Low"])
        if rng < 1e-6:
            continue
        upper_wick = float(candle["High"]) - max(float(candle["Open"]), float(candle["Close"]))
        lower_wick = min(float(candle["Open"]), float(candle["Close"])) - float(candle["Low"])
        ref_body = max(body, 0.0001)
        if (float(candle["High"]) > key_high * 1.0001 and
                float(candle["Close"]) < key_high and
                upper_wick > ref_body * 1.5):
            events.append({
                "tipo": "BEAR STOP HUNT", "nivel": round(key_high, 5),
                "precio": round(float(candle["High"]), 5), "emoji": "🐻",
                "señal": "SHORT",
                "descripcion": f"Barrido alcista en {key_high:.5f} — posible SHORT",
                "fuerza": "ALTA" if upper_wick > ref_body * 2.5 else "MEDIA"
            })
        if (float(candle["Low"]) < key_low * 0.9999 and
                float(candle["Close"]) > key_low and
                lower_wick > ref_body * 1.5):
            events.append({
                "tipo": "BULL STOP HUNT", "nivel": round(key_low, 5),
                "precio": round(float(candle["Low"]), 5), "emoji": "🐂",
                "señal": "LONG",
                "descripcion": f"Barrido bajista en {key_low:.5f} — posible LONG",
                "fuerza": "ALTA" if lower_wick > ref_body * 2.5 else "MEDIA"
            })
    return events[-3:]


def detect_market_structure(df, lookback=60):
    """Identifica estructura: HH/HL (alcista), LH/LL (bajista), BOS y ChoCH."""
    empty = {"estructura": "INDEFINIDA", "tendencia": "LATERAL",
             "bos": [], "choch": [], "score": 50, "last_sh": None, "last_sl": None}
    if df.empty or len(df) < 10:
        return empty
    recent = df.tail(min(lookback, len(df)))
    highs, lows = recent["High"].values, recent["Low"].values
    swing_highs, swing_lows = [], []
    for i in range(3, len(highs) - 3):
        if (highs[i] > highs[i-1] and highs[i] > highs[i-2] and
                highs[i] > highs[i+1] and highs[i] > highs[i+2]):
            swing_highs.append(float(highs[i]))
        if (lows[i] < lows[i-1] and lows[i] < lows[i-2] and
                lows[i] < lows[i+1] and lows[i] < lows[i+2]):
            swing_lows.append(float(lows[i]))
    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return empty
    sh3 = swing_highs[-3:] if len(swing_highs) >= 3 else swing_highs
    sl3 = swing_lows[-3:]  if len(swing_lows)  >= 3 else swing_lows
    hh = len(sh3) >= 2 and all(sh3[i] < sh3[i+1] for i in range(len(sh3)-1))
    hl = len(sl3) >= 2 and all(sl3[i] < sl3[i+1] for i in range(len(sl3)-1))
    ll = len(sl3) >= 2 and all(sl3[i] > sl3[i+1] for i in range(len(sl3)-1))
    lh = len(sh3) >= 2 and all(sh3[i] > sh3[i+1] for i in range(len(sh3)-1))
    bos, choch = [], []
    if hh and hl:
        estructura, tendencia, score = "HH/HL — ALCISTA", "ALCISTA", 75
        bos.append(f"BOS ALCISTA: Nuevo HH en {sh3[-1]:.5f}")
    elif ll and lh:
        estructura, tendencia, score = "LH/LL — BAJISTA", "BAJISTA", 25
        bos.append(f"BOS BAJISTA: Nuevo LL en {sl3[-1]:.5f}")
    elif hh and not hl:
        estructura, tendencia, score = "HH/LH — AGOTAMIENTO ALCISTA", "LATERAL", 55
        choch.append("ChoCH: HH sin HL — posible cambio de tendencia")
    elif ll and not lh:
        estructura, tendencia, score = "LL/HL — AGOTAMIENTO BAJISTA", "LATERAL", 45
        choch.append("ChoCH: LL sin LH — posible cambio de tendencia")
    else:
        estructura, tendencia, score = "INDEFINIDA", "LATERAL", 50
    return {"estructura": estructura, "tendencia": tendencia, "bos": bos, "choch": choch,
            "score": score,
            "last_sh": round(sh3[-1], 5) if sh3 else None,
            "last_sl": round(sl3[-1], 5) if sl3 else None}


def detect_volume_absorption(df, vol_window=20):
    """Absorción institucional: alta actividad con poco movimiento de precio."""
    if df.empty or "Close" not in df.columns or len(df) < vol_window:
        return None
    vol      = _ensure_volume(df)
    avg_vol  = float(vol.tail(vol_window).mean())
    last_vol = float(vol.iloc[-1])
    body = abs(float(df["Close"].iloc[-1]) - float(df["Open"].iloc[-1]))
    rng  = float(df["High"].iloc[-1]) - float(df["Low"].iloc[-1])
    if avg_vol <= 0 or rng <= 0:
        return None
    vol_ratio  = last_vol / avg_vol
    body_ratio = body / rng
    if vol_ratio >= 1.5 and body_ratio < 0.3:
        side = "COMPRADORA" if float(df["Close"].iloc[-1]) >= float(df["Open"].iloc[-1]) else "VENDEDORA"
        return {
            "tipo": f"ABSORCIÓN {side}", "vol_ratio": round(vol_ratio, 2),
            "body_ratio": round(body_ratio, 2),
            "descripcion": f"Actividad {vol_ratio:.1f}x con mecha grande — institucional absorbiendo",
            "sesgo": "LONG" if side == "COMPRADORA" else "SHORT",
            "fuerza": "ALTA" if vol_ratio >= 2.5 else "MEDIA",
        }
    return None


def ai_candlestick_patterns(df):
    """Reconocimiento de patrones de velas con scoring ponderado."""
    if df.empty or len(df) < 3:
        return [], 0
    patterns, score = [], 0
    last = df.iloc[-1]; prev = df.iloc[-2]
    o, c, h, l = float(last["Open"]), float(last["Close"]), float(last["High"]), float(last["Low"])
    po, pc = float(prev["Open"]), float(prev["Close"])
    body = abs(c - o); prev_body = abs(pc - po); rng = h - l
    if rng < 1e-6:
        return patterns, score
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l
    ref = max(body, 0.0001)
    if lower_wick > ref * 2 and lower_wick > upper_wick * 2 and body < rng * 0.35:
        patterns.append({"patron": "HAMMER / PIN BAR ALCISTA", "emoji": "🔨", "sesgo": "LONG",  "peso": 15}); score += 15
    if upper_wick > ref * 2 and upper_wick > lower_wick * 2 and body < rng * 0.35:
        patterns.append({"patron": "SHOOTING STAR BAJISTA",    "emoji": "💫", "sesgo": "SHORT", "peso": 15}); score -= 15
    if pc < po and c > o and c > po and o < pc and prev_body > 0:
        patterns.append({"patron": "BULLISH ENGULFING",        "emoji": "🟢", "sesgo": "LONG",  "peso": 20}); score += 20
    if pc > po and c < o and c < po and o > pc and prev_body > 0:
        patterns.append({"patron": "BEARISH ENGULFING",        "emoji": "🔴", "sesgo": "SHORT", "peso": 20}); score -= 20
    if body < rng * 0.1:
        patterns.append({"patron": "DOJI — INDECISIÓN",        "emoji": "➕", "sesgo": "NEUTRAL","peso": 0})
    if c > o and upper_wick < body * 0.1 and lower_wick < body * 0.1 and body > rng * 0.85:
        patterns.append({"patron": "MARUBOZU ALCISTA",         "emoji": "🚀", "sesgo": "LONG",  "peso": 25}); score += 25
    if c < o and upper_wick < body * 0.1 and lower_wick < body * 0.1 and body > rng * 0.85:
        patterns.append({"patron": "MARUBOZU BAJISTA",         "emoji": "📉", "sesgo": "SHORT", "peso": 25}); score -= 25
    if h < float(prev["High"]) and l > float(prev["Low"]):
        patterns.append({"patron": "INSIDE BAR — CONTRACCIÓN", "emoji": "📦", "sesgo": "NEUTRAL","peso": 5})
    return patterns, score


def calculate_trend_strength(df, period=14):
    """Fuerza de tendencia tipo ADX (sin dependencias externas)."""
    empty = {"adx": 0, "tendencia": "LATERAL", "fuerza": "DÉBIL",
             "score": 50, "plus_di": 0, "minus_di": 0}
    if df.empty or len(df) < period + 5:
        return empty
    high, low, close = df["High"], df["Low"], df["Close"]
    tr = pd.concat([high - low, (high - close.shift()).abs(),
                    (low - close.shift()).abs()], axis=1).max(axis=1)
    hd, ld    = high.diff(), low.diff()
    plus_dm   = hd.where((hd > (-ld)) & (hd > 0), 0.0)
    minus_dm  = (-ld).where((-ld > hd) & (ld < 0), 0.0)
    atr14     = tr.rolling(period).mean().replace(0, np.nan)
    plus_di   = 100 * plus_dm.rolling(period).mean()  / atr14
    minus_di  = 100 * minus_dm.rolling(period).mean() / atr14
    dx        = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx_val   = last_scalar(dx.rolling(period).mean()) or 0
    plus_val  = last_scalar(plus_di) or 0
    minus_val = last_scalar(minus_di) or 0
    fuerza    = "FUERTE" if adx_val >= 30 else ("MODERADA" if adx_val >= 20 else "DÉBIL")
    if plus_val > minus_val and adx_val >= 20:
        tendencia, score = "ALCISTA", min(50 + int(adx_val), 90)
    elif minus_val > plus_val and adx_val >= 20:
        tendencia, score = "BAJISTA", max(50 - int(adx_val), 10)
    else:
        tendencia, score = "LATERAL", 50
    return {"adx": round(adx_val, 1), "plus_di": round(plus_val, 1),
            "minus_di": round(minus_val, 1), "tendencia": tendencia,
            "fuerza": fuerza, "score": score}


def calc_smart_tp_sl(price, direction, df=None, liq_levels=None,
                     market_structure=None, atr_pips=None):
    """TP1/TP2/TP3 y SL inteligentes basados en estructura, ATR y liquidez."""
    if price is None or direction not in ("LONG", "SHORT"):
        return None, None, None, None, None, None, None, []
    warnings_out = []
    atr = max(min((atr_pips or 10) * PIP, 0.0020), 0.0005)
    sl = None
    if market_structure:
        if direction == "LONG" and market_structure.get("last_sl"):
            cand = market_structure["last_sl"] - atr * 0.5
            if 3 <= abs(price - cand) / PIP <= 25:
                sl = cand
                warnings_out.append(f"SL en swing low {market_structure['last_sl']:.5f}")
        elif direction == "SHORT" and market_structure.get("last_sh"):
            cand = market_structure["last_sh"] + atr * 0.5
            if 3 <= abs(price - cand) / PIP <= 25:
                sl = cand
                warnings_out.append(f"SL en swing high {market_structure['last_sh']:.5f}")
    if sl is None and liq_levels:
        for level in liq_levels[:5]:
            lvl = level.get("nivel")
            if not lvl:
                continue
            dist = abs(price - lvl) / PIP
            if direction == "LONG" and lvl < price and 3 <= dist <= 20:
                sl = lvl - atr * 0.3; break
            elif direction == "SHORT" and lvl > price and 3 <= dist <= 20:
                sl = lvl + atr * 0.3; break
    if sl is None:
        sl = price - atr * 1.2 if direction == "LONG" else price + atr * 1.2
    risk = abs(price - sl)
    risk_pips = risk / PIP
    if direction == "LONG":
        tp1, tp2, tp3 = price + risk, price + risk * 2.0, price + risk * 3.0
    else:
        tp1, tp2, tp3 = price - risk, price - risk * 2.0, price - risk * 3.0
    if liq_levels:
        for level in liq_levels[:6]:
            lvl = level.get("nivel")
            if not lvl:
                continue
            if direction == "LONG" and lvl > price and abs(lvl - tp2) / PIP <= 15:
                tp2 = lvl * 0.9999; warnings_out.append(f"TP2 en liquidez {lvl:.5f}"); break
            elif direction == "SHORT" and lvl < price and abs(lvl - tp2) / PIP <= 15:
                tp2 = lvl * 1.0001; warnings_out.append(f"TP2 en liquidez {lvl:.5f}"); break
    rr2 = abs(tp2 - price) / risk if risk > 0 else 0
    return tp1, tp2, tp3, sl, rr2, risk_pips, atr / PIP, warnings_out


def ai_market_bias(signal_data, market_structures, vol_absorption, stop_hunts, patterns_score):
    """Motor de IA: pondera múltiples factores y produce bias final con confianza."""
    scores  = {"LONG": 0, "SHORT": 0}
    evidence = []
    for tf, ms in market_structures.items():
        if not ms:
            continue
        w = {"1d": 30, "4h": 25, "1h": 20, "15m": 15}.get(tf, 10)
        t = ms.get("tendencia", "LATERAL")
        e = ms.get("estructura", "?")
        if t == "ALCISTA":
            scores["LONG"]  += w; evidence.append(f"📈 Estructura {tf}: {e} (+{w})")
        elif t == "BAJISTA":
            scores["SHORT"] += w; evidence.append(f"📉 Estructura {tf}: {e} (+{w})")
    for sh in stop_hunts[-2:]:
        if sh["señal"] == "LONG":
            scores["LONG"]  += 20; evidence.append("🐂 Stop Hunt BULL detectado (+20)")
        elif sh["señal"] == "SHORT":
            scores["SHORT"] += 20; evidence.append("🐻 Stop Hunt BEAR detectado (+20)")
    if patterns_score > 10:
        w = min(patterns_score, 25)
        scores["LONG"]  += w; evidence.append(f"🕯️ Patrones alcistas (+{w})")
    elif patterns_score < -10:
        w = min(-patterns_score, 25)
        scores["SHORT"] += w; evidence.append(f"🕯️ Patrones bajistas (+{w})")
    if vol_absorption:
        if vol_absorption["sesgo"] == "LONG":
            scores["LONG"]  += 15; evidence.append("📦 Absorción compradora (+15)")
        elif vol_absorption["sesgo"] == "SHORT":
            scores["SHORT"] += 15; evidence.append("📦 Absorción vendedora (+15)")
    if signal_data.get("direction") == "LONG":
        scores["LONG"]  += 20
    elif signal_data.get("direction") == "SHORT":
        scores["SHORT"] += 20
    total     = scores["LONG"] + scores["SHORT"] + 1
    long_pct  = scores["LONG"]  / total * 100
    short_pct = scores["SHORT"] / total * 100
    if long_pct > short_pct + 10:
        bias, confidence = "LONG",  min(int(long_pct),  95)
    elif short_pct > long_pct + 10:
        bias, confidence = "SHORT", min(int(short_pct), 95)
    else:
        bias, confidence = "NEUTRAL", 50
    return {"bias": bias, "confidence": confidence,
            "long_score": int(scores["LONG"]), "short_score": int(scores["SHORT"]),
            "evidence": evidence}

# ============================================
# NOTICIAS — ESTIMACIÓN DE IMPACTO
# ============================================
def estimate_impact(title, description=""):
    if not title: return 0, "⚪ BAJO", "⚪", []
    text  = (title + " " + (description or "")).upper()
    score = 15; found = []
    groups = [
        {"FED DECISION": 95, "ECB DECISION": 95, "FOMC": 95, "RATE DECISION": 95,
         "INTEREST RATE DECISION": 98, "RATE HIKE": 92, "RATE CUT": 92,
         "MONETARY POLICY MEETING": 95, "POWELL SPEECH": 88, "LAGARDE SPEECH": 88,
         "INFLATION REPORT": 90, "EMPLOYMENT REPORT": 90, "JOBS REPORT": 90,
         "GDP REPORT": 88, "RECESSION": 92, "FINANCIAL CRISIS": 95,
         "BANKING COLLAPSE": 96, "CENTRAL BANK": 85, "EMERGENCY": 90,
         "WAR": 87, "MILITARY": 85, "SANCTIONS": 82, "TRADE WAR": 85, "TARIFF": 78},
        {"INFLATION": 72, "UNEMPLOYMENT": 75, "GDP": 78, "POLICY": 70,
         "ECONOMIC": 72, "STIMULUS": 75, "BAILOUT": 82, "CRISIS": 78,
         "BANKING": 78, "EURO": 65, "DOLLAR": 65, "CURRENCY": 60, "TRADE": 68},
        {"EARNINGS": 58, "FINANCIAL": 60, "ECONOMY": 68, "GROWTH": 62,
         "OIL": 62, "ENERGY": 60, "RATE": 65},
        {"NEWS": 25, "REPORT": 35, "STATEMENT": 40, "ANALYST": 35, "OPINION": 25},
    ]
    for g in groups:
        for kw, val in g.items():
            if kw in text: found.append(kw); score = max(score, val)
    if len(found) > 2:  score = min(score + 5, 100)
    if "FED" in text or "ECB" in text: score = min(score + 3, 100)
    if any(c.isdigit() for c in title): score = min(score + 2, 100)
    if len(title.split()) < 10: score = max(score - 5, 5)
    score = max(score, 5)
    if score >= 85:   return score, "🔴 CRÍTICO",  "🔴", found
    elif score >= 70: return score, "🟠 MUY ALTO", "🟠", found
    elif score >= 50: return score, "🟡 ALTO",     "🟡", found
    elif score >= 30: return score, "🟢 MEDIO",    "🟢", found
    return score, "⚪ BAJO", "⚪", found

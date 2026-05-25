import streamlit as st
import pandas as pd
import numpy as np
import requests
import os
from datetime import datetime

# ════════════════════════════════════════════════════════════════════════════════
# CONFIG
# ════════════════════════════════════════════════════════════════════════════════

MT5_API_URL = os.environ.get("MT5_API_URL", "http://localhost:5000")
SYMBOL = "EURUSD"
PIP = 0.0001
SCALP_TP_PIPS = 30
SCALP_SL_PIPS = 12

# ════════════════════════════════════════════════════════════════════════════════
# MT5 REMOTE API
# ════════════════════════════════════════════════════════════════════════════════

def get_mt5_tick():
    """Obtiene tick actual desde MT5 local via API"""
    try:
        r = requests.get(f"{MT5_API_URL}/api/tick/{SYMBOL}", timeout=5)
        if r.status_code == 200:
            data = r.json()
            return {
                "bid": data["bid"],
                "ask": data["ask"],
                "spread_pips": data["spread_pips"],
                "time": datetime.fromisoformat(data["time"])
            }
    except Exception as e:
        st.error(f"❌ Error MT5: {e}")
    return None

def get_mt5_account():
    """Obtiene info de cuenta"""
    try:
        r = requests.get(f"{MT5_API_URL}/api/account", timeout=5)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        st.error(f"❌ Error MT5: {e}")
    return None

def get_mt5_candles(symbol=SYMBOL, tf="1h", count=200):
    """Obtiene velas históricas"""
    try:
        r = requests.get(f"{MT5_API_URL}/api/candles/{symbol}/{tf}/{count}", timeout=10)
        if r.status_code == 200:
            data = r.json()
            df = pd.DataFrame(data["candles"])
            df["time"] = pd.to_datetime(df["time"])
            df = df.set_index("time")
            return df[["open", "high", "low", "close", "volume"]].rename(
                columns={"open": "Open", "high": "High", "low": "Low", "close": "Close", "volume": "Volume"}
            )
    except Exception as e:
        st.error(f"❌ Error MT5: {e}")
    return pd.DataFrame()

def place_mt5_order(symbol, direction, volume, price, sl, tp):
    """Coloca orden en MT5"""
    try:
        r = requests.post(
            f"{MT5_API_URL}/api/order",
            json={
                "symbol": symbol,
                "direction": direction,
                "volume": volume,
                "price": price,
                "sl": sl,
                "tp": tp
            },
            timeout=10
        )
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        st.error(f"❌ Error MT5: {e}")
    return None

def get_mt5_positions():
    """Obtiene posiciones abiertas"""
    try:
        r = requests.get(f"{MT5_API_URL}/api/positions", timeout=5)
        if r.status_code == 200:
            return r.json().get("positions", [])
    except Exception as e:
        st.error(f"❌ Error MT5: {e}")
    return []

# ════════════════════════════════════════════════════════════════════════════════
# ANÁLISIS TÉCNICO
# ════════════════════════════════════════════════════════════════════════════════

def calculate_indicators(df):
    """Calcula EMA, RSI, MACD"""
    if df.empty or len(df) < 26:
        return {}
    
    close = df["Close"]
    
    # EMAs
    ema9 = close.ewm(span=9, adjust=False).mean()
    ema21 = close.ewm(span=21, adjust=False).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()
    
    # RSI
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    
    # MACD
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    hist = macd - signal
    
    return {
        "ema9": ema9.iloc[-1],
        "ema21": ema21.iloc[-1],
        "ema50": ema50.iloc[-1],
        "rsi": rsi.iloc[-1],
        "macd_hist": hist.iloc[-1],
        "close": close.iloc[-1]
    }

def generate_signal(df, tick):
    """Genera señal de trading"""
    if df.empty or not tick:
        return None
    
    ind = calculate_indicators(df)
    if not ind:
        return None
    
    price = tick["bid"]
    e9 = ind["ema9"]
    e21 = ind["ema21"]
    e50 = ind["ema50"]
    rsi = ind["rsi"]
    hist = ind["macd_hist"]
    
    # Lógica simple
    if e9 > e21 > e50 and hist > 0 and 42 <= rsi <= 73:
        return {
            "direction": "LONG",
            "price": price,
            "sl": price - SCALP_SL_PIPS * PIP,
            "tp": price + SCALP_TP_PIPS * PIP,
            "rr": SCALP_TP_PIPS / SCALP_SL_PIPS
        }
    elif e9 < e21 < e50 and hist < 0 and 27 <= rsi <= 58:
        return {
            "direction": "SHORT",
            "price": price,
            "sl": price + SCALP_SL_PIPS * PIP,
            "tp": price - SCALP_TP_PIPS * PIP,
            "rr": SCALP_TP_PIPS / SCALP_SL_PIPS
        }
    
    return None

# ════════════════════════════════════════════════════════════════════════════════
# UI STREAMLIT
# ════════════════════════════════════════════════════════════════════════════════

st.set_page_config(page_title="SMC Pro — MT5", page_icon="⚡", layout="wide")
st.title("⚡ SMC Pro — EUR/USD Trading Bot")

# Sidebar
with st.sidebar:
    st.header("⚙️ Configuración")
    
    mt5_url = st.text_input("MT5 API URL", value=MT5_API_URL)
    volume = st.number_input("Volumen (lotes)", 0.01, 1.0, 0.01, 0.01)
    
    if st.button("🔗 Conectar MT5"):
        acct = get_mt5_account()
        if acct:
            st.success(f"✅ Conectado: {acct['name']}")
            st.write(f"Balance: {acct['balance']:.2f} {acct['currency']}")
        else:
            st.error("❌ No se pudo conectar a MT5")

# Main panel
col1, col2 = st.columns([2, 1])

with col1:
    st.subheader("📊 Análisis EUR/USD 1H")
    
    if st.button("🔍 ANALIZAR", type="primary", use_container_width=True):
        with st.spinner("Analizando..."):
            tick = get_mt5_tick()
            df = get_mt5_candles(SYMBOL, "1h", 200)
            
            if tick and not df.empty:
                signal = generate_signal(df, tick)
                
                # Mostrar precio
                st.metric("Precio EUR/USD", f"{tick['bid']:.5f}", f"Spread: {tick['spread_pips']} pips")
                
                # Mostrar señal
                if signal:
                    direction = signal["direction"]
                    color = "🟢" if direction == "LONG" else "🔴"
                    
                    st.markdown(f"## {color} {direction}")
                    
                    col_e, col_sl, col_tp, col_rr = st.columns(4)
                    col_e.metric("Entrada", f"{signal['price']:.5f}")
                    col_sl.metric("SL", f"{signal['sl']:.5f}")
                    col_tp.metric("TP", f"{signal['tp']:.5f}")
                    col_rr.metric("R:R", f"1:{signal['rr']:.2f}")
                    
                    # Botón ejecutar
                    if st.button("🚀 EJECUTAR ORDEN", type="primary", use_container_width=True):
                        result = place_mt5_order(
                            SYMBOL, direction, volume,
                            signal["price"], signal["sl"], signal["tp"]
                        )
                        if result:
                            st.success(f"✅ Orden ejecutada: #{result.get('order')}")
                        else:
                            st.error("❌ Error ejecutando orden")
                else:
                    st.info("⚪ Sin señal clara en este momento")
            else:
                st.error("❌ No se pudieron obtener datos")

with col2:
    st.subheader("📍 Posiciones Abiertas")
    
    positions = get_mt5_positions()
    if positions:
        for p in positions:
            profit_color = "🟢" if p["profit"] > 0 else "🔴"
            st.write(f"{profit_color} #{p['ticket']} {p['type']} {p['volume']}L")
            st.write(f"Entrada: {p['price_open']:.5f}")
            st.write(f"P&L: ${p['profit']:.2f}")
            st.divider()
    else:
        st.info("Sin posiciones abiertas")

# Footer
st.divider()
st.caption("⚠️ Solo informativo. Usa siempre SL. No es consejo financiero.")


from functools import lru_cache
# Cargas pesadas y opcionales se realizan de forma perezosa (lazy) dentro de helpers
# para acelerar el import/reload de la aplicación Streamlit.
_st = None
_requests = None
_textblob = None

def get_streamlit():
    global _st
    if _st is None:
        try:
            import streamlit as st
            _st = st
        except Exception:
            _st = False
    return _st

def get_requests():
    global _requests
    if _requests is None:
        try:
            import requests as requests_module
            _requests = requests_module
        except Exception:
            _requests = False
    return _requests

def get_textblob():
    global _textblob
    if _textblob is None:
        try:
            from textblob import TextBlob as TB
            _textblob = TB
        except Exception:
            _textblob = False
    return _textblob

# Small in-memory caches for JSON files to avoid frequent disk I/O
_POSITION_CACHE = None  # (ts, data)
_TRADES_CACHE = None    # (ts, data)
_JSON_CACHE_TTL = None
from datetime import datetime, timedelta
_JSON_CACHE_TTL = timedelta(seconds=5)
import pandas as pd
import numpy as np
import logging
import time
import feedparser
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
import random
import sys
import importlib.util

# Lazy-load MetaTrader5 / yfinance
_mt5 = None
_yf = None
_mt5_import_error = None

def get_mt5():
    global _mt5, _mt5_import_error
    if _mt5 is None:
        try:
            import MetaTrader5 as mt5_module
            _mt5 = mt5_module
            globals()['mt5'] = _mt5
            _mt5_import_error = None
        except Exception as e:
            _mt5_import_error = str(e)
            logging.warning(f"MetaTrader5 import error: {_mt5_import_error}")
            _mt5 = False
    return _mt5

def is_mt5_available():
    return get_mt5() not in (None, False)

def get_mt5_error():
    """Devuelve el último error de importación o conexión de MT5."""
    if _mt5_import_error:
        return f"MetaTrader5 import error: {_mt5_import_error}"
    return _mt5_error_message

def get_yf():
    global _yf
    if _yf is None:
        try:
            import yfinance as yf_module
            _yf = yf_module
            globals()['yf'] = _yf
        except Exception:
            _yf = False
    return _yf

def is_yf_available():
    return get_yf() not in (None, False)

logging.basicConfig(level=logging.WARNING)

"""
DOCUMENTACIÓN RÁPIDA — `smc_pro_app.py`

- Modo simulación: si el terminal MetaTrader5 tiene el trading deshabilitado
    (`terminal_info().trade_allowed` es False) o AutoTrading está apagado, la
    función `place_mt5_order()` no fallará: devolverá un resultado simulado
    (ticket aleatorio) para permitir pruebas locales sin operaciones reales.

- Comportamiento en `auto_trade_signal()`: si no puede calcular TP/SL mediante
    `calc_scalp_levels`, aplica TP/SL por defecto (configurables con
    `SCALP_TP_PIPS` / `SCALP_SL_PIPS`). Si el terminal no permite trading, el
    flujo entra en modo simulación y registra un ticket simulado.

- Cómo habilitar trading real en MT5 (pasos locales):
    1) Abrir la terminal MetaTrader 5 en la misma máquina.
    2) Activar 'AutoTrading' en la barra de herramientas del terminal.
    3) En `Tools -> Options -> Expert Advisors`, permitir operaciones de
         EAs/DLLs/web requests según la configuración del bróker.
    4) Confirmar en Python que `mt5.terminal_info().trade_allowed` es True.

Esta documentación es orientativa; para ejecutar órdenes reales asegúrate de
usar credenciales y configuración apropiadas y de probar primero en cuenta
demo.
"""

# ============================================
# CONFIGURACIÓN
# ============================================
NEWS_API_KEY   = "0091d5b9d2dc46b4b907d04f5b66cee7"
SCALP_TP_PIPS  = 30  # SL 12p * 2.5 ratio promedio
SCALP_SL_PIPS  = 12  # Máximo 12 pips de stop loss
SCALP_MAX_HOLD = 3
PIP            = 0.0001
SYMBOL         = "EURUSD"

TELEGRAM_TOKEN   = "7967414683:AAGmyLDjobQOvpU_OVzlwHJ-Tf1o9GjbIlE"
TELEGRAM_CHAT_ID = "1442582228"

# Configuración persistente de usuario
USER_CONFIG_FILE = "user_config.json"

# Sistema de posiciones definitivas
POSITION_FILE = "position_state.json"
MIN_DEFINITIVE_SCORE = 70  # Score mínimo para considerar señal definitiva

# Configuración del Bot Automático
BOT_ENABLED = False
BOT_VOLUME = 0.01
BOT_LAST_SIGNAL = None

def load_position_state():
    """Carga el estado de posición desde archivo JSON"""
    global _POSITION_CACHE
    # Usar caché en memoria si está reciente
    if _POSITION_CACHE:
        ts, data = _POSITION_CACHE
        if datetime.now() - ts < _JSON_CACHE_TTL:
            return data
    try:
        with open(POSITION_FILE, 'r') as f:
            data = json.load(f)
            _POSITION_CACHE = (datetime.now(), data)
            return data
    except (FileNotFoundError, json.JSONDecodeError):
        data = {
            "is_open": False,
            "direction": None,
            "entry_price": None,
            "tp": None,
            "sl": None,
            "entry_time": None,
            "last_update": None,
            "score": 0,
            "be_alert_sent": False  # Para evitar múltiples alertas BE
        }
        _POSITION_CACHE = (datetime.now(), data)
        return data

def save_position_state(state):
    """Guarda el estado de posición en archivo JSON"""
    try:
        with open(POSITION_FILE, 'w') as f:
            json.dump(state, f, indent=2, default=str)
    except Exception as e:
        logging.warning(f"Error guardando estado de posición: {e}")
    # Invalidar caché
    global _POSITION_CACHE
    _POSITION_CACHE = (datetime.now(), state)


def load_user_config():
    """Carga credenciales y configuración persistente del usuario."""
    try:
        with open(USER_CONFIG_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def save_user_config(cfg):
    """Guarda la configuración persistente del usuario."""
    try:
        with open(USER_CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        logging.warning(f"Error guardando configuración de usuario: {e}")

def check_position_status(current_price=None):
    """Verifica si la posición abierta alcanzó TP, SL o BE (1:1)"""
    if current_price is None:
        return False, None, False

    state = load_position_state()
    if not state["is_open"]:
        return False, None, False

    entry_price = state["entry_price"]
    direction = state["direction"]
    risk_amount = abs(entry_price - state["sl"])

    # Calcular beneficio actual
    if direction == "LONG":
        current_profit = current_price - entry_price
        if current_price >= state["tp"]:
            return True, "TP", False
        elif current_price <= state["sl"]:
            return True, "SL", False
    elif direction == "SHORT":
        current_profit = entry_price - current_price
        if current_price <= state["tp"]:
            return True, "TP", False
        elif current_price >= state["sl"]:
            return True, "SL", False

    # Verificar si alcanzó BE (1:1) y no se envió alerta aún
    if current_profit >= risk_amount and not state["be_alert_sent"]:
        return False, None, True  # No cerrar, pero enviar alerta BE

    return False, None, False

def close_position(reason="MANUAL"):
    """Cierra la posición abierta"""
    state = load_position_state()
    if state["is_open"]:
        # Calcular pips ganados/perdidos
        entry_price = state["entry_price"]
        exit_price = state["tp"] if reason == "TP" else state["sl"] if reason == "SL" else entry_price
        pips = (exit_price - entry_price) / PIP if state["direction"] == "LONG" else (entry_price - exit_price) / PIP

        # Crear señal para registro
        signal = {
            "direction": state["direction"],
            "price": state["entry_price"],
            "tp": state["tp"],
            "sl": state["sl"],
            "score": state["score"]
        }

        state["is_open"] = False
        state["be_alert_sent"] = False  # Reset para próxima posición
        state["last_update"] = datetime.now()
        save_position_state(state)

        # Registrar cierre de posición
        log_trade_operation("CLOSE", signal, reason, pips)

        # Enviar alerta de cierre por Telegram
        send_telegram_alert(signal, state["score"], reason=f"CLOSED_{reason}")

        return True
    return False

def open_definitive_position(signal, score):
    """Abre una posición definitiva"""
    if score < MIN_DEFINITIVE_SCORE:
        return False

    # Verificar si ya hay posición abierta
    state = load_position_state()
    if state["is_open"]:
        return False  # Ya hay posición abierta

    # Abrir nueva posición
    state.update({
        "is_open": True,
        "direction": signal["direction"],
        "entry_price": signal["price"],
        "tp": signal["tp"],
        "sl": signal["sl"],
        "entry_time": datetime.now(),
        "last_update": datetime.now(),
        "score": score,
        "be_alert_sent": False
    })
    save_position_state(state)

    # Registrar apertura de posición
    log_trade_operation("OPEN", signal)

    return True

def send_be_alert(signal):
    """Envía alerta de Break Even y marca como enviada"""
    if send_telegram_alert(signal, 0, reason="BE"):
        # Marcar que la alerta BE ya se envió
        state = load_position_state()
        state["be_alert_sent"] = True
        save_position_state(state)

        # Registrar BE alcanzado
        risk_pips = abs(signal["price"] - signal["sl"]) / PIP
        log_trade_operation("BE", signal, "BE", risk_pips)

        return True
    return False

# Sistema de registro de operaciones
TRADES_LOG_FILE = "trades_history.json"

def load_trades_history():
    """Carga el historial de operaciones desde archivo JSON"""
    global _TRADES_CACHE
    if _TRADES_CACHE:
        ts, data = _TRADES_CACHE
        if datetime.now() - ts < _JSON_CACHE_TTL:
            return data
    try:
        with open(TRADES_LOG_FILE, 'r') as f:
            data = json.load(f)
            _TRADES_CACHE = (datetime.now(), data)
            return data
    except (FileNotFoundError, json.JSONDecodeError):
        data = []
        _TRADES_CACHE = (datetime.now(), data)
        return data

def save_trades_history(trades):
    """Guarda el historial de operaciones en archivo JSON"""
    try:
        with open(TRADES_LOG_FILE, 'w') as f:
            json.dump(trades, f, indent=2, default=str)
    except Exception as e:
        logging.warning(f"Error guardando historial de trades: {e}")
    # Invalidar caché
    global _TRADES_CACHE
    _TRADES_CACHE = (datetime.now(), trades)

def log_trade_operation(operation_type, signal=None, outcome=None, pips=None):
    """Registra una operación en el historial"""
    trade = {
        "timestamp": datetime.now(),
        "type": operation_type,  # "OPEN", "CLOSE", "BE"
        "direction": signal.get("direction") if signal else None,
        "entry_price": signal.get("price") if signal else None,
        "tp": signal.get("tp") if signal else None,
        "sl": signal.get("sl") if signal else None,
        "outcome": outcome,  # "TP", "SL", "BE", "MANUAL"
        "pips": pips,
        "score": signal.get("score") if signal else None
    }

    trades = load_trades_history()
    trades.append(trade)

    # Mantener solo las últimas 1000 operaciones
    if len(trades) > 1000:
        trades = trades[-1000:]

    save_trades_history(trades)

# ============================================
# CACHE
# ============================================
CACHE_FILE     = "news_cache.json"
CACHE_DURATION = timedelta(minutes=15)

# Caché en memoria para datos históricos pequeños (evita recargas inmediatas)
_EURUSD_CACHE = {}
_EURUSD_CACHE_TTL = timedelta(seconds=30)

def load_cache():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r") as f:
                data = json.load(f)
                ts = datetime.fromisoformat(data["timestamp"])
                if datetime.now() - ts < CACHE_DURATION:
                    return data["news"]
        except Exception:
            pass
    return None

def save_cache(news):
    with open(CACHE_FILE, "w") as f:
        json.dump({"timestamp": datetime.now().isoformat(), "news": news}, f)

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
# MT5 — CONEXIÓN
# ============================================
_mt5_connected = False
_mt5_error_message = None

def mt5_connect(login=None, password=None, server=None):
    global _mt5_connected, _mt5_error_message
    _mt5_error_message = None
    mt5 = get_mt5()
    if mt5 is False:
        _mt5_error_message = "MetaTrader5 no está instalado"
        print(f"⚠️  {_mt5_error_message}")
        return False
    if _mt5_connected:
        return True
    try:
        # Si se proporcionan credenciales, usarlas
        if login and password:
            if not mt5.initialize():
                error_msg = mt5.last_error()
                _mt5_error_message = f"MT5 initialize() failed: {error_msg}"
                print(f"❌ {_mt5_error_message}")
                logging.warning(_mt5_error_message)
                return False
            
            # Intentar login con credenciales
            login_kwargs = {"login": int(login), "password": password}
            if server:
                login_kwargs["server"] = server
            if not mt5.login(**login_kwargs):
                error_msg = mt5.last_error()
                _mt5_error_message = f"MT5 login failed para usuario {login}: {error_msg}"
                print(f"❌ {_mt5_error_message}")
                logging.warning(_mt5_error_message)
                mt5.shutdown()
                return False
        else:
            # Conexión automática (MT5 ya abierto)
            if not mt5.initialize():
                error_msg = mt5.last_error()
                _mt5_error_message = f"MT5 initialize() failed: {error_msg}"
                print(f"❌ {_mt5_error_message}")
                logging.warning(_mt5_error_message)
                return False
        
        print("MT5 inicializado correctamente")
        _mt5_connected = True
        return True
    except Exception as e:
        _mt5_error_message = f"mt5_connect error: {e}"
        print(_mt5_error_message)
        logging.warning(_mt5_error_message)
        return False

def get_mt5_tf_map():
    mt5 = get_mt5()
    if mt5 and mt5 is not False:
        return {
            "1m":  mt5.TIMEFRAME_M1,
            "5m":  mt5.TIMEFRAME_M5,
            "15m": mt5.TIMEFRAME_M15,
            "1h":  mt5.TIMEFRAME_H1,
            "4h":  mt5.TIMEFRAME_H4,
            "1d":  mt5.TIMEFRAME_D1,
        }
    # Fallback (not used when MT5 no está disponible)
    return {
        "1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240, "1d": 1440
    }

def get_mt5_candles(symbol=SYMBOL, tf="1h", count=200):
    if not mt5_connect():
        return pd.DataFrame()
    try:
        tf_map = get_mt5_tf_map()
        timeframe = tf_map.get(tf, tf_map.get("1h"))
        mt5 = get_mt5()
        rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, count)
        if rates is None or len(rates) == 0:
            return pd.DataFrame()
        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s")
        df = df.set_index("time")
        df = df.rename(columns={
            "open": "Open", "high": "High", "low": "Low",
            "close": "Close", "tick_volume": "Volume"
        })
        return df[["Open", "High", "Low", "Close", "Volume"]]
    except Exception as e:
        logging.warning(f"get_mt5_candles: {e}")
        return pd.DataFrame()

def get_mt5_tick(symbol=SYMBOL):
    if not mt5_connect():
        return None
    try:
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            return None
        return {
            "bid":         tick.bid,
            "ask":         tick.ask,
            "spread_pips": round((tick.ask - tick.bid) / PIP, 1),
            "time":        datetime.fromtimestamp(tick.time)
        }
    except Exception as e:
        logging.warning(f"get_mt5_tick: {e}")
        return None

def get_mt5_account():
    if not mt5_connect():
        return None
    try:
        info = mt5.account_info()
        if info is None:
            return None
        return {
            "balance":     info.balance,
            "equity":      info.equity,
            "profit":      info.profit,
            "margin_free": info.margin_free,
            "leverage":    info.leverage,
            "currency":    info.currency,
            "server":      info.server,
            "name":        info.name,
        }
    except Exception as e:
        logging.warning(f"get_mt5_account: {e}")
        return None

def mt5_can_trade():
    """Devuelve (bool, mensaje) indicando si el terminal permite trading por API.
    Usa `mt5.terminal_info().trade_allowed` cuando está disponible.
    """
    try:
        mt5 = get_mt5()
        if mt5 is False:
            return False, "MetaTrader5 no disponible"
        info = mt5.terminal_info()
        if info is None:
            return False, "No se obtuvo terminal_info()"
        # Algunos terminales devuelven atributos con distinto nombre
        trade_allowed = getattr(info, "trade_allowed", None)
        if trade_allowed is False:
            return False, "Trading deshabilitado en terminal MT5 (habilita AutoTrading/Trade API)"
        if trade_allowed is None:
            return True, "Estado de trading desconocido — intentar orden"
        return True, "Trading permitido"
    except Exception as e:
        logging.warning(f"mt5_can_trade: {e}")
        return False, f"Error verificando terminal: {e}"

def get_mt5_ticks_volume(symbol=SYMBOL, minutes=60):
    """
    Obtiene volumen de ticks del último periodo.
    Devuelve un DataFrame con volumen por minuto.
    """
    if not mt5_connect():
        return pd.DataFrame()
    try:
        from_date = datetime.now() - timedelta(minutes=minutes)
        ticks = mt5.copy_ticks_from(symbol, from_date, 100000, mt5.COPY_TICKS_ALL)
        if ticks is None or len(ticks) == 0:
            return pd.DataFrame()
        df = pd.DataFrame(ticks)
        df["time"] = pd.to_datetime(df["time"], unit="s")
        df = df.set_index("time")
        # Agrupar por minuto
        vol_by_minute = df.resample("1min").size().rename("tick_count")
        return pd.DataFrame(vol_by_minute)
    except Exception as e:
        logging.warning(f"get_mt5_ticks_volume: {e}")
        return pd.DataFrame()

# ============================================
# MT5 TRADING BOT FUNCTIONS
# ============================================

def get_mt5_positions(symbol=SYMBOL):
    """Obtiene posiciones abiertas en MT5"""
    if not mt5_connect():
        return []
    positions = mt5.positions_get(symbol=symbol)
    return positions if positions else []

def get_mt5_orders(symbol=SYMBOL):
    """Obtiene órdenes pendientes en MT5"""
    if not mt5_connect():
        return []
    orders = mt5.orders_get(symbol=symbol)
    return orders if orders else []

def place_mt5_order(symbol, direction, volume, price, sl, tp, comment="SMC Pro Bot"):
    """Coloca una orden de mercado en MT5"""
    if not mt5_connect():
        return None
    # Verificar si el terminal permite trading
    ok, msg = mt5_can_trade()
    if not ok:
        logging.warning(f"place_mt5_order: simulando orden porque: {msg}")
        # Simular resultado de orden para que la aplicación pueda continuar
        class SimResult:
            def __init__(self, order_id):
                self.retcode = None
                self.order = order_id
                self.deal = 0
                self.volume = volume
                self.price = price
                self.comment = f"SIMULATED: {msg}"

        fake_order_id = random.randint(1000000, 9999999)
        return SimResult(fake_order_id)

    # Determinar tipo de orden
    order_type = mt5.ORDER_TYPE_BUY if direction == "LONG" else mt5.ORDER_TYPE_SELL

    # Preparar la solicitud
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": volume,
        "type": order_type,
        "price": price,
        "sl": sl,
        "tp": tp,
        "deviation": 10,  # slippage máximo en puntos
        "magic": 123456,  # número mágico para identificar órdenes del bot
        "comment": comment,
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    # Enviar orden
    result = mt5.order_send(request)
    return result

def close_mt5_position(position_ticket, volume=None, comment="SMC Pro Bot Close"):
    """Cierra una posición abierta en MT5"""
    if not mt5_connect():
        return None

    # Obtener información de la posición
    position = mt5.positions_get(ticket=position_ticket)
    if not position:
        return None
    position = position[0]

    # Determinar tipo de cierre (opuesto a la posición)
    if position.type == mt5.POSITION_TYPE_BUY:
        order_type = mt5.ORDER_TYPE_SELL
        price = mt5.symbol_info_tick(position.symbol).bid
    else:
        order_type = mt5.ORDER_TYPE_BUY
        price = mt5.symbol_info_tick(position.symbol).ask

    # Volumen a cerrar (todo si no se especifica)
    close_volume = volume if volume else position.volume

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "position": position_ticket,
        "symbol": position.symbol,
        "volume": close_volume,
        "type": order_type,
        "price": price,
        "deviation": 10,
        "magic": 123456,
        "comment": comment,
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    result = mt5.order_send(request)
    return result

def auto_trade_signal(signal, volume=0.01, liq_levels=None, check_windows=True):
    """Ejecuta automáticamente una señal de trading en MT5"""
    if not signal or not signal.get("direction"):
        return False, "Sin señal válida"

    # Verificar ventana horaria de trading
    if check_windows:
        try:
            in_win, win_label, win_eta = get_trading_window_info()
            if not in_win:
                return False, f"Fuera de horario — {win_label}. {win_eta}"
        except NameError:
            pass  # get_trading_window_info aún no disponible (primera carga)

    try:
        # Comprobar si el terminal permite trading. Si no, usar modo simulación.
        simulate = False
        ok, msg = (True, "MT5 no disponible")
        if is_mt5_available():
            ok, msg = mt5_can_trade()
        if not ok:
            logging.warning(f"auto_trade_signal: trade no permitido, entrando en modo SIMULACIÓN: {msg}")
            simulate = True

        # Verificar que no haya posiciones abiertas
        positions = get_mt5_positions()
        if positions:
            return False, f"Ya hay {len(positions)} posición(es) abierta(s)"

        # Obtener precio actual
        tick = get_mt5_tick()
        if not tick:
            return False, "No se pudo obtener precio actual"

        entry_price = tick['ask'] if signal['direction'] == 'LONG' else tick['bid']

        # Calcular SL y TP: si no vienen en la señal, generar con lógica institucional
        tp = signal.get('tp')
        sl = signal.get('sl')

        if not tp or not sl:
            # Intentar calcular usando niveles de scalping institucional
            try:
                df_1h = get_eurusd_data("1h")
                calc_tp, calc_sl, rr, viable, risk_pips, liquidity_warnings = calc_scalp_levels(
                    entry_price, signal['direction'], df=df_1h, atr_pips=signal.get('atr_1h_pips'), liq_levels=liq_levels)
                if calc_tp and calc_sl:
                    tp, sl = calc_tp, calc_sl
                    # Adjuntar al signal para registro y evitar recálculos
                    signal.update({"tp": tp, "sl": sl})
                    if liquidity_warnings:
                        logging.info(f"Auto-trade: ajustado TP/SL por liquidez: {liquidity_warnings}")
                else:
                    # Fallback a valores por defecto si el cálculo no devolvió niveles
                    logging.warning("calc_scalp_levels no devolvió TP/SL; usando valores por defecto")
                    if signal['direction'] == 'LONG':
                        sl = entry_price - SCALP_SL_PIPS * PIP
                        tp = entry_price + SCALP_TP_PIPS * PIP
                    else:
                        sl = entry_price + SCALP_SL_PIPS * PIP
                        tp = entry_price - SCALP_TP_PIPS * PIP
                    signal.update({"tp": tp, "sl": sl})
            except Exception as e:
                logging.warning(f"Error en calc_scalp_levels: {e}; usando valores por defecto")
                # Fallback a valores por defecto
                if signal['direction'] == 'LONG':
                    sl = entry_price - SCALP_SL_PIPS * PIP
                    tp = entry_price + SCALP_TP_PIPS * PIP
                else:
                    sl = entry_price + SCALP_SL_PIPS * PIP
                    tp = entry_price - SCALP_TP_PIPS * PIP
                signal.update({"tp": tp, "sl": sl})

        # Colocar orden
        result = place_mt5_order(
            symbol=SYMBOL,
            direction=signal['direction'],
            volume=volume,
            price=entry_price,
            sl=sl,
            tp=tp,
            comment=f"SMC Pro {signal['direction']} Score:{signal.get('score', 0)}"
        )

        # Verificar éxito de la orden (soporta resultados simulados)
        def is_trade_success(res):
            try:
                # Si MT5 está disponible, usar su código
                if is_mt5_available():
                    mt5_mod = get_mt5()
                    success_code = getattr(mt5_mod, 'TRADE_RETCODE_DONE', None)
                    if success_code is not None and getattr(res, 'retcode', None) == success_code:
                        return True
                # Para simulaciones o resultados sin retcode, considerar pedido con order != 0 como éxito
                return getattr(res, 'order', 0) != 0
            except Exception:
                return False

        if result and is_trade_success(result):
            # Registrar en historial
            log_trade_operation("AUTO_OPEN", signal, "AUTO", 0)
            return True, f"Orden ejecutada - Ticket: {getattr(result,'order',None)}"
        else:
            # Intentar obtener mensaje de error claro
            if result is None:
                error_msg = "Resultado vacío"
            else:
                try:
                    # mt5.last_error() puede no reflejar el comentario
                    err = getattr(result, 'comment', None) or mt5.last_error()
                    error_msg = err
                except Exception:
                    error_msg = str(result)
            return False, f"Error ejecutando orden: {error_msg}"

    except Exception as e:
        return False, f"Error en auto-trading: {str(e)}"

def auto_close_positions():
    """Cierra automáticamente posiciones basadas en el estado local"""
    try:
        positions_mt5 = get_mt5_positions()
        if not positions_mt5:
            return False, "No hay posiciones abiertas en MT5"

        state = load_position_state()
        if not state["is_open"]:
            return False, "No hay posición registrada localmente"

        # Cerrar todas las posiciones del símbolo
        closed_count = 0
        for position in positions_mt5:
            if position.symbol == SYMBOL:
                result = close_mt5_position(position.ticket, comment="SMC Pro Auto Close")
                if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                    closed_count += 1

        if closed_count > 0:
            close_position("AUTO_CLOSE")
            return True, f"Cerradas {closed_count} posiciones automáticamente"
        else:
            return False, "Error cerrando posiciones"

    except Exception as e:
        return False, f"Error en auto-cierre: {str(e)}"

# ============================================
# DATOS — MT5 primero, yfinance fallback
# ============================================
_TF_MAP_YF = {
    "15m": ("5d",  "15m"),
    "1h":  ("5d",  "1h"),
    "4h":  ("30d", "1h"),
    "1d":  ("90d", "1d"),
}

def get_eurusd_data(tf="1h", extended=False):
    """Obtiene datos históricos. extended=True para backtest con más historia"""
    # Cache corto en memoria para evitar recargas en hot-reload / recargas frecuentes
    key = (tf, bool(extended))
    cache_entry = _EURUSD_CACHE.get(key)
    if cache_entry:
        ts, df = cache_entry
        if datetime.now() - ts < _EURUSD_CACHE_TTL:
            return df
    if is_mt5_available() and mt5_connect():
        # Para backtest extendido, obtener más datos
        if extended and tf == "1h":
            count = 1000  # ~1.5 meses de datos para backtest
        else:
            count = {"15m": 300, "1h": 300, "4h": 150, "1d": 100}.get(tf, 200)

        df = get_mt5_candles(SYMBOL, tf, count)
        if not df.empty:
            _EURUSD_CACHE[key] = (datetime.now(), df)
            return df

    yf = get_yf()
    if yf:
        try:
            # Para datos extendidos usar períodos más largos
            if extended:
                period, interval = {
                    "1h": ("3mo", "1h"),   # 3 meses
                    "4h": ("6mo", "1h"),   # 6 meses
                    "1d": ("2y", "1d")     # 2 años
                }.get(tf, ("1y", "1d"))    # 1 año por defecto
            else:
                period, interval = _TF_MAP_YF.get(tf, ("5d", "1h"))

            df = yf.download("EURUSD=X", period=period, interval=interval,
                             progress=False, auto_adjust=True)
            df = flatten_columns(df)
            if df.empty:
                return pd.DataFrame()

            if tf == "4h":
                df = df.resample("4h").agg({
                    "Open": "first", "High": "max",
                    "Low": "min", "Close": "last", "Volume": "sum"
                }).dropna()
            _EURUSD_CACHE[key] = (datetime.now(), df)
            return df
        except Exception as e:
            logging.warning(f"yfinance ({'extended ' if extended else ''}{tf}): {e}")
    return pd.DataFrame()

def get_multiple_timeframes():
    out = {}
    for tf in ["15m", "1h", "4h", "1d"]:
        df = get_eurusd_data(tf)
        if not df.empty:
            out[tf] = df
    return out

# ============================================
# DXY
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


def get_dxy_tf(tf):
    if is_mt5_available() and mt5_connect():
        for sym in ["USDX", "DXY", "DX"]:
            df = get_mt5_candles(sym, tf, 60)
            if not df.empty:
                close = df["Close"].dropna()
                if close.empty:
                    continue
                current = scalar(close.iloc[-1])
                open_day = scalar(close.iloc[0])
                if not current or not open_day:
                    continue
                change = ((current - open_day) / open_day) * 100
                recent_change = None
                if len(close) >= 5:
                    recent_start = close.iloc[-5]
                    recent_change = ((current - recent_start) / recent_start) * 100
                direction, trend, e8, e21 = interpret_dxy_signal(close)
                return {
                    "tf": tf,
                    "source": f"MT5:{sym}",
                    "price": current,
                    "chg": round(change, 2),
                    "recent_chg": round(recent_change, 2) if recent_change is not None else None,
                    "direction": direction,
                    "trend": trend,
                    "ema8": e8,
                    "ema21": e21,
                    "close": close
                }
    yf = get_yf()
    if yf:
        # Mapa de período correcto por timeframe para yfinance
        _yf_dxy_period_map = {
            "5m":  ("1d",  "5m"),
            "15m": ("5d",  "15m"),
            "1h":  ("5d",  "1h"),
            "4h":  ("30d", "1h"),
            "1d":  ("90d", "1d"),
        }
        yf_period, yf_interval = _yf_dxy_period_map.get(tf, ("5d", "15m"))
        # UUP (ETF del dólar) suele tener datos intradía más fiables que DX-Y.NYB
        for ticker in ["UUP", "DX=F", "DX-Y.NYB"]:
            try:
                df = yf.download(ticker, period=yf_period, interval=yf_interval,
                                 progress=False, auto_adjust=True)
                df = flatten_columns(df)
                if df.empty or "Close" not in df.columns:
                    continue
                close = df["Close"].dropna()
                if len(close) < 3:
                    continue
                current = scalar(close.iloc[-1])
                open_day = scalar(close.iloc[0])
                if not current or not open_day:
                    continue
                change = ((current - open_day) / open_day) * 100
                recent_change = None
                if len(close) >= 5:
                    recent_start = close.iloc[-5]
                    recent_change = ((current - recent_start) / recent_start) * 100
                direction, trend, e8, e21 = interpret_dxy_signal(close)
                return {
                    "tf": tf,
                    "source": ticker,
                    "price": current,
                    "chg": round(change, 2),
                    "recent_chg": round(recent_change, 2) if recent_change is not None else None,
                    "direction": direction,
                    "trend": trend,
                    "ema8": e8,
                    "ema21": e21,
                    "close": close
                }
            except Exception as e:
                logging.warning(f"DXY yf {ticker} ({tf}): {e}")
    return None


def get_dxy():
    dxy_15m = get_dxy_tf("15m")
    dxy_5m  = get_dxy_tf("5m")
    if not dxy_15m and not dxy_5m:
        return {
            "dxy_dir": "NO DATA", "dxy_price": None, "dxy_chg": None,
            "dxy_trend": "NO DATA", "dxy_src": None,
            "dxy_ema8": None, "dxy_ema21": None,
            "dxy_15m_dir": None, "dxy_15m_trend": None, "dxy_15m_price": None,
            "dxy_15m_chg": None, "dxy_5m_dir": None, "dxy_5m_trend": None,
            "dxy_5m_price": None, "dxy_5m_chg": None
        }

    if dxy_5m and dxy_15m:
        if dxy_5m["direction"] == dxy_15m["direction"]:
            combined_dir = dxy_5m["direction"]
        elif dxy_5m["direction"] == "LATERAL":
            combined_dir = dxy_15m["direction"]
        elif dxy_15m["direction"] == "LATERAL":
            combined_dir = dxy_5m["direction"]
        else:
            combined_dir = "LATERAL"
    else:
        combined_dir = (dxy_5m or dxy_15m)["direction"]

    main_data = dxy_5m or dxy_15m
    return {
        "dxy_dir": combined_dir,
        "dxy_price": main_data["price"],
        "dxy_chg": main_data["chg"],
        "dxy_trend": f"15m {dxy_15m['direction'] if dxy_15m else '??'} / 5m {dxy_5m['direction'] if dxy_5m else '??'}",
        "dxy_src": main_data["source"],
        "dxy_ema8": main_data["ema8"],
        "dxy_ema21": main_data["ema21"],
        "dxy_15m_dir": dxy_15m["direction"] if dxy_15m else None,
        "dxy_15m_trend": dxy_15m["trend"] if dxy_15m else None,
        "dxy_15m_price": dxy_15m["price"] if dxy_15m else None,
        "dxy_15m_chg": dxy_15m["chg"] if dxy_15m else None,
        "dxy_5m_dir": dxy_5m["direction"] if dxy_5m else None,
        "dxy_5m_trend": dxy_5m["trend"] if dxy_5m else None,
        "dxy_5m_price": dxy_5m["price"] if dxy_5m else None,
        "dxy_5m_chg": dxy_5m["chg"] if dxy_5m else None
    }

# ============================================
# DATOS INSTITUCIONALES — COT REPORT (CFTC)
# ============================================
_COT_CACHE = None
_COT_CACHE_TTL = timedelta(hours=12)

def get_cot_data():
    """Obtiene COT (Commitment of Traders) para EUR FX Futures desde CFTC."""
    global _COT_CACHE
    if _COT_CACHE:
        ts, data = _COT_CACHE
        if datetime.now() - ts < _COT_CACHE_TTL:
            return data
    try:
        req = get_requests()
        if not req:
            return None
        url = (
            "https://publicreporting.cftc.gov/api/odata/v1/MarketsAndPositions"
            "?$filter=MarketAndExchangeNames eq 'EURO FX - CHICAGO MERCANTILE EXCHANGE'"
            "&$top=2&$orderby=ReportDate desc"
        )
        r = req.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        values = r.json().get("value", [])
        if not values:
            return None
        latest = values[0]
        prev   = values[1] if len(values) > 1 else None
        nc_long  = int(latest.get("NonCommercialLong",  0) or 0)
        nc_short = int(latest.get("NonCommercialShort", 0) or 0)
        net      = nc_long - nc_short
        prev_net = 0
        if prev:
            prev_net = int(prev.get("NonCommercialLong", 0) or 0) - int(prev.get("NonCommercialShort", 0) or 0)
        change = net - prev_net
        result = {
            "date":            (latest.get("ReportDate") or "")[:10],
            "nc_long":         nc_long,
            "nc_short":        nc_short,
            "net":             net,
            "prev_net":        prev_net,
            "change":          change,
            "bias":            "ALCISTA (EUR)" if net > 0 else "BAJISTA (EUR)",
            "bias_direction":  "LONG" if net > 0 else "SHORT",
            "change_lbl":      "Aumentando longs" if change > 0 else "Reduciendo longs",
        }
        _COT_CACHE = (datetime.now(), result)
        return result
    except Exception as e:
        logging.warning(f"COT data error: {e}")
        return None

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
def detect_volume_spikes(df, threshold=2.0):
    """Detecta picos de volumen usando tick volume de MT5."""
    if df.empty or "Volume" not in df.columns:
        return []
    vol = df["Volume"].dropna()
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
                "mensaje": f"Volumen {ratio:.1f}x sobre la media — posible movimiento institucional"
            }]
    return []

def detect_volume_trend(df):
    """Detecta si el volumen confirma la tendencia de precio."""
    if df.empty or "Volume" not in df.columns or len(df) < 5:
        return "Sin datos"
    price_up  = float(df["Close"].iloc[-1]) > float(df["Close"].iloc[-5])
    volume_up = float(df["Volume"].iloc[-1]) > float(df["Volume"].iloc[-5])
    if price_up and volume_up:
        return "✅ Volumen confirma tendencia ALCISTA"
    elif not price_up and volume_up:
        return "✅ Volumen confirma tendencia BAJISTA"
    elif price_up and not volume_up:
        return "⚠️ Precio sube pero volumen cae — posible debilidad alcista"
    else:
        return "⚠️ Precio baja pero volumen cae — posible agotamiento bajista"

def analyze_volume_profile(df, n_levels=10):
    """
    Calcula un perfil de volumen simplificado.
    Muestra en qué niveles de precio hay más volumen acumulado.
    """
    if df.empty or "Volume" not in df.columns or len(df) < 10:
        return [], None
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
        vol_at_level = float(df.loc[mask, "Volume"].sum())
        levels.append({
            "precio": round((low_lvl + high_lvl) / 2, 5),
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
    Estima el delta de volumen (diferencia entre volumen alcista y bajista).
    Aproximación: velas alcistas = volumen comprador, bajistas = volumen vendedor.
    """
    if df.empty or "Volume" not in df.columns or len(df) < 5:
        return None
    recent = df.tail(20)
    bull_vol = recent.loc[recent["Close"] >= recent["Open"], "Volume"].sum()
    bear_vol = recent.loc[recent["Close"] <  recent["Open"], "Volume"].sum()
    total    = bull_vol + bear_vol
    if total == 0:
        return None
    delta = bull_vol - bear_vol
    delta_pct = delta / total * 100
    return {
        "bull_vol":  int(bull_vol),
        "bear_vol":  int(bear_vol),
        "delta":     int(delta),
        "delta_pct": round(delta_pct, 1),
        "bias":      "COMPRADORES" if delta > 0 else "VENDEDORES"
    }

def get_cvd(df):
    """
    Calcula el CVD (Cumulative Volume Delta) de las últimas velas.
    Muestra si hay presión acumulada de compra o venta.
    """
    if df.empty or "Volume" not in df.columns or len(df) < 5:
        return []
    recent = df.tail(30).copy()
    deltas = []
    for _, row in recent.iterrows():
        if row["Close"] >= row["Open"]:
            deltas.append(float(row["Volume"]))
        else:
            deltas.append(-float(row["Volume"]))
    cvd = pd.Series(deltas).cumsum().tolist()
    return cvd

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
# TELEGRAM
# ============================================
def send_telegram_alert(signal, score, tick=None, definitive=False, reason=None):
    if TELEGRAM_TOKEN == "TU_TELEGRAM_BOT_TOKEN":
        return False
    try:
        direction = signal.get("direction", "")
        price = signal.get("price", 0)
        tp = signal.get("tp", 0)
        sl = signal.get("sl", 0)
        rr = signal.get("rr", 0)

        if reason and reason.startswith("CLOSED_"):
            # Alerta de cierre de posición
            outcome = reason.split("_")[1]
            emoji = "✅" if outcome == "TP" else "❌" if outcome == "SL" else "🔄"
            msg = (
                f"🔒 *POSICIÓN CERRADA*\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"*Resultado:* {emoji} {outcome}\n"
                f"*Dirección:* {'📈 LONG' if direction=='LONG' else '📉 SHORT'}\n"
                f"*Entrada:* `{price:.5f}`\n"
                f"*TP:* `{tp:.5f}` | *SL:* `{sl:.5f}`\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"🚀 _Listo para nueva señal definitiva_"
            )
        elif reason == "BE":
            # Alerta de Break Even (1:1 alcanzado)
            msg = (
                f"⚖️ *BREAK EVEN ALCANZADO*\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"*Dirección:* {'📈 LONG' if direction=='LONG' else '📉 SHORT'}\n"
                f"*Beneficio:* +{abs(price-sl)/PIP:.1f}p (1:1)\n"
                f"*Entrada:* `{price:.5f}`\n"
                f"*TP:* `{tp:.5f}` | *SL:* `{sl:.5f}`\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"🎯 _Posición en ganancias — Continúa seguimiento_"
            )
        elif definitive:
            # Alerta de posición definitiva
            msg = (
                f"🚨 *SEÑAL DEFINITIVA — SMC PRO v2*\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"*Dir:* {'📈 LONG' if direction=='LONG' else '📉 SHORT'}\n"
                f"*Score:* {score}/100 — ⭐ DEFINITIVA ⭐\n"
                f"*Confluencia:* >{MIN_DEFINITIVE_SCORE}%\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"*Entrada:* `{price:.5f}`\n"
                f"*TP:* `{tp:.5f}` (+{abs(tp-price)/PIP:.1f}p)\n"
                f"*SL:* `{sl:.5f}` (-{abs(price-sl)/PIP:.1f}p)\n"
                f"*R:R:* 1:{rr:.2f}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🎯 _Posición abierta — Seguimiento activo_"
            )
        else:
            # Alerta normal (si se mantiene)
            label, _ = score_label(score)
            spread_txt = f"\nSpread: {tick['spread_pips']} pips" if tick else ""
            msg = (
                f"⚡ *SMC PRO v2 — EURUSD*\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"*Dir:* {'📈 LONG' if direction=='LONG' else '📉 SHORT'}\n"
                f"*Score:* {score}/100 — {label}\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"*Entrada:* `{price:.5f}`\n"
                f"*TP:* `{tp:.5f}` (+{abs(tp-price)/PIP:.1f}p)\n"
                f"*SL:* `{sl:.5f}` (-{abs(price-sl)/PIP:.1f}p)\n"
                f"*R:R:* 1:{rr:.2f}{spread_txt}\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"⚠️ _Solo informativo. Usa siempre SL._"
            )

        req = get_requests()
        if not req:
            return False

        token = TELEGRAM_TOKEN
        chat_id = TELEGRAM_CHAT_ID
        st_obj = globals().get("st")
        if st_obj and hasattr(st_obj, "session_state"):
            token = st_obj.session_state.get("tg_token", token)
            chat_id = st_obj.session_state.get("tg_chat", chat_id)

        if not token or token == "TU_TELEGRAM_BOT_TOKEN" or not chat_id or chat_id == "TU_CHAT_ID":
            return False

        req.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"},
            timeout=10
        )
        return True
    except Exception as e:
        logging.warning(f"Telegram: {e}")
        return False

# ============================================
# BACKTESTING
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
    yf = get_yf()
    if not yf:
        return pd.DataFrame()
    # Para 1h yfinance soporta hasta ~60 días de forma fiable en la API gratuita.
    # Descargamos varios bloques de 60d y los concatenamos para obtener hasta ~1 año.
    if tf in ("1h", "4h"):
        from datetime import date, timedelta as _td
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
    yf_mod = get_yf()
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
            if not ((7 <= hs < 12) or (15 <= hs < 20)) and not in_trade:
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

# ============================================
# MOTOR DE CONOCIMIENTO — MULTI-ESTRATEGIA + FUNDAMENTAL
# ============================================

# ── Knowledge Base (JSON en disco) ──────────────────────────────────────────
_KB_FILE = os.path.join(os.getcwd(), "strategy_knowledge.json")

def load_knowledge_base():
    try:
        if os.path.exists(_KB_FILE):
            with open(_KB_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {"runs": [], "best_strategy": None, "strategy_wins": {}}

def save_knowledge_base(kb):
    try:
        with open(_KB_FILE, "w", encoding="utf-8") as f:
            json.dump(kb, f, indent=2, ensure_ascii=False, default=str)
    except Exception as e:
        logging.warning(f"KB save: {e}")

def update_kb(comparison_result, cot=None, calendar=None, market_ctx=None):
    kb = load_knowledge_base()
    best = comparison_result["best"]
    entry = {
        "ts": datetime.now().isoformat()[:16],
        "best": best["strategy"],
        "pf":   best["profit_factor"],
        "wr":   best["winrate"],
        "total": best["total"],
        "net_pips": best["net_pips"],
        "strategies": [{"n": r["strategy"], "pf": r["profit_factor"],
                         "wr": r["winrate"], "total": r["total"]}
                        for r in comparison_result["results"]],
        "cot_bias":    cot.get("bias") if cot else None,
        "events_high": sum(1 for e in (calendar or []) if e.get("impact","").upper() == "HIGH"),
        "market_ctx":  (market_ctx or [])[:6],  # por qué se mueve el mercado
    }
    kb["runs"] = (kb.get("runs", []) + [entry])[-50:]
    wins = kb.get("strategy_wins", {})
    wins[best["strategy"]] = wins.get(best["strategy"], 0) + 1
    kb["strategy_wins"] = wins
    recent = kb["runs"][-5:]
    votes = {}
    for r in recent:
        votes[r["best"]] = votes.get(r["best"], 0) + 1
    kb["best_strategy"] = max(votes, key=votes.get) if votes else best["strategy"]
    save_knowledge_base(kb)
    return kb


def kb_record_pending_signal(direction, price, strategy, reason, df=None, cot=None, calendar=None):
    """Guarda la señal actual con contexto técnico+fundamental para evaluarla después."""
    kb = load_knowledge_base()

    context = {}
    if df is not None and not df.empty:
        try:
            regime, regime_lbl, regime_details = detect_market_regime(df, calendar)
            context["regime"]     = regime
            context["regime_lbl"] = regime_lbl
            context["rsi"]        = regime_details.get("rsi")
            context["atr_pips"]   = regime_details.get("atr_pips")
            context["high_vol"]   = regime_details.get("high_vol", False)
            context["news_risk"]  = regime_details.get("news_risk", "low")
            if "minutes_to_news" in regime_details:
                context["minutes_to_news"] = regime_details["minutes_to_news"]
        except Exception:
            pass
    if cot:
        context["cot_bias"] = cot.get("bias", "neutral")

    kb["pending_signal"] = {
        "ts":        datetime.now().isoformat()[:19],
        "direction": direction,
        "price":     price,
        "strategy":  strategy,
        "reason":    reason,
        "context":   context,
    }
    save_knowledge_base(kb)


def kb_evaluate_and_learn(current_price):
    """Compara señal pendiente con precio actual y actualiza estadísticas con contexto."""
    kb = load_knowledge_base()
    pending = kb.get("pending_signal")
    if not pending or pending.get("direction") == "NO TRADE":
        return kb
    direction   = pending["direction"]
    entry_price = pending.get("price")
    strategy    = pending.get("strategy", "unknown")
    context     = pending.get("context", {})
    if entry_price is None or current_price is None:
        return kb
    move_pips = (current_price - entry_price) / 0.0001
    if direction == "LONG":
        correct = move_pips > 3
    elif direction == "SHORT":
        correct = move_pips < -3
    else:
        return kb
    outcome_key = "correct" if correct else "wrong"

    stats = kb.get("signal_stats", {})
    s = stats.get(strategy, {"correct": 0, "wrong": 0, "by_regime": {}, "by_news_risk": {}})
    s[outcome_key] = s.get(outcome_key, 0) + 1

    # Desglose por régimen de mercado
    regime = context.get("regime", "unknown")
    by_regime = s.get("by_regime", {})
    r_s = by_regime.get(regime, {"correct": 0, "wrong": 0})
    r_s[outcome_key] = r_s.get(outcome_key, 0) + 1
    by_regime[regime] = r_s
    s["by_regime"] = by_regime

    # Desglose por riesgo de noticias
    news_risk = context.get("news_risk", "low")
    by_news = s.get("by_news_risk", {})
    n_s = by_news.get(news_risk, {"correct": 0, "wrong": 0})
    n_s[outcome_key] = n_s.get(outcome_key, 0) + 1
    by_news[news_risk] = n_s
    s["by_news_risk"] = by_news

    stats[strategy] = s
    kb["signal_stats"] = stats
    kb.pop("pending_signal", None)
    save_knowledge_base(kb)
    return kb

# ── Economic Calendar (ForexFactory unofficial JSON) ────────────────────────
_CALENDAR_CACHE = None
_CALENDAR_TTL   = timedelta(hours=4)

def get_economic_calendar():
    global _CALENDAR_CACHE
    if _CALENDAR_CACHE:
        ts, data = _CALENDAR_CACHE
        if datetime.now() - ts < _CALENDAR_TTL:
            return data
    try:
        req = get_requests()
        if not req:
            return []
        r = req.get(
            "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
            timeout=10, headers={"User-Agent": "Mozilla/5.0 SMC-Bot/1.0"}
        )
        r.raise_for_status()
        all_ev = r.json()
        relevant = [e for e in all_ev
                    if e.get("impact", "").upper() in ("HIGH", "MEDIUM")
                    and e.get("currency", "") in ("EUR", "USD")]
        _CALENDAR_CACHE = (datetime.now(), relevant)
        return relevant
    except Exception as e:
        logging.warning(f"Calendar: {e}")
        return []

# ── Contexto Fundamental + Técnico (POR QUÉ se mueve el mercado) ────────────
def explain_market_context(df, cot=None, calendar=None, news=None):
    """Devuelve lista de cadenas explicando por qué el EUR/USD está donde está."""
    if df.empty or len(df) < 55:
        return ["Sin datos suficientes para contexto."]

    close = df["Close"]
    c     = float(close.iloc[-1])
    ema21 = close.ewm(span=21,  adjust=False).mean()
    ema50 = close.ewm(span=50,  adjust=False).mean()
    ema200= close.ewm(span=200, adjust=False).mean() if len(close) >= 200 else None
    e21   = float(ema21.iloc[-1])
    e50   = float(ema50.iloc[-1])

    dc   = close.diff()
    gain = dc.clip(lower=0).rolling(14).mean()
    loss = (-dc.clip(upper=0)).rolling(14).mean()
    rsi_v = float((100 - (100 / (1 + gain / loss.replace(0, np.nan)))).iloc[-1])

    macd_l = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
    hist_v = float((macd_l - macd_l.ewm(span=9, adjust=False).mean()).iloc[-1])

    reasons = []

    # ── Técnico ──
    trend = "ALCISTA" if c > e50 else "BAJISTA"
    reasons.append(
        f"📈 TENDENCIA ({trend}): Precio {c:.5f} está "
        f"{'SOBRE' if c > e50 else 'BAJO'} la EMA50 ({e50:.5f}). "
        f"El mercado de corto plazo favorece posiciones {'LONG' if c > e50 else 'SHORT'}."
    )

    if ema200 is not None:
        e200 = float(ema200.iloc[-1])
        macro = "ALCISTA" if c > e200 else "BAJISTA"
        reasons.append(
            f"🗺️ MACRO (EMA200): Tendencia institucional {macro}. "
            f"EUR/USD {'por encima' if c > e200 else 'por debajo'} de {e200:.5f}. "
            f"{'Los grandes fondos mantienen posición neta LARGA en EUR.' if c > e200 else 'Los grandes fondos mantienen posición neta CORTA en EUR.'}"
        )

    if not np.isnan(rsi_v):
        if rsi_v > 70:
            reasons.append(f"⚠️ RSI={rsi_v:.0f} — SOBRECOMPRA. El precio ha subido demasiado rápido. Alta probabilidad de pausa o retroceso técnico a EMA21 ({e21:.5f}).")
        elif rsi_v < 30:
            reasons.append(f"⚠️ RSI={rsi_v:.0f} — SOBREVENTA. El precio ha caído demasiado rápido. Alta probabilidad de rebote técnico hacia EMA21 ({e21:.5f}).")
        elif rsi_v > 55:
            reasons.append(f"✅ RSI={rsi_v:.0f} — Compradores en control. Momentum alcista confirmado, mercado en expansión.")
        else:
            reasons.append(f"🔻 RSI={rsi_v:.0f} — Vendedores en control. Momentum bajista activo.")

    reasons.append(
        f"{'✅' if hist_v > 0 else '🔻'} MACD histogram {'positivo' if hist_v > 0 else 'negativo'} — "
        f"la fuerza del movimiento a corto plazo apunta {'ARRIBA (compradores)' if hist_v > 0 else 'ABAJO (vendedores)'}."
    )

    # ── Institucional / COT ──
    if cot:
        net    = cot.get("net", 0)
        change = cot.get("change", 0)
        if abs(net) > 50000:
            bias_lbl = "MUY ALCISTA" if net > 0 else "MUY BAJISTA"
        elif abs(net) > 20000:
            bias_lbl = "ALCISTA" if net > 0 else "BAJISTA"
        else:
            bias_lbl = "NEUTRAL"
        reasons.append(
            f"🏦 INVERSORES INSTITUCIONALES (CFTC COT): {bias_lbl} en EUR. "
            f"Posición neta especuladores: {net:+,.0f} contratos. "
            f"Cambio esta semana: {change:+,.0f}. "
            + (
                "Los hedge funds y bancos llevan semanas COMPRANDO EUR masivamente → fuerza alcista estructural."
                if net > 50000 else
                "Los hedge funds y bancos llevan semanas VENDIENDO EUR masivamente → presión bajista estructural."
                if net < -50000 else
                "Posicionamiento institucional neutro — el mercado espera un catalizador fundamental."
            )
        )

    # ── Calendario económico ──
    if calendar:
        high_ev = [e for e in calendar if e.get("impact","").upper() == "HIGH"]
        if high_ev:
            reasons.append(f"📅 CALENDARIO ({len(high_ev)} eventos ALTO impacto esta semana):")
            for ev in high_ev[:4]:
                cur   = ev.get("currency", "")
                title = ev.get("title", "")
                prev  = ev.get("previous", "?")
                fore  = ev.get("forecast", "?")
                date  = str(ev.get("date", ""))[:10]
                effect = (
                    f"Si dato > pronóstico → USD sube → EUR/USD BAJA."
                    if cur == "USD" else
                    f"Si dato > pronóstico → EUR sube → EUR/USD SUBE."
                )
                reasons.append(f"  → [{cur}] {title} | Anterior:{prev} Pronóstico:{fore} | {date} — {effect}")
        med_ev = [e for e in calendar if e.get("impact","").upper() == "MEDIUM"]
        if med_ev:
            reasons.append(f"  ({len(med_ev)} eventos de impacto MEDIO esta semana — monitorear.)")

    # ── Noticias alto impacto ──
    if news:
        top = sorted([n for n in news if n.get("impact_score", 0) >= 6],
                     key=lambda x: x.get("impact_score", 0), reverse=True)[:3]
        for n in top:
            reasons.append(
                f"📰 NOTICIA ({n.get('impact_label','ALTA')}): "
                f"{n.get('title','')[:85]} "
                f"[{n.get('source',{}).get('name','')}]"
            )

    return reasons


# ── Detección de régimen de mercado ─────────────────────────────────────────
def detect_market_regime(df, calendar=None):
    """
    Clasifica el mercado actual: trending_bull, trending_bear, ranging,
    volatile, volatile_trend, pre_news.
    Devuelve (regime_key, regime_label, details_dict).
    """
    if df.empty or len(df) < 50:
        return "unknown", "Desconocido", {}

    close = df["Close"]
    high  = df["High"]
    low   = df["Low"]
    c     = float(close.iloc[-1])
    PIP   = 0.0001

    e9  = float(close.ewm(span=9,  adjust=False).mean().iloc[-1])
    e21 = float(close.ewm(span=21, adjust=False).mean().iloc[-1])
    e50 = float(close.ewm(span=50, adjust=False).mean().iloc[-1])

    dc   = close.diff()
    gain = dc.clip(lower=0).rolling(14).mean()
    loss = (-dc.clip(upper=0)).rolling(14).mean()
    rsi  = float((100 - 100 / (1 + gain / loss.replace(0, np.nan))).iloc[-1])
    if np.isnan(rsi):
        rsi = 50.0

    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low  - close.shift()).abs(),
    ], axis=1).max(axis=1)
    atr14  = float(tr.rolling(14).mean().iloc[-1]) / PIP
    atr_avg = float(tr.rolling(50).mean().iloc[-1]) / PIP if len(tr) >= 50 else atr14
    high_vol = atr14 > atr_avg * 1.3

    ema_spread = abs(e9 - e50) / (PIP * 10)   # "spread" en unidades de 10 pips
    trending   = ema_spread > 3.0
    bull       = e9 > e21 > e50
    bear       = e9 < e21 < e50

    # Riesgo de noticias
    news_risk        = "low"
    minutes_to_news  = None
    if calendar:
        now_utc = datetime.utcnow()
        high_ev = [e for e in calendar
                   if e.get("impact", "").upper() == "HIGH"
                   and e.get("currency", "") in ("EUR", "USD")]
        best_delta = None
        for ev in high_ev:
            try:
                ev_dt = datetime.strptime(str(ev.get("date", ""))[:16], "%Y-%m-%dT%H:%M")
                dm = (ev_dt - now_utc).total_seconds() / 60
                if -30 <= dm <= 120:
                    if best_delta is None or abs(dm) < abs(best_delta):
                        best_delta = dm
            except Exception:
                pass
        if best_delta is not None:
            minutes_to_news = int(best_delta)
            news_risk = "high" if -30 <= best_delta <= 60 else "medium"

    # Clasificación
    if news_risk == "high":
        regime, lbl = "pre_news",       "Riesgo Noticias — Precaución"
    elif high_vol and trending:
        regime, lbl = "volatile_trend", "Tendencia Explosiva (alta volatilidad)"
    elif trending and bull:
        regime, lbl = "trending_bull",  "Tendencia Alcista"
    elif trending and bear:
        regime, lbl = "trending_bear",  "Tendencia Bajista"
    elif high_vol:
        regime, lbl = "volatile",       "Volatilidad Alta (sin tendencia clara)"
    else:
        regime, lbl = "ranging",        "Mercado Lateral / Rango"

    details = {
        "regime":      regime,
        "rsi":         round(rsi, 1),
        "atr_pips":    round(atr14, 1),
        "atr_avg":     round(atr_avg, 1),
        "high_vol":    high_vol,
        "trending":    trending,
        "bull":        bull,
        "bear":        bear,
        "news_risk":   news_risk,
        "ema_spread":  round(ema_spread, 1),
    }
    if minutes_to_news is not None:
        details["minutes_to_news"] = minutes_to_news

    return regime, lbl, details


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


def kb_best_strategy_for_conditions(df, cot=None, calendar=None):
    """
    Selecciona la mejor estrategia según régimen de mercado actual (técnico + fundamental).
    Devuelve (strategy_key, regime_key, regime_label, regime_details, explanation_why).
    """
    kb     = load_knowledge_base()
    regime, regime_lbl, regime_details = detect_market_regime(df, calendar)
    stats  = kb.get("signal_stats", {})
    wins   = kb.get("strategy_wins", {})
    total_runs = len(kb.get("runs", []))

    scores = {}
    explanations = {}

    for strat in _STRATEGY_META.keys():
        score = 0.0
        parts = []

        # 1. Tasa de acierto global (0–40 pts)
        s   = stats.get(strat, {})
        ok  = s.get("correct", 0)
        ko  = s.get("wrong", 0)
        tot = ok + ko
        if tot > 0:
            wr = ok / tot
            score += wr * 40
            parts.append(f"{ok}/{tot} señales correctas ({wr*100:.0f}%)")

        # 2. Tasa de acierto en el régimen actual (0–35 pts)
        by_regime = s.get("by_regime", {})
        r_s  = by_regime.get(regime, {})
        r_ok = r_s.get("correct", 0)
        r_ko = r_s.get("wrong", 0)
        if r_ok + r_ko >= 2:
            r_wr = r_ok / (r_ok + r_ko)
            score += r_wr * 35
            parts.append(f"En {regime_lbl}: {r_ok}/{r_ok+r_ko} ({r_wr*100:.0f}%)")
        elif regime in _STRATEGY_REGIME_AFFINITY.get(strat, []):
            score += 15  # bonus por afinidad de diseño
            parts.append(f"Diseñada para {regime_lbl}")

        # 3. Victorias en backtests (0–15 pts)
        if total_runs > 0:
            score += (wins.get(strat, 0) / total_runs) * 15

        # 4. Alineación COT con dirección del régimen (0–10 pts)
        if cot:
            cot_bias = cot.get("bias", "neutral")
            if cot_bias == "bullish" and regime in ("trending_bull", "volatile_trend"):
                score += 10
                parts.append("COT institucional alcista confirma dirección")
            elif cot_bias == "bearish" and regime in ("trending_bear", "volatile_trend"):
                score += 10
                parts.append("COT institucional bajista confirma dirección")

        scores[strat]       = score
        explanations[strat] = parts

    # Elegir mejor; fallback al mejor global si no hay datos suficientes
    if any(v > 0 for v in scores.values()):
        best = max(scores, key=scores.get)
    else:
        best = kb.get("best_strategy")

    if best is None:
        best = next(iter(_STRATEGY_META))

    why_parts = explanations.get(best, [])
    why = f"Seleccionada por régimen actual ({regime_lbl})"
    if why_parts:
        why += " — " + " · ".join(why_parts[:3])

    return best, regime, regime_lbl, regime_details, why


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

def _run_single_strategy(df, strategy="ema_trend", use_windows=True, utc_offset=2, daily_mode=False):
    min_bars = 200 if daily_mode else 110
    if df.empty or len(df) < min_bars:
        return None
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
            if not ((7 <= hs < 12) or (15 <= hs < 20)) and not in_trade:
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

_ALL_STRATEGIES = list(_STRATEGY_META.keys())
_RANK_EMOJI = ["🥇","🥈","🥉","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟",
               "⓫","⓬","⓭","⓮","⓯","⓰","⓱"]

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
# INDICADORES
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

# ============================================
# SMC
# ============================================
def detect_liquidity(df):
    if df.empty or len(df) < 5: return None, None, "Sin datos"
    high  = scalar(df["High"].tail(20).max())
    low   = scalar(df["Low"].tail(20).min())
    price = last_scalar(df["Close"])
    if any(v is None for v in [high, low, price]): return None, None, "Sin datos"
    sig = ("Liquidez arriba — posible sweep alcista" if price < high
           else "Precio en máximos — posible sweep bajista")
    return high, low, sig

def detect_structure(df):
    if df.empty or len(df) < 10: return []
    obs = []
    O, C, H, L = (df["Open"].values, df["Close"].values,
                  df["High"].values, df["Low"].values)
    for i in range(1, min(20, len(df))):
        idx = -(i + 1)
        try:
            po, pc = float(O[idx-1]), float(C[idx-1])
            co, cc = float(O[idx]),   float(C[idx])
            ho     = float(H[idx-1])
            lo     = float(L[idx-1])
        except (IndexError, ValueError): continue
        if pc < po and cc > co and cc > po and co < pc and cc > ho:
            obs.append(("BULLISH OB", float(L[idx]), "🟢"))
        elif pc > po and cc < co and cc < po and co > pc and cc < lo:
            obs.append(("BEARISH OB", float(H[idx]), "🔴"))
    return obs[:5]

def detect_fvg(df):
    if df.empty or len(df) < 3: return []
    fvgs = []
    for i in range(1, min(30, len(df))):
        prev = df.iloc[-(i+1)]; curr = df.iloc[-i]
        if curr["Low"] > prev["High"]:
            gap = (curr["Low"] - prev["High"]) / PIP
            if gap > 5:
                fvgs.append(("FVG BULLISH",
                             (prev["High"] + curr["Low"]) / 2, "🟢", f"{gap:.1f}p"))
        elif curr["High"] < prev["Low"]:
            gap = (prev["Low"] - curr["High"]) / PIP
            if gap > 5:
                fvgs.append(("FVG BEARISH",
                             (prev["Low"] + curr["High"]) / 2, "🔴", f"{gap:.1f}p"))
    return fvgs[:5]

def detect_swing_points(df, lookback=50):
    if df.empty or len(df) < lookback: return [], []
    recent = df.tail(lookback)
    highs, lows = recent["High"], recent["Low"]
    shs, sls = [], []
    for i in range(4, len(recent) - 4):
        if (all(highs.iloc[i] > highs.iloc[i-j] for j in range(1, 5)) and
                all(highs.iloc[i] > highs.iloc[i+j] for j in range(1, 5))):
            shs.append(("FRACTAL HIGH", highs.iloc[i], "🔴"))
        if (all(lows.iloc[i] < lows.iloc[i-j] for j in range(1, 5)) and
                all(lows.iloc[i] < lows.iloc[i+j] for j in range(1, 5))):
            sls.append(("FRACTAL LOW", lows.iloc[i], "🟢"))
    return shs[-5:], sls[-5:]

# ============================================
# SESIÓN
# ============================================
def get_market_session():
    h = datetime.utcnow().hour
    if h >= 22 or h < 7: return "Sydney",      "BAJA",        "⚪"
    if 8  <= h < 13:      return "Londres",     "ALTA",        "🟢"
    if 13 <= h < 17:      return "Londres+NY",  "MUY ALTA ⚡", "🟡"
    if 17 <= h < 22:      return "Nueva York",  "ALTA",        "🟢"
    return                       "Tokio",       "MEDIA",       "🟡"

# ============================================
# VENTANAS HORARIAS DE TRADING (ESPAÑA)
# ============================================
UTC_OFFSET_SPAIN = 2  # CEST verano (+2); cambiar a 1 para CET invierno

def get_spain_hour():
    return (datetime.utcnow().hour + UTC_OFFSET_SPAIN) % 24

def is_trading_window():
    """True si hora España está en 07:00-12:00 o 15:00-20:00"""
    h = get_spain_hour()
    return (7 <= h < 12) or (15 <= h < 20)

def get_trading_window_info():
    h = get_spain_hour()
    if 7 <= h < 12:
        return True, "VENTANA MAÑANA (07:00-12:00)", f"Cierra en ~{12 - h}h | España aprox. {h:02d}:xx"
    elif 15 <= h < 20:
        return True, "VENTANA TARDE (15:00-20:00)", f"Cierra en ~{20 - h}h | España aprox. {h:02d}:xx"
    elif h < 7:
        return False, "CERRADO (noche)", f"Abre ventana mañana en ~{7 - h}h"
    elif 12 <= h < 15:
        return False, "DESCANSO MEDIODÍA", f"Reabre ventana tarde en ~{15 - h}h"
    else:
        return False, "CERRADO (noche)", f"Abre ventana mañana en ~{24 - h + 7}h"

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
    """Absorción institucional: alto volumen con poco movimiento de precio."""
    if df.empty or "Volume" not in df.columns or len(df) < vol_window:
        return None
    avg_vol  = float(df["Volume"].tail(vol_window).mean())
    last_vol = float(df["Volume"].iloc[-1])
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
            "descripcion": f"Volumen {vol_ratio:.1f}x con mecha grande — institucional absorbiendo",
            "sesgo": "LONG" if side == "COMPRADORA" else "SHORT",
            "fuerza": "ALTA" if vol_ratio >= 2.5 else "MEDIA"
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
# NOTICIAS
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

def get_rss_news():
    feeds = [
        {"name": "Reuters",          "url": "https://feeds.reuters.com/reuters/topNews"},
        {"name": "BBC Business",     "url": "http://feeds.bbci.co.uk/news/business/rss.xml"},
        {"name": "CNBC",             "url": "https://www.cnbc.com/id/100003114/device/rss/rss.html"},
        {"name": "MarketWatch",      "url": "https://feeds.marketwatch.com/marketwatch/marketpulse/"},
        {"name": "FXStreet",         "url": "https://www.fxstreet.com/rss"},
        {"name": "DailyFX",          "url": "https://www.dailyfx.com/feeds"},
        {"name": "ECB Press",        "url": "https://www.ecb.europa.eu/rss/press.html"},
        {"name": "Federal Reserve",  "url": "https://www.federalreserve.gov/feeds/press_all.xml"},
        {"name": "BabyPips",         "url": "https://www.babypips.com/rss"},
        {"name": "Bloomberg",        "url": "https://feeds.bloomberg.com/markets/news.rss"},
        {"name": "WSJ Markets",      "url": "https://feeds.a.dj.com/rss/RSSMarketsMain.xml"},
        {"name": "Yahoo Finance",    "url": "https://finance.yahoo.com/rss/"},
        {"name": "AP Business",      "url": "https://feeds.apnews.com/rss/apf-business"},
        {"name": "Guardian Business","url": "https://www.theguardian.com/business/rss"},
        {"name": "Al Jazeera",       "url": "https://www.aljazeera.com/xml/rss/all.xml"},
        {"name": "Bank of England",  "url": "https://www.bankofengland.co.uk/rss/news"},
        {"name": "IMF",              "url": "https://www.imf.org/en/rss"},
        {"name": "Euronews",         "url": "https://www.euronews.com/rss?format=mrss&level=theme&name=business"},
        {"name": "ZeroHedge",        "url": "https://feeds.feedburner.com/zerohedge/feed"},
        {"name": "Investing.com",    "url": "https://www.investing.com/rss/news.rss"},
    ]
    HEADERS = {"User-Agent": "Mozilla/5.0"}

    def fetch(feed):
        try:
            req = get_requests()
            if not req:
                return []
            r = req.get(feed["url"], timeout=8, headers=HEADERS)
            r.raise_for_status()
            parsed = feedparser.parse(r.content)
            arts = []
            for e in parsed.entries[:3]:
                title = e.get("title", ""); desc = e.get("summary", "")
                imp, lbl, emoji, kws = estimate_impact(title, desc)
                arts.append({
                    "title": title, "description": desc, "url": e.get("link", ""),
                    "source": {"name": feed["name"]},
                    "publishedAt": e.get("published") or e.get("updated") or "",
                    "impact_score": imp, "impact_label": lbl,
                    "impact_emoji": emoji, "keywords": kws
                })
            return arts
        except: return []

    all_news = []
    with ThreadPoolExecutor(max_workers=10) as ex:
        for fut in as_completed([ex.submit(fetch, f) for f in feeds]):
            try: all_news.extend(fut.result())
            except: pass

    def pdt(a):
        try:
            dt = datetime.fromisoformat(a["publishedAt"].replace("Z", "+00:00"))
            return dt.replace(tzinfo=None) if dt.tzinfo else dt
        except: return datetime.min

    all_news.sort(key=pdt, reverse=True)
    return all_news

def get_news(n=25):
    cached = load_cache()
    if cached: return cached[:n]
    try:
        url = (f"https://newsapi.org/v2/everything?q=EUR+USD+Fed+ECB+inflation"
               f"&language=en&sortBy=publishedAt&apiKey={NEWS_API_KEY}")
        req = get_requests()
        if not req:
            api = []
        else:
            r = req.get(url, timeout=10); r.raise_for_status()
            api = []
            for a in r.json().get("articles", [])[:n//3]:
                imp, lbl, emoji, kws = estimate_impact(
                    a.get("title", ""), a.get("description", ""))
                a.update({"impact_score": imp, "impact_label": lbl,
                          "impact_emoji": emoji, "keywords": kws})
                api.append(a)
    except: api = []
    rss = get_rss_news()[:2*n//3]
    all_a = api + rss

    def sk(a):
        try:
            dt = datetime.fromisoformat(
                a.get("publishedAt", "").replace("Z", "+00:00"))
            dt = dt.replace(tzinfo=None) if dt.tzinfo else dt
        except: 
            dt = datetime(1970, 1, 1)  # Usa epoch en lugar de datetime.min
        try:
            ts = dt.timestamp()
        except:
            ts = 0
        return (-a.get("impact_score", 0), -ts)

    all_a.sort(key=sk)
    save_cache(all_a)
    return all_a[:n]

def analyze_consensus(news):
    if not news:
        return {"consensus": "Sin datos", "details": [], "avg_impact_score": 0,
                "weighted_sentiment": 0, "total_sources": 0}
    themes = {k: [] for k in ["FED/ECB", "INFLATION", "GDP/ECONOMY",
                                "INTEREST_RATES", "EURUSD", "BANKING/FINANCIAL",
                                "TRADE/WAR", "ENERGY/OIL", "GEOPOLITICS"]}
    total_imp = 0; w_sent = 0
    tb_cls = get_textblob()
    for a in news:
        text = (a.get("title", "") + " " + a.get("description", "")).upper()
        src  = a.get("source", {}).get("name", "Unknown")
        try:
            sent = tb_cls(text).sentiment.polarity if tb_cls else 0
        except Exception:
            sent = 0
        imp  = a.get("impact_score", 0); imp_n = imp / 100.0
        total_imp += imp_n; w_sent += sent * imp_n
        entry = {"source": src, "sentiment": sent, "impact": imp, "weighted": sent*imp_n}
        if any(k in text for k in ["FED","ECB","POWELL","LAGARDE"]):
            themes["FED/ECB"].append(entry)
        if "INFLATION" in text: themes["INFLATION"].append(entry)
        if any(k in text for k in ["GDP","ECONOMY","ECONOMIC"]):
            themes["GDP/ECONOMY"].append(entry)
        if any(k in text for k in ["INTEREST RATE","RATE HIKE","RATE CUT"]):
            themes["INTEREST_RATES"].append(entry)
        if "EUR" in text and "USD" in text: themes["EURUSD"].append(entry)
        if any(k in text for k in ["BANK","FINANCIAL","CREDIT"]):
            themes["BANKING/FINANCIAL"].append(entry)
        if any(k in text for k in ["TRADE","WAR","SANCTION","TARIFF"]):
            themes["TRADE/WAR"].append(entry)
        if any(k in text for k in ["OIL","ENERGY","GAS","CRUDE"]):
            themes["ENERGY/OIL"].append(entry)
        if any(k in text for k in ["GEOPOLITIC","POLITIC","ELECTION","GOVERNMENT"]):
            themes["GEOPOLITICS"].append(entry)

    details = []; pos_w = neg_w = 0
    for theme, srcs in themes.items():
        if not srcs: continue
        avg_s = sum(s["sentiment"] for s in srcs) / len(srcs)
        avg_i = sum(s["impact"]    for s in srcs) / len(srcs)
        avg_w = sum(s["weighted"]  for s in srcs) / len(srcs)
        details.append({"theme": theme, "avg_sentiment": avg_s,
                         "avg_impact": avg_i, "avg_weighted": avg_w,
                         "sources_count": len(srcs)})
        if avg_w >  0.05: pos_w += avg_w * len(srcs)
        if avg_w < -0.05: neg_w += abs(avg_w) * len(srcs)

    if   pos_w > neg_w * 1.5: cons = "Bullish (más fuentes positivas con mayor impacto)"
    elif neg_w > pos_w * 1.5: cons = "Bearish (más fuentes negativas con mayor impacto)"
    else:                      cons = "Mixed (consenso dividido)"

    total_sources = sum(len(v) for v in themes.values())
    return {"consensus": cons, "details": details, "total_sources": total_sources,
            "avg_impact_score": total_imp / len(news) * 100 if news else 0,
            "weighted_sentiment": w_sent}

def analyze_news():
    usd_score = eur_score = 0.0
    news = get_news()
    tb_cls = get_textblob()
    for a in news:
        text = (a.get("title","") + " " + a.get("description","")).upper()
        try:
            sent = tb_cls(text).sentiment if tb_cls else type('S', (), {'polarity':0,'subjectivity':0})()
            pol  = sent.polarity; subj = sent.subjectivity
        except Exception:
            pol = 0; subj = 0
        imp_m = a.get("impact_score", 0) / 100.0 * (0.5 if subj > 0.7 else 1.0)
        is_usd = any(k in text for k in ["FED","DOLLAR","USD","USA","TRUMP",
                                          "POWELL","FOMC","FEDERAL RESERVE"])
        is_eur = any(k in text for k in ["ECB","EURO","EUR","LAGARDE",
                                          "EUROZONE","EUROPEAN CENTRAL"])
        if is_usd and abs(pol) > 0.1:
            usd_score += pol * imp_m * 2; eur_score -= pol * imp_m * 1.5
        elif is_eur and abs(pol) > 0.1:
            eur_score += pol * imp_m * 2; usd_score -= pol * imp_m * 1.5
    return usd_score, eur_score, news, analyze_consensus(news)

# ============================================
# SEÑAL GLOBAL
# ============================================
def generate_signal():
    sig = {
        "direction": None, "reasons": [], "timeframes": {},
        "final_signal": "NO TRADE", "buy_signals": 0, "sell_signals": 0,
        "price": None, "atr_1h_pips": None
    }
    buy_signals = sell_signals = 0
    tfs = get_multiple_timeframes()
    for tf_name, df in tfs.items():
        a = analyze_timeframe(tf_name, df)
        sig["timeframes"][tf_name] = a
        if sig["price"] is None and tf_name == "15m": sig["price"] = a.get("price")
        if tf_name == "1h": sig["atr_1h_pips"] = a.get("atr")
        if a["signal"] == "COMPRA":
            buy_signals += 1
            sig["reasons"].append(f"{tf_name}: COMPRA ({a.get('trend','')})")
        elif a["signal"] == "VENTA":
            sell_signals += 1
            sig["reasons"].append(f"{tf_name}: VENTA ({a.get('trend','')})")
    if sig["price"] is None and tfs:
        sig["price"] = last_scalar(list(tfs.values())[0]["Close"])

    dxy_data = get_dxy()
    sig.update(dxy_data)
    if dxy_data.get("dxy_dir") == "UP":
        sell_signals += 1; sig["reasons"].append("DXY alcista → EUR bajista")
    elif dxy_data.get("dxy_dir") == "DOWN":
        buy_signals  += 1; sig["reasons"].append("DXY bajista → EUR alcista")

    usd_sc, eur_sc, news, consensus = analyze_news()
    sig.update({"news": news, "consensus": consensus})
    if usd_sc > eur_sc + 0.1:
        sell_signals += 1; sig["reasons"].append("Noticias USD positivas → bajista EUR")
    elif eur_sc > usd_sc + 0.1:
        buy_signals  += 1; sig["reasons"].append("Noticias EUR positivas → alcista EUR")

    session, volatility, sess_icon = get_market_session()
    sig.update({"session": session, "volatility": volatility, "sess_icon": sess_icon})
    sig["reasons"].append(f"Sesión: {session} — Volatilidad {volatility}")

    # Ventana horaria
    in_win, win_label, win_eta = get_trading_window_info()
    sig.update({"in_trading_window": in_win, "window_label": win_label, "window_eta": win_eta})

    sig["buy_signals"]  = buy_signals
    sig["sell_signals"] = sell_signals
    total = buy_signals + sell_signals
    if total == 0:
        sig["final_signal"] = "⚪ NO TRADE — Sin confluencia"
    elif buy_signals > sell_signals:
        sig["final_signal"] = f"🟢 COMPRA — Confluencia {buy_signals/total*100:.0f}%"
        sig["direction"] = "LONG"
    elif sell_signals > buy_signals:
        sig["final_signal"] = f"🔴 VENTA — Confluencia {sell_signals/total*100:.0f}%"
        sig["direction"] = "SHORT"
    else:
        sig["final_signal"] = "⚪ NO TRADE — Señales en conflicto"

    df_1h = get_eurusd_data("1h")
    liq_levels = detect_liquidity_levels(df_1h)
    tp, sl, rr, viable, risk_pips, liquidity_warnings = calc_scalp_levels(
        sig["price"], sig["direction"], df_1h, sig["atr_1h_pips"], liq_levels)
    sig.update({"tp": tp, "sl": sl, "rr": rr, "viable": viable, "risk_pips": risk_pips, "liquidity_warnings": liquidity_warnings})

    # ── Señal KB: selecciona estrategia por régimen actual (técnico + fundamental) ──
    kb          = load_knowledge_base()
    strat_wins  = kb.get("strategy_wins", {})
    _cot_sig    = None   # COT no disponible en generate_signal (viene del session_state en UI)
    _cal_sig    = None
    try:
        _cal_sig = get_economic_calendar()
    except Exception:
        pass

    kb_direction, kb_reason = "NO TRADE", "Sin historial de backtest"
    best_strat = regime_key = regime_lbl = why_selection = None
    regime_details = {}
    if not df_1h.empty:
        try:
            best_strat, regime_key, regime_lbl, regime_details, why_selection = \
                kb_best_strategy_for_conditions(df_1h, cot=_cot_sig, calendar=_cal_sig)
        except Exception:
            best_strat = kb.get("best_strategy")
            why_selection = "Selección por histórico global"
        if best_strat:
            kb_direction, kb_reason = _live_strategy_signal(df_1h, best_strat)

    sig["kb_best_strategy"]   = best_strat
    sig["kb_direction"]       = kb_direction
    sig["kb_reason"]          = kb_reason
    sig["kb_strategy_wins"]   = strat_wins
    sig["kb_runs"]            = len(kb.get("runs", []))
    sig["kb_signal_stats"]    = kb.get("signal_stats", {})
    sig["kb_regime"]          = regime_key
    sig["kb_regime_label"]    = regime_lbl
    sig["kb_regime_details"]  = regime_details
    sig["kb_why_selection"]   = why_selection
    return sig

# ============================================
# INTERFAZ STREAMLIT
# ============================================
_APP_RERUN_START = time.time()
st = get_streamlit()
if not st:
    print("Streamlit no disponible. Ejecutando en modo no-interactivo.")
    connected = is_mt5_available() and mt5_connect()
    data_src = "MT5 (tiempo real)" if connected else "yfinance (delay ~15min)"
    print(f"UTC: {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} | Datos: {data_src}")
else:
    st.set_page_config(
        page_title="SMC Pro v2 — EURUSD + MT5",
        page_icon="⚡", layout="wide"
    )

    # Inicializar session state para análisis y credenciales
    if "last_analysis_time" not in st.session_state:
        st.session_state.last_analysis_time = None
    if "analysis_executed" not in st.session_state:
        st.session_state.analysis_executed = False
    if "backtest_result" not in st.session_state:
        st.session_state.backtest_result = None
    if "strategy_comparison" not in st.session_state:
        st.session_state.strategy_comparison = None
    if "market_context_reasons" not in st.session_state:
        st.session_state.market_context_reasons = None
    if "economic_calendar" not in st.session_state:
        st.session_state.economic_calendar = None
    if "cot_data" not in st.session_state:
        st.session_state.cot_data = None
    cfg = load_user_config()

    if "mt5_login" not in st.session_state:
        st.session_state.mt5_login = os.environ.get("MT5_LOGIN", cfg.get("MT5_LOGIN", "")) or ""
    if "mt5_password" not in st.session_state:
        st.session_state.mt5_password = os.environ.get("MT5_PASSWORD", cfg.get("MT5_PASSWORD", "")) or ""
    if "mt5_server" not in st.session_state:
        st.session_state.mt5_server = os.environ.get("MT5_SERVER", cfg.get("MT5_SERVER", "")) or ""
    if "tg_token" not in st.session_state:
        st.session_state.tg_token = TELEGRAM_TOKEN if TELEGRAM_TOKEN != "TU_TELEGRAM_BOT_TOKEN" else ""
    if "tg_chat" not in st.session_state:
        st.session_state.tg_chat = TELEGRAM_CHAT_ID if TELEGRAM_CHAT_ID != "TU_CHAT_ID" else ""
    if "symbol" not in st.session_state:
        st.session_state.symbol = SYMBOL

    st.markdown("""
    <style>
    .big-signal{font-size:2.2rem;font-weight:800;text-align:center;
                padding:1rem;border-radius:10px;margin-bottom:1rem}
    .sl{background:#0f5132;color:#d1e7dd}
    .ss{background:#842029;color:#f8d7da}
    .sw{background:#332701;color:#fff3cd}
    .scalp-box{border:1px solid #555;border-radius:8px;padding:0.8rem;
               background:#1a1a2e;margin-top:0.5rem}
    .score-box{border-radius:12px;padding:1rem;text-align:center;
               font-size:1.8rem;font-weight:800;margin:0.5rem 0}
    .vol-bar{height:18px;border-radius:4px;background:#00ff88;margin:2px 0}
    </style>
    """, unsafe_allow_html=True)

    mt5_login = st.session_state.mt5_login or None
    mt5_password = st.session_state.mt5_password or None
    mt5_server = st.session_state.mt5_server or None
    connected = is_mt5_available() and mt5_connect(
        login=mt5_login,
        password=mt5_password,
        server=mt5_server
    )
    data_src  = "🟢 MT5 (tiempo real)" if connected else "🟡 yfinance (delay ~15min)"

    st.title("⚡ SMC Pro v2 — EURUSD Scalper + MT5")
    st.caption(
        f"UTC: {datetime.utcnow().strftime('%Y-%m-%d %H:%M')}  |  "
        f"Datos: {data_src}  |  "
        f"TP variable · SL {SCALP_SL_PIPS}p máx · R:R 1:2-3"
    )

    # ── Ventana horaria de trading ─────────────────────────────────────────
    _win_in, _win_label, _win_eta = get_trading_window_info()
    if _win_in:
        st.success(f"✅ **HORARIO ACTIVO** — {_win_label} | {_win_eta}")
    else:
        st.warning(f"⏸️ **FUERA DE HORARIO** — {_win_label} | {_win_eta}")

    # ── Estado de Posición ──────────────────────────────────────────────────────
    position_state = load_position_state()
    if position_state["is_open"]:
        direction_emoji = "📈" if position_state["direction"] == "LONG" else "📉"
        entry_time = position_state["entry_time"]
        if isinstance(entry_time, str):
            entry_time = datetime.fromisoformat(entry_time)

        time_open = datetime.now() - entry_time
        hours_open = int(time_open.total_seconds() // 3600)
        minutes_open = int((time_open.total_seconds() % 3600) // 60)

        st.info(
            f"🔥 **POSICIÓN ACTIVA** {direction_emoji}  \n"
            f"**Entrada:** {position_state['entry_price']:.5f}  |  "
            f"**TP:** {position_state['tp']:.5f}  |  "
            f"**SL:** {position_state['sl']:.5f}  |  "
            f"**Score:** {position_state['score']}/100  |  "
            f"**Tiempo:** {hours_open}h {minutes_open}m"
        )
    else:
        st.success("🎯 **SIN POSICIÓN** — Esperando señal definitiva (>70%)")

    # ── Sidebar ───────────────────────────────────────────────────────────────────
    with st.sidebar:
        st.header("⚙️ Configuración")
        st.subheader("🖥️ MetaTrader 5")
        if is_mt5_available():
            # Campos para login
            st.write("**Credenciales de MT5:**")
            mt5_login = st.text_input(
                "MT5 Login",
                value=st.session_state.mt5_login,
                key="mt5_login",
                placeholder="Ej: 1234567",
                help="ID de tu cuenta MT5"
            )
            mt5_password = st.text_input(
                "MT5 Password",
                value=st.session_state.mt5_password,
                key="mt5_password",
                type="password",
                placeholder="Tu contraseña",
                help="Contraseña de MT5"
            )
            mt5_server = st.text_input(
                "MT5 Server",
                value=st.session_state.mt5_server,
                key="mt5_server",
                placeholder="Ej: ICMarketsSC-Demo",
                help="Servidor MT5 (opcional)"
            )
            
            # Botón para conectar con credenciales
            if st.button("🔗 Conectar MT5 con Credenciales"):
                if st.session_state.mt5_login and st.session_state.mt5_password:
                    with st.spinner("Conectando..."):
                        if mt5_connect(
                            login=st.session_state.mt5_login,
                            password=st.session_state.mt5_password,
                            server=st.session_state.mt5_server or None
                        ):
                            st.success("✅ Conectado a MT5")
                            save_user_config({
                                "MT5_LOGIN": st.session_state.mt5_login,
                                "MT5_PASSWORD": st.session_state.mt5_password,
                                "MT5_SERVER": st.session_state.mt5_server,
                            })
                        else:
                            st.error(f"❌ Error de conexión: {get_mt5_error()}")
                else:
                    st.error("Completa Login y Password")
            
            st.markdown("---")
            
            if connected:
                st.success("✅ MT5 conectado")
                acct = get_mt5_account()
                if acct:
                    st.markdown(
                        f"**Servidor:** {acct['server']}  \n"
                        f"**Cuenta:** {acct['name']}  \n"
                        f"**Balance:** {acct['balance']:.2f} {acct['currency']}  \n"
                        f"**Equity:** {acct['equity']:.2f} {acct['currency']}  \n"
                        f"**Profit:** {acct['profit']:+.2f}  \n"
                        f"**Apalancamiento:** 1:{acct['leverage']}"
                    )
            else:
                st.info("ℹ️ Ingresa credenciales arriba o abre MT5")
        else:
            if sys.platform != "win32":
                st.info(
                    "ℹ️ **MT5 no disponible en servidor cloud (Linux)**\n\n"
                    "MetaTrader5 solo funciona en **Windows**. En la versión web, "
                    "la app opera en modo **análisis** (señales, backtest, alertas Telegram) "
                    "sin ejecución automática de órdenes.\n\n"
                    "Para ejecutar órdenes reales, instala la app localmente en tu PC con Windows."
                )
            else:
                st.warning(
                    "⚠️ Paquete MetaTrader5 no instalado.\n\n"
                    f"Ejecuta en tu terminal: `pip install MetaTrader5`"
                )

        symbol_input = st.text_input("Símbolo MT5", value=SYMBOL,
                                      help="Ej: EURUSD, EURUSDm, EURUSD.")
        if symbol_input != SYMBOL:
            SYMBOL = symbol_input

        st.markdown("---")
        st.subheader("📱 Telegram")
        tg_token  = st.text_input(
            "Bot Token",
            value=st.session_state.tg_token,
            key="tg_token",
            type="password"
        )
        tg_chat   = st.text_input(
            "Chat ID",
            value=st.session_state.tg_chat,
            key="tg_chat"
        )
        min_score = st.slider("Score mínimo para alerta", 50, 90, 70)
        if st.session_state.tg_token:
            st.success("✅ Telegram OK")

        st.markdown("---")
        st.subheader("🎯 Gestión de Posiciones")
        position_state = load_position_state()

        if position_state["is_open"]:
            st.warning(f"Posición {position_state['direction']} abierta")
            if st.button("🔒 Cerrar Posición Manualmente", type="primary"):
                if close_position("MANUAL"):
                    st.success("✅ Posición cerrada manualmente")
                    st.rerun()
                else:
                    st.error("❌ Error cerrando posición")

    st.markdown("---")
    st.subheader("� Historial de Operaciones")

    trades = load_trades_history()
    if trades:
        # Mostrar estadísticas rápidas
        recent_trades = trades[-10:]  # Últimas 10 operaciones
        total_trades = len([t for t in trades if t["type"] == "CLOSE"])
        wins = len([t for t in trades if t["type"] == "CLOSE" and t.get("outcome") == "TP"])
        winrate = wins / total_trades * 100 if total_trades > 0 else 0

        col1, col2, col3 = st.columns(3)
        col1.metric("Total Cerradas", total_trades)
        col2.metric("✅ Wins", wins)
        col3.metric("Winrate", f"{winrate:.1f}%")

        # Mostrar últimas operaciones
        st.write("**Últimas operaciones:**")
        for trade in reversed(recent_trades[-5:]):  # Mostrar últimas 5
            timestamp = trade["timestamp"]
            if isinstance(timestamp, str):
                timestamp = datetime.fromisoformat(timestamp)

            emoji = {"OPEN": "🚀", "BE": "⚖️", "CLOSE": "🔒"}.get(trade["type"], "❓")
            outcome_emoji = {"TP": "✅", "SL": "❌", "BE": "⚖️", "MANUAL": "🔄"}.get(trade.get("outcome"), "")
            pips = trade.get('pips')
            pips_display = f"{pips:+.1f}p" if isinstance(pips, (int, float)) else "N/A"

            st.write(f"{emoji} {trade['type']} {outcome_emoji} "
                    f"{trade.get('direction', '')} "
                    f"{timestamp.strftime('%H:%M')} "
                    f"{pips_display}")
    else:
        st.info("Sin operaciones registradas aún")

    st.markdown("---")
    st.subheader("�🔄 Auto-actualización")
    _refresh_opts = ["Desactivado", "1 minuto", "2 minutos", "5 minutos", "10 minutos"]
    if "refresh_select" not in st.session_state:
        st.session_state["refresh_select"] = "Desactivado"
    refresh_option = st.selectbox(
        "Refrescar cada:",
        _refresh_opts,
        key="refresh_select"
    )
    refresh_map  = {"Desactivado": 0, "1 minuto": 60, "2 minutos": 120,
                    "5 minutos": 300, "10 minutos": 600}
    refresh_secs = refresh_map[refresh_option]
    if refresh_secs > 0: st.success(f"✅ Auto-refresh activo: cada {refresh_option}")
    else:                st.info("Auto-refresh desactivado — pulsa el boton para analizar")
    st.markdown("---")
    st.caption("⚠️ Solo informativo. No es consejo financiero.")

# ── Botón ─────────────────────────────────────────────────────────────────────
run_analysis = st.button("🔍 ANALIZAR MERCADO", type="primary", use_container_width=True)

# Guard de análisis: evita doble-trigger y asegura intervalo mínimo
should_auto_refresh = False
if refresh_secs > 0:
    _t_now  = time.time()
    _t_last = st.session_state.last_analysis_time
    if _t_last is None:
        # Primera carga: analizar inmediatamente
        should_auto_refresh = True
    else:
        _elapsed = _t_now - _t_last
        # Requiere que haya pasado al menos el 95% del intervalo para evitar doble-trigger
        if _elapsed >= refresh_secs * 0.95:
            should_auto_refresh = True

run_fresh_analysis = run_analysis or should_auto_refresh
if run_fresh_analysis:
    # Fijar timestamp ANTES del análisis para que el timer empiece desde aquí
    st.session_state.last_analysis_time = time.time()
    st.session_state.analysis_executed = True

# Valores por defecto para evitar errores cuando no se ha analizado aún
signal      = {}
consensus   = {}
session     = ""
dxy_dir     = ""
dxy_trend   = "N/A"
dxy_price   = None
direction   = None
dxy_chg     = 0
avg_impact  = 0
total_sources = 0
vol_spikes  = []
vol_trend   = None
delta       = None
cvd         = None
vol_profile = []
poc         = None
liq_levels  = []
score       = 0
label       = ""
price       = None
tick        = None
# Nuevos: estructura, IA, manipulación
market_structures = {}
stop_hunts        = []
vol_absorption    = None
ai_patterns       = []
patterns_score    = 0
trend_strength_1h = {}
ai_bias           = {}
tp1 = tp2 = tp3 = smart_sl = rr2 = risk_pips_smart = atr_val = None
smart_warnings    = []

if st.session_state.analysis_executed:
    if run_fresh_analysis:
        with st.spinner("Obteniendo datos de MT5…"):
            signal   = generate_signal()
            tick     = get_mt5_tick(SYMBOL) if connected else None
            df_1h    = get_eurusd_data("1h")
            df_15    = get_eurusd_data("15m")

        # Análisis de volumen completo
        vol_spikes   = detect_volume_spikes(df_1h)
        vol_trend    = detect_volume_trend(df_1h)
        delta        = get_volume_delta(df_1h)
        cvd          = get_cvd(df_1h)
        vol_profile, poc = analyze_volume_profile(df_1h) if not df_1h.empty else ([], None)
        liq_levels   = detect_liquidity_levels(df_1h)

        # Recalcular niveles de scalping con información de liquidez
        if signal.get("direction") and signal.get("price"):
            tp, sl, rr, viable, risk_pips, liquidity_warnings = calc_scalp_levels(
                signal["price"], signal["direction"], df_1h, signal.get("atr_1h_pips"), liq_levels)
            signal.update({"tp": tp, "sl": sl, "rr": rr, "viable": viable, "risk_pips": risk_pips, "liquidity_warnings": liquidity_warnings})

        # ── Análisis avanzado: estructura, IA, manipulación ──────────────────
        df_4h = get_eurusd_data("4h")
        df_1d = get_eurusd_data("1d")
        market_structures = {
            "15m": detect_market_structure(df_15),
            "1h":  detect_market_structure(df_1h),
            "4h":  detect_market_structure(df_4h),
            "1d":  detect_market_structure(df_1d),
        }
        stop_hunts        = detect_stop_hunt(df_1h)
        vol_absorption    = detect_volume_absorption(df_1h)
        ai_patterns, patterns_score = ai_candlestick_patterns(df_15)
        trend_strength_1h = calculate_trend_strength(df_1h)
        ai_bias           = ai_market_bias(signal, market_structures, vol_absorption,
                                           stop_hunts, patterns_score)
        ms_1h = market_structures.get("1h", {})
        tp1, tp2, tp3, smart_sl, rr2, risk_pips_smart, atr_val, smart_warnings = calc_smart_tp_sl(
            signal.get("price"), signal.get("direction"), df_1h, liq_levels,
            ms_1h, signal.get("atr_1h_pips"))

        # ── Auto-aprendizaje: evaluar señal anterior + generar contexto ────────
        _cur_price = signal.get("price")
        if _cur_price:
            # Evalúa si la señal anterior fue correcta y actualiza KB
            try:
                kb_evaluate_and_learn(_cur_price)
            except Exception:
                pass
            # Guarda la nueva señal KB como pendiente para evaluarla en el próximo ciclo
            _kb_dir   = signal.get("kb_direction", "NO TRADE")
            _kb_strat = signal.get("kb_best_strategy") or "none"
            _kb_rsn   = signal.get("kb_reason", "")
            if _kb_dir != "NO TRADE":
                try:
                    _cot_rec = st.session_state.get("cot_data")
                    _cal_rec = st.session_state.get("economic_calendar") or get_economic_calendar()
                    kb_record_pending_signal(
                        _kb_dir, _cur_price, _kb_strat, _kb_rsn,
                        df=df_1h, cot=_cot_rec, calendar=_cal_rec
                    )
                except Exception:
                    pass

        # ── Contexto fundamental automático ─────────────────────────────────
        try:
            _cot_auto = st.session_state.get("cot_data")
            _cal_auto = st.session_state.get("economic_calendar") or get_economic_calendar()
            _news_auto = signal.get("news", [])
            _ctx_auto  = explain_market_context(df_1h, cot=_cot_auto, calendar=_cal_auto, news=_news_auto)
            st.session_state.market_context_reasons = _ctx_auto
        except Exception:
            pass

        # Guardar resultados en caché para los reruns del temporizador
        st.session_state._analysis_cache = {
            "signal": signal, "tick": tick, "df_1h": df_1h, "df_15": df_15,
            "vol_spikes": vol_spikes, "vol_trend": vol_trend, "delta": delta,
            "cvd": cvd, "vol_profile": vol_profile, "poc": poc, "liq_levels": liq_levels,
            "market_structures": market_structures, "stop_hunts": stop_hunts,
            "vol_absorption": vol_absorption, "ai_patterns": ai_patterns,
            "patterns_score": patterns_score, "trend_strength_1h": trend_strength_1h,
            "ai_bias": ai_bias,
            "tp1": tp1, "tp2": tp2, "tp3": tp3, "smart_sl": smart_sl,
            "rr2": rr2, "risk_pips_smart": risk_pips_smart, "atr_val": atr_val,
            "smart_warnings": smart_warnings,
        }
    else:
        # Rerun del temporizador: usar datos cacheados sin volver a pedir MT5/yfinance
        _c = st.session_state.get("_analysis_cache", {})
        signal            = _c.get("signal", {})
        tick              = _c.get("tick")
        df_1h             = _c.get("df_1h", pd.DataFrame())
        df_15             = _c.get("df_15", pd.DataFrame())
        vol_spikes        = _c.get("vol_spikes", [])
        vol_trend         = _c.get("vol_trend")
        delta             = _c.get("delta")
        cvd               = _c.get("cvd")
        vol_profile       = _c.get("vol_profile", [])
        poc               = _c.get("poc")
        liq_levels        = _c.get("liq_levels", [])
        market_structures = _c.get("market_structures", {})
        stop_hunts        = _c.get("stop_hunts", [])
        vol_absorption    = _c.get("vol_absorption")
        ai_patterns       = _c.get("ai_patterns", [])
        patterns_score    = _c.get("patterns_score", 0)
        trend_strength_1h = _c.get("trend_strength_1h", {})
        ai_bias           = _c.get("ai_bias", {})
        tp1               = _c.get("tp1")
        tp2               = _c.get("tp2")
        tp3               = _c.get("tp3")
        smart_sl          = _c.get("smart_sl")
        rr2               = _c.get("rr2")
        risk_pips_smart   = _c.get("risk_pips_smart")
        atr_val           = _c.get("atr_val")
        smart_warnings    = _c.get("smart_warnings", [])

    consensus    = signal.get("consensus", {})
    session      = signal.get("session", "")
    dxy_dir      = signal.get("dxy_dir", "")
    dxy_trend    = signal.get("dxy_trend", "N/A")
    dxy_price    = signal.get("dxy_price")
    direction    = signal.get("direction")
    dxy_chg      = signal.get("dxy_chg") or 0
    avg_impact   = consensus.get("avg_impact_score", 0)
    total_sources= consensus.get("total_sources", 0)

    # ── Panel MT5 ─────────────────────────────────────────────────────────────
    if tick:
        st.markdown("---")
        st.subheader("📡 MT5 — Precio en Tiempo Real")
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Bid", f"{tick['bid']:.5f}")
        m2.metric("Ask", f"{tick['ask']:.5f}")
        sp = tick["spread_pips"]
        m3.metric("Spread", f"{sp} pips",
                  delta="✅ OK" if sp < 1.5 else "⚠️ Alto")
        m4.metric("Hora tick", tick["time"].strftime("%H:%M:%S"))
        if sp > 2:
            st.warning(f"⚠️ Spread de {sp} pips — espera que baje antes de entrar")

    # ── Señal principal ───────────────────────────────────────────────────────
    st.markdown("---")
    final = signal.get("final_signal", "⚪ NO TRADE")
    css   = "sl" if "COMPRA" in final else ("ss" if "VENTA" in final else "sw")
    st.markdown(f'<div class="big-signal {css}">{final}</div>', unsafe_allow_html=True)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("✅ Señales COMPRA", signal.get("buy_signals", 0))
    c2.metric("❌ Señales VENTA",  signal.get("sell_signals", 0))
    _sess_icon = signal.get("sess_icon", "🕐")
    _session   = signal.get("session", "Desconocida")
    c3.metric(f"{_sess_icon} Sesión", _session.split(" ")[0] if _session else "—")
    c4.metric("⚡ Volatilidad", signal.get("volatility", "—"))

    # ── Panel Inteligencia Adaptativa (KB + Señal Estrategia + Régimen) ─────────
    kb_dir          = signal.get("kb_direction", "NO TRADE")
    kb_strat        = signal.get("kb_best_strategy")
    kb_rsn          = signal.get("kb_reason", "Sin historial")
    kb_runs         = signal.get("kb_runs", 0)
    kb_wins         = signal.get("kb_strategy_wins", {})
    kb_stats        = signal.get("kb_signal_stats", {})
    kb_regime       = signal.get("kb_regime", "unknown")
    kb_regime_lbl   = signal.get("kb_regime_label", "Desconocido")
    kb_regime_det   = signal.get("kb_regime_details", {})
    kb_why_sel      = signal.get("kb_why_selection", "")
    if kb_strat:
        st.markdown("---")
        # Título con régimen actual
        _regime_icon = _REGIME_ICONS.get(kb_regime, "❓")
        st.subheader(f"🧠 Señal Inteligente — {_regime_icon} Régimen: {kb_regime_lbl}")

        # Fila: régimen + stats de régimen
        _r1, _r2, _r3, _r4 = st.columns(4)
        _r1.metric("Régimen actual", f"{_regime_icon} {kb_regime_lbl}")
        _r_s    = kb_stats.get(kb_strat, {}).get("by_regime", {}).get(kb_regime, {})
        _r_ok   = _r_s.get("correct", 0)
        _r_ko   = _r_s.get("wrong", 0)
        _r_acc  = f"{_r_ok/(_r_ok+_r_ko)*100:.0f}%" if (_r_ok + _r_ko) > 0 else "Sin datos"
        _r2.metric(f"Acierto en {kb_regime_lbl[:12]}", _r_acc)
        _r3.metric("ATR actual (pips)", f"{kb_regime_det.get('atr_pips', '—')}")
        _r4.metric("RSI actual", f"{kb_regime_det.get('rsi', '—')}")

        # Señal principal
        meta_label = _STRATEGY_META.get(kb_strat, {}).get("label", kb_strat)
        _kb_css   = "sl" if kb_dir == "LONG" else ("ss" if kb_dir == "SHORT" else "sw")
        _kb_emoji = "🟢" if kb_dir == "LONG" else ("🔴" if kb_dir == "SHORT" else "⚪")
        _kb_txt   = "COMPRA" if kb_dir == "LONG" else ("VENTA" if kb_dir == "SHORT" else "SIN SETUP")
        st.markdown(
            f'<div class="big-signal {_kb_css}" style="font-size:1.4rem;padding:12px">'
            f'{_kb_emoji} {_kb_txt} — {meta_label}</div>',
            unsafe_allow_html=True
        )
        st.caption(f"💡 {kb_rsn}")
        if kb_why_sel:
            st.caption(f"📊 **Por qué esta estrategia:** {kb_why_sel}")

        # Métricas globales de la estrategia
        _a1, _a2, _a3, _a4 = st.columns(4)
        _a1.metric("Backtests en KB", kb_runs)
        _a2.metric("Veces nº1 global", kb_wins.get(kb_strat, 0))
        _strat_s = kb_stats.get(kb_strat, {})
        _ok = _strat_s.get("correct", 0)
        _ko = _strat_s.get("wrong", 0)
        _acc = f"{_ok/(_ok+_ko)*100:.0f}%" if (_ok + _ko) > 0 else "—"
        _a3.metric("Aciertos señal (global)", f"{_ok}✅ / {_ko}❌")
        _a4.metric("Tasa acierto (global)", _acc)

        # Tabla de aciertos por régimen para esta estrategia
        _by_regime = _strat_s.get("by_regime", {})
        if _by_regime:
            with st.expander("📊 Rendimiento por régimen de mercado", expanded=False):
                _rows = []
                for _rk, _rv in _by_regime.items():
                    _rok = _rv.get("correct", 0)
                    _rko = _rv.get("wrong", 0)
                    _rtot = _rok + _rko
                    _rwr = f"{_rok/_rtot*100:.0f}%" if _rtot > 0 else "—"
                    _rows.append({
                        "Régimen": f"{_REGIME_ICONS.get(_rk,'❓')} {_REGIME_LABELS.get(_rk, _rk)}",
                        "Señales": _rtot,
                        "Correctas": _rok,
                        "Erróneas": _rko,
                        "Tasa acierto": _rwr,
                    })
                if _rows:
                    import pandas as _pd_local
                    st.dataframe(_pd_local.DataFrame(_rows), hide_index=True, use_container_width=True)

        # Contexto fundamental automático (por qué se mueve)
        _ctx = st.session_state.get("market_context_reasons")
        if _ctx:
            with st.expander("🔍 Por qué se mueve el mercado ahora (técnico + fundamental)", expanded=False):
                for _r in _ctx:
                    st.markdown(f"- {_r}")
                st.caption("Fuentes: EMA · RSI · MACD · COT CFTC · Calendario ForexFactory · RSS noticias")

    # ── Score de confluencia ──────────────────────────────────────────────────
    st.markdown("---")
    st.subheader("🎯 Score de Confluencia")
    score, score_reasons = calculate_confluence_score(
        signal, consensus, dxy_dir, session, vol_spikes, liq_levels, delta,
        cot=st.session_state.get("cot_data"),
        trend_strength=trend_strength_1h)
    label, color = score_label(score)
    col_sc1, col_sc2 = st.columns([1, 2])
    with col_sc1:
        bg = {"green": "#0f5132", "lightgreen": "#1a3a1a",
              "orange": "#4a2e00", "red": "#4a0000"}.get(color, "#333")
        st.markdown(
            f'<div class="score-box" style="background:{bg};color:white">'
            f'{score}/100<br><small>{label}</small></div>',
            unsafe_allow_html=True
        )
        # Sistema de posiciones definitivas
        position_state = load_position_state()
        current_price = tick['bid'] if tick else None

        if position_state["is_open"]:
            # Verificar si la posición se cerró o alcanzó BE
            closed, outcome, be_reached = check_position_status(current_price)
            if closed:
                close_position(outcome)
                st.info(f"🔒 Posición cerrada por {outcome}")
            elif be_reached:
                # Enviar alerta BE
                send_be_alert({
                    "direction": position_state["direction"],
                    "price": position_state["entry_price"],
                    "tp": position_state["tp"],
                    "sl": position_state["sl"]
                })
                st.success("⚖️ ¡Break Even alcanzado!")
            else:
                st.warning("📍 Posición abierta — Esperando resolución")
                # No enviar nuevas señales mientras hay posición abierta
        else:
            # No hay posición abierta — buscar señal definitiva
            if score >= MIN_DEFINITIVE_SCORE and signal.get("direction"):
                if open_definitive_position(signal, score):
                    st.success("🚨 ¡Posición definitiva abierta!")
                else:
                    st.error("❌ Error abriendo posición definitiva")
            elif score >= min_score and signal.get("direction"):
                st.info("📊 Señal detectada — Esperando >70% para definitiva")

        if score >= 70:   st.success("✅ Score válido para considerar entrada")
        elif score >= 50: st.warning("⚠️ Score bajo — espera mejor confluencia")
        else:             st.error("❌ NO operar — score insuficiente")

        # Ventana horaria
        _win_ok = signal.get("in_trading_window", True)
        _win_lbl = signal.get("window_label", "")
        _win_eta = signal.get("window_eta", "")
        if _win_ok:
            st.success(f"✅ Horario OK: {_win_lbl}")
        else:
            st.error(f"⏸️ FUERA HORARIO: {_win_lbl} — {_win_eta}")
    with col_sc2:
        st.write("**Desglose del score:**")
        for r in score_reasons: st.write(f"• {r}")

    # ── VOLUMEN — Panel principal ─────────────────────────────────────────────
    st.markdown("---")
    st.subheader("📊 Análisis de Volumen Completo (MT5 Tick Volume)")

    tab1, tab2, tab3 = st.tabs(["🔥 Spikes & Tendencia", "📦 Delta & CVD", "📈 Volume Profile"])

    with tab1:
        v1, v2 = st.columns(2)
        with v1:
            st.write("**Spike de Volumen:**")
            if vol_spikes:
                for vs in vol_spikes:
                    st.error(
                        f"{vs['emoji']} **{vs['tipo']}**\n\n"
                        f"Ratio: **{vs['ratio']}x** sobre la media\n\n"
                        f"{vs['mensaje']}"
                    )
            else:
                st.info("⚪ Volumen normal — sin spikes detectados")
        with v2:
            st.write("**Tendencia de Volumen:**")
            st.info(vol_trend)
            if not df_1h.empty and "Volume" in df_1h.columns:
                recent_vol = df_1h["Volume"].tail(10)
                avg_v = float(recent_vol.mean())
                cur_v = float(recent_vol.iloc[-1])
                st.metric("Volumen actual (tick)", int(cur_v),
                          delta=f"{((cur_v-avg_v)/avg_v*100):+.1f}% vs media" if avg_v > 0 else None)

    with tab2:
        d1, d2 = st.columns(2)
        with d1:
            st.write("**Delta de Volumen (últimas 20 velas):**")
            if delta:
                bias_color = "🟢" if delta["delta"] > 0 else "🔴"
                st.metric("Bias", f"{bias_color} {delta['bias']}")
                st.metric("Vol. Compradores", f"{delta['bull_vol']:,}")
                st.metric("Vol. Vendedores",  f"{delta['bear_vol']:,}")
                st.metric("Delta neto",       f"{delta['delta']:+,} ({delta['delta_pct']:+.1f}%)")
                if delta["delta"] > 0:
                    st.success("✅ Presión compradora dominante")
                else:
                    st.error("🔴 Presión vendedora dominante")
            else:
                st.info("Sin datos de delta disponibles.")
        with d2:
            st.write("**CVD — Cumulative Volume Delta:**")
            if cvd and len(cvd) > 1:
                cvd_series = pd.Series(cvd, name="CVD")
                final_cvd  = cvd[-1]
                trend_cvd  = "ALCISTA 📈" if final_cvd > cvd[0] else "BAJISTA 📉"
                st.metric("CVD final", f"{final_cvd:+,.0f}", trend_cvd)
                st.line_chart(cvd_series)
            else:
                st.info("Sin datos CVD disponibles.")

    with tab3:
        st.write("**Volume Profile (últimas 300 velas 1h):**")
        if vol_profile and poc:
            st.info(f"📍 **POC (Point of Control):** `{poc['precio']:.5f}` "
                    f"— mayor volumen en este nivel ({poc['volumen']:,} ticks)")
            profile_data = []
            for lvl in vol_profile:
                bar_width = int(lvl["pct"])
                is_poc    = abs(lvl["precio"] - poc["precio"]) < 0.0001
                tag       = " ← POC 🎯" if is_poc else ""
                profile_data.append({
                    "Precio":  lvl["precio"],
                    "Volumen": lvl["volumen"],
                    "% del max": lvl["pct"],
                    "Nivel":   f"{'🔴' if is_poc else '🟦'}{tag}"
                })
            st.dataframe(pd.DataFrame(profile_data),
                         use_container_width=True, hide_index=True)
        else:
            st.info("Sin datos de Volume Profile.")

    # ── Niveles scalping inteligentes ─────────────────────────────────────────
    st.markdown("---")
    st.subheader("🎯 Niveles de Scalping — TP1 / TP2 / TP3 + SL Estructural")
    price     = signal.get("price")
    tp        = signal.get("tp"); sl = signal.get("sl")
    rr        = signal.get("rr")
    atr_pips  = signal.get("atr_1h_pips"); risk_pips = signal.get("risk_pips")
    if tick: price = tick["bid"]

    if price and direction:
        # Mostrar targets múltiples si están calculados
        if tp1 and tp2 and tp3 and smart_sl:
            st.caption(f"SL estructural · ATR base: {atr_val:.1f} pips" if atr_val is not None else "SL estructural")
            col_t0, col_t1, col_t2, col_t3, col_sl = st.columns(5)
            col_t0.metric("💰 Precio",  f"{price:.5f}")
            col_t1.metric("🎯 TP1 (1:1)", f"{tp1:.5f}",
                          f"+{abs(tp1-price)/PIP:.1f}p")
            col_t2.metric("🎯 TP2 (1:2)", f"{tp2:.5f}",
                          f"+{abs(tp2-price)/PIP:.1f}p · R:{rr2:.2f}" if rr2 else f"+{abs(tp2-price)/PIP:.1f}p")
            col_t3.metric("🎯 TP3 (1:3)", f"{tp3:.5f}",
                          f"+{abs(tp3-price)/PIP:.1f}p")
            col_sl.metric("🛡️ SL Estructural", f"{smart_sl:.5f}",
                          f"-{abs(price-smart_sl)/PIP:.1f}p")
            if smart_warnings:
                st.success("🧠 **IA:** " + " | ".join(smart_warnings))
            # Estrategia recomendada
            st.info(
                f"**Estrategia sugerida:** Cierra 50% en TP1 ({abs(tp1-price)/PIP:.0f}p), "
                f"mueve SL a BE. Deja correr el resto hasta TP2 ({abs(tp2-price)/PIP:.0f}p). "
                f"TP3 solo si hay impulso fuerte."
            )
        elif tp and sl and price:
            # Fallback a niveles clásicos
            s1, s2, s3, s4 = st.columns(4)
            s1.metric("💰 Precio",      f"{price:.5f}")
            s2.metric("🎯 Take Profit", f"{tp:.5f}", f"+{abs(tp-price)/PIP:.1f}p")
            s3.metric("🛡️ Stop Loss",   f"{sl:.5f}", f"-{abs(price-sl)/PIP:.1f}p")
            s4.metric("📐 R:R",         f"1:{rr:.2f}" if rr else "—")
        liquidity_warnings = signal.get("liquidity_warnings", [])
        if liquidity_warnings:
            for w in liquidity_warnings:
                st.warning(w)
    else:
        st.info("Realiza el análisis para calcular niveles.")

    # ── Estructura de Mercado ─────────────────────────────────────────────────
    st.markdown("---")
    st.subheader("🏗️ Estructura de Mercado — Multi-Timeframe")
    if market_structures:
        ms_cols = st.columns(4)
        for idx_tf, tf_key in enumerate(["15m", "1h", "4h", "1d"]):
            ms = market_structures.get(tf_key, {})
            tendencia = ms.get("tendencia", "—")
            estructura = ms.get("estructura", "—")
            score_ms = ms.get("score", 50)
            color_ms = "🟢" if tendencia == "ALCISTA" else ("🔴" if tendencia == "BAJISTA" else "🟡")
            ms_cols[idx_tf].metric(
                f"{color_ms} {tf_key.upper()}",
                tendencia,
                estructura[:20] if estructura else "—"
            )
        # BOS y ChoCH
        ms_1h_ui = market_structures.get("1h", {})
        bos_list  = ms_1h_ui.get("bos", [])
        choch_list = ms_1h_ui.get("choch", [])
        if bos_list:
            for b in bos_list:
                st.success(f"✅ BOS: {b}")
        if choch_list:
            for c_item in choch_list:
                st.warning(f"⚠️ ChoCH: {c_item}")
        # Fuerza de tendencia 1h
        if trend_strength_1h:
            ts = trend_strength_1h
            adx_color = "🟢" if ts["fuerza"] == "FUERTE" else ("🟡" if ts["fuerza"] == "MODERADA" else "🔴")
            st.info(
                f"**ADX 1h:** {ts['adx']} ({adx_color} {ts['fuerza']})  |  "
                f"+DI: {ts['plus_di']}  |  -DI: {ts['minus_di']}  |  "
                f"Tendencia: **{ts['tendencia']}**"
            )
    else:
        st.info("Sin datos de estructura disponibles.")

    # ── Detección de Manipulación ─────────────────────────────────────────────
    st.markdown("---")
    st.subheader("🕵️ Detección de Manipulación y Liquidez Institucional")
    man_col1, man_col2 = st.columns(2)
    with man_col1:
        st.write("**Stop Hunts detectados (1h):**")
        if stop_hunts:
            for sh in stop_hunts:
                color_fn = st.error if sh["señal"] == "SHORT" else st.success
                color_fn(
                    f"{sh['emoji']} **{sh['tipo']}** — {sh['fuerza']}\n\n"
                    f"{sh['descripcion']}\n\n"
                    f"Nivel barrido: `{sh['nivel']:.5f}`"
                )
        else:
            st.info("⚪ Sin stop hunts recientes detectados")
    with man_col2:
        st.write("**Absorción Institucional (1h):**")
        if vol_absorption:
            fn = st.success if vol_absorption["sesgo"] == "LONG" else st.error
            fn(
                f"**{vol_absorption['tipo']}** — {vol_absorption['fuerza']}\n\n"
                f"{vol_absorption['descripcion']}\n\n"
                f"Vol. ratio: **{vol_absorption['vol_ratio']}x** · "
                f"Body ratio: {vol_absorption['body_ratio']:.0%}"
            )
        else:
            st.info("⚪ Sin absorción institucional detectada")

    # ── COT — Datos Institucionales ───────────────────────────────────────────
    st.markdown("---")
    st.subheader("🏦 Datos Institucionales — COT Report (CFTC) + Grandes Inversores")
    cot_col1, cot_col2 = st.columns([2, 1])
    with cot_col1:
        if st.button("🔄 Actualizar COT Data", key="refresh_cot"):
            with st.spinner("Descargando datos CFTC..."):
                st.session_state.cot_data = get_cot_data()
        elif st.session_state.cot_data is None:
            with st.spinner("Cargando COT..."):
                st.session_state.cot_data = get_cot_data()
        cot = st.session_state.cot_data
        if cot:
            bias_color = "🟢" if cot["bias_direction"] == "LONG" else "🔴"
            st.markdown(
                f"**Informe COT** — fecha: `{cot['date']}`  \n"
                f"**Bias especuladores:** {bias_color} {cot['bias']}  \n"
                f"**Net non-commercial:** `{cot['net']:+,}` contratos  \n"
                f"**Cambio semana:** `{cot['change']:+,}` — {cot['change_lbl']}"
            )
            cot_dir, cot_pts = interpret_cot_for_signal(cot)
            if cot_dir == "LONG":
                st.success(f"✅ COT señala LONG EUR ({cot_pts}pts de confluencia)")
            elif cot_dir == "SHORT":
                st.error(f"🔴 COT señala SHORT EUR ({cot_pts}pts de confluencia)")
            else:
                st.info("⚪ COT neutral — posiciones equilibradas")
        else:
            st.info("Sin datos COT disponibles (timeout o sin conexión CFTC)")
    with cot_col2:
        st.write("**¿Qué es el COT?**")
        st.caption(
            "Muestra las posiciones de grandes especuladores "
            "(fondos de inversión, hedge funds) en EUR futures. "
            "Si aumentan longs → institucionales apuestan al alza del EUR."
        )
        st.write("**Fuente:** CFTC (semanal, viernes)")
        st.write("**Refresco:** 12h de caché")

    # ── IA — Motor de Bias ────────────────────────────────────────────────────
    st.markdown("---")
    st.subheader("🤖 Motor de IA — Análisis de Confluencia Inteligente")
    if ai_bias:
        bias_dir  = ai_bias.get("bias", "NEUTRAL")
        conf      = ai_bias.get("confidence", 50)
        long_sc   = ai_bias.get("long_score", 0)
        short_sc  = ai_bias.get("short_score", 0)
        evidence  = ai_bias.get("evidence", [])
        bias_color = "#0f5132" if bias_dir == "LONG" else ("#842029" if bias_dir == "SHORT" else "#333")
        bias_emoji = "📈" if bias_dir == "LONG" else ("📉" if bias_dir == "SHORT" else "⚪")
        st.markdown(
            f'<div class="score-box" style="background:{bias_color};color:white;font-size:1.4rem">'
            f'{bias_emoji} BIAS IA: {bias_dir} — Confianza {conf}%'
            f'<br><small>LONG score: {long_sc} | SHORT score: {short_sc}</small></div>',
            unsafe_allow_html=True
        )
        ia_c1, ia_c2 = st.columns(2)
        with ia_c1:
            st.write("**Evidencia del motor IA:**")
            for ev in evidence:
                st.write(f"• {ev}")
        with ia_c2:
            st.write("**Patrones de velas (15m):**")
            if ai_patterns:
                for pat in ai_patterns:
                    emoji_p = pat.get("emoji", "")
                    fn_p = st.success if pat["sesgo"] == "LONG" else (st.error if pat["sesgo"] == "SHORT" else st.info)
                    fn_p(f"{emoji_p} **{pat['patron']}** (peso: {pat['peso']:+d})")
            else:
                st.info("⚪ Sin patrones significativos en 15m")
            st.caption(f"Score de patrones: **{patterns_score:+d}**")
    else:
        st.info("Ejecuta el análisis para ver el bias de IA.")

else:
    st.info("📊 Presiona 'ANALIZAR MERCADO' para ver el análisis completo")

# ── BACKTEST + COMPARACIÓN DE ESTRATEGIAS + CONTEXTO DE MERCADO ───────────────
st.markdown("---")
st.subheader("🧠 Backtest Inteligente — 4 Estrategias + Contexto Fundamental")
st.caption(
    "Compara 4 estrategias sobre el mismo año de datos. La ganadora se guarda en la base de conocimiento. "
    "El panel de contexto explica POR QUÉ el mercado está donde está (técnico + fundamental + institucional)."
)

bt_ctrl_l, bt_ctrl_r = st.columns([1, 2])
with bt_ctrl_l:
    bt_use_windows = st.checkbox("Solo ventanas 7-12h / 15-20h", value=True, key="bt_windows")
    run_bt_btn = st.button("🚀 Comparar 17 Estrategias (~1 año)", type="primary", key="run_bt")
    if run_bt_btn:
        with st.spinner("Descargando hasta 1 año de datos EURUSD 1h..."):
            bt_df = get_backtest_data("1h")
        if bt_df.empty:
            st.error("Sin datos históricos — verifica conexión a internet.")
        else:
            n_c = len(bt_df)
            with st.spinner(f"Comparando 17 estrategias sobre {n_c} velas ({n_c//24}d) — puede tardar 30-90s..."):
                cmp = run_strategy_comparison(bt_df, use_windows=bt_use_windows)
            if not cmp:
                st.warning("Sin operaciones — pocos datos o mercado lateral extremo.")
            else:
                st.session_state.strategy_comparison = cmp
                st.session_state.backtest_result = cmp["best"]
                # Contexto de mercado sobre los mismos datos del backtest
                cot_snap  = st.session_state.get("cot_data")
                cal_snap  = st.session_state.get("economic_calendar") or get_economic_calendar()
                news_snap = st.session_state.get("current_news") or []
                ctx_snap  = explain_market_context(bt_df, cot=cot_snap, calendar=cal_snap, news=news_snap)
                st.session_state.market_context_reasons = ctx_snap
                # Guardar en base de conocimiento (incluye contexto fundamental)
                kb = update_kb(cmp, cot=cot_snap, calendar=cal_snap, market_ctx=ctx_snap)
                st.success(
                    f"✅ Comparación completada. Mejor estrategia: **{cmp['best']['label']}** "
                    f"(PF={cmp['best']['profit_factor']} · WR={cmp['best']['winrate']}%)"
                )

with bt_ctrl_r:
    # Cargar calendario económico
    if st.button("📅 Cargar Calendario Económico", key="load_cal"):
        with st.spinner("Obteniendo eventos EUR/USD esta semana..."):
            cal = get_economic_calendar()
        st.session_state.economic_calendar = cal
        if cal:
            st.success(f"✅ {len(cal)} eventos cargados "
                       f"({sum(1 for e in cal if e.get('impact','').upper()=='HIGH')} de alto impacto)")
        else:
            st.info("Sin eventos esta semana o API no disponible.")
    cal_data = st.session_state.get("economic_calendar") or []
    if cal_data:
        high_ev = [e for e in cal_data if e.get("impact","").upper() == "HIGH"]
        med_ev  = [e for e in cal_data if e.get("impact","").upper() == "MEDIUM"]
        st.markdown(f"**Esta semana:** {len(high_ev)} eventos ALTO impacto · {len(med_ev)} MEDIO impacto")
        for ev in high_ev[:5]:
            st.markdown(
                f"🔴 **[{ev.get('currency','')}]** {ev.get('title','')} "
                f"— {str(ev.get('date',''))[:10]} "
                f"| Prev: {ev.get('previous','?')} | Fore: {ev.get('forecast','?')}"
            )

# ── Comparación de estrategias ─────────────────────────────────────────────
cmp_result = st.session_state.get("strategy_comparison")
if cmp_result:
    st.markdown("### 📊 Ranking de Estrategias")
    best_name = cmp_result["best"]["strategy"]

    # Tabla comparativa
    rows = []
    for i, r in enumerate(cmp_result["results"]):
        rentable = r.get("profit_factor", 0) >= 1.0 and r.get("winrate", 0) >= r.get("be_winrate", 0)
        be_n = r.get("be_count", 0)
        rows.append({
            "Pos":          _RANK_EMOJI[i] if i < len(_RANK_EMOJI) else f"#{i+1}",
            "Estrategia":   r.get("label", r.get("strategy", "?")),
            "Operaciones":  r.get("total", 0),
            "Win Rate":     f"{r.get('winrate', 0)}%",
            "BE (scratch)": be_n,
            "WR sin BE":    f"{r.get('be_winrate', 0)}%",
            "Profit Factor":f"{r.get('profit_factor', 0)}x",
            "Net Pips":     f"{r.get('net_pips', 0):+.1f}",
            "Max DD":       f"{r.get('max_dd', 0)}%",
            "P&L $":        f"${r.get('net_pnl', 0):+.2f}",
            "Estado":       "✅ Rentable" if rentable else "⚠️ Marginal",
        })
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    # Ganadora detallada
    best = cmp_result["best"]
    st.markdown(f"### 🏆 Estrategia Ganadora: {best['label']}")
    st.info(f"**Por qué funciona:** {best['why']}\n\n✅ **Ventajas:** {best['pros']}\n\n⚠️ **Limitaciones:** {best['cons']}")

    mc1, mc2, mc3, mc4, mc5 = st.columns(5)
    mc1.metric("Operaciones", best["total"])
    mc2.metric("Win Rate", f"{best['winrate']}%", delta=f"BE={best['be_winrate']}%")
    mc3.metric("Profit Factor", f"{best['profit_factor']}x")
    mc4.metric("Net Pips", f"{best['net_pips']:+.1f}p")
    mc5.metric("Max Drawdown", f"{best['max_dd']}%")

    # Curva de capital de la ganadora
    eq_list = best.get("equity", [])
    if len(eq_list) > 2:
        st.write("**Curva de capital — estrategia ganadora ($10,000 capital inicial · 0.01 lot):**")
        st.line_chart(pd.DataFrame({"Capital ($)": eq_list}), height=220)

    # Tabs: curvas de todas + tabla de trades
    with st.expander("Ver curvas de capital de todas las estrategias", expanded=False):
        max_len = max(len(r["equity"]) for r in cmp_result["results"])
        eq_all = {}
        for r in cmp_result["results"]:
            eq_padded = r["equity"] + [r["equity"][-1]] * (max_len - len(r["equity"]))
            eq_all[r["label"][:20]] = eq_padded
        st.line_chart(pd.DataFrame(eq_all), height=240)

    with st.expander(f"Ver operaciones de '{best['label']}' ({len(best.get('trades',[]))} trades)", expanded=False):
        raw = best.get("trades", [])
        if raw:
            td = pd.DataFrame(raw)
            if "outcome" in td.columns:
                td["Resultado"] = td["outcome"].map({"TP":"✅ TP","SL":"❌ SL","BE":"🔄 BE 0p","MAX":"⏱ MAX","OPEN":"🔄 Abierta"})
            cols_s = [c for c in ["time","dir","Resultado","pips","pnl"] if c in td.columns]
            st.dataframe(
                td[cols_s].rename(columns={"time":"Entrada","dir":"Dirección","pips":"Pips","pnl":"P&L $"}),
                use_container_width=True, hide_index=True
            )

    # Base de conocimiento histórica
    kb = load_knowledge_base()
    if kb.get("runs"):
        _kb_total_runs = len(kb["runs"])
        _kb_sig_stats  = kb.get("signal_stats", {})
        with st.expander(f"📚 Base de conocimiento ({_kb_total_runs} backtests · señales evaluadas)", expanded=False):
            _col_kb1, _col_kb2 = st.columns(2)
            with _col_kb1:
                if kb.get("strategy_wins"):
                    st.markdown("**Ranking histórico de estrategias:**")
                    for s, cnt in sorted(kb["strategy_wins"].items(), key=lambda x: -x[1]):
                        meta = _STRATEGY_META.get(s, {})
                        st.markdown(f"- **{meta.get('label', s)}**: nº1 en {cnt} backtest(s)")
            with _col_kb2:
                if _kb_sig_stats:
                    st.markdown("**Aciertos de señal KB por estrategia:**")
                    for s, ss in sorted(_kb_sig_stats.items(), key=lambda x: -(x[1].get("correct",0))):
                        ok = ss.get("correct", 0); ko = ss.get("wrong", 0)
                        acc = f"{ok/(ok+ko)*100:.0f}%" if (ok+ko) > 0 else "—"
                        meta = _STRATEGY_META.get(s, {})
                        st.markdown(f"- **{meta.get('label',s)}**: {ok}✅ {ko}❌ → {acc}")
            hist_rows = []
            for run in reversed(kb["runs"][-15:]):
                hist_rows.append({
                    "Fecha":    run.get("ts","?")[:10],
                    "Ganadora": _STRATEGY_META.get(run.get("best",""), {}).get("label", run.get("best","?")),
                    "PF":       run.get("pf","?"),
                    "WR":       f"{run.get('wr','?')}%",
                    "Ops":      run.get("total","?"),
                    "NetPips":  run.get("net_pips","?"),
                    "COT":      run.get("cot_bias") or "—",
                    "Eventos":  run.get("events_high",0),
                })
            st.dataframe(pd.DataFrame(hist_rows), use_container_width=True, hide_index=True)
            # Mostrar el "por qué" del último backtest
            _last_run = kb["runs"][-1]
            if _last_run.get("market_ctx"):
                st.markdown("**Contexto fundamental del último backtest:**")
                for _rc in _last_run["market_ctx"]:
                    st.markdown(f"  - {_rc}")
else:
    st.info("Pulsa **'Comparar 17 Estrategias'** para encontrar la mejor estrategia en el año actual. La señal inteligente aparecerá automáticamente al analizar el mercado.")

# ════════════════════════════════════════════════════════════════════════════════
# BACKTEST HISTÓRICO LARGO PLAZO — DESDE 2008 (DATOS DIARIOS)
# ════════════════════════════════════════════════════════════════════════════════
st.markdown("---")
st.subheader("🌍 Backtest Histórico — Desde 2008 hasta Hoy (Datos Diarios)")
st.caption(
    "Descarga datos diarios EUR/USD desde 2008 (~4,000 velas) y ejecuta las 17 estrategias. "
    "Los umbrales ATR se escalan automáticamente para barras diarias. "
    "Resultado: cuál estrategia habría sido más rentable en 16+ años de mercado real."
)

if "lt_comparison" not in st.session_state:
    st.session_state.lt_comparison = None

_lt_cols = st.columns([1, 2])
with _lt_cols[0]:
    _run_lt = st.button("🚀 Backtest 2008–Hoy (17 estrategias · datos diarios)",
                        type="primary", key="run_lt")
    if _run_lt:
        with st.spinner("Descargando datos diarios EUR/USD desde 2008..."):
            _lt_df = get_longterm_data_2008()
        if _lt_df.empty:
            st.error("No se pudieron descargar datos históricos. Verifica conexión a internet.")
        else:
            _lt_n = len(_lt_df)
            _lt_years = round(_lt_n / 252)
            with st.spinner(f"Ejecutando 17 estrategias sobre {_lt_n} días (~{_lt_years} años) — paralelo, ~15-30s..."):
                _lt_cmp = run_longterm_comparison(_lt_df)
            if not _lt_cmp:
                st.warning("Sin operaciones válidas en datos históricos.")
            else:
                st.session_state.lt_comparison = _lt_cmp
                st.session_state.lt_n_bars = _lt_n
                st.success(
                    f"✅ Completado — {_lt_n} barras diarias · Mejor estrategia: "
                    f"**{_lt_cmp['best']['label']}** "
                    f"(PF={_lt_cmp['best']['profit_factor']} · "
                    f"WR={_lt_cmp['best']['winrate']}% · "
                    f"{_lt_cmp['best']['net_pips']:+.0f} pips)"
                )

with _lt_cols[1]:
    st.markdown(
        "**Diferencias vs backtest de 1 año:**\n"
        "- Datos diarios — cada barra = 1 día de trading\n"
        "- ATR umbral escalado a ≥40 pips (vs 4p en 1h)\n"
        "- Cooldown de 3 días entre entradas\n"
        "- Sin filtro de horario (ventana London/NY)\n"
        "- 16+ años incluyen: crisis 2008, COVID 2020, subidas Fed 2022-23"
    )

_lt_cmp_result = st.session_state.get("lt_comparison")
if _lt_cmp_result:
    _lt_n_bars = st.session_state.get("lt_n_bars", 0)
    _lt_years  = round(_lt_n_bars / 252) if _lt_n_bars else "?"
    st.markdown(f"### 📊 Ranking Histórico 2008–Hoy ({_lt_n_bars} barras · ~{_lt_years} años)")

    _lt_rows = []
    for _i, _r in enumerate(_lt_cmp_result["results"]):
        _rentable = _r.get("profit_factor", 0) >= 1.0
        _be_n = _r.get("be_count", 0)
        _lt_rows.append({
            "Pos":           _RANK_EMOJI[_i] if _i < len(_RANK_EMOJI) else f"#{_i+1}",
            "Estrategia":    _r.get("label", _r.get("strategy", "?")),
            "Operaciones":   _r.get("total", 0),
            "Win Rate":      f"{_r.get('winrate', 0)}%",
            "BE (scratch)":  _be_n,
            "Profit Factor": f"{_r.get('profit_factor', 0)}x",
            "Net Pips":      f"{_r.get('net_pips', 0):+.0f}",
            "Max DD":        f"{_r.get('max_dd', 0)}%",
            "P&L $":         f"${_r.get('net_pnl', 0):+.2f}",
            "Estado":        "✅ Rentable" if _rentable else "⚠️ Marginal",
        })
    st.dataframe(pd.DataFrame(_lt_rows), use_container_width=True, hide_index=True)

    _lt_best = _lt_cmp_result["best"]
    st.markdown(f"### 🏆 Mejor Estrategia Histórica (2008–Hoy): {_lt_best['label']}")
    _ltc1, _ltc2, _ltc3, _ltc4, _ltc5 = st.columns(5)
    _ltc1.metric("Operaciones", _lt_best.get("total", 0))
    _ltc2.metric("Win Rate", f"{_lt_best.get('winrate', 0)}%")
    _ltc3.metric("Profit Factor", f"{_lt_best.get('profit_factor', 0)}x")
    _ltc4.metric("Net Pips", f"{_lt_best.get('net_pips', 0):+.0f}p")
    _ltc5.metric("Max Drawdown", f"{_lt_best.get('max_dd', 0)}%")

    st.info(
        f"**Por qué funciona a largo plazo:** {_lt_best.get('why', '')}\n\n"
        f"✅ **Ventajas:** {_lt_best.get('pros', '')}\n\n"
        f"⚠️ **Limitaciones:** {_lt_best.get('cons', '')}"
    )

    # Curva de capital de la ganadora histórica
    _lt_eq = _lt_best.get("equity", [])
    if len(_lt_eq) > 2:
        st.write(f"**Curva de capital 2008–Hoy — {_lt_best['label']} ($10,000 inicial · 0.01 lot):**")
        st.line_chart(pd.DataFrame({"Capital ($)": _lt_eq}), height=260)

    with st.expander("Ver curvas de todas las estrategias — largo plazo", expanded=False):
        _lt_max_len = max(len(_r["equity"]) for _r in _lt_cmp_result["results"])
        _lt_eq_all  = {}
        for _r in _lt_cmp_result["results"]:
            _eq_pad = _r["equity"] + [_r["equity"][-1]] * (_lt_max_len - len(_r["equity"]))
            _lt_eq_all[_r["label"][:20]] = _eq_pad
        st.line_chart(pd.DataFrame(_lt_eq_all), height=260)

    with st.expander(
        f"Ver operaciones de '{_lt_best['label']}' ({len(_lt_best.get('trades', []))} trades)",
        expanded=False
    ):
        _lt_raw = _lt_best.get("trades", [])
        if _lt_raw:
            _lt_td = pd.DataFrame(_lt_raw)
            if "outcome" in _lt_td.columns:
                _lt_td["Resultado"] = _lt_td["outcome"].map(
                    {"TP": "✅ TP", "SL": "❌ SL", "BE": "🔄 BE 0p", "OPEN": "🔄 Abierta"}
                )
            _lt_cols_s = [c for c in ["time", "dir", "Resultado", "pips", "pnl"] if c in _lt_td.columns]
            st.dataframe(
                _lt_td[_lt_cols_s].rename(
                    columns={"time": "Fecha", "dir": "Dirección", "pips": "Pips (día)", "pnl": "P&L $"}
                ),
                use_container_width=True, hide_index=True,
            )

# ── Panel: Por qué se mueve el mercado ────────────────────────────────────────
st.markdown("---")
st.subheader("🔍 Por qué se mueve el EUR/USD — Análisis Técnico + Fundamental")
ctx_reasons = st.session_state.get("market_context_reasons")
if ctx_reasons:
    for reason in ctx_reasons:
        st.markdown(f"- {reason}")
    st.caption("Fuentes: EMA técnico · RSI/MACD momentum · COT CFTC institucional · Calendario ForexFactory · RSS noticias")
else:
    st.info(
        "Ejecuta primero el backtest (botón arriba) y asegúrate de tener datos COT y calendario cargados. "
        "Este panel explicará en detalle POR QUÉ el mercado está donde está."
    )

# ── BOT AUTOMÁTICO (SIEMPRE VISIBLE) ───────────────────────────────────────────
st.markdown("---")
st.subheader("🤖 Modo Bot Automático")

# Información de diagnóstico MT5
if sys.platform != "win32":
    st.info(
        "ℹ️ **Bot automático desactivado en servidor cloud.**  "
        "MT5 requiere Windows. Las señales, alertas Telegram y el backtest "
        "funcionan con normalidad — solo la ejecución de órdenes requiere la "
        "versión local en PC con Windows."
    )
else:
    st.write("**🔍 Diagnóstico MT5:**")
    col_diag1, col_diag2, col_diag3 = st.columns(3)
    with col_diag1:
        if is_mt5_available():
            st.success("✅ MT5 instalado")
        else:
            st.error("❌ MT5 no instalado")
    with col_diag2:
        if is_mt5_available():
            connected_diag = mt5_connect()
            if connected_diag:
                st.success("✅ MT5 conectado")
            else:
                st.error("❌ MT5 no conectado")
                err = get_mt5_error()
                if err:
                    st.warning(f"🛠️ Error: {err}")
                st.info("💡 Abre MetaTrader 5 y verifica que esté funcionando")
        else:
            st.error("❌ No disponible")
    with col_diag3:
        if is_mt5_available() and mt5_connect():
            terminal_info = mt5.terminal_info()
            if terminal_info:
                st.success(f"✅ Terminal: {terminal_info.name}")
            else:
                st.warning("⚠️ Terminal info no disponible")
        else:
            st.error("❌ No disponible")

# Inicializar estado del bot en session_state
if "bot_enabled" not in st.session_state:
    st.session_state.bot_enabled = False
if "bot_volume" not in st.session_state:
    st.session_state.bot_volume = 0.01
if "bot_last_signal" not in st.session_state:
    st.session_state.bot_last_signal = None
if "bot_just_activated" not in st.session_state:
    st.session_state.bot_just_activated = False

# Sincronizar variables globales con session_state
BOT_ENABLED = st.session_state.bot_enabled
BOT_VOLUME = st.session_state.bot_volume
BOT_LAST_SIGNAL = st.session_state.bot_last_signal

# Verificar conexión MT5
if is_mt5_available() and mt5_connect():
    st.success("✅ MT5 conectado - Bot disponible")

    # Estado actual del bot
    if st.session_state.bot_enabled:
        st.success("🚀 **BOT ACTIVO** - Ejecutando señales automáticamente")
    else:
        st.info("⏸️ **BOT INACTIVO** - Modo manual")

    # Controles del bot
    col_bot1, col_bot2, col_bot3 = st.columns([2, 1, 1])

    with col_bot1:
        if st.session_state.bot_enabled:
            if st.button("⏸️ **DESACTIVAR BOT**", type="secondary"):
                st.session_state.bot_enabled = False
                st.session_state.bot_just_activated = False
        else:
            if st.button("🚀 **ACTIVAR BOT**", type="primary"):
                st.session_state.bot_enabled = True
                st.session_state.bot_just_activated = True

    with col_bot2:
        st.session_state.bot_volume = st.number_input(
            "Volumen", 0.01, 1.0, st.session_state.bot_volume, 0.01,
            help="Lote estándar (0.01 = 1000 unidades)"
        )

    with col_bot3:
        # Estado del bot
        positions_mt5 = get_mt5_positions()
        if positions_mt5:
            st.warning(f"📊 {len(positions_mt5)} pos.")
            if st.button("🔒 Cerrar Todo", type="secondary"):
                success, msg = auto_close_positions()
                if success:
                    st.success(f"✅ {msg}")
                else:
                    st.error(f"❌ {msg}")
        else:
            st.info("🎯 Sin pos.")

    # Información de riesgo cuando bot está activo
    if st.session_state.bot_enabled:
        st.warning("⚠️ **BOT ACTIVO** - Ejecutará operaciones automáticamente")
        st.info(f"💰 Volumen: {st.session_state.bot_volume} lotes | SL: {SCALP_SL_PIPS}p máximo")

        # Lógica del bot automático
        if st.session_state.analysis_executed:
            try:
                if not st.session_state.bot_just_activated and score >= MIN_DEFINITIVE_SCORE and signal.get("direction"):
                    if st.session_state.bot_last_signal != signal.get("direction"):
                        success, msg = auto_trade_signal(signal, st.session_state.bot_volume, liq_levels=liq_levels)
                        if success:
                            st.success(f"🚀 Bot ejecutó: {msg}")
                            st.session_state.bot_last_signal = signal.get("direction")
                        else:
                            st.error(f"❌ Error del bot: {msg}")
                    else:
                        st.info("🔄 Señal ya ejecutada por el bot")
                elif st.session_state.bot_just_activated:
                    st.info("🤖 Bot activado - Esperando nueva señal...")
                    st.session_state.bot_just_activated = False
                else:
                    st.session_state.bot_last_signal = None
            except NameError:
                st.info("🔄 Presiona 'ANALIZAR MERCADO' para que el bot comience")
        else:
            st.info("🔄 Presiona 'ANALIZAR MERCADO' primero para activar el bot")

else:
    st.error("❌ MT5 no conectado - Bot no disponible")
    st.info("💡 **Para conectar MT5:**")
    st.markdown("""
    1. **Abre MetaTrader 5** (como administrador si es necesario)
    2. **Inicia sesión** en tu cuenta demo/real
    3. **Verifica** que puedas ver gráficos de EURUSD
    4. **No cierres** MT5 mientras uses el bot
    5. **Asegúrate** de que MT5 esté respondiendo (no congelado)
    6. **Si usas VPN** - puede interferir con la conexión
    """)
    st.warning("🔄 **Después de conectar MT5, refresca esta página** (F5)")

# Mostrar noticias si están disponibles
news = signal.get("news", []) if 'signal' in dir() and signal else []
if news:
    for idx, a in enumerate(news[:20]):
        title = a.get("title", "") or "Sin título"
        src   = a.get("source", {}).get("name", "")
        url   = a.get("url", ""); pub = (a.get("publishedAt") or "")[:10]
        imp   = a.get("impact_score", 0); lbl = a.get("impact_label", "⚪ BAJO")
        tb_cls = get_textblob()
        if tb_cls and tb_cls is not False:
            try:
                pol = tb_cls(title).sentiment.polarity
            except Exception:
                pol = 0.0
        else:
            pol = 0.0
        s_emoji = "🟢" if pol > 0.1 else ("🔴" if pol < -0.1 else "⚪")
        with st.expander(
            f"[{idx}] {lbl} {s_emoji} — {title[:80]}... | {imp:.0f}%",
            expanded=False
        ):
            n1, n2 = st.columns([3, 1])
            with n1:
                st.write(f"**{src}** | {pub}")
                if url: st.write(f"[Leer →]({url})")
            with n2:
                st.metric("Impacto", f"{imp:.0f}%")
            desc = a.get("description", "")
            if desc: st.write(f"*{desc[:180]}...*")
else:
    st.info("Sin noticias disponibles.")

# ── Dashboard final ───────────────────────────────────────────────────────
st.markdown("---")
st.subheader("📋 Dashboard Final — Resumen de Decisión")
tech_sig = ("COMPRA" if signal.get("buy_signals", 0) > signal.get("sell_signals", 0)
            else "VENTA" if signal.get("sell_signals", 0) > 0 else "NEUTRAL")
fund_raw = consensus.get("consensus", "Neutral")
fund_sig = ("COMPRA" if "Bullish" in fund_raw
            else "VENTA" if "Bearish" in fund_raw else "NEUTRAL")
ws       = consensus.get("weighted_sentiment", 0)
news_sig = "COMPRA" if ws > 0.1 else ("VENTA" if ws < -0.1 else "NEUTRAL")
delta_sig = "NEUTRAL"
if delta:
    if delta["delta"] > 0:   delta_sig = "🟢 COMPRADORES"
    elif delta["delta"] < 0: delta_sig = "🔴 VENDEDORES"

def sig_icon(s):
    return "🟢" if s == "COMPRA" else ("🔴" if s == "VENTA" else "⚪")

bt_result = "N/A"
if direction and not df_1h.empty:
    bt = run_backtest(df_1h, direction, SCALP_SL_PIPS, SCALP_TP_PIPS)
    if bt:
        bt_result = (f"{'✅' if bt['net_pips']>0 else '❌'} "
                     f"{bt['winrate']}% WR | {bt['net_pips']}p netos")

spread_result = (f"{tick['spread_pips']}p {'✅' if tick['spread_pips']<1.5 else '⚠️'}"
                 if tick else "N/A (sin MT5)")
poc_result    = f"`{poc['precio']:.5f}`" if poc else "N/A"

# ── Niveles de DXY ────────────────────────────────────────────────────────
st.markdown("---")
st.subheader("💱 Niveles de DXY (Índice del Dólar)")
dxy_levels = {
    "Precio Actual": dxy_price,
    "Tendencia": dxy_trend or "N/A",
    "Cambio": f"{dxy_chg:+.2f}%"
}
if dxy_price is not None:
    col1, col2 = st.columns([2, 1])
    with col1:
        st.metric("📊 Precio DXY", f"{dxy_levels['Precio Actual']:.2f}")
    with col2:
        st.metric("📈 Cambio", dxy_levels['Cambio'])
    st.write(f"**Tendencia:** {dxy_levels['Tendencia']}")
else:
    st.info("📡 Actualizando niveles de DXY...")

matrix = [
    {"Análisis": "Técnico (TF + Indicadores)",
     "Señal": f"{sig_icon(tech_sig)} {tech_sig}",
     "Fuerza": f"{signal.get('buy_signals',0)+signal.get('sell_signals',0)} señales"},
    {"Análisis": "Fundamental (Noticias)",
     "Señal": f"{sig_icon(fund_sig)} {fund_sig}",
     "Fuerza": f"{avg_impact:.0f}% impacto"},
    {"Análisis": "Sentimiento de mercado",
     "Señal": f"{sig_icon(news_sig)} {news_sig}",
     "Fuerza": f"{total_sources} fuentes"},
    {"Análisis": "DXY (Dólar)",
     "Señal": f"{'🔴' if dxy_dir=='UP' else '🟢' if dxy_dir=='DOWN' else '⚪'} {dxy_dir}",
     "Fuerza": f"{dxy_chg:+.2f}%"},
    {"Análisis": "Spike de volumen",
     "Señal": "⚡ Spike detectado" if vol_spikes else "⚪ Normal",
     "Fuerza": f"{vol_spikes[0]['ratio']}x" if vol_spikes else "—"},
    {"Análisis": "Delta de volumen",
     "Señal": delta_sig,
     "Fuerza": f"{delta['delta_pct']:+.1f}%" if delta else "—"},
    {"Análisis": "POC (Volume Profile)",
     "Señal": poc_result,
     "Fuerza": f"{poc['volumen']:,} ticks" if poc else "—"},
    {"Análisis": "Score de confluencia",
     "Señal": label,
     "Fuerza": f"{score}/100"},
    {"Análisis": "Spread MT5",
     "Señal": spread_result,
     "Fuerza": "Tiempo real" if tick else "—"},
    {"Análisis": "Backtesting 1h",
     "Señal": bt_result,
     "Fuerza": "Histórico"},
]
st.dataframe(pd.DataFrame(matrix), use_container_width=True, hide_index=True)

st.markdown("---")
st.subheader("🎯 Resumen de Acción Recomendada")
recomendacion = {
    "Dirección": signal.get('final_signal', 'NEUTRAL'),
    "Confianza": f"{score}/100 — {label}",
    "Precio Entrada": f"{price:.5f}" if price else "N/A",
    "Stop Loss": f"{signal.get('stop_loss', 'N/A')}",
    "Take Profit": f"{signal.get('take_profit', 'N/A')}",
    "Sesión Activa": session,
    "Contexto DXY": f"{dxy_trend or 'N/A'} ({dxy_chg:+.2f}%)",
    "Acción": "✅ OPERABLE" if score >= MIN_DEFINITIVE_SCORE else "⏸️ ESPERAR"
}
for key, val in recomendacion.items():
    st.write(f"**{key}:** {val}")

final_signal = signal.get('final_signal', 'NEUTRAL')
st.info(
    f"**Señal:** {final_signal}  \n"
    f"**Score:** {score}/100 — {label}  \n"
    f"**Precio:** {f'{price:.5f}' if price else 'N/A'}  \n"
    f"**Delta volumen:** {delta_sig}  \n"
    f"**Sesión:** {session}  |  **DXY:** {dxy_trend or 'N/A'} ({dxy_chg:+.2f}%)"
)

st.markdown("---")
st.caption("⚠️ Solo informativo. No es consejo financiero. Usa siempre SL.")

# ── Auto-rerun: timer limpio sin time.sleep() ─────────────────────────────
# SOLUCIÓN DEFINITIVA:
#   time.sleep() bloquea el WebSocket de Streamlit → congelación / desconexión.
#   @st.fragment(run_every="1s") usa un timer JavaScript puro:
#     • re-renderiza SOLO este widget cada segundo
#     • NO bloquea el hilo de Python
#     • NO provoca parpadeo en el resto de la página
#     • el contador baja exactamente 1s en cada tick
#   Cuando llega a 0 dispara st.rerun() completo → el guard de análisis
#   (elapsed >= refresh_secs * 0.95) lanza generate_signal() solo en ese momento.
if refresh_secs > 0:
    st.session_state["_refresh_secs_live"] = refresh_secs

    @st.fragment(run_every="1s")
    def _auto_refresh_fragment():
        _rs   = st.session_state.get("_refresh_secs_live", 0)
        _last = st.session_state.get("last_analysis_time")
        _now  = time.time()
        # Si no hay timestamp aún (primera carga sin análisis previo) forzar rerun
        if not _last or _rs <= 0:
            st.rerun()
            return
        _rem = max(0.0, _rs - (_now - _last))
        _m, _s = divmod(int(_rem), 60)
        st.markdown(f"🔄 Próxima actualización en **{_m:02d}:{_s:02d}**")
        if _rem <= 0:
            st.rerun()   # full rerun → activa should_auto_refresh → análisis

    _auto_refresh_fragment()
else:
    # Auto-refresh desactivado: limpiar el valor para que el fragment pare si quedó activo
    st.session_state["_refresh_secs_live"] = 0
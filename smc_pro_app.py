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
    MIN_RATIO = 2.8 + np.random.random() * 0.4  # Entre 2.8 y 3.2
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
        w = {"1d": 30, "4h": 25, "1h": 20, "15m": 15}.get(tf, 10)
        if ms["tendencia"] == "ALCISTA":
            scores["LONG"]  += w; evidence.append(f"📈 Estructura {tf}: {ms['estructura']} (+{w})")
        elif ms["tendencia"] == "BAJISTA":
            scores["SHORT"] += w; evidence.append(f"📉 Estructura {tf}: {ms['estructura']} (+{w})")
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

# Auto-refresh solo si: 1) está habilitado, 2) pasó suficiente tiempo, 3) NO presionó el botón del bot
should_auto_refresh = False
if refresh_secs > 0:
    current_time = time.time()
    if st.session_state.last_analysis_time is None:
        should_auto_refresh = True
    else:
        elapsed = current_time - st.session_state.last_analysis_time
        if elapsed >= refresh_secs:
            should_auto_refresh = True

run_fresh_analysis = run_analysis or should_auto_refresh
if run_fresh_analysis:
    st.session_state.last_analysis_time = time.time()
    st.session_state.analysis_executed = True
# No se resetea analysis_executed a False: los resultados persisten entre reruns del temporizador

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
    final = signal["final_signal"]
    css   = "sl" if "COMPRA" in final else ("ss" if "VENTA" in final else "sw")
    st.markdown(f'<div class="big-signal {css}">{final}</div>', unsafe_allow_html=True)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("✅ Señales COMPRA", signal["buy_signals"])
    c2.metric("❌ Señales VENTA",  signal["sell_signals"])
    c3.metric(f"{signal['sess_icon']} Sesión", signal["session"].split(" ")[0])
    c4.metric("⚡ Volatilidad",    signal["volatility"])

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
            st.caption(f"SL estructural · ATR base: {atr_val:.1f} pips" if atr_val else "")
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

# ── BACKTEST COMPLETO ──────────────────────────────────────────────────────────
st.markdown("---")
st.subheader("📊 Backtest — Estrategia EMA Trend + MACD + RSI | R:R 1:3")
st.caption("Estrategia: alineación EMA9>21>50, MACD en dirección, RSI 42-73 (L) / 27-58 (S), vela de confirmación. Break-even con solo 25% win rate. Objetivo: 3-5 entradas/semana.")

bt_col_left, bt_col_right = st.columns([1, 3])
with bt_col_left:
    bt_use_windows = st.checkbox("Solo ventanas 7-12h / 15-20h", value=True, key="bt_windows")
    if st.button("🚀 Ejecutar Backtest (~1 año)", type="primary", key="run_bt"):
        with st.spinner("Descargando datos EURUSD 1h (hasta 1 año)..."):
            bt_df = get_backtest_data("1h")
        if bt_df.empty:
            st.error("Sin datos historicos — verifica la conexion a internet")
        else:
            n_candles = len(bt_df)
            with st.spinner(f"Simulando {n_candles} velas ({n_candles//24} dias aprox)..."):
                st.session_state.backtest_result = run_full_backtest(
                    bt_df, use_windows=bt_use_windows
                )
            if not st.session_state.backtest_result:
                st.warning("Sin operaciones generadas — pocos datos o filtros muy estrictos")
            else:
                st.success(f"✅ Backtest completado: {st.session_state.backtest_result['total']} operaciones simuladas")

with bt_col_right:
    bt_res = st.session_state.backtest_result
    if bt_res:
        rr = bt_res.get("rr_ratio", 3.0)
        be_wr = bt_res.get("be_winrate", 25.0)
        wr = bt_res["winrate"]
        rentable = wr >= be_wr and bt_res["profit_factor"] >= 1.0

        # Métricas principales
        b1, b2, b3, b4, b5 = st.columns(5)
        b1.metric("Operaciones", bt_res["total"])
        b2.metric("Win Rate", f"{wr}%",
                  delta=f"{'✅ Rentable' if rentable else '⚠️ Marginal'} (BE={be_wr}%)")
        b3.metric("R:R Objetivo", f"1:{rr:.0f}")
        b4.metric("Profit Factor", f"{bt_res['profit_factor']}x",
                  delta="✅ Positivo" if bt_res["profit_factor"] >= 1 else "❌ Negativo")
        b5.metric("Net Pips", f"{bt_res['net_pips']:+.1f}p",
                  delta="✅" if bt_res["net_pips"] > 0 else "❌")

        b6, b7, b8 = st.columns(3)
        b6.metric("Wins / Losses", f"{bt_res['wins']}W / {bt_res['losses']}L")
        b7.metric("Max Drawdown", f"{bt_res['max_dd']}%",
                  delta="✅ Controlado" if bt_res["max_dd"] < 20 else "⚠️ Alto")
        b8.metric("P&L simulado", f"${bt_res['net_pnl']:+.2f}",
                  delta="(0.01 lot = $1/pip)")

        if rentable:
            st.success(f"✅ ESTRATEGIA RENTABLE — Win Rate {wr}% supera el break-even ({be_wr}%) con R:R 1:{rr:.0f}")
        else:
            st.warning(f"⚠️ Win Rate {wr}% — necesita >{be_wr}% para ser rentable con R:R 1:{rr:.0f}. Ajusta filtros.")

        # Curva de capital
        eq_list = bt_res.get("equity", [])
        if len(eq_list) > 2:
            st.write("**Curva de capital (capital inicial $10,000 / 0.01 lot):**")
            eq_df = pd.DataFrame({"Capital ($)": eq_list})
            eq_df.index.name = "Trade #"
            st.line_chart(eq_df, height=200)

        # Tabla de operaciones
        raw_trades = bt_res.get("trades", [])
        if raw_trades:
            with st.expander(f"Ver todas las operaciones ({len(raw_trades)})", expanded=False):
                td = pd.DataFrame(raw_trades)
                if "outcome" in td.columns:
                    td["Resultado"] = td["outcome"].map(
                        {"TP": "✅ TP", "SL": "❌ SL", "OPEN": "🔄 Abierta"})
                cols_show = [c for c in ["time", "dir", "Resultado", "pips", "pnl"] if c in td.columns]
                st.dataframe(
                    td[cols_show].rename(columns={
                        "time": "Entrada", "dir": "Direccion",
                        "pips": "Pips", "pnl": "P&L $"
                    }),
                    use_container_width=True, hide_index=True
                )
    else:
        st.info("Pulsa 'Ejecutar Backtest' para simular ~1 año de operaciones con la estrategia actual")

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

# ── Auto-rerun (Streamlit-nativo, preserva session_state) ─────────────────
# Siempre se ejecuta cuando auto-refresh está activo (se controla con sleep corto).
# El análisis pesado solo corre cuando elapsed >= refresh_secs (should_auto_refresh).
if refresh_secs > 0:
    now = time.time()
    last = st.session_state.get("last_analysis_time") or now
    elapsed = now - last
    remaining_secs = max(0.0, refresh_secs - elapsed)
    m, s = divmod(int(remaining_secs), 60)
    st.markdown(f"🔄 Próxima actualización en **{m:02d}:{s:02d}**")
    exec_time = time.time() - _APP_RERUN_START
    sleep_t = max(0.5, min(1.0, remaining_secs)) if remaining_secs > 0 else 1.0
    sleep_t = max(0.5, sleep_t - max(0.0, exec_time - 0.5))
    time.sleep(sleep_t)
    st.rerun()
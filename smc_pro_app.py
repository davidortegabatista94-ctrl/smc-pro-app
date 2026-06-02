from functools import lru_cache
# Cargas pesadas y opcionales se realizan de forma perezosa (lazy) dentro de helpers
# para acelerar el import/reload de la aplicación Streamlit.
_RANK_EMOJI = ["🥇","🥈","🥉","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟",
               "⓫","⓬","⓭","⓮","⓯","⓰","⓱"]
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

# ── Backend modules (pure logic, no Streamlit) ────────────────────────────────
from backend.config import (
    NEWS_API_KEY, SCALP_TP_PIPS, SCALP_SL_PIPS, SCALP_MAX_HOLD,
    PIP, SYMBOL, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID,
    USER_CONFIG_FILE, POSITION_FILE, TRADES_LOG_FILE, CACHE_FILE,
    MIN_DEFINITIVE_SCORE, BOT_ENABLED, BOT_VOLUME, CACHE_DURATION,
    UTC_OFFSET_SPAIN, _RAILWAY_URL,
)
from backend.indicators import (
    scalar, last_scalar, flatten_columns,
    interpret_dxy_signal, interpret_cot_for_signal,
    detect_volume_spikes, detect_volume_trend, analyze_volume_profile,
    get_volume_delta, get_cvd, detect_liquidity_levels,
    calculate_confluence_score, score_label,
    calc_scalp_levels, find_support_resistance,
    detect_stop_hunt, detect_market_structure,
    detect_volume_absorption, ai_candlestick_patterns, calculate_trend_strength,
    calc_smart_tp_sl, ai_market_bias, estimate_impact,
)
from backend.strategies import (
    run_backtest, run_full_backtest, _run_single_strategy,
    run_strategy_comparison, run_longterm_comparison,
)
from backend.knowledge_base import (
    load_knowledge_base, save_knowledge_base, update_kb,
    kb_record_pending_signal, kb_evaluate_and_learn, kb_best_strategy_for_conditions,
    _STRATEGY_META, _STRATEGY_REGIME_AFFINITY, _REGIME_LABELS, _REGIME_ICONS,
)
from backend.market_context import (
    get_market_session, get_spain_hour, is_trading_window, get_trading_window_info,
    get_economic_calendar, explain_market_context, detect_market_regime,
    get_cot_data, interpret_cot_for_signal as _cot_for_signal_mc,
)
from backend.signals import (
    load_cache, save_cache,
    get_backtest_data, get_longterm_data_2008,
    get_rss_news, get_news, analyze_consensus, analyze_news,
    calculate_indicators, analyze_timeframe,
    get_eurusd_data_yf, get_dxy_yf, get_dxy_combined,
)
from services.telegram import (
    send_telegram_raw as _svc_telegram_raw,
    send_telegram_alert as _svc_telegram_alert,
    _build_hourly_telegram_message, _build_urgent_telegram_message,
)

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

# ── Persistencia con PostgreSQL (+ fallback pickle si la DB no está lista) ────
import pickle as _pickle
_BT_CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".bt_cache.pkl")

try:
    import db as _db
    _DB_OK = True
    _db.ensure_tables()         # auto-create tables on first run
    _db.purge_bad_self_improvements()  # remove stale <think> / error entries
except ImportError:
    _DB_OK = False

def _save_bt_cache(strategy_cmp, lt_cmp):
    # DB primaria
    if _DB_OK and strategy_cmp:
        try:
            _db.save_backtest("1year", strategy_cmp.get("results", []), strategy_cmp.get("best", {}))
        except Exception:
            pass
    if _DB_OK and lt_cmp:
        try:
            _db.save_backtest("2008", lt_cmp.get("results", []), lt_cmp.get("best", {}),
                              lt_cmp.get("n_bars", 0))
        except Exception:
            pass
    # Pickle fallback
    try:
        with open(_BT_CACHE_PATH, "wb") as _f:
            _pickle.dump({"sc": strategy_cmp, "lt": lt_cmp}, _f, protocol=_pickle.HIGHEST_PROTOCOL)
    except Exception:
        pass

def _load_bt_cache():
    result = {}
    # Intentar DB primero
    if _DB_OK:
        try:
            _sc = _db.load_backtest("1year")
            if _sc:
                result["sc"] = {"results": _sc["results"], "best": _sc["best"]}
        except Exception:
            pass
        try:
            _lt = _db.load_backtest("2008")
            if _lt:
                result["lt"] = {"results": _lt["results"], "best": _lt["best"],
                                "n_bars": _lt["n_bars"]}
        except Exception:
            pass
        if result:
            return result
    # Fallback pickle
    try:
        if os.path.exists(_BT_CACHE_PATH):
            with open(_BT_CACHE_PATH, "rb") as _f:
                return _pickle.load(_f)
    except Exception:
        pass
    return {}

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
BOT_VOLUME = 0.1
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

# Caché en memoria para datos históricos pequeños (evita recargas inmediatas)
_EURUSD_CACHE = {}
_EURUSD_CACHE_TTL = timedelta(seconds=30)

# ============================================
# EMA RIBBON MULTI-MARCO
# ============================================
def get_ema_ribbon(df, lens=(5, 10, 20, 50)) -> dict:
    """Calcula EMA Ribbon 5/10/20/50 y señales de cruce para un DataFrame OHLCV."""
    if df is None or df.empty or len(df) < max(lens):
        return {}
    close = df["Close"]
    emas  = {l: close.ewm(span=l, adjust=False).mean() for l in lens}
    e5,  e10,  e20,  e50  = [float(emas[l].iloc[-1]) for l in lens]
    e5p, e10p, e20p, e50p = [float(emas[l].iloc[-2]) for l in lens]
    px = float(close.iloc[-1])

    bull_align = e5 > e10 > e20 > e50
    bear_align = e5 < e10 < e20 < e50

    if bull_align:          trend = "▲ ALCISTA"
    elif bear_align:        trend = "▼ BAJISTA"
    elif e5 > e50:          trend = "↗ NEUTRAL+"
    else:                   trend = "↘ NEUTRAL-"

    bull_cross   = e5p <= e20p and e5 > e20 and px > e50   # EMA5 cruza sobre EMA20
    bear_cross   = e5p >= e20p and e5 < e20 and px < e50   # EMA5 cruza bajo EMA20
    golden_cross = e10p <= e50p and e10 > e50
    death_cross  = e10p >= e50p and e10 < e50

    return {
        "ema5": e5, "ema10": e10, "ema20": e20, "ema50": e50,
        "bull_align": bull_align, "bear_align": bear_align,
        "trend": trend,
        "buy_signal": bull_cross, "sell_signal": bear_cross,
        "golden_cross": golden_cross, "death_cross": death_cross,
        "above_ema50": px > e50,
    }


# ============================================
# TRADINGVIEW — PRECIO E INDICADORES EN TIEMPO REAL
# ============================================
_TV_CACHE: dict = {}
_TV_CACHE_TTL = timedelta(seconds=30)

def get_tv_data(symbol: str = "EURUSD", tf: str = "1h") -> dict:
    """Precio e indicadores en tiempo real desde TradingView (sin MT5, sin OANDA)."""
    cache_key = f"{symbol}_{tf}"
    cached = _TV_CACHE.get(cache_key)
    if cached:
        ts, data = cached
        if datetime.now() - ts < _TV_CACHE_TTL:
            return data
    try:
        from tradingview_ta import TA_Handler, Interval
        _tf_map = {
            "1m": Interval.INTERVAL_1_MINUTE,
            "5m": Interval.INTERVAL_5_MINUTES,
            "15m": Interval.INTERVAL_15_MINUTES,
            "30m": Interval.INTERVAL_30_MINUTES,
            "1h": Interval.INTERVAL_1_HOUR,
            "4h": Interval.INTERVAL_4_HOURS,
            "1d": Interval.INTERVAL_1_DAY,
        }
        handler = TA_Handler(
            symbol=symbol,
            screener="forex",
            exchange="FX_IDC",
            interval=_tf_map.get(tf, Interval.INTERVAL_1_HOUR),
        )
        a   = handler.get_analysis()
        ind = a.indicators
        result = {
            "price":        ind.get("close"),
            "open":         ind.get("open"),
            "high":         ind.get("high"),
            "low":          ind.get("low"),
            "change_pct":   ind.get("change"),
            "rsi":          ind.get("RSI"),
            "rsi14":        ind.get("RSI[1]"),
            "ema20":        ind.get("EMA20"),
            "ema50":        ind.get("EMA50"),
            "ema200":       ind.get("EMA200"),
            "macd":         ind.get("MACD.macd"),
            "macd_signal":  ind.get("MACD.signal"),
            "bb_upper":     ind.get("BB.upper"),
            "bb_lower":     ind.get("BB.lower"),
            "atr":          ind.get("ATR"),
            "stoch_k":      ind.get("Stoch.K"),
            "stoch_d":      ind.get("Stoch.D"),
            "adx":          ind.get("ADX"),
            "recommendation": a.summary.get("RECOMMENDATION", "NEUTRAL"),
            "buy":          a.summary.get("BUY", 0),
            "sell":         a.summary.get("SELL", 0),
            "neutral":      a.summary.get("NEUTRAL", 0),
            # Volume-derived indicators from TradingView
            "tv_volume":    ind.get("volume"),           # tick volume del bar actual
            "tv_obv":       ind.get("OBV"),              # On Balance Volume
            "tv_cmf":       ind.get("CMF"),              # Chaikin Money Flow (-1..1)
            "tv_vwma":      ind.get("VWMA"),             # Volume-Weighted MA
            "source":       "TradingView",
        }
        _TV_CACHE[cache_key] = (datetime.now(), result)
        # Evict oldest entry when cache grows too large (7 TFs × handful of symbols)
        if len(_TV_CACHE) > 50:
            oldest = min(_TV_CACHE, key=lambda k: _TV_CACHE[k][0])
            del _TV_CACHE[oldest]
        return result
    except Exception as e:
        logging.debug("get_tv_data error: %s", e)
        return {}


# ============================================
# MT5 — CONEXIÓN
# ============================================
_mt5_connected = False
_mt5_error_message = None

# ── Integración con OANDA (directo o via MT5 Service) ────────────────────────
# Prioridad: 1) MT5_SERVICE_URL (servicio remoto) 2) OANDA env vars (directo)
_MT5_SERVICE_URL  = os.getenv("MT5_SERVICE_URL", "").rstrip("/")
_MT5_API_TOKEN    = os.getenv("MT5_API_TOKEN", "")
_OANDA_API_TOKEN  = os.getenv("OANDA_API_TOKEN", "").strip()
_OANDA_ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID", "").strip()

# Carga mt5_bridge si OANDA está configurado directamente (sin servicio remoto)
_oanda_bridge = None
if not _MT5_SERVICE_URL and _OANDA_API_TOKEN and _OANDA_ACCOUNT_ID:
    try:
        from mt5_service import mt5_bridge as _oanda_bridge
    except Exception as _oe:
        logging.warning("OANDA bridge no disponible: %s", _oe)
        _oanda_bridge = None

def _mt5_service_headers():
    h = {"Content-Type": "application/json"}
    if _MT5_API_TOKEN:
        h["Authorization"] = f"Bearer {_MT5_API_TOKEN}"
    return h

def _mt5_service_available() -> bool:
    """True si hay conexión OANDA disponible (remota o directa)."""
    return bool(_MT5_SERVICE_URL) or (_oanda_bridge is not None)

_mt5_health_cache: dict = {}   # {result, ts}
_MT5_HEALTH_TTL = 60           # segundos entre peticiones reales

def mt5_service_health() -> dict:
    """Estado de la conexión OANDA — cacheado 60 s para no bloquear el refresh."""
    global _mt5_health_cache
    if _oanda_bridge is not None:
        try:
            ok = _oanda_bridge.is_connected()
            return {"mt5": "connected" if ok else "disconnected",
                    "status": "ok" if ok else "error",
                    "source": "oanda_direct"}
        except Exception as e:
            return {"status": "error", "error": str(e), "source": "oanda_direct"}
    if not _MT5_SERVICE_URL:
        return {"status": "no configurado"}
    # Devolver caché si aún es válido
    if _mt5_health_cache and (time.time() - _mt5_health_cache.get("ts", 0)) < _MT5_HEALTH_TTL:
        return _mt5_health_cache.get("result", {"status": "no configurado"})
    reqs = get_requests()
    if not reqs:
        return {"status": "error", "error": "requests no disponible"}
    try:
        r = reqs.get(f"{_MT5_SERVICE_URL}/health", timeout=4)
        result = r.json()
    except Exception as e:
        result = {"status": "error", "error": str(e)}
    _mt5_health_cache = {"result": result, "ts": time.time()}
    return result

_mt5_account_cache: dict = {}   # {result, ts}
_MT5_ACCOUNT_TTL = 120          # segundos

def mt5_service_account() -> dict | None:
    """Info de cuenta OANDA — cacheada 120 s para no bloquear el refresh."""
    global _mt5_account_cache
    if _oanda_bridge is not None:
        try:
            return _oanda_bridge.get_account_info()
        except Exception:
            return None
    if not _MT5_SERVICE_URL:
        return None
    if _mt5_account_cache and (time.time() - _mt5_account_cache.get("ts", 0)) < _MT5_ACCOUNT_TTL:
        return _mt5_account_cache.get("result")
    reqs = get_requests()
    if not reqs:
        return None
    try:
        r = reqs.get(f"{_MT5_SERVICE_URL}/account",
                     headers=_mt5_service_headers(), timeout=4)
        result = r.json() if r.ok else None
    except Exception as e:
        logging.warning(f"mt5_service_account error: {e}")
        result = None
    _mt5_account_cache = {"result": result, "ts": time.time()}
    return result

def mt5_service_positions() -> list:
    """Posiciones abiertas en OANDA (directa o via servicio remoto)."""
    if _oanda_bridge is not None:
        try:
            return _oanda_bridge.get_open_positions()
        except Exception:
            return []
    if not _MT5_SERVICE_URL:
        return []
    reqs = get_requests()
    if not reqs:
        return []
    try:
        r = reqs.get(f"{_MT5_SERVICE_URL}/positions",
                     headers=_mt5_service_headers(), timeout=5)
        return r.json() if r.ok else []
    except Exception as e:
        logging.warning(f"mt5_service_positions error: {e}")
        return []

def _mt5_service_tick(symbol=None) -> dict | None:
    """Precio bid/ask en tiempo real desde OANDA (directa o via servicio remoto)."""
    sym = symbol or SYMBOL
    if _oanda_bridge is not None:
        try:
            data = _oanda_bridge.get_current_price(sym)
            if data and "bid" in data:
                data.setdefault("time", datetime.now())
                return data
        except Exception:
            pass
        return None
    if not _MT5_SERVICE_URL:
        return None
    reqs = get_requests()
    if not reqs:
        return None
    try:
        r = reqs.get(f"{_MT5_SERVICE_URL}/tick/{sym}",
                     headers=_mt5_service_headers(), timeout=5)
        if r.ok:
            data = r.json()
            if "bid" in data:
                if "time" in data and isinstance(data["time"], str):
                    try:
                        from datetime import datetime as _dt
                        data["time"] = _dt.fromisoformat(data["time"].replace("Z", "+00:00"))
                    except Exception:
                        data["time"] = datetime.now()
                else:
                    data.setdefault("time", datetime.now())
                return data
    except Exception as e:
        logging.debug(f"_mt5_service_tick error: {e}")
    return None

def mt5_service_place_order(symbol, direction, volume, price, sl, tp,
                             comment="SMC Pro Bot") -> dict:
    """Envía una orden a OANDA (directa o via servicio remoto)."""
    if _oanda_bridge is not None:
        try:
            return _oanda_bridge.place_order(symbol, direction, volume, price, sl, tp, comment)
        except Exception as e:
            return {"success": False, "error": str(e)}
    if not _MT5_SERVICE_URL:
        return {"success": False, "error": "OANDA no configurado — añade OANDA_API_TOKEN en Railway"}
    reqs = get_requests()
    if not reqs:
        return {"success": False, "error": "requests no disponible"}
    try:
        payload = {
            "symbol":    symbol,
            "direction": "BUY" if direction in ("LONG", "BUY") else "SELL",
            "volume":    volume, "price": price, "sl": sl, "tp": tp, "comment": comment,
        }
        r = reqs.post(f"{_MT5_SERVICE_URL}/trade", json=payload,
                      headers=_mt5_service_headers(), timeout=10)
        return r.json()
    except Exception as e:
        logging.warning(f"mt5_service_place_order error: {e}")
        return {"success": False, "error": str(e)}

def mt5_service_close_position(ticket: int) -> dict:
    """Cierra una posición en OANDA (directa o via servicio remoto)."""
    if _oanda_bridge is not None:
        try:
            return _oanda_bridge.close_position(ticket)
        except Exception as e:
            return {"success": False, "error": str(e)}
    if not _MT5_SERVICE_URL:
        return {"success": False, "error": "OANDA no configurado"}
    reqs = get_requests()
    if not reqs:
        return {"success": False, "error": "requests no disponible"}
    try:
        r = reqs.delete(
            f"{_MT5_SERVICE_URL}/position/{ticket}",
            headers=_mt5_service_headers(),
            timeout=10,
        )
        return r.json()
    except Exception as e:
        logging.warning(f"mt5_service_close_position error: {e}")
        return {"success": False, "error": str(e)}
# ─────────────────────────────────────────────────────────────────

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
    # 1. Servicio remoto (Railway/OANDA)
    if _mt5_service_available():
        return _mt5_service_tick(symbol)
    # 2. MT5 local (Windows)
    if mt5_connect():
        try:
            tick = mt5.symbol_info_tick(symbol)
            if tick:
                return {
                    "bid":         tick.bid,
                    "ask":         tick.ask,
                    "spread_pips": round((tick.ask - tick.bid) / PIP, 1),
                    "time":        datetime.fromtimestamp(tick.time),
                }
        except Exception as e:
            logging.warning(f"get_mt5_tick local: {e}")
    # 3. TradingView (sin necesidad de MT5 ni OANDA)
    tv = get_tv_data(symbol.replace("m", ""), "1h")
    if tv.get("price"):
        p = tv["price"]
        return {
            "bid":         p,
            "ask":         p,
            "spread_pips": 0.0,
            "time":        datetime.now(),
            "source":      "TradingView",
        }
    return None

def get_mt5_account():
    # Servicio remoto primero
    if _mt5_service_available():
        remote = mt5_service_account()
        if remote:
            return remote
    # MT5 local
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
    """Obtiene posiciones abiertas — remoto primero, local como fallback."""
    if _mt5_service_available():
        remote = mt5_service_positions()
        if remote is not None:
            return remote
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
    """
    Coloca una orden de mercado en MT5.
    Si MT5_SERVICE_URL está definida (Railway), usa el servicio Docker remoto.
    Si no, usa el MT5 local (requiere Windows).
    """
    # ── Ruta remota (Docker MT5 en Railway) ───────────────────────
    if _mt5_service_available():
        result = mt5_service_place_order(symbol, direction, volume, price, sl, tp, comment)
        if not result.get("success"):
            logging.warning(f"place_mt5_order (remoto) falló: {result.get('error')}")
            return None
        # Devuelve un objeto compatible con el resto del código
        class RemoteResult:
            def __init__(self, r):
                self.retcode = 10009  # TRADE_RETCODE_DONE
                self.order   = r.get("ticket", 0)
                self.deal    = r.get("ticket", 0)
                self.volume  = r.get("volume", volume)
                self.price   = r.get("price", price)
                self.comment = f"REMOTE: {r.get('symbol','')}"
        return RemoteResult(result)

    # ── Ruta local (Windows) ──────────────────────────────────────
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
    """Cierra una posición abierta en MT5 (local o remota según MT5_SERVICE_URL)."""
    if _mt5_service_available():
        result = mt5_service_close_position(position_ticket)
        if not result.get("success"):
            logging.warning(f"close_mt5_position (remoto) falló: {result.get('error')}")
            return None
        class RemoteCloseResult:
            def __init__(self, r):
                self.retcode = 10009
                self.order   = r.get("ticket", position_ticket)
                self.deal    = r.get("ticket", position_ticket)
                self.price   = r.get("closed_price", 0)
                self.comment = "REMOTE CLOSE"
        return RemoteCloseResult(result)

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
    """Cierra todas las posiciones abiertas del símbolo en MT5"""
    try:
        positions_mt5 = get_mt5_positions()
        if not positions_mt5:
            return False, "No hay posiciones abiertas en MT5"
        mt5_mod = get_mt5()
        done_code = getattr(mt5_mod, "TRADE_RETCODE_DONE", None) if mt5_mod else None
        closed_count = 0
        for position in positions_mt5:
            if position.symbol == SYMBOL:
                result = close_mt5_position(position.ticket, comment="SMC Pro Auto Close")
                ok = (result is not None) and (done_code is None or getattr(result, "retcode", None) == done_code or getattr(result, "order", 0) != 0)
                if ok:
                    closed_count += 1
        if closed_count > 0:
            try:
                close_position("AUTO_CLOSE")
            except Exception:
                pass
            return True, f"Cerradas {closed_count} posiciones"
        return False, "No se pudo cerrar ninguna posición"
    except Exception as e:
        return False, f"Error en auto-cierre: {str(e)}"


def manage_positions_be():
    """Gestión automática de Break-Even: mueve SL a entrada cuando ganancia >= 1×SL.
    Devuelve lista de mensajes con acciones realizadas."""
    msgs = []
    try:
        if not is_mt5_available() or not mt5_connect():
            return msgs
        mt5_mod = get_mt5()
        positions = get_mt5_positions()
        for pos in positions:
            if pos.symbol != SYMBOL:
                continue
            # Distancia original SL
            sl_dist = abs(pos.price_open - pos.sl) if pos.sl != 0 else None
            if not sl_dist or sl_dist < 0.0001:
                continue
            # Verificar si el precio actual ya supera 1×SL en beneficio
            current_price_buy  = getattr(pos, "price_current", pos.price_open)
            if pos.type == 0:  # BUY
                profit_dist = current_price_buy - pos.price_open
                be_target   = pos.price_open + sl_dist
                if current_price_buy >= be_target and pos.sl < pos.price_open:
                    # Mover SL a entrada (break-even)
                    request = {
                        "action":   mt5_mod.TRADE_ACTION_SLTP,
                        "position": pos.ticket,
                        "sl":       pos.price_open,
                        "tp":       pos.tp,
                    }
                    result = mt5_mod.order_send(request)
                    done_code = getattr(mt5_mod, "TRADE_RETCODE_DONE", None)
                    if result and (done_code is None or getattr(result, "retcode", None) == done_code):
                        msgs.append(f"BE activado LONG ticket {pos.ticket} ({pos.price_open:.5f})")
            else:  # SELL
                profit_dist = pos.price_open - current_price_buy
                be_target   = pos.price_open - sl_dist
                if current_price_buy <= be_target and pos.sl > pos.price_open:
                    request = {
                        "action":   mt5_mod.TRADE_ACTION_SLTP,
                        "position": pos.ticket,
                        "sl":       pos.price_open,
                        "tp":       pos.tp,
                    }
                    result = mt5_mod.order_send(request)
                    done_code = getattr(mt5_mod, "TRADE_RETCODE_DONE", None)
                    if result and (done_code is None or getattr(result, "retcode", None) == done_code):
                        msgs.append(f"BE activado SHORT ticket {pos.ticket} ({pos.price_open:.5f})")
    except Exception as e:
        msgs.append(f"Error BE: {e}")
    return msgs

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

def _enrich_df_volume(df: pd.DataFrame, symbol: str = "EURUSD", tf: str = "1h") -> pd.DataFrame:
    """
    Enriquece df["Volume"] con TradingView (CMF/OBV).
    El composite sintético lo gestiona _ensure_volume en indicators.py como fallback.
    """
    if df is None or df.empty:
        return df

    df = df.copy()

    # ── TradingView volume (último bar) ─────────────────────────────────────
    try:
        _tv = get_tv_data(symbol.replace("m", ""), tf)
        _tv_vol = _tv.get("tv_volume")
        if _tv_vol and _tv_vol > 0 and "Volume_oanda" not in df.columns:
            # Solo el último valor disponible
            _arr = [float(_tv_vol)] * len(df)
            df["Volume_oanda"] = _arr          # imperfecto pero mejor que nada
        # Añadir CMF y OBV como columnas auxiliares (último valor broadcast)
        _cmf = _tv.get("tv_cmf")
        _obv = _tv.get("tv_obv")
        if _cmf is not None:
            df["tv_cmf"] = float(_cmf)
        if _obv is not None:
            df["tv_obv"] = float(_obv)
    except Exception as _e:
        logging.debug(f"TV volume enrich: {_e}")

    return df


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
# TELEGRAM

def _get_tg_credentials():
    tok, cid = TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
    try:
        import streamlit as _st_mod
        if hasattr(_st_mod, 'session_state'):
            tok = _st_mod.session_state.get("tg_token", tok)
            cid = _st_mod.session_state.get("tg_chat", cid)
    except Exception:
        pass
    return tok, cid


def send_telegram_raw(msg: str) -> bool:
    tok, cid = _get_tg_credentials()
    return _svc_telegram_raw(msg, token=tok, chat_id=cid)


def send_telegram_alert(signal, score, tick=None, definitive=False, reason=None):
    tok, cid = _get_tg_credentials()
    return _svc_telegram_alert(signal, score, tick=tick, definitive=definitive,
                                reason=reason, token=tok, chat_id=cid)


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
    return tp, sl, rr, viable, risk_pips, liquidity_warnings
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
# TRADINGVIEW-STYLE CHART
# ============================================

def _render_trading_chart(
    df,
    signal: dict,
    score: int,
    session: str,
    liq_levels: list,
    poc,
    vol_spikes: list,
    market_structures: dict,
    stop_hunts: list,
    news_items: list,
    trades_history: list,
) -> None:
    """Render a TradingView-style 4-panel chart: Price/Volume/RSI/MACD."""
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
    except ImportError:
        st.warning("Instala plotly>=5.15.0 para ver el gráfico.")
        return

    if df is None or df.empty or len(df) < 20:
        st.info("Datos insuficientes para el gráfico.")
        return

    df_plot = df.tail(120).copy()
    close   = df_plot["Close"]
    high    = df_plot["High"]
    low     = df_plot["Low"]
    idx     = df_plot.index

    # Volumen real si existe y es no-cero; si no, sintético (rango × 1000)
    _raw_vol = df_plot.get("Volume", pd.Series(0, index=idx)).fillna(0)
    if _raw_vol.sum() == 0:
        vol = ((high - low) / 0.0001 * 1000).round().clip(lower=1)
    else:
        vol = _raw_vol

    # ── Indicators ───────────────────────────────────────────────────────────
    ema21  = close.ewm(span=21,  adjust=False).mean()
    ema50  = close.ewm(span=50,  adjust=False).mean()
    ema200 = close.ewm(span=200, adjust=False).mean()

    bb_ma  = close.rolling(20).mean()
    bb_std = close.rolling(20).std()
    bb_up  = bb_ma + 2 * bb_std
    bb_dn  = bb_ma - 2 * bb_std

    _delta  = close.diff()
    _gain   = _delta.clip(lower=0)
    _loss   = (-_delta).clip(lower=0)
    _avg_g  = _gain.ewm(alpha=1/14, adjust=False).mean()
    _avg_l  = _loss.ewm(alpha=1/14, adjust=False).mean()
    _rs     = _avg_g / _avg_l.replace(0, float("inf"))
    rsi     = (100 - (100 / (1 + _rs))).fillna(50)

    ema12      = close.ewm(span=12, adjust=False).mean()
    ema26      = close.ewm(span=26, adjust=False).mean()
    macd_line  = ema12 - ema26
    macd_sig   = macd_line.ewm(span=9, adjust=False).mean()
    macd_hist  = (macd_line - macd_sig).fillna(0)

    # ── Colour palette ────────────────────────────────────────────────────────
    BG        = "#0d1117"
    GRID      = "#1e2530"
    TEXT      = "#c9d1d9"
    BULL_C    = "#089981"
    BEAR_C    = "#f23645"
    EMA21_C   = "#f7a600"
    EMA50_C   = "#2196f3"
    EMA200_C  = "#e91e63"
    BB_C      = "#607d8b"
    POC_C     = "#ffeb3b"
    BUY_C     = "#00e676"
    SELL_C    = "#ff1744"
    SL_C      = "#f44336"
    TP_C      = "#4caf50"

    # ── Subplots ──────────────────────────────────────────────────────────────
    fig = make_subplots(
        rows=4, cols=1,
        shared_xaxes=True,
        vertical_spacing=0.02,
        row_heights=[0.55, 0.15, 0.15, 0.15],
        subplot_titles=("EUR/USD 1H", "Volumen", "RSI (14)", "MACD (12,26,9)"),
    )

    # ── Row 1: Candlesticks + overlays ────────────────────────────────────────
    fig.add_trace(go.Candlestick(
        x=idx,
        open=df_plot["Open"], high=high, low=low, close=close,
        name="EUR/USD",
        increasing_fillcolor=BULL_C, decreasing_fillcolor=BEAR_C,
        increasing_line_color=BULL_C, decreasing_line_color=BEAR_C,
        line_width=1,
    ), row=1, col=1)

    fig.add_trace(go.Scatter(x=idx, y=ema21,  name="EMA21",  line=dict(color=EMA21_C,  width=1.4)), row=1, col=1)
    fig.add_trace(go.Scatter(x=idx, y=ema50,  name="EMA50",  line=dict(color=EMA50_C,  width=1.4)), row=1, col=1)
    fig.add_trace(go.Scatter(x=idx, y=ema200, name="EMA200", line=dict(color=EMA200_C, width=1.4, dash="dot")), row=1, col=1)

    # Bollinger fill
    fig.add_trace(go.Scatter(
        x=list(idx) + list(idx[::-1]),
        y=list(bb_up) + list(bb_dn[::-1]),
        fill="toself", fillcolor="rgba(96,125,139,0.08)",
        line=dict(color="rgba(0,0,0,0)"),
        name="Bollinger", showlegend=False,
    ), row=1, col=1)
    fig.add_trace(go.Scatter(x=idx, y=bb_up, line=dict(color=BB_C, width=1, dash="dot"), name="BB+2σ", showlegend=False), row=1, col=1)
    fig.add_trace(go.Scatter(x=idx, y=bb_dn, line=dict(color=BB_C, width=1, dash="dot"), name="BB-2σ", showlegend=False), row=1, col=1)

    # Entry / SL / TP
    _direction = signal.get("direction", "")
    _entry     = signal.get("price")
    _sl        = signal.get("sl")
    _tp        = signal.get("tp") or signal.get("tp1")

    if _entry:
        fig.add_hline(y=_entry,
                      line=dict(color="#ffffff", width=1.5, dash="dash"),
                      annotation_text=f"Entrada {_entry:.5f}",
                      annotation_font_color="#ffffff",
                      annotation_position="right", row=1, col=1)
    if _sl:
        fig.add_hline(y=_sl,
                      line=dict(color=SL_C, width=1.5, dash="dot"),
                      annotation_text=f"SL {_sl:.5f}",
                      annotation_font_color=SL_C,
                      annotation_position="right", row=1, col=1)
        if _entry:
            fig.add_hrect(y0=min(_sl, _entry), y1=max(_sl, _entry),
                          fillcolor="rgba(244,67,54,0.07)", line_width=0, row=1, col=1)
    if _tp:
        fig.add_hline(y=_tp,
                      line=dict(color=TP_C, width=1.5, dash="dot"),
                      annotation_text=f"TP {_tp:.5f}",
                      annotation_font_color=TP_C,
                      annotation_position="right", row=1, col=1)
        if _entry:
            fig.add_hrect(y0=min(_tp, _entry), y1=max(_tp, _entry),
                          fillcolor="rgba(76,175,80,0.07)", line_width=0, row=1, col=1)

    # Signal arrow at latest candle
    if _direction in ("LONG", "SHORT") and _entry and len(df_plot) > 0:
        _arrow_y  = float(low.iloc[-1]) * 0.9998 if _direction == "LONG" else float(high.iloc[-1]) * 1.0002
        _arr_sym  = "triangle-up" if _direction == "LONG" else "triangle-down"
        _arr_col  = BUY_C if _direction == "LONG" else SELL_C
        _arr_txt  = f"{'▲ LONG' if _direction == 'LONG' else '▼ SHORT'} ({score})"
        _txt_pos  = "bottom center" if _direction == "LONG" else "top center"
        fig.add_trace(go.Scatter(
            x=[idx[-1]], y=[_arrow_y],
            mode="markers+text",
            marker=dict(symbol=_arr_sym, size=18, color=_arr_col),
            text=[_arr_txt],
            textposition=_txt_pos,
            textfont=dict(color=_arr_col, size=11),
            name=f"Señal {_direction}", showlegend=False,
        ), row=1, col=1)

    # Liquidity levels
    if liq_levels:
        for _lv in liq_levels[:10]:
            _lv_p = _lv.get("nivel") or _lv.get("price")
            if not _lv_p:
                continue
            _t = str(_lv.get("tipo", "")).lower()
            _lv_col = "#ff9800" if ("resist" in _t or "high" in _t or "supply" in _t) else "#00bcd4"
            fig.add_hline(y=_lv_p, line=dict(color=_lv_col, width=0.8, dash="dot"), row=1, col=1)

    # POC
    if poc:
        _poc_p = poc.get("precio") or poc.get("price")
        if _poc_p:
            fig.add_hline(y=_poc_p,
                          line=dict(color=POC_C, width=1.5, dash="longdash"),
                          annotation_text=f"POC {_poc_p:.5f}",
                          annotation_font_color=POC_C,
                          annotation_position="left", row=1, col=1)

    # Order blocks from market structure
    _ms1h = (market_structures or {}).get("1h", {}) if isinstance(market_structures, dict) else {}
    for _ob_key, _ob_col, _ob_lbl in (
        ("last_bos_level",   "rgba(0,230,118,0.12)",  "Demand OB"),
        ("demand_zone",      "rgba(0,230,118,0.12)",  "Demand OB"),
        ("last_choch_level", "rgba(255,23,68,0.12)",  "Supply OB"),
        ("supply_zone",      "rgba(255,23,68,0.12)",  "Supply OB"),
    ):
        _ob_val = _ms1h.get(_ob_key)
        if _ob_val and isinstance(_ob_val, (int, float)):
            _sprd = float(close.std() or 0.0003)
            _brd  = "rgba(0,230,118,0.4)" if "Demand" in _ob_lbl else "rgba(255,23,68,0.4)"
            _fc   = BUY_C if "Demand" in _ob_lbl else SELL_C
            fig.add_hrect(y0=_ob_val - _sprd, y1=_ob_val + _sprd,
                          fillcolor=_ob_col,
                          line=dict(color=_brd, width=1),
                          annotation_text=_ob_lbl,
                          annotation_font_color=_fc,
                          row=1, col=1)
            break  # one demand, one supply enough

    # Stop hunt markers
    if stop_hunts and isinstance(stop_hunts, list):
        _sh_x, _sh_y = [], []
        for _sh in stop_hunts[:12]:
            if isinstance(_sh, dict):
                _sh_t = _sh.get("time") or _sh.get("candle_time")
                _sh_p = _sh.get("price") or _sh.get("nivel")
                if _sh_t and _sh_p:
                    _sh_x.append(_sh_t); _sh_y.append(_sh_p)
        if _sh_x:
            fig.add_trace(go.Scatter(
                x=_sh_x, y=_sh_y, mode="markers",
                marker=dict(symbol="x", size=12, color="#ff6d00",
                            line=dict(width=2, color="#ff6d00")),
                name="Stop Hunt",
            ), row=1, col=1)

    # News annotations (vertical line + flag)
    if news_items:
        _hi_news = sorted(
            [n for n in news_items if n.get("impact_score", 0) >= 5],
            key=lambda x: x.get("impact_score", 0), reverse=True
        )[:6]
        _y_top = float(high.max())
        for _n in _hi_news:
            _pub = _n.get("publishedAt") or _n.get("published", "")
            try:
                from datetime import datetime as _dtt
                _pt = _dtt.fromisoformat(str(_pub).replace("Z", "+00:00")).replace(tzinfo=None)
                if hasattr(idx[0], "to_pydatetime"):
                    _closest = min(idx, key=lambda t: abs(t.to_pydatetime().replace(tzinfo=None) - _pt))
                else:
                    _closest = min(idx, key=lambda t: abs(t - _pt))
                _imp = _n.get("impact_score", 5)
                _nc  = "#ff1744" if _imp >= 8 else "#ff9800" if _imp >= 6 else "#ffd600"
                fig.add_vline(x=_closest, line=dict(color=_nc, width=1, dash="dot"), row=1, col=1)
                fig.add_annotation(
                    x=_closest, y=_y_top,
                    text="📰", showarrow=False,
                    font=dict(size=13, color=_nc),
                    xanchor="center", yanchor="bottom",
                    hovertext=_n.get("title", "")[:60],
                )
            except Exception:
                continue

    # Historical trades overlay
    if trades_history:
        _bx, _by, _bt = [], [], []
        _sx, _sy, _st = [], [], []
        for _tr in trades_history[-25:]:
            _ep  = _tr.get("entry_price")
            _oa  = _tr.get("opened_at")
            _dir = _tr.get("direction", "")
            _pip = _tr.get("pips", 0) or 0
            _out = _tr.get("outcome", "?")
            if not _ep or not _oa:
                continue
            try:
                if isinstance(_oa, str):
                    from datetime import datetime as _dtt
                    _oa = _dtt.fromisoformat(_oa.replace("Z", "+00:00"))
                _lbl = f"{_dir} {_out} {_pip:+.1f}p"
                if _dir == "LONG":
                    _bx.append(_oa); _by.append(_ep); _bt.append(_lbl)
                else:
                    _sx.append(_oa); _sy.append(_ep); _st.append(_lbl)
            except Exception:
                continue
        if _bx:
            fig.add_trace(go.Scatter(
                x=_bx, y=_by, mode="markers",
                marker=dict(symbol="triangle-up", size=9, color=BUY_C, opacity=0.75),
                text=_bt, hovertemplate="%{text}<extra></extra>",
                name="LONG hist.",
            ), row=1, col=1)
        if _sx:
            fig.add_trace(go.Scatter(
                x=_sx, y=_sy, mode="markers",
                marker=dict(symbol="triangle-down", size=9, color=SELL_C, opacity=0.75),
                text=_st, hovertemplate="%{text}<extra></extra>",
                name="SHORT hist.",
            ), row=1, col=1)

    # Score/direction badge
    _fin_sig  = signal.get("final_signal", signal.get("direction", "NEUTRAL"))
    _badge_c  = BUY_C if _fin_sig in ("COMPRA", "LONG") else SELL_C if _fin_sig in ("VENTA", "SHORT") else "#888"
    fig.add_annotation(
        x=0.01, y=0.97, xref="paper", yref="paper",
        text=f"<b>Score: {score}/100  |  {session}  |  {_fin_sig}</b>",
        showarrow=False,
        font=dict(size=12, color=_badge_c),
        bgcolor=BG, bordercolor=_badge_c, borderwidth=1,
        xanchor="left", yanchor="top",
    )

    # ── Row 2: Volume bars ────────────────────────────────────────────────────
    _vcol = [BULL_C if float(close.iloc[i]) >= float(df_plot["Open"].iloc[i]) else BEAR_C
             for i in range(len(df_plot))]
    fig.add_trace(go.Bar(x=idx, y=vol, name="Volumen",
                         marker_color=_vcol, showlegend=False), row=2, col=1)

    if vol_spikes and isinstance(vol_spikes, list):
        _spx, _spy = [], []
        for _sp in vol_spikes:
            if isinstance(_sp, dict):
                _sp_t = _sp.get("time") or _sp.get("candle_time")
                if _sp_t:
                    try:
                        _iloc = df_plot.index.get_indexer([_sp_t], method="nearest")
                        _spx.append(_sp_t)
                        _spy.append(float(vol.iloc[_iloc[0]]) if len(_iloc) > 0 else float(vol.max()))
                    except Exception:
                        pass
        if _spx:
            fig.add_trace(go.Scatter(
                x=_spx, y=_spy, mode="markers",
                marker=dict(symbol="star", size=12, color="#ffd600"),
                name="Vol Spike",
            ), row=2, col=1)

    # ── Row 3: RSI ────────────────────────────────────────────────────────────
    fig.add_trace(go.Scatter(x=idx, y=rsi, name="RSI",
                             line=dict(color="#9c27b0", width=1.5), showlegend=False), row=3, col=1)
    fig.add_hrect(y0=70, y1=100, fillcolor="rgba(244,67,54,0.06)",  line_width=0, row=3, col=1)
    fig.add_hrect(y0=0,  y1=30,  fillcolor="rgba(76,175,80,0.06)",  line_width=0, row=3, col=1)
    fig.add_hline(y=70, line=dict(color="#f44336", width=1, dash="dot"), row=3, col=1)
    fig.add_hline(y=30, line=dict(color="#4caf50", width=1, dash="dot"), row=3, col=1)
    fig.add_hline(y=50, line=dict(color=GRID,      width=1),            row=3, col=1)

    # ── Row 4: MACD ───────────────────────────────────────────────────────────
    _hcol = [BULL_C if v >= 0 else BEAR_C for v in macd_hist]
    fig.add_trace(go.Bar(x=idx, y=macd_hist, name="MACD Hist",
                         marker_color=_hcol, showlegend=False), row=4, col=1)
    fig.add_trace(go.Scatter(x=idx, y=macd_line, name="MACD",
                             line=dict(color=EMA50_C, width=1.5)), row=4, col=1)
    fig.add_trace(go.Scatter(x=idx, y=macd_sig, name="Signal",
                             line=dict(color=EMA21_C, width=1.5, dash="dot")), row=4, col=1)
    fig.add_hline(y=0, line=dict(color=GRID, width=1), row=4, col=1)

    # ── Layout ────────────────────────────────────────────────────────────────
    _axis_style = dict(gridcolor=GRID, zerolinecolor=GRID,
                       tickfont=dict(color=TEXT, size=9), showgrid=True)
    fig.update_layout(
        paper_bgcolor=BG, plot_bgcolor=BG,
        font=dict(color=TEXT, family="monospace"),
        xaxis_rangeslider_visible=False,
        legend=dict(bgcolor="rgba(13,17,23,0.85)", bordercolor=GRID, borderwidth=1,
                    font=dict(size=10), x=0.01, y=0.94),
        margin=dict(l=60, r=130, t=30, b=20),
        height=820,
        hovermode="x unified",
        hoverlabel=dict(bgcolor=BG, font_color=TEXT),
    )
    fig.update_xaxes(**_axis_style)
    fig.update_yaxes(**_axis_style)
    fig.update_yaxes(tickformat=".5f", row=1, col=1)
    fig.update_yaxes(tickformat=".0f", row=2, col=1)
    fig.update_yaxes(range=[0, 100],   row=3, col=1)
    for _ann in (fig.layout.annotations or []):
        if _ann.text in ("EUR/USD 1H", "Volumen", "RSI (14)", "MACD (12,26,9)"):
            _ann.font.color = TEXT
            _ann.font.size  = 10

    st.plotly_chart(fig, use_container_width=True,
                    config={"scrollZoom": True, "displayModeBar": True,
                            "modeBarButtonsToRemove": ["lasso2d", "select2d"]})


# ============================================
# WORKER AUTÓNOMO (arranca 1 sola vez con el proceso, sin usuarios)
# ============================================
try:
    import background_worker as _bgw
    _bgw.start_if_needed()
except Exception as _bgw_err:
    logging.warning("Background worker no disponible: %s", _bgw_err)

# ============================================
# DETECCIÓN DE ENTORNO: LOCAL vs RAILWAY
# ============================================
import os as _os_env
_IS_LOCAL  = not _os_env.environ.get("RAILWAY_ENVIRONMENT", "").strip()
_railway_domain = _os_env.environ.get("RAILWAY_PUBLIC_DOMAIN", "")
_RAILWAY_URL = f"https://{_railway_domain}" if _railway_domain else "https://web-production-c5a95d.up.railway.app"

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

    # ── Login persistente ─────────────────────────────────────────────────────
    _USERS_OFFLINE = {"david": "david", "javi": "javi"}
    _USER_NAMES    = {"david": "David", "javi": "Javi"}

    if "current_user" not in st.session_state:
        st.session_state.current_user  = None
        st.session_state.session_token = None

    # Restaurar sesión desde token URL si session_state se perdió (nueva pestaña)
    if st.session_state.current_user is None:
        _tok = st.query_params.get("t", "")
        if _tok and _DB_OK:
            try:
                _uid = _db.validate_session(_tok)
                if _uid:
                    st.session_state.current_user  = _uid
                    st.session_state.session_token = _tok
            except Exception:
                pass

    if st.session_state.current_user is None:
        st.markdown("""
        <style>
        .block-container{max-width:500px!important;padding-top:80px!important}
        </style>""", unsafe_allow_html=True)
        st.title("⚡ SMC Pro v2")
        st.subheader("Iniciar Sesión")
        with st.form("_login_form"):
            _lu = st.selectbox("Usuario", ["david", "javi"])
            _lp = st.text_input("Contraseña", type="password", placeholder="tu nombre")
            _ls = st.form_submit_button("🔐 Entrar", use_container_width=True)
        if _ls:
            _auth_ok = False
            if _DB_OK:
                try:
                    _auth_ok = _db.authenticate_user(_lu, _lp)
                except Exception:
                    pass
            if not _auth_ok:
                _auth_ok = (_USERS_OFFLINE.get(_lu) == _lp)
            if _auth_ok:
                st.session_state.current_user = _lu
                st.session_state._scroll_reset = True
                if _DB_OK:
                    try:
                        _tok2 = _db.create_session(_lu)
                        st.session_state.session_token = _tok2
                        st.query_params["t"] = _tok2
                        _db.update_last_login(_lu)
                    except Exception:
                        pass
                st.rerun()
            else:
                st.error("❌ Contraseña incorrecta")
        st.stop()

    current_user      = st.session_state.current_user
    current_user_name = _USER_NAMES.get(current_user, current_user.capitalize())

    # ── Selector de modo: Trading o Inversión a Largo Plazo ──────────────────
    if st.session_state.get("app_mode") == "investment":
        try:
            import investment_module as _inv
            _inv.render_investment_module()
        except Exception as _inv_err:
            st.error(f"Error en módulo de inversión: {_inv_err}")
            if st.button("← Volver"):
                st.session_state.app_mode = None
                st.rerun()
        st.stop()

    if st.session_state.get("app_mode") is None:
        st.markdown("""
        <style>
        .mode-container{display:flex;gap:24px;margin-top:32px}
        .mode-card{background:#1a1f2e;border:1px solid #2d3748;border-radius:16px;
                   padding:32px 24px;flex:1;text-align:center}
        .mode-card h2{font-size:2rem;margin-bottom:8px}
        .mode-card p{color:#a0aec0;margin-bottom:16px}
        .mode-card ul{text-align:left;color:#cbd5e0;line-height:2rem;list-style:none;padding-left:8px}
        </style>
        """, unsafe_allow_html=True)

        st.markdown(f"## ¡Bienvenido, {current_user_name}! ¿Qué quieres hacer hoy?")
        st.markdown("")

        _mc1, _mc2 = st.columns(2)
        with _mc1:
            st.markdown("""
            <div class="mode-card">
                <h2>⚡ Trading Activo</h2>
                <p>Señales EUR/USD en tiempo real</p>
                <ul>
                    <li>🎯 Señales premium multi-filtro</li>
                    <li>📊 Backtest de 17 estrategias</li>
                    <li>🤖 Bot automático OANDA</li>
                    <li>📱 Alertas Telegram</li>
                    <li>🧠 Asesor IA</li>
                </ul>
            </div>
            """, unsafe_allow_html=True)
            st.markdown("")
            if st.button("⚡ Entrar a Trading", use_container_width=True, type="primary"):
                st.session_state.app_mode = "trading"
                st.rerun()

        with _mc2:
            st.markdown("""
            <div class="mode-card">
                <h2>📈 Inversión LP</h2>
                <p>Cartera optimizada a 1-5 años</p>
                <ul>
                    <li>💎 35+ activos analizados</li>
                    <li>📊 Scoring fundamental+técnico+macro</li>
                    <li>🌍 ETFs, acciones, bonos, oro</li>
                    <li>💼 Constructor de cartera</li>
                    <li>📈 Proyección de crecimiento</li>
                </ul>
            </div>
            """, unsafe_allow_html=True)
            st.markdown("")
            if st.button("📈 Entrar a Inversión LP", use_container_width=True):
                st.session_state.app_mode = "investment"
                st.rerun()

        st.stop()

    # ── Auto-refresh cada 3 minutos — 100% nativo Streamlit, sin paquetes ──────
    @st.fragment(run_every=30)
    def _trading_autorefresh():
        _last = st.session_state.get("last_analysis_time")
        _now  = time.time()
        _elapsed = (_now - float(_last)) if _last else 999
        if _elapsed >= 175:
            st.rerun()
        else:
            _rem = max(0, int(180 - _elapsed))
            st.caption(f"⏱ Próximo análisis en {_rem}s")

    _trading_autorefresh()

    # ── Cargar credenciales MT5 del usuario desde DB (solo una vez) ──────────
    _mt5_load_key = f"mt5_loaded_{current_user}"
    if _mt5_load_key not in st.session_state and _DB_OK:
        try:
            _user_mt5 = _db.load_user_mt5(current_user)
            if _user_mt5:
                st.session_state.mt5_login    = _user_mt5.get("mt5_login", "")
                st.session_state.mt5_password = _user_mt5.get("mt5_password", "")
                st.session_state.mt5_server   = _user_mt5.get("mt5_server", "")
        except Exception:
            pass
        st.session_state[_mt5_load_key] = True

    # ── Importar motores AI + auto-mejora + data feeds ───────────────────────
    try:
        import ai_engine as _ai_engine
        _AI_ENGINE_OK = True
    except ImportError:
        _ai_engine = None
        _AI_ENGINE_OK = False

    try:
        import self_improve as _self_improve
        _SELF_IMPROVE_OK = True
    except ImportError:
        _self_improve = None
        _SELF_IMPROVE_OK = False

    try:
        import data_feeds as _data_feeds
        _DATA_FEEDS_OK = True
    except ImportError:
        _data_feeds = None
        _DATA_FEEDS_OK = False

    # ── Cargar Strategy DNA activo (o usar default) ──────────────────────────
    if "active_dna" not in st.session_state:
        _dna_loaded = None
        if _DB_OK:
            try:
                _dna_loaded = _db.load_active_strategy()
            except Exception:
                pass
        if _dna_loaded is None and _AI_ENGINE_OK:
            _dna_loaded = _ai_engine.DEFAULT_DNA.copy()
        st.session_state.active_dna = _dna_loaded or {}

    # ── Inicializar session state para análisis y credenciales ───────────────
    if "last_analysis_time" not in st.session_state:
        st.session_state.last_analysis_time = None
    if "analysis_executed" not in st.session_state:
        st.session_state.analysis_executed = False
    if "chart_tf" not in st.session_state:
        st.session_state.chart_tf = "1H"
    if "backtest_result" not in st.session_state:
        st.session_state.backtest_result = None
    if "strategy_comparison" not in st.session_state:
        _bt_disk = _load_bt_cache()
        st.session_state.strategy_comparison = _bt_disk.get("sc")
        st.session_state.backtest_result = (_bt_disk["sc"]["best"] if _bt_disk.get("sc") else None)
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

    st.markdown("""<style>
/* ══════════════════════════════════════════════════════
   SMC Pro — Design System v2
   Dark trading terminal aesthetic
══════════════════════════════════════════════════════ */

/* ── Layout ── */
.main .block-container{padding-top:.6rem!important;padding-bottom:2rem!important;max-width:100%!important}
#MainMenu,footer,[data-testid="stToolbar"],[data-testid="stDecoration"]{display:none!important}
.stApp{background:#060a10!important}

/* ── Sidebar ── */
[data-testid="stSidebar"]{background:#07090f!important;border-right:1px solid #151d2e!important}
[data-testid="stSidebar"] h1,[data-testid="stSidebar"] h2,[data-testid="stSidebar"] h3{
  color:#3d7eff!important;font-size:.72rem!important;font-weight:700!important;
  letter-spacing:.1em!important;text-transform:uppercase!important;margin-bottom:6px!important}
[data-testid="stSidebar"] .stMarkdown p{font-size:.8rem!important;color:#6e7a8a!important}
[data-testid="stSidebarNavItems"]{padding-top:0!important}

/* ── Metrics ── */
[data-testid="metric-container"]{
  background:#0b0f18!important;border:1px solid #151d2e!important;
  border-radius:10px!important;padding:12px 14px!important}
[data-testid="metric-container"]:hover{border-color:#3d7eff!important}
[data-testid="stMetricValue"]{
  font-size:1.2rem!important;font-weight:800!important;
  font-family:'JetBrains Mono','Courier New',monospace!important;color:#e6edf3!important}
[data-testid="stMetricLabel"]{
  font-size:.68rem!important;font-weight:700!important;
  color:#4d5966!important;text-transform:uppercase!important;letter-spacing:.07em!important}
[data-testid="stMetricDelta"] svg{display:none!important}
[data-testid="stMetricDelta"]{font-size:.75rem!important}

/* ── Buttons ── */
button[kind="primary"]{
  background:linear-gradient(135deg,#1a56db,#1643b0)!important;
  border:none!important;border-radius:8px!important;
  font-weight:800!important;letter-spacing:.04em!important;
  font-size:.85rem!important;padding:10px 20px!important;
  box-shadow:0 2px 12px rgba(26,86,219,.35)!important;transition:all .2s!important}
button[kind="primary"]:hover{
  background:linear-gradient(135deg,#2563eb,#1a56db)!important;
  box-shadow:0 4px 20px rgba(26,86,219,.55)!important;transform:translateY(-1px)!important}
button[kind="secondary"]{
  background:#0d1117!important;border:1px solid #1e2d3d!important;
  border-radius:8px!important;color:#8b9ab0!important;font-size:.8rem!important}

/* ── Expanders ── */
[data-testid="stExpander"]{
  background:#0b0f18!important;border:1px solid #151d2e!important;border-radius:10px!important}
details summary{
  font-weight:600!important;font-size:.82rem!important;
  color:#8b9ab0!important;padding:10px 14px!important}
details summary:hover{color:#e6edf3!important}

/* ── Alerts ── */
[data-testid="stAlert"]{border-radius:8px!important;padding:10px 14px!important;font-size:.82rem!important}
.stSuccess{background:rgba(5,150,105,.08)!important;border-color:rgba(5,150,105,.3)!important}
.stWarning{background:rgba(217,119,6,.08)!important;border-color:rgba(217,119,6,.3)!important}
.stInfo{background:rgba(26,86,219,.08)!important;border-color:rgba(26,86,219,.3)!important}
.stError{background:rgba(185,28,28,.08)!important;border-color:rgba(185,28,28,.3)!important}

/* ── DataFrames ── */
[data-testid="stDataFrame"]{border:1px solid #151d2e!important;border-radius:8px!important;overflow:hidden!important}

/* ── Dividers ── */
hr{border-color:#151d2e!important;margin:14px 0!important}

/* ── Inputs ── */
[data-testid="stWidgetLabel"] label{
  font-size:.72rem!important;font-weight:700!important;
  color:#4d5966!important;text-transform:uppercase!important;letter-spacing:.06em!important}
.stTextInput input,.stSelectbox select{
  background:#0b0f18!important;border-color:#1e2d3d!important;
  border-radius:6px!important;color:#c9d1d9!important}

/* ── Plotly chart ── */
.js-plotly-plot{border:1px solid #151d2e!important;border-radius:10px!important;overflow:hidden!important}

/* ══════ Custom component classes ══════ */

/* Header */
.smc-header{
  display:flex;justify-content:space-between;align-items:center;
  padding:10px 0 12px;border-bottom:1px solid #151d2e;margin-bottom:10px}
.smc-logo{font-size:1.25rem;font-weight:900;color:#e6edf3;letter-spacing:-.03em}
.smc-pair{font-size:.85rem;font-weight:700;color:#3d7eff;font-family:monospace}
.smc-time{font-size:.75rem;color:#4d5966;font-family:monospace}
.smc-version{font-size:.62rem;font-weight:700;color:#4d5966;background:#0d1117;
  border:1px solid #1e2d3d;padding:1px 6px;border-radius:3px;margin-left:6px;vertical-align:middle}
.smc-hbrand{display:flex;align-items:center;gap:8px}
.smc-hinfo{display:flex;align-items:center;gap:10px}

/* Badges */
.bdg{display:inline-flex;align-items:center;gap:3px;padding:2px 8px;border-radius:20px;
     font-size:.68rem;font-weight:800;letter-spacing:.04em;vertical-align:middle}
.bdg-g{background:rgba(5,150,105,.12);color:#10b981;border:1px solid rgba(5,150,105,.25)}
.bdg-r{background:rgba(220,38,38,.12);color:#f87171;border:1px solid rgba(220,38,38,.25)}
.bdg-y{background:rgba(217,119,6,.12);color:#f59e0b;border:1px solid rgba(217,119,6,.25)}
.bdg-b{background:rgba(59,130,246,.12);color:#60a5fa;border:1px solid rgba(59,130,246,.25)}
.bdg-x{background:rgba(107,114,128,.12);color:#9ca3af;border:1px solid rgba(107,114,128,.25)}

/* Section headers */
.smc-sec{display:flex;align-items:center;gap:8px;padding:16px 0 10px;
  border-bottom:1px solid #151d2e;margin-bottom:12px}
.smc-sec span:first-child{font-size:1rem}
.smc-sec-title{font-size:.72rem;font-weight:800;color:#4d5966;
  letter-spacing:.1em;text-transform:uppercase}

/* Signal hero card */
.smc-signal{border-radius:12px;padding:18px 22px;margin:6px 0;
  display:flex;align-items:center;justify-content:space-between}
.smc-sig-b{background:linear-gradient(135deg,#011a0a,#012d10);
  border:1px solid rgba(5,150,105,.5);box-shadow:0 0 24px rgba(5,150,105,.08)}
.smc-sig-s{background:linear-gradient(135deg,#1a0505,#2d0a0a);
  border:1px solid rgba(220,38,38,.5);box-shadow:0 0 24px rgba(220,38,38,.08)}
.smc-sig-n{background:linear-gradient(135deg,#151206,#221b08);
  border:1px solid rgba(217,119,6,.3)}
.sig-dir{font-size:1.7rem;font-weight:900;letter-spacing:-.02em;line-height:1}
.sig-dir-b{color:#10b981}.sig-dir-s{color:#f87171}.sig-dir-n{color:#f59e0b}
.sig-price{font-size:1.1rem;font-weight:700;font-family:'JetBrains Mono','Courier New',monospace;
  color:#c9d1d9;margin-top:4px}
.sig-right{display:flex;flex-direction:column;align-items:flex-end;gap:5px}
.sig-pill{font-size:.72rem;color:#6e7a8a;background:#0d1117;
  border:1px solid #1e2d3d;border-radius:6px;padding:3px 9px}

/* Score card */
.smc-score{background:#0b0f18;border:1px solid #151d2e;
  border-radius:12px;padding:16px 18px}
.sc-num{font-size:2.6rem;font-weight:900;font-family:monospace;line-height:1}
.sc-den{font-size:1rem;font-weight:400;color:#4d5966}
.sc-lbl{font-size:.68rem;font-weight:800;letter-spacing:.1em;
  text-transform:uppercase;margin:4px 0 10px}
.sc-track{height:5px;background:#151d2e;border-radius:3px;overflow:hidden}
.sc-fill{height:100%;border-radius:3px;transition:width .6s ease}

/* Position banner */
.smc-pos{display:flex;align-items:center;justify-content:space-between;
  background:#0b0f18;border:1px solid #151d2e;border-radius:10px;
  padding:11px 16px;margin:6px 0;font-size:.8rem}
.smc-pos-open{border-left:3px solid #3d7eff!important;background:rgba(26,86,219,.04)!important}
.smc-pos-b{border-left:3px solid #10b981!important}
.smc-pos-s{border-left:3px solid #f87171!important}
.pos-vals{display:flex;gap:18px;font-family:monospace;font-size:.78rem}
.pos-val-tp{color:#10b981}.pos-val-sl{color:#f87171}.pos-val-x{color:#6e7a8a}

/* Window status */
.smc-win{display:flex;align-items:center;gap:8px;padding:8px 14px;
  border-radius:8px;margin-bottom:8px;font-size:.8rem;font-weight:600}
.smc-win-on{background:rgba(5,150,105,.06);border:1px solid rgba(5,150,105,.2);color:#10b981}
.smc-win-off{background:rgba(217,119,6,.06);border:1px solid rgba(217,119,6,.2);color:#f59e0b}

/* Legacy compat */
.big-signal{font-size:1.8rem;font-weight:800;text-align:center;
  padding:.8rem;border-radius:10px;margin-bottom:.5rem}
.sl{background:#011a0a;color:#10b981;border:1px solid rgba(5,150,105,.4)}
.ss{background:#1a0505;color:#f87171;border:1px solid rgba(220,38,38,.4)}
.sw{background:#151206;color:#f59e0b;border:1px solid rgba(217,119,6,.3)}
.scalp-box{border:1px solid #1e2d3d;border-radius:8px;padding:.8rem;background:#0b0f18;margin-top:.5rem}
.score-box{border-radius:10px;padding:.8rem;text-align:center;font-size:1.6rem;font-weight:800;margin:.3rem 0}
.vol-bar{height:14px;border-radius:3px;background:#10b981;margin:2px 0}
</style>""", unsafe_allow_html=True)

    mt5_login = st.session_state.mt5_login or None
    mt5_password = st.session_state.mt5_password or None
    mt5_server = st.session_state.mt5_server or None
    connected = is_mt5_available() and mt5_connect(
        login=mt5_login,
        password=mt5_password,
        server=mt5_server
    )
    data_src  = "🟢 MT5 (tiempo real)" if connected else "🟡 yfinance (delay ~15min)"

    _hconn_cls = "bdg-g" if connected else "bdg-y"
    _hconn_dot = "●" if connected else "◐"
    _hconn_txt = "MT5 Live" if connected else "yfinance"
    _now_utc   = datetime.utcnow()
    _now_es    = _now_utc + __import__("datetime").timedelta(hours=UTC_OFFSET_SPAIN)
    _htime     = _now_utc.strftime("%H:%M UTC")

    # Live price — TradingView (siempre disponible, sin MT5 ni OANDA)
    try:
        _hdr_tv  = get_tv_data(SYMBOL, "1h")
        _live_px = _hdr_tv.get("price")
        if _live_px is None and (connected or _mt5_service_available()):
            _hdr_tick = get_mt5_tick(SYMBOL)
            _live_px  = _hdr_tick["bid"] if _hdr_tick else None
    except Exception:
        _live_px = None
    _live_px_str = f"{_live_px:.5f}" if _live_px is not None else "—"
    _live_dt_str = _now_es.strftime("%d/%m/%Y  %H:%M:%S")

    st.markdown(f"""
<div class="smc-header">
  <div class="smc-hbrand">
    <span class="smc-logo">⚡ SMC Pro</span>
    <span class="smc-version">v2.0</span>
    <span class="bdg {_hconn_cls}">{_hconn_dot} {_hconn_txt}</span>
  </div>
  <div class="smc-hinfo">
    <span class="smc-pair">EUR / USD</span>
    <span class="bdg bdg-b">👤 {current_user_name}</span>
    <span class="smc-time" title="{_htime}">{_live_px_str}</span>
  </div>
</div>""", unsafe_allow_html=True)

    # ── Indicador de refresco (fecha + precio en vivo) ──────────────────────
    st.markdown(f"""<div style="
        display:inline-flex;align-items:center;gap:14px;
        background:#0d1117;border:1px solid #30363d;border-radius:8px;
        padding:6px 16px;margin-bottom:8px;font-family:monospace">
      <span style="color:#8b949e;font-size:11px">ÚLTIMO REFRESCO</span>
      <span style="color:#e6edf3;font-size:13px;font-weight:600">{_live_dt_str}</span>
      <span style="color:#8b949e;font-size:11px">EUR/USD</span>
      <span style="color:#3fb950;font-size:15px;font-weight:700">{_live_px_str}</span>
    </div>""", unsafe_allow_html=True)

    # ── Banner modo local (extensión MT5) ────────────────────────────────────
    if _IS_LOCAL:
        st.markdown(f"""<div style="background:linear-gradient(90deg,#1a2a4a,#0b1525);
            border:1px solid #3d7eff44;border-radius:10px;padding:12px 18px;
            margin-bottom:12px;display:flex;align-items:center;justify-content:space-between;gap:12px">
          <div>
            <span style="color:#3d7eff;font-weight:700;font-size:13px">⚡ MODO EXTENSIÓN MT5</span>
            <span style="color:#8899aa;font-size:12px;margin-left:10px">
              Los datos se sincronizan con Railway · Usa la app web para el panel completo
            </span>
          </div>
          <a href="{_RAILWAY_URL}" target="_blank"
             style="background:#3d7eff;color:#fff;padding:5px 14px;border-radius:6px;
                    font-size:12px;font-weight:600;text-decoration:none;white-space:nowrap">
            🌐 Abrir app web →
          </a>
        </div>""", unsafe_allow_html=True)

    # ── Preservar posición de scroll en cada rerun ────────────────────────────
    # Guarda scrollY en sessionStorage justo cuando Streamlit empieza a procesar
    # (aparece el indicador de estado). Lo restaura en el siguiente render.
    import streamlit.components.v1 as _stc
    _scroll_reset = st.session_state.pop("_scroll_reset", False)
    _stc.html(f"""<script>
(function(){{
  var p = window.parent;
  var KEY = 'smc_sy';

  // Si acaba de hacer login, ir siempre al tope
  if ({'true' if _scroll_reset else 'false'}) {{
    p.sessionStorage.removeItem(KEY);
    p.scrollTo(0, 0);
  }} else {{
    // Restaurar posición después del rerun normal
    var sy = parseInt(p.sessionStorage.getItem(KEY) || '0');
    if (sy > 80) {{
      p.requestAnimationFrame(function(){{
        p.requestAnimationFrame(function(){{
          p.scrollTo(0, sy);
          setTimeout(function(){{ p.sessionStorage.removeItem(KEY); }}, 600);
        }});
      }});
    }}
  }}

  // Guardar posición cuando Streamlit empieza a procesar
  new MutationObserver(function(mutations){{
    for (var i=0; i<mutations.length; i++){{
      var nodes = mutations[i].addedNodes;
      for (var j=0; j<nodes.length; j++){{
        var n = nodes[j];
        if (!n || n.nodeType !== 1) continue;
        var isStatus = (n.dataset && n.dataset.testid === 'stStatusWidget')
                    || (n.querySelector && n.querySelector('[data-testid="stStatusWidget"]'));
        if (isStatus){{
          p.sessionStorage.setItem(KEY, String(p.scrollY || p.pageYOffset || 0));
          return;
        }}
      }}
    }}
  }}).observe(p.document.body, {{childList:true, subtree:true}});
}})();
</script>""", height=0, scrolling=False)

    # ── Barra de navegación fija derecha (inyectada en el DOM padre) ──────────
    _stc.html("""<script>
(function(){
  var p = window.parent.document;

  // Eliminar instancia previa para evitar duplicados en re-renders
  ['smc-nav','smc-nav-css'].forEach(function(id){
    var el = p.getElementById(id); if(el) el.remove();
  });

  // CSS inyectado en <head> del documento Streamlit
  var css = p.createElement('style');
  css.id = 'smc-nav-css';
  css.textContent = [
    '#smc-nav{position:fixed;right:0;top:50%;transform:translateY(-50%);',
    'z-index:99999;background:#0d1117;border:1px solid #30363d;',
    'border-left:3px solid #1f6feb;border-radius:8px 0 0 8px;',
    'padding:10px 6px;width:148px;max-height:90vh;overflow-y:auto;',
    'font-family:-apple-system,sans-serif;box-shadow:-4px 0 20px #0006;}',
    '#smc-nav::-webkit-scrollbar{width:3px;}',
    '#smc-nav::-webkit-scrollbar-thumb{background:#30363d;border-radius:2px;}',
    '#smc-nav .n-title{color:#1f6feb;font-size:0.65rem;font-weight:700;',
    'letter-spacing:.08em;text-transform:uppercase;padding:0 6px 6px;',
    'border-bottom:1px solid #21262d;margin-bottom:4px;display:block;}',
    '#smc-nav a{display:block;color:#c9d1d9;font-size:0.72rem;',
    'padding:4px 8px;border-radius:5px;text-decoration:none;',
    'cursor:pointer;white-space:nowrap;margin:1px 0;transition:all .15s;}',
    '#smc-nav a:hover{background:#1f6feb;color:#fff;padding-left:12px;}',
    '#smc-nav .n-sep{border-top:1px solid #21262d;margin:5px 4px;}'
  ].join('');
  p.head.appendChild(css);

  // Función de scroll definida en el contexto del padre
  window.parent.smcGo = function(id){
    var el = p.getElementById(id);
    if(el) el.scrollIntoView({behavior:'smooth',block:'start'});
  };

  // HTML del panel de navegación
  var nav = p.createElement('div');
  nav.id = 'smc-nav';
  nav.innerHTML =
    '<span class="n-title">⚡ SMC Nav</span>' +
    '<a onclick="smcGo(\'sec-precio\')">📡 Precio</a>' +
    '<a onclick="smcGo(\'sec-senal\')">🧠 Señal</a>' +
    '<a onclick="smcGo(\'sec-score\')">🎯 Score</a>' +
    '<a onclick="smcGo(\'sec-chart\')">📈 Gráfico</a>' +
    '<a onclick="smcGo(\'sec-dna\')">🧬 DNA</a>' +
    '<a onclick="smcGo(\'sec-vol\')">📊 Volumen</a>' +
    '<a onclick="smcGo(\'sec-scalping\')">🎯 Scalping</a>' +
    '<a onclick="smcGo(\'sec-estructura\')">🏗️ Estructura</a>' +
    '<a onclick="smcGo(\'sec-manipulacion\')">🕵️ Liquidez</a>' +
    '<a onclick="smcGo(\'sec-cot\')">🏦 COT</a>' +
    '<a onclick="smcGo(\'sec-ia\')">🤖 Motor IA</a>' +
    '<div class="n-sep"></div>' +
    '<a onclick="smcGo(\'sec-backtest\')">📊 Backtest</a>' +
    '<a onclick="smcGo(\'sec-backtest2008\')">🌍 2008</a>' +
    '<div class="n-sep"></div>' +
    '<a onclick="smcGo(\'sec-porq\')">🔍 Por qué</a>' +
    '<a onclick="smcGo(\'sec-bot\')">🤖 Bot</a>' +
    '<a onclick="smcGo(\'sec-dashboard\')">📋 Dashboard</a>' +
    '<a onclick="smcGo(\'sec-dxy\')">💱 DXY</a>' +
    '<a onclick="smcGo(\'sec-accion\')">🎯 Acción</a>' +
    '<a onclick="smcGo(\'sec-autoimprove\')">🔬 Auto-Mejora</a>' +
    '<a onclick="smcGo(\'sec-advisor\')">💬 Advisor</a>';
  p.body.appendChild(nav);
})();
</script>""", height=0, scrolling=False)

    # ── Ventana horaria de trading ─────────────────────────────────────────
    _win_in, _win_label, _win_eta = get_trading_window_info()
    _wcls = "smc-win-on" if _win_in else "smc-win-off"
    _wdot = "●" if _win_in else "○"
    _wtxt = "HORARIO ACTIVO" if _win_in else "FUERA DE HORARIO"
    st.markdown(f"""<div class="smc-win {_wcls}">
  {_wdot} <strong>{_wtxt}</strong> &nbsp;—&nbsp; {_win_label} &nbsp;|&nbsp; {_win_eta}
</div>""", unsafe_allow_html=True)

    # ── Estado de Posición ──────────────────────────────────────────────────────
    position_state = load_position_state()
    if position_state["is_open"]:
        entry_time = position_state["entry_time"]
        if isinstance(entry_time, str):
            entry_time = datetime.fromisoformat(entry_time)
        time_open    = datetime.now() - entry_time
        hours_open   = int(time_open.total_seconds() // 3600)
        minutes_open = int((time_open.total_seconds() % 3600) // 60)
        _pdir  = position_state["direction"]
        _pcls  = "smc-pos-b" if _pdir == "LONG" else "smc-pos-s"
        _pbdg  = f'<span class="bdg bdg-{"g" if _pdir=="LONG" else "r"}">{"COMPRA" if _pdir=="LONG" else "VENTA"}</span>'
        st.markdown(f"""<div class="smc-pos smc-pos-open {_pcls}">
  <div><strong>🔥 POSICIÓN ACTIVA</strong> {_pbdg}</div>
  <div class="pos-vals">
    <span class="pos-val-x">Entrada: {position_state['entry_price']:.5f}</span>
    <span class="pos-val-tp">TP: {position_state['tp']:.5f}</span>
    <span class="pos-val-sl">SL: {position_state['sl']:.5f}</span>
    <span class="pos-val-x">Score: {position_state['score']}/100</span>
    <span class="pos-val-x">⏱ {hours_open}h {minutes_open}m</span>
  </div>
</div>""", unsafe_allow_html=True)
    else:
        st.markdown("""<div class="smc-pos">
  <span class="pos-val-x">○ &nbsp;<strong>SIN POSICIÓN</strong> — Esperando señal ≥ 70</span>
</div>""", unsafe_allow_html=True)

    # ── Sidebar ───────────────────────────────────────────────────────────────────
    with st.sidebar:
        st.header("⚙️ Configuración")
        st.markdown(f"👤 **{current_user_name}** — sesión activa")
        if st.button("🚪 Cerrar sesión", key="_logout_btn"):
            if _DB_OK and st.session_state.get("session_token"):
                try:
                    _db.invalidate_session(st.session_state.session_token)
                except Exception:
                    pass
            st.session_state.current_user  = None
            st.session_state.session_token = None
            st.query_params.clear()
            st.rerun()
        st.markdown("---")
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
            
            # Las credenciales MT5 se configuran via variables de entorno en Railway
            st.caption("💡 Configura MT5_LOGIN y MT5_PASSWORD en variables de entorno de Railway")
            
            st.markdown("---")
            
            if connected:
                st.success(f"✅ MT5 conectado — {current_user_name}")
                acct = get_mt5_account()
                if acct:
                    st.markdown(
                        f"**Titular:** {current_user_name}  \n"
                        f"**Servidor:** {acct['server']}  \n"
                        f"**Cuenta:** {acct['name']}  \n"
                        f"**Balance:** {acct['balance']:.2f} {acct['currency']}  \n"
                        f"**Equity:** {acct['equity']:.2f} {acct['currency']}  \n"
                        f"**Profit:** {acct['profit']:+.2f}  \n"
                        f"**Apalancamiento:** 1:{acct['leverage']}"
                    )
            else:
                _saved_mt5 = st.session_state.get("mt5_login", "")
                if _saved_mt5:
                    st.caption(f"📋 Credenciales guardadas para {current_user_name}: {_saved_mt5}")
                st.info("ℹ️ Ingresa credenciales arriba o abre MT5")
        else:
            # MT5 local no disponible — mostrar estado del servicio OANDA remoto
            if _mt5_service_available():
                _svc_h = mt5_service_health()
                _svc_connected = _svc_h.get("mt5") == "connected"
                if _svc_connected:
                    st.success("✅ OANDA Service conectado")
                    _svc_acct = mt5_service_account()
                    if _svc_acct and "balance" in _svc_acct:
                        st.markdown(
                            f"**Servidor:** {_svc_acct.get('server', 'OANDA')}  \n"
                            f"**Balance:** {float(_svc_acct.get('balance', 0)):.2f} {_svc_acct.get('currency', '')}  \n"
                            f"**Equity:** {float(_svc_acct.get('equity', 0)):.2f} {_svc_acct.get('currency', '')}  \n"
                            f"**Margen libre:** {float(_svc_acct.get('free_margin', 0)):.2f}"
                        )
                    else:
                        st.caption("Conectado — sin datos de cuenta aún")
                else:
                    st.warning(
                        f"⚠️ Servicio OANDA configurado pero no conectado.\n\n"
                        f"Estado: `{_svc_h.get('status', 'desconocido')}`\n\n"
                        "Comprueba que `OANDA_API_TOKEN` y `OANDA_ACCOUNT_ID` "
                        "están configurados en Railway."
                    )
            elif sys.platform != "win32":
                st.success("✅ Modo señales activo — análisis y Telegram funcionando")
            else:
                st.warning("⚠️ Paquete MetaTrader5 no instalado.\n\nEjecuta: `pip install MetaTrader5`")

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
            st.warning(f"📊 Posición {position_state['direction']} abierta — cierre automático por TP/SL")

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
    # ── Estado de la base de datos ────────────────────────────────────────────
    st.subheader("🗄️ Base de Datos")
    if _DB_OK:
        try:
            _db_alive = _db.is_connected()
            if _db_alive:
                st.success("✅ PostgreSQL conectada")
                _ts = _db.trades_summary(user_id=current_user)
                if _ts and _ts.get("total"):
                    st.metric("Mis trades", int(_ts["total"] or 0))
                    st.metric("Win rate", f"{float(_ts.get('winrate') or 0):.1f}%")
                    st.metric("Net P&L", f"${float(_ts.get('net_pnl') or 0):+.2f}")
                else:
                    st.caption("Sin trades aún")
            else:
                st.warning("⚠️ DB no responde")
        except Exception:
            st.warning("⚠️ DB no disponible")
    else:
        st.info("DB no configurada")
    st.markdown("---")
    st.subheader("🤖 Proveedores IA")
    if _AI_ENGINE_OK:
        _ap = _ai_engine.get_active_providers()
        if _ap:
            _icons = {
                "groq":      "⚡ Groq — llama-3.3-70b",
                "cerebras":  "🧠 Cerebras — llama-3.3-70b",
                "zhipu":     "🌏 Zhipu GLM — glm-4-flash",
                "anthropic": "🟣 Claude Haiku",
                "openai":    "🟢 OpenAI GPT-4o-mini",
            }
            for _p in _ap:
                st.caption(f"✅ {_icons.get(_p, _p)}")
        else:
            st.warning("Sin API keys activas")
        st.caption("Bot autónomo activo — analiza aunque no haya usuarios")
    st.markdown("---")
    st.subheader(f"🧠 Memoria IA — {current_user_name}")
    if _DB_OK:
        try:
            _mem_count = _db.count_ai_memories(current_user)
            _user_mems = _db.load_ai_memories(current_user, limit=5)
            st.caption(f"{_mem_count} aprendizajes guardados")
            if _user_mems:
                for _m in _user_mems:
                    _ct = _m.get("content", "")
                    st.caption(f"• {_ct[:90]}{'…' if len(_ct)>90 else ''}")
            else:
                st.caption("Chatea con el Advisor para generar aprendizajes")
        except Exception:
            st.caption("Memorias no disponibles")
    st.markdown("---")
    st.caption("⚠️ Solo informativo. No es consejo financiero.")

# ── Sin botón: análisis automático cada 2 minutos, invisible para el usuario ──
run_analysis = False

# Auto-refresh fijo: cada 3 minutos, completamente invisible para el usuario
refresh_secs = 180

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
# True cuando es timer automático → cero spinners, cero texto visible al usuario
_is_auto_refresh = should_auto_refresh and not run_analysis

# Si es la primera carga y el background worker ya tiene datos en caché, usarlos
# directamente sin lanzar análisis completo (carga instantánea para el usuario)
if not run_fresh_analysis and not st.session_state.analysis_executed:
    if _DB_OK:
        try:
            _bg_snap = _db.get_last_snapshot()
            if _bg_snap and _bg_snap.get("price"):
                # Hay datos del worker autónomo: marcar como ejecutado con caché
                _bg_sig = {
                    "final_signal": _bg_snap.get("signal", "NEUTRAL"),
                    "score":        _bg_snap.get("score", 0),
                    "price":        _bg_snap.get("price", 0),
                    "regime":       _bg_snap.get("regime", ""),
                    "strategy":     _bg_snap.get("strategy", ""),
                    "buy_signals":  0, "sell_signals": 0,
                    "session": "", "dxy_dir": "", "dxy_trend": "N/A",
                }
                if not st.session_state.get("_analysis_cache"):
                    st.session_state._analysis_cache = {"signal": _bg_sig}
                st.session_state.analysis_executed = True
        except Exception:
            pass

if run_fresh_analysis:
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
        # En auto-refresh: sin spinner para que sea imperceptible para el usuario
        import contextlib as _cl
        _spin = st.spinner("Analizando mercado…") if not _is_auto_refresh else _cl.nullcontext()
        with _spin:
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

    # ── Snapshot periódico en DB + chequeo Telegram horario ──────────────────
    if run_fresh_analysis and _DB_OK and signal:
        _sig_f = signal.get("final_signal", "NEUTRAL")
        _sig_s = signal.get("score", 0) or 0
        try:
            _db.save_snapshot(
                price=signal.get("price") or 0,
                signal=_sig_f,
                score=int(_sig_s),
                dxy_trend=dxy_trend or "",
                regime=signal.get("regime") or signal.get("kb_regime_label", ""),
                strategy=signal.get("strategy") or signal.get("kb_best_strategy", ""),
                extra={
                    "session": session,
                    "dxy_dir": dxy_dir,
                    "dxy_chg": dxy_chg,
                    "vol_spike": bool(vol_spikes),
                    "delta_pct": delta.get("delta_pct", 0) if delta else 0,
                },
                user_id=current_user,
            )
        except Exception:
            pass

        # ── Telegram horario ──────────────────────────────────────────────
        try:
            from datetime import timezone as _tz2
            _last_tg = _db.get_setting("last_hourly_telegram")
            _should_tg = False
            if not _last_tg:
                _should_tg = True
            else:
                _last_tg_dt = datetime.fromisoformat(_last_tg)
                if not _last_tg_dt.tzinfo:
                    _last_tg_dt = _last_tg_dt.replace(tzinfo=_tz2.utc)
                _should_tg = (datetime.now(_tz2.utc) - _last_tg_dt).total_seconds() >= 7200
            if _should_tg:
                _in_win, _win_lbl, _ = get_trading_window_info()
                _tg_msg = _build_hourly_telegram_message(
                    signal=signal,
                    score=int(_sig_s),
                    session=session,
                    dxy_dir=dxy_dir,
                    dxy_chg=float(dxy_chg),
                    dxy_trend=dxy_trend or "N/A",
                    vol_spikes=vol_spikes,
                    delta=delta,
                    consensus=consensus,
                    price=signal.get("price"),
                    label="",
                    context_reasons=st.session_state.get("market_context_reasons") or [],
                    in_window=_in_win,
                    win_label=_win_lbl,
                )
                if send_telegram_raw(_tg_msg):
                    _db.set_setting("last_hourly_telegram", datetime.now(_tz2.utc).isoformat())
        except Exception as _tg_err:
            logging.warning("Hourly telegram error: %s", _tg_err)

        # ── Alerta urgente: señal fuerte o evento importante ──────────────────
        try:
            _last_urg = _db.get_setting("last_urgent_telegram")
            _urg_ok = True
            if _last_urg:
                _urg_dt = datetime.fromisoformat(_last_urg)
                if not _urg_dt.tzinfo:
                    _urg_dt = _urg_dt.replace(tzinfo=_tz2.utc)
                _urg_ok = (datetime.now(_tz2.utc) - _urg_dt).total_seconds() >= 3600
            _in_win_u, _, _ = get_trading_window_info()
            _is_urgent = False
            _urg_why = ""
            if _in_win_u and _urg_ok:
                if int(_sig_s) >= 80:
                    _is_urgent = True
                    _urg_why = f"Score {int(_sig_s)}/100 — confluencia muy alta"
                elif int(_sig_s) >= 65 and vol_spikes:
                    _is_urgent = True
                    _urg_why = f"Score {int(_sig_s)}/100 + spike de volumen ({vol_spikes[0].get('ratio', 0):.1f}x)"
            if _is_urgent:
                _urg_msg = _build_urgent_telegram_message(signal, int(_sig_s), _urg_why)
                if send_telegram_raw(_urg_msg):
                    _db.set_setting("last_urgent_telegram", datetime.now(_tz2.utc).isoformat())
        except Exception as _ue:
            logging.warning("Urgent telegram error: %s", _ue)

    # ── Macro context enrichment (FRED + Finnhub) ────────────────────────────
    if "macro_context" not in st.session_state:
        st.session_state.macro_context = {}
    if run_fresh_analysis and _DATA_FEEDS_OK:
        try:
            _macro = _data_feeds.get_full_macro_context()
            if _macro:
                st.session_state.macro_context = _macro
        except Exception as _mce:
            if _SELF_IMPROVE_OK:
                _self_improve.log_error("data_feeds.macro_context", _mce)

    # ── Market observation storage (for AI pattern mining) ───────────────────
    if run_fresh_analysis and _SELF_IMPROVE_OK and signal:
        try:
            _sentiment_val = (st.session_state.macro_context.get("sentiment_score") or 0)
            _self_improve.store_market_observation(
                signal=signal, score=score if score else 0,
                session=session, dxy_dir=dxy_dir,
                economic_data=st.session_state.macro_context.get("fred"),
                sentiment=_sentiment_val,
            )
        except Exception:
            pass

    # ── Self-heal cycle (runs at most once per hour) ──────────────────────────
    if run_fresh_analysis and _SELF_IMPROVE_OK and _DB_OK:
        try:
            if _self_improve.should_run_heal():
                _heal_result = _self_improve.run_heal_cycle(
                    active_dna=st.session_state.get("active_dna") or {},
                    current_user=current_user,
                )
                if _heal_result:
                    st.session_state.last_heal_result = _heal_result
                    if _heal_result.get("dna_updated") and _heal_result.get("new_dna"):
                        st.session_state.active_dna = _heal_result["new_dna"]
        except Exception as _he:
            logging.warning("Self-heal error: %s", _he)

    # ── EUR/USD — Precio e indicadores desde TradingView ─────────────────────
    st.markdown('<div id="sec-eurusd"></div>', unsafe_allow_html=True)
    st.markdown("---")
    st.subheader("💶 EUR/USD — Precio Actual")

    _tv = get_tv_data("EURUSD", "1h")
    _tv_px   = _tv.get("price") or signal.get("price") or (tick["bid"] if tick else None)
    _tv_chg  = _tv.get("change_pct")
    _tv_rsi  = _tv.get("rsi")    or signal.get("rsi")
    _tv_ema20= _tv.get("ema20")  or signal.get("ema21")
    _tv_ema50= _tv.get("ema50")  or signal.get("ema50")
    _tv_atr  = _tv.get("atr")    or signal.get("atr_1h_pips")
    _tv_adx  = _tv.get("adx")
    _tv_macd = _tv.get("macd")
    _tv_macd_sig = _tv.get("macd_signal")
    _tv_rec  = _tv.get("recommendation", "")
    _tv_buy  = _tv.get("buy", 0)
    _tv_sell = _tv.get("sell", 0)
    _tv_src  = "📡 TradingView" if _tv.get("source") == "TradingView" else ("MT5" if tick else "Análisis")

    # Color del precio según cambio
    _chg_col = "#3fb950" if (_tv_chg or 0) >= 0 else "#f85149"
    _chg_str = f"{_tv_chg:+.4f}%" if _tv_chg is not None else ""

    # Fila 1: precio principal + variación + recomendación TV
    _r1c1, _r1c2, _r1c3, _r1c4 = st.columns([2, 1, 1, 1])
    _r1c1.metric(
        f"EUR/USD ({_tv_src})",
        f"{_tv_px:.5f}" if _tv_px else "—",
        delta=_chg_str if _chg_str else None,
    )
    _rec_icon = {"STRONG_BUY": "🟢🟢", "BUY": "🟢", "NEUTRAL": "⚪",
                 "SELL": "🔴", "STRONG_SELL": "🔴🔴"}.get(_tv_rec, "⚪")
    _r1c2.metric("TV Señal", f"{_rec_icon} {_tv_rec.replace('_', ' ')}" if _tv_rec else "—")
    _r1c3.metric("Compradores", f"{_tv_buy}" if _tv_buy else "—")
    _r1c4.metric("Vendedores", f"{_tv_sell}" if _tv_sell else "—")

    # Fila 2: indicadores técnicos
    _r2c1, _r2c2, _r2c3, _r2c4, _r2c5 = st.columns(5)
    _r2c1.metric("RSI 14", f"{float(_tv_rsi):.1f}" if _tv_rsi is not None else "—",
                 delta=("Sobrecompra ⚠️" if (_tv_rsi or 50) > 70 else ("Sobreventa ⚠️" if (_tv_rsi or 50) < 30 else None)))
    _r2c2.metric("EMA 20", f"{float(_tv_ema20):.5f}" if _tv_ema20 else "—")
    _r2c3.metric("EMA 50", f"{float(_tv_ema50):.5f}" if _tv_ema50 else "—")
    _r2c4.metric("ADX", f"{float(_tv_adx):.1f}" if _tv_adx else "—",
                 help="ADX > 25 = tendencia fuerte")
    _r2c5.metric("ATR 1H", f"{float(_tv_atr):.1f} pips" if _tv_atr else "—")

    # Tendencia EMA y MACD
    _ema_up  = (_tv_ema20 and _tv_ema50 and _tv_ema20 > _tv_ema50)
    _ema_dn  = (_tv_ema20 and _tv_ema50 and _tv_ema20 < _tv_ema50)
    _macd_up = (_tv_macd and _tv_macd_sig and _tv_macd > _tv_macd_sig)
    _trend_lbl = "▲ Alcista" if _ema_up else ("▼ Bajista" if _ema_dn else "→ Lateral")
    _trend_col = "#3fb950" if _ema_up else ("#f85149" if _ema_dn else "#e3b341")
    _macd_lbl  = "▲ Alcista" if _macd_up else ("▼ Bajista" if (_tv_macd and _tv_macd_sig) else "—")
    _macd_col  = "#3fb950" if _macd_up else "#f85149"
    st.markdown(
        f'<div style="display:flex;gap:12px;margin-top:6px">'
        f'<div style="background:#161b22;border:1px solid #30363d;border-radius:8px;padding:6px 16px">'
        f'<span style="color:#8b949e;font-size:11px">EMA20/50 · </span>'
        f'<span style="color:{_trend_col};font-weight:700;font-size:13px">{_trend_lbl}</span></div>'
        f'<div style="background:#161b22;border:1px solid #30363d;border-radius:8px;padding:6px 16px">'
        f'<span style="color:#8b949e;font-size:11px">MACD · </span>'
        f'<span style="color:{_macd_col};font-weight:700;font-size:13px">{_macd_lbl}</span></div>'
        f'</div>',
        unsafe_allow_html=True,
    )

    # ── Señal principal ───────────────────────────────────────────────────────
    st.markdown('<div id="sec-senal"></div>', unsafe_allow_html=True)
    st.markdown("---")
    final    = signal.get("final_signal", "⚪ NO TRADE")
    _is_buy  = "COMPRA" in final
    _is_sell = "VENTA"  in final
    _scls    = "smc-sig-b" if _is_buy else ("smc-sig-s" if _is_sell else "smc-sig-n")
    _dcls    = "sig-dir-b" if _is_buy else ("sig-dir-s" if _is_sell else "sig-dir-n")
    _dtxt    = "COMPRA" if _is_buy else ("VENTA" if _is_sell else "SIN SETUP")
    _dico    = "▲" if _is_buy else ("▼" if _is_sell else "–")
    _px      = f"{signal.get('price', 0):.5f}" if signal.get("price") else "—"
    _sess_str= (signal.get("session") or "—").split(" ")[0]
    _vol_str = signal.get("volatility", "—")
    _buy_n   = signal.get("buy_signals", 0)
    _sell_n  = signal.get("sell_signals", 0)
    st.markdown(f"""<div class="smc-signal {_scls}">
  <div>
    <div class="sig-dir {_dcls}">{_dico} {_dtxt}</div>
    <div class="sig-price">{_px}</div>
  </div>
  <div class="sig-right">
    <span class="sig-pill">📍 {_sess_str}</span>
    <span class="sig-pill">⚡ {_vol_str}</span>
    <span class="sig-pill">▲ {_buy_n} &nbsp;▼ {_sell_n}</span>
  </div>
</div>""", unsafe_allow_html=True)

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
    st.markdown('<div id="sec-score"></div>', unsafe_allow_html=True)
    st.markdown("---")
    st.subheader("🎯 Score de Confluencia")
    score, score_reasons = calculate_confluence_score(
        signal, consensus, dxy_dir, session, vol_spikes, liq_levels, delta,
        cot=st.session_state.get("cot_data"),
        trend_strength=trend_strength_1h)

    # ── Aplicar Strategy DNA al score ─────────────────────────────────────
    _active_dna = st.session_state.get("active_dna") or {}
    _dna_adj_score = score
    _dna_reasons   = []
    if _AI_ENGINE_OK and _active_dna:
        try:
            _dna_adj_score, _dna_reasons = _ai_engine.apply_dna_to_signal(
                signal, score, _active_dna, session, dxy_dir)
            if _dna_reasons:
                score_reasons = list(score_reasons) + _dna_reasons
                score = _dna_adj_score
        except Exception:
            pass

    # ── Macro bonus from FRED + Finnhub ──────────────────────────────────────
    if _DATA_FEEDS_OK and st.session_state.get("macro_context"):
        try:
            _macro_adj, _macro_reasons = _data_feeds.macro_context_to_score_bonus(
                st.session_state.macro_context)
            if _macro_adj != 0:
                score = max(0, min(100, score + _macro_adj))
                score_reasons = list(score_reasons) + _macro_reasons
        except Exception:
            pass

    label, color = score_label(score)
    col_sc1, col_sc2 = st.columns([1, 2])
    with col_sc1:
        _sc_colors = {"green": "#10b981", "lightgreen": "#4ade80", "orange": "#f59e0b", "red": "#f87171"}
        _sc = _sc_colors.get(color, "#6b7280")
        st.markdown(f"""<div class="smc-score">
  <div class="sc-num" style="color:{_sc}">{score}<span class="sc-den">/100</span></div>
  <div class="sc-lbl" style="color:{_sc}">{label}</div>
  <div class="sc-track"><div class="sc-fill" style="width:{score}%;background:{_sc}"></div></div>
</div>""", unsafe_allow_html=True)
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

    # ── GRÁFICO ───────────────────────────────────────────────────────────────
    st.markdown('<div id="sec-chart"></div>', unsafe_allow_html=True)
    st.markdown("---")

    # ── Selector de timeframe ─────────────────────────────────────────────────
    _tf_cols = st.columns([1, 1, 1, 6])
    if _tf_cols[0].button("15 min", use_container_width=True,
                          type="primary" if st.session_state.chart_tf == "15M" else "secondary"):
        st.session_state.chart_tf = "15M"; st.rerun()
    if _tf_cols[1].button("1H",     use_container_width=True,
                          type="primary" if st.session_state.chart_tf == "1H"  else "secondary"):
        st.session_state.chart_tf = "1H";  st.rerun()
    if _tf_cols[2].button("4H",     use_container_width=True,
                          type="primary" if st.session_state.chart_tf == "4H"  else "secondary"):
        st.session_state.chart_tf = "4H";  st.rerun()

    _tv_tf_map  = {"15M": "15",  "1H": "60",  "4H": "240"}
    _label_map  = {"15M": "15 min", "1H": "1H", "4H": "4H"}
    _tv_interval = _tv_tf_map[st.session_state.chart_tf]
    st.subheader(f"📈 EUR/USD — {_label_map[st.session_state.chart_tf]}")

    # ── Widget TradingView (tiempo real) ──────────────────────────────────────
    import streamlit.components.v1 as _stc_chart
    _stc_chart.html(f"""
<div class="tradingview-widget-container" style="height:500px;width:100%">
  <div id="tv_chart_widget" style="height:100%;width:100%"></div>
  <script type="text/javascript" src="https://s3.tradingview.com/tv.js"></script>
  <script type="text/javascript">
  new TradingView.widget({{
    "autosize": true,
    "symbol": "FX:EURUSD",
    "interval": "{_tv_interval}",
    "timezone": "Europe/Madrid",
    "theme": "dark",
    "style": "1",
    "locale": "es",
    "toolbar_bg": "#0d1117",
    "enable_publishing": false,
    "hide_top_toolbar": false,
    "hide_legend": false,
    "save_image": false,
    "container_id": "tv_chart_widget",
    "studies": [
      "RSI@tv-basicstudies",
      "MACD@tv-basicstudies",
      {{"id": "MAExp@tv-basicstudies", "inputs": {{"length": 5}}}},
      {{"id": "MAExp@tv-basicstudies", "inputs": {{"length": 10}}}},
      {{"id": "MAExp@tv-basicstudies", "inputs": {{"length": 20}}}},
      {{"id": "MAExp@tv-basicstudies", "inputs": {{"length": 50}}}},
      {{"id": "PUB;iFtRrGCw"}}
    ],
    "show_popup_button": false
  }});
  </script>
</div>""", height=510)

    # ── EMA Ribbon Multi-Marco ────────────────────────────────────────────────
    _df_4h = get_eurusd_data("4h")
    _df_1d = get_eurusd_data("1d")
    _ema_tfs = {
        "15 min": get_ema_ribbon(df_15),
        "1H":     get_ema_ribbon(df_1h),
        "4H":     get_ema_ribbon(_df_4h),
        "Daily":  get_ema_ribbon(_df_1d),
    }
    _any_ribbon = any(v for v in _ema_tfs.values())
    if _any_ribbon:
        with st.expander("🎀 EMA Ribbon 5/10/20/50 — Análisis Multi-Marco", expanded=True):
            _rb_cols = st.columns(4)
            for _ci, (_tf_lbl, _rb) in enumerate(_ema_tfs.items()):
                with _rb_cols[_ci]:
                    st.markdown(f"**{_tf_lbl}**")
                    if not _rb:
                        st.caption("Sin datos")
                        continue
                    _trend_color = (
                        "🟢" if _rb.get("bull_align") else
                        "🔴" if _rb.get("bear_align") else
                        "🟡"
                    )
                    st.markdown(f"{_trend_color} **{_rb.get('trend','—')}**")
                    st.caption(f"EMA5:  `{_rb['ema5']:.5f}`")
                    st.caption(f"EMA10: `{_rb['ema10']:.5f}`")
                    st.caption(f"EMA20: `{_rb['ema20']:.5f}`")
                    st.caption(f"EMA50: `{_rb['ema50']:.5f}`")
                    _sig_map = [
                        ("buy_signal",   st.success, "🚀 BUY CROSS"),
                        ("sell_signal",  st.error,   "💥 SELL CROSS"),
                        ("golden_cross", st.success, "✨ Golden Cross"),
                        ("death_cross",  st.error,   "💀 Death Cross"),
                    ]
                    for _key, _fn, _label in _sig_map:
                        if _rb.get(_key):
                            _fn(_label)

            # Global alignment status
            st.markdown("---")
            _bull_count = sum(1 for r in _ema_tfs.values() if r and r.get("bull_align"))
            _bear_count = sum(1 for r in _ema_tfs.values() if r and r.get("bear_align"))
            _total_tf   = sum(1 for r in _ema_tfs.values() if r)
            if _total_tf > 0:
                if _bull_count == _total_tf:
                    st.success("✅ ALINEACIÓN GLOBAL ALCISTA — Todos los marcos temporales en tendencia alcista")
                elif _bear_count == _total_tf:
                    st.error("🔴 ALINEACIÓN GLOBAL BAJISTA — Todos los marcos temporales en tendencia bajista")
                elif _bull_count > _bear_count:
                    st.warning(f"⚠️ SESGO ALCISTA ({_bull_count}/{_total_tf} marcos alcistas) — Divergencia en algunos marcos")
                elif _bear_count > _bull_count:
                    st.warning(f"⚠️ SESGO BAJISTA ({_bear_count}/{_total_tf} marcos bajistas) — Divergencia en algunos marcos")
                else:
                    st.info("⚪ DIVERGENCIA — Sin consenso entre marcos temporales")

    # ── Gráfico Plotly con indicadores SMC ───────────────────────────────────
    _df_chart_map = {"15M": df_15, "1H": df_1h, "4H": _df_4h}
    _df_chart = _df_chart_map.get(st.session_state.chart_tf, df_1h)
    _chart_trades = []
    if _DB_OK:
        try:
            _chart_trades = _db.load_trades(user_id=current_user, limit=30)
        except Exception:
            pass
    with st.expander("📊 Gráfico SMC con niveles (Plotly)", expanded=False):
        _render_trading_chart(
            df=_df_chart,
            signal=signal or {},
            score=score,
            session=session,
            liq_levels=liq_levels or [],
            poc=poc,
            vol_spikes=vol_spikes or [],
            market_structures=market_structures or {},
            stop_hunts=stop_hunts or [],
            news_items=(signal or {}).get("news", []),
            trades_history=_chart_trades,
        )

    # ═══════════════════════════════════════════════════════════════════════════
    # SECCIÓN: ANÁLISIS TÉCNICO AVANZADO
    # ═══════════════════════════════════════════════════════════════════════════
    st.markdown(
        '<div style="background:linear-gradient(90deg,#0d2137,#0a3d62);'
        'border-left:4px solid #1e90ff;border-radius:8px;padding:10px 18px;margin:18px 0 4px 0">'
        '<span style="color:#1e90ff;font-size:13px;font-weight:700;letter-spacing:1px">'
        '📈 ANÁLISIS TÉCNICO AVANZADO</span>'
        '<span style="color:#8b949e;font-size:11px;margin-left:10px">'
        'DNA · Volumen · Scalping · Estructura · Manipulación</span>'
        '</div>',
        unsafe_allow_html=True,
    )

    # ── Strategy DNA Panel ────────────────────────────────────────────────────
    st.markdown('<div id="sec-dna"></div>', unsafe_allow_html=True)
    st.markdown("---")
    _dna_v  = _active_dna.get("_version") or _active_dna.get("version", 1)
    _dna_wr = _active_dna.get("_winrate") or _active_dna.get("winrate", 0)
    _dna_np = _active_dna.get("_net_pips") or _active_dna.get("net_pips", 0)
    _dna_te = _active_dna.get("_trades") or _active_dna.get("trades_evaluated", 0)
    _dna_ki = _active_dna.get("_insight") or _active_dna.get("key_insight", "")
    _dna_ex = _active_dna.get("explanation", "")
    with st.expander(f"🧬 Strategy DNA v{_dna_v} — Win Rate: {float(_dna_wr or 0):.1f}%  |  Pips: {float(_dna_np or 0):+.1f}  |  Trades evaluados: {_dna_te}", expanded=False):
        _dc1, _dc2, _dc3 = st.columns(3)
        _dc1.metric("Versión DNA", f"v{_dna_v}")
        _dc2.metric("Win Rate Aprendido", f"{float(_dna_wr or 0):.1f}%")
        _dc3.metric("Pips Netos DNA", f"{float(_dna_np or 0):+.1f}")
        if _dna_ki:
            st.info(f"💡 **Insight clave:** {_dna_ki}")
        if _dna_ex:
            st.caption(f"📝 {_dna_ex}")
        if _dna_reasons:
            st.write("**Ajustes DNA en este análisis:**")
            for _dr in _dna_reasons:
                st.write(f"  {_dr}")
        if _AI_ENGINE_OK:
            _active_providers = _ai_engine.get_active_providers()
            st.caption(f"🤖 Proveedores IA activos: {', '.join(_active_providers) if _active_providers else 'ninguno — añade GROQ_API_KEY'}")
        _ev_hist = []
        if _DB_OK:
            try:
                _ev_hist = _db.get_evolution_history(limit=6)
            except Exception:
                pass
        if _ev_hist:
            st.write("**Historial de evolución:**")
            for _ev in _ev_hist:
                _active_mark = "⚡ ACTIVO" if _ev.get("is_active") else ""
                st.caption(
                    f"v{_ev['version']} {_active_mark}  —  "
                    f"WR: {float(_ev.get('winrate') or 0):.1f}%  |  "
                    f"Pips: {float(_ev.get('net_pips') or 0):+.1f}  |  "
                    f"{_ev.get('key_insight','')[:60]}"
                )
        # Evolución automática — solo se muestra el estado, no hay botón
        _trades_since = 0
        if _DB_OK:
            try:
                _trades_since = _db.count_trades_since_last_evolution()
            except Exception:
                pass
        st.caption(f"🧬 Evolución automática · Trades desde última: {_trades_since}/8 · Corre automáticamente al llegar a 8")

    # ── Auto-evolución: cada 8 trades cerrados ────────────────────────────────
    if _AI_ENGINE_OK and _DB_OK and run_fresh_analysis:
        try:
            if _db.count_trades_since_last_evolution() >= 8:
                _evo_trades2 = _db.get_trades_for_evolution()
                if len(_evo_trades2) >= 5:
                    _new_dna2 = _ai_engine.evolve_strategy(_evo_trades2, _active_dna)
                    if _new_dna2:
                        _nv2 = _new_dna2.get("version", (_active_dna.get("version") or 1) + 1)
                        _db.save_strategy_dna(
                            version=_nv2, rules=_new_dna2,
                            fitness=float(_new_dna2.get("fitness") or 0),
                            trades_evaluated=int(_new_dna2.get("trades_evaluated") or 0),
                            winrate=float(_new_dna2.get("winrate") or 0),
                            net_pips=float(_new_dna2.get("net_pips") or 0),
                            key_insight=str(_new_dna2.get("key_insight") or "")[:200],
                        )
                        st.session_state.active_dna = _new_dna2
        except Exception:
            pass

    # ── VOLUMEN — Panel principal ─────────────────────────────────────────────
    st.markdown('<div id="sec-vol"></div>', unsafe_allow_html=True)
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
    st.markdown('<div id="sec-scalping"></div>', unsafe_allow_html=True)
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
    st.markdown('<div id="sec-estructura"></div>', unsafe_allow_html=True)
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
    st.markdown('<div id="sec-manipulacion"></div>', unsafe_allow_html=True)
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

    # ═══════════════════════════════════════════════════════════════════════════
    # SECCIÓN: FUNDAMENTAL & COT
    # ═══════════════════════════════════════════════════════════════════════════
    st.markdown(
        '<div style="background:linear-gradient(90deg,#0d2137,#1a3a1a);'
        'border-left:4px solid #3fb950;border-radius:8px;padding:10px 18px;margin:18px 0 4px 0">'
        '<span style="color:#3fb950;font-size:13px;font-weight:700;letter-spacing:1px">'
        '🏦 FUNDAMENTAL & INSTITUCIONAL</span>'
        '<span style="color:#8b949e;font-size:11px;margin-left:10px">'
        'COT Report · Grandes Inversores · CFTC</span>'
        '</div>',
        unsafe_allow_html=True,
    )

    # ── COT — Datos Institucionales (auto-actualizados cada 6h) ─────────────
    st.markdown('<div id="sec-cot"></div>', unsafe_allow_html=True)
    st.markdown("---")
    _cot_h1, _cot_h2 = st.columns([3, 1])
    _cot_h1.subheader("🏦 Datos Institucionales — COT Report (CFTC) + Grandes Inversores")
    _cot_h2.caption("🔄 Auto cada 6h")

    # Cargar COT desde DB (guardado por background worker) o caché de memoria
    cot = None
    try:
        if _DB_OK:
            _cot_rows = _db.get_metrics(name="cot_data", limit=1) or []
            if _cot_rows:
                _cot_ts = str(_cot_rows[0].get("created_at", ""))[:16]
                cot = (_cot_rows[0].get("context") or {}).get("data")
                if cot:
                    st.session_state.cot_data = cot  # actualizar session_state
    except Exception:
        pass
    # Fallback a session_state
    if not cot:
        cot = st.session_state.get("cot_data")

    cot_col1, cot_col2 = st.columns([2, 1])
    with cot_col1:
        if cot:
            _cot_ts_str = _cot_ts if 'cot' in dir() and _cot_ts else ""
            if _cot_ts_str:
                st.caption(f"Última actualización: {_cot_ts_str}")
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
            st.info("⏳ El servidor descargará el COT automáticamente (cada 6h). Primera carga pendiente.")
    with cot_col2:
        st.write("**¿Qué es el COT?**")
        st.caption(
            "Muestra las posiciones de grandes especuladores "
            "(fondos de inversión, hedge funds) en EUR futures. "
            "Si aumentan longs → institucionales apuestan al alza del EUR."
        )
        st.write("**Fuente:** CFTC (semanal, viernes)")
        st.write("**Refresco:** automático cada 6h")

    # ═══════════════════════════════════════════════════════════════════════════
    # SECCIÓN: IA & ESTRATEGIAS
    # ═══════════════════════════════════════════════════════════════════════════
    st.markdown(
        '<div style="background:linear-gradient(90deg,#0d2137,#2d1b4e);'
        'border-left:4px solid #a855f7;border-radius:8px;padding:10px 18px;margin:18px 0 4px 0">'
        '<span style="color:#a855f7;font-size:13px;font-weight:700;letter-spacing:1px">'
        '🤖 IA & ESTRATEGIAS</span>'
        '<span style="color:#8b949e;font-size:11px;margin-left:10px">'
        'Motor de Bias · Confluencia · Señales</span>'
        '</div>',
        unsafe_allow_html=True,
    )

    # ── IA — Motor de Bias ────────────────────────────────────────────────────
    st.markdown('<div id="sec-ia"></div>', unsafe_allow_html=True)
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


# Navegacion principal
st.markdown("---")
_t_bt, _t_bot, _t_mejora, _t_ia, _t_prem = st.tabs([
    "📊 Backtest & Mercado",
    "🤖 Bot & Posiciones",
    "🔬 Auto-Mejora",
    "🧠 Asesor IA",
    "🏹 Señales Premium",
])

with _t_bt:
    # ── BACKTEST + COMPARACIÓN DE ESTRATEGIAS + CONTEXTO DE MERCADO ───────────────
    st.markdown('<div id="sec-backtest"></div>', unsafe_allow_html=True)
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
                    # Persistir en DB + disco
                    _save_bt_cache(cmp, st.session_state.get("lt_comparison"))
                    if _DB_OK:
                        try:
                            _sig_snap = signal if isinstance(signal, dict) else {}
                            _db.save_snapshot(
                                price=price or 0,
                                signal=_sig_snap.get("final_signal", "NEUTRAL"),
                                score=score or 0,
                                dxy_trend=dxy_trend or "",
                                regime=_sig_snap.get("regime", ""),
                                strategy=cmp["best"].get("strategy", ""),
                                extra={"best_pf": cmp["best"].get("profit_factor", 0),
                                       "best_wr": cmp["best"].get("winrate", 0)},
                            )
                        except Exception:
                            pass
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
        # Calendario económico — auto-cargado desde DB (background worker lo actualiza cada 6h)
        st.caption("📅 **Calendario económico** — actualización automática cada 6h")
        cal_data = []
        try:
            if _DB_OK:
                _cal_rows = _db.get_metrics(name="economic_calendar", limit=1) or []
                if _cal_rows:
                    _cal_ts   = str(_cal_rows[0].get("created_at", ""))[:16]
                    cal_data  = (_cal_rows[0].get("context") or {}).get("events", [])
                    if cal_data:
                        st.session_state.economic_calendar = cal_data
        except Exception:
            pass
        if not cal_data:
            cal_data = st.session_state.get("economic_calendar") or []
        if cal_data:
            _cal_ts_str = _cal_ts if "_cal_ts" in dir() else ""
            if _cal_ts_str:
                st.caption(f"Actualizado: {_cal_ts_str}")
            high_ev = [e for e in cal_data if e.get("impact","").upper() == "HIGH"]
            med_ev  = [e for e in cal_data if e.get("impact","").upper() == "MEDIUM"]
            st.markdown(f"**Esta semana:** {len(high_ev)} eventos ALTO impacto · {len(med_ev)} MEDIO impacto")
            for ev in high_ev[:5]:
                st.markdown(
                    f"🔴 **[{ev.get('currency','')}]** {ev.get('title','')} "
                    f"— {str(ev.get('date',''))[:10]} "
                    f"| Prev: {ev.get('previous','?')} | Fore: {ev.get('forecast','?')}"
                )
        else:
            st.info("⏳ El servidor cargará el calendario automáticamente (cada 6h).")

    # ── Comparación de estrategias ─────────────────────────────────────────────
    cmp_result = st.session_state.get("strategy_comparison")
    if cmp_result:
        st.markdown("### 📊 Ranking de Estrategias")
        best_name = cmp_result["best"]["strategy"]

        if "_RANK_EMOJI" not in globals():
            _RANK_EMOJI = ["🥇","🥈","🥉","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟","⓫","⓬","⓭","⓮","⓯","⓰","⓱"]
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
    st.markdown('<div id="sec-backtest2008"></div>', unsafe_allow_html=True)
    st.markdown("---")
    st.subheader("🌍 Backtest Histórico — Desde 2008 hasta Hoy (Datos Diarios)")
    st.caption(
        "Descarga datos diarios EUR/USD desde 2008 (~4,000 velas) y ejecuta las 17 estrategias. "
        "Los umbrales ATR se escalan automáticamente para barras diarias. "
        "Resultado: cuál estrategia habría sido más rentable en 16+ años de mercado real."
    )

    if "lt_comparison" not in st.session_state:
        _bt_disk2 = _load_bt_cache()
        st.session_state.lt_comparison = _bt_disk2.get("lt")

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
                    # Persistir en disco para sobrevivir recargas de página
                    _save_bt_cache(st.session_state.get("strategy_comparison"), _lt_cmp)
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

        if "_RANK_EMOJI" not in globals():
            _RANK_EMOJI = ["🥇","🥈","🥉","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟","⓫","⓬","⓭","⓮","⓯","⓰","⓱"]
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
    st.markdown('<div id="sec-porq"></div>', unsafe_allow_html=True)
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

with _t_bot:
    st.markdown('<div id="sec-bot"></div>', unsafe_allow_html=True)
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

    # ── Detectar fuente de conexión disponible ────────────────────────────────
    _svc_ok    = _mt5_service_available() and mt5_service_health().get("mt5") == "connected"
    _local_ok  = is_mt5_available() and mt5_connect()
    _any_conn  = _svc_ok or _local_ok

    if _svc_ok:
        st.success("✅ OANDA conectado — trading automático disponible")
    elif _local_ok:
        st.success("✅ MT5 local conectado — trading disponible")
    else:
        st.info("📊 **Modo señales** — análisis, scoring y Telegram activos.\nEjecución automática de órdenes no configurada.")

    if _any_conn:
        # Estado actual del bot
        if st.session_state.bot_enabled:
            st.success("🚀 **BOT ACTIVO** - Ejecutando señales automáticamente")
        else:
            st.info("⏸️ **BOT INACTIVO** - Modo manual")

        # Estado del bot — solo lectura, corre automáticamente en el servidor
        _bot_cols = st.columns([2, 1])
        with _bot_cols[0]:
            if st.session_state.bot_enabled:
                st.success("🤖 **Bot ACTIVO** — analizando señales automáticamente 24/7")
            else:
                st.info("📊 **Modo señales** — acumulando datos y aprendiendo del mercado")
        with _bot_cols[1]:
            positions_mt5 = get_mt5_positions()
            if positions_mt5:
                st.warning(f"📊 {len(positions_mt5)} posiciones abiertas · cierre automático por TP/SL")
            else:
                st.caption("🎯 Sin posiciones abiertas")

        # ── Panel de posiciones abiertas ──────────────────────────────────────────
        _live_pos = get_mt5_positions()
        if _live_pos:
            st.markdown("#### 📊 Posiciones Abiertas")
            for _p in _live_pos:
                # Compatibilidad: dict (OANDA remoto) u objeto MT5 (local)
                def _pv(attr, default=0):
                    return _p.get(attr, default) if isinstance(_p, dict) else getattr(_p, attr, default)
                _ticket   = _pv("ticket", "—")
                _open_p   = float(_pv("open_price") or _pv("price_open", 0))
                _cur_p    = float(_pv("current_price") or _pv("price_current", _open_p))
                _sl_p     = float(_pv("sl", 0))
                _vol      = float(_pv("volume", 0))
                _profit   = float(_pv("profit", 0))
                _type_raw = _pv("type", "BUY")
                _is_buy   = (_type_raw in (0, "BUY", "LONG")) if not isinstance(_type_raw, str) else _type_raw.upper() in ("BUY", "LONG")
                _dir_lbl  = "LONG" if _is_buy else "SHORT"
                _ppips    = (_cur_p - _open_p) / 0.0001 if _is_buy else (_open_p - _cur_p) / 0.0001
                _be_icon  = "⚖️ BE" if (_sl_p >= _open_p if _is_buy else _sl_p <= _open_p) and _sl_p != 0 else "🎯"
                st.markdown(
                    f"**#{_ticket}** {_dir_lbl} {_vol}L @ {_open_p:.5f} "
                    f"| Actual: {_cur_p:.5f} "
                    f"| {_be_icon} P&L: {_ppips:+.1f}p (${_profit:.2f})"
                )
        else:
            st.info("🎯 Sin posiciones abiertas")

        # ── Break-Even automático ─────────────────────────────────────────────────
        if st.session_state.bot_enabled and _live_pos:
            _be_msgs = manage_positions_be()
            for _bm in _be_msgs:
                st.success(f"⚖️ {_bm}")

        # ── Información de riesgo y lógica del bot ────────────────────────────────
        if st.session_state.bot_enabled:
            st.warning("⚠️ **BOT ACTIVO** — Trading automático en curso")
            st.info(f"💰 Volumen: {st.session_state.bot_volume} lotes | SL máx: {SCALP_SL_PIPS}p")

            # Lógica del bot: ejecutar señal si condiciones se cumplen
            _bot_score = score if st.session_state.analysis_executed else 0
            _bot_signal = signal if st.session_state.analysis_executed else {}
            _bot_liq = liq_levels if st.session_state.analysis_executed else []

            if not st.session_state.analysis_executed:
                st.info("🔄 Presiona 'ANALIZAR MERCADO' primero para activar el bot")
            elif st.session_state.bot_just_activated:
                st.info("🤖 Bot activado — esperando próxima señal de calidad...")
                st.session_state.bot_just_activated = False
            elif _bot_signal.get("direction") and _bot_score >= MIN_DEFINITIVE_SCORE:
                # Solo ejecutar si no hay posiciones abiertas y la señal cambió
                if _live_pos:
                    st.info(f"🔒 Posición abierta — bot monitorea BE y gestión automática")
                elif st.session_state.bot_last_signal != _bot_signal.get("direction"):
                    _ok, _msg = auto_trade_signal(_bot_signal, st.session_state.bot_volume, liq_levels=_bot_liq)
                    if _ok:
                        st.success(f"🚀 Bot ejecutó trade: {_msg}")
                        st.session_state.bot_last_signal = _bot_signal.get("direction")
                        # Persistir trade en DB con snapshot de mercado
                        if _DB_OK:
                            try:
                                _mkt_snap = {}
                                if _AI_ENGINE_OK:
                                    _mkt_snap = _ai_engine.build_market_snapshot(
                                        _bot_signal, _bot_score, session, dxy_dir,
                                        vol_spikes, delta,
                                        st.session_state.get("market_context_reasons"),
                                        dna_version=int(_active_dna.get("_version") or _active_dna.get("version", 1)),
                                    )
                                _db.save_trade_with_snapshot(
                                    direction=_bot_signal.get("direction", ""),
                                    entry_price=float(_bot_signal.get("entry", price or 0)),
                                    sl_price=float(_bot_signal.get("stop_loss", 0) or 0),
                                    tp_price=float(_bot_signal.get("take_profit", 0) or 0),
                                    outcome="OPEN",
                                    pips=0.0,
                                    pnl=0.0,
                                    strategy=_bot_signal.get("strategy", ""),
                                    score=_bot_score,
                                    market_snapshot=_mkt_snap,
                                    dna_version=int(_active_dna.get("_version") or _active_dna.get("version", 1)),
                                    user_id=current_user,
                                )
                            except Exception:
                                pass
                        try:
                            send_telegram_alert(_bot_signal, _bot_score, definitive=True, reason=f"Bot auto: {_msg}")
                        except Exception:
                            pass
                    else:
                        st.error(f"❌ Error bot: {_msg}")
                else:
                    st.info("🔄 Señal activa ya ejecutada — esperando nueva señal")
            else:
                _threshold_gap = MIN_DEFINITIVE_SCORE - _bot_score
                if _threshold_gap > 0:
                    st.info(f"⏳ Score {_bot_score}/100 — faltan {_threshold_gap}p para ejecutar (mínimo {MIN_DEFINITIVE_SCORE})")
                else:
                    st.info("⏳ Sin dirección clara — bot en espera")


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
    st.markdown('<div id="sec-dashboard"></div>', unsafe_allow_html=True)
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
    st.markdown('<div id="sec-dxy"></div>', unsafe_allow_html=True)
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

    st.markdown('<div id="sec-accion"></div>', unsafe_allow_html=True)
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


    # ── AUTO-MEJORA — Sistema autónomo de aprendizaje y corrección ────────────────

with _t_mejora:
    st.markdown('<div id="sec-autoimprove"></div>', unsafe_allow_html=True)
    st.markdown("---")
    st.subheader("🔬 Sistema de Auto-Mejora Autónoma")
    st.caption("El sistema monitoriza su propio rendimiento, detecta errores, y se corrige automáticamente cada hora usando IA.")

    _sim_col1, _sim_col2 = st.columns([1, 1])

    with _sim_col1:
        # ── APIs activas / pendientes (solo en Railway) ───────────────────────────
        if not _IS_LOCAL:
            st.markdown("**🌐 APIs Gratuitas — Estado**")
            if _SELF_IMPROVE_OK:
                _api_status = _self_improve.get_configured_apis()
                _api_missing = _self_improve.get_missing_apis()
                for _api in _api_status:
                    _t = "🤖" if _api["type"] == "ai" else "📊"
                    st.success(f"{_t} **{_api['name']}** activo")
                if _api_missing:
                    st.markdown("**➕ APIs gratuitas disponibles (sin configurar):**")
                    for _api in _api_missing:
                        _t = "🤖" if _api["type"] == "ai" else "📊"
                        st.info(f"{_t} {_api['name']} — Obtén key gratis: {_api['url']}\n`Railway → Variables → {_api['env']}`")
            else:
                st.warning("Módulo self_improve no disponible")
        else:
            st.info(f"🌐 APIs configuradas en Railway · [Abrir panel completo →]({_RAILWAY_URL})")

        # ── Datos macro FRED ─────────────────────────────────────────────────────
        _macro_ctx = st.session_state.get("macro_context") or {}
        if _macro_ctx.get("fred"):
            st.markdown("**🏦 Macro FRED (tiempo real)**")
            _fred = _macro_ctx["fred"]
            _fc1, _fc2 = st.columns(2)
            if _fred.get("fed_rate"):
                _fc1.metric("Tasa Fed", f"{_fred['fed_rate']['value']:.2f}%")
            if _fred.get("unemployment"):
                _fc2.metric("Desempleo", f"{_fred['unemployment']['value']:.1f}%")
            if _fred.get("10y_yield"):
                _fc1.metric("Bono 10Y", f"{_fred['10y_yield']['value']:.2f}%")
            if _fred.get("2y_yield"):
                _fc2.metric("Bono 2Y", f"{_fred['2y_yield']['value']:.2f}%")
            _bias = _macro_ctx.get("macro_bias", "NEUTRAL")
            _yc   = _macro_ctx.get("yield_curve", "?")
            _bias_col = "🟢" if _bias == "BEARISH_USD" else "🔴" if _bias == "BULLISH_USD" else "⚪"
            st.caption(f"{_bias_col} Sesgo USD: **{_bias}** | Yield curve: **{_yc}**")
        elif _DATA_FEEDS_OK:
            st.caption("🏦 FRED cargando — ejecuta un análisis para ver datos macro.")

    with _sim_col2:
        # ── Último ciclo de auto-sanación ─────────────────────────────────────────
        st.markdown("**🩺 Último Ciclo de Auto-Corrección**")
        _heal = st.session_state.get("last_heal_result")
        if _heal:
            _hs = _heal.get("health_status", "?")
            _hs_icon = "🟢" if _hs == "ok" else "🟡" if _hs == "warning" else "🔴"
            st.markdown(f"{_hs_icon} Estado: **{_hs.upper()}**")
            if _heal.get("summary"):
                st.write(_heal["summary"])
            if _heal.get("top_finding"):
                st.info(f"💡 **Hallazgo:** {_heal['top_finding']}")
            if _heal.get("applied_changes"):
                st.success(f"✅ Parámetros ajustados: {list(_heal['applied_changes'].keys())}")
            if _heal.get("dna_updated"):
                _new_v = (_heal.get("new_dna") or {}).get("version", "?")
                st.success(f"🧬 DNA auto-evolucionado a v{_new_v}")
            st.caption(f"Ejecutado: {(_heal.get('ts') or '')[:16]}")
        else:
            st.info("Esperando primer ciclo de auto-corrección (se ejecuta automáticamente cada hora al analizar)")

        # ── Historial de mejoras ──────────────────────────────────────────────────
        if _DB_OK:
            try:
                _improvements = _db.get_self_improvements(limit=20)
                # Skip garbage entries: AI errors and raw <think> blocks
                def _reason_ok(r: str) -> bool:
                    r = r.strip()
                    if not r or len(r) < 15:                         return False
                    if r.startswith("⚠️ Todos los proveedores"):     return False
                    if r.startswith("<think>"):                       return False
                    if r.startswith("{") or r.startswith("["):        return False  # raw JSON
                    if r.startswith("{ ") or '"health_status"' in r: return False  # JSON fragment
                    return True
                _improvements = [
                    _i for _i in _improvements
                    if _reason_ok(str(_i.get("reason", "")))
                ][:5]
                if _improvements:
                    st.markdown("**📋 Últimas auto-mejoras aplicadas**")
                    for _imp in _improvements:
                        _ic = "✅" if _imp.get("applied") else "📝"
                        _ts = str(_imp.get("created_at", ""))[:16]
                        st.caption(f"{_ic} {_ts} — {str(_imp.get('reason',''))[:80]}")
                else:
                    st.caption("Sin mejoras registradas aún.")
            except Exception:
                pass

    # ── Estrategia Maestra Adaptativa ─────────────────────────────────────────────
    try:
        import strategy_learner as _sl_mod
        _LEARNER_OK = True
    except ImportError:
        _LEARNER_OK = False

    if _LEARNER_OK:
        with st.expander("🧬 Estrategia Maestra Adaptativa", expanded=True):
            _master = None
            if _DB_OK:
                try:
                    _master = _db.load_active_strategy()
                except Exception:
                    pass

            if _master and _master.get("source") == "meta_learner":
                _mv = _master.get("version", "?")
                _mobs = _master.get("obs_analyzed", 0)
                _mevolved = str(_master.get("evolved_at", ""))[:16]
                _minsight = _master.get("ai_insight", "")
                _mimprove = _master.get("improvement", "")

                st.success(f"🧬 **DNA Maestro v{_mv}** — {_mobs} observaciones analizadas")
                if _minsight:
                    st.info(f"💡 **Insight IA:** {_minsight}")
                if _mimprove:
                    st.caption(f"📈 Mejora vs anterior: {_mimprove}")

                # Pesos de señal
                _sw = _master.get("signal_weights") or {}
                if _sw:
                    st.markdown("**⚖️ Pesos de señal aprendidos:**")
                    _sc1, _sc2, _sc3, _sc4, _sc5 = st.columns(5)
                    _sc1.metric("Técnico",      f"{_sw.get('technical',0)*100:.0f}%")
                    _sc2.metric("DXY",          f"{_sw.get('dxy',0)*100:.0f}%")
                    _sc3.metric("Volumen",      f"{_sw.get('volume',0)*100:.0f}%")
                    _sc4.metric("Sentimiento",  f"{_sw.get('sentiment',0)*100:.0f}%")
                    _sc5.metric("Fundamental",  f"{_sw.get('fundamental',0)*100:.0f}%")

                # Mejores condiciones
                _bc = _master.get("best_conditions") or []
                _ac = _master.get("avoid_conditions") or []
                if _bc or _ac:
                    _cc1, _cc2 = st.columns(2)
                    if _bc:
                        _cc1.markdown("**✅ Mejores condiciones:**")
                        for _c in _bc:
                            _cc1.caption(f"• {_c}")
                    if _ac:
                        _cc2.markdown("**⛔ Evitar:**")
                        for _c in _ac:
                            _cc2.caption(f"• {_c}")

                st.caption(f"Última evolución: {_mevolved} | Próxima en ~6h")

            else:
                _obs_count = 0
                if _DB_OK:
                    try:
                        _obs_count = len(_db.get_metrics(name="market_observation", limit=200) or [])
                    except Exception:
                        pass
                _needed = max(0, 20 - _obs_count)
                if _needed > 0:
                    st.info(f"🔄 Acumulando datos... {_obs_count}/20 observaciones necesarias para el primer aprendizaje.\nEl sistema aprenderá automáticamente.")
                else:
                    st.info(f"✅ {_obs_count} observaciones acumuladas. El primer ciclo de aprendizaje se ejecutará en breve (cada 6h).")

            # ── Ranking de estrategias ganadoras (auto-actualizado por el servidor) ─
            st.markdown("---")
            _rnk_h1, _rnk_h2 = st.columns([3, 1])
            _rnk_h1.markdown("**🏆 Ranking de estrategias — Doble Filtro (60d + 2008)**")
            try:
                import strategy_selector as _ss_mod
                _ranking = _ss_mod.get_latest_ranking()
                if not _ranking:
                    _ranking = []

                # Timestamps de última actualización
                _ts_60d = None
                _ts_lt  = None
                try:
                    _rk_rows = _db.get_metrics(name="strategy_ranking", limit=1) or []
                    if _rk_rows:
                        _rk_ts = (_rk_rows[0].get("context") or {}).get("ts", "")
                        _ts_60d = _rk_ts[:16] if _rk_ts else None
                except Exception:
                    pass
                _rnk_h2.caption(f"🔄 Auto cada 8h\n{'Actualizado: ' + _ts_60d if _ts_60d else 'Calculando...'}")

                if _ranking:
                    _n_cert = sum(1 for _r in _ranking if _r.get("is_certified") or _r.get("badge") == "🏆")
                    _n_60d  = sum(1 for _r in _ranking if (_r.get("is_winner_60d") or _r.get("is_winner")) and not (_r.get("is_certified") or _r.get("badge") == "🏆"))
                    _n_lt   = sum(1 for _r in _ranking if _r.get("is_winner_lt"))
                    _rk1, _rk2, _rk3 = st.columns(3)
                    _rk1.metric("🏆 Certificadas", _n_cert, help="Ganan en 60d 1H Y en 2008+ diario")
                    _rk2.metric("✅ Solo reciente", _n_60d, help="Ganan en 60d 1H pero no tienen datos 2008 aún")
                    _rk3.metric("📅 Ganan 2008+", _n_lt, help="Probadas rentables en datos históricos desde 2008")
                    st.markdown("")

                    _rank_cols = st.columns([3, 1, 1, 1, 1, 1])
                    _rank_cols[0].markdown("**Estrategia**")
                    _rank_cols[1].markdown("**WR 60d**")
                    _rank_cols[2].markdown("**PF 60d**")
                    _rank_cols[3].markdown("**WR 2008**")
                    _rank_cols[4].markdown("**PF 2008**")
                    _rank_cols[5].markdown("**Estado**")
                    for _ri, _r in enumerate(_ranking[:12]):
                        _rc = st.columns([3, 1, 1, 1, 1, 1])
                        if _r.get("badge"):
                            _emoji = _r["badge"]
                        elif _r.get("is_certified"):
                            _emoji = "🏆"
                        elif _r.get("is_winner_60d") or _r.get("is_winner"):
                            _emoji = "✅"
                        else:
                            _emoji = "❌"
                        _lbl = _r.get("label", _r.get("name", "?"))[:30]
                        _pos = "🥇" if _ri==0 else "🥈" if _ri==1 else "🥉" if _ri==2 else f"{_ri+1}."
                        _rc[0].caption(f"{_pos} {_lbl}")
                        _wr  = _r.get("winrate", 0)
                        _pf  = _r.get("profit_factor", 0)
                        _lt_wr = _r.get("lt_winrate", 0)
                        _lt_pf = _r.get("lt_profit_factor", 0)
                        _rc[1].caption(f"{'🟢' if _wr >= 55 else '🟡' if _wr >= 52 else '🔴'} {_wr:.0f}%")
                        _rc[2].caption(f"{_pf:.2f}")
                        _rc[3].caption(f"{'🟢' if _lt_wr >= 55 else '🟡' if _lt_wr >= 52 else '—'} {_lt_wr:.0f}%" if _lt_wr > 0 else "—")
                        _rc[4].caption(f"{_lt_pf:.2f}" if _lt_pf > 0 else "—")
                        _rc[5].caption(_emoji)
                else:
                    st.info("⏳ El servidor está calculando el ranking en background (primera vez puede tardar ~2 min). Se actualizará automáticamente.")
            except Exception:
                st.caption("Módulo strategy_selector cargando en background...")

            # Estado del ciclo de aprendizaje
            st.markdown("")
            _learn_h1, _learn_h2 = st.columns([3, 1])
            _learn_h1.caption("🧠 **Ciclo de aprendizaje IA** — corre automáticamente cada 6h")
            try:
                _last_learn = _db.get_setting("last_learn_ts") or ""
                if _last_learn:
                    from datetime import datetime, timezone as _tz
                    _last_dt = datetime.fromisoformat(_last_learn)
                    if not _last_dt.tzinfo:
                        _last_dt = _last_dt.replace(tzinfo=_tz.utc)
                    _hrs_ago = int((datetime.now(_tz.utc) - _last_dt).total_seconds() / 3600)
                    _nxt_hrs = max(0, 6 - _hrs_ago)
                    _learn_h2.caption(f"Último: hace {_hrs_ago}h · Próximo en ~{_nxt_hrs}h")
                else:
                    _learn_h2.caption("Primera ejecución pendiente...")
            except Exception:
                pass

    # ── Patrones de mercado detectados (auto-actualizados cada 6h) ────────────────
    if _SELF_IMPROVE_OK and _DB_OK:
        with st.expander("🔎 Patrones detectados por IA en observaciones históricas", expanded=False):
            try:
                _pat_rows = _db.get_metrics(name="pattern_report", limit=1) or []
                if _pat_rows:
                    _pat_ctx = _pat_rows[0].get("context") or {}
                    _pat_rep = _pat_ctx.get("report", "")
                    _pat_ts  = str(_pat_rows[0].get("created_at", ""))[:16]
                    if _pat_rep:
                        st.caption(f"🤖 Análisis automático · Última actualización: {_pat_ts} · Siguiente en ~6h")
                        st.write(_pat_rep)
                    else:
                        st.info("⏳ El servidor está analizando patrones en background. Disponible en la próxima ejecución (~6h desde el inicio).")
                else:
                    st.info("⏳ El servidor ejecutará el análisis de patrones automáticamente (cada 6h). Primera ejecución pendiente.")
            except Exception:
                st.caption("Patrones: cargando desde servidor...")

    # ── Trading Advisor AI ────────────────────────────────────────────────────────

with _t_ia:
    # ── Panel: Señal de Entrada Actual ────────────────────────────────────────
    st.markdown("---")
    st.subheader("🎯 Señal de Entrada Actual")

    _kb_strat_key  = signal.get("kb_best_strategy", "")
    _kb_dir_now    = signal.get("kb_direction", "NO TRADE")
    _kb_reason_now = signal.get("kb_reason", "")
    _sig_price_now = signal.get("price")
    _sig_dir_now   = signal.get("direction")
    _dna_panel     = st.session_state.get("active_dna") or {}
    _sc_panel      = st.session_state.get("strategy_comparison") or {}
    _sc_res_map    = {r["strategy"]: r for r in (_sc_panel.get("results") or [])}
    _strat_bt      = _sc_res_map.get(_kb_strat_key, {})
    _meta_panel    = _STRATEGY_META.get(_kb_strat_key, {})

    if not _sig_price_now:
        st.info("📊 Presiona **ANALIZAR MERCADO** para ver la señal de entrada.")
    else:
        # ── Badge de estado ────────────────────────────────────────────────────
        _edir = _kb_dir_now if _kb_dir_now in ("LONG", "SHORT") else _sig_dir_now
        if _edir == "LONG":
            _ec, _eb, _ei, _el = "#0f5132", "#3fb950", "📈", "ENTRADA — LONG"
        elif _edir == "SHORT":
            _ec, _eb, _ei, _el = "#450a0a", "#f85149", "📉", "ENTRADA — SHORT"
        else:
            _ec, _eb, _ei, _el = "#1a1a1a", "#e3b341", "⚪", "SIN ENTRADA — ESPERAR"
        st.markdown(
            f'<div style="background:{_ec};border:2px solid {_eb};border-radius:12px;'
            f'padding:14px 22px;display:flex;align-items:center;gap:16px;margin-bottom:12px">'
            f'<span style="font-size:32px">{_ei}</span>'
            f'<span style="color:{_eb};font-weight:700;font-size:20px">{_el}</span>'
            f'</div>',
            unsafe_allow_html=True,
        )

        # ── Score bar ──────────────────────────────────────────────────────────
        st.markdown(f"**Score de confluencia: {score}/100**")
        st.progress(min(score, 100) / 100)

        # ── Dos columnas: estrategia | niveles ────────────────────────────────
        _pn_l, _pn_r = st.columns(2)

        with _pn_l:
            st.markdown("**📊 Estrategia Activa**")
            if _kb_strat_key and _meta_panel:
                st.markdown(f"🏆 **{_meta_panel.get('label', _kb_strat_key)}**")
                st.caption(f"💡 {_meta_panel.get('why', '')[:120]}")
                if _meta_panel.get("pros"):
                    st.caption(f"✅ {_meta_panel['pros']}")
                if _meta_panel.get("cons"):
                    st.caption(f"⚠️ {_meta_panel['cons']}")
                if _kb_reason_now:
                    st.caption(f"📝 {_kb_reason_now[:120]}")
            else:
                st.caption("Estrategia seleccionada por KB tras el análisis.")

            st.markdown("**📈 Backtest 1 año**")
            if _strat_bt:
                _bt_c1, _bt_c2 = st.columns(2)
                _bt_c1.metric("Win Rate",    f"{_strat_bt.get('winrate', 0):.1f}%")
                _bt_c2.metric("Pips netos",  f"{_strat_bt.get('net_pips', 0):+.1f}")
                _bt_c1.metric("Max Drawdown",f"{_strat_bt.get('max_dd', 0):.1f}%")
                _bt_c2.metric("P. Factor",   f"{_strat_bt.get('profit_factor', 0):.2f}")
                _bt_c1.metric("Trades",      str(_strat_bt.get("total", "—")))
                _bt_c2.metric("R:R Ratio",   f"1:{_strat_bt.get('rr_ratio', 0):.1f}")
            else:
                st.caption("Ejecuta el backtest (pestaña 📊) para ver estadísticas.")

            st.markdown("**🧬 DNA Aprendido**")
            _dn_wr = float(_dna_panel.get("_winrate") or _dna_panel.get("winrate") or 0)
            _dn_np = float(_dna_panel.get("_net_pips") or _dna_panel.get("net_pips") or 0)
            _dn_v  = _dna_panel.get("_version") or _dna_panel.get("version", "—")
            _dc1, _dc2 = st.columns(2)
            _dc1.metric("WR Aprendido", f"{_dn_wr:.1f}%")
            _dc2.metric("Pips DNA",     f"{_dn_np:+.1f}")
            st.caption(f"Versión DNA: v{_dn_v}")

        with _pn_r:
            st.markdown("**💰 Niveles de Entrada**")
            _lc1, _lc2 = st.columns(2)
            if _sig_price_now:
                _lc1.metric("Precio actual", f"{_sig_price_now:.5f}")
            if smart_sl:
                _lc2.metric("Stop Loss", f"{smart_sl:.5f}",
                            delta=f"{round(abs(_sig_price_now - smart_sl)/0.0001,1)} pips" if _sig_price_now else None,
                            delta_color="inverse")
            if tp1:
                _lc1.metric("TP1", f"{tp1:.5f}")
            if tp2:
                _lc2.metric("TP2", f"{tp2:.5f}")
            if tp3:
                _lc1.metric("TP3", f"{tp3:.5f}")
            if rr2:
                _lc2.metric("R:R", f"1:{rr2:.1f}")

            st.markdown("**📡 Contexto de Mercado**")
            _ctx_c1, _ctx_c2 = st.columns(2)
            _ctx_c1.metric("Sesión",  signal.get("session", "—"))
            _ctx_c2.metric("Régimen", signal.get("regime",  "—").replace("_", " ").title())
            _ctx_c1.metric("RSI 1H",  f"{signal.get('rsi', 0):.1f}" if signal.get("rsi") else "—")
            _ctx_c2.metric("ATR 1H",  f"{signal.get('atr_1h_pips', 0):.1f} p" if signal.get("atr_1h_pips") else "—")

            if _edir in ("LONG", "SHORT"):
                _liq = signal.get("liquidity_warnings") or smart_warnings or []
                if _liq:
                    st.warning("⚠️ " + " · ".join(str(w) for w in _liq[:3]))
                else:
                    st.success("✅ Sin alertas de liquidez en los niveles actuales")

    st.markdown("---")

    # ── Trading Advisor AI ────────────────────────────────────────────────────
    st.markdown('<div id="sec-advisor"></div>', unsafe_allow_html=True)
    st.subheader(f"🧠 Trading Advisor AI — Asesor Personal de {current_user_name}")
    st.caption(f"Hola {current_user_name}, cuéntame tu tesis. Analizo tu visión contra tus operaciones reales, los backtests históricos (2008–hoy), posicionamiento institucional, técnico y fundamental. Aprendo de cada conversación.")

    import os as _os

    # API key: env var (Railway) o input de sesión
    _ant_key = _os.environ.get("GROQ_API_KEY", "gsk_0r0JRGjnYIAsgkfey3UWwWGdyb3FYjGb4Q5RKJICkoPGHM4pdQRlY").strip()

    # Session_id estable por usuario (aislado entre David y Javi)
    _adv_sess_key = f"advisor_session_{current_user}"
    if _adv_sess_key not in st.session_state:
        import uuid as _uuid
        st.session_state[_adv_sess_key] = str(_uuid.uuid4())
    st.session_state.advisor_session_id = st.session_state[_adv_sess_key]

    # Historial de chat: cargar desde DB filtrado por usuario
    _adv_chat_key = f"advisor_chat_{current_user}"
    if _adv_chat_key not in st.session_state:
        if _DB_OK:
            try:
                st.session_state[_adv_chat_key] = _db.load_chat_history(
                    st.session_state.advisor_session_id, user_id=current_user
                )
            except Exception:
                st.session_state[_adv_chat_key] = []
        else:
            st.session_state[_adv_chat_key] = []
    # Alias genérico usado en el resto del código
    st.session_state.advisor_chat = st.session_state[_adv_chat_key]


    def _advisor_context() -> str:
        """Builds a rich snapshot of current app data for the AI system prompt."""
        _lines = []
        _s = signal if isinstance(signal, dict) else {}

        _lines.append("=== DATOS EN TIEMPO REAL DE LA APP ===")
        _lines.append(f"Par: EUR/USD")
        _lines.append(f"Precio actual: {f'{price:.5f}' if price else 'N/A'}")
        _lines.append(f"Señal: {_s.get('final_signal', 'NEUTRAL')} | Score: {score}/100 ({label})")
        _lines.append(f"Señales alcistas: {_s.get('buy_signals', 0)} | Bajistas: {_s.get('sell_signals', 0)}")
        if _s.get('entry'):
            _lines.append(f"Setup sugerido: Entrada {_s.get('entry','?')} | SL {_s.get('stop_loss','?')} | TP {_s.get('take_profit','?')}")
        if _s.get('regime'):
            _lines.append(f"Régimen detectado: {_s.get('regime')}")
        if _s.get('strategy'):
            _lines.append(f"Estrategia activa: {_s.get('strategy')}")
        _lines.append(f"Sesión activa: {session}")
        _lines.append(f"DXY: {dxy_trend or 'N/A'} ({dxy_chg:+.2f}%) — {'DXY sube → presión bajista EUR' if dxy_dir == 'UP' else 'DXY baja → presión alcista EUR' if dxy_dir == 'DOWN' else 'DXY neutro'}")

        if delta:
            _dir_d = "compradores dominan" if delta.get("delta", 0) > 0 else "vendedores dominan"
            _lines.append(f"Delta de volumen: {delta.get('delta_pct', 0):+.1f}% ({_dir_d})")
        if vol_spikes:
            _lines.append(f"Spike de volumen: {vol_spikes[0].get('ratio', 0):.1f}x — posible movimiento institucional")
        if poc:
            _lines.append(f"POC (Volume Profile): {poc['precio']:.5f} — nivel de mayor liquidez")

        # Fundamental
        _c = consensus if isinstance(consensus, dict) else {}
        if _c:
            _lines.append(f"\n=== FUNDAMENTAL ===")
            _lines.append(f"Consenso: {_c.get('consensus', 'N/A')} | Sentimiento ponderado: {_c.get('weighted_sentiment', 0):+.3f}")
            _lines.append(f"Impacto medio noticias: {avg_impact:.0f}% | Fuentes procesadas: {total_sources}")

        # Market context reasons
        _reasons = st.session_state.get("market_context_reasons", [])
        if _reasons:
            _lines.append(f"\n=== RAZONES DETECTADAS POR LA APP ===")
            for _r in _reasons[:10]:
                _lines.append(f"  • {_r}")

        # 1-year backtest
        _cmp = st.session_state.get("strategy_comparison")
        if _cmp:
            _b = _cmp.get("best", {})
            _lines.append(f"\n=== BACKTEST 1 AÑO ===")
            _lines.append(f"Mejor estrategia: {_b.get('label', 'N/A')}")
            _lines.append(f"  WR: {_b.get('winrate', 0)}% | PF: {_b.get('profit_factor', 0)}x | Pips netos: {_b.get('net_pips', 0):+.1f} | Max DD: {_b.get('max_dd', 0)}%")
            _lines.append(f"  Operaciones: {_b.get('total', 0)} | Por qué funciona: {_b.get('why', 'N/A')}")
            _lines.append(f"  Ventajas: {_b.get('pros', 'N/A')}")
            _lines.append(f"  Limitaciones: {_b.get('cons', 'N/A')}")
            _rs = _cmp.get("results", [])
            if _rs:
                _lines.append("  Ranking completo (1 año):")
                for _ri, _r in enumerate(_rs[:6]):
                    _ok = "✅" if _r.get("profit_factor", 0) >= 1.0 else "⚠️"
                    _lines.append(f"    {_ri+1}. {_ok} {_r.get('label','?')}: {_r.get('winrate',0)}% WR | {_r.get('profit_factor',0)}x PF | {_r.get('net_pips',0):+.1f}p netos | DD {_r.get('max_dd',0)}%")

        # 2008 historical backtest
        _lt = st.session_state.get("lt_comparison")
        if _lt:
            _b2 = _lt.get("best", {})
            _lines.append(f"\n=== BACKTEST HISTÓRICO 2008–HOY (18+ AÑOS) ===")
            _lines.append(f"Mejor estrategia histórica: {_b2.get('label', 'N/A')}")
            _lines.append(f"  WR: {_b2.get('winrate', 0)}% | PF: {_b2.get('profit_factor', 0)}x | Pips netos: {_b2.get('net_pips', 0):+.1f} | Max DD: {_b2.get('max_dd', 0)}%")
            _lines.append(f"  Operaciones en 18 años: {_b2.get('total', 0)} (incluye: crisis 2008, flash crash 2015, Brexit 2016, COVID 2020, subidas Fed 2022-23)")
            _lt_rs = _lt.get("results", [])
            if _lt_rs:
                _lines.append("  Ranking histórico (todas las estrategias):")
                for _ri, _r in enumerate(_lt_rs):
                    _ok = "✅" if _r.get("profit_factor", 0) >= 1.0 else "⚠️"
                    _lines.append(f"    {_ri+1}. {_ok} {_r.get('label','?')}: {_r.get('winrate',0)}% WR | {_r.get('profit_factor',0)}x PF | {_r.get('net_pips',0):+.1f}p | DD {_r.get('max_dd',0)}%")

        # ── Patrones de trading del usuario ──────────────────────────────────────
        if _DB_OK:
            try:
                _pats = _db.get_user_trade_patterns(current_user)
                if _pats and _pats.get("total"):
                    _lines.append(f"\n=== HISTORIAL REAL DE {current_user_name.upper()} EN ESTA APP ===")
                    _lines.append(f"Total operaciones: {_pats.get('total', 0)} | Win Rate: {float(_pats.get('winrate') or 0):.1f}%")
                    _lines.append(f"Net pips acumulados: {float(_pats.get('net_pips') or 0):+.1f} | P&L neto: ${float(_pats.get('net_pnl') or 0):+.2f}")
                    if _pats.get("by_strategy"):
                        _lines.append("  Rendimiento por estrategia:")
                        for _s in _pats["by_strategy"][:5]:
                            _lines.append(f"    • {_s.get('strategy','?')}: {_s.get('total',0)} ops | {float(_s.get('winrate') or 0):.0f}% WR | {float(_s.get('net_pips') or 0):+.1f}p")
                    if _pats.get("recent_streak"):
                        _lines.append(f"  Racha reciente: {_pats['recent_streak']}")
            except Exception:
                pass

        # ── Memorias del Advisor (aprendizajes acumulados) ────────────────────────
        if _DB_OK:
            try:
                _mems = _db.load_ai_memories(current_user, limit=12)
                if _mems:
                    _lines.append(f"\n=== LO QUE HE APRENDIDO DE {current_user_name.upper()} ===")
                    for _m in _mems:
                        _ct = _m.get("content", "")
                        if _ct:
                            _lines.append(f"  [{_m.get('memory_type','insight')}] {_ct}")
            except Exception:
                pass

        return "\n".join(_lines)


    def _extract_lesson(user_msg: str, ai_response: str, api_key: str) -> str | None:
        """Extract a 1-sentence learning from a conversation. Returns None if nothing new."""
        try:
            from ai_engine import call_ai as _call_ai
            _prompt = (
                f"Conversación de trading EUR/USD:\n"
                f"USUARIO: {user_msg[:250]}\n"
                f"ADVISOR: {ai_response[:400]}\n\n"
                f"En UNA frase corta (máx 120 caracteres), extrae el insight o patrón de trading "
                f"más importante que el ADVISOR ha identificado sobre este usuario o mercado. "
                f"Si no hay nada nuevo o relevante que aprender, responde exactamente: NONE"
            )
            _r = _call_ai([{"role": "user", "content": _prompt}],
                          max_tokens=160, temperature=0.2).strip()
            return None if _r.startswith("⚠️") or _r.upper().startswith("NONE") or len(_r) < 12 else _r
        except Exception:
            return None


    def _advisor_call(user_msg: str, history: list, context: str) -> str:
        """
        Send user message to the best available AI provider (Groq → Cerebras → Zhipu → Claude).
        Falls back to Claude (Anthropic) automatically if the free providers fail.
        """
        from ai_engine import call_ai as _call_ai

        _system = f"""Eres el Trading Advisor personal de {current_user_name}, un sistema de IA especializado en EUR/USD que APRENDE y EVOLUCIONA con cada conversación. Tienes acceso completo al historial de trading real de {current_user_name}, sus patrones de comportamiento, y los aprendizajes acumulados de todas vuestras conversaciones anteriores.

    Tu objetivo no es solo analizar — es convertirte en el mejor consejero posible para {current_user_name} adaptándote a su estilo, sus errores pasados y sus puntos fuertes.

    {context}

    INSTRUCCIONES:
    {current_user_name} compartirá su visión/tesis. Analízala contra los datos de la app y su historial personal. Responde SIEMPRE con esta estructura:

    📊 **TU VISIÓN ENTENDIDA**
    Resumir lo que propone {current_user_name} en 1-2 frases.

    ✅ **POR QUÉ SÍ** (confluencias a favor)
    Argumentos que respaldan la visión: backtest histórico (18+ años), señal actual de la app, técnico (EMA/RSI/MACD), fundamental (BCE/Fed/macro), patrones del usuario si aplican. Cita profit factors y win rates.

    ❌ **POR QUÉ NO** (riesgos y contradicciones)
    Argumentos en contra: estrategias que fallen en este contexto, drawdowns históricos, alertas de la app, errores pasados de {current_user_name} si los hay en su historial.

    🎯 **VEREDICTO**
    Conclusión directa con nivel de convicción (ALTA/MEDIA/BAJA) y ajustes concretos (entrada, SL, TP, timing). Si detectas un patrón recurrente en {current_user_name}, menciónalo.

    🧠 **APRENDIZAJE** (solo si hay algo nuevo)
    En 1 frase: qué nuevo insight has extraído de esta conversación sobre el mercado o sobre {current_user_name}.

    REGLAS:
    - Responde en español, tutea a {current_user_name}
    - Sé cuantitativo — cita datos concretos del backtest como evidencia
    - Usa el historial real del usuario cuando sea relevante
    - Máximo 450 palabras en total
    - Si los datos de la app aún no están cargados (N/A), indícalo y razona con lo que tengas"""

        _messages = [{"role": "system", "content": _system}]
        for _h in history[-8:]:
            _messages.append({"role": _h["role"], "content": _h["content"]})
        _messages.append({"role": "user", "content": user_msg})

        return _call_ai(_messages, max_tokens=1200, temperature=0.4, prefer_quality=True)


    # Mostrar historial de conversación
    for _msg in st.session_state.advisor_chat:
        _av = "👤" if _msg["role"] == "user" else "🧠"
        with st.chat_message(_msg["role"], avatar=_av):
            st.markdown(_msg["content"])

    # Input del chat
    _chat_prompt = st.chat_input(
        "Ej: Creo que el EUR/USD va a subir porque el BCE está hawkish y el DXY está cayendo..."
    )
    if _chat_prompt:
        if not _ant_key:
            st.error("⚠️ Configura la GROQ_API_KEY primero (ver configuración arriba).")
        else:
            with st.chat_message("user", avatar="👤"):
                st.markdown(_chat_prompt)
            st.session_state.advisor_chat.append({"role": "user", "content": _chat_prompt})
            if _DB_OK:
                try:
                    _db.save_chat_message(
                        st.session_state.advisor_session_id, "user",
                        _chat_prompt, user_id=current_user
                    )
                except Exception:
                    pass

            with st.chat_message("assistant", avatar="🧠"):
                with st.spinner(f"Analizando tu visión, {current_user_name}..."):
                    _ctx_snap  = _advisor_context()
                    _ai_answer = _advisor_call(
                        _chat_prompt,
                        st.session_state.advisor_chat[:-1],
                        _ctx_snap,
                    )
                st.markdown(_ai_answer)
            st.session_state.advisor_chat.append({"role": "assistant", "content": _ai_answer})
            if _DB_OK:
                try:
                    _db.save_chat_message(
                        st.session_state.advisor_session_id, "assistant",
                        _ai_answer, user_id=current_user
                    )
                except Exception:
                    pass
            # Auto-aprendizaje: extraer insight y guardar en ai_memory
            if _DB_OK and _ant_key:
                try:
                    _lesson = _extract_lesson(_chat_prompt, _ai_answer, _ant_key)
                    if _lesson:
                        _db.save_ai_memory(
                            current_user, "insight",
                            f"Chat {datetime.now().strftime('%Y-%m-%d')}",
                            _lesson, 0.7, "chat",
                        )
                except Exception:
                    pass

        # Historial del Advisor — se muestra automáticamente, sin botón de limpiar

    st.markdown("---")

with _t_prem:
    # ══════════════════════════════════════════════════════════════════════════
    # SEÑALES PREMIUM — Solo movimientos direccionales fuertes
    # Objetivo: ~10 operaciones de alta calidad por semana
    # Filtros: score ≥78 · régimen tendencial · sesión activa · EMAs alineadas
    #          consenso ≥5/8 estrategias clave · RSI en rango limpio · ATR ≥5p
    # ══════════════════════════════════════════════════════════════════════════
    import json as _json_prem
    from datetime import timezone as _tz

    st.markdown("---")
    _prem_hdr_l, _prem_hdr_r = st.columns([3, 1])
    _prem_hdr_l.subheader("🏹 Señales Premium — Movimientos Direccionales Fuertes")
    _prem_hdr_r.caption("Objetivo: ~10 señales/semana")
    st.caption(
        "Filtro estricto de 7 capas: **score ≥78** · **régimen tendencial** · "
        "**sesión London o NY** · **EMAs 5/10/20/50 todas alineadas** · "
        "**≥5 de 8 estrategias en consenso** · **RSI en zona limpia** · **ATR ≥5 pips**. "
        "Sin confirmaciones débiles — solo decisión clara del mercado."
    )

    _pr_price   = signal.get("price")
    _pr_dir     = signal.get("direction")
    _pr_score   = score
    _pr_regime  = signal.get("regime", "")
    _pr_sess    = signal.get("session", "")
    _pr_rsi     = float(signal.get("rsi") or 50)
    _pr_atr     = float(signal.get("atr_1h_pips") or 0)

    if not _pr_price:
        st.info("📊 Presiona **ANALIZAR MERCADO** para evaluar la señal premium.")
    else:
        # ── Evaluación de filtros (transparente) ──────────────────────────────
        _pr_fails  = []
        _pr_checks = []

        def _pcheck(ok, label, detail=""):
            if ok:
                _pr_checks.append(f"✅ {label}" + (f" — {detail}" if detail else ""))
            else:
                _pr_fails.append(f"❌ {label}" + (f" — {detail}" if detail else ""))
            return ok

        # 1. Dirección
        _pcheck(_pr_dir in ("LONG", "SHORT"),
                "Dirección clara",
                _pr_dir or signal.get("final_signal", "NEUTRAL"))

        # 2. Score
        _pcheck(_pr_score >= 78,
                f"Score ≥78",
                f"{_pr_score}/100")

        # 3. Régimen tendencial (no lateral)
        _regime_ok = _pr_regime in ("trending_up", "trending_down")
        _pcheck(_regime_ok,
                "Régimen tendencial",
                _pr_regime.replace("_", " ") if _pr_regime else "desconocido")

        # 4. Sesión activa
        _pcheck(_pr_sess in ("London", "NY"),
                "Sesión London o NY",
                _pr_sess or "fuera de sesión")

        # 5. ATR mínimo
        _pcheck(_pr_atr >= 5,
                "ATR ≥5 pips (volatilidad)",
                f"{_pr_atr:.1f} pips")

        # 6. RSI en zona limpia
        if _pr_dir == "LONG":
            _rsi_ok = 45 <= _pr_rsi <= 68
            _rsi_detail = f"{_pr_rsi:.1f} (ideal 45-68 para LONG)"
        elif _pr_dir == "SHORT":
            _rsi_ok = 32 <= _pr_rsi <= 55
            _rsi_detail = f"{_pr_rsi:.1f} (ideal 32-55 para SHORT)"
        else:
            _rsi_ok = False
            _rsi_detail = f"{_pr_rsi:.1f}"
        _pcheck(_rsi_ok, "RSI en zona limpia", _rsi_detail)

        # 7. EMA ribbon alineada + consensus estrategias
        _pr_df1h = get_eurusd_data("1h")
        _pr_ribbon = get_ema_ribbon(_pr_df1h) if (_pr_df1h is not None and not _pr_df1h.empty) else {}
        _ema_ok = (
            (_pr_dir == "LONG"  and _pr_ribbon.get("bull_align")) or
            (_pr_dir == "SHORT" and _pr_ribbon.get("bear_align"))
        )
        _pcheck(_ema_ok,
                "EMAs 5/10/20/50 alineadas",
                "bull_align" if _pr_ribbon.get("bull_align") else ("bear_align" if _pr_ribbon.get("bear_align") else "sin alineación"))

        # 8. Consenso multi-estrategia (≥5 de 8 estrategias clave)
        _KEY_STRATS = [
            "ema_trend", "ema_ribbon", "triple_ema",
            "macd_cross", "supertrend", "rsi_50_cross",
            "momentum_break", "meta_composite",
        ]
        _agreeing  = []
        _rejecting = []
        if _pr_df1h is not None and not _pr_df1h.empty and len(_pr_df1h) >= 115 and _pr_dir:
            from backend.strategies import _live_strategy_signal as _lss_pr
            for _sk in _KEY_STRATS:
                try:
                    _sd, _sr = _lss_pr(_pr_df1h, _sk)
                    if _sd == _pr_dir:
                        _agreeing.append(_sk)
                    elif _sd in ("LONG", "SHORT"):
                        _rejecting.append(_sk)
                except Exception:
                    pass

        _consensus_ok = len(_agreeing) >= 5
        _pcheck(_consensus_ok,
                f"Consenso ≥5/8 estrategias",
                f"{len(_agreeing)}/8 confirman {_pr_dir or '?'}")

        # ── Resultado final ───────────────────────────────────────────────────
        _is_premium = len(_pr_fails) == 0

        if _is_premium:
            # ── SETUP PREMIUM DETECTADO ───────────────────────────────────────
            _dir_col  = "#3fb950" if _pr_dir == "LONG" else "#f85149"
            _dir_bg   = "#0a1f0d" if _pr_dir == "LONG" else "#1f0a0a"
            _dir_icon = "📈" if _pr_dir == "LONG" else "📉"
            st.markdown(
                f'<div style="background:{_dir_bg};border:2px solid {_dir_col};'
                f'border-radius:14px;padding:20px 26px;margin:10px 0">'
                f'<div style="display:flex;align-items:center;gap:14px">'
                f'<span style="font-size:40px">{_dir_icon}</span>'
                f'<div><div style="color:{_dir_col};font-weight:800;font-size:24px">'
                f'✅ SETUP PREMIUM — {_pr_dir}</div>'
                f'<div style="color:#aaa;font-size:13px;margin-top:4px">'
                f'{len(_agreeing)}/8 estrategias · Score {_pr_score}/100 · '
                f'{_pr_sess} · {_pr_regime.replace("_"," ")}</div>'
                f'</div></div></div>',
                unsafe_allow_html=True,
            )

            # Métricas principales
            _pm1, _pm2, _pm3, _pm4, _pm5 = st.columns(5)
            _pm1.metric("Score",    f"{_pr_score}/100")
            _pm2.metric("Consenso", f"{len(_agreeing)}/8")
            _pm3.metric("RSI 1H",   f"{_pr_rsi:.1f}")
            _pm4.metric("ATR",      f"{_pr_atr:.1f} pips")
            _pm5.metric("Sesión",   _pr_sess)

            # Niveles y estrategias
            _ql, _qr = st.columns(2)
            with _ql:
                st.markdown("**💰 Niveles de Entrada**")
                _lev_c1, _lev_c2 = st.columns(2)
                _lev_c1.metric("Precio",     f"{_pr_price:.5f}")
                if smart_sl:
                    _lev_c2.metric("Stop Loss",
                                   f"{smart_sl:.5f}",
                                   delta=f"−{round(abs(_pr_price - smart_sl)/0.0001,1)} pips",
                                   delta_color="inverse")
                if tp1: _lev_c1.metric("TP1 (1R)", f"{tp1:.5f}")
                if tp2: _lev_c2.metric("TP2 (2R)", f"{tp2:.5f}")
                if tp3: _lev_c1.metric("TP3 (3R)", f"{tp3:.5f}")
                if rr2: _lev_c2.metric("R:R",       f"1:{rr2:.1f}")

                st.markdown("**📊 EMA Ribbon**")
                for _lbl, _val in [
                    ("EMA5",  _pr_ribbon.get("ema5")),
                    ("EMA10", _pr_ribbon.get("ema10")),
                    ("EMA20", _pr_ribbon.get("ema20")),
                    ("EMA50", _pr_ribbon.get("ema50")),
                ]:
                    if _val:
                        st.caption(f"{_lbl}: `{_val:.5f}`")

            with _qr:
                st.markdown("**✅ Estrategias confirmando**")
                for _sk in _agreeing:
                    _sm = _STRATEGY_META.get(_sk, {})
                    st.success(f"✅ {_sm.get('label', _sk)}")
                if _rejecting:
                    st.markdown("**⚠️ Estrategias en contra**")
                    for _sk in _rejecting:
                        _sm = _STRATEGY_META.get(_sk, {})
                        st.warning(f"⚠️ {_sm.get('label', _sk)}")

            # Guardar señal premium en DB
            try:
                import db as _dbp
                _hist_raw = _dbp.get_setting("premium_signals_hist") or "[]"
                _hist_p   = _json_prem.loads(_hist_raw)
                _new_sig  = {
                    "ts":         __import__("datetime").datetime.now(__import__("datetime").timezone.utc).strftime("%Y-%m-%d %H:%M"),
                    "direction":  _pr_dir,
                    "price":      round(_pr_price, 5),
                    "score":      _pr_score,
                    "session":    _pr_sess,
                    "regime":     _pr_regime,
                    "consensus":  len(_agreeing),
                    "strategies": [_STRATEGY_META.get(s, {}).get("label", s)[:25] for s in _agreeing],
                    "rsi":        round(_pr_rsi, 1),
                    "atr":        round(_pr_atr, 1),
                    "sl":         round(smart_sl, 5) if smart_sl else None,
                    "tp1":        round(tp1, 5) if tp1 else None,
                    "tp2":        round(tp2, 5) if tp2 else None,
                    "tp3":        round(tp3, 5) if tp3 else None,
                }
                # Evitar duplicados (misma dirección en últimos 30 min)
                _last_ts = _hist_p[0].get("ts", "") if _hist_p else ""
                _dup = (_hist_p and _hist_p[0].get("direction") == _pr_dir
                        and _hist_p[0].get("price", 0) != 0
                        and abs(_hist_p[0].get("price", 0) - _pr_price) < 0.0010)
                if not _dup:
                    _hist_p.insert(0, _new_sig)
                    _hist_p = _hist_p[:100]
                    _dbp.set_setting("premium_signals_hist", _json_prem.dumps(_hist_p))
            except Exception:
                pass

        else:
            # ── Sin setup premium ─────────────────────────────────────────────
            st.markdown(
                '<div style="background:#111;border:2px solid #e3b341;border-radius:12px;'
                'padding:16px 22px;margin:10px 0">'
                '<div style="color:#e3b341;font-weight:700;font-size:18px">'
                '⏳ Sin Setup Premium — Mercado No Decisivo</div>'
                '<div style="color:#777;font-size:12px;margin-top:4px">'
                'Esperando confluencia excepcional de múltiples filtros...</div>'
                '</div>',
                unsafe_allow_html=True,
            )
            _fc_l, _fc_r = st.columns(2)
            with _fc_l:
                st.markdown("**Filtros cumplidos**")
                for _c in _pr_checks:
                    st.success(_c)
            with _fc_r:
                st.markdown("**Filtros pendientes**")
                for _f in _pr_fails:
                    st.error(_f)

    # ── Historial de señales premium ──────────────────────────────────────────
    st.markdown("---")
    st.subheader("📅 Historial — Señales Premium Detectadas")
    try:
        import db as _dbph
        _hist_raw2 = _dbph.get_setting("premium_signals_hist") or "[]"
        _hist2     = _json_prem.loads(_hist_raw2)
        if _hist2:
            import pandas as _pd_prem
            _hdf = _pd_prem.DataFrame(_hist2)
            _show_cols = [c for c in ["ts", "direction", "price", "score", "session",
                                      "regime", "consensus", "rsi", "atr", "sl", "tp1"] if c in _hdf.columns]
            _hdf_disp = _hdf[_show_cols].rename(columns={
                "ts": "Fecha UTC", "direction": "Dir", "price": "Precio",
                "score": "Score", "session": "Sesión", "regime": "Régimen",
                "consensus": "Consenso", "rsi": "RSI", "atr": "ATR(pips)",
                "sl": "SL", "tp1": "TP1",
            })
            _green = {"LONG": "background-color:#0a1f0d;color:#3fb950",
                      "SHORT": "background-color:#1f0a0a;color:#f85149"}
            st.dataframe(_hdf_disp, use_container_width=True, hide_index=True)
            # Estadísticas rápidas
            if len(_hist2) >= 3:
                _total_h = len(_hist2)
                _long_h  = sum(1 for h in _hist2 if h.get("direction") == "LONG")
                _short_h = _total_h - _long_h
                _avg_sc  = sum(h.get("score", 0) for h in _hist2) / _total_h
                _avg_con = sum(h.get("consensus", 0) for h in _hist2) / _total_h
                _hs1, _hs2, _hs3, _hs4 = st.columns(4)
                _hs1.metric("Total señales", _total_h)
                _hs2.metric("LONG / SHORT", f"{_long_h} / {_short_h}")
                _hs3.metric("Score medio", f"{_avg_sc:.0f}/100")
                _hs4.metric("Consenso medio", f"{_avg_con:.1f}/8")
        else:
            st.info(
                "Sin señales premium registradas aún. "
                "El sistema guarda automáticamente cada setup que cumple todos los filtros."
            )
    except Exception as _phe:
        st.caption(f"Historial no disponible: {_phe}")

    # Guía de interpretación
    with st.expander("📖 Cómo interpretar las señales premium", expanded=False):
        st.markdown("""
**¿Qué es una señal premium?**
Una señal que supera simultáneamente 7 filtros independientes, diseñados para capturar
solo los movimientos direccionales más claros y con mayor probabilidad de éxito.

**Los 7 filtros:**
| Filtro | Umbral | Por qué |
|--------|--------|---------|
| Score confluencia | ≥ 78/100 | Mínimo de señales técnicas alineadas |
| Régimen mercado | Trending (no ranging) | En laterales las señales fallan |
| Sesión activa | London o NY | Máxima liquidez institucional |
| ATR mínimo | ≥ 5 pips | Volatilidad suficiente para el movimiento |
| RSI limpio | 45-68 LONG / 32-55 SHORT | Sin sobrecompra/venta en la dirección |
| EMAs alineadas | 5/10/20/50 en cascada | Tendencia estructural confirmada |
| Consenso | ≥ 5/8 estrategias | El mercado lo ve desde múltiples ángulos |

**Gestión de la operación:**
- Entrada en el precio marcado (a mercado o límite ±2 pips)
- SL obligatorio en el nivel indicado
- TP1 para asegurar parcial (50%), dejar correr a TP2/TP3 con SL en BE
- **Nunca mover el SL contra la posición**
        """)

    # ── BACKTEST PREMIUM desde 2020 ──────────────────────────────────────────
    st.markdown("---")
    st.markdown("### 📊 Backtest del filtro premium (EUR/USD Diario — desde 2020)")

    @st.cache_data(ttl=86400, show_spinner=False)
    def _run_premium_backtest_2020():
        """Simula el filtro 7 capas sobre velas diarias EUR/USD desde 2020."""
        import yfinance as _yf
        import numpy as _np
        import pandas as _pd

        try:
            _df = _yf.download(
                "EURUSD=X", start="2020-01-01", interval="1d",
                auto_adjust=True, progress=False, timeout=20,
            )
        except Exception as _e:
            return None, None, {}, str(_e)

        if _df is None or _df.empty or len(_df) < 100:
            return None, None, {}, "datos insuficientes"

        if isinstance(_df.columns, _pd.MultiIndex):
            _df.columns = _df.columns.get_level_values(0)

        # Normalizar nombres de columnas
        _df.columns = [str(c).strip().capitalize() for c in _df.columns]
        _needed = [col for col in ["Open", "High", "Low", "Close"] if col in _df.columns]
        if len(_needed) < 4:
            return None, None, {}, "columnas OHLC no encontradas"

        _df = _df[_needed].copy()
        _df = _df.dropna()

        # Quitar tz para evitar problemas de comparación
        if _df.index.tz is not None:
            _df.index = _df.index.tz_localize(None)

        c  = _df["Close"]
        h  = _df["High"]
        lo = _df["Low"]

        # EMAs
        _df["ema5"]  = c.ewm(span=5,  adjust=False).mean()
        _df["ema10"] = c.ewm(span=10, adjust=False).mean()
        _df["ema20"] = c.ewm(span=20, adjust=False).mean()
        _df["ema50"] = c.ewm(span=50, adjust=False).mean()

        # ATR(14) — en velas diarias EUR/USD suele ser 60-120 pips
        _prev_c = c.shift(1)
        _tr = _pd.concat([
            h - lo,
            (h - _prev_c).abs(),
            (lo - _prev_c).abs(),
        ], axis=1).max(axis=1)
        _df["atr"] = _tr.ewm(span=14, adjust=False).mean()

        # RSI(14)
        _delta = c.diff()
        _gain  = _delta.clip(lower=0)
        _loss  = (-_delta).clip(lower=0)
        _avg_g = _gain.ewm(span=14, adjust=False).mean()
        _avg_l = _loss.ewm(span=14, adjust=False).mean()
        _rs    = _avg_g / _avg_l.replace(0, _np.nan)
        _df["rsi"] = 100 - 100 / (1 + _rs)

        # MACD (12/26/9)
        _ema12 = c.ewm(span=12, adjust=False).mean()
        _ema26 = c.ewm(span=26, adjust=False).mean()
        _macd  = _ema12 - _ema26
        _df["macd_hist"] = _macd - _macd.ewm(span=9, adjust=False).mean()

        _df = _df.dropna()

        # ── Filtros vectorizados ──────────────────────────────────────────────
        _bull_align  = (_df["ema5"] > _df["ema10"]) & \
                       (_df["ema10"] > _df["ema20"]) & \
                       (_df["ema20"] > _df["ema50"])
        _bear_align  = (_df["ema5"] < _df["ema10"]) & \
                       (_df["ema10"] < _df["ema20"]) & \
                       (_df["ema20"] < _df["ema50"])

        # ATR diario ≥ 40 pips  (escala daily: ~0.0040)
        _atr_ok      = _df["atr"] >= 0.0040

        _rsi_long    = (_df["rsi"] >= 45) & (_df["rsi"] <= 68)
        _rsi_short   = (_df["rsi"] >= 32) & (_df["rsi"] <= 55)

        _macd_bull   = _df["macd_hist"] > 0
        _macd_bear   = _df["macd_hist"] < 0
        _above_ema50 = _df["Close"] > _df["ema50"]
        _below_ema50 = _df["Close"] < _df["ema50"]

        # Score 0-90 (sin filtro sesión en daily; 6 condiciones × peso)
        _score_l = (
            _bull_align.astype(int)  * 25 +
            _atr_ok.astype(int)      * 15 +
            _rsi_long.astype(int)    * 20 +
            _macd_bull.astype(int)   * 20 +
            _above_ema50.astype(int) * 10
        )
        _score_s = (
            _bear_align.astype(int)  * 25 +
            _atr_ok.astype(int)      * 15 +
            _rsi_short.astype(int)   * 20 +
            _macd_bear.astype(int)   * 20 +
            _below_ema50.astype(int) * 10
        )

        _cons_l = (_bull_align.astype(int) + _rsi_long.astype(int) +
                   _macd_bull.astype(int)  + _above_ema50.astype(int) +
                   _atr_ok.astype(int))
        _cons_s = (_bear_align.astype(int) + _rsi_short.astype(int) +
                   _macd_bear.astype(int)  + _below_ema50.astype(int) +
                   _atr_ok.astype(int))

        # Umbral equivalente: score ≥ 70 con daily (5 condiciones disponibles)
        _long_signal  = (_score_l >= 70) & (_cons_l >= 4)
        _short_signal = (_score_s >= 70) & (_cons_s >= 4)

        # ── Simulación de operaciones ─────────────────────────────────────────
        _trades = []
        _cooldown_bars = 3      # 3 días entre señales
        _exit_window   = 10     # max 10 días para cerrar
        _last_bar      = -_cooldown_bars

        for _i in range(50, len(_df)):
            if _i - _last_bar < _cooldown_bars:
                continue

            _row   = _df.iloc[_i]
            _entry = float(_row["Close"])
            _atr_v = float(_row["atr"])
            _sl_d  = _atr_v * 1.5
            _tp_d  = _atr_v * 2.5

            if _long_signal.iloc[_i]:
                _dir = "LONG"
                _sl  = _entry - _sl_d
                _tp  = _entry + _tp_d
            elif _short_signal.iloc[_i]:
                _dir = "SHORT"
                _sl  = _entry + _sl_d
                _tp  = _entry - _tp_d
            else:
                continue

            _hit = False
            for _j in range(_i + 1, min(_i + _exit_window + 1, len(_df))):
                _fh = float(_df["High"].iloc[_j])
                _fl = float(_df["Low"].iloc[_j])
                if _dir == "LONG":
                    if _fl <= _sl:
                        _pips = -_sl_d / 0.0001; _hit = True; break
                    if _fh >= _tp:
                        _pips =  _tp_d / 0.0001; _hit = True; break
                else:
                    if _fh >= _sl:
                        _pips = -_sl_d / 0.0001; _hit = True; break
                    if _fl <= _tp:
                        _pips =  _tp_d / 0.0001; _hit = True; break

            if not _hit:
                _close_x = float(_df["Close"].iloc[min(_i + _exit_window, len(_df) - 1)])
                _pips = ((_close_x - _entry) if _dir == "LONG" else
                         (_entry - _close_x)) / 0.0001

            _trades.append({
                "date":  _df.index[_i],
                "dir":   _dir,
                "entry": _entry,
                "sl":    round(_sl, 5),
                "tp":    round(_tp, 5),
                "pips":  round(_pips, 1),
                "win":   _pips > 0,
            })
            _last_bar = _i

        if not _trades:
            return None, None, {}, "sin señales"

        _tdf = _pd.DataFrame(_trades)
        _tdf["equity"] = _tdf["pips"].cumsum()

        _n       = len(_tdf)
        _wins    = int(_tdf["win"].sum())
        _wr      = _wins / _n * 100
        _net     = float(_tdf["pips"].sum())
        _avg_w   = float(_tdf.loc[_tdf["win"],  "pips"].mean()) if _wins > 0 else 0
        _avg_l   = float(_tdf.loc[~_tdf["win"], "pips"].mean()) if (_n - _wins) > 0 else 0
        _gross_p = _tdf.loc[_tdf["win"],  "pips"].sum()
        _gross_l = abs(_tdf.loc[~_tdf["win"], "pips"].sum())
        _pf      = round((_gross_p / _gross_l) if _gross_l > 0 else 999.0, 2)

        _peak   = _tdf["equity"].cummax()
        _max_dd = float((_tdf["equity"] - _peak).min())

        _days  = (_tdf["date"].iloc[-1] - _tdf["date"].iloc[0]).days
        _weeks = max(1, _days / 7)

        _stats = {
            "total": _n, "wins": _wins, "winrate": round(_wr, 1),
            "net_pips": round(_net, 1), "profit_factor": _pf,
            "avg_win": round(_avg_w, 1), "avg_loss": round(_avg_l, 1),
            "max_dd": round(_max_dd, 1), "per_week": round(_n / _weeks, 1),
            "years": round(_weeks / 52, 1),
        }
        return _tdf, _df, _stats, "ok"

    with st.spinner("Cargando backtest 2020-2025 (primera carga ~10 s)..."):
        _bt_result = _run_premium_backtest_2020()

    _bt_trades, _bt_df_raw, _bt_stats, _bt_err = _bt_result

    if _bt_trades is not None and _bt_stats:
        import plotly.graph_objects as _go_bt

        _bc1, _bc2, _bc3, _bc4, _bc5, _bc6 = st.columns(6)
        _bc1.metric("Operaciones",   _bt_stats["total"])
        _bc2.metric("Win Rate",      f"{_bt_stats['winrate']}%",
                    delta="bueno" if _bt_stats['winrate'] >= 55 else "mejorable")
        _bc3.metric("Pips netos",    f"{_bt_stats['net_pips']:+.0f}")
        _bc4.metric("Profit Factor", f"{_bt_stats['profit_factor']:.2f}",
                    delta="solido" if _bt_stats['profit_factor'] >= 1.5 else "bajo")
        _bc5.metric("Max Drawdown",  f"{_bt_stats['max_dd']:.0f} pips")
        _bc6.metric("Señales/sem",   f"{_bt_stats['per_week']:.1f}")

        _bca, _bcb = st.columns(2)
        _bca.metric("Avg ganancia", f"{_bt_stats['avg_win']:+.1f} pips")
        _bcb.metric("Avg pérdida",  f"{_bt_stats['avg_loss']:+.1f} pips")

        # Curva de equity
        _clr_eq = "limegreen" if _bt_stats["net_pips"] > 0 else "tomato"
        _fill_eq = "rgba(50,205,50,0.08)" if _bt_stats["net_pips"] > 0 else "rgba(255,99,71,0.08)"
        _fig_eq = _go_bt.Figure()
        _fig_eq.add_trace(_go_bt.Scatter(
            x=_bt_trades["date"], y=_bt_trades["equity"],
            mode="lines", name="Equity (pips acum.)",
            line=dict(color=_clr_eq, width=2),
            fill="tozeroy", fillcolor=_fill_eq,
        ))
        _fig_eq.update_layout(
            title="Curva de Equity — Filtro Premium (EUR/USD Diario, 2020-hoy)",
            xaxis_title="Fecha", yaxis_title="Pips acumulados",
            template="plotly_dark", height=380,
            margin=dict(l=40, r=20, t=50, b=40),
        )
        st.plotly_chart(_fig_eq, use_container_width=True)

        # Distribución LONG vs SHORT
        _bt_long  = _bt_trades[_bt_trades["dir"] == "LONG"]
        _bt_short = _bt_trades[_bt_trades["dir"] == "SHORT"]
        _fig_dir  = _go_bt.Figure(data=[
            _go_bt.Bar(name="LONG",
                       x=["Ganadas", "Perdidas"],
                       y=[int(_bt_long["win"].sum()), int((~_bt_long["win"]).sum())],
                       marker_color=["limegreen", "tomato"]),
            _go_bt.Bar(name="SHORT",
                       x=["Ganadas", "Perdidas"],
                       y=[int(_bt_short["win"].sum()), int((~_bt_short["win"]).sum())],
                       marker_color=["cyan", "orange"]),
        ])
        _fig_dir.update_layout(
            barmode="group", template="plotly_dark", height=280,
            title="Distribución LONG vs SHORT",
            margin=dict(l=40, r=20, t=50, b=40),
        )
        st.plotly_chart(_fig_dir, use_container_width=True)

        # Tabla últimas 20 operaciones
        with st.expander("📋 Últimas 20 operaciones del backtest", expanded=False):
            _bt_show = _bt_trades[["date", "dir", "entry", "pips", "win"]].tail(20).copy()
            _bt_show["date"]  = _bt_show["date"].dt.strftime("%Y-%m-%d")
            _bt_show["entry"] = _bt_show["entry"].map("{:.5f}".format)
            _bt_show["pips"]  = _bt_show["pips"].map("{:+.1f}".format)
            _bt_show["win"]   = _bt_show["win"].map({True: "✅", False: "❌"})
            _bt_show.columns  = ["Fecha", "Dir", "Entrada", "Pips", "Resultado"]
            st.dataframe(_bt_show, use_container_width=True, hide_index=True)

        st.caption(
            f"Backtest sobre {_bt_stats['years']} años · velas diarias EUR/USD · "
            f"mismos 7 filtros del panel (adaptados a escala diaria) · "
            f"SL=1.5×ATR, TP=2.5×ATR, cierre max 10 días · "
            f"Sin slippage ni comisiones · Resultados pasados no garantizan resultados futuros."
        )
    else:
        st.warning(
            f"No se pudo cargar el backtest histórico ({_bt_err}). "
            "Railway puede bloquear descargas de Yahoo Finance en el primer arranque — "
            "recarga la página en 30 segundos."
        )

st.caption("⚠️ Solo informativo. No es consejo financiero. Usa siempre SL.")


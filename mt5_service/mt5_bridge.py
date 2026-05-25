"""
mt5_bridge.py
─────────────
Capa de abstracción sobre la librería MetaTrader5.
Centraliza la conexión, el login y las operaciones de trading
para que api_server.py se mantenga limpio.

Diseño de seguridad (alineado con CLAUDE.md):
  - stop_loss es OBLIGATORIO en toda orden
  - riesgo por operación limitado a MAX_RISK_PCT
  - las credenciales vienen SOLO de variables de entorno
"""

import os
import time
import logging
from typing import Optional, Tuple, Dict, Any

logger = logging.getLogger(__name__)

# ─── Constantes de riesgo ─────────────────────────────────────────
MAX_RISK_PCT   = float(os.getenv("MT5_MAX_RISK_PCT", "1.0"))   # 1% por operación
DAILY_LOSS_PCT = float(os.getenv("MT5_DAILY_LOSS_PCT", "3.0")) # circuit breaker diario
# ─────────────────────────────────────────────────────────────────

_mt5 = None
_connected = False
_daily_loss_triggered = False
_day_start_balance: Optional[float] = None


def _load_mt5():
    global _mt5
    if _mt5 is None:
        try:
            import MetaTrader5 as mt5
            _mt5 = mt5
        except ImportError as e:
            logger.error(f"No se puede importar MetaTrader5: {e}")
            _mt5 = False
    return _mt5


def connect(login: Optional[int] = None,
            password: Optional[str] = None,
            server: Optional[str] = None) -> Tuple[bool, str]:
    """
    Inicializa MT5 y hace login.
    Si login/password/server son None, lee MT5_LOGIN / MT5_PASSWORD / MT5_SERVER
    desde variables de entorno.
    """
    global _connected, _day_start_balance

    mt5 = _load_mt5()
    if not mt5:
        return False, "MetaTrader5 no disponible"

    if _connected:
        return True, "Ya conectado"

    login    = login    or int(os.getenv("MT5_LOGIN", "0"))
    password = password or os.getenv("MT5_PASSWORD", "")
    server   = server   or os.getenv("MT5_SERVER", "")

    if not mt5.initialize():
        return False, f"mt5.initialize() falló: {mt5.last_error()}"

    if login and password and server:
        ok = mt5.login(login=login, password=password, server=server)
        if not ok:
            mt5.shutdown()
            return False, f"Login fallido: {mt5.last_error()}"

    _connected = True
    info = mt5.account_info()
    if info:
        _day_start_balance = info.balance
        logger.info(f"MT5 conectado — cuenta {info.login}, balance {info.balance}")
    return True, "Conectado"


def disconnect():
    global _connected
    mt5 = _load_mt5()
    if mt5 and _connected:
        mt5.shutdown()
        _connected = False


def _check_daily_loss() -> Tuple[bool, str]:
    """Circuit breaker: detiene trading si el drawdown diario supera DAILY_LOSS_PCT."""
    global _daily_loss_triggered
    if _daily_loss_triggered:
        return False, f"Circuit breaker activo: pérdida diaria >{DAILY_LOSS_PCT}% alcanzada"

    mt5 = _load_mt5()
    if not mt5 or not _connected or _day_start_balance is None:
        return True, ""

    info = mt5.account_info()
    if info and _day_start_balance > 0:
        loss_pct = (_day_start_balance - info.equity) / _day_start_balance * 100
        if loss_pct >= DAILY_LOSS_PCT:
            _daily_loss_triggered = True
            logger.warning(f"🚨 Circuit breaker: pérdida diaria {loss_pct:.2f}% >= {DAILY_LOSS_PCT}%")
            return False, f"Circuit breaker: pérdida diaria {loss_pct:.2f}%"
    return True, ""


def get_account_info() -> Dict[str, Any]:
    mt5 = _load_mt5()
    if not mt5 or not _connected:
        return {"error": "No conectado"}
    info = mt5.account_info()
    if not info:
        return {"error": str(mt5.last_error())}
    return {
        "login":    info.login,
        "name":     info.name,
        "server":   info.server,
        "balance":  info.balance,
        "equity":   info.equity,
        "margin":   info.margin,
        "free_margin": info.margin_free,
        "leverage": info.leverage,
        "currency": info.currency,
    }


def get_open_positions() -> list:
    mt5 = _load_mt5()
    if not mt5 or not _connected:
        return []
    positions = mt5.positions_get()
    if positions is None:
        return []
    result = []
    for p in positions:
        result.append({
            "ticket":    p.ticket,
            "symbol":    p.symbol,
            "type":      "BUY" if p.type == 0 else "SELL",
            "volume":    p.volume,
            "open_price": p.price_open,
            "current_price": p.price_current,
            "sl":        p.sl,
            "tp":        p.tp,
            "profit":    p.profit,
            "comment":   p.comment,
            "time":      p.time,
        })
    return result


def place_order(symbol: str,
                direction: str,
                volume: float,
                price: float,
                sl: float,
                tp: float,
                comment: str = "SMC Bot") -> Dict[str, Any]:
    """
    Envía una orden de mercado.

    Reglas de seguridad (NO negociables):
      - sl DEBE ser distinto de 0
      - volumen DEBE ser > 0
      - Se comprueba el circuit breaker diario antes de operar
    """
    mt5 = _load_mt5()
    if not mt5 or not _connected:
        return {"success": False, "error": "No conectado a MT5"}

    # ── Validaciones de riesgo ────────────────────────────────────
    if sl == 0:
        return {"success": False, "error": "Stop Loss obligatorio (sl ≠ 0)"}
    if volume <= 0:
        return {"success": False, "error": "Volumen debe ser > 0"}

    ok, msg = _check_daily_loss()
    if not ok:
        return {"success": False, "error": msg}

    # ── Preparar la orden ─────────────────────────────────────────
    order_type = mt5.ORDER_TYPE_BUY if direction.upper() == "BUY" else mt5.ORDER_TYPE_SELL

    request = {
        "action":       mt5.TRADE_ACTION_DEAL,
        "symbol":       symbol,
        "volume":       volume,
        "type":         order_type,
        "price":        price,
        "sl":           sl,
        "tp":           tp,
        "comment":      comment,
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    result = mt5.order_send(request)

    if result is None:
        return {"success": False, "error": str(mt5.last_error())}

    if result.retcode != mt5.TRADE_RETCODE_DONE:
        return {
            "success":  False,
            "retcode":  result.retcode,
            "error":    result.comment,
        }

    logger.info(f"Orden ejecutada: {direction} {volume} {symbol} @ {result.price} "
                f"SL={sl} TP={tp} ticket={result.order}")
    return {
        "success":  True,
        "ticket":   result.order,
        "price":    result.price,
        "volume":   result.volume,
        "symbol":   symbol,
        "direction": direction,
    }


def close_position(ticket: int) -> Dict[str, Any]:
    """Cierra una posición por ticket."""
    mt5 = _load_mt5()
    if not mt5 or not _connected:
        return {"success": False, "error": "No conectado"}

    positions = mt5.positions_get(ticket=ticket)
    if not positions:
        return {"success": False, "error": f"Posición {ticket} no encontrada"}

    pos = positions[0]
    close_type = mt5.ORDER_TYPE_SELL if pos.type == 0 else mt5.ORDER_TYPE_BUY
    price = mt5.symbol_info_tick(pos.symbol).bid if pos.type == 0 else mt5.symbol_info_tick(pos.symbol).ask

    request = {
        "action":   mt5.TRADE_ACTION_DEAL,
        "position": ticket,
        "symbol":   pos.symbol,
        "volume":   pos.volume,
        "type":     close_type,
        "price":    price,
        "comment":  "SMC Bot close",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    result = mt5.order_send(request)
    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        err = str(mt5.last_error()) if result is None else result.comment
        return {"success": False, "error": err}

    return {"success": True, "ticket": ticket, "closed_price": result.price}


def is_connected() -> bool:
    return _connected

"""
mt5_bridge.py
─────────────
Cliente TCP que habla con mt5_win_bridge.py (Wine Python).
La API Flask llama a este módulo; este módulo habla con el proceso Wine.

Flujo:
  api_server.py → mt5_bridge.py → TCP 9999 → mt5_win_bridge.py (Wine) → MT5
"""

import socket
import json
import os
import logging
from typing import Dict, Any, Tuple

logger = logging.getLogger(__name__)

BRIDGE_HOST = os.getenv("MT5_BRIDGE_HOST", "127.0.0.1")
BRIDGE_PORT = int(os.getenv("MT5_BRIDGE_PORT", "9999"))
TIMEOUT     = 15  # segundos

MAX_RISK_PCT   = float(os.getenv("MT5_MAX_RISK_PCT", "1.0"))
DAILY_LOSS_PCT = float(os.getenv("MT5_DAILY_LOSS_PCT", "3.0"))

_daily_loss_triggered = False
_day_start_balance    = None


def _send(cmd: dict) -> dict:
    """Envía un comando al bridge Wine y recibe la respuesta."""
    try:
        with socket.create_connection((BRIDGE_HOST, BRIDGE_PORT), timeout=TIMEOUT) as sock:
            sock.sendall(json.dumps(cmd).encode("utf-8") + b"\n")
            data = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk
                if data.endswith(b"\n"):
                    break
        return json.loads(data.decode("utf-8").strip())
    except ConnectionRefusedError:
        return {"ok": False, "error": "Bridge MT5 no disponible (Wine no arrancado aún)"}
    except socket.timeout:
        return {"ok": False, "error": "Timeout conectando con bridge MT5"}
    except Exception as e:
        logger.warning(f"_send error: {e}")
        return {"ok": False, "error": str(e)}


def is_connected() -> bool:
    r = _send({"action": "ping"})
    return r.get("ok") and r.get("connected", False)


def connect(login=None, password=None, server=None) -> Tuple[bool, str]:
    r = _send({"action": "connect"})
    return r.get("ok", False), r.get("msg", r.get("error", ""))


def get_account_info() -> Dict[str, Any]:
    r = _send({"action": "account"})
    if not r.get("ok"):
        return {"error": r.get("error", "Error desconocido")}
    return r.get("data", {})


def get_open_positions() -> list:
    r = _send({"action": "positions"})
    if not r.get("ok"):
        return []
    return r.get("data", [])


def place_order(symbol: str, direction: str, volume: float,
                price: float, sl: float, tp: float,
                comment: str = "SMC Bot") -> Dict[str, Any]:
    if sl == 0:
        return {"success": False, "error": "Stop Loss obligatorio (sl ≠ 0)"}

    # Circuit breaker diario
    global _daily_loss_triggered
    if _daily_loss_triggered:
        return {"success": False, "error": f"Circuit breaker: pérdida diaria >{DAILY_LOSS_PCT}% alcanzada"}

    r = _send({
        "action":    "trade",
        "symbol":    symbol,
        "direction": direction,
        "volume":    volume,
        "price":     price,
        "sl":        sl,
        "tp":        tp,
        "comment":   comment,
    })

    if not r.get("ok"):
        return {"success": False, "error": r.get("error", "Error desconocido")}

    return {
        "success":   True,
        "ticket":    r.get("ticket"),
        "price":     r.get("price"),
        "volume":    r.get("volume"),
        "symbol":    symbol,
        "direction": direction,
    }


def close_position(ticket: int) -> Dict[str, Any]:
    r = _send({"action": "close", "ticket": ticket})
    if not r.get("ok"):
        return {"success": False, "error": r.get("error", "Error desconocido")}
    return {"success": True, "ticket": ticket, "closed_price": r.get("price")}

"""
mt5_bridge.py — MetaAPI edition
────────────────────────────────
Conecta con MT5 via MetaAPI cloud REST API.
Sin Wine, sin MT5 local. Solo HTTP.

Variables de entorno requeridas:
  METAAPI_TOKEN       → token de la cuenta MetaAPI
  METAAPI_ACCOUNT_ID  → ID de la cuenta MT5 en MetaAPI

Variables opcionales:
  METAAPI_REGION      → región (defecto: new-york)
"""

import os
import logging
import requests
from typing import Dict, Any, Tuple

logger = logging.getLogger(__name__)

# ── Config MetaAPI ────────────────────────────────────────────────
_TOKEN      = os.getenv("METAAPI_TOKEN", "")
_ACCOUNT_ID = os.getenv("METAAPI_ACCOUNT_ID", "")
_REGION     = os.getenv("METAAPI_REGION", "new-york")
_BASE       = f"https://mt-client-api-v1.{_REGION}.agiliumtrade.agiliumtrade.ai"
_TIMEOUT    = 15

MAX_RISK_PCT   = float(os.getenv("MT5_MAX_RISK_PCT", "1.0"))
DAILY_LOSS_PCT = float(os.getenv("MT5_DAILY_LOSS_PCT", "3.0"))
_daily_loss_triggered = False


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {_TOKEN}",
        "Content-Type":  "application/json",
    }


def _url(path: str) -> str:
    return f"{_BASE}/users/current/accounts/{_ACCOUNT_ID}{path}"


def _get(path: str) -> dict:
    try:
        r = requests.get(_url(path), headers=_headers(), timeout=_TIMEOUT)
        r.raise_for_status()
        return {"ok": True, "data": r.json()}
    except requests.exceptions.HTTPError as e:
        return {"ok": False, "error": f"HTTP {e.response.status_code}: {e.response.text[:200]}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _post(path: str, body: dict) -> dict:
    try:
        r = requests.post(_url(path), headers=_headers(), json=body, timeout=_TIMEOUT)
        r.raise_for_status()
        return {"ok": True, "data": r.json() if r.text else {}}
    except requests.exceptions.HTTPError as e:
        return {"ok": False, "error": f"HTTP {e.response.status_code}: {e.response.text[:200]}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── API pública ───────────────────────────────────────────────────

def is_connected() -> bool:
    if not _TOKEN or not _ACCOUNT_ID:
        return False
    r = _get("/accountInformation")
    return r.get("ok", False)


def connect(login=None, password=None, server=None) -> Tuple[bool, str]:
    if not _TOKEN:
        return False, "METAAPI_TOKEN no configurado. Ve a app.metaapi.cloud y copia tu token."
    if not _ACCOUNT_ID:
        return False, "METAAPI_ACCOUNT_ID no configurado. Añade tu cuenta MT5 en MetaAPI y copia el ID."
    r = _get("/accountInformation")
    if r.get("ok"):
        info = r["data"]
        return True, f"Conectado: {info.get('name','?')} balance={info.get('balance','?')} {info.get('currency','')}"
    return False, r.get("error", "Error desconocido")


def get_account_info() -> Dict[str, Any]:
    r = _get("/accountInformation")
    if not r.get("ok"):
        return {"error": r.get("error")}
    d = r["data"]
    return {
        "login":       d.get("login"),
        "balance":     d.get("balance"),
        "equity":      d.get("equity"),
        "margin":      d.get("margin"),
        "free_margin": d.get("freeMargin"),
        "currency":    d.get("currency"),
        "server":      d.get("broker"),
        "leverage":    d.get("leverage"),
    }


def get_open_positions() -> list:
    r = _get("/positions")
    if not r.get("ok"):
        return []
    return [
        {
            "ticket":        p.get("id"),
            "symbol":        p.get("symbol"),
            "type":          "BUY" if p.get("type") == "POSITION_TYPE_BUY" else "SELL",
            "volume":        p.get("volume"),
            "open_price":    p.get("openPrice"),
            "current_price": p.get("currentPrice"),
            "sl":            p.get("stopLoss"),
            "tp":            p.get("takeProfit"),
            "profit":        p.get("profit"),
        }
        for p in r["data"]
    ]


def place_order(symbol: str, direction: str, volume: float,
                price: float, sl: float, tp: float,
                comment: str = "SMC Bot") -> Dict[str, Any]:
    if sl == 0:
        return {"success": False, "error": "Stop Loss obligatorio (sl != 0)"}

    global _daily_loss_triggered
    if _daily_loss_triggered:
        return {"success": False, "error": f"Circuit breaker: perdida diaria >{DAILY_LOSS_PCT}%"}

    order_type = "ORDER_TYPE_BUY" if direction.upper() in ("BUY", "LONG") else "ORDER_TYPE_SELL"

    body = {
        "actionType": order_type,
        "symbol":     symbol,
        "volume":     volume,
        "stopLoss":   sl,
        "comment":    comment,
    }
    if tp:
        body["takeProfit"] = tp

    r = _post("/trade", body)
    if not r.get("ok"):
        return {"success": False, "error": r.get("error")}

    data = r.get("data", {})
    return {
        "success":   True,
        "ticket":    data.get("orderId") or data.get("positionId"),
        "symbol":    symbol,
        "direction": direction,
        "volume":    volume,
    }


def close_position(ticket) -> Dict[str, Any]:
    body = {
        "actionType": "POSITION_CLOSE_ID",
        "positionId": str(ticket),
    }
    r = _post("/trade", body)
    if not r.get("ok"):
        return {"success": False, "error": r.get("error")}
    return {"success": True, "ticket": ticket}

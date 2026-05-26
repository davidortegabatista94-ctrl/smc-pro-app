"""
mt5_bridge.py — OANDA REST API edition
────────────────────────────────────────
Conecta con OANDA practice/demo via REST API nativa.
Sin Wine, sin MT5 local, sin dependencias externas.
Funciona 100% en Linux/Railway.

Variables de entorno:
  OANDA_API_TOKEN   → token de OANDA (obligatorio)
  OANDA_ACCOUNT_ID  → ID de cuenta OANDA (obligatorio)
  OANDA_ENVIRONMENT → "practice" (demo) o "live" (real) — defecto: practice
"""

import os
import logging
import requests
from typing import Dict, Any, Tuple

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────
_TOKEN   = os.getenv("OANDA_API_TOKEN", "")
_ACCOUNT = os.getenv("OANDA_ACCOUNT_ID", "")
_ENV     = os.getenv("OANDA_ENVIRONMENT", "practice")  # practice = demo

_BASE = (
    "https://api-fxpractice.oanda.com/v3"
    if _ENV == "practice"
    else "https://api-fxtrade.oanda.com/v3"
)
_TIMEOUT = 10

MAX_RISK_PCT   = float(os.getenv("MT5_MAX_RISK_PCT", "1.0"))
DAILY_LOSS_PCT = float(os.getenv("MT5_DAILY_LOSS_PCT", "3.0"))
_daily_loss_triggered = False


def _h() -> dict:
    return {
        "Authorization":  f"Bearer {_TOKEN}",
        "Content-Type":   "application/json",
        "Accept-Datetime-Format": "RFC3339",
    }


def _get(path: str) -> dict:
    try:
        r = requests.get(f"{_BASE}{path}", headers=_h(), timeout=_TIMEOUT)
        r.raise_for_status()
        return {"ok": True, "data": r.json()}
    except requests.exceptions.HTTPError as e:
        return {"ok": False, "error": f"HTTP {e.response.status_code}: {e.response.text[:300]}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _post(path: str, body: dict) -> dict:
    try:
        r = requests.post(f"{_BASE}{path}", headers=_h(), json=body, timeout=_TIMEOUT)
        r.raise_for_status()
        return {"ok": True, "data": r.json()}
    except requests.exceptions.HTTPError as e:
        return {"ok": False, "error": f"HTTP {e.response.status_code}: {e.response.text[:300]}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _put(path: str, body: dict = None) -> dict:
    try:
        r = requests.put(f"{_BASE}{path}", headers=_h(), json=body or {}, timeout=_TIMEOUT)
        r.raise_for_status()
        return {"ok": True, "data": r.json()}
    except requests.exceptions.HTTPError as e:
        return {"ok": False, "error": f"HTTP {e.response.status_code}: {e.response.text[:300]}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _to_oanda_symbol(symbol: str) -> str:
    """EURUSD → EUR_USD"""
    sym = symbol.upper().replace("_", "")
    if len(sym) == 6:
        return f"{sym[:3]}_{sym[3:]}"
    return symbol


def _to_units(direction: str, volume: float) -> str:
    """Convierte volumen MT5 a units OANDA (1 lot = 100,000 units)"""
    units = int(volume * 100_000)
    return str(units) if direction.upper() in ("BUY", "LONG") else str(-units)


# ── API pública (misma interfaz que antes) ────────────────────────

def is_connected() -> bool:
    if not _TOKEN or not _ACCOUNT:
        return False
    r = _get(f"/accounts/{_ACCOUNT}/summary")
    return r.get("ok", False)


def connect(login=None, password=None, server=None) -> Tuple[bool, str]:
    if not _TOKEN:
        return False, "OANDA_API_TOKEN no configurado."
    if not _ACCOUNT:
        return False, "OANDA_ACCOUNT_ID no configurado."
    r = _get(f"/accounts/{_ACCOUNT}/summary")
    if r.get("ok"):
        acct = r["data"].get("account", {})
        bal  = acct.get("balance", "?")
        curr = acct.get("currency", "")
        return True, f"Conectado OANDA {_ENV}: balance={bal} {curr}"
    return False, r.get("error", "Error")


def get_account_info() -> Dict[str, Any]:
    r = _get(f"/accounts/{_ACCOUNT}/summary")
    if not r.get("ok"):
        return {"error": r.get("error")}
    a = r["data"].get("account", {})
    return {
        "login":       a.get("id"),
        "balance":     float(a.get("balance", 0)),
        "equity":      float(a.get("NAV", 0)),
        "margin":      float(a.get("marginUsed", 0)),
        "free_margin": float(a.get("marginAvailable", 0)),
        "currency":    a.get("currency"),
        "server":      f"OANDA {_ENV}",
        "leverage":    a.get("marginRate"),
    }


def get_open_positions() -> list:
    r = _get(f"/accounts/{_ACCOUNT}/trades?state=OPEN")
    if not r.get("ok"):
        return []
    result = []
    for t in r["data"].get("trades", []):
        units = float(t.get("currentUnits", 0))
        result.append({
            "ticket":        t.get("id"),
            "symbol":        t.get("instrument", "").replace("_", ""),
            "type":          "BUY" if units > 0 else "SELL",
            "volume":        abs(units) / 100_000,
            "open_price":    float(t.get("price", 0)),
            "current_price": float(t.get("price", 0)),
            "sl":            float(t.get("stopLossOrder", {}).get("price", 0) or 0),
            "tp":            float(t.get("takeProfitOrder", {}).get("price", 0) or 0),
            "profit":        float(t.get("unrealizedPL", 0)),
        })
    return result


def place_order(symbol: str, direction: str, volume: float,
                price: float, sl: float, tp: float,
                comment: str = "SMC Bot") -> Dict[str, Any]:
    if sl == 0:
        return {"success": False, "error": "Stop Loss obligatorio (sl != 0)"}

    global _daily_loss_triggered
    if _daily_loss_triggered:
        return {"success": False, "error": f"Circuit breaker: perdida diaria >{DAILY_LOSS_PCT}%"}

    oanda_sym  = _to_oanda_symbol(symbol)
    units      = _to_units(direction, volume)
    sl_str     = f"{sl:.5f}"

    order: dict = {
        "type":   "MARKET",
        "instrument": oanda_sym,
        "units":  units,
        "stopLossOnFill": {"price": sl_str, "timeInForce": "GTC"},
    }
    if tp:
        order["takeProfitOnFill"] = {"price": f"{tp:.5f}", "timeInForce": "GTC"}

    r = _post(f"/accounts/{_ACCOUNT}/orders", {"order": order})
    if not r.get("ok"):
        return {"success": False, "error": r.get("error")}

    data = r["data"]
    fill = data.get("orderFillTransaction", {})
    return {
        "success":   True,
        "ticket":    fill.get("tradeOpened", {}).get("tradeID") or fill.get("id"),
        "price":     float(fill.get("price", price)),
        "volume":    volume,
        "symbol":    symbol,
        "direction": direction,
    }


def close_position(ticket) -> Dict[str, Any]:
    r = _put(f"/accounts/{_ACCOUNT}/trades/{ticket}/close")
    if not r.get("ok"):
        return {"success": False, "error": r.get("error")}
    fill = r["data"].get("orderFillTransaction", {})
    return {
        "success":      True,
        "ticket":       ticket,
        "closed_price": float(fill.get("price", 0)),
    }

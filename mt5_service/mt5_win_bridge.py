"""
mt5_win_bridge.py
─────────────────
Servidor TCP que corre bajo Wine Python (Windows) dentro del contenedor.
Acepta comandos JSON en el puerto 9999 y los ejecuta via MetaTrader5.

Arquitectura:
  [Flask API (Linux Python)] <--TCP 9999--> [Este script (Wine Python)] <--> [MT5 terminal]

Arranque: wine python.exe mt5_win_bridge.py
"""

import socket
import json
import os
import sys
import time
import threading

try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    MT5_AVAILABLE = False
    print("ERROR: MetaTrader5 no disponible", flush=True)

HOST = "127.0.0.1"
PORT = 9999
_connected = False


def mt5_connect():
    global _connected
    if not MT5_AVAILABLE:
        return False, "MetaTrader5 no disponible"
    if _connected:
        return True, "Ya conectado"

    login    = os.environ.get("MT5_LOGIN")
    password = os.environ.get("MT5_PASSWORD", "")
    server   = os.environ.get("MT5_SERVER", "")

    if not mt5.initialize():
        return False, f"initialize() falló: {mt5.last_error()}"

    if login and password and server:
        ok = mt5.login(login=int(login), password=password, server=server)
        if not ok:
            mt5.shutdown()
            return False, f"login falló: {mt5.last_error()}"

    _connected = True
    return True, "Conectado"


def handle_command(cmd: dict) -> dict:
    global _connected
    action = cmd.get("action", "")

    if action == "ping":
        return {"ok": True, "mt5": MT5_AVAILABLE, "connected": _connected}

    if action == "connect":
        ok, msg = mt5_connect()
        return {"ok": ok, "msg": msg}

    if action == "account":
        if not _connected:
            mt5_connect()
        info = mt5.account_info()
        if not info:
            return {"ok": False, "error": str(mt5.last_error())}
        return {"ok": True, "data": {
            "login": info.login, "balance": info.balance,
            "equity": info.equity, "margin": info.margin,
            "free_margin": info.margin_free, "currency": info.currency,
            "server": info.server, "leverage": info.leverage,
        }}

    if action == "positions":
        if not _connected:
            mt5_connect()
        positions = mt5.positions_get() or []
        result = [{"ticket": p.ticket, "symbol": p.symbol,
                   "type": "BUY" if p.type == 0 else "SELL",
                   "volume": p.volume, "open_price": p.price_open,
                   "current_price": p.price_current,
                   "sl": p.sl, "tp": p.tp, "profit": p.profit} for p in positions]
        return {"ok": True, "data": result}

    if action == "trade":
        if not _connected:
            ok, msg = mt5_connect()
            if not ok:
                return {"ok": False, "error": msg}

        symbol    = cmd["symbol"]
        direction = cmd["direction"].upper()
        volume    = float(cmd["volume"])
        price     = float(cmd["price"])
        sl        = float(cmd["sl"])
        tp        = float(cmd.get("tp", 0))
        comment   = cmd.get("comment", "SMC Bot")

        if sl == 0:
            return {"ok": False, "error": "Stop Loss obligatorio"}

        order_type = mt5.ORDER_TYPE_BUY if direction in ("BUY", "LONG") else mt5.ORDER_TYPE_SELL
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol, "volume": volume, "type": order_type,
            "price": price, "sl": sl, "tp": tp, "comment": comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            err = str(mt5.last_error()) if result is None else result.comment
            return {"ok": False, "error": err, "retcode": getattr(result, "retcode", None)}
        return {"ok": True, "ticket": result.order, "price": result.price, "volume": result.volume}

    if action == "close":
        ticket = int(cmd["ticket"])
        positions = mt5.positions_get(ticket=ticket)
        if not positions:
            return {"ok": False, "error": f"Posición {ticket} no encontrada"}
        pos = positions[0]
        close_type = mt5.ORDER_TYPE_SELL if pos.type == 0 else mt5.ORDER_TYPE_BUY
        tick = mt5.symbol_info_tick(pos.symbol)
        price = tick.bid if pos.type == 0 else tick.ask
        request = {
            "action": mt5.TRADE_ACTION_DEAL, "position": ticket,
            "symbol": pos.symbol, "volume": pos.volume, "type": close_type,
            "price": price, "comment": "SMC Bot close",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            return {"ok": False, "error": str(mt5.last_error())}
        return {"ok": True, "ticket": ticket, "price": result.price}

    return {"ok": False, "error": f"Acción desconocida: {action}"}


def handle_client(conn, addr):
    try:
        data = b""
        while True:
            chunk = conn.recv(4096)
            if not chunk:
                break
            data += chunk
            if data.endswith(b"\n"):
                break
        cmd = json.loads(data.decode("utf-8").strip())
        response = handle_command(cmd)
    except Exception as e:
        response = {"ok": False, "error": str(e)}
    finally:
        try:
            conn.sendall(json.dumps(response).encode("utf-8") + b"\n")
        except:
            pass
        conn.close()


def main():
    print(f"MT5 Win Bridge arrancando en {HOST}:{PORT}", flush=True)

    # Intentar conectar MT5 al arrancar
    ok, msg = mt5_connect()
    print(f"MT5 conexión inicial: {msg}", flush=True)

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((HOST, PORT))
    server.listen(10)
    print(f"Escuchando en {HOST}:{PORT}", flush=True)

    while True:
        try:
            conn, addr = server.accept()
            t = threading.Thread(target=handle_client, args=(conn, addr), daemon=True)
            t.start()
        except Exception as e:
            print(f"Error accept: {e}", flush=True)
            time.sleep(1)


if __name__ == "__main__":
    main()

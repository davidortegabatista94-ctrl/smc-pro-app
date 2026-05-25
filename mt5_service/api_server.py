"""
api_server.py
─────────────
REST API que expone las operaciones de MetaTrader 5.
El Streamlit app (otro servicio en Railway) llama a esta API
cuando la variable de entorno MT5_SERVICE_URL está definida.

Endpoints:
  GET  /health              → estado del servicio y conexión MT5
  POST /connect             → conectar/reconectar a MT5
  GET  /account             → info de la cuenta
  GET  /positions           → posiciones abiertas
  POST /trade               → ejecutar una orden
  DELETE /position/<ticket> → cerrar una posición
"""

import os
import logging
from flask import Flask, request, jsonify
from flask_cors import CORS

import mt5_bridge as mt5

# ─── Config ──────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)  # permite llamadas desde el Streamlit (dominio diferente en Railway)

# Token de seguridad simple — define MT5_API_TOKEN en Railway
API_TOKEN = os.getenv("MT5_API_TOKEN", "")


def _check_auth():
    """Valida Bearer token si MT5_API_TOKEN está configurado."""
    if not API_TOKEN:
        return None  # sin token configurado, acceso libre (solo dentro de Railway)
    auth = request.headers.get("Authorization", "")
    if auth != f"Bearer {API_TOKEN}":
        return jsonify({"error": "No autorizado"}), 401
    return None


# ─── Rutas ───────────────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    """Health check — Railway lo usa para saber si el servicio está listo."""
    return jsonify({
        "status":    "ok",
        "mt5":       "connected" if mt5.is_connected() else "disconnected",
        "service":   "mt5-service",
    })


@app.route("/connect", methods=["POST"])
def connect():
    err = _check_auth()
    if err:
        return err

    data     = request.get_json(silent=True) or {}
    login    = data.get("login")
    password = data.get("password")
    server   = data.get("server")

    ok, msg = mt5.connect(login=login, password=password, server=server)
    status = 200 if ok else 500
    return jsonify({"success": ok, "message": msg}), status


@app.route("/account", methods=["GET"])
def account():
    err = _check_auth()
    if err:
        return err

    if not mt5.is_connected():
        # Intento de conexión automática con variables de entorno
        mt5.connect()

    info = mt5.get_account_info()
    if "error" in info:
        return jsonify(info), 503
    return jsonify(info)


@app.route("/positions", methods=["GET"])
def positions():
    err = _check_auth()
    if err:
        return err

    if not mt5.is_connected():
        mt5.connect()

    return jsonify(mt5.get_open_positions())


@app.route("/trade", methods=["POST"])
def trade():
    """
    Cuerpo JSON esperado:
    {
        "symbol":    "EURUSD",
        "direction": "BUY" | "SELL",
        "volume":    0.01,
        "price":     1.0850,
        "sl":        1.0820,   ← OBLIGATORIO
        "tp":        1.0900,
        "comment":   "SMC Bot"
    }
    """
    err = _check_auth()
    if err:
        return err

    if not mt5.is_connected():
        ok, msg = mt5.connect()
        if not ok:
            return jsonify({"success": False, "error": f"MT5 no conectado: {msg}"}), 503

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"success": False, "error": "Body JSON requerido"}), 400

    required = ["symbol", "direction", "volume", "price", "sl"]
    missing  = [f for f in required if f not in data or data[f] is None]
    if missing:
        return jsonify({"success": False, "error": f"Campos faltantes: {missing}"}), 400

    result = mt5.place_order(
        symbol    = data["symbol"],
        direction = data["direction"],
        volume    = float(data["volume"]),
        price     = float(data["price"]),
        sl        = float(data["sl"]),
        tp        = float(data.get("tp", 0)),
        comment   = data.get("comment", "SMC Bot"),
    )

    status = 200 if result.get("success") else 400
    return jsonify(result), status


@app.route("/position/<int:ticket>", methods=["DELETE"])
def close_position(ticket: int):
    err = _check_auth()
    if err:
        return err

    if not mt5.is_connected():
        return jsonify({"success": False, "error": "MT5 no conectado"}), 503

    result = mt5.close_position(ticket)
    status = 200 if result.get("success") else 400
    return jsonify(result), status


@app.route("/debug", methods=["GET"])
def debug():
    """Diagnóstico del sistema — muestra procesos, archivos y estado Wine."""
    import subprocess
    import os

    def run(cmd):
        try:
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=5)
            return (r.stdout + r.stderr).strip()
        except Exception as e:
            return str(e)

    wine_c = os.path.join(os.getenv("WINEPREFIX", "/root/.wine"), "drive_c")

    return jsonify({
        "processes":        run("ps aux | grep -E 'wine|python|gunicorn|Xvfb' | grep -v grep"),
        "port_9999":        run("nc -z 127.0.0.1 9999 && echo 'ABIERTO' || echo 'CERRADO'"),
        "win_python_exists": os.path.exists(f"{wine_c}/Python311/python.exe"),
        "bridge_in_wine_c": os.path.exists(f"{wine_c}/mt5_win_bridge.py"),
        "bridge_in_app":    os.path.exists("/app/mt5_win_bridge.py"),
        "wine_c_contents":  run(f"ls {wine_c}/"),
        "python311_dir":    run(f"ls {wine_c}/Python311/ 2>/dev/null || echo 'NO EXISTE'"),
        "display":          os.getenv("DISPLAY", "NO SET"),
        "wineprefix":       os.getenv("WINEPREFIX", "NO SET"),
        "env_mt5_login":    "SET" if os.getenv("MT5_LOGIN") else "NO SET",
    })


# ─── Arranque ────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    logger.info(f"Arrancando MT5 API en puerto {port}")

    # Intento de conexión automática al arrancar
    ok, msg = mt5.connect()
    logger.info(f"Conexión MT5 inicial: {msg}")

    app.run(host="0.0.0.0", port=port, debug=False)

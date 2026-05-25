"""
api_server.py — MetaAPI edition
REST API que envuelve las operaciones MT5 via MetaAPI cloud.
"""

import os
import logging
from flask import Flask, request, jsonify
from flask_cors import CORS
import mt5_bridge as mt5

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

API_TOKEN   = os.getenv("MT5_API_TOKEN", "")
META_TOKEN  = os.getenv("METAAPI_TOKEN", "")
META_ACCT   = os.getenv("METAAPI_ACCOUNT_ID", "")


def _check_auth():
    if not API_TOKEN:
        return None
    auth = request.headers.get("Authorization", "")
    if auth != f"Bearer {API_TOKEN}":
        return jsonify({"error": "No autorizado"}), 401
    return None


@app.route("/health", methods=["GET"])
def health():
    configured = bool(META_TOKEN and META_ACCT)
    connected  = mt5.is_connected() if configured else False
    return jsonify({
        "status":     "ok",
        "mt5":        "connected" if connected else "disconnected",
        "service":    "mt5-service-metaapi",
        "configured": configured,
    })


@app.route("/connect", methods=["POST"])
def connect():
    err = _check_auth()
    if err:
        return err
    ok, msg = mt5.connect()
    return jsonify({"success": ok, "message": msg}), (200 if ok else 500)


@app.route("/account", methods=["GET"])
def account():
    err = _check_auth()
    if err:
        return err
    info = mt5.get_account_info()
    if "error" in info:
        return jsonify(info), 503
    return jsonify(info)


@app.route("/positions", methods=["GET"])
def positions():
    err = _check_auth()
    if err:
        return err
    return jsonify(mt5.get_open_positions())


@app.route("/trade", methods=["POST"])
def trade():
    err = _check_auth()
    if err:
        return err

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
    return jsonify(result), (200 if result.get("success") else 400)


@app.route("/position/<ticket>", methods=["DELETE"])
def close_position(ticket):
    err = _check_auth()
    if err:
        return err
    result = mt5.close_position(ticket)
    return jsonify(result), (200 if result.get("success") else 400)


@app.route("/status", methods=["GET"])
def status():
    """Diagnóstico de configuración."""
    return jsonify({
        "METAAPI_TOKEN":      "SET" if META_TOKEN else "FALTA — ve a app.metaapi.cloud",
        "METAAPI_ACCOUNT_ID": "SET" if META_ACCT  else "FALTA — copia el ID de tu cuenta MT5 en MetaAPI",
        "MT5_API_TOKEN":      "SET" if API_TOKEN  else "no configurado (opcional)",
        "region":             os.getenv("METAAPI_REGION", "new-york"),
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)), debug=False)

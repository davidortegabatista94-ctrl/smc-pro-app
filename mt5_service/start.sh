#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────
#  start.sh — arranque del servicio MT5
#  Orden: Xvfb → MT5 terminal → Wine Python bridge → Flask API
# ─────────────────────────────────────────────────────────────────
set -euo pipefail

MT5_DIR="$WINEPREFIX/drive_c/Program Files/MetaTrader 5"
MT5_EXE="$MT5_DIR/terminal64.exe"
WIN_PYTHON="C:\\Python311\\python.exe"
PORT="${PORT:-8080}"

echo "═══════════════════════════════════════════"
echo "  MT5 Service arrancando — PORT=$PORT"
echo "═══════════════════════════════════════════"

# ── 1. Display virtual ────────────────────────────────────────────
echo "[1/5] Xvfb..."
Xvfb :1 -screen 0 1024x768x16 -nolisten tcp &
export DISPLAY=:1
sleep 3

# ── 2. Instalar MT5 si no existe ──────────────────────────────────
if [ ! -f "$MT5_EXE" ]; then
    echo "[2/5] Instalando MT5 (primera vez, ~60s)..."
    wine /tmp/mt5setup.exe /auto &
    WAIT=0
    until [ -f "$MT5_EXE" ] || [ $WAIT -ge 120 ]; do
        sleep 5; WAIT=$((WAIT+5))
        echo "  ... $WAIT s"
    done
    if [ ! -f "$MT5_EXE" ]; then
        echo "❌ MT5 no se instaló. Continuando sin terminal (modo sin broker)..."
    else
        echo "✅ MT5 instalado"
    fi
else
    echo "[2/5] MT5 ya instalado."
fi

# ── 3. Arrancar MT5 terminal ──────────────────────────────────────
if [ -f "$MT5_EXE" ]; then
    echo "[3/5] Arrancando MT5 terminal..."
    wine "$MT5_EXE" /portable &
    sleep 15
    echo "✅ MT5 terminal arrancado"
else
    echo "[3/5] MT5 terminal no disponible — saltando."
fi

# ── 4. Arrancar Wine Python bridge (TCP 9999) ─────────────────────
echo "[4/5] Arrancando Wine Python bridge en puerto 9999..."
wine "$WIN_PYTHON" /app/mt5_win_bridge.py &
BRIDGE_PID=$!
sleep 5
echo "✅ Bridge PID=$BRIDGE_PID"

# ── 5. Arrancar Flask API ─────────────────────────────────────────
echo "[5/5] Arrancando Flask API en puerto $PORT..."
exec gunicorn \
    --bind "0.0.0.0:$PORT" \
    --workers 1 \
    --timeout 60 \
    --log-level info \
    api_server:app

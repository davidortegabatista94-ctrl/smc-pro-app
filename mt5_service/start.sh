#!/bin/bash
# ─────────────────────────────────────────────────────────────────
#  start.sh — MT5 Service
#  Flask SIEMPRE arranca. Bridge en background completamente aislado.
# ─────────────────────────────────────────────────────────────────

PORT="${PORT:-8080}"

echo "════ MT5 Service PORT=$PORT ════"

# 1. Display virtual para Wine
echo "[1/3] Xvfb..."
Xvfb :1 -screen 0 1024x768x16 -nolisten tcp &
export DISPLAY=:1
sleep 2

# 2. Bridge Wine Python — subshell completamente aislado en background
#    Z:\ en Wine = / en Linux, no hay que copiar nada
echo "[2/3] Iniciando bridge Wine Python en background..."
(
    echo "[bridge] Comprobando Wine Python..."
    if [ ! -f /root/.wine/drive_c/Python311/python.exe ]; then
        echo "[bridge] ERROR: Wine Python no encontrado"
        exit 1
    fi
    echo "[bridge] Wine Python OK, arrancando servidor TCP..."
    exec wine "C:\\Python311\\python.exe" "Z:\\app\\mt5_win_bridge.py"
) >> /tmp/bridge.log 2>&1 &

echo "  Bridge lanzado en background. Log: /tmp/bridge.log"
sleep 5

# 3. Flask API — siempre arranca pase lo que pase con el bridge
echo "[3/3] Gunicorn en puerto $PORT..."
exec gunicorn \
    --bind "0.0.0.0:$PORT" \
    --workers 1 \
    --timeout 60 \
    --log-level info \
    api_server:app

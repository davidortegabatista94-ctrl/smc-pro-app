#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────
#  start.sh — MT5 Service
#  Flask SIEMPRE arranca. El bridge corre en background aislado.
# ─────────────────────────────────────────────────────────────────
set -euo pipefail

PORT="${PORT:-8080}"
WINEPREFIX="${WINEPREFIX:-/root/.wine}"
WIN_PYTHON_LINUX="$WINEPREFIX/drive_c/Python311/python.exe"
MT5_EXE="$WINEPREFIX/drive_c/Program Files/MetaTrader 5/terminal64.exe"

echo "════════════════════════════════════════"
echo " MT5 Service  PORT=$PORT"
echo " WINEPREFIX=$WINEPREFIX"
echo "════════════════════════════════════════"

# ── 1. Display virtual ────────────────────────────────────────────
echo "[1/4] Xvfb..."
Xvfb :1 -screen 0 1024x768x16 -nolisten tcp &
export DISPLAY=:1
sleep 3
echo "  OK DISPLAY=:1"

# ── 2. Instalar MT5 si no existe ──────────────────────────────────
if [ ! -f "$MT5_EXE" ]; then
    echo "[2/4] Instalando MT5 (~90s)..."
    wine /tmp/mt5setup.exe /auto &
    WAITED=0
    while [ ! -f "$MT5_EXE" ] && [ $WAITED -lt 120 ]; do
        sleep 5; WAITED=$((WAITED+5))
        echo "  ...${WAITED}s"
    done
    [ -f "$MT5_EXE" ] && echo "  ✅ MT5 instalado" || echo "  ⚠️  MT5 no instalado, continuando"
else
    echo "[2/4] MT5 ya existe, iniciando terminal..."
    wine "$MT5_EXE" /portable &
    sleep 15
fi

# ── 3. Wine Python bridge — subshell completamente aislado ────────
# Si falla por cualquier razón NO afecta a Flask/gunicorn
echo "[3/4] Iniciando bridge Wine Python en background..."

(
    if [ ! -f "$WIN_PYTHON_LINUX" ]; then
        echo "[bridge] ❌ Wine Python no encontrado: $WIN_PYTHON_LINUX"
        echo "[bridge] Contenido drive_c:"
        ls "$WINEPREFIX/drive_c/" 2>&1 || true
        exit 1
    fi
    echo "[bridge] ✅ Wine Python encontrado"
    cp /app/mt5_win_bridge.py "$WINEPREFIX/drive_c/mt5_win_bridge.py"
    echo "[bridge] Script copiado, arrancando..."
    exec wine "C:\\Python311\\python.exe" "C:\\mt5_win_bridge.py"
) >> /tmp/bridge.log 2>&1 &

BRIDGE_PID=$!
echo "  Bridge PID=$BRIDGE_PID (log: /tmp/bridge.log)"
sleep 8

# ── 4. Flask API — siempre arranca ───────────────────────────────
echo "[4/4] Gunicorn en $PORT..."
exec gunicorn \
    --bind "0.0.0.0:$PORT" \
    --workers 1 \
    --timeout 60 \
    --log-level info \
    api_server:app

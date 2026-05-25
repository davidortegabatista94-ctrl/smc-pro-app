#!/bin/bash
# ─────────────────────────────────────────────────────────────────
#  start.sh — MT5 Service
# ─────────────────────────────────────────────────────────────────

PORT="${PORT:-8080}"
echo "════ MT5 Service PORT=$PORT ════"

# 1. Display virtual
echo "[1/5] Xvfb..."
Xvfb :1 -screen 0 1024x768x16 -nolisten tcp &
export DISPLAY=:1
sleep 3

# 2. Inicializar Wine en runtime (CRÍTICO — sin esto wine no encuentra kernel32.dll)
echo "[2/5] Inicializando Wine runtime..."
wineserver -f >> /tmp/wine.log 2>&1 &
sleep 2
wineboot >> /tmp/wine.log 2>&1 || true
sleep 8
echo "  Wine inicializado"

# 3. Instalar MT5 si no existe
MT5_EXE="/root/.wine/drive_c/Program Files/MetaTrader 5/terminal64.exe"
if [ ! -f "$MT5_EXE" ]; then
    echo "[3/5] Instalando MT5 (~90s)..."
    wine /tmp/mt5setup.exe /auto >> /tmp/mt5.log 2>&1 &
    WAITED=0
    while [ ! -f "$MT5_EXE" ] && [ $WAITED -lt 120 ]; do
        sleep 5; WAITED=$((WAITED+5))
        echo "  ...${WAITED}s"
    done
    [ -f "$MT5_EXE" ] && echo "  ✅ MT5 instalado" || echo "  ⚠️  MT5 no instalado"
else
    echo "[3/5] MT5 existe, iniciando terminal..."
    wine "$MT5_EXE" /portable >> /tmp/mt5.log 2>&1 &
    sleep 10
fi

# 4. Bridge Wine Python — subshell aislado
echo "[4/5] Bridge Wine Python..."
(
    echo "[bridge] Arrancando bridge..."
    exec wine "C:\\Python311\\python.exe" "Z:\\app\\mt5_win_bridge.py"
) >> /tmp/bridge.log 2>&1 &
sleep 5

# 5. Flask — siempre arranca
echo "[5/5] Gunicorn en puerto $PORT..."
exec gunicorn \
    --bind "0.0.0.0:$PORT" \
    --workers 1 \
    --timeout 60 \
    --log-level info \
    api_server:app

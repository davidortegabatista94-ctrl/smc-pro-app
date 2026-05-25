#!/bin/bash
# ─────────────────────────────────────────────────────────────────
#  start.sh — MT5 Service
#  Flask arranca siempre. Wine y el bridge en background total.
# ─────────────────────────────────────────────────────────────────

PORT="${PORT:-8080}"
echo "════ MT5 Service PORT=$PORT ════"

# 1. Display virtual para Wine
echo "[1/3] Xvfb..."
Xvfb :1 -screen 0 1024x768x16 -nolisten tcp &
export DISPLAY=:1
sleep 2

# 2. Todo lo de Wine en background (no puede bloquear Flask)
echo "[2/3] Wine + Bridge en background total..."
(
    echo "[wine] Inicializando Wine prefix en runtime..."
    # WINEDLLOVERRIDES evita que mscoree/mshtml cuelguen wineboot
    WINEDLLOVERRIDES="mscoree,mshtml=" wineboot --init >> /tmp/wine.log 2>&1 || true
    echo "[wine] wineboot completado"
    sleep 5

    MT5_EXE="/root/.wine/drive_c/Program Files/MetaTrader 5/terminal64.exe"
    if [ ! -f "$MT5_EXE" ]; then
        echo "[wine] Instalando MT5..."
        WINEDLLOVERRIDES="mscoree,mshtml=" wine /tmp/mt5setup.exe /auto >> /tmp/mt5.log 2>&1 &
        WAITED=0
        while [ ! -f "$MT5_EXE" ] && [ $WAITED -lt 120 ]; do
            sleep 5; WAITED=$((WAITED+5))
        done
        [ -f "$MT5_EXE" ] && echo "[wine] MT5 instalado" || echo "[wine] MT5 no instalado, continuando"
    else
        echo "[wine] MT5 existe, iniciando terminal..."
        WINEDLLOVERRIDES="mscoree,mshtml=" wine "$MT5_EXE" /portable >> /tmp/mt5.log 2>&1 &
        sleep 10
    fi

    echo "[bridge] Arrancando Wine Python bridge..."
    WINEDLLOVERRIDES="mscoree,mshtml=" wine "C:\\Python311\\python.exe" "Z:\\app\\mt5_win_bridge.py"
    echo "[bridge] Bridge terminó (exit $?)"
) >> /tmp/bridge.log 2>&1 &

echo "  Wine/Bridge PID=$! corriendo en background"

# 3. Flask — arranca inmediatamente, sin esperar Wine
echo "[3/3] Gunicorn en puerto $PORT..."
exec gunicorn \
    --bind "0.0.0.0:$PORT" \
    --workers 1 \
    --timeout 60 \
    --log-level info \
    api_server:app

#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────
#  start.sh — Arranque del servicio MT5 en Railway
#  Orden: Xvfb → instalación MT5 (si primera vez) → MT5 terminal → API
# ─────────────────────────────────────────────────────────────────
set -euo pipefail

MT5_DIR="$HOME/.wine/drive_c/Program Files/MetaTrader 5"
MT5_EXE="$MT5_DIR/terminal64.exe"
PORT="${PORT:-8080}"

echo "═══════════════════════════════════════════"
echo "  MT5 Service — arranque"
echo "  PORT=$PORT"
echo "═══════════════════════════════════════════"

# ── 1. Display virtual ────────────────────────────────────────────
echo "[1/4] Iniciando Xvfb en :1 ..."
Xvfb :1 -screen 0 1024x768x16 -nolisten tcp &
XVFB_PID=$!
export DISPLAY=:1
sleep 3

# ── 2. Instalar MT5 si no está presente ──────────────────────────
if [ ! -f "$MT5_EXE" ]; then
    echo "[2/4] Instalando MetaTrader 5 por primera vez (puede tardar ~60 s)..."
    wine /tmp/mt5setup.exe /auto 2>/dev/null &
    MT5_SETUP_PID=$!
    # Esperar hasta que aparezca el ejecutable (max 120 s)
    WAIT=0
    until [ -f "$MT5_EXE" ] || [ $WAIT -ge 120 ]; do
        sleep 5
        WAIT=$((WAIT+5))
        echo "  ... esperando instalación ($WAIT s)"
    done
    kill $MT5_SETUP_PID 2>/dev/null || true
    if [ ! -f "$MT5_EXE" ]; then
        echo "❌ MT5 no se instaló correctamente. Revisa los logs."
        exit 1
    fi
    echo "✅ MT5 instalado en: $MT5_DIR"
else
    echo "[2/4] MT5 ya instalado — omitiendo."
fi

# ── 3. Arrancar MT5 terminal en modo portable/headless ───────────
echo "[3/4] Arrancando terminal MT5..."
wine "$MT5_EXE" /portable &
MT5_PID=$!
sleep 15  # Dale tiempo para que cargue y abra el pipe de IPC

echo "✅ MT5 terminal PID=$MT5_PID"

# ── 4. Arrancar la API Flask ──────────────────────────────────────
echo "[4/4] Arrancando API Flask en puerto $PORT..."
exec gunicorn \
    --bind "0.0.0.0:$PORT" \
    --workers 1 \
    --timeout 60 \
    --log-level info \
    api_server:app

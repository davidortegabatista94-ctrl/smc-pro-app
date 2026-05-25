#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────
#  start.sh — arranque del servicio MT5
#  Orden: Xvfb → MT5 terminal → Wine Python bridge → Flask API
# ─────────────────────────────────────────────────────────────────
set -uo pipefail   # sin -e para no abortar si algo falla

MT5_DIR="$WINEPREFIX/drive_c/Program Files/MetaTrader 5"
MT5_EXE="$MT5_DIR/terminal64.exe"
WIN_PYTHON="C:\\Python311\\python.exe"
WIN_PYTHON_LINUX="$WINEPREFIX/drive_c/Python311/python.exe"
PORT="${PORT:-8080}"

echo "═══════════════════════════════════════════"
echo "  MT5 Service arrancando — PORT=$PORT"
echo "  WINEPREFIX=$WINEPREFIX"
echo "  DISPLAY=$DISPLAY"
echo "═══════════════════════════════════════════"

# ── 1. Display virtual ────────────────────────────────────────────
echo "[1/5] Iniciando Xvfb en :1..."
Xvfb :1 -screen 0 1024x768x16 -nolisten tcp &
XVFB_PID=$!
export DISPLAY=:1
sleep 3
echo "  Xvfb PID=$XVFB_PID"

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
        echo "⚠️  MT5 no se instaló. Continuando sin terminal..."
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
echo "[4/5] Verificando Wine Python en: $WIN_PYTHON_LINUX"

if [ ! -f "$WIN_PYTHON_LINUX" ]; then
    echo "❌ Wine Python NO encontrado."
    echo "  Contenido de drive_c:"
    ls "$WINEPREFIX/drive_c/" 2>&1 || echo "  (no se puede listar)"
    echo "  SALTANDO bridge — la API arrancará sin MT5"
else
    echo "✅ Wine Python encontrado."
    WINE_C="$WINEPREFIX/drive_c"
    echo "  Copiando bridge a $WINE_C/mt5_win_bridge.py..."
    cp /app/mt5_win_bridge.py "$WINE_C/mt5_win_bridge.py"
    echo "  ✅ Copiado. Arrancando bridge..."

    wine "$WIN_PYTHON" "C:\\mt5_win_bridge.py" >> /tmp/bridge.log 2>&1 &
    BRIDGE_PID=$!
    echo "  Bridge lanzado PID=$BRIDGE_PID"

    # Esperar hasta 30s a que el bridge abra el puerto 9999
    WAIT=0
    until ss -tlnp 2>/dev/null | grep -q 9999 || [ $WAIT -ge 30 ]; do
        sleep 2; WAIT=$((WAIT+2))
        echo "  Esperando bridge... ${WAIT}s"
        if ! kill -0 $BRIDGE_PID 2>/dev/null; then
            echo "  ❌ Bridge proceso muerto. Log /tmp/bridge.log:"
            cat /tmp/bridge.log 2>/dev/null || echo "  (sin log)"
            break
        fi
    done

    if ss -tlnp 2>/dev/null | grep -q 9999; then
        echo "✅ Bridge escuchando en puerto 9999"
    else
        echo "⚠️  Bridge no responde en 9999 tras 30s. Log:"
        cat /tmp/bridge.log 2>/dev/null || echo "  (sin log)"
    fi
fi

# ── 5. Arrancar Flask API ─────────────────────────────────────────
echo "[5/5] Arrancando Flask API en puerto $PORT..."
exec gunicorn \
    --bind "0.0.0.0:$PORT" \
    --workers 1 \
    --timeout 60 \
    --log-level info \
    api_server:app

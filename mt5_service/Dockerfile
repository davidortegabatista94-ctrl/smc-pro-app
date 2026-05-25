# ─────────────────────────────────────────────────────────────────
#  MT5 Service — Railway Docker
#  Arquitectura:
#    [Flask API - Linux Python] --TCP 9999--> [mt5_win_bridge.py - Wine Python] --> MT5 terminal
# ─────────────────────────────────────────────────────────────────
FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV WINEPREFIX=/root/.wine
ENV WINEARCH=win64
ENV DISPLAY=:1
ENV PYTHONUNBUFFERED=1

# ── 1. Dependencias base ──────────────────────────────────────────
RUN dpkg --add-architecture i386 && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
        wget curl ca-certificates gnupg2 \
        xvfb cabextract \
        python3 python3-pip \
        software-properties-common \
        netcat-openbsd iproute2 procps \
    && rm -rf /var/lib/apt/lists/*

# ── 2. Wine desde WineHQ ──────────────────────────────────────────
RUN mkdir -pm755 /etc/apt/keyrings && \
    wget -qO /etc/apt/keyrings/winehq-archive.key \
        https://dl.winehq.org/wine-builds/winehq.key && \
    echo "deb [signed-by=/etc/apt/keyrings/winehq-archive.key] \
        https://dl.winehq.org/wine-builds/ubuntu/ jammy main" \
        > /etc/apt/sources.list.d/winehq.list && \
    apt-get update && \
    apt-get install -y --install-recommends winehq-stable && \
    rm -rf /var/lib/apt/lists/*

# ── 3. Python Linux deps (Flask API) ─────────────────────────────
COPY requirements.txt /app/requirements.txt
RUN pip3 install --no-cache-dir -r /app/requirements.txt

# ── 4. Inicializar prefijo Wine ───────────────────────────────────
RUN Xvfb :1 -screen 0 1024x768x16 & \
    sleep 3 && \
    wineboot --init && \
    sleep 5 && \
    kill %1 || true

# ── 5. Windows Python 3.11 via Wine ──────────────────────────────
RUN wget -q "https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe" \
        -O /tmp/python-win.exe && \
    Xvfb :1 -screen 0 1024x768x16 & \
    sleep 3 && \
    wine /tmp/python-win.exe /quiet InstallAllUsers=0 PrependPath=0 \
        TargetDir="C:\\Python311" && \
    sleep 15 && \
    kill %1 || true && \
    rm /tmp/python-win.exe

# ── 6. MetaTrader5 via Wine Python ───────────────────────────────
RUN Xvfb :1 -screen 0 1024x768x16 & \
    sleep 3 && \
    wine "C:\\Python311\\python.exe" -m pip install --no-cache-dir MetaTrader5 && \
    sleep 10 && \
    kill %1 || true

# ── 7. Descargar instalador MT5 (se instala en start.sh) ─────────
RUN wget -q \
    "https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe" \
    -O /tmp/mt5setup.exe

# ── 8. Archivos de la aplicación ─────────────────────────────────
COPY api_server.py     /app/api_server.py
COPY mt5_bridge.py     /app/mt5_bridge.py
COPY mt5_win_bridge.py /app/mt5_win_bridge.py
COPY start.sh          /app/start.sh
RUN chmod +x /app/start.sh

WORKDIR /app
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

CMD ["/app/start.sh"]

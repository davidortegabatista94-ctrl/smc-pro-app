# ─────────────────────────────────────────────────────────────────
#  MT5 Service — Railway Docker
#  Corre MetaTrader 5 bajo Wine en Ubuntu y expone una REST API
#  para que el Streamlit app (otro servicio) ejecute trades.
# ─────────────────────────────────────────────────────────────────
FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV WINEPREFIX=/root/.wine
ENV WINEARCH=win64
ENV DISPLAY=:1
ENV PYTHONUNBUFFERED=1

# ── 1. Dependencias del sistema ───────────────────────────────────
RUN dpkg --add-architecture i386 && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
        wget \
        curl \
        cabextract \
        xvfb \
        x11vnc \
        xauth \
        winbind \
        python3 \
        python3-pip \
        python3-dev \
        # Wine (repo oficial de WineHQ para Ubuntu 22.04)
        gnupg2 \
        software-properties-common \
    && rm -rf /var/lib/apt/lists/*

# ── 2. Wine desde WineHQ (más estable que el paquete de Ubuntu) ───
RUN mkdir -pm755 /etc/apt/keyrings && \
    wget -O /etc/apt/keyrings/winehq-archive.key \
        https://dl.winehq.org/wine-builds/winehq.key && \
    echo "deb [signed-by=/etc/apt/keyrings/winehq-archive.key] \
        https://dl.winehq.org/wine-builds/ubuntu/ jammy main" \
        > /etc/apt/sources.list.d/winehq.list && \
    apt-get update && \
    apt-get install -y --install-recommends winehq-stable && \
    rm -rf /var/lib/apt/lists/*

# ── 3. Python deps ────────────────────────────────────────────────
COPY requirements.txt /app/requirements.txt
RUN pip3 install --no-cache-dir -r /app/requirements.txt

# ── 4. Pre-inicializar Wine (genera el prefijo ~/.wine) ───────────
#    Se hace en build para no pagar el coste en cada arranque.
RUN Xvfb :1 -screen 0 1024x768x16 & \
    sleep 3 && \
    wineboot --init && \
    sleep 5 && \
    kill %1 || true

# ── 5. Descargar el instalador de MT5 ─────────────────────────────
#    El instalador se descarga en build; la instalación real se hace
#    en start.sh porque necesita el display virtual activo.
RUN wget -q \
    "https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe" \
    -O /tmp/mt5setup.exe

# ── 6. Código de la API ───────────────────────────────────────────
COPY api_server.py  /app/api_server.py
COPY mt5_bridge.py  /app/mt5_bridge.py
COPY start.sh       /app/start.sh
RUN chmod +x /app/start.sh

WORKDIR /app

# Puerto de la REST API (Railway lo lee desde $PORT)
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

CMD ["/app/start.sh"]

# ─────────────────────────────────────────────────────────────────
#  MT5 Service — Railway Docker
#  [Flask API - Linux Python] --TCP 9999--> [Wine Python] --> MT5
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
        xvfb cabextract unzip \
        python3 python3-pip \
        software-properties-common \
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
    WINEDLLOVERRIDES="mscoree,mshtml=" wineboot --init && \
    sleep 10 && \
    kill %1 || true

# ── 5. Windows Python 3.11 — embeddable (sin instalador, 100% fiable) ──
# El zip se extrae directamente al filesystem de Wine, sin ejecutar ningún .exe
RUN wget -q "https://www.python.org/ftp/python/3.11.9/python-3.11.9-embed-amd64.zip" \
        -O /tmp/python-embed.zip && \
    mkdir -p /root/.wine/drive_c/Python311 && \
    unzip -q /tmp/python-embed.zip -d /root/.wine/drive_c/Python311/ && \
    rm /tmp/python-embed.zip && \
    # Activar site-packages (necesario para que pip instale paquetes accesibles)
    sed -i 's/#import site/import site/' /root/.wine/drive_c/Python311/python311._pth && \
    echo "Embeddable Python extraído OK" && \
    ls /root/.wine/drive_c/Python311/python.exe

# ── 6. Instalar pip en Wine Python ────────────────────────────────
# Z:\ en Wine = / en Linux, así accedemos a /tmp sin copiar nada
RUN wget -q "https://bootstrap.pypa.io/get-pip.py" -O /tmp/get-pip.py && \
    Xvfb :1 -screen 0 1024x768x16 & \
    sleep 3 && \
    wine "C:\\Python311\\python.exe" "Z:\\tmp\\get-pip.py" --no-warn-script-location && \
    sleep 10 && \
    kill %1 || true && \
    rm /tmp/get-pip.py

# Verificar que Wine Python funciona (build falla aquí si algo está mal)
RUN Xvfb :1 -screen 0 1024x768x16 & \
    sleep 3 && \
    wine "C:\\Python311\\python.exe" --version && \
    kill %1 || true

# ── 7. MetaTrader5 via Wine Python ───────────────────────────────
RUN Xvfb :1 -screen 0 1024x768x16 & \
    sleep 3 && \
    wine "C:\\Python311\\python.exe" -m pip install --no-cache-dir MetaTrader5 && \
    sleep 15 && \
    kill %1 || true

# Verificar que MetaTrader5 se importa (build falla aquí si algo está mal)
RUN Xvfb :1 -screen 0 1024x768x16 & \
    sleep 3 && \
    wine "C:\\Python311\\python.exe" -c "import MetaTrader5; print('MetaTrader5 OK')" && \
    kill %1 || true

# ── 8. Descargar instalador MT5 (se instala en start.sh) ─────────
RUN wget -q \
    "https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe" \
    -O /tmp/mt5setup.exe

# ── 9. Archivos de la aplicación ─────────────────────────────────
COPY api_server.py     /app/api_server.py
COPY mt5_bridge.py     /app/mt5_bridge.py
COPY mt5_win_bridge.py /app/mt5_win_bridge.py
COPY start.sh          /app/start.sh
RUN chmod +x /app/start.sh

WORKDIR /app
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=5 \
    CMD curl -f http://localhost:8080/health || exit 1

CMD ["/app/start.sh"]

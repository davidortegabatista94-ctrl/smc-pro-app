# ─────────────────────────────────────────────────────────────────
#  MT5 Service — MetaAPI edition (sin Wine, sin MT5 local)
#  Se conecta a MT5 via MetaAPI cloud REST API
# ─────────────────────────────────────────────────────────────────
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1

RUN pip install --no-cache-dir \
    flask>=3.0.0 \
    flask-cors>=4.0.0 \
    gunicorn>=21.0.0 \
    requests>=2.31.0

COPY api_server.py     /app/api_server.py
COPY mt5_bridge.py     /app/mt5_bridge.py
COPY start.sh          /app/start.sh
RUN chmod +x /app/start.sh

WORKDIR /app
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')" || exit 1

CMD ["/app/start.sh"]

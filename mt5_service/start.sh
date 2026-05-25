#!/bin/bash
echo "════ MT5 Service (MetaAPI) PORT=${PORT:-8080} ════"
exec gunicorn \
    --bind "0.0.0.0:${PORT:-8080}" \
    --workers 1 \
    --timeout 30 \
    --log-level info \
    api_server:app

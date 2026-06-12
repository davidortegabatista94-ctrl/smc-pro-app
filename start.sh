#!/bin/sh
# Arranque en Railway: worker de paper trading 24/7 + dashboard Streamlit.
#
# El worker es un PROCESO DEDICADO (no depende de que nadie abra la web):
# corre el ciclo de análisis/paper-trading cada N minutos sin parar.
# Streamlit solo MUESTRA (lee los ficheros que el worker escribe).
#
# PAPER_WORKER_DEDICATED=1 → el dashboard NO arranca su propio worker interno
# (evita dos escritores sobre paper_trades.jsonl, que corrompería el fichero).

export PAPER_WORKER_DEDICATED=1

# Worker en segundo plano (se reinicia solo dentro de su bucle si falla un ciclo)
python -m backend.paper_worker &

# Dashboard en primer plano (su ciclo de vida controla el contenedor)
exec streamlit run smc_pro_app.py \
    --server.port="${PORT:-8501}" \
    --server.address=0.0.0.0 \
    --server.headless=true

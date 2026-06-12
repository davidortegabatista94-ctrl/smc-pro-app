FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && python -m textblob.download_corpora lite

COPY . .

# Verify syntax before starting
RUN python -m py_compile smc_pro_app.py backend/paper_worker.py && echo "Syntax OK"

# Arranca worker dedicado 24/7 + dashboard Streamlit (ver start.sh)
CMD ["sh", "start.sh"]

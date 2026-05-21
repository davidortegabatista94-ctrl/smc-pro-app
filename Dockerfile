FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && python -m textblob.download_corpora lite

COPY . .

CMD streamlit run smc_pro_app.py --server.port=${PORT:-8501} --server.address=0.0.0.0 --server.headless=true

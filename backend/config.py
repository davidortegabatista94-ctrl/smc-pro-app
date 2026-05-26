"""
backend/config.py — All global constants extracted from smc_pro_app.py
"""
from datetime import timedelta
import os as _os_env

# ── API Keys ──────────────────────────────────────────────────────────────────
NEWS_API_KEY   = "0091d5b9d2dc46b4b907d04f5b66cee7"

# ── Scalping parameters ───────────────────────────────────────────────────────
SCALP_TP_PIPS  = 30  # SL 12p * 2.5 ratio promedio
SCALP_SL_PIPS  = 12  # Máximo 12 pips de stop loss
SCALP_MAX_HOLD = 3

# ── Instrument ────────────────────────────────────────────────────────────────
PIP    = 0.0001
SYMBOL = "EURUSD"

# ── Telegram ──────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = "7967414683:AAGmyLDjobQOvpU_OVzlwHJ-Tf1o9GjbIlE"
TELEGRAM_CHAT_ID = "1442582228"

# ── Persistence files ─────────────────────────────────────────────────────────
USER_CONFIG_FILE = "user_config.json"
POSITION_FILE    = "position_state.json"
TRADES_LOG_FILE  = "trades_history.json"
CACHE_FILE       = "news_cache.json"

# ── Scoring ───────────────────────────────────────────────────────────────────
MIN_DEFINITIVE_SCORE = 70  # Score mínimo para considerar señal definitiva

# ── Bot defaults ──────────────────────────────────────────────────────────────
BOT_ENABLED = False
BOT_VOLUME  = 0.1

# ── Cache ─────────────────────────────────────────────────────────────────────
CACHE_DURATION = timedelta(minutes=15)

# ── Spain UTC offset ──────────────────────────────────────────────────────────
UTC_OFFSET_SPAIN = 2  # CEST verano (+2); cambiar a 1 para CET invierno

# ── Railway deployment ────────────────────────────────────────────────────────
_railway_domain = _os_env.environ.get("RAILWAY_PUBLIC_DOMAIN", "")
_RAILWAY_URL = (
    f"https://{_railway_domain}"
    if _railway_domain
    else "https://web-production-c5a95d.up.railway.app"
)

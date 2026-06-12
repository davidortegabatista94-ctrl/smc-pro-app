"""
backend/config.py — Global constants. All secrets come from environment variables.
"""
from datetime import timedelta
import os as _os

# ── API Keys (env vars only — never hardcode secrets) ─────────────────────────
NEWS_API_KEY   = _os.environ.get("NEWS_API_KEY", "").strip()

# ── Scalping parameters ───────────────────────────────────────────────────────
SCALP_TP_PIPS  = 30
SCALP_SL_PIPS  = 12
SCALP_MAX_HOLD = 3

# ── Instrument ────────────────────────────────────────────────────────────────
PIP    = 0.0001
SYMBOL = "EURUSD"

# ── Telegram ──────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = _os.environ.get("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = _os.environ.get("TELEGRAM_CHAT_ID", "").strip()

# ── Persistence files ─────────────────────────────────────────────────────────
USER_CONFIG_FILE = "user_config.json"
POSITION_FILE    = "position_state.json"
TRADES_LOG_FILE  = "trades_history.json"
CACHE_FILE       = "news_cache.json"

# ── Scoring ───────────────────────────────────────────────────────────────────
MIN_DEFINITIVE_SCORE = 70

# ── Bot defaults ──────────────────────────────────────────────────────────────
BOT_ENABLED = False
BOT_VOLUME  = 0.1

# ── Cache ─────────────────────────────────────────────────────────────────────
CACHE_DURATION = timedelta(minutes=15)

# ── Spain UTC offset ──────────────────────────────────────────────────────────
UTC_OFFSET_SPAIN = 2  # CEST (+2); set to 1 for CET winter

# ── Railway deployment ────────────────────────────────────────────────────────
_railway_domain = _os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")
_RAILWAY_URL = (
    f"https://{_railway_domain}"
    if _railway_domain
    else "https://web-production-c5a95d.up.railway.app"
)

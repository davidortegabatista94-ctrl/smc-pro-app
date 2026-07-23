"""
backend/store.py — Router de almacenamiento del bot: Neon Postgres o ficheros.

Si hay DATABASE_URL (Neon en producción) → guarda en Postgres (persistente, gratis,
compartido entre el worker de GitHub Actions y el dashboard de Streamlit Cloud).
Si no (local) → ficheros JSON/JSONL como siempre.

Así el mismo código corre en local (ficheros) y en la nube gratis (Neon) sin cambios.
"""
from __future__ import annotations

import json
import os

from backend.storage import data_path


def use_db() -> bool:
    # Fuente única de verdad: db.db_url() detecta env O st.secrets (Streamlit Cloud)
    try:
        import db
        return bool(db.db_url())
    except Exception:
        return bool(os.environ.get("DATABASE_URL"))


def ensure_ready() -> None:
    """Crea las tablas del bot si usamos Postgres (idempotente)."""
    if use_db():
        try:
            import db
            db.ensure_tables()
        except Exception:
            pass


# ── KV (config, results, backtest, heartbeat) ────────────────────────────────
_KV_FILES = {
    "config":    "worker_config.json",
    "results":   "worker_results.json",
    "backtest":  "worker_backtest.json",
    "heartbeat": "worker_heartbeat.json",
}


def kv_get(key: str):
    if use_db():
        try:
            import db
            return db.bot_kv_get(key)
        except Exception:
            return None
    f = data_path(_KV_FILES.get(key, key + ".json"))
    try:
        if f.exists():
            with open(f, "r", encoding="utf-8") as fh:
                return json.load(fh)
    except Exception:
        pass
    return None


def kv_set(key: str, value) -> None:
    if use_db():
        try:
            import db
            db.bot_kv_set(key, value)
        except Exception:
            pass
        return
    f = data_path(_KV_FILES.get(key, key + ".json"))
    try:
        with open(f, "w", encoding="utf-8") as fh:
            json.dump(value, fh, default=str)
    except Exception:
        pass


# ── Paper trades ─────────────────────────────────────────────────────────────
_TRADES_FILE = "paper_trades.jsonl"


def trades_all() -> list[dict]:
    if use_db():
        try:
            import db
            return db.bot_trades_all()
        except Exception:
            return []
    f = data_path(_TRADES_FILE)
    out = []
    try:
        if f.exists():
            for ln in f.read_text(encoding="utf-8").strip().splitlines():
                if ln.strip():
                    try:
                        out.append(json.loads(ln))
                    except Exception:
                        pass
    except Exception:
        pass
    return out


def trades_append(trade: dict) -> None:
    if use_db():
        try:
            import db
            db.bot_trades_append(trade)
        except Exception:
            pass
        return
    f = data_path(_TRADES_FILE)
    try:
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(trade, default=str) + "\n")
    except Exception:
        pass


def trades_replace(trades: list[dict]) -> None:
    if use_db():
        try:
            import db
            db.bot_trades_replace(trades)
        except Exception:
            pass
        return
    f = data_path(_TRADES_FILE)
    try:
        with open(f, "w", encoding="utf-8") as fh:
            for t in trades:
                fh.write(json.dumps(t, default=str) + "\n")
    except Exception:
        pass

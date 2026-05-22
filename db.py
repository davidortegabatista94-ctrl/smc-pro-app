"""
db.py — PostgreSQL persistence layer for smc_pro_app.
All functions are safe: they return None/[] on any DB error so the app
never crashes if the database is temporarily unavailable.
"""

import os
import json
import logging
from contextlib import contextmanager
from datetime import datetime, timezone

_log = logging.getLogger(__name__)
_DB_URL = os.environ.get("DATABASE_URL", "")


def _get_conn():
    import psycopg2
    import psycopg2.extras
    return psycopg2.connect(_DB_URL, cursor_factory=psycopg2.extras.RealDictCursor)


@contextmanager
def _cursor():
    if not _DB_URL:
        yield None
        return
    conn = None
    try:
        conn = _get_conn()
        cur = conn.cursor()
        yield cur
        conn.commit()
    except Exception as e:
        _log.warning("DB error: %s", e)
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        yield None
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


# ── Backtest cache ─────────────────────────────────────────────────────────────

def save_backtest(cache_type: str, results: list, best: dict, n_bars: int = 0):
    """Save backtest results to DB. cache_type: '1year' or '2008'."""
    with _cursor() as cur:
        if cur is None:
            return
        import psycopg2.extras
        cur.execute(
            """
            INSERT INTO backtest_cache (cache_type, results_json, best_json, n_bars, updated_at)
            VALUES (%s, %s, %s, %s, NOW())
            ON CONFLICT (cache_type) DO UPDATE
                SET results_json = EXCLUDED.results_json,
                    best_json    = EXCLUDED.best_json,
                    n_bars       = EXCLUDED.n_bars,
                    updated_at   = NOW()
            """,
            (cache_type, json.dumps(results), json.dumps(best), n_bars),
        )


def load_backtest(cache_type: str) -> dict | None:
    """Load backtest results from DB. Returns dict with 'results', 'best', 'n_bars' or None."""
    if not _DB_URL:
        return None
    try:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT results_json, best_json, n_bars FROM backtest_cache WHERE cache_type = %s",
            (cache_type,),
        )
        row = cur.fetchone()
        conn.close()
        if not row:
            return None
        return {
            "results": row["results_json"],
            "best":    row["best_json"],
            "n_bars":  row["n_bars"] or 0,
        }
    except Exception as e:
        _log.warning("load_backtest error: %s", e)
        return None


# ── Chat history ───────────────────────────────────────────────────────────────

def save_chat_message(session_id: str, role: str, content: str):
    """Persist a single chat message."""
    with _cursor() as cur:
        if cur is None:
            return
        cur.execute(
            "INSERT INTO advisor_chat (session_id, role, content) VALUES (%s, %s, %s)",
            (session_id, role, content),
        )


def load_chat_history(session_id: str, limit: int = 40) -> list[dict]:
    """Load recent chat messages for a session. Returns list of {role, content}."""
    if not _DB_URL:
        return []
    try:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT role, content FROM advisor_chat
            WHERE session_id = %s
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (session_id, limit),
        )
        rows = cur.fetchall()
        conn.close()
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]
    except Exception as e:
        _log.warning("load_chat_history error: %s", e)
        return []


def clear_chat_history(session_id: str):
    """Delete all messages for a session."""
    with _cursor() as cur:
        if cur is None:
            return
        cur.execute("DELETE FROM advisor_chat WHERE session_id = %s", (session_id,))


# ── Trades history ─────────────────────────────────────────────────────────────

def save_trade(
    direction: str,
    entry_price: float,
    sl_price: float,
    tp_price: float,
    outcome: str,
    pips: float,
    pnl: float,
    strategy: str = "",
    score: int = 0,
    exit_price: float | None = None,
    opened_at: datetime | None = None,
    closed_at: datetime | None = None,
):
    """Persist a completed trade."""
    with _cursor() as cur:
        if cur is None:
            return
        cur.execute(
            """
            INSERT INTO trades_history
                (direction, entry_price, sl_price, tp_price, exit_price,
                 pips, pnl, outcome, strategy, score, opened_at, closed_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                direction, entry_price, sl_price, tp_price, exit_price,
                pips, pnl, outcome, strategy, score,
                opened_at or datetime.now(timezone.utc),
                closed_at or datetime.now(timezone.utc),
            ),
        )


def load_trades(limit: int = 200) -> list[dict]:
    """Return the most recent trades."""
    if not _DB_URL:
        return []
    try:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT direction, entry_price, sl_price, tp_price, exit_price,
                   pips, pnl, outcome, strategy, score, opened_at, closed_at
            FROM trades_history
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (limit,),
        )
        rows = cur.fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        _log.warning("load_trades error: %s", e)
        return []


def trades_summary() -> dict:
    """Aggregate stats: total trades, win rate, net pips, net P&L."""
    if not _DB_URL:
        return {}
    try:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
                COUNT(*)                                            AS total,
                ROUND(AVG(CASE WHEN pips > 0 THEN 1 ELSE 0 END)*100, 1) AS winrate,
                ROUND(SUM(pips)::numeric, 1)                        AS net_pips,
                ROUND(SUM(pnl)::numeric, 2)                         AS net_pnl,
                COUNT(CASE WHEN outcome='TP' THEN 1 END)            AS tp_count,
                COUNT(CASE WHEN outcome='SL' THEN 1 END)            AS sl_count,
                COUNT(CASE WHEN outcome='BE' THEN 1 END)            AS be_count
            FROM trades_history
            """
        )
        row = cur.fetchone()
        conn.close()
        return dict(row) if row else {}
    except Exception as e:
        _log.warning("trades_summary error: %s", e)
        return {}


# ── Market snapshots ───────────────────────────────────────────────────────────

def save_snapshot(price: float, signal: str, score: int, dxy_trend: str,
                  regime: str, strategy: str, extra: dict | None = None):
    """Save a periodic market snapshot (called on each analysis run)."""
    with _cursor() as cur:
        if cur is None:
            return
        cur.execute(
            """
            INSERT INTO market_snapshots
                (price, signal, score, dxy_trend, regime, strategy, snapshot_data)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            """,
            (price, signal, score, dxy_trend, regime, strategy,
             json.dumps(extra or {})),
        )


def is_connected() -> bool:
    """Quick health check — True if DB is reachable."""
    if not _DB_URL:
        return False
    try:
        conn = _get_conn()
        conn.close()
        return True
    except Exception:
        return False

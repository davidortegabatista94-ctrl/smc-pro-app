"""
db.py — PostgreSQL persistence layer for smc_pro_app.
All functions are safe: they return None/[] on any DB error so the app
never crashes if the database is temporarily unavailable.
"""

import os
import json
import uuid
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


# ── Auth & sessions ────────────────────────────────────────────────────────────

def authenticate_user(user_id: str, password: str) -> bool:
    """Check credentials against users table. Returns True if valid."""
    if not _DB_URL:
        return False
    try:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT id FROM users WHERE id = %s AND password_hash = %s",
            (user_id, password),
        )
        row = cur.fetchone()
        conn.close()
        return row is not None
    except Exception as e:
        _log.warning("authenticate_user error: %s", e)
        return False


def create_session(user_id: str) -> str:
    """Create a 30-day session token. Returns the token string."""
    token = str(uuid.uuid4())
    with _cursor() as cur:
        if cur is None:
            return token
        cur.execute(
            """
            INSERT INTO user_sessions (token, user_id, expires_at)
            VALUES (%s, %s, NOW() + INTERVAL '30 days')
            """,
            (token, user_id),
        )
    return token


def validate_session(token: str) -> str | None:
    """Check token validity. Returns user_id if valid and not expired, else None."""
    if not _DB_URL or not token:
        return None
    try:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT user_id FROM user_sessions WHERE token = %s AND expires_at > NOW()",
            (token,),
        )
        row = cur.fetchone()
        conn.close()
        return row["user_id"] if row else None
    except Exception as e:
        _log.warning("validate_session error: %s", e)
        return None


def invalidate_session(token: str):
    """Delete a session token (logout)."""
    with _cursor() as cur:
        if cur is None:
            return
        cur.execute("DELETE FROM user_sessions WHERE token = %s", (token,))


def update_last_login(user_id: str):
    """Update the last_login timestamp for a user."""
    with _cursor() as cur:
        if cur is None:
            return
        cur.execute(
            "UPDATE users SET last_login = NOW() WHERE id = %s",
            (user_id,),
        )


# ── Backtest cache ─────────────────────────────────────────────────────────────

def save_backtest(cache_type: str, results: list, best: dict, n_bars: int = 0):
    """Save backtest results to DB. cache_type: '1year' or '2008'."""
    with _cursor() as cur:
        if cur is None:
            return
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

def save_chat_message(session_id: str, role: str, content: str, user_id: str = "david"):
    """Persist a single chat message."""
    with _cursor() as cur:
        if cur is None:
            return
        cur.execute(
            "INSERT INTO advisor_chat (session_id, role, content, user_id) VALUES (%s, %s, %s, %s)",
            (session_id, role, content, user_id),
        )


def load_chat_history(session_id: str, user_id: str = None, limit: int = 40) -> list[dict]:
    """Load recent chat messages for a session. Returns list of {role, content}."""
    if not _DB_URL:
        return []
    try:
        conn = _get_conn()
        cur = conn.cursor()
        if user_id:
            cur.execute(
                """
                SELECT role, content FROM advisor_chat
                WHERE session_id = %s AND user_id = %s
                ORDER BY created_at DESC LIMIT %s
                """,
                (session_id, user_id, limit),
            )
        else:
            cur.execute(
                """
                SELECT role, content FROM advisor_chat
                WHERE session_id = %s
                ORDER BY created_at DESC LIMIT %s
                """,
                (session_id, limit),
            )
        rows = cur.fetchall()
        conn.close()
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]
    except Exception as e:
        _log.warning("load_chat_history error: %s", e)
        return []


def clear_chat_history(session_id: str, user_id: str = None):
    """Delete all messages for a session."""
    with _cursor() as cur:
        if cur is None:
            return
        if user_id:
            cur.execute(
                "DELETE FROM advisor_chat WHERE session_id = %s AND user_id = %s",
                (session_id, user_id),
            )
        else:
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
    user_id: str = "david",
):
    """Persist a completed trade."""
    with _cursor() as cur:
        if cur is None:
            return
        cur.execute(
            """
            INSERT INTO trades_history
                (direction, entry_price, sl_price, tp_price, exit_price,
                 pips, pnl, outcome, strategy, score, opened_at, closed_at, user_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                direction, entry_price, sl_price, tp_price, exit_price,
                pips, pnl, outcome, strategy, score,
                opened_at or datetime.now(timezone.utc),
                closed_at or datetime.now(timezone.utc),
                user_id,
            ),
        )


def load_trades(user_id: str = None, limit: int = 200) -> list[dict]:
    """Return the most recent trades, optionally filtered by user."""
    if not _DB_URL:
        return []
    try:
        conn = _get_conn()
        cur = conn.cursor()
        if user_id:
            cur.execute(
                """
                SELECT direction, entry_price, sl_price, tp_price, exit_price,
                       pips, pnl, outcome, strategy, score, opened_at, closed_at
                FROM trades_history WHERE user_id = %s
                ORDER BY created_at DESC LIMIT %s
                """,
                (user_id, limit),
            )
        else:
            cur.execute(
                """
                SELECT direction, entry_price, sl_price, tp_price, exit_price,
                       pips, pnl, outcome, strategy, score, opened_at, closed_at
                FROM trades_history
                ORDER BY created_at DESC LIMIT %s
                """,
                (limit,),
            )
        rows = cur.fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        _log.warning("load_trades error: %s", e)
        return []


def trades_summary(user_id: str = None) -> dict:
    """Aggregate stats: total trades, win rate, net pips, net P&L."""
    if not _DB_URL:
        return {}
    try:
        conn = _get_conn()
        cur = conn.cursor()
        where = "WHERE user_id = %s" if user_id else ""
        params = (user_id,) if user_id else ()
        cur.execute(
            f"""
            SELECT
                COUNT(*)                                                AS total,
                ROUND(AVG(CASE WHEN pips > 0 THEN 1 ELSE 0 END)*100, 1) AS winrate,
                ROUND(SUM(pips)::numeric, 1)                            AS net_pips,
                ROUND(SUM(pnl)::numeric, 2)                             AS net_pnl,
                COUNT(CASE WHEN outcome='TP' THEN 1 END)                AS tp_count,
                COUNT(CASE WHEN outcome='SL' THEN 1 END)                AS sl_count,
                COUNT(CASE WHEN outcome='BE' THEN 1 END)                AS be_count
            FROM trades_history {where}
            """,
            params,
        )
        row = cur.fetchone()
        conn.close()
        return dict(row) if row else {}
    except Exception as e:
        _log.warning("trades_summary error: %s", e)
        return {}


def get_user_trade_patterns(user_id: str) -> dict:
    """Analyze trade patterns for a specific user. Returns stats dict."""
    if not _DB_URL:
        return {}
    try:
        conn = _get_conn()
        cur = conn.cursor()

        # Overall summary
        cur.execute(
            """
            SELECT
                COUNT(*) AS total,
                ROUND(AVG(CASE WHEN pips > 0 THEN 1 ELSE 0 END)*100, 1) AS winrate,
                ROUND(SUM(pips)::numeric, 1) AS net_pips,
                ROUND(SUM(pnl)::numeric, 2) AS net_pnl
            FROM trades_history WHERE user_id = %s
            """,
            (user_id,),
        )
        summary = dict(cur.fetchone() or {})

        # By strategy
        cur.execute(
            """
            SELECT strategy,
                COUNT(*) AS total,
                ROUND(AVG(CASE WHEN pips > 0 THEN 1 ELSE 0 END)*100, 1) AS winrate,
                ROUND(SUM(pips)::numeric, 1) AS net_pips
            FROM trades_history WHERE user_id = %s AND strategy != ''
            GROUP BY strategy ORDER BY net_pips DESC LIMIT 5
            """,
            (user_id,),
        )
        by_strategy = [dict(r) for r in cur.fetchall()]

        # Recent 5 trades for streak
        cur.execute(
            """
            SELECT outcome FROM trades_history WHERE user_id = %s
            ORDER BY created_at DESC LIMIT 5
            """,
            (user_id,),
        )
        recent = [r["outcome"] for r in cur.fetchall()]
        streak = _compute_streak(recent)

        conn.close()
        return {
            **summary,
            "by_strategy": by_strategy,
            "recent_streak": streak,
        }
    except Exception as e:
        _log.warning("get_user_trade_patterns error: %s", e)
        return {}


def _compute_streak(outcomes: list) -> str:
    if not outcomes:
        return ""
    wins = sum(1 for o in outcomes if o == "TP")
    losses = sum(1 for o in outcomes if o == "SL")
    if wins == len(outcomes):
        return f"{wins} ganancias consecutivas"
    if losses == len(outcomes):
        return f"{losses} pérdidas consecutivas"
    return f"{wins}W / {losses}L últimas {len(outcomes)} ops"


# ── AI Memory ──────────────────────────────────────────────────────────────────

def save_ai_memory(
    user_id: str,
    memory_type: str,
    title: str,
    content: str,
    confidence: float = 0.7,
    source: str = "auto",
):
    """Save a learning or insight for a user."""
    with _cursor() as cur:
        if cur is None:
            return
        cur.execute(
            """
            INSERT INTO ai_memory (user_id, memory_type, title, content, confidence, source)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (user_id, memory_type, title, content, confidence, source),
        )


def load_ai_memories(user_id: str, memory_type: str = None, limit: int = 20) -> list[dict]:
    """Load AI memories for a user."""
    if not _DB_URL:
        return []
    try:
        conn = _get_conn()
        cur = conn.cursor()
        if memory_type:
            cur.execute(
                """
                SELECT memory_type, title, content, confidence, source, created_at
                FROM ai_memory WHERE user_id = %s AND memory_type = %s
                ORDER BY created_at DESC LIMIT %s
                """,
                (user_id, memory_type, limit),
            )
        else:
            cur.execute(
                """
                SELECT memory_type, title, content, confidence, source, created_at
                FROM ai_memory WHERE user_id = %s
                ORDER BY created_at DESC LIMIT %s
                """,
                (user_id, limit),
            )
        rows = cur.fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        _log.warning("load_ai_memories error: %s", e)
        return []


def clear_ai_memories(user_id: str):
    """Delete all AI memories for a user."""
    with _cursor() as cur:
        if cur is None:
            return
        cur.execute("DELETE FROM ai_memory WHERE user_id = %s", (user_id,))


def count_ai_memories(user_id: str) -> int:
    """Count stored memories for a user."""
    if not _DB_URL:
        return 0
    try:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS n FROM ai_memory WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
        conn.close()
        return int(row["n"]) if row else 0
    except Exception:
        return 0


# ── Market snapshots ───────────────────────────────────────────────────────────

def save_snapshot(price: float, signal: str, score: int, dxy_trend: str,
                  regime: str, strategy: str, extra: dict | None = None,
                  user_id: str = "david"):
    """Save a periodic market snapshot (called on each analysis run)."""
    with _cursor() as cur:
        if cur is None:
            return
        cur.execute(
            """
            INSERT INTO market_snapshots
                (price, signal, score, dxy_trend, regime, strategy, snapshot_data, user_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (price, signal, score, dxy_trend, regime, strategy,
             json.dumps(extra or {}), user_id),
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


# ── Strategy DNA ───────────────────────────────────────────────────────────────

def save_strategy_dna(version: int, rules: dict, fitness: float,
                      trades_evaluated: int, winrate: float, net_pips: float,
                      key_insight: str = "") -> int | None:
    """Save a new DNA version and mark it active; deactivate all previous."""
    if not _DB_URL:
        return None
    try:
        conn = _get_conn()
        cur = conn.cursor()
        # Deactivate previous
        cur.execute("UPDATE strategy_dna SET is_active = FALSE")
        # Insert new active version
        cur.execute(
            """
            INSERT INTO strategy_dna
                (version, rules, fitness, trades_evaluated, winrate, net_pips, key_insight, is_active)
            VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE)
            RETURNING id
            """,
            (version, json.dumps(rules), fitness, trades_evaluated, winrate, net_pips, key_insight),
        )
        row = cur.fetchone()
        conn.commit()
        conn.close()
        return row["id"] if row else None
    except Exception as e:
        _log.warning("save_strategy_dna error: %s", e)
        return None


def load_active_strategy() -> dict | None:
    """Load the currently active Strategy DNA rules. Returns rules dict or None."""
    if not _DB_URL:
        return None
    try:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT version, rules, fitness, trades_evaluated, winrate, net_pips,
                   key_insight, evolved_at
            FROM strategy_dna WHERE is_active = TRUE
            ORDER BY version DESC LIMIT 1
            """,
        )
        row = cur.fetchone()
        conn.close()
        if not row:
            return None
        dna = dict(row["rules"])
        dna["_version"]   = row["version"]
        dna["_fitness"]   = row["fitness"]
        dna["_trades"]    = row["trades_evaluated"]
        dna["_winrate"]   = row["winrate"]
        dna["_net_pips"]  = row["net_pips"]
        dna["_insight"]   = row["key_insight"]
        dna["_evolved_at"] = str(row["evolved_at"]) if row["evolved_at"] else None
        return dna
    except Exception as e:
        _log.warning("load_active_strategy error: %s", e)
        return None


def get_evolution_history(limit: int = 8) -> list[dict]:
    """Return list of past DNA versions (newest first)."""
    if not _DB_URL:
        return []
    try:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT version, fitness, winrate, net_pips, trades_evaluated,
                   key_insight, is_active, evolved_at
            FROM strategy_dna ORDER BY version DESC LIMIT %s
            """,
            (limit,),
        )
        rows = cur.fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        _log.warning("get_evolution_history error: %s", e)
        return []


def get_trades_for_evolution(limit: int = 60) -> list[dict]:
    """Return recent closed trades with market_snapshot for evolution analysis."""
    if not _DB_URL:
        return []
    try:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT direction, outcome, pips, strategy, score,
                   market_snapshot, dna_version, created_at
            FROM trades_history
            WHERE outcome IN ('TP','SL','BE')
            ORDER BY created_at DESC LIMIT %s
            """,
            (limit,),
        )
        rows = cur.fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        _log.warning("get_trades_for_evolution error: %s", e)
        return []


def count_trades_since_last_evolution() -> int:
    """Count closed trades that happened after the last evolution."""
    if not _DB_URL:
        return 0
    try:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute("SELECT evolved_at FROM strategy_dna WHERE is_active = TRUE LIMIT 1")
        row = cur.fetchone()
        last_dt = row["evolved_at"] if row else None
        if last_dt:
            cur.execute(
                "SELECT COUNT(*) AS n FROM trades_history WHERE outcome IN ('TP','SL','BE') AND created_at > %s",
                (last_dt,),
            )
        else:
            cur.execute(
                "SELECT COUNT(*) AS n FROM trades_history WHERE outcome IN ('TP','SL','BE')"
            )
        n = cur.fetchone()["n"]
        conn.close()
        return int(n)
    except Exception as e:
        _log.warning("count_trades_since_last_evolution error: %s", e)
        return 0


def save_trade_analysis(direction: str, outcome: str, pips: float,
                        strategy: str, score: int, market_snapshot: dict,
                        ai_analysis: str, dna_version: int = 1,
                        user_id: str = "david"):
    """Save post-mortem analysis for a completed trade."""
    with _cursor() as cur:
        if cur is None:
            return
        cur.execute(
            """
            INSERT INTO trade_analysis
                (direction, outcome, pips, strategy, score, market_snapshot,
                 ai_analysis, dna_version, user_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (direction, outcome, pips, strategy, score,
             json.dumps(market_snapshot), ai_analysis, dna_version, user_id),
        )


def save_trade_with_snapshot(
    direction: str, entry_price: float, sl_price: float, tp_price: float,
    outcome: str, pips: float, pnl: float, strategy: str = "", score: int = 0,
    exit_price: float | None = None, market_snapshot: dict | None = None,
    dna_version: int = 1, user_id: str = "david",
):
    """save_trade wrapper that also stores market_snapshot and dna_version."""
    from datetime import timezone
    with _cursor() as cur:
        if cur is None:
            return
        cur.execute(
            """
            INSERT INTO trades_history
                (direction, entry_price, sl_price, tp_price, exit_price,
                 pips, pnl, outcome, strategy, score,
                 market_snapshot, dna_version, user_id,
                 opened_at, closed_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                direction, entry_price, sl_price, tp_price, exit_price,
                pips, pnl, outcome, strategy, score,
                json.dumps(market_snapshot or {}), dna_version, user_id,
                datetime.now(timezone.utc), datetime.now(timezone.utc),
            ),
        )

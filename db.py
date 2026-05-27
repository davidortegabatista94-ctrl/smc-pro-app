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
    url = _DB_URL
    # Railway Postgres URLs sometimes start with postgres:// — psycopg2 needs postgresql://
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    return psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)


def ensure_tables() -> bool:
    """Create all required tables if they don't exist. Safe to call on every startup."""
    if not _DB_URL:
        return False
    try:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id VARCHAR(50) PRIMARY KEY,
            display_name VARCHAR(100) NOT NULL,
            password_hash VARCHAR(255) NOT NULL,
            mt5_login VARCHAR(100) DEFAULT '',
            mt5_password VARCHAR(100) DEFAULT '',
            mt5_server VARCHAR(100) DEFAULT '',
            created_at TIMESTAMPTZ DEFAULT NOW(),
            last_login TIMESTAMPTZ
        )""")
        cur.execute("""
        INSERT INTO users (id, display_name, password_hash) VALUES
            ('david', 'David', 'david'),
            ('javi', 'Javi', 'javi')
        ON CONFLICT (id) DO NOTHING""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS user_sessions (
            token VARCHAR(255) PRIMARY KEY,
            user_id VARCHAR(50) NOT NULL,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            expires_at TIMESTAMPTZ DEFAULT NOW() + INTERVAL '30 days'
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS advisor_chat (
            id SERIAL PRIMARY KEY,
            session_id VARCHAR(255) NOT NULL,
            role VARCHAR(20) NOT NULL,
            content TEXT NOT NULL,
            user_id VARCHAR(50) DEFAULT 'david',
            created_at TIMESTAMPTZ DEFAULT NOW()
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS trades_history (
            id SERIAL PRIMARY KEY,
            direction VARCHAR(10) NOT NULL,
            entry_price FLOAT,
            sl_price FLOAT,
            tp_price FLOAT,
            exit_price FLOAT,
            pips FLOAT DEFAULT 0,
            pnl FLOAT DEFAULT 0,
            outcome VARCHAR(20) DEFAULT '',
            strategy VARCHAR(100) DEFAULT '',
            score INT DEFAULT 0,
            market_snapshot JSONB DEFAULT '{}'::jsonb,
            dna_version INT DEFAULT 1,
            user_id VARCHAR(50) DEFAULT 'david',
            opened_at TIMESTAMPTZ DEFAULT NOW(),
            closed_at TIMESTAMPTZ DEFAULT NOW(),
            created_at TIMESTAMPTZ DEFAULT NOW()
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS market_snapshots (
            id SERIAL PRIMARY KEY,
            price FLOAT,
            signal VARCHAR(50),
            score INT DEFAULT 0,
            dxy_trend VARCHAR(50) DEFAULT '',
            regime VARCHAR(50) DEFAULT '',
            strategy VARCHAR(100) DEFAULT '',
            snapshot_data JSONB DEFAULT '{}'::jsonb,
            user_id VARCHAR(50) DEFAULT 'david',
            created_at TIMESTAMPTZ DEFAULT NOW()
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS backtest_cache (
            cache_type VARCHAR(50) PRIMARY KEY,
            results_json JSONB,
            best_json JSONB,
            n_bars INT DEFAULT 0,
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS app_settings (
            key VARCHAR(100) PRIMARY KEY,
            value TEXT DEFAULT '',
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS ai_memory (
            id SERIAL PRIMARY KEY,
            user_id VARCHAR(50) NOT NULL,
            memory_type VARCHAR(50) DEFAULT 'insight',
            title VARCHAR(200),
            content TEXT NOT NULL,
            confidence FLOAT DEFAULT 0.7,
            source VARCHAR(100) DEFAULT 'auto',
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS strategy_dna (
            id SERIAL PRIMARY KEY,
            version INT DEFAULT 1,
            rules JSONB DEFAULT '{}'::jsonb,
            fitness FLOAT DEFAULT 0,
            trades_evaluated INT DEFAULT 0,
            winrate FLOAT DEFAULT 0,
            net_pips FLOAT DEFAULT 0,
            key_insight TEXT DEFAULT '',
            is_active BOOLEAN DEFAULT FALSE,
            evolved_at TIMESTAMPTZ DEFAULT NOW()
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS trade_analysis (
            id SERIAL PRIMARY KEY,
            direction VARCHAR(10),
            outcome VARCHAR(20),
            pips FLOAT DEFAULT 0,
            strategy VARCHAR(100) DEFAULT '',
            score INT DEFAULT 0,
            market_snapshot JSONB DEFAULT '{}'::jsonb,
            ai_analysis TEXT DEFAULT '',
            dna_version INT DEFAULT 1,
            user_id VARCHAR(50) DEFAULT 'david',
            created_at TIMESTAMPTZ DEFAULT NOW()
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS error_log (
            id SERIAL PRIMARY KEY,
            component VARCHAR(100) DEFAULT '',
            severity VARCHAR(20) DEFAULT 'warning',
            message TEXT DEFAULT '',
            traceback TEXT DEFAULT '',
            context JSONB DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS performance_metrics (
            id SERIAL PRIMARY KEY,
            metric_name VARCHAR(100) NOT NULL,
            value FLOAT NOT NULL,
            context JSONB DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS self_improvements (
            id SERIAL PRIMARY KEY,
            improvement_type VARCHAR(50) DEFAULT 'heal_cycle',
            before_state JSONB DEFAULT '{}'::jsonb,
            after_state JSONB DEFAULT '{}'::jsonb,
            reason TEXT DEFAULT '',
            applied BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )""")
        # Indexes
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ai_memory_user ON ai_memory(user_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_chat_session ON advisor_chat(session_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_trades_user ON trades_history(user_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user ON user_sessions(user_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_sessions_exp ON user_sessions(expires_at)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_snapshots_ts ON market_snapshots(created_at)")
        conn.commit()
        conn.close()
        _log.info("DB tables ensured OK")
        return True
    except Exception as e:
        _log.warning("ensure_tables error: %s", e)
        return False


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


# ── App settings key-value ─────────────────────────────────────────────────────

def get_setting(key: str) -> str | None:
    """Read a global app setting by key."""
    if not _DB_URL:
        return None
    try:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute("SELECT value FROM app_settings WHERE key = %s", (key,))
        row = cur.fetchone()
        conn.close()
        return row["value"] if row else None
    except Exception as e:
        _log.warning("get_setting error: %s", e)
        return None


def set_setting(key: str, value: str):
    """Write or update a global app setting."""
    with _cursor() as cur:
        if cur is None:
            return
        cur.execute(
            """
            INSERT INTO app_settings (key, value, updated_at)
            VALUES (%s, %s, NOW())
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
            """,
            (key, value),
        )


# ── Per-user MT5 credentials ───────────────────────────────────────────────────

def save_user_mt5(user_id: str, mt5_login: str, mt5_password: str, mt5_server: str):
    """Persist MT5 credentials for a user."""
    with _cursor() as cur:
        if cur is None:
            return
        cur.execute(
            """
            UPDATE users SET mt5_login = %s, mt5_password = %s, mt5_server = %s
            WHERE id = %s
            """,
            (mt5_login or "", mt5_password or "", mt5_server or "", user_id),
        )


def load_user_mt5(user_id: str) -> dict | None:
    """Load saved MT5 credentials for a user. Returns dict or None."""
    if not _DB_URL:
        return None
    try:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT mt5_login, mt5_password, mt5_server FROM users WHERE id = %s",
            (user_id,),
        )
        row = cur.fetchone()
        conn.close()
        if not row:
            return None
        if not row["mt5_login"]:
            return None
        return {"mt5_login": row["mt5_login"], "mt5_password": row["mt5_password"],
                "mt5_server": row["mt5_server"]}
    except Exception as e:
        _log.warning("load_user_mt5 error: %s", e)
        return None


# ── Recent market snapshots (for hourly analysis) ─────────────────────────────

def get_recent_snapshots(hours: int = 4, limit: int = 30) -> list[dict]:
    """Return market snapshots from the last N hours, newest first."""
    if not _DB_URL:
        return []
    try:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT price, signal, score, dxy_trend, regime, strategy,
                   snapshot_data, created_at
            FROM market_snapshots
            WHERE created_at > NOW() - INTERVAL '%s hours'
            ORDER BY created_at DESC LIMIT %s
            """,
            (hours, limit),
        )
        rows = cur.fetchall()
        conn.close()
        result = []
        for r in rows:
            d = dict(r)
            if isinstance(d.get("snapshot_data"), str):
                try:
                    import json as _j
                    d["snapshot_data"] = _j.loads(d["snapshot_data"])
                except Exception:
                    d["snapshot_data"] = {}
            result.append(d)
        return result
    except Exception as e:
        _log.warning("get_recent_snapshots error: %s", e)
        return []


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


def get_strategy_dna_history(limit: int = 10) -> list[dict]:
    """Alias de get_evolution_history para strategy_learner."""
    return get_evolution_history(limit=limit)


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


# ── Error log ──────────────────────────────────────────────────────────────────

def log_app_error(component: str, severity: str, message: str,
                  traceback: str = "", context: dict | None = None) -> None:
    """Store an application error to error_log table."""
    with _cursor() as cur:
        if cur is None:
            return
        cur.execute(
            """INSERT INTO error_log (component, severity, message, traceback, context)
               VALUES (%s, %s, %s, %s, %s)""",
            (component[:100], severity[:20], message[:500], traceback[:1000],
             json.dumps(context or {})),
        )


def get_error_log(hours: int = 6, limit: int = 50) -> list[dict]:
    """Return recent error log entries."""
    if not _DB_URL:
        return []
    try:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute(
            """SELECT component, severity, message, context, created_at
               FROM error_log
               WHERE created_at > NOW() - INTERVAL '%s hours'
               ORDER BY created_at DESC LIMIT %s""",
            (hours, limit),
        )
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows
    except Exception as e:
        _log.warning("get_error_log error: %s", e)
        return []


# ── Performance metrics ────────────────────────────────────────────────────────

def save_metric(name: str, value: float, context: dict | None = None) -> None:
    """Store a scalar metric."""
    with _cursor() as cur:
        if cur is None:
            return
        cur.execute(
            """INSERT INTO performance_metrics (metric_name, value, context)
               VALUES (%s, %s, %s)""",
            (name[:100], float(value), json.dumps(context or {})),
        )


def get_metrics(name: str, limit: int = 100) -> list[dict]:
    """Return recent metrics for a given name."""
    if not _DB_URL:
        return []
    try:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute(
            """SELECT value, context, created_at FROM performance_metrics
               WHERE metric_name = %s ORDER BY created_at DESC LIMIT %s""",
            (name, limit),
        )
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows
    except Exception as e:
        _log.warning("get_metrics error: %s", e)
        return []


# ── Self-improvements log ──────────────────────────────────────────────────────

def save_self_improvement(improvement_type: str, before: dict, after: dict,
                          reason: str, applied: bool = False) -> None:
    """Store a self-improvement record."""
    with _cursor() as cur:
        if cur is None:
            return
        cur.execute(
            """INSERT INTO self_improvements
                   (improvement_type, before_state, after_state, reason, applied)
               VALUES (%s, %s, %s, %s, %s)""",
            (improvement_type[:50], json.dumps(before), json.dumps(after),
             reason[:300], applied),
        )


def get_self_improvements(limit: int = 20) -> list[dict]:
    """Return recent self-improvement records."""
    if not _DB_URL:
        return []
    try:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute(
            """SELECT improvement_type, before_state, after_state, reason, applied, created_at
               FROM self_improvements ORDER BY created_at DESC LIMIT %s""",
            (limit,),
        )
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows
    except Exception as e:
        _log.warning("get_self_improvements error: %s", e)
        return []


def purge_bad_self_improvements() -> int:
    """Delete garbage self-improvement records (AI errors and raw <think> blocks)."""
    if not _DB_URL:
        return 0
    try:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute(
            """DELETE FROM self_improvements
               WHERE reason LIKE '⚠️ Todos los proveedores%'
                  OR reason LIKE '<think>%'
                  OR reason LIKE '%<think>%'
                  OR reason LIKE '{%'
                  OR reason LIKE '{ %'
                  OR reason LIKE '%"health_status"%'
                  OR LENGTH(TRIM(reason)) < 15"""
        )
        deleted = cur.rowcount
        conn.commit()
        conn.close()
        _log.info("purge_bad_self_improvements: deleted %d rows", deleted)
        return deleted
    except Exception as e:
        _log.warning("purge_bad_self_improvements error: %s", e)
        return 0


def get_last_snapshot() -> dict | None:
    """Return the most recent market snapshot (from any source including bg worker)."""
    if not _DB_URL:
        return None
    try:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute(
            """SELECT price, signal, score, dxy_trend, regime, strategy,
                      snapshot_data, created_at
               FROM market_snapshots
               ORDER BY created_at DESC LIMIT 1"""
        )
        row = cur.fetchone()
        conn.close()
        if not row:
            return None
        result = dict(row)
        if isinstance(result.get("snapshot_data"), str):
            try:
                result["snapshot_data"] = json.loads(result["snapshot_data"])
            except Exception:
                pass
        return result
    except Exception as e:
        _log.warning("get_last_snapshot error: %s", e)
        return None

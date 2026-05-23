"""Migration 4: error_log, performance_metrics, self_improvements tables."""
import psycopg2, psycopg2.extras

DB_URL = "postgresql://postgres:ouNdHWjbawrNXRWWuvXVpMQdUCqcNlbR@kodama.proxy.rlwy.net:18031/railway"
conn = psycopg2.connect(DB_URL, cursor_factory=psycopg2.extras.RealDictCursor)
cur = conn.cursor()

cur.execute("""
CREATE TABLE IF NOT EXISTS error_log (
    id SERIAL PRIMARY KEY,
    component VARCHAR(100) DEFAULT '',
    severity VARCHAR(20) DEFAULT 'warning',
    message TEXT DEFAULT '',
    traceback TEXT DEFAULT '',
    context JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ DEFAULT NOW()
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS performance_metrics (
    id SERIAL PRIMARY KEY,
    metric_name VARCHAR(100) NOT NULL,
    value FLOAT NOT NULL,
    context JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ DEFAULT NOW()
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS self_improvements (
    id SERIAL PRIMARY KEY,
    improvement_type VARCHAR(50) DEFAULT 'heal_cycle',
    before_state JSONB DEFAULT '{}'::jsonb,
    after_state JSONB DEFAULT '{}'::jsonb,
    reason TEXT DEFAULT '',
    applied BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW()
)
""")

cur.execute("CREATE INDEX IF NOT EXISTS idx_error_log_ts ON error_log(created_at)")
cur.execute("CREATE INDEX IF NOT EXISTS idx_error_log_severity ON error_log(severity)")
cur.execute("CREATE INDEX IF NOT EXISTS idx_metrics_name ON performance_metrics(metric_name)")
cur.execute("CREATE INDEX IF NOT EXISTS idx_metrics_ts ON performance_metrics(created_at)")
cur.execute("CREATE INDEX IF NOT EXISTS idx_self_imp_ts ON self_improvements(created_at)")

conn.commit(); conn.close()
print("Schema 4 migrado OK")

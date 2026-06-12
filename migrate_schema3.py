"""Migration 3: app_settings key-value store + MT5 credentials per user."""
import psycopg2, psycopg2.extras

DB_URL = "postgresql://postgres:ouNdHWjbawrNXRWWuvXVpMQdUCqcNlbR@kodama.proxy.rlwy.net:18031/railway"
conn = psycopg2.connect(DB_URL, cursor_factory=psycopg2.extras.RealDictCursor)
cur = conn.cursor()

cur.execute("""
CREATE TABLE IF NOT EXISTS app_settings (
    key VARCHAR(100) PRIMARY KEY,
    value TEXT,
    updated_at TIMESTAMPTZ DEFAULT NOW()
)
""")

cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS mt5_login VARCHAR(50) DEFAULT ''")
cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS mt5_password VARCHAR(255) DEFAULT ''")
cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS mt5_server VARCHAR(100) DEFAULT ''")

conn.commit(); conn.close()
print("Schema 3 migrado OK")

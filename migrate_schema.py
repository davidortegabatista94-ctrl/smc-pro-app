import psycopg2
import psycopg2.extras

DB_URL = "postgresql://postgres:ouNdHWjbawrNXRWWuvXVpMQdUCqcNlbR@kodama.proxy.rlwy.net:18031/railway"
conn = psycopg2.connect(DB_URL, cursor_factory=psycopg2.extras.RealDictCursor)
cur = conn.cursor()

cur.execute("""
CREATE TABLE IF NOT EXISTS users (
    id VARCHAR(50) PRIMARY KEY,
    display_name VARCHAR(100) NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    last_login TIMESTAMPTZ
)
""")

cur.execute("""
INSERT INTO users (id, display_name, password_hash) VALUES
    ('david', 'David', 'david'),
    ('javi', 'Javi', 'javi')
ON CONFLICT (id) DO NOTHING
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS user_sessions (
    token VARCHAR(255) PRIMARY KEY,
    user_id VARCHAR(50) NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    expires_at TIMESTAMPTZ DEFAULT NOW() + INTERVAL '30 days'
)
""")

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
)
""")

cur.execute("ALTER TABLE advisor_chat ADD COLUMN IF NOT EXISTS user_id VARCHAR(50) DEFAULT 'david'")
cur.execute("ALTER TABLE trades_history ADD COLUMN IF NOT EXISTS user_id VARCHAR(50) DEFAULT 'david'")
cur.execute("ALTER TABLE market_snapshots ADD COLUMN IF NOT EXISTS user_id VARCHAR(50) DEFAULT 'david'")

cur.execute("CREATE INDEX IF NOT EXISTS idx_ai_memory_user_id ON ai_memory(user_id)")
cur.execute("CREATE INDEX IF NOT EXISTS idx_advisor_chat_user_id ON advisor_chat(user_id)")
cur.execute("CREATE INDEX IF NOT EXISTS idx_trades_user_id ON trades_history(user_id)")
cur.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON user_sessions(user_id)")
cur.execute("CREATE INDEX IF NOT EXISTS idx_sessions_expires ON user_sessions(expires_at)")

conn.commit()
conn.close()
print("Schema migrado correctamente")

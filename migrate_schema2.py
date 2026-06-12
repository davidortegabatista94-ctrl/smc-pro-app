"""Migration: add strategy_dna, trade_analysis tables + market_snapshot columns."""
import psycopg2
import psycopg2.extras

DB_URL = "postgresql://postgres:ouNdHWjbawrNXRWWuvXVpMQdUCqcNlbR@kodama.proxy.rlwy.net:18031/railway"
conn = psycopg2.connect(DB_URL, cursor_factory=psycopg2.extras.RealDictCursor)
cur = conn.cursor()

# Strategy DNA — versioned evolving rule set
cur.execute("""
CREATE TABLE IF NOT EXISTS strategy_dna (
    id SERIAL PRIMARY KEY,
    version INT NOT NULL DEFAULT 1,
    rules JSONB NOT NULL,
    fitness FLOAT DEFAULT 0,
    trades_evaluated INT DEFAULT 0,
    winrate FLOAT DEFAULT 0,
    net_pips FLOAT DEFAULT 0,
    is_active BOOLEAN DEFAULT FALSE,
    key_insight TEXT DEFAULT '',
    evolved_at TIMESTAMPTZ DEFAULT NOW()
)
""")

# Trade analysis — post-mortem records with market context
cur.execute("""
CREATE TABLE IF NOT EXISTS trade_analysis (
    id SERIAL PRIMARY KEY,
    direction VARCHAR(10),
    outcome VARCHAR(10),
    pips FLOAT,
    strategy VARCHAR(100),
    score INT,
    market_snapshot JSONB DEFAULT '{}'::jsonb,
    ai_analysis TEXT DEFAULT '',
    dna_version INT DEFAULT 1,
    user_id VARCHAR(50) DEFAULT 'david',
    created_at TIMESTAMPTZ DEFAULT NOW()
)
""")

# Add market_snapshot and dna_version to trades_history for evolution context
cur.execute("ALTER TABLE trades_history ADD COLUMN IF NOT EXISTS market_snapshot JSONB DEFAULT '{}'::jsonb")
cur.execute("ALTER TABLE trades_history ADD COLUMN IF NOT EXISTS dna_version INT DEFAULT 1")

# Indexes
cur.execute("CREATE INDEX IF NOT EXISTS idx_dna_active ON strategy_dna(is_active)")
cur.execute("CREATE INDEX IF NOT EXISTS idx_dna_version ON strategy_dna(version)")
cur.execute("CREATE INDEX IF NOT EXISTS idx_trade_analysis_outcome ON trade_analysis(outcome)")
cur.execute("CREATE INDEX IF NOT EXISTS idx_trade_analysis_user ON trade_analysis(user_id)")

conn.commit()
conn.close()
print("Schema 2 migrado OK")

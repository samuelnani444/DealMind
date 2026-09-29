import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATABASE_DIR = BASE_DIR / "database"
DATABASE_PATH = DATABASE_DIR / "dealmind.db"


def get_connection():
    DATABASE_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATABASE_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


def _ensure_column(connection, table, column, definition):
    existing = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def initialize_database():
    connection = get_connection()
    try:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("""
            CREATE TABLE IF NOT EXISTS deals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_name TEXT NOT NULL,
                deal_name TEXT NOT NULL,
                deal_value REAL DEFAULT 0,
                stage TEXT DEFAULT 'Discovery',
                probability INTEGER DEFAULT 0,
                contact_name TEXT,
                contact_email TEXT,
                notes TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS activities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                deal_id INTEGER NOT NULL,
                activity_type TEXT NOT NULL,
                description TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (deal_id) REFERENCES deals(id) ON DELETE CASCADE
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS analyses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                deal_id INTEGER NOT NULL,
                risk_level TEXT,
                summary TEXT,
                risks TEXT,
                opportunities TEXT,
                next_actions TEXT,
                health_score INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (deal_id) REFERENCES deals(id) ON DELETE CASCADE
            )
        """)
        _ensure_column(connection, "analyses", "health_score", "INTEGER DEFAULT 0")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_activities_deal_id ON activities(deal_id)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_analyses_deal_id ON analyses(deal_id)")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

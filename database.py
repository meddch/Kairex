import sqlite3
import os
from contextlib import contextmanager

DB_PATH = os.getenv("DB_PATH", "memory.db")


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def get_db():
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                id          TEXT PRIMARY KEY,
                org_id      TEXT NOT NULL,
                transcript  TEXT NOT NULL,
                status      TEXT NOT NULL DEFAULT 'processing',
                created_at  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS facts (
                id             TEXT PRIMARY KEY,
                session_id     TEXT NOT NULL,
                org_id         TEXT NOT NULL,
                entity_type    TEXT NOT NULL,
                entity_value   TEXT NOT NULL,
                attribute      TEXT NOT NULL,
                value          TEXT NOT NULL,
                confidence     REAL NOT NULL,
                status         TEXT NOT NULL DEFAULT 'pending',
                source_snippet TEXT,
                created_at     TEXT NOT NULL,
                FOREIGN KEY (session_id) REFERENCES sessions(id)
            );

            CREATE TABLE IF NOT EXISTS checkpoints (
                id          TEXT PRIMARY KEY,
                session_id  TEXT NOT NULL,
                fact_index  INTEGER NOT NULL,
                fact_data   TEXT NOT NULL,
                status      TEXT NOT NULL DEFAULT 'done',
                created_at  TEXT NOT NULL,
                UNIQUE(session_id, fact_index),
                FOREIGN KEY (session_id) REFERENCES sessions(id)
            );
        """)

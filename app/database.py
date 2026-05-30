"""
database.py — SQLite setup and connection management.
"""
import sqlite3
import os
from contextlib import contextmanager

DB_PATH = os.environ.get("DB_PATH", "data/store_intelligence.db")


def init_db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with get_conn() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS events (
            event_id     TEXT PRIMARY KEY,
            store_id     TEXT NOT NULL,
            camera_id    TEXT NOT NULL,
            visitor_id   TEXT NOT NULL,
            event_type   TEXT NOT NULL,
            timestamp    TEXT NOT NULL,
            zone_id      TEXT,
            dwell_ms     INTEGER DEFAULT 0,
            is_staff     INTEGER DEFAULT 0,
            confidence   REAL NOT NULL,
            queue_depth  INTEGER,
            sku_zone     TEXT,
            session_seq  INTEGER,
            group_id     TEXT,
            ingested_at  TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_events_store_ts
            ON events(store_id, timestamp);
        CREATE INDEX IF NOT EXISTS idx_events_visitor
            ON events(visitor_id, store_id);
        CREATE INDEX IF NOT EXISTS idx_events_type
            ON events(event_type, store_id);

        CREATE TABLE IF NOT EXISTS health_log (
            store_id         TEXT NOT NULL,
            last_event_ts    TEXT,
            updated_at       TEXT NOT NULL,
            PRIMARY KEY (store_id)
        );
        """)


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

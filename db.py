"""SQLite storage for crops, mandis and historical prices."""
import os
import sqlite3

DB_PATH = os.environ.get(
    "AGRI_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "agrisaarthi.db")
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS crops (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS markets (
    id       INTEGER PRIMARY KEY,
    name     TEXT NOT NULL,
    state    TEXT NOT NULL,
    district TEXT,
    lat      REAL,
    lon      REAL,
    UNIQUE (name, state)
);
CREATE TABLE IF NOT EXISTS prices (
    market_id   INTEGER NOT NULL REFERENCES markets(id),
    crop_id     INTEGER NOT NULL REFERENCES crops(id),
    date        TEXT    NOT NULL,           -- ISO yyyy-mm-dd
    min_price   REAL,
    max_price   REAL,
    modal_price REAL    NOT NULL,           -- Rs per quintal
    source      TEXT    NOT NULL,           -- 'agmarknet' | 'demo'
    PRIMARY KEY (market_id, crop_id, date)
);
CREATE INDEX IF NOT EXISTS idx_prices_crop ON prices (crop_id, market_id, date);
"""


def connect(path=None):
    path = path or DB_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn):
    conn.executescript(SCHEMA)
    conn.commit()


def upsert_crop(conn, name):
    conn.execute("INSERT OR IGNORE INTO crops (name) VALUES (?)", (name,))
    return conn.execute("SELECT id FROM crops WHERE name = ?", (name,)).fetchone()["id"]


def upsert_market(conn, name, state, district=None, lat=None, lon=None):
    conn.execute(
        """INSERT INTO markets (name, state, district, lat, lon) VALUES (?, ?, ?, ?, ?)
           ON CONFLICT (name, state) DO UPDATE SET
               district = COALESCE(excluded.district, markets.district),
               lat      = COALESCE(excluded.lat, markets.lat),
               lon      = COALESCE(excluded.lon, markets.lon)""",
        (name, state, district, lat, lon),
    )
    return conn.execute("SELECT id FROM markets WHERE name = ? AND state = ?", (name, state)).fetchone()["id"]

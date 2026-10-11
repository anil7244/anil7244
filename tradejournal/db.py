"""SQLite storage. `fills` is raw executions (never edited); `trades` is rebuilt from fills;
`journal` holds your notes keyed by a stable trade_uid so they survive rebuilds."""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS fills (
    id              INTEGER PRIMARY KEY,
    dedupe_key      TEXT NOT NULL UNIQUE,
    broker          TEXT NOT NULL,
    account         TEXT NOT NULL DEFAULT 'default',
    source          TEXT NOT NULL,
    trade_id        TEXT,
    order_id        TEXT,
    ts              TEXT NOT NULL,          -- ISO datetime (IST, naive)
    trade_date      TEXT NOT NULL,          -- YYYY-MM-DD
    exchange        TEXT,
    segment         TEXT,
    symbol_raw      TEXT,
    underlying      TEXT NOT NULL,
    kind            TEXT NOT NULL,          -- EQ / FUT / CE / PE
    expiry          TEXT,
    expiry_exact    INTEGER NOT NULL DEFAULT 1,
    strike          REAL,
    instrument_key  TEXT NOT NULL,
    side            TEXT NOT NULL CHECK (side IN ('BUY','SELL')),
    qty             REAL NOT NULL,
    price           REAL NOT NULL,
    charges         REAL,                   -- actual charges if the broker gave them
    product         TEXT,
    imported_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_fills_date ON fills(broker, account, trade_date);
CREATE INDEX IF NOT EXISTS ix_fills_inst ON fills(broker, account, instrument_key);

CREATE TABLE IF NOT EXISTS trades (
    trade_uid       TEXT PRIMARY KEY,
    broker          TEXT NOT NULL,
    account         TEXT NOT NULL,
    instrument_key  TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    underlying      TEXT NOT NULL,
    kind            TEXT NOT NULL,
    expiry          TEXT,
    strike          REAL,
    exchange        TEXT,
    segment         TEXT,
    direction       TEXT NOT NULL,          -- LONG / SHORT
    status          TEXT NOT NULL,          -- CLOSED / OPEN / EXPIRED / NEEDS_SETTLEMENT
    open_ts         TEXT NOT NULL,
    close_ts        TEXT,
    qty             REAL NOT NULL,          -- max position size
    entry_qty       REAL NOT NULL,
    exit_qty        REAL NOT NULL,
    entry_avg       REAL NOT NULL,
    exit_avg        REAL,
    gross_pnl       REAL NOT NULL,
    charges         REAL NOT NULL,
    charges_estimated INTEGER NOT NULL,
    net_pnl         REAL NOT NULL,
    num_fills       INTEGER NOT NULL,
    holding_minutes REAL
);
CREATE INDEX IF NOT EXISTS ix_trades_close ON trades(close_ts);

CREATE TABLE IF NOT EXISTS trade_fills (
    trade_uid   TEXT NOT NULL,
    fill_id     INTEGER NOT NULL,
    qty         REAL NOT NULL,
    role        TEXT NOT NULL,              -- ENTRY / EXIT / SETTLEMENT
    PRIMARY KEY (trade_uid, fill_id, role)
);

CREATE TABLE IF NOT EXISTS journal (
    trade_uid    TEXT PRIMARY KEY,
    strategy     TEXT,
    setup        TEXT,
    tags         TEXT,
    mistakes     TEXT,
    emotion      TEXT,
    rating       INTEGER,
    planned_risk REAL,
    notes        TEXT,
    screenshots  TEXT,                      -- newline-separated file names in data/screenshots
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS settlements (
    instrument_key TEXT PRIMARY KEY,
    price          REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_log (
    id        INTEGER PRIMARY KEY,
    broker    TEXT NOT NULL,
    source    TEXT NOT NULL,
    from_date TEXT,
    to_date   TEXT,
    fetched   INTEGER,
    inserted  INTEGER,
    at        TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

FILL_COLUMNS = [
    "dedupe_key", "broker", "account", "source", "trade_id", "order_id", "ts", "trade_date",
    "exchange", "segment", "symbol_raw", "underlying", "kind", "expiry", "expiry_exact", "strike",
    "instrument_key", "side", "qty", "price", "charges", "product",
]


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path or config.db_path()))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def make_dedupe_key(fill: dict) -> str:
    parts = [fill["broker"], fill["account"], fill["trade_date"], fill["instrument_key"], fill["side"]]
    if fill.get("trade_id"):
        parts += ["T", str(fill["trade_id"]), str(fill.get("order_id") or "")]
    else:
        parts += ["C", fill["ts"], f'{float(fill["qty"]):g}', f'{float(fill["price"]):.4f}',
                  str(fill.get("order_id") or ""), str(fill.get("_row_seq", ""))]
    return hashlib.sha1("|".join(parts).encode()).hexdigest()


def insert_fills(conn: sqlite3.Connection, fills: list[dict]) -> int:
    """Insert fills, ignoring ones already stored. Returns number of new rows."""
    sql = (
        f"INSERT INTO fills ({', '.join(FILL_COLUMNS)}) VALUES ({', '.join('?' * len(FILL_COLUMNS))}) "
        "ON CONFLICT(dedupe_key) DO UPDATE SET charges = COALESCE(fills.charges, excluded.charges) "
        "WHERE fills.charges IS NULL AND excluded.charges IS NOT NULL"
    )
    inserted = 0
    for f in fills:
        f.setdefault("dedupe_key", make_dedupe_key(f))
        cur = conn.execute("SELECT 1 FROM fills WHERE dedupe_key = ?", (f["dedupe_key"],))
        exists = cur.fetchone() is not None
        conn.execute(sql, [f.get(c) for c in FILL_COLUMNS])
        inserted += 0 if exists else 1
    conn.commit()
    return inserted


def log_sync(conn, broker, source, from_date, to_date, fetched, inserted):
    conn.execute(
        "INSERT INTO sync_log (broker, source, from_date, to_date, fetched, inserted) VALUES (?,?,?,?,?,?)",
        (broker, source, from_date, to_date, fetched, inserted),
    )
    conn.commit()

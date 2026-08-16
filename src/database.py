"""SQLite schema and helpers."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.config import DATABASE_PATH


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS raw_items(
  id INTEGER PRIMARY KEY,
  source TEXT,
  family TEXT CHECK(family IN('news','social')),
  url TEXT,
  title TEXT,
  text TEXT,
  author TEXT,
  lang TEXT,
  published_at TEXT,
  fetched_at TEXT,
  raw_hash TEXT UNIQUE
);

CREATE TABLE IF NOT EXISTS scores(
  id INTEGER PRIMARY KEY,
  raw_id INTEGER REFERENCES raw_items(id),
  family TEXT,
  scope TEXT CHECK(scope IN('coin','market')),
  coin TEXT,
  direction REAL,
  confidence REAL,
  event_type TEXT,
  magnitude INTEGER,
  is_shill INTEGER DEFAULT 0,
  model TEXT,
  batch_id TEXT,
  scored_at TEXT,
  UNIQUE(raw_id, coin)
);

CREATE TABLE IF NOT EXISTS index_hourly(
  ts TEXT,
  family TEXT,
  scope TEXT,
  coin TEXT,
  sent REAL,
  sent_z REAL,
  mentions INTEGER,
  confidence_flag TEXT,
  PRIMARY KEY(ts, family, coin)
);

CREATE TABLE IF NOT EXISTS overall_hourly(
  ts TEXT PRIMARY KEY,
  overall_news REAL,
  overall_social REAL,
  market_news REAL,
  breadth REAL
);

CREATE TABLE IF NOT EXISTS quality_log(
  ts TEXT,
  sample_n INTEGER,
  agreement_rate REAL,
  model_a TEXT,
  model_b TEXT
);

CREATE INDEX IF NOT EXISTS idx_scores_coin_ts ON scores(coin, scored_at);
CREATE INDEX IF NOT EXISTS idx_raw_ts ON raw_items(published_at);
"""


def get_db_path() -> Path:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    return DATABASE_PATH


def init_db() -> sqlite3.Connection:
    path = get_db_path()
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    return conn


def prune_raw_items(conn: sqlite3.Connection, days: int = 14) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    cur = conn.execute("DELETE FROM raw_items WHERE fetched_at < ?", (cutoff,))
    conn.commit()
    return cur.rowcount


def insert_raw_items(
    conn: sqlite3.Connection, items: list[dict[str, Any]]
) -> list[int]:
    """Insert raw items, skip duplicates. Returns inserted ids."""
    inserted: list[int] = []
    for item in items:
        try:
            cur = conn.execute(
                """
                INSERT INTO raw_items
                (source, family, url, title, text, author, lang, published_at, fetched_at, raw_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.get("source"),
                    item.get("family"),
                    item.get("url"),
                    item.get("title"),
                    item.get("text"),
                    item.get("author"),
                    item.get("lang"),
                    item.get("published_at"),
                    item.get("fetched_at"),
                    item.get("raw_hash"),
                ),
            )
            inserted.append(cur.lastrowid)
        except sqlite3.IntegrityError:
            continue
    conn.commit()
    return inserted


def fetch_unscored_raw_ids(conn: sqlite3.Connection, family: str | None = None) -> list[int]:
    sql = """
    SELECT id FROM raw_items r
    WHERE NOT EXISTS (SELECT 1 FROM scores s WHERE s.raw_id = r.id)
    """
    params: list[Any] = []
    if family:
        sql += " AND r.family = ?"
        params.append(family)
    cur = conn.execute(sql, params)
    return [row[0] for row in cur.fetchall()]

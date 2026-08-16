"""Tests for 15-minute slot aggregation semantics.

Covers: in-slot mention counting, carry-forward for empty slots, market-news
carry, z-score history excluding carried rows, and export downsampling.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from src.coins import Coin, CoinManager
from src.database import SCHEMA_SQL
from src.index import (
    FLAG_CARRIED,
    FLAG_LOW,
    FLAG_NORMAL,
    _downsample_for_export,
    aggregate_slot,
)

UTC = timezone.utc
SLOT = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)  # slot [12:00, 12:15)


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.executescript(SCHEMA_SQL)
    c.commit()
    return c


@pytest.fixture
def manager(monkeypatch: pytest.MonkeyPatch) -> CoinManager:
    coins = [
        Coin(symbol="BTC", name="Bitcoin", market_cap=1e12),
        Coin(symbol="ETH", name="Ethereum", market_cap=5e11),
    ]
    monkeypatch.setattr(CoinManager, "_fetch_coingecko", lambda self: coins)
    cm = CoinManager()
    cm.load()
    return cm


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def add_scored_item(
    conn: sqlite3.Connection,
    published_at: datetime,
    coin: str = "BTC",
    family: str = "news",
    scope: str = "coin",
    direction: float = 1.0,
    confidence: float = 1.0,
    source: str = "news_rss",
    raw_hash: str | None = None,
) -> None:
    raw_hash = raw_hash or f"h-{published_at.timestamp()}-{coin}-{family}-{scope}-{direction}"
    cur = conn.execute(
        """
        INSERT INTO raw_items
        (source, family, url, title, text, author, lang, published_at, fetched_at, raw_hash)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (source, family, "http://x", "t", "some long enough text body", "a", "en",
         _iso(published_at), _iso(published_at), raw_hash),
    )
    conn.execute(
        """
        INSERT INTO scores
        (raw_id, family, scope, coin, direction, confidence, event_type,
         magnitude, is_shill, model, batch_id, scored_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (cur.lastrowid, family, scope, coin, direction, confidence, "other",
         1, 0, "test", "b1", _iso(published_at)),
    )
    conn.commit()


def get_index_row(conn, ts: datetime, family: str, coin: str):
    return conn.execute(
        "SELECT sent, sent_z, mentions, confidence_flag FROM index_hourly "
        "WHERE ts = ? AND family = ? AND coin = ?",
        (_iso(ts), family, coin),
    ).fetchone()


def test_slot_mentions_only_count_in_slot(conn, manager) -> None:
    """Items published before the slot contribute to the decay window but not
    to the slot's mention count."""
    add_scored_item(conn, SLOT - timedelta(minutes=10))  # 11:50, previous slot
    add_scored_item(conn, SLOT + timedelta(minutes=5))   # 12:05, inside slot
    aggregate_slot(conn, manager, SLOT)
    sent, sent_z, mentions, flag = get_index_row(conn, SLOT, "news", "BTC")
    assert mentions == 1
    assert flag == FLAG_LOW  # < 3 new mentions in the slot
    assert sent > 0.9  # both scores are +1.0, decay barely matters


def test_empty_slot_carries_previous_value(conn, manager) -> None:
    """No new mentions in the slot + a previous value => carried, z cleared."""
    prev_ts = SLOT - timedelta(minutes=15)
    conn.execute(
        "INSERT INTO index_hourly (ts, family, scope, coin, sent, sent_z, mentions, confidence_flag) "
        "VALUES (?, 'news', 'coin', 'BTC', 0.42, 1.1, 5, ?)",
        (_iso(prev_ts), FLAG_NORMAL),
    )
    conn.commit()
    # In-window but out-of-slot data so the series stays eligible.
    add_scored_item(conn, SLOT - timedelta(hours=2))
    aggregate_slot(conn, manager, SLOT)
    sent, sent_z, mentions, flag = get_index_row(conn, SLOT, "news", "BTC")
    assert flag == FLAG_CARRIED
    assert sent == pytest.approx(0.42)
    assert sent_z is None
    assert mentions == 0


def test_empty_slot_without_prev_uses_window_value(conn, manager) -> None:
    """No new mentions and no previous value: fall back to the computed
    window value rather than skipping the series."""
    add_scored_item(conn, SLOT - timedelta(hours=2), coin="ETH")
    aggregate_slot(conn, manager, SLOT)
    row = get_index_row(conn, SLOT, "news", "ETH")
    assert row is not None
    assert row[3] == FLAG_LOW
    assert row[0] > 0.9


def test_market_news_carries_when_window_empty(conn, manager) -> None:
    prev_ts = SLOT - timedelta(minutes=15)
    conn.execute(
        "INSERT INTO overall_hourly (ts, overall_news, overall_social, market_news, breadth) "
        "VALUES (?, 0.1, 0.2, 0.33, 0.0)",
        (_iso(prev_ts),),
    )
    # Coin data exists, but no scope='market' rows in the window.
    add_scored_item(conn, SLOT + timedelta(minutes=3))
    conn.commit()
    stats = aggregate_slot(conn, manager, SLOT)
    assert stats["market_news"] == pytest.approx(0.33)


def test_z_score_excludes_carried_history(conn, manager) -> None:
    """A history made only of carried rows must not unlock the z-score."""
    for i in range(1, 101):
        h_ts = SLOT - timedelta(minutes=15 * i)
        conn.execute(
            "INSERT INTO index_hourly (ts, family, scope, coin, sent, sent_z, mentions, confidence_flag) "
            "VALUES (?, 'news', 'coin', 'BTC', 0.5, NULL, 0, ?)",
            (_iso(h_ts), FLAG_CARRIED),
        )
    conn.commit()
    add_scored_item(conn, SLOT + timedelta(minutes=2))
    aggregate_slot(conn, manager, SLOT)
    _, sent_z, _, flag = get_index_row(conn, SLOT, "news", "BTC")
    assert flag == FLAG_LOW
    assert sent_z is None  # all 100 history rows were carried => excluded


def test_export_downsampling() -> None:
    now = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    records = [
        {"ts": _iso(now - timedelta(minutes=30))},                    # recent 15-min: keep
        {"ts": _iso(now - timedelta(days=10))},                      # old, minute 0: keep
        {"ts": _iso(now - timedelta(days=10) + timedelta(minutes=15))},  # old, minute 15: drop
    ]
    out = _downsample_for_export(records, now)
    kept = {r["ts"] for r in out}
    assert records[0]["ts"] in kept
    assert records[1]["ts"] in kept
    assert records[2]["ts"] not in kept

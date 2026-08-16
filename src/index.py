"""Index aggregation and export."""
from __future__ import annotations

import json
import math
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.coins import CoinManager
from src.config import (
    DOCS_DATA_DIR,
    NEWS_HALF_LIFE_HOURS,
    SOCIAL_HALF_LIFE_HOURS,
    SOURCE_WEIGHTS,
    DATA_DIR,
)
from src.database import DATABASE_PATH
from src.utils import now_iso


def _hour_floor(dt: datetime) -> datetime:
    return dt.replace(minute=0, second=0, microsecond=0)


def _parse_iso(ts: str) -> datetime:
    # Trim possible timezone suffix to handle Python <3.11 edge cases.
    text = ts.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return datetime.now(timezone.utc)


def _decay_hours(published_at: str, ts: datetime, half_life: float) -> float:
    """Exponential decay factor."""
    pub_dt = _parse_iso(published_at)
    if pub_dt.tzinfo is None:
        pub_dt = pub_dt.replace(tzinfo=timezone.utc)
    hours = (ts - pub_dt).total_seconds() / 3600.0
    if hours < 0:
        return 1.0
    return 0.5 ** (hours / half_life)


def _source_weight(source: str, is_shill: bool) -> float:
    w = SOURCE_WEIGHTS.get(source, 1.0)
    if is_shill:
        w *= 0.2
    return w


def aggregate_hour(
    conn: sqlite3.Connection,
    coin_manager: CoinManager,
    ts: datetime | None = None,
) -> dict[str, Any]:
    """Aggregate scores into index_hourly and overall_hourly for the target hour."""
    if ts is None:
        ts = _hour_floor(datetime.now(timezone.utc))
    ts_iso = ts.isoformat()

    # Fetch recent scores so the decay window has enough history (news half-life 24h).
    cutoff = (ts - timedelta(hours=72)).isoformat()
    cursor = conn.execute(
        """
        SELECT s.coin, s.family, s.scope, s.direction, s.confidence,
               s.is_shill, r.source, r.published_at
        FROM scores s
        JOIN raw_items r ON r.id = s.raw_id
        WHERE s.scored_at > ?
        """,
        (cutoff,),
    )
    rows = cursor.fetchall()

    # Group by coin/family for coin-level index.
    coin_groups: dict[tuple[str, str], list[tuple]] = defaultdict(list)
    market_rows: list[tuple] = []

    for coin, family, scope, direction, confidence, is_shill, source, published_at in rows:
        if scope == "market":
            market_rows.append((coin, family, scope, direction, confidence, is_shill, source, published_at))
        else:
            coin_groups[(coin, family)].append((direction, confidence, is_shill, source, published_at))

    index_rows: list[tuple[str, str, str, str, float, float | None, int, str]] = []
    coin_sents: dict[str, dict[str, float]] = {}  # coin -> {news, social}

    for (coin, family), group in coin_groups.items():
        half_life = NEWS_HALF_LIFE_HOURS if family == "news" else SOCIAL_HALF_LIFE_HOURS
        weighted_sum = 0.0
        weight_sum = 0.0
        mentions = 0
        for direction, confidence, is_shill, source, published_at in group:
            d = _decay_hours(published_at, ts, half_life)
            w = _source_weight(source, bool(is_shill))
            c = float(confidence or 0)
            v = float(direction or 0)
            weighted_sum += v * c * w * d
            weight_sum += c * w * d
            mentions += 1
        sent = weighted_sum / weight_sum if weight_sum > 0 else 0.0
        sent_z = _compute_z_score(conn, coin, family, ts, sent)
        confidence_flag = "low" if mentions < 3 else "normal"
        index_rows.append(
            (ts_iso, family, "coin", coin, sent, sent_z, mentions, confidence_flag)
        )
        coin_sents.setdefault(coin, {})[family] = sent

    # Market news index.
    market_sent = 0.0
    market_weight_sum = 0.0
    for _, family, _, direction, confidence, is_shill, source, published_at in market_rows:
        half_life = NEWS_HALF_LIFE_HOURS
        d = _decay_hours(published_at, ts, half_life)
        w = _source_weight(source, bool(is_shill))
        c = float(confidence or 0)
        v = float(direction or 0)
        market_sent += v * c * w * d
        market_weight_sum += c * w * d
    market_sent = market_sent / market_weight_sum if market_weight_sum > 0 else 0.0

    # Overall indices.
    overall_news, overall_social, breadth = _compute_overall(
        coin_manager, coin_sents, market_sent
    )

    # Persist.
    _write_index_hourly(conn, index_rows)
    _write_overall_hourly(conn, ts_iso, overall_news, overall_social, market_sent, breadth)

    return {
        "ts": ts_iso,
        "index_rows": len(index_rows),
        "overall_news": overall_news,
        "overall_social": overall_social,
        "market_news": market_sent,
        "breadth": breadth,
    }


def _compute_z_score(
    conn: sqlite3.Connection,
    coin: str,
    family: str,
    ts: datetime,
    current_sent: float,
    lookback_days: int = 30,
) -> float | None:
    """Rolling z-score over prior lookback_days."""
    start = (ts - timedelta(days=lookback_days)).isoformat()
    end = ts.isoformat()
    cursor = conn.execute(
        "SELECT sent FROM index_hourly WHERE coin = ? AND family = ? AND ts >= ? AND ts < ?",
        (coin, family, start, end),
    )
    history = [row[0] for row in cursor.fetchall() if row[0] is not None]
    if len(history) < 10:  # Require at least 10 prior hours for stable z.
        return None
    mean = sum(history) / len(history)
    variance = sum((x - mean) ** 2 for x in history) / len(history)
    std = math.sqrt(variance) if variance > 0 else 0.0
    if std == 0:
        return None
    return (current_sent - mean) / std


def _compute_overall(
    coin_manager: CoinManager,
    coin_sents: dict[str, dict[str, float]],
    market_news: float,
) -> tuple[float, float, float]:
    """Compute overall_news, overall_social, breadth."""
    news_weighted = 0.0
    social_weighted = 0.0
    total_weight = 0.0
    for symbol, families in coin_sents.items():
        coin = coin_manager.coins.get(symbol)
        if not coin:
            continue
        mc = max(coin.market_cap, 0.0)
        total_weight += mc
        if "news" in families:
            news_weighted += families["news"] * mc
        if "social" in families:
            social_weighted += families["social"] * mc

    if total_weight > 0:
        overall_news = news_weighted / total_weight
        overall_social = social_weighted / total_weight
    else:
        overall_news = 0.0
        overall_social = 0.0

    # Blend market news into overall news (spec: +0.5*MARKET_NEWS then normalize).
    overall_news = (overall_news + 0.5 * market_news) / 1.5
    overall_news = max(-1.0, min(1.0, overall_news))
    overall_social = max(-1.0, min(1.0, overall_social))

    # Breadth: net bullish - net bearish.
    bullish = 0
    bearish = 0
    total_coins = 0
    for symbol, families in coin_sents.items():
        coin = coin_manager.coins.get(symbol)
        if not coin:
            continue
        total_coins += 1
        # Use average of news and social if both present.
        vals = [v for v in families.values()]
        avg = sum(vals) / len(vals) if vals else 0.0
        if avg > 0.1:
            bullish += 1
        elif avg < -0.1:
            bearish += 1

    breadth = 0.0
    if total_coins > 0:
        breadth = (bullish / total_coins) - (bearish / total_coins)
    return overall_news, overall_social, breadth


def _write_index_hourly(
    conn: sqlite3.Connection,
    rows: list[tuple[str, str, str, str, float, float | None, int, str]],
) -> None:
    for row in rows:
        conn.execute(
            """
            INSERT INTO index_hourly (ts, family, scope, coin, sent, sent_z, mentions, confidence_flag)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ts, family, coin) DO UPDATE SET
              sent=excluded.sent,
              sent_z=excluded.sent_z,
              mentions=excluded.mentions,
              confidence_flag=excluded.confidence_flag
            """,
            row,
        )
    conn.commit()


def _write_overall_hourly(
    conn: sqlite3.Connection,
    ts: str,
    overall_news: float,
    overall_social: float,
    market_news: float,
    breadth: float,
) -> None:
    conn.execute(
        """
        INSERT INTO overall_hourly (ts, overall_news, overall_social, market_news, breadth)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(ts) DO UPDATE SET
          overall_news=excluded.overall_news,
          overall_social=excluded.overall_social,
          market_news=excluded.market_news,
          breadth=excluded.breadth
        """,
        (ts, overall_news, overall_social, market_news, breadth),
    )
    conn.commit()


def export_json(
    conn: sqlite3.Connection,
    coin_manager: CoinManager,
    ts: datetime,
) -> dict[str, Any]:
    """Export dashboard JSON files."""
    DOCS_DATA_DIR.mkdir(parents=True, exist_ok=True)
    cutoff = (ts - timedelta(days=90)).isoformat()

    # meta.json
    cursor = conn.execute(
        "SELECT sample_n, agreement_rate FROM quality_log ORDER BY ts DESC LIMIT 1"
    )
    quality = cursor.fetchone()
    quality_summary = {
        "sample_n": quality[0] if quality else 0,
        "agreement_rate": quality[1] if quality else None,
    }
    meta = {
        "coins": sorted(coin_manager.coins.keys()),
        "updated_at": ts.isoformat(),
        "quality_summary": quality_summary,
    }
    (DOCS_DATA_DIR / "meta.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # overall.json
    cursor = conn.execute(
        "SELECT ts, overall_news, overall_social, market_news, breadth "
        "FROM overall_hourly WHERE ts >= ? ORDER BY ts",
        (cutoff,),
    )
    overall_records = [
        {
            "ts": row[0],
            "overall_news": row[1],
            "overall_social": row[2],
            "market_news": row[3],
            "breadth": row[4],
        }
        for row in cursor.fetchall()
    ]
    (DOCS_DATA_DIR / "overall.json").write_text(
        json.dumps(overall_records, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # Per-coin files.
    (DOCS_DATA_DIR / "coins").mkdir(parents=True, exist_ok=True)
    cursor = conn.execute(
        "SELECT DISTINCT coin FROM index_hourly WHERE ts >= ?",
        (cutoff,),
    )
    coins_in_index = [row[0] for row in cursor.fetchall()]
    for symbol in coins_in_index:
        cursor = conn.execute(
            "SELECT ts, family, sent, sent_z, mentions, confidence_flag "
            "FROM index_hourly WHERE coin = ? AND ts >= ? ORDER BY ts",
            (symbol, cutoff),
        )
        records = [
            {
                "ts": row[0],
                "family": row[1],
                "sent": row[2],
                "sent_z": row[3],
                "mentions": row[4],
                "confidence_flag": row[5],
            }
            for row in cursor.fetchall()
        ]
        (DOCS_DATA_DIR / "coins" / f"{symbol}.json").write_text(
            json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    return {
        "meta": meta,
        "overall_records": len(overall_records),
        "coin_files": len(coins_in_index),
    }


def export_csv_backup(
    conn: sqlite3.Connection,
    ts: datetime,
) -> dict[str, Path]:
    """Backup index tables to CSV in data/index_export/."""
    import csv

    export_dir = DATA_DIR / "index_export"
    export_dir.mkdir(parents=True, exist_ok=True)
    stamp = ts.strftime("%Y%m%d_%H%M")
    paths: dict[str, Path] = {}

    for table in ["index_hourly", "overall_hourly"]:
        path = export_dir / f"{table}_{stamp}.csv"
        cursor = conn.execute(f"SELECT * FROM {table}")
        columns = [desc[0] for desc in cursor.description]
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(columns)
            writer.writerows(cursor.fetchall())
        paths[table] = path
    return paths

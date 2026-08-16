"""Index aggregation (15-minute slots) and export."""
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

# Aggregation cadence. The dashboard renders one point per slot.
SLOT_MINUTES = 15
# Decay window for the SENT weighted average (news half-life is 24h, so 72h
# keeps ~3 half-lives of history in the denominator).
WINDOW_HOURS = 72
# Z-score needs roughly one day of 15-minute slots before it is shown.
MIN_Z_SAMPLES = 96
# How far back to look for (coin, family) series worth carrying forward.
CARRY_KEY_LOOKBACK_HOURS = 24
# Export: keep native 15-minute resolution for the most recent N days,
# downsample older history to one point per hour to bound repo growth.
EXPORT_FULL_RES_DAYS = 7

FLAG_NORMAL = "normal"
FLAG_LOW = "low"        # fewer than 3 new mentions in the slot
FLAG_CARRIED = "carried"  # no new mentions in the slot; value carried forward

# Discount applied when blending market-wide (scope='market') news into each
# coin's news sentiment. Macro/policy news (Fed, regulation, ETFs) moves all
# coins, but should not drown out coin-specific evidence.
MARKET_BLEND = 0.5


def _slot_floor(dt: datetime) -> datetime:
    minute = (dt.minute // SLOT_MINUTES) * SLOT_MINUTES
    return dt.replace(minute=minute, second=0, microsecond=0)


def _parse_iso(ts: str) -> datetime:
    # Trim possible timezone suffix to handle Python <3.11 edge cases.
    text = ts.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _decay_hours(published_at: str, ts: datetime, half_life: float) -> float:
    """Exponential decay factor."""
    pub_dt = _parse_iso(published_at)
    hours = (ts - pub_dt).total_seconds() / 3600.0
    if hours < 0:
        return 1.0
    return 0.5 ** (hours / half_life)


def _source_weight(source: str, is_shill: bool) -> float:
    w = SOURCE_WEIGHTS.get(source, 1.0)
    if is_shill:
        w *= 0.2
    return w


def aggregate_slot(
    conn: sqlite3.Connection,
    coin_manager: CoinManager,
    ts: datetime | None = None,
) -> dict[str, Any]:
    """Aggregate scores into index_hourly / overall_hourly for one 15-min slot.

    Semantics:
    - ``sent`` is always computed over the trailing 72h decay window (spec).
    - ``mentions`` counts only texts whose published_at falls inside
      [slot_start, slot_end) — never earlier slots.
    - Slots with zero new mentions carry the previous value forward with
      ``confidence_flag='carried'`` and ``sent_z=None`` so the dashboard can
      mark them explicitly.
    - Market-wide news (scope='market') is blended into every coin's news
      channel at MARKET_BLEND weight, so crypto-wide policy/macro events
      move all coins without drowning out coin-specific evidence.
    """
    if ts is None:
        ts = _slot_floor(datetime.now(timezone.utc))
    else:
        ts = _slot_floor(ts)
    ts_iso = ts.isoformat()
    slot_end = ts + timedelta(minutes=SLOT_MINUTES)

    # Fetch recent scores so the decay window has enough history.
    cutoff = (ts - timedelta(hours=WINDOW_HOURS)).isoformat()
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

    # Series that recently had a value are eligible for carry-forward even if
    # they fell out of the 72h scoring window.
    key_lookback = (ts - timedelta(hours=CARRY_KEY_LOOKBACK_HOURS)).isoformat()
    cursor = conn.execute(
        "SELECT DISTINCT coin, family FROM index_hourly WHERE ts >= ? AND ts < ?",
        (key_lookback, ts_iso),
    )
    recent_keys = {(row[0], row[1]) for row in cursor.fetchall()}
    all_keys = set(coin_groups.keys()) | recent_keys

    # Market news index (computed first: coin news sentiment blends it in).
    # Carry the previous value when the window is empty.
    market_sent = 0.0
    market_weight_sum = 0.0
    for _, family, _, direction, confidence, is_shill, source, published_at in market_rows:
        d = _decay_hours(published_at, ts, NEWS_HALF_LIFE_HOURS)
        w = _source_weight(source, bool(is_shill))
        c = float(confidence or 0)
        v = float(direction or 0)
        market_sent += v * c * w * d
        market_weight_sum += c * w * d
    if market_weight_sum > 0:
        market_sent = market_sent / market_weight_sum
    else:
        cursor = conn.execute(
            "SELECT market_news FROM overall_hourly WHERE ts < ? ORDER BY ts DESC LIMIT 1",
            (ts_iso,),
        )
        prev_overall = cursor.fetchone()
        market_sent = float(prev_overall[0]) if prev_overall else 0.0

    index_rows: list[tuple[str, str, str, str, float, float | None, int, str]] = []
    coin_sents: dict[str, dict[str, float]] = {}  # coin -> {news, social}

    for coin, family in sorted(all_keys):
        group = coin_groups.get((coin, family), [])
        half_life = NEWS_HALF_LIFE_HOURS if family == "news" else SOCIAL_HALF_LIFE_HOURS

        weighted_sum = 0.0
        weight_sum = 0.0
        slot_mentions = 0
        for direction, confidence, is_shill, source, published_at in group:
            d = _decay_hours(published_at, ts, half_life)
            w = _source_weight(source, bool(is_shill))
            c = float(confidence or 0)
            v = float(direction or 0)
            weighted_sum += v * c * w * d
            weight_sum += c * w * d
            pub_dt = _parse_iso(published_at)
            if ts <= pub_dt < slot_end:
                slot_mentions += 1
        # Blend market-wide news into the coin's news channel at a discounted
        # weight, so crypto-wide policy/macro events nudge every coin.
        if family == "news":
            for _, _, _, direction, confidence, is_shill, source, published_at in market_rows:
                d = _decay_hours(published_at, ts, half_life)
                w = _source_weight(source, bool(is_shill)) * MARKET_BLEND
                c = float(confidence or 0)
                v = float(direction or 0)
                weighted_sum += v * c * w * d
                weight_sum += c * w * d
        computed_sent = weighted_sum / weight_sum if weight_sum > 0 else None

        # Previous value for carry-forward.
        cursor = conn.execute(
            "SELECT sent FROM index_hourly "
            "WHERE coin = ? AND family = ? AND ts < ? ORDER BY ts DESC LIMIT 1",
            (coin, family, ts_iso),
        )
        prev = cursor.fetchone()

        if slot_mentions == 0 and prev is not None:
            sent = float(prev[0])
            sent_z = None
            flag = FLAG_CARRIED
        elif computed_sent is None:
            # No window data and no previous value: nothing to write.
            continue
        else:
            sent = computed_sent
            sent_z = _compute_z_score(conn, coin, family, ts, sent)
            flag = FLAG_LOW if slot_mentions < 3 else FLAG_NORMAL

        index_rows.append((ts_iso, family, "coin", coin, sent, sent_z, slot_mentions, flag))
        coin_sents.setdefault(coin, {})[family] = sent

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
        "carried_rows": sum(1 for r in index_rows if r[7] == FLAG_CARRIED),
        "overall_news": overall_news,
        "overall_social": overall_social,
        "market_news": market_sent,
        "breadth": breadth,
    }


def aggregate_recent(
    conn: sqlite3.Connection,
    coin_manager: CoinManager,
    now: datetime | None = None,
    slots: int = 2,
) -> list[dict[str, Any]]:
    """Aggregate the current slot plus ``slots - 1`` preceding ones.

    Re-aggregating the previous slot is upsert-safe and picks up mentions from
    items that were published in that slot but scored slightly late.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    base = _slot_floor(now)
    stats = []
    for i in range(slots - 1, -1, -1):
        slot_ts = base - timedelta(minutes=SLOT_MINUTES * i)
        stats.append(aggregate_slot(conn, coin_manager, slot_ts))
    return stats


def _compute_z_score(
    conn: sqlite3.Connection,
    coin: str,
    family: str,
    ts: datetime,
    current_sent: float,
    lookback_days: int = 30,
) -> float | None:
    """Rolling z-score over prior lookback_days (carried rows excluded)."""
    start = (ts - timedelta(days=lookback_days)).isoformat()
    end = ts.isoformat()
    cursor = conn.execute(
        "SELECT sent FROM index_hourly "
        "WHERE coin = ? AND family = ? AND ts >= ? AND ts < ? "
        "AND confidence_flag != ?",
        (coin, family, start, end, FLAG_CARRIED),
    )
    history = [row[0] for row in cursor.fetchall() if row[0] is not None]
    if len(history) < MIN_Z_SAMPLES:
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


def _downsample_for_export(
    records: list[dict[str, Any]],
    ts_now: datetime,
    full_res_days: int = EXPORT_FULL_RES_DAYS,
) -> list[dict[str, Any]]:
    """Keep native 15-min points for recent history; older points hourly only."""
    cutoff = ts_now - timedelta(days=full_res_days)
    out: list[dict[str, Any]] = []
    for rec in records:
        dt = _parse_iso(rec["ts"])
        if dt >= cutoff or dt.minute == 0:
            out.append(rec)
    return out


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
        "coins": [
            {"symbol": c.symbol_upper, "name": c.name}
            for c in sorted(
                coin_manager.coins.values(), key=lambda c: c.market_cap, reverse=True
            )
        ],
        "updated_at": ts.isoformat(),
        "slot_minutes": SLOT_MINUTES,
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
    overall_records = _downsample_for_export(
        [
            {
                "ts": row[0],
                "overall_news": row[1],
                "overall_social": row[2],
                "market_news": row[3],
                "breadth": row[4],
            }
            for row in cursor.fetchall()
        ],
        ts,
    )
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
        records = _downsample_for_export(
            [
                {
                    "ts": row[0],
                    "family": row[1],
                    "sent": row[2],
                    "sent_z": row[3],
                    "mentions": row[4],
                    "confidence_flag": row[5],
                }
                for row in cursor.fetchall()
            ],
            ts,
        )
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

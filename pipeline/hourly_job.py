"""Hourly sentiment pipeline orchestrator."""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Ensure project root is importable.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.coins import CoinManager
from src.config import (
    DATABASE_PATH,
    DOCS_DATA_DIR,
    LLM_CONCURRENCY,
    LLM_MAX_CALLS_PER_RUN,
    MIN_TEXT_LENGTH,
)
from src.aliases import refresh_aliases
from src.database import init_db, insert_raw_items, prune_raw_items
from src.index import aggregate_recent, export_csv_backup, export_json
from src.llm_client import LLMClient, normalize_symbol, scored_at_now
from src.quality import run_self_check
from src.utils import clean_text, now_iso
from sources.base import RawItem, SourceBase
from sources.binance_ann import BinanceAnnSource
from sources.bing_news import BingNewsSource
from sources.bluesky import BlueskySource
from sources.chan_biz import FourChanBizSource
from sources.cryptopanic import CryptoPanicSource
from sources.google_news import GoogleNewsSource
from sources.news_rss import NewsRSSSource
from sources.reddit_arctic import RedditArcticSource
from sources.stocktwits import StocktwitsSource


ALL_SOURCES: list[type[SourceBase]] = [
    NewsRSSSource,
    GoogleNewsSource,
    BingNewsSource,
    BinanceAnnSource,
    CryptoPanicSource,
    BlueskySource,
    StocktwitsSource,
    RedditArcticSource,
    FourChanBizSource,
]

SOCIAL_BATCH_SIZE = 15


def is_ad_or_url(text: str) -> bool:
    """Basic prefilter."""
    t = text.strip()
    if len(t) < MIN_TEXT_LENGTH:
        return True
    # Pure URL.
    if t.startswith("http") and len(t.split()) <= 2:
        return True
    return False


def prefilter_items(items: list[RawItem]) -> list[RawItem]:
    return [item for item in items if not is_ad_or_url(item.text)]


def mock_score(raw_id: int, family: str, coin_candidates: set[str]) -> list[dict[str, Any]]:
    """Generate deterministic-ish mock scores for dry runs."""
    scores: list[dict[str, Any]] = []
    if not coin_candidates:
        scores.append(
            {
                "raw_id": raw_id,
                "family": family,
                "scope": "market",
                "coin": "MARKET",
                "direction": round(random.uniform(-1, 1), 2),
                "confidence": round(random.uniform(0.5, 0.9), 2),
                "event_type": "commentary",
                "magnitude": random.choice([1, 2, 3]),
                "is_shill": 0,
                "model": "mock",
                "batch_id": "dry-run",
                "scored_at": now_iso(),
            }
        )
    else:
        for coin in coin_candidates:
            scores.append(
                {
                    "raw_id": raw_id,
                    "family": family,
                    "scope": "coin",
                    "coin": coin,
                    "direction": round(random.uniform(-1, 1), 2),
                    "confidence": round(random.uniform(0.5, 0.9), 2),
                    "event_type": "commentary",
                    "magnitude": random.choice([1, 2, 3]),
                    "is_shill": 0,
                    "model": "mock",
                    "batch_id": "dry-run",
                    "scored_at": now_iso(),
                }
            )
    return scores


def insert_scores(conn: sqlite3.Connection, scores: list[dict[str, Any]]) -> int:
    inserted = 0
    for score in scores:
        try:
            conn.execute(
                """
                INSERT INTO scores
                (raw_id, family, scope, coin, direction, confidence, event_type,
                 magnitude, is_shill, model, batch_id, scored_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    score["raw_id"],
                    score["family"],
                    score["scope"],
                    score["coin"],
                    score["direction"],
                    score["confidence"],
                    score["event_type"],
                    score["magnitude"],
                    score["is_shill"],
                    score["model"],
                    score["batch_id"],
                    score["scored_at"],
                ),
            )
            inserted += 1
        except sqlite3.IntegrityError:
            continue
    conn.commit()
    return inserted


def _normalize_and_filter(
    coin_symbols: list[str], coin_manager: CoinManager
) -> list[str]:
    return [normalize_symbol(s) for s in coin_symbols if normalize_symbol(s) in coin_manager.coins]


async def score_with_llm(
    conn: sqlite3.Connection,
    coin_manager: CoinManager,
    dry_run: bool = False,
) -> dict[str, int]:
    """Score all unscored raw items. Returns counters."""
    batch_id = uuid.uuid4().hex[:12]
    model = "mock" if dry_run else LLMClient().model

    # Pull all unscored rows.
    cursor = conn.execute(
        "SELECT id, family, title, text FROM raw_items "
        "WHERE NOT EXISTS (SELECT 1 FROM scores s WHERE s.raw_id = raw_items.id)"
    )
    rows = cursor.fetchall()
    news_rows = [(rid, title, text) for rid, family, title, text in rows if family == "news"]
    social_rows = [(rid, text) for rid, family, title, text in rows if family == "social"]
    print(f"[pipeline] Unscored: {len(news_rows)} news, {len(social_rows)} social")

    scores: list[dict[str, Any]] = []

    if dry_run:
        for raw_id, _, text in news_rows:
            candidates = coin_manager.attribution_candidates(text)
            scores.extend(mock_score(raw_id, "news", candidates))
        for raw_id, text in social_rows:
            candidates = coin_manager.attribution_candidates(text)
            scores.extend(mock_score(raw_id, "social", candidates))
        inserted = insert_scores(conn, scores)
        return {"news_scored": len(news_rows), "social_scored": len(social_rows), "inserted": inserted}

    client = LLMClient()
    if not client.available():
        print("[pipeline] LLM_API_KEY not set; falling back to mock scores.")
        for raw_id, _, text in news_rows:
            candidates = coin_manager.attribution_candidates(text)
            scores.extend(mock_score(raw_id, "news", candidates))
        for raw_id, text in social_rows:
            candidates = coin_manager.attribution_candidates(text)
            scores.extend(mock_score(raw_id, "social", candidates))
        inserted = insert_scores(conn, scores)
        return {"news_scored": len(news_rows), "social_scored": len(social_rows), "inserted": inserted}

    # Real LLM path — concurrent scoring bounded by a semaphore.
    # Serial scoring costs ~5s per call; with 10 in flight a full hourly
    # pass drops from ~15 min to ~2 min at identical token cost.
    sem = asyncio.Semaphore(LLM_CONCURRENCY)

    def budget_exhausted() -> bool:
        return client.call_count >= client.max_calls

    async def call_news(row: tuple[int, str, str]) -> tuple[int, dict[str, Any] | None]:
        raw_id, title, text = row
        candidates = list(coin_manager.attribution_candidates(text))
        async with sem:
            if budget_exhausted():
                return raw_id, None
            result = await client.score_news(title, text, candidates)
        return raw_id, result

    news_results = await asyncio.gather(*(call_news(r) for r in news_rows))

    news_scored = 0
    for raw_id, result in news_results:
        if result is None:
            continue
        scope = result.get("scope", "coin")
        event_type = result.get("event_type", "other")
        magnitude = result.get("magnitude", 1)
        if scope == "market":
            # The A1 prompt carries market direction only via the coins array
            # (scope=market may still list affected coins). Derive the
            # market-level value as the confidence-weighted mean of those
            # coin directions; empty coins => neutral, zero weight.
            m_coins = result.get("coins", [])
            m_num = sum(
                (float(c.get("direction", 0)) / 2.0) * float(c.get("confidence", 0))
                for c in m_coins
            )
            m_den = sum(float(c.get("confidence", 0)) for c in m_coins)
            m_direction = m_num / m_den if m_den > 0 else 0.0
            m_confidence = max((float(c.get("confidence", 0)) for c in m_coins), default=0.0)
            scores.append(
                {
                    "raw_id": raw_id,
                    "family": "news",
                    "scope": "market",
                    "coin": "MARKET",
                    "direction": m_direction,
                    "confidence": m_confidence,
                    "event_type": event_type,
                    "magnitude": magnitude,
                    "is_shill": 0,
                    "model": client.model,
                    "batch_id": batch_id,
                    "scored_at": scored_at_now(),
                }
            )
        for coin_obj in result.get("coins", []):
            sym = normalize_symbol(coin_obj.get("symbol", ""))
            if sym not in coin_manager.coins:
                continue
            scores.append(
                {
                    "raw_id": raw_id,
                    "family": "news",
                    "scope": "coin",
                    "coin": sym,
                    "direction": float(coin_obj.get("direction", 0)) / 2.0,
                    "confidence": float(coin_obj.get("confidence", 0)),
                    "event_type": event_type,
                    "magnitude": magnitude,
                    "is_shill": 0,
                    "model": client.model,
                    "batch_id": batch_id,
                    "scored_at": scored_at_now(),
                }
            )
        news_scored += 1

    # Batch social (also concurrent across batches).
    social_batches = [
        social_rows[i : i + SOCIAL_BATCH_SIZE]
        for i in range(0, len(social_rows), SOCIAL_BATCH_SIZE)
    ]

    async def call_social(
        batch: list[tuple[int, str]],
    ) -> tuple[list[tuple[int, str]], list[dict[str, Any]] | None]:
        posts = [{"id": i, "text": text} for i, (raw_id, text) in enumerate(batch)]
        all_text = " ".join(text for _, text in batch)
        candidates = list(coin_manager.attribution_candidates(all_text))
        async with sem:
            if budget_exhausted():
                return batch, None
            results = await client.score_social(posts, candidates)
        return batch, results

    social_results = await asyncio.gather(*(call_social(b) for b in social_batches))

    social_scored = 0
    for batch, results in social_results:
        if results is None:
            continue
        id_map = {i: raw_id for i, (raw_id, _) in enumerate(batch)}
        for entry in results:
            raw_id = id_map.get(entry["id"])
            if raw_id is None:
                continue
            is_shill = 1 if entry.get("is_shill") else 0
            for coin_obj in entry.get("coins", []):
                sym = normalize_symbol(coin_obj.get("symbol", ""))
                if sym not in coin_manager.coins:
                    continue
                scores.append(
                    {
                        "raw_id": raw_id,
                        "family": "social",
                        "scope": "coin",
                        "coin": sym,
                        "direction": float(coin_obj.get("direction", 0)) / 2.0,
                        "confidence": float(coin_obj.get("confidence", 0)),
                        "event_type": "commentary",
                        "magnitude": 1,
                        "is_shill": is_shill,
                        "model": client.model,
                        "batch_id": batch_id,
                        "scored_at": scored_at_now(),
                    }
                )
        social_scored += len(batch)

    print(f"[pipeline] LLM calls used: {client.call_count}/{client.max_calls}")
    if budget_exhausted():
        print("[pipeline] Budget reached; unscored items stay queued for the next hourly run.")

    inserted = insert_scores(conn, scores)
    return {"news_scored": news_scored, "social_scored": social_scored, "inserted": inserted}


async def run_pipeline(dry_run: bool = False, source_names: list[str] | None = None) -> dict[str, Any]:
    print("[pipeline] Initializing database...")
    conn = init_db()

    print("[pipeline] Loading coin list...")
    coin_manager = CoinManager()
    coin_manager.load()
    print(f"[pipeline] Loaded {len(coin_manager.coins)} coins")

    source_classes = ALL_SOURCES
    if source_names:
        source_classes = [s for s in ALL_SOURCES if s.name in source_names]

    print(f"[pipeline] Fetching from {len(source_classes)} sources...")
    tasks = [s().fetch() for s in source_classes]
    source_results = await asyncio.gather(*tasks, return_exceptions=True)

    all_items: list[RawItem] = []
    for source_cls, result in zip(source_classes, source_results):
        if isinstance(result, Exception):
            print(f"[pipeline] Source {source_cls.name} failed: {result}")
            continue
        print(f"[pipeline] Source {source_cls.name}: {len(result)} items")
        all_items.extend(result)

    print(f"[pipeline] Total items before prefilter: {len(all_items)}")
    all_items = prefilter_items(all_items)
    print(f"[pipeline] Total items after prefilter: {len(all_items)}")

    print("[pipeline] Inserting raw items (dedup via raw_hash)...")
    rows = [item.to_dict() for item in all_items]
    inserted_ids = insert_raw_items(conn, rows)
    print(f"[pipeline] Inserted {len(inserted_ids)} new raw items")

    score_stats = await score_with_llm(conn, coin_manager, dry_run=dry_run)
    print(f"[pipeline] Scoring stats: {score_stats}")

    print("[pipeline] Aggregating 15-minute slots (current + previous)...")
    agg_stats = aggregate_recent(conn, coin_manager, slots=2)
    print(f"[pipeline] Aggregation stats: {agg_stats}")

    print("[pipeline] Exporting JSON...")
    from datetime import datetime, timezone

    export_ts = datetime.now(timezone.utc)
    export_stats = export_json(conn, coin_manager, export_ts)
    print(f"[pipeline] Export stats: {export_stats}")

    csv_paths: dict[str, Path] = {}
    if export_ts.minute == 0:
        # Full-table CSV snapshots are large; once per hour is enough.
        print("[pipeline] Backing up CSV (top of hour)...")
        csv_paths = export_csv_backup(conn, export_ts)
        print(f"[pipeline] CSV backups: {list(csv_paths.keys())}")
    else:
        print("[pipeline] Skipping CSV backup (runs at the top of the hour only)")

    if export_ts.minute == 0:
        # Self-check re-scores a sample with the LLM (serial calls); hourly
        # cadence is enough and protects the per-run call budget.
        print("[pipeline] Running self-check (top of hour)...")
        quality_stats = await run_self_check(conn, coin_manager)
    else:
        print("[pipeline] Skipping self-check (runs at the top of the hour only)")
        quality_stats = {"sample_n": 0, "agreement_rate": None, "note": "hourly only"}
    print(f"[pipeline] Self-check: {quality_stats}")

    print("[pipeline] Checking alias refresh...")
    aliases = await refresh_aliases()
    print(f"[pipeline] Aliases refreshed/persisted: {len(aliases)} symbols")

    pruned = prune_raw_items(conn)
    print(f"[pipeline] Pruned {pruned} old raw_items")

    conn.close()
    return {
        "raw_total": len(all_items),
        "raw_inserted": len(inserted_ids),
        "score_stats": score_stats,
        "aggregation": agg_stats,
        "export": export_stats,
        "csv_paths": {k: str(v) for k, v in csv_paths.items()},
        "quality": quality_stats,
        "aliases": len(aliases),
        "dry_run": dry_run,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Crypto Sentiment Hourly Pipeline")
    parser.add_argument("--dry-run", action="store_true", help="Use mock LLM scoring")
    parser.add_argument("--sources", nargs="+", help="Limit to named sources")
    args = parser.parse_args()

    result = asyncio.run(run_pipeline(dry_run=args.dry_run, source_names=args.sources))
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()

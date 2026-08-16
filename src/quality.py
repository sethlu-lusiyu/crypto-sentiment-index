"""Automated quality self-check via model re-scoring."""
from __future__ import annotations

import random
import sqlite3
from datetime import datetime, timezone
from typing import Any

from src.coins import CoinManager
from src.config import LLM_MODEL, LLM_MODEL_2
from src.llm_client import LLMClient, normalize_symbol
from src.utils import now_iso


def sample_scored_records(conn: sqlite3.Connection, sample_rate: float = 0.02) -> list[dict[str, Any]]:
    """Randomly sample scored records for re-scoring."""
    cursor = conn.execute(
        """
        SELECT s.id, s.raw_id, r.family, r.title, r.text, s.direction, s.coin
        FROM scores s
        JOIN raw_items r ON r.id = s.raw_id
        WHERE s.scope = 'coin'
        ORDER BY RANDOM()
        LIMIT 1000
        """
    )
    rows = [
        {
            "score_id": row[0],
            "raw_id": row[1],
            "family": row[2],
            "title": row[3],
            "text": row[4],
            "direction": row[5],
            "coin": row[6],
        }
        for row in cursor.fetchall()
    ]
    k = max(1, int(len(rows) * sample_rate))
    return rows[:k]


def directions_agree(a: float, b: float) -> bool:
    """Agreement if both on same side of zero or either is zero."""
    if a == 0 or b == 0:
        return True
    return (a > 0 and b > 0) or (a < 0 and b < 0)


async def run_self_check(
    conn: sqlite3.Connection,
    coin_manager: CoinManager,
    sample_rate: float = 0.02,
) -> dict[str, Any]:
    """Re-score a sample and log agreement rate."""
    samples = sample_scored_records(conn, sample_rate)
    if not samples:
        return {"sample_n": 0, "agreement_rate": None}

    model_b = LLM_MODEL_2 if LLM_MODEL_2 != LLM_MODEL else LLM_MODEL
    client = LLMClient(model=model_b)
    if not client.available():
        return {"sample_n": 0, "agreement_rate": None, "note": "LLM not configured"}

    agreements = 0
    checked = 0
    for rec in samples:
        candidates = list(coin_manager.attribution_candidates(rec["text"]))
        if rec["family"] == "news":
            result = await client.score_news(rec["title"], rec["text"], candidates)
            if result is None:
                continue
            for coin_obj in result.get("coins", []):
                if normalize_symbol(coin_obj.get("symbol", "")) == rec["coin"]:
                    new_dir = float(coin_obj.get("direction", 0)) / 2.0
                    if directions_agree(new_dir, rec["direction"]):
                        agreements += 1
                    checked += 1
                    break
        else:
            result = await client.score_social(
                [{"id": 0, "text": rec["text"]}], candidates
            )
            if not result:
                continue
            entry = result[0]
            for coin_obj in entry.get("coins", []):
                if normalize_symbol(coin_obj.get("symbol", "")) == rec["coin"]:
                    new_dir = float(coin_obj.get("direction", 0)) / 2.0
                    if directions_agree(new_dir, rec["direction"]):
                        agreements += 1
                    checked += 1
                    break

    rate = agreements / checked if checked > 0 else None
    conn.execute(
        "INSERT INTO quality_log (ts, sample_n, agreement_rate, model_a, model_b) VALUES (?, ?, ?, ?, ?)",
        (now_iso(), checked, rate, LLM_MODEL, model_b),
    )
    conn.commit()
    return {"sample_n": checked, "agreement_rate": rate}

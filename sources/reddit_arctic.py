"""Reddit source via the Arctic Shift public mirror (no API key needed).

Endpoints verified 2026-08:
- Posts:    GET /api/posts/search?subreddit=cryptocurrency&limit=100&after={epoch}
- Comments: GET /api/comments/search?...same params

Incremental strategy: the high-water mark is max(raw_items.published_at) for
this source, persisted in SQLite (which itself survives across GitHub Actions
runs via the actions/cache of data/sentiment.db). On a cold start we only look
back 1 hour — no historical backfill by design. If the PRAW env vars are ever
configured they can replace this module behind the same SourceBase interface.
"""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from sources.base import RawItem, SourceBase
from src.config import DATABASE_PATH
from src.utils import clean_text

BASE_URL = "https://arctic-shift.photon-reddit.com/api"
SUBREDDITS = ["cryptocurrency", "cryptomarkets", "bitcoin", "ethereum"]
REDDIT_URL = "https://www.reddit.com"


def _iso_from_epoch(epoch: Any) -> str:
    try:
        return datetime.fromtimestamp(int(epoch), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return datetime.now(timezone.utc).isoformat()


def _epoch_from_iso(text: str) -> int:
    try:
        return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())
    except (ValueError, AttributeError):
        return int(time.time()) - 3600


class RedditArcticSource(SourceBase):
    name = "reddit_arctic"
    family = "social"

    def __init__(self, limit: int = 100, lookback_seconds: int = 3600) -> None:
        self.limit = limit
        self.lookback_seconds = lookback_seconds

    def _watermark(self) -> int:
        """Epoch seconds of the newest item we already stored, else now-1h."""
        default = int(time.time()) - self.lookback_seconds
        try:
            conn = sqlite3.connect(f"file:{DATABASE_PATH}?mode=ro", uri=True)
            row = conn.execute(
                "SELECT MAX(published_at) FROM raw_items WHERE source = ?",
                (self.name,),
            ).fetchone()
            conn.close()
            if row and row[0]:
                # Small overlap to avoid edge misses; dedup handles re-fetches.
                return max(_epoch_from_iso(row[0]) - 300, default - self.lookback_seconds)
        except sqlite3.Error:
            pass
        return default

    async def fetch(self) -> list[RawItem]:
        after = self._watermark()
        items: list[RawItem] = []
        headers = {"User-Agent": "crypto-sentiment-bot/0.1"}
        async with httpx.AsyncClient(timeout=60, headers=headers) as client:
            for subreddit in SUBREDDITS:
                for kind in ("posts", "comments"):
                    try:
                        batch = await self._fetch_kind(client, kind, subreddit, after)
                        items.extend(batch)
                    except Exception as exc:
                        print(f"[reddit_arctic] {kind}/{subreddit} failed: {exc}")
        return items

    async def _fetch_kind(
        self, client: httpx.AsyncClient, kind: str, subreddit: str, after: int
    ) -> list[RawItem]:
        resp = await client.get(
            f"{BASE_URL}/{kind}/search",
            params={
                "subreddit": subreddit,
                "limit": self.limit,
                "after": after,
                "sort": "asc",
            },
        )
        resp.raise_for_status()
        records = resp.json().get("data") or []
        items: list[RawItem] = []
        for rec in records:
            if kind == "posts":
                title = clean_text(rec.get("title", ""))
                body = clean_text(rec.get("selftext", ""))
                if body in ("[removed]", "[deleted]"):
                    body = ""
                if "removed by moderator" in title.lower():
                    continue
                text = f"{title}. {body}".strip()
            else:
                title = ""
                text = clean_text(rec.get("body", ""))
                if text in ("[removed]", "[deleted]"):
                    continue
            if len(text) < 20:
                continue
            permalink = rec.get("permalink", "")
            items.append(
                RawItem(
                    source=self.name,
                    family=self.family,
                    url=f"{REDDIT_URL}{permalink}" if permalink else "",
                    title=title or text[:80],
                    text=text[:2000],
                    author=rec.get("author", ""),
                    published_at=_iso_from_epoch(rec.get("created_utc")),
                    lang="en",
                )
            )
        return items

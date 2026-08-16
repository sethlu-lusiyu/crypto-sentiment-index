"""Bluesky search source."""
from __future__ import annotations

import asyncio
import urllib.parse
from typing import Any

import httpx

from sources.base import RawItem, SourceBase
from src.coins import CoinManager
from src.utils import clean_text, parse_datetime


class BlueskySource(SourceBase):
    name = "bluesky"
    family = "social"

    def __init__(self, max_queries: int = 8, limit: int = 25, sleep_seconds: float = 2.0) -> None:
        self.max_queries = max_queries
        self.limit = limit
        self.sleep_seconds = sleep_seconds

    async def fetch(self) -> list[RawItem]:
        manager = CoinManager()
        manager.load()
        coins = sorted(manager.coins.values(), key=lambda c: c.market_cap, reverse=True)
        items: list[RawItem] = []
        for coin in coins[: self.max_queries]:
            query = f"${coin.symbol_upper}"
            try:
                batch = await self._fetch_query(query)
                items.extend(batch)
            except Exception:
                pass
            if self.sleep_seconds:
                await asyncio.sleep(self.sleep_seconds)
        return items

    async def _fetch_query(self, query: str) -> list[RawItem]:
        encoded = urllib.parse.quote(query)
        url = (
            "https://api.bsky.app/xrpc/app.bsky.feed.searchPosts"
            f"?q={encoded}&limit={self.limit}"
        )
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url)
            resp.raise_for_status()
        data = resp.json()
        posts = data.get("posts", [])
        items: list[RawItem] = []
        for post in posts:
            author = post.get("author", {}).get("handle", "")
            record = post.get("record", {})
            text = clean_text(record.get("text", ""))
            if len(text) < 5:
                continue
            uri = post.get("uri", "")
            created = record.get("createdAt", "")
            items.append(
                RawItem(
                    source=self.name,
                    family=self.family,
                    url=uri,
                    title=text[:80],
                    text=text,
                    author=author,
                    published_at=parse_datetime(created),
                    lang="en",
                )
            )
        return items

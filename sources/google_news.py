"""Google News RSS source."""
from __future__ import annotations

import asyncio
import urllib.parse

import feedparser
import httpx

from sources.base import RawItem, SourceBase
from src.coins import CoinManager
from src.utils import clean_text, parse_datetime


class GoogleNewsSource(SourceBase):
    name = "google_news"
    family = "news"

    def __init__(self, max_queries: int = 10, sleep_seconds: float = 2.0) -> None:
        self.max_queries = max_queries
        self.sleep_seconds = sleep_seconds

    async def fetch(self) -> list[RawItem]:
        manager = CoinManager()
        manager.load()
        # Pick top N by market cap.
        coins = sorted(manager.coins.values(), key=lambda c: c.market_cap, reverse=True)
        queries: list[str] = []
        for coin in coins[: self.max_queries]:
            queries.append(f"{coin.name} crypto")
        items: list[RawItem] = []
        for query in queries:
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
            f"https://news.google.com/rss/search?q={encoded}"
            "&hl=en-US&gl=US&ceid=US:en"
        )
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
        parsed = feedparser.parse(resp.content)
        items: list[RawItem] = []
        for entry in parsed.entries[:10]:
            title = clean_text(entry.get("title", ""))
            text = clean_text(entry.get("summary", ""))
            if len(title) < 10:
                continue
            full_text = f"{title}. {text}".strip()
            if len(full_text) < 20:
                continue
            items.append(
                RawItem(
                    source=self.name,
                    family=self.family,
                    url=entry.get("link", ""),
                    title=title,
                    text=full_text,
                    author=clean_text(entry.get("source", {}).get("title", "")),
                    published_at=parse_datetime(entry.get("published")),
                    lang="en",
                )
            )
        return items

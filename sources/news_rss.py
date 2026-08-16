"""RSS news source."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import feedparser
import httpx
import yaml

from sources.base import RawItem, SourceBase
from src.config import FEEDS_PATH
from src.utils import clean_text, parse_datetime


class NewsRSSSource(SourceBase):
    name = "news_rss"
    family = "news"

    def __init__(self, max_items_per_feed: int = 30) -> None:
        self.max_items_per_feed = max_items_per_feed

    async def fetch(self) -> list[RawItem]:
        feeds = self._load_feeds()
        tasks = [self._fetch_one(url) for _, url in feeds]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        items: list[RawItem] = []
        for result in results:
            if isinstance(result, Exception):
                continue
            items.extend(result)
        return items

    def _load_feeds(self) -> list[tuple[str, str]]:
        data = yaml.safe_load(FEEDS_PATH.read_text(encoding="utf-8"))
        return [(f["name"], f["url"]) for f in data.get("feeds", [])]

    async def _fetch_one(self, url: str) -> list[RawItem]:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
        parsed = feedparser.parse(resp.content)
        items: list[RawItem] = []
        for entry in parsed.entries[: self.max_items_per_feed]:
            title = clean_text(entry.get("title", ""))
            summary = clean_text(entry.get("summary", entry.get("description", "")))
            link = entry.get("link", "")
            text = f"{title}. {summary}".strip()
            if len(text) < 20:
                continue
            items.append(
                RawItem(
                    source=self.name,
                    family=self.family,
                    url=link,
                    title=title,
                    text=text,
                    author=clean_text(entry.get("author", "")),
                    published_at=parse_datetime(entry.get("published")),
                    lang="en",
                )
            )
        return items

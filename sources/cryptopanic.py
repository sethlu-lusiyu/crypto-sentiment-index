"""CryptoPanic news source (free API).

Requires env CRYPTOPANIC_TOKEN; without a token the source skips itself and
returns an empty list so the pipeline is never blocked. Per spec only
title/summary text is used — vote data is ignored.
"""
from __future__ import annotations

import httpx

from sources.base import RawItem, SourceBase
from src.config import CRYPTOPANIC_TOKEN
from src.utils import clean_text, parse_datetime

API_URL = "https://cryptopanic.com/api/free/v1/posts/"


class CryptoPanicSource(SourceBase):
    name = "cryptopanic"
    family = "news"

    def __init__(self, max_pages: int = 2) -> None:
        self.max_pages = max_pages

    async def fetch(self) -> list[RawItem]:
        if not CRYPTOPANIC_TOKEN:
            print("[cryptopanic] CRYPTOPANIC_TOKEN not set; skipping source.")
            return []
        items: list[RawItem] = []
        url: str | None = API_URL
        params: dict[str, str] | None = {
            "auth_token": CRYPTOPANIC_TOKEN,
            "public": "true",
            "kind": "news",
        }
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            for _ in range(self.max_pages):
                if not url:
                    break
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()
                items.extend(self._parse_results(data.get("results") or []))
                # Pagination: follow `next` (already carries the token).
                url = data.get("next")
                params = None
        return items

    def _parse_results(self, results: list[dict]) -> list[RawItem]:
        items: list[RawItem] = []
        for post in results:
            title = clean_text(post.get("title", ""))
            if len(title) < 10:
                continue
            # Free tier has no body; metadata may carry a description.
            description = clean_text((post.get("metadata") or {}).get("description", ""))
            text = f"{title}. {description}".strip()
            items.append(
                RawItem(
                    source=self.name,
                    family=self.family,
                    url=post.get("url", ""),
                    title=title,
                    text=text,
                    author=clean_text((post.get("source") or {}).get("title", "")),
                    published_at=parse_datetime(post.get("published_at")),
                    lang="en",
                )
            )
        return items

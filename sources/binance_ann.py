"""Binance announcement source (catalog 48: new listings / product news).

Endpoints verified 2026-08:
- List:   GET  /bapi/composite/v1/public/cms/article/catalog/list/query
          ?catalogId=48&pageNo=1&pageSize=20  (POST returns 400 "illegal parameter")
- Detail: GET  /bapi/composite/v1/public/cms/article/detail/query?articleCode={code}
          The detail `body` is a JSON node tree; text lives in leaf `text` fields.

This is a strong event source (listings/delisting/product changes), hence the
1.5 source weight in src/config.py.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import httpx

from sources.base import RawItem, SourceBase
from src.utils import clean_text

LIST_URL = "https://www.binance.com/bapi/composite/v1/public/cms/article/catalog/list/query"
DETAIL_URL = "https://www.binance.com/bapi/composite/v1/public/cms/article/detail/query"
ARTICLE_URL = "https://www.binance.com/en/support/announcement/{code}"
CATALOG_ID = 48


def _extract_body_text(node: Any, parts: list[str]) -> None:
    """Recursively collect text leaves from Binance's article body node tree."""
    if isinstance(node, dict):
        text = node.get("text")
        if isinstance(text, str) and text.strip():
            parts.append(text.strip())
        for child in node.get("child") or []:
            _extract_body_text(child, parts)
    elif isinstance(node, list):
        for child in node:
            _extract_body_text(child, parts)


def _parse_publish_date(value: Any) -> str:
    """publishDate is epoch milliseconds on this API."""
    try:
        ms = int(value)
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return datetime.now(timezone.utc).isoformat()


class BinanceAnnSource(SourceBase):
    name = "binance_ann"
    family = "news"

    def __init__(self, page_size: int = 20, max_details: int = 10) -> None:
        self.page_size = page_size
        # Detail fetches are one request per article; cap them per run.
        self.max_details = max_details

    async def fetch(self) -> list[RawItem]:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; crypto-sentiment-bot/0.1)"}
        async with httpx.AsyncClient(timeout=30, headers=headers) as client:
            resp = await client.get(
                LIST_URL,
                params={"catalogId": CATALOG_ID, "pageNo": 1, "pageSize": self.page_size},
            )
            resp.raise_for_status()
            payload = resp.json()
            if payload.get("code") != "000000":
                raise RuntimeError(f"Binance list API error: {payload.get('message')}")
            articles = (payload.get("data") or {}).get("articles") or []

            items: list[RawItem] = []
            details_fetched = 0
            for article in articles:
                title = clean_text(article.get("title", ""))
                if not title:
                    continue
                body_text = ""
                if details_fetched < self.max_details:
                    body_text = await self._fetch_body(client, article.get("code", ""))
                    details_fetched += 1
                text = f"{title}. {body_text}".strip()
                if len(text) < 20:
                    continue
                items.append(
                    RawItem(
                        source=self.name,
                        family=self.family,
                        url=ARTICLE_URL.format(code=article.get("code", "")),
                        title=title,
                        text=text[:4000],
                        author="Binance",
                        published_at=_parse_publish_date(article.get("publishDate")),
                        lang="en",
                    )
                )
            return items

    async def _fetch_body(self, client: httpx.AsyncClient, code: str) -> str:
        if not code:
            return ""
        try:
            resp = await client.get(DETAIL_URL, params={"articleCode": code})
            resp.raise_for_status()
            data = resp.json().get("data") or {}
            raw_body = data.get("body") or ""
            parts: list[str] = []
            _extract_body_text(json.loads(raw_body), parts)
            return clean_text(" ".join(parts))
        except Exception:
            return ""

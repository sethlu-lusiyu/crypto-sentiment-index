"""4chan /biz/ catalog source.

Endpoint verified 2026-08: GET https://a.4cdn.org/biz/catalog.json returns a
list of pages, each with `threads` (fields: no, sub, com, time, replies).

Only threads whose OP text mentions a known coin alias are kept — /biz/ is
noisy, so alias filtering at collection time keeps the LLM budget focused.
"""
from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from typing import Any

import httpx

from sources.base import RawItem, SourceBase
from src.coins import CoinManager
from src.utils import clean_text

CATALOG_URL = "https://a.4cdn.org/biz/catalog.json"
THREAD_URL = "https://boards.4chan.org/biz/thread/{no}"
TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    text = html.unescape(text or "")
    text = TAG_RE.sub(" ", text)
    return clean_text(text)


def _iso_from_epoch(epoch: Any) -> str:
    try:
        return datetime.fromtimestamp(int(epoch), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return datetime.now(timezone.utc).isoformat()


class FourChanBizSource(SourceBase):
    name = "4chan_biz"
    family = "social"

    async def fetch(self) -> list[RawItem]:
        manager = CoinManager()
        manager.load()
        headers = {"User-Agent": "Mozilla/5.0 (compatible; crypto-sentiment-bot/0.1)"}
        async with httpx.AsyncClient(timeout=30, headers=headers) as client:
            resp = await client.get(CATALOG_URL)
            resp.raise_for_status()
            pages = resp.json()

        items: list[RawItem] = []
        for page in pages:
            for thread in page.get("threads", []):
                if thread.get("sticky"):
                    continue  # Board welcome/rules posts are pinned noise.
                subject = _strip_html(thread.get("sub", ""))
                comment = _strip_html(thread.get("com", ""))
                text = f"{subject}. {comment}".strip()
                if len(text) < 20:
                    continue
                # Only keep threads that mention at least one known coin.
                if not manager.attribution_candidates(text):
                    continue
                items.append(
                    RawItem(
                        source=self.name,
                        family=self.family,
                        url=THREAD_URL.format(no=thread.get("no", "")),
                        title=subject or text[:80],
                        text=text[:2000],
                        author="anonymous",
                        published_at=_iso_from_epoch(thread.get("time")),
                        lang="en",
                    )
                )
        return items

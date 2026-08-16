"""Base classes for all data sources."""
from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class RawItem:
    source: str
    family: str  # 'news' or 'social'
    url: str
    title: str
    text: str
    author: str = ""
    published_at: str = ""
    fetched_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    lang: str = "en"
    raw_hash: str = ""

    def __post_init__(self) -> None:
        if not self.raw_hash:
            payload = f"{self.url or ''}|{self.text or ''}".encode("utf-8")
            self.raw_hash = hashlib.sha256(payload).hexdigest()
        if not self.published_at:
            self.published_at = self.fetched_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "family": self.family,
            "url": self.url,
            "title": self.title,
            "text": self.text,
            "author": self.author,
            "published_at": self.published_at,
            "fetched_at": self.fetched_at,
            "lang": self.lang,
            "raw_hash": self.raw_hash,
        }


class SourceBase(ABC):
    """Abstract base for news/social sources."""

    name: str = ""
    family: str = ""  # 'news' or 'social'

    @abstractmethod
    async def fetch(self) -> list[RawItem]:
        """Fetch new items from the source."""
        raise NotImplementedError

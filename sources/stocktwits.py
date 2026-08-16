"""StockTwits symbol stream source."""
from __future__ import annotations

import asyncio
from typing import Any

import httpx

from sources.base import RawItem, SourceBase
from src.coins import CoinManager
from src.utils import clean_text, parse_datetime


class StocktwitsSource(SourceBase):
    name = "stocktwits"
    family = "social"

    def __init__(self, max_symbols: int = 10, sleep_seconds: float = 1.0) -> None:
        self.max_symbols = max_symbols
        self.sleep_seconds = sleep_seconds

    async def fetch(self) -> list[RawItem]:
        manager = CoinManager()
        manager.load()
        coins = sorted(manager.coins.values(), key=lambda c: c.market_cap, reverse=True)
        items: list[RawItem] = []
        for coin in coins[: self.max_symbols]:
            try:
                batch = await self._fetch_symbol(coin.symbol_upper)
                items.extend(batch)
            except Exception:
                pass
            if self.sleep_seconds:
                await asyncio.sleep(self.sleep_seconds)
        return items

    async def _fetch_symbol(self, symbol: str) -> list[RawItem]:
        url = f"https://api.stocktwits.com/api/2/streams/symbol/{symbol}.X.json"
        headers = {"User-Agent": "Mozilla/5.0 (compatible; crypto-sentiment-bot/0.1)"}
        async with httpx.AsyncClient(timeout=30, headers=headers) as client:
            resp = await client.get(url)
            if resp.status_code == 403:
                # Cloudflare challenges some egress IPs (datacenter/VPN). Per the
                # spec a single source must never break the pipeline: log and skip.
                print(f"[stocktwits] 403 Cloudflare challenge for {symbol}; skipping")
                return []
            resp.raise_for_status()
        data = resp.json()
        messages = data.get("messages", [])
        items: list[RawItem] = []
        for msg in messages:
            text = clean_text(msg.get("body", ""))
            if len(text) < 5:
                continue
            user = msg.get("user", {}).get("username", "")
            created = msg.get("created_at", "")
            items.append(
                RawItem(
                    source=self.name,
                    family=self.family,
                    url=f"https://stocktwits.com/symbol/{symbol}.X",
                    title=text[:80],
                    text=text,
                    author=user,
                    published_at=parse_datetime(created),
                    lang="en",
                )
            )
        return items

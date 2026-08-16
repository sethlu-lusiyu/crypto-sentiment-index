"""One-off smoke test: fetch a small sample from every source and print stats."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sources.news_rss import NewsRSSSource
from sources.google_news import GoogleNewsSource
from sources.bing_news import BingNewsSource
from sources.binance_ann import BinanceAnnSource
from sources.cryptopanic import CryptoPanicSource
from sources.bluesky import BlueskySource
from sources.stocktwits import StocktwitsSource
from sources.reddit_arctic import RedditArcticSource
from sources.chan_biz import FourChanBizSource


async def main():
    # Small limits to keep the smoke test fast; production limits live in the modules.
    sources = [
        NewsRSSSource(max_items_per_feed=5),
        GoogleNewsSource(max_queries=2, sleep_seconds=1),
        BingNewsSource(max_queries=2, sleep_seconds=1),
        BinanceAnnSource(page_size=5, max_details=2),
        CryptoPanicSource(max_pages=1),
        BlueskySource(max_queries=2, limit=10, sleep_seconds=1),
        StocktwitsSource(max_symbols=2, sleep_seconds=1),
        RedditArcticSource(limit=10),
        FourChanBizSource(),
    ]

    for src in sources:
        try:
            items = await src.fetch()
        except Exception as exc:
            print(f"[FAIL] {src.name}: {type(exc).__name__}: {exc}")
            continue
        print(f"[OK] {src.name}: {len(items)} items")
        for it in items[:2]:
            print(f"     - {it.published_at[:19]} | {it.title[:70]}")


if __name__ == "__main__":
    asyncio.run(main())

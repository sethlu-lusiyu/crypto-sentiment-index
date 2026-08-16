"""Acceptance test: market-scope classification with a real LLM.

Skipped unless LLM_API_KEY is configured. In CI the key comes from secrets,
so this runs on every GitHub Actions job; locally it skips by default.

Spec acceptance criterion: a Fed-rate-hike style news item must be classified
as scope=market (it moves the whole market, not one coin).
"""
from __future__ import annotations

import asyncio
import os

import pytest

from src.llm_client import LLMClient

pytestmark = pytest.mark.skipif(
    not os.environ.get("LLM_API_KEY"), reason="LLM_API_KEY not configured"
)

FED_NEWS_TITLE = "Federal Reserve raises interest rates by 25 basis points"
FED_NEWS_TEXT = (
    "The Federal Reserve raised its benchmark interest rate by 25 basis points "
    "on Wednesday, citing persistent inflation. Risk assets including "
    "cryptocurrencies fell broadly following the announcement, with analysts "
    "expecting tighter liquidity conditions across financial markets."
)


def test_fed_rate_hike_classified_as_market_scope() -> None:
    client = LLMClient()
    result = asyncio.run(
        client.score_news(FED_NEWS_TITLE, FED_NEWS_TEXT, ["BTC", "ETH"])
    )
    assert result is not None, "LLM returned no parseable result"
    assert result.get("scope") == "market", (
        f"Fed rate-hike news must be scope=market, got: {result}"
    )

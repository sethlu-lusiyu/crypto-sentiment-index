"""Runtime configuration loaded from environment variables."""
from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DOCS_DATA_DIR = PROJECT_ROOT / "docs" / "data"
CONFIG_DIR = PROJECT_ROOT / "config"


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


LLM_BASE_URL = _env("LLM_BASE_URL", "https://api.moonshot.cn/v1")
LLM_API_KEY = _env("LLM_API_KEY", "")
LLM_MODEL = _env("LLM_MODEL", "kimi-k2")
LLM_MODEL_2 = _env("LLM_MODEL_2", LLM_MODEL)
LLM_MAX_CALLS_PER_RUN = int(_env("LLM_MAX_CALLS_PER_RUN", "200"))

CRYPTOPANIC_TOKEN = _env("CRYPTOPANIC_TOKEN", "")
REDDIT_CLIENT_ID = _env("REDDIT_CLIENT_ID", "")
REDDIT_CLIENT_SECRET = _env("REDDIT_CLIENT_SECRET", "")
REDDIT_USER_AGENT = _env("REDDIT_USER_AGENT", "crypto-sentiment-bot/0.1")

DATABASE_PATH = DATA_DIR / "sentiment.db"
COINS_FALLBACK_PATH = CONFIG_DIR / "coins_fallback.json"
ALIASES_PATH = DATA_DIR / "aliases.json"
FEEDS_PATH = CONFIG_DIR / "feeds.yaml"
AMBIGUOUS_SYMBOLS_PATH = CONFIG_DIR / "ambiguous_symbols.json"

# Source weights used by the index aggregator.
SOURCE_WEIGHTS = {
    "news_rss": 1.0,
    "google_news": 1.0,
    "bing_news": 1.0,
    "cryptopanic": 1.0,
    "binance_ann": 1.5,
    "bluesky": 0.8,
    "stocktwits": 0.8,
    "reddit_arctic": 0.8,
    "4chan_biz": 0.8,
}

# Decay half-life in hours.
NEWS_HALF_LIFE_HOURS = 24.0
SOCIAL_HALF_LIFE_HOURS = 6.0

# Minimum text length for processing.
MIN_TEXT_LENGTH = 20

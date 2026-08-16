# Crypto Sentiment Index

A free, text-only cryptocurrency sentiment index system. It collects crypto-related news and social posts, scores them with an LLM, aggregates per-coin sentiment indices, and serves a static dashboard via GitHub Pages.

## Data Flow

```
Sources (RSS, Google News, Bing News, Binance Announcements, CryptoPanic,
         Bluesky, StockTwits, Reddit via Arctic Shift, 4chan /biz/)
  ↓
Async Fetch + Deduplication (raw_hash sha256)
  ↓
Attribution (alias matching with ambiguous-symbol rules)
  ↓
LLM Scoring (news per-item, social batch ≤15)
  ↓
SQLite (scores)
  ↓
Hourly Aggregation (index_hourly, overall_hourly)
  ↓
JSON Export (docs/data/)
  ↓
GitHub Pages Dashboard
```

## Quick Start

1. **Fork** this repository.
2. Add these **Secrets** in Settings → Secrets and variables → Actions:
   - `LLM_API_KEY` — your OpenAI-compatible API key (Kimi, DeepSeek, etc.)
   - `LLM_BASE_URL` — e.g. `https://api.moonshot.cn/v1`
   - `LLM_MODEL` — e.g. `kimi-k2`
3. Enable **GitHub Pages** with source = `/docs` (branch `main`).
4. (Optional) Add `CRYPTOPANIC_TOKEN`, `REDDIT_CLIENT_ID/SECRET` for extra sources.
5. The workflow runs every hour at minute 7.

Run locally with mock scoring:

```bash
python -m pipeline.hourly_job --dry-run
```

Run locally with real LLM (set env vars first):

```bash
python -m pipeline.hourly_job
```

## Project Structure

```
.
├── config/                 # YAML/JSON config
│   ├── feeds.yaml          # RSS feed list
│   ├── coins_fallback.json # Top50 fallback if CoinGecko fails
│   └── ambiguous_symbols.json # Symbols requiring crypto context
├── src/
│   ├── config.py           # Environment defaults
│   ├── coins.py            # Coin list + alias attribution
│   ├── database.py         # SQLite schema
│   ├── llm_client.py       # OpenAI-compatible scorer
│   ├── prompts.py          # LLM prompts (verbatim from spec)
│   ├── index.py            # Aggregation + export
│   └── utils.py            # Helpers
├── sources/                # Data source modules
│   ├── base.py
│   ├── news_rss.py
│   ├── google_news.py
│   ├── bing_news.py
│   ├── binance_ann.py      # Binance announcements (weight 1.5)
│   ├── cryptopanic.py      # skips itself without CRYPTOPANIC_TOKEN
│   ├── bluesky.py
│   ├── stocktwits.py
│   ├── reddit_arctic.py    # incremental via SQLite high-water mark
│   └── chan_biz.py         # 4chan /biz/ (module renamed: py modules can't start with a digit)
├── pipeline/
│   └── hourly_job.py       # Hourly orchestrator
├── smoke_sources.py        # One-off live smoke test for all sources
├── docs/                   # GitHub Pages static dashboard
│   ├── index.html
│   ├── app.js
│   ├── style.css
│   └── data/               # Generated JSON
├── data/
│   ├── sentiment.db        # SQLite (not committed)
│   └── index_export/       # CSV backups (committed)
└── tests/                  # Unit tests
```

## Data Sources

All 9 planned sources are implemented. Each runs in isolation — a single
source failure never interrupts the pipeline.

| Source | Family | Status | Notes |
|--------|--------|--------|-------|
| RSS feeds (Cointelegraph, Decrypt, BeInCrypto, CryptoSlate) | news | ✅ live | feedparser |
| Google News RSS | news | ✅ live | rotates queries per coin, ≥2s apart |
| Bing News RSS | news | ✅ live | same rotation pattern |
| Binance Announcements | news | ✅ live | catalog list + article detail body; weight 1.5 |
| CryptoPanic | news | ✅ implemented | requires `CRYPTOPANIC_TOKEN`; skips itself without one |
| Bluesky | social | ✅ live | unauthenticated search, ≥2s per query |
| StockTwits | social | ⚠️ implemented | Cloudflare challenges some egress IPs; logs and skips on 403 |
| Reddit (Arctic Shift) | social | ✅ live | posts + comments, incremental via DB high-water mark, no backfill |
| 4chan /biz/ | social | ✅ live | catalog scan, alias-filtered, sticky threads skipped |

Verify collection locally at any time:

```bash
python smoke_sources.py
```

## Index Formulas

All indices are derived from LLM-scored text only. No price/volume/market data is used in scoring or index calculation.

### Per-coin sentiment (hourly)

```
SENT(coin, family, t) = Σ value·confidence·weight·decay / Σ confidence·weight·decay
```

- `value = direction / 2`, range [-1, 1]
- `weight`: news=1.0, Binance=1.5, social=0.8; shill ×0.2
- `decay = 0.5^((t - published_at)/half_life)`: news=24h, social=6h
- `z = (SENT - mean30d) / std30d`
- `confidence_flag=low` when effective records < 3

### Overall indices

```
MARKET_NEWS(t) = weighted average of all scope=market news scores
OVERALL_NEWS(t) = (market_cap_weighted Σ SENT(coin,news) + 0.5·MARKET_NEWS) / 1.5
OVERALL_SOCIAL(t) = market_cap_weighted Σ SENT(coin,social)
BREADTH(t) = bullish% - bearish%
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_BASE_URL` | `https://api.moonshot.cn/v1` | OpenAI-compatible endpoint |
| `LLM_MODEL` | `kimi-k2` | Primary model |
| `LLM_MODEL_2` | same as primary | Self-check model |
| `LLM_MAX_CALLS_PER_RUN` | 200 | Call budget per run |
| `LLM_API_KEY` | — | API key |

## Testing

```bash
pytest
```

Key test cases:
- Ambiguous symbols: `"I live near you"` does **not** attribute NEAR; `"$NEAR breaking out"` does.
- Common-word tickers: `"stocks, bonds, etc."` does **not** attribute ETC; 1-letter tickers (`M`, `U`…) are auto-treated as ambiguous.
- Purity: no price/close/funding strings participate in `scores` or `index` calculation.
- Market scope (acceptance): a Fed rate-hike news item must classify as `scope=market`. This test calls the real LLM and is **skipped unless `LLM_API_KEY` is set**; it runs in CI via secrets.

Attribution tests are offline-safe (CoinGecko is monkeypatched to the local fallback list).

## Disclaimer

This project is for informational and educational purposes only. Sentiment indices are derived from text and do not constitute investment advice. Always do your own research.

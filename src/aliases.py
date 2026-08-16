"""Weekly alias bootstrap via LLM."""
from __future__ import annotations

import json
import time
from typing import Any

from openai import AsyncOpenAI

from src.config import ALIASES_PATH, LLM_API_KEY, LLM_BASE_URL, LLM_MODEL
from src.coins import CoinManager


ALIAS_PROMPT = """System: 你是加密货币知识库。只输出 JSON。
User: 为以下每个币种生成常见别名（英文名、常见英文缩写、中文社区常用名），输出:
{"BTC": ["Bitcoin","比特币","大饼"], ...}
注意中文社区俗称（如 大饼=BTC、二饼/姨太=ETH、柚子=EOS 等）。
币种列表: {coin_list_placeholder}
"""

SECONDS_IN_7_DAYS = 7 * 24 * 3600


async def refresh_aliases(force: bool = False) -> dict[str, list[str]]:
    """Generate and persist aliases from LLM if older than 7 days. Falls back to existing file on failure."""
    if not force and ALIASES_PATH.exists():
        age = time.time() - ALIASES_PATH.stat().st_mtime
        if age < SECONDS_IN_7_DAYS:
            return _load_existing()

    manager = CoinManager()
    manager.load()
    coin_list = ", ".join(f"{c.symbol}({c.name})" for c in manager.coins.values())
    prompt = ALIAS_PROMPT.replace("{coin_list_placeholder}", coin_list[:4000])

    if not LLM_API_KEY:
        return _load_existing()

    client = AsyncOpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)
    try:
        resp = await client.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
        )
        content = resp.choices[0].message.content or "{}"
        content = content.strip()
        if content.startswith("```"):
            lines = content.splitlines()
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            content = "\n".join(lines).strip()
        parsed = json.loads(content)
        normalized: dict[str, list[str]] = {}
        for sym, aliases in parsed.items():
            if isinstance(aliases, list):
                normalized[sym.upper()] = [str(a) for a in aliases]
        ALIASES_PATH.parent.mkdir(parents=True, exist_ok=True)
        ALIASES_PATH.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
        return normalized
    except Exception as exc:
        print(f"[aliases] Failed to refresh aliases: {exc}")
        return _load_existing()


def _load_existing() -> dict[str, list[str]]:
    if ALIASES_PATH.exists():
        return json.loads(ALIASES_PATH.read_text(encoding="utf-8"))
    return {}

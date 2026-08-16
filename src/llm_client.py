"""OpenAI-compatible LLM client with validation and rate limiting."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

import jsonschema
from openai import AsyncOpenAI

from src.config import LLM_API_KEY, LLM_BASE_URL, LLM_MAX_CALLS_PER_RUN, LLM_MODEL
from src.prompts import build_news_prompt, build_social_prompt


NEWS_SCHEMA = {
    "type": "object",
    "properties": {
        "scope": {"enum": ["coin", "market"]},
        "coins": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "direction": {"type": "number", "enum": [-2, -1, 0, 1, 2]},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
                "required": ["symbol", "direction", "confidence"],
            },
        },
        "event_type": {"type": "string"},
        "magnitude": {"type": "integer", "enum": [1, 2, 3]},
        "time_sensitivity": {"enum": ["breaking", "recent", "dated"]},
        "summary_zh": {"type": "string"},
        "skip": {"type": "boolean"},
    },
}

SOCIAL_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "id": {"type": "integer"},
            "coins": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string"},
                        "direction": {"type": "number", "enum": [-2, -1, 0, 1, 2]},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    },
                    "required": ["symbol", "direction", "confidence"],
                },
            },
            "is_shill": {"type": "boolean"},
            "sarcasm": {"type": "boolean"},
        },
        "required": ["id", "coins", "is_shill", "sarcasm"],
    },
}


class LLMClient:
    """Thin wrapper around OpenAI-compatible chat completions."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        max_calls: int = LLM_MAX_CALLS_PER_RUN,
    ) -> None:
        self.api_key = api_key or LLM_API_KEY
        self.base_url = base_url or LLM_BASE_URL
        self.model = model or LLM_MODEL
        self.max_calls = max_calls
        self.call_count = 0
        self.client: AsyncOpenAI | None = None
        if self.api_key:
            self.client = AsyncOpenAI(api_key=self.api_key, base_url=self.base_url)

    def available(self) -> bool:
        return self.client is not None

    async def _call(self, system: str, user: str, temperature: float = 0.2) -> str:
        if not self.client:
            raise RuntimeError("LLM client not configured")
        if self.call_count >= self.max_calls:
            raise RuntimeError("LLM call budget exhausted")
        self.call_count += 1
        resp = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=temperature,
        )
        content = resp.choices[0].message.content or ""
        return content.strip()

    @staticmethod
    def _extract_json(text: str) -> str:
        text = text.strip()
        if text.startswith("```"):
            # Strip markdown fences.
            lines = text.splitlines()
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        return text

    async def score_news(
        self, title: str, text: str, coin_candidates: list[str]
    ) -> dict[str, Any] | None:
        user_prompt = build_news_prompt(title, text, coin_candidates)
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                raw = await self._call("你是加密市场新闻分析器。只输出合法 JSON，不要输出任何其他内容。", user_prompt)
                raw = self._extract_json(raw)
                parsed = json.loads(raw)
                if parsed.get("skip"):
                    return None
                jsonschema.validate(parsed, NEWS_SCHEMA)
                return parsed
            except Exception as exc:
                last_error = exc
                continue
        print(f"[llm] News scoring failed after 2 attempts: {last_error}")
        return None

    async def score_social(
        self, posts: list[dict[str, Any]], coin_candidates: list[str]
    ) -> list[dict[str, Any]]:
        if not posts:
            return []
        user_prompt = build_social_prompt(posts, coin_candidates)
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                raw = await self._call("你是加密社媒情绪分析器，精通币圈俚语(rekt/moon/ngmi/wagmi/FUD/shill/ape in)、反讽和emoji。只输出合法 JSON。", user_prompt)
                raw = self._extract_json(raw)
                parsed = json.loads(raw)
                if not isinstance(parsed, list):
                    parsed = [parsed]
                jsonschema.validate(parsed, SOCIAL_SCHEMA)
                # Ensure every input id has a matching output; fill missing with empty.
                by_id = {p["id"]: p for p in parsed if "id" in p}
                result = []
                for post in posts:
                    entry = by_id.get(post["id"], {"id": post["id"], "coins": [], "is_shill": False, "sarcasm": False})
                    result.append(entry)
                return result
            except Exception as exc:
                last_error = exc
                continue
        print(f"[llm] Social scoring failed after 2 attempts: {last_error}")
        return []


def normalize_symbol(symbol: str) -> str:
    return symbol.upper().strip()


def scored_at_now() -> str:
    return datetime.now(timezone.utc).isoformat()

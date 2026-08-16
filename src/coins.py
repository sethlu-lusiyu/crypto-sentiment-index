"""Coin list and alias management."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from src.config import (
    AMBIGUOUS_SYMBOLS_PATH,
    COINS_FALLBACK_PATH,
    ALIASES_PATH,
)


@dataclass(frozen=True)
class Coin:
    symbol: str
    name: str
    market_cap: float

    @property
    def symbol_upper(self) -> str:
        return self.symbol.upper()


class CoinManager:
    """Loads Top200 coins from CoinGecko and manages aliases."""

    def __init__(self) -> None:
        self.coins: dict[str, Coin] = {}
        self.aliases: dict[str, set[str]] = {}
        self.ambiguous: set[str] = set()
        self.crypto_context_words: list[str] = []
        self._load_ambiguous()

    def _load_ambiguous(self) -> None:
        data = json.loads(AMBIGUOUS_SYMBOLS_PATH.read_text(encoding="utf-8"))
        self.ambiguous = {s.upper() for s in data.get("ambiguous", [])}
        self.crypto_context_words = [w.lower() for w in data.get("crypto_context_words", [])]

    def load(self) -> None:
        """Load coins and aliases. Try CoinGecko first, fallback to local file."""
        coins = self._fetch_coingecko()
        if not coins:
            coins = self._load_fallback()
        self.coins = {c.symbol_upper: c for c in coins}
        # Symbols of 1-2 letters ("M", "BC", "U"...) collide with ordinary
        # words too often; treat them as ambiguous regardless of the config list.
        self.ambiguous |= {s for s in self.coins if len(s) <= 2}
        self.aliases = self._build_aliases(coins)

    def _fetch_coingecko(self) -> list[Coin]:
        url = (
            "https://api.coingecko.com/api/v3/coins/markets"
            "?vs_currency=usd&order=market_cap_desc&per_page=200&page=1"
        )
        try:
            with httpx.Client(timeout=20) as client:
                resp = client.get(url)
                resp.raise_for_status()
                data = resp.json()
            coins = []
            for item in data:
                mc = item.get("market_cap") or 0.0
                coins.append(Coin(symbol=item["symbol"], name=item["name"], market_cap=float(mc)))
            return coins
        except Exception as exc:  # pragma: no cover
            print(f"CoinGecko fetch failed: {exc}; using fallback")
            return []

    def _load_fallback(self) -> list[Coin]:
        data = json.loads(COINS_FALLBACK_PATH.read_text(encoding="utf-8"))
        return [
            Coin(symbol=c.get("symbol", ""), name=c.get("name", ""), market_cap=float(c.get("market_cap", 0)))
            for c in data.get("coins", [])
        ]

    def _build_aliases(self, coins: list[Coin]) -> dict[str, set[str]]:
        aliases: dict[str, set[str]] = {}
        if ALIASES_PATH.exists():
            try:
                stored = json.loads(ALIASES_PATH.read_text(encoding="utf-8"))
            except Exception:
                stored = {}
        else:
            stored = {}

        for coin in coins:
            sym = coin.symbol_upper
            alias_set = {sym, f"${sym}", coin.name, coin.name.lower()}
            alias_set.update(stored.get(sym, []))
            aliases[sym] = {a for a in alias_set if a}
        return aliases

    def attribution_candidates(self, text: str) -> set[str]:
        """Return set of coin symbols that appear in text.

        Handles ambiguous symbols with crypto-context co-occurrence rules.
        A $-prefixed alias match is always accepted, even for ambiguous symbols.
        NOTE: dollar and word matching are evaluated independently — breaking
        out of a single loop on the first word match would randomly skip the
        $-alias check depending on set iteration order (observed as flaky
        attribution of e.g. "$NEAR breaking out").
        """
        text_norm = f" {text.lower()} "
        result: set[str] = set()
        for sym, alias_set in self.aliases.items():
            dollar_matched = any(
                alias.startswith("$") and alias.lower() in text_norm
                for alias in alias_set
                if alias
            )
            word_matched = any(
                not alias.startswith("$")
                and re.search(r"\b" + re.escape(alias.lower()) + r"\b", text_norm)
                for alias in alias_set
                if alias
            )
            if not dollar_matched and not word_matched:
                continue
            if sym in self.ambiguous:
                # $-prefix is sufficient; otherwise require crypto context.
                cond = dollar_matched or self._has_crypto_context(text_norm)
                if cond:
                    result.add(sym)
            else:
                result.add(sym)
        return result

    def _has_crypto_context(self, text_lower: str) -> bool:
        return any(word in text_lower for word in self.crypto_context_words)

    def coin_for_symbol(self, symbol: str) -> Coin | None:
        return self.coins.get(symbol.upper())


def load_aliases_from_llm_raw(raw: dict[str, Any]) -> dict[str, list[str]]:
    """Normalize LLM alias response into a dict of symbol -> alias list."""
    out: dict[str, list[str]] = {}
    for k, v in raw.items():
        if isinstance(v, list):
            out[k.upper()] = [str(a) for a in v]
    return out


def compute_raw_hash(url: str, text: str) -> str:
    """Stable sha256 hash used for deduplication."""
    payload = f"{url or ''}|{text or ''}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()

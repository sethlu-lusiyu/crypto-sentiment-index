"""Tests for coin attribution logic, especially ambiguous symbols.

Offline-safe: CoinGecko is monkeypatched out so tests always use the local
fallback coin list (config/coins_fallback.json) or a synthetic list.
"""
from __future__ import annotations

import pytest

from src.coins import Coin, CoinManager


@pytest.fixture
def manager(monkeypatch: pytest.MonkeyPatch) -> CoinManager:
    monkeypatch.setattr(CoinManager, "_fetch_coingecko", lambda self: [])
    cm = CoinManager()
    cm.load()  # falls back to config/coins_fallback.json
    return cm


@pytest.fixture
def mini_manager(monkeypatch: pytest.MonkeyPatch) -> CoinManager:
    """Synthetic list including a 1-letter symbol to test the short-ticker rule."""
    coins = [
        Coin(symbol="M", name="MemeToken", market_cap=1e9),
        Coin(symbol="BTC", name="Bitcoin", market_cap=1e12),
    ]
    monkeypatch.setattr(CoinManager, "_fetch_coingecko", lambda self: coins)
    cm = CoinManager()
    cm.load()
    return cm


def test_near_ambiguous_no_context(manager: CoinManager) -> None:
    """"I live near you" must NOT attribute NEAR."""
    assert "NEAR" not in manager.attribution_candidates("I live near you")


def test_near_dollar_prefix(manager: CoinManager) -> None:
    """"$NEAR breaking out" must attribute NEAR."""
    assert "NEAR" in manager.attribution_candidates("$NEAR breaking out")


def test_btc_matches(manager: CoinManager) -> None:
    assert "BTC" in manager.attribution_candidates("Bitcoin is looking bullish today")
    assert "BTC" in manager.attribution_candidates("BTC dominance rising")


def test_sol_ambiguous_crypto_context(manager: CoinManager) -> None:
    """SOL is ambiguous but should match with crypto context."""
    assert "SOL" in manager.attribution_candidates("SOL price chart looks great")


def test_etc_common_word_no_context(manager: CoinManager) -> None:
    """'etc.' as an ordinary word must NOT attribute Ethereum Classic."""
    assert "ETC" not in manager.attribution_candidates(
        "We discussed stocks, bonds, etc. Nothing about digital assets here"
    )


def test_etc_dollar_prefix(manager: CoinManager) -> None:
    assert "ETC" in manager.attribution_candidates("$ETC is pumping today")


def test_single_letter_symbol_no_context(mini_manager: CoinManager) -> None:
    """1-letter tickers are auto-treated as ambiguous: "I'm" must not match M."""
    assert "M" not in mini_manager.attribution_candidates("I'm so happy today")


def test_single_letter_symbol_dollar(mini_manager: CoinManager) -> None:
    assert "M" in mini_manager.attribution_candidates("$M breaking out")

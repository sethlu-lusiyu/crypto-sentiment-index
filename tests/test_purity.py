"""Purity test: market data must not influence scores or index calculation."""
from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Files that participate in scoring or index calculation. Prompts and orchestration files
# may legitimately contain these words (e.g. LLM instructions or .close() methods).
SCORING_FILES = [
    "src/coins.py",
    "src/database.py",
    "src/index.py",
    "src/llm_client.py",
    "src/quality.py",
]


def test_no_market_data_in_scoring_or_index() -> None:
    """Ensure price/close/funding do not participate in scores or index logic."""
    forbidden = re.compile(r"\b(price|close|funding)\b", re.IGNORECASE)
    checked = []
    for rel in SCORING_FILES:
        path = PROJECT_ROOT / rel
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        if forbidden.search(text):
            checked.append(rel)
    assert not checked, f"Forbidden market terms found in scoring/index files: {checked}"

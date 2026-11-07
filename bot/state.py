"""Remembers the last numbered list per chat, so typing "2" picks coin number 2.

GitHub Actions starts every run fresh, so the workflow saves this small file with
actions/cache and restores it on the next run. Tapping a button doesn't need it.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from bot.config import ROOT


def _path() -> Path:
    return Path(os.getenv("BOT_STATE", str(ROOT / ".state" / "state.json")))


def load() -> dict:
    try:
        return json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save(state: dict) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state), encoding="utf-8")


def touch() -> None:
    """Make sure the file exists (the workflow's cache step needs it)."""
    save(load())


def remember_list(chat_id: str, market: str, symbols: list[str]) -> None:
    st = load()
    st.setdefault("lists", {})[str(chat_id)] = {"market": market, "symbols": symbols, "ts": int(time.time())}
    save(st)


def pick(chat_id: str, number: int) -> tuple[str, str] | None:
    """(market, symbol) for list item `number` (1-based), or None."""
    entry = load().get("lists", {}).get(str(chat_id))
    if not entry or not (1 <= number <= len(entry["symbols"])):
        return None
    return entry["market"], entry["symbols"][number - 1]

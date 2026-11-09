"""Remembers the last numbered list per chat, so typing "2" picks item number 2 of whatever list
was sent last (a scanner list or a news page), plus what the bot is waiting for after a menu
button such as "Any NSE stock".

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


WAIT_SECONDS = 15 * 60


def remember_list(chat_id: str, market: str, symbols: list[str], tf: str | None = None) -> None:
    st = load()
    st.setdefault("lists", {})[str(chat_id)] = {"market": market, "symbols": symbols, "tf": tf,
                                                "ts": int(time.time())}
    st.setdefault("last_kind", {})[str(chat_id)] = "scan"
    save(st)


def remember_news(chat_id: str, cat: str, items: list[dict], page: int = 1) -> None:
    st = load()
    st.setdefault("news", {})[str(chat_id)] = {"cat": cat, "items": items, "page": page, "ts": int(time.time())}
    st.setdefault("last_kind", {})[str(chat_id)] = "news"
    save(st)


def news(chat_id: str) -> dict | None:
    return load().get("news", {}).get(str(chat_id))


def set_news_page(chat_id: str, page: int) -> None:
    st = load()
    entry = st.get("news", {}).get(str(chat_id))
    if entry:
        entry["page"] = page
        st.setdefault("last_kind", {})[str(chat_id)] = "news"
        save(st)


def last_kind(chat_id: str) -> str:
    return load().get("last_kind", {}).get(str(chat_id), "scan")


def set_wait(chat_id: str, mode: str | None) -> None:
    """What the next plain text means: 'stock', 'fund', 'fut', 'fno' (or None to clear)."""
    st = load()
    waits = st.setdefault("wait", {})
    if mode:
        waits[str(chat_id)] = {"mode": mode, "ts": int(time.time())}
    else:
        waits.pop(str(chat_id), None)
    save(st)


def take_wait(chat_id: str) -> str | None:
    entry = load().get("wait", {}).get(str(chat_id))
    set_wait(chat_id, None)
    if entry and time.time() - entry.get("ts", 0) <= WAIT_SECONDS:
        return entry.get("mode")
    return None


def pick(chat_id: str, number: int) -> tuple[str, str, str | None] | None:
    """(market, symbol, timeframe) for list item `number` (1-based), or None."""
    entry = load().get("lists", {}).get(str(chat_id))
    if not entry or not (1 <= number <= len(entry["symbols"])):
        return None
    return entry["market"], entry["symbols"][number - 1], entry.get("tf")


def wait_mode(chat_id: str) -> str | None:
    """The pending menu question for this user (without using it up)."""
    entry = load().get("wait", {}).get(str(chat_id))
    if entry and time.time() - entry.get("ts", 0) <= WAIT_SECONDS:
        return entry.get("mode")
    return None


def pending_wait(chat_id: str) -> bool:
    """A menu question is waiting for this user's answer (doesn't use it up)."""
    entry = load().get("wait", {}).get(str(chat_id))
    return bool(entry and time.time() - entry.get("ts", 0) <= WAIT_SECONDS)


def recent_list(chat_id: str, seconds: int = WAIT_SECONDS) -> bool:
    """This user got a numbered list (scanner or news) in the last 15 minutes."""
    st = load()
    ts = max(st.get("lists", {}).get(str(chat_id), {}).get("ts", 0),
             st.get("news", {}).get(str(chat_id), {}).get("ts", 0))
    return time.time() - ts <= seconds


# ---------------------------------------------------------------- paper-trade ticket being filled in

def set_draft(chat_id: str, draft: dict | None) -> None:
    st = load()
    drafts = st.setdefault("drafts", {})
    if draft is None:
        drafts.pop(str(chat_id), None)
    else:
        drafts[str(chat_id)] = {**draft, "ts": int(time.time())}
    save(st)


def draft(chat_id: str) -> dict | None:
    d = load().get("drafts", {}).get(str(chat_id))
    if d and time.time() - d.get("ts", 0) <= 30 * 60:
        return d
    return None

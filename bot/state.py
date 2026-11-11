"""The bot's short-term memory: the last numbered list per chat (so typing "2" picks item 2), what the
bot is waiting for after a menu button, and the paper-trade ticket that is being filled in.

Where it lives:
  Cloud Run (BOT_STORE=gs://...)  inside the same Cloud Storage document as the paper trades: it survives
                                  restarts, is the same for every server, and an open ticket waits a day.
  PC / VM / GitHub Actions        a small file (BOT_STATE, default .state/state.json); GitHub's workflow
                                  saves it with actions/cache between runs.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import time
from pathlib import Path

from bot import store
from bot.config import ROOT

log = logging.getLogger(__name__)

WAIT_SECONDS = 15 * 60          # a menu question ("which stock?") waits this long for its answer
DAY = 24 * 3600                 # an open paper ticket (and its typed values) is kept this long
SECTIONS = ("lists", "last_kind", "wait", "drafts")     # saved in the shared document
# News pages (up to 50 stories with summaries per member) are big and quick to fetch again, so in shared
# mode they stay in this server's memory only.
_news: dict = {}

# Shared mode: during one update the memory is held here and written back once at the end (flush) -
# one request, however many things changed.
_live: dict | None = None       # the memory as this update sees it
_base: dict = {}                # the memory as it was when this update started (to write only what changed)
_dirty = False


def shared() -> bool:
    return store.is_gcs()


def _path() -> Path:
    return Path(os.getenv("BOT_STATE", str(ROOT / ".state" / "state.json")))


def load() -> dict:
    global _live, _base
    if shared():
        if _live is None:
            try:
                _live = dict(store.load().get("state") or {})
            except store.StoreError as exc:
                log.warning("Memory not loaded (%s); starting empty", exc)
                _live = {}
            _base = copy.deepcopy(_live)
            _live["news"] = _news
        return _live
    try:
        return json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save(state: dict) -> None:
    global _live, _dirty
    if shared():
        _live, _dirty = state, True
        return
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state), encoding="utf-8")


def touch() -> None:
    """Make sure the file exists (the workflow's cache step needs it)."""
    if not shared():
        save(load())


def _prune(st: dict) -> None:
    """Forget what is older than a day, so the memory stays small."""
    now = time.time()
    for section in ("lists", "news", "wait", "drafts"):
        box = st.get(section) or {}
        for k in [k for k, v in box.items() if now - (v or {}).get("ts", 0) > DAY]:
            del box[k]
    alive = set(st.get("lists") or {}) | set(st.get("news") or {})
    for k in [k for k in (st.get("last_kind") or {}) if k not in alive]:
        del st["last_kind"][k]


def begin(update_id=None) -> bool:
    """Start of one Telegram update on a server. Marks the update as taken - in the shared document, so a
    copy of the same update can never be handled twice, not even by another server - and loads the
    freshest memory. Returns False if this update was already taken."""
    global _live, _base, _dirty
    if not shared():
        return True

    def fn(doc):
        if update_id is not None:
            seen = doc.setdefault("seen", [])
            if update_id in seen:
                raise store.Skip(None)
            seen.append(update_id)
            del seen[:-300]
        return copy.deepcopy(doc.get("state") or {})

    try:
        got = store.update(fn)
    except store.StoreError as exc:      # storage down: keep answering from this server's own memory
        log.warning("Shared memory unavailable (%s); using this server's copy", exc)
        if _live is None:
            _live, _base = {"news": _news}, {}
        return True
    if got is None:
        return False
    got.pop("news", None)
    _live, _base, _dirty = got, copy.deepcopy(got), False
    _live["news"] = _news
    return True


def flush() -> None:
    """End of the update: write what changed (and only that) back to the shared document."""
    global _base, _dirty
    if not shared() or not _dirty or _live is None:
        return
    _prune(_live)
    mine, before = _live, _base

    def fn(doc):
        st = doc.setdefault("state", {})
        changed = False
        for section in SECTIONS:
            new, old = mine.get(section) or {}, before.get(section) or {}
            for k in set(new) | set(old):
                if k not in new:
                    changed |= st.get(section, {}).pop(k, None) is not None
                elif new[k] != old.get(k):
                    st.setdefault(section, {})[k] = new[k]
                    changed = True
        if not changed:
            raise store.Skip(None)

    try:
        store.update(fn)
        _base, _dirty = copy.deepcopy({k: v for k, v in mine.items() if k != "news"}), False
    except store.StoreError as exc:
        log.warning("Memory not saved (%s); it stays in this server until the next message", exc)


def forget() -> None:
    """Drop this process's copy (tests)."""
    global _live, _base, _dirty
    _live, _base, _dirty = None, {}, False
    _news.clear()


def _ttl(mode: str | None) -> int:
    return DAY if mode == "pt" else WAIT_SECONDS


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
    if entry and time.time() - entry.get("ts", 0) <= _ttl(entry.get("mode")):
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
    if entry and time.time() - entry.get("ts", 0) <= _ttl(entry.get("mode")):
        return entry.get("mode")
    return None


def pending_wait(chat_id: str) -> bool:
    """A menu question is waiting for this user's answer (doesn't use it up)."""
    entry = load().get("wait", {}).get(str(chat_id))
    return bool(entry and time.time() - entry.get("ts", 0) <= _ttl(entry.get("mode")))


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
    if d and time.time() - d.get("ts", 0) <= DAY:       # an open ticket waits a day, until you confirm or cancel
        return d
    return None

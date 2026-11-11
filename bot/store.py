"""Small JSON document store that survives restarts: paper trades and approved group chats.

Where it lives (BOT_STORE):
  gs://BUCKET/path.json  Google Cloud Storage (Cloud Run: deploy_cloudrun.sh creates the bucket and sets this).
                         Uses Cloud Run's own service account through the metadata server - no key file.
  a file path            your PC / VM (default: .state/store.json next to the code)
Writes use the object's generation number, so two servers can never overwrite each other's changes.
The last copy read or written is kept in memory for a few seconds, so one screen (for example the paper
trades list) costs one Cloud Storage request instead of five.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import threading
import time
from pathlib import Path
from urllib.parse import quote

import requests

from bot.config import ROOT

log = logging.getLogger(__name__)
TOKEN_URL = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"
_lock = threading.Lock()
_tok: dict = {}
_HTTP = requests.Session()     # kept-open connection to Cloud Storage
_mem: dict = {}                # {"loc", "doc", "gen", "ts"}: the last copy seen
CACHE_SECONDS = 20             # reads newer than this come from memory; writes are always checked by generation


class StoreError(RuntimeError):
    pass


def location() -> str:
    return os.getenv("BOT_STORE", "").strip() or str(ROOT / ".state" / "store.json")


def is_gcs() -> bool:
    return location().startswith("gs://")


def describe() -> str:
    loc = location()
    return f"Google Cloud Storage ({loc})" if is_gcs() else f"local file ({loc})"


# ---------------------------------------------------------------- Google Cloud Storage (JSON API)

def _token() -> str:
    if _tok.get("exp", 0) > time.time() + 60:
        return _tok["value"]
    try:
        r = _HTTP.get(TOKEN_URL, headers={"Metadata-Flavor": "Google"}, timeout=5)
        body = r.json()
    except (requests.RequestException, ValueError) as exc:
        raise StoreError(f"No Google credentials here ({exc.__class__.__name__}); BOT_STORE=gs:// only works on "
                         "Google Cloud") from None
    _tok.update(value=body["access_token"], exp=time.time() + int(body.get("expires_in", 300)))
    return _tok["value"]


def _split() -> tuple[str, str]:
    rest = location()[5:]
    bucket, _, obj = rest.partition("/")
    return bucket, obj or "bot-store.json"


def _gcs_get() -> tuple[dict, int]:
    bucket, obj = _split()
    url = f"https://storage.googleapis.com/storage/v1/b/{bucket}/o/{quote(obj, safe='')}?alt=media"
    try:
        r = _HTTP.get(url, headers={"Authorization": f"Bearer {_token()}"}, timeout=15)
    except requests.RequestException as exc:
        raise StoreError(f"Cloud Storage read failed: {exc.__class__.__name__}") from None
    if r.status_code == 404:
        return {}, 0
    if r.status_code != 200:
        raise StoreError(f"Cloud Storage read: HTTP {r.status_code} {r.text[:120]}")
    try:
        return r.json(), int(r.headers.get("x-goog-generation", "0"))
    except ValueError:
        return {}, int(r.headers.get("x-goog-generation", "0"))


def _gcs_put(doc: dict, generation: int) -> int | None:
    """The new generation number, or None if someone else wrote first (then the caller re-reads and retries)."""
    bucket, obj = _split()
    url = (f"https://storage.googleapis.com/upload/storage/v1/b/{bucket}/o?uploadType=media"
           f"&name={quote(obj, safe='')}&ifGenerationMatch={generation}")
    try:
        r = _HTTP.post(url, data=json.dumps(doc).encode(), timeout=15,
                          headers={"Authorization": f"Bearer {_token()}", "Content-Type": "application/json"})
    except requests.RequestException as exc:
        raise StoreError(f"Cloud Storage write failed: {exc.__class__.__name__}") from None
    if r.status_code == 412:
        return None
    if r.status_code not in (200, 201):
        raise StoreError(f"Cloud Storage write: HTTP {r.status_code} {r.text[:120]}")
    try:
        return int(r.json().get("generation", 0)) or -1
    except (ValueError, TypeError, AttributeError):
        return -1                      # written, but the new generation is unknown: re-read next time


def _remember(doc: dict, gen: int) -> None:
    if gen > 0:
        _mem.update(loc=location(), doc=copy.deepcopy(doc), gen=gen, ts=time.time())
    else:
        _mem.clear()


def _gcs_read(max_age: float) -> tuple[dict, int]:
    """The document and its generation: from memory if fresh enough, else from Cloud Storage."""
    if _mem.get("loc") == location() and time.time() - _mem.get("ts", 0) <= max_age:
        return copy.deepcopy(_mem["doc"]), _mem["gen"]
    doc, gen = _gcs_get()
    _remember(doc, gen)
    return doc, gen


def forget() -> None:
    """Drop the in-memory copy (tests, or after an error)."""
    _mem.clear()


# ---------------------------------------------------------------- local file

def _file() -> Path:
    return Path(location())


def _file_get() -> dict:
    try:
        return json.loads(_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _file_put(doc: dict) -> None:
    p = _file()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=1), encoding="utf-8")
    os.replace(tmp, p)


# ---------------------------------------------------------------- public

def load() -> dict:
    with _lock:
        return _gcs_read(CACHE_SECONDS)[0] if is_gcs() else _file_get()


def update(fn):
    """Read the document, let fn change it (and return a result), write it back; retried on conflicts."""
    with _lock:
        if not is_gcs():
            doc = _file_get()
            out = fn(doc)
            _file_put(doc)
            return out
        for attempt in range(5):
            # First try with the copy in memory (any age): the generation check refuses a stale one.
            doc, gen = _gcs_read(float("inf") if attempt == 0 else 0)
            out = fn(doc)
            new_gen = _gcs_put(doc, gen)
            if new_gen is not None:
                _remember(doc, new_gen)
                return out
            _mem.clear()
            time.sleep(0.3)
        raise StoreError("Cloud Storage is busy (5 write conflicts); try again")


# ---------------------------------------------------------------- approved groups (owner runs /allow in a group)

def approved_groups() -> set[str]:
    try:
        return set(load().get("groups", []))
    except StoreError as exc:
        log.warning("Store unavailable: %s", exc)
        return set()


def set_group(chat_id: str, allowed: bool) -> None:
    def fn(doc):
        g = set(doc.get("groups", []))
        (g.add if allowed else g.discard)(str(chat_id))
        doc["groups"] = sorted(g)
    update(fn)

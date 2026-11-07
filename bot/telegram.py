"""Minimal Telegram Bot API client (messages, photos, reading commands)."""
from __future__ import annotations

import logging

import requests

from bot.config import telegram_chat_id, telegram_token

log = logging.getLogger(__name__)
MAX_TEXT = 4000
MAX_CAPTION = 1000


class TelegramError(RuntimeError):
    pass


def _call(method: str, data: dict | None = None, files: dict | None = None) -> object:
    token = telegram_token()
    if not token:
        raise TelegramError("TELEGRAM_BOT_TOKEN is not set")
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/{method}", data=data, files=files, timeout=60)
        body = r.json()
    except (requests.RequestException, ValueError) as exc:
        raise TelegramError(f"{method}: {exc.__class__.__name__}") from None
    if not body.get("ok"):
        raise TelegramError(f"{method}: {body.get('description')}")
    return body["result"]


def split_text(text: str, limit: int = MAX_TEXT) -> list[str]:
    """Split on blank lines so HTML tags are never cut in half."""
    chunks, cur = [], ""
    for block in text.split("\n\n"):
        piece = block if not cur else cur + "\n\n" + block
        if len(piece) <= limit:
            cur = piece
            continue
        if cur:
            chunks.append(cur)
        while len(block) > limit:
            cut = block.rfind("\n", 0, limit)
            cut = cut if cut > 0 else limit
            chunks.append(block[:cut])
            block = block[cut:].lstrip("\n")
        cur = block
    if cur:
        chunks.append(cur)
    return chunks


def send_text(text: str, chat_id: str | None = None) -> None:
    for chunk in split_text(text):
        _call("sendMessage", {"chat_id": chat_id or telegram_chat_id(), "text": chunk,
                              "parse_mode": "HTML", "disable_web_page_preview": "true"})


def send_photo(png: bytes, caption: str = "", chat_id: str | None = None) -> None:
    _call("sendPhoto", {"chat_id": chat_id or telegram_chat_id(), "caption": caption[:MAX_CAPTION],
                        "parse_mode": "HTML"}, files={"photo": ("chart.png", png, "image/png")})


def pending_updates() -> list[dict]:
    return _call("getUpdates", {"timeout": 0, "allowed_updates": '["message"]'})


def confirm_updates(last_update_id: int) -> None:
    """Tell Telegram these updates are handled so they are not returned again."""
    _call("getUpdates", {"offset": last_update_id + 1, "timeout": 0})

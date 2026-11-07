"""Minimal Telegram Bot API client: messages, photos, tap buttons, command menu, reading updates."""
from __future__ import annotations

import json
import logging

import requests

from bot.config import telegram_chat_id, telegram_token

log = logging.getLogger(__name__)
MAX_TEXT = 4000
MAX_CAPTION = 1024

COMMANDS = [
    ("start", "Main menu"),
    ("crypto", "Scan coins: numbered list of BUY/SELL setups"),
    ("stocks", "Scan NSE stocks and indices"),
    ("fno", "F&O report, e.g. /fno NIFTY"),
    ("analyze", "Analyse one symbol, e.g. /analyze ETH"),
    ("strategies", "What the 7 strategies look for"),
    ("settings", "Capital, risk and watchlists"),
    ("help", "How to use the bot"),
]


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


def keyboard(rows: list[list[tuple[str, str]]] | None) -> str | None:
    """rows of (label, callback_data) -> Telegram inline keyboard JSON."""
    if not rows:
        return None
    return json.dumps({"inline_keyboard": [[{"text": t, "callback_data": d[:64]} for t, d in row] for row in rows]})


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


def send_text(text: str, chat_id: str | None = None, buttons=None) -> None:
    chunks = split_text(text)
    for n, chunk in enumerate(chunks):
        payload = {"chat_id": chat_id or telegram_chat_id(), "text": chunk,
                   "parse_mode": "HTML", "disable_web_page_preview": "true"}
        kb = keyboard(buttons) if n == len(chunks) - 1 else None
        if kb:
            payload["reply_markup"] = kb
        _call("sendMessage", payload)


def send_photo(png: bytes, caption: str = "", chat_id: str | None = None, buttons=None) -> None:
    payload = {"chat_id": chat_id or telegram_chat_id(), "caption": caption[:MAX_CAPTION], "parse_mode": "HTML"}
    kb = keyboard(buttons)
    if kb:
        payload["reply_markup"] = kb
    _call("sendPhoto", payload, files={"photo": ("chart.png", png, "image/png")})


def answer_callback(callback_id: str, text: str = "") -> None:
    """Stops the loading spinner on a tapped button. Old taps can't be answered; that's fine."""
    try:
        _call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:190]})
    except TelegramError as exc:
        log.info("Callback answer skipped: %s", exc)


def set_commands() -> None:
    """Shows the command menu when you type / in Telegram."""
    try:
        _call("setMyCommands", {"commands": json.dumps([{"command": c, "description": d} for c, d in COMMANDS])})
    except TelegramError as exc:
        log.warning("setMyCommands failed: %s", exc)


def pending_updates() -> list[dict]:
    return _call("getUpdates", {"timeout": 0, "allowed_updates": '["message","callback_query"]'})


def confirm_updates(last_update_id: int) -> None:
    """Tell Telegram these updates are handled so they are not returned again."""
    _call("getUpdates", {"offset": last_update_id + 1, "timeout": 0})

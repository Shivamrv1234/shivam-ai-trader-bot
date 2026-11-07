"""Minimal Telegram Bot API client: messages, photos, tap buttons, command menu, reading updates."""
from __future__ import annotations

import hashlib
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
    ("stocks", "Scan NSE watchlist and indices"),
    ("stock", "Analyse any NSE stock, e.g. /stock TATAMOTORS"),
    ("search", "Find an NSE symbol, e.g. /search tata"),
    ("fundamentals", "Company fundamentals, e.g. /fundamentals INFY"),
    ("fno", "Option chain + option idea, e.g. /fno NIFTY"),
    ("futures", "Futures report, e.g. /futures RELIANCE"),
    ("buildup", "Today's F&O OI build-up (long/short)"),
    ("news", "Market news & geopolitics, 10 per page"),
    ("analyze", "Analyse a coin or stock, e.g. /analyze ETH"),
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


def _button(text: str, data: str) -> dict:
    if data.startswith(("https://", "http://")):
        return {"text": text, "url": data}          # opens a web page
    return {"text": text, "callback_data": data.encode()[:64].decode("utf-8", "ignore")}


def keyboard(rows: list[list[tuple[str, str]]] | None) -> str | None:
    """rows of (label, callback_data or https:// link) -> Telegram inline keyboard JSON."""
    rows = [row for row in (rows or []) if row]
    if not rows:
        return None
    return json.dumps({"inline_keyboard": [[_button(t, d) for t, d in row] for row in rows]})


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


def edit_text(text: str, message_id: int, chat_id: str | None = None, buttons=None) -> None:
    """Replace a message in place (news pages flip like tabs). Falls back to a new message."""
    text = split_text(text)[0]
    payload = {"chat_id": chat_id or telegram_chat_id(), "message_id": message_id, "text": text,
               "parse_mode": "HTML", "disable_web_page_preview": "true"}
    kb = keyboard(buttons)
    if kb:
        payload["reply_markup"] = kb
    try:
        _call("editMessageText", payload)
    except TelegramError as exc:
        if "not modified" in str(exc).lower():
            return
        log.info("Edit failed (%s); sending a new message", exc)
        send_text(text, chat_id, buttons)


def show(text: str, chat_id: str | None = None, buttons=None, message_id: int | None = None) -> None:
    if message_id:
        edit_text(text, message_id, chat_id, buttons)
    else:
        send_text(text, chat_id, buttons)


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


def chat_action(chat_id: str | None = None, action: str = "typing") -> None:
    """Shows "typing…" in Telegram while the bot works."""
    try:
        _call("sendChatAction", {"chat_id": chat_id or telegram_chat_id(), "action": action})
    except TelegramError:
        pass


def webhook_secret() -> str:
    """Secret Telegram adds to every webhook call, derived from the bot token (nothing extra to store)."""
    return hashlib.sha256(telegram_token().encode()).hexdigest()[:32]


def set_webhook(url: str) -> None:
    _call("setWebhook", {"url": url, "secret_token": webhook_secret(),
                         "allowed_updates": '["message","callback_query"]', "max_connections": "1"})


def delete_webhook() -> None:
    _call("deleteWebhook", {"drop_pending_updates": "false"})


def webhook_info() -> dict:
    return _call("getWebhookInfo")

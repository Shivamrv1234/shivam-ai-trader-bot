"""Web server for Google Cloud Run: Telegram pushes each message here, so replies are instant.

Telegram calls POST /telegram with a secret header. Requests without the right secret
are refused, and only your own chat (TELEGRAM_CHAT_ID) is ever answered.
Alerts keep running on GitHub Actions; this server only answers messages and button taps.
"""
from __future__ import annotations

import hmac
import logging
import threading
from collections import deque

from flask import Flask, abort, request

from bot import main, telegram

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("server")

app = Flask(__name__)
_seen: deque = deque(maxlen=500)      # Telegram may resend an update; handle each only once
_seen_lock = threading.Lock()
_commands_set = False


@app.get("/")
def health():
    return "SHIVAM AI TRADER bot is running.", 200


@app.post("/telegram")
def telegram_webhook():
    global _commands_set
    sent = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not hmac.compare_digest(sent, telegram.webhook_secret()):
        abort(403)
    update = request.get_json(silent=True) or {}
    uid = update.get("update_id")
    with _seen_lock:
        if uid in _seen:
            return "duplicate", 200
        _seen.append(uid)
    if not _commands_set:
        telegram.set_commands()
        _commands_set = True
    try:
        main.process_update(update)
    except Exception:  # never return an error: Telegram would keep retrying the same update
        log.exception("Update %s failed", uid)
    return "ok", 200

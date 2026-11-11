"""Running on your own PC / a VM: .env file, long polling, scheduled alerts."""
import os
import time
from datetime import datetime, timezone

import pytest

from bot import config, main, telegram


def test_env_file(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_text("﻿# comment\nTELEGRAM_CHAT_ID=555\nGEMINI_MODEL=\"gemini-x\"\nexport ANGEL_PIN='1234'\n"
                 "ALREADY=from-file\nbad line\n", encoding="utf-8")
    for k in ("TELEGRAM_CHAT_ID", "GEMINI_MODEL", "ANGEL_PIN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ALREADY", "from-env")
    assert config.load_env_file(f) == 3
    assert os.environ["TELEGRAM_CHAT_ID"] == "555" and os.environ["GEMINI_MODEL"] == "gemini-x"
    assert os.environ["ANGEL_PIN"] == "1234" and os.environ["ALREADY"] == "from-env"
    assert config.load_env_file(tmp_path / "missing") == 0


def test_alert_schedule_matches_github_and_fires_once():
    fired = set()
    sat = datetime(2026, 11, 7, 4, 20, tzinfo=timezone.utc)        # Saturday 04:20 UTC
    assert main.due_alerts(sat, fired) == ["crypto"]                # crypto slot 04:07; no F&O on weekends
    assert main.due_alerts(sat, fired) == []                        # never twice
    mon = datetime(2026, 11, 9, 4, 16, tzinfo=timezone.utc)
    assert main.due_alerts(mon, fired) == ["crypto", "fno"]
    assert main.due_alerts(datetime(2026, 11, 9, 10, 41, tzinfo=timezone.utc), fired) == ["stocks"]
    assert main.due_alerts(datetime(2026, 11, 9, 11, 30, tzinfo=timezone.utc), set()) == []   # window passed
    wf = (config.ROOT / ".github/workflows/alerts.yml").read_text()
    for cron in ('"7 0,4,8,12,16,20 * * *"', '"40 10 * * 1-5"', '"15 4 * * 1-5"', '"0 9 * * 1-5"'):
        assert cron in wf


def test_local_mode_answers_messages(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:x")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "555")
    monkeypatch.setenv("BOT_STATE", str(tmp_path / "s.json"))
    sent, offsets = [], []
    monkeypatch.setattr(telegram, "webhook_info", lambda: {"url": ""})
    monkeypatch.setattr(telegram, "set_commands", lambda: None)
    monkeypatch.setattr(telegram, "send_text", lambda t, chat_id=None, buttons=None: sent.append(t))

    def poll(offset, timeout=50):
        offsets.append(offset)
        return [{"update_id": 41, "message": {"chat": {"id": 555}, "text": "/help"}}] if offset is None else []

    confirmed = []
    monkeypatch.setattr(telegram, "poll_updates", poll)
    monkeypatch.setattr(telegram, "confirm_updates", confirmed.append)
    main.run_local(with_alerts=False, max_loops=2)
    assert offsets == [None, 42] and "HELP" in sent[0]
    for _ in range(50):                      # confirmed at once (in the background), not only at the next poll
        if confirmed:
            break
        time.sleep(0.02)
    assert confirmed == [41]


def test_local_mode_refuses_while_webhook_active(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:x")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "555")
    monkeypatch.setattr(telegram, "webhook_info", lambda: {"url": "https://bot.run.app/telegram"})
    with pytest.raises(SystemExit, match="webhook delete"):
        main.run_local(max_loops=1)

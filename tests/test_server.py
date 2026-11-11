import pytest

pytest.importorskip("flask")  # the web server is optional; skip these tests if Flask is missing

from bot import main, telegram
from tests.test_bot import candles


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:ABC")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "555")
    monkeypatch.setenv("BOT_STATE", str(tmp_path / "s.json"))
    sent = []
    monkeypatch.setattr(telegram, "send_text", lambda t, chat_id=None, buttons=None: sent.append(("text", t)))
    monkeypatch.setattr(telegram, "send_photo",
                        lambda png, caption="", chat_id=None, buttons=None: sent.append(("photo", caption)))
    monkeypatch.setattr(telegram, "set_commands", lambda: None)
    monkeypatch.setattr(telegram, "answer_callback", lambda cid, text="": None)
    monkeypatch.setattr(main.ai, "summarize", lambda facts: None)
    from bot import server

    server._seen.clear()
    c = server.app.test_client()
    c.sent = sent
    return c


def headers(secret=None):
    return {"X-Telegram-Bot-Api-Secret-Token": secret or telegram.webhook_secret()}


def msg(uid, text, chat=555):
    return {"update_id": uid, "message": {"chat": {"id": chat}, "text": text}}


def test_health(client):
    assert client.get("/").status_code == 200


def test_rejects_wrong_secret(client):
    r = client.post("/telegram", json=msg(1, "/help"), headers=headers("wrong"))
    assert r.status_code == 403 and client.sent == []


def test_answers_owner_once(client):
    assert client.post("/telegram", json=msg(2, "/help"), headers=headers()).status_code == 200
    assert client.post("/telegram", json=msg(2, "/help"), headers=headers()).data == b"duplicate"
    assert len(client.sent) == 1 and "/crypto" in client.sent[0][1]


def test_ignores_strangers(client):
    client.post("/telegram", json=msg(3, "/help", chat=999), headers=headers())
    assert client.sent == []


def test_button_tap_gives_three_messages(client, monkeypatch):
    from bot import data

    monkeypatch.setattr(data, "crypto_candles", lambda sym, timeframe=None, limit=3000: candles())
    tap = {"update_id": 4, "callback_query": {"id": "q", "data": "a:c:BTCUSDT", "from": {"id": 555},
                                              "message": {"chat": {"id": 555}}}}
    assert client.post("/telegram", json=tap, headers=headers()).status_code == 200
    assert [k for k, _ in client.sent] == ["photo", "text"]


def test_errors_still_return_200(client, monkeypatch):
    def boom(u):
        raise RuntimeError("x")

    monkeypatch.setattr(main, "process_update", boom)
    assert client.post("/telegram", json=msg(5, "/help"), headers=headers()).status_code == 200


def test_github_run_skips_when_webhook_active(monkeypatch):
    def conflict():
        raise telegram.TelegramError("getUpdates: Conflict: can't use getUpdates method while webhook is active")

    monkeypatch.setattr(telegram, "set_commands", lambda: None)
    monkeypatch.setattr(telegram, "pending_updates", conflict)
    main.run_commands()  # must not raise


def test_resent_update_is_answered_at_once_while_the_first_is_still_running(client, monkeypatch):
    """Telegram resends an update when the answer is slow. The copy must get "duplicate" immediately
    (not wait, and not be handled a second time), and the next update waits its turn."""
    import threading

    from bot import server

    started, release, order = threading.Event(), threading.Event(), []

    def slow(update):
        order.append(("start", update["update_id"]))
        if update["update_id"] == 70:
            started.set()
            assert release.wait(5)
        order.append(("end", update["update_id"]))

    monkeypatch.setattr(main, "process_update", slow)
    results = {}

    def post(name, uid):
        results[name] = server.app.test_client().post("/telegram", json=msg(uid, "/help"), headers=headers()).data

    first = threading.Thread(target=post, args=("first", 70))
    first.start()
    assert started.wait(5)
    post("copy", 70)                                   # returns while the first is still working
    assert results["copy"] == b"duplicate" and "first" not in results
    nxt = threading.Thread(target=post, args=("next", 71))
    nxt.start()
    nxt.join(0.2)
    assert ("start", 71) not in order                  # one update at a time
    release.set()
    first.join(5)
    nxt.join(5)
    assert results == {"first": b"ok", "copy": b"duplicate", "next": b"ok"}
    assert order == [("start", 70), ("end", 70), ("start", 71), ("end", 71)]


def test_health_page_shows_the_version(client):
    from bot.version import VERSION

    assert f"Version {VERSION} ".encode() in client.get("/").data

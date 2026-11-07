import pytest

from bot import angel, data, news, symbols

REAL_CRYPTO_ORDERFLOW = data.crypto_orderflow


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """Tests never touch the network: live order flow, instrument list, Yahoo search and news feeds
    return nothing unless a test fakes them."""
    monkeypatch.setattr(data, "crypto_orderflow", lambda *a, **k: None)

    def no_list():
        raise angel.AngelError("offline")

    monkeypatch.setattr(angel, "instruments", no_list)
    monkeypatch.setattr(symbols, "yahoo_search", lambda q, limit=8: [])
    monkeypatch.setattr(news, "fetch_feed", lambda url, source, cat: [])
    monkeypatch.setattr(news, "article_description", lambda url: "")
    for k in ("GEMINI_API_KEY", "GEMINI_MODEL"):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture(autouse=True)
def no_chat_action(monkeypatch):
    from bot import telegram

    monkeypatch.setattr(telegram, "chat_action", lambda *a, **k: None)

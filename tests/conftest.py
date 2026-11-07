import pytest

from bot import data

REAL_CRYPTO_ORDERFLOW = data.crypto_orderflow


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """Tests never touch the network: live order flow returns nothing unless a test fakes it."""
    monkeypatch.setattr(data, "crypto_orderflow", lambda *a, **k: None)

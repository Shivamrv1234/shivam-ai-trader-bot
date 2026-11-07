"""Settings: editable values live in config.json; secrets come from environment
variables (GitHub Actions secrets)."""
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@lru_cache(maxsize=1)
def cfg() -> dict:
    path = Path(os.getenv("BOT_CONFIG", ROOT / "config.json"))
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def secret(name: str) -> str:
    return os.getenv(name, "").strip()


def telegram_token() -> str:
    return secret("TELEGRAM_BOT_TOKEN")


def telegram_chat_id() -> str:
    return secret("TELEGRAM_CHAT_ID")


def gemini_key() -> str:
    return secret("GEMINI_API_KEY")


def gemini_model() -> str:
    return secret("GEMINI_MODEL")


def cost_pct(market: str) -> float:
    c = cfg()["costs"]
    side = c["crypto_pct_per_side"] if market == "crypto" else c["stocks_pct_per_side"]
    return side + c["slippage_pct"]


def capital(market: str) -> tuple[float, str]:
    r = cfg()["risk"]
    return (r["capital_usdt"], "USDT") if market == "crypto" else (r["capital_inr"], "INR")

"""Settings: editable values live in config.json; secrets come from environment variables
(GitHub Actions secrets, Cloud Run settings) or, on your own PC / a VM, from a private .env file."""
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_env_file(path: Path | None = None) -> int:
    """Read KEY=value lines from .env into the environment (values already set win).
    Used only when running on your own machine; GitHub and Cloud Run have no .env file."""
    path = path or Path(os.getenv("BOT_ENV_FILE", ROOT / ".env"))
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return 0
    n = 0
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip().removeprefix("export ").strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and not os.environ.get(key):
            os.environ[key] = value
            n += 1
    return n


load_env_file()


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


def _fee_pct(market: str) -> float:
    c = cfg()["costs"]
    key = {"crypto": "crypto_pct_per_side", "fx": "forex_pct_per_side", "mcx": "mcx_pct_per_side"}.get(
        market, "stocks_pct_per_side")
    return float(c.get(key, c["stocks_pct_per_side"]))


def cost_pct(market: str) -> float:
    """Fee + slippage per side, in percent."""
    return _fee_pct(market) + cfg()["costs"]["slippage_pct"]


def cost_pct_side(market: str) -> tuple[float, float]:
    """(fee, slippage) per side as fractions, for the backtest."""
    return _fee_pct(market) / 100, cfg()["costs"]["slippage_pct"] / 100


def allowed_chats() -> set[str]:
    """Chats the bot answers: your own chat plus approved group IDs (ALLOWED_CHATS, comma separated)."""
    ids = {telegram_chat_id()} | {c.strip() for c in secret("ALLOWED_CHATS").split(",") if c.strip()}
    return {i for i in ids if i}


def allowed_users() -> set[str]:
    """Optional: only these Telegram user IDs may use the bot (comma separated). Empty = anyone in an
    allowed chat."""
    return {u.strip() for u in secret("ALLOWED_USERS").split(",") if u.strip()}

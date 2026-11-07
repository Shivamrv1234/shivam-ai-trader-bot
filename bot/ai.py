"""Optional plain-language summary from Google Gemini (free API key from Google AI Studio).

The model only rewrites facts the code already computed; it is told not to add
numbers or predictions. If the key/model is missing or the call fails, the
message is sent without a summary.
"""
from __future__ import annotations

import json
import logging

import requests

from bot.config import gemini_key, gemini_model

log = logging.getLogger(__name__)
LAST_ERROR = ""   # human-readable reason for the last failure (shown by the self-test)
URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

PROMPT = (
    "You explain trading-analysis results to a beginner in India, in simple English. "
    "Use ONLY the facts in the JSON below. Do not add any number, price or prediction that is "
    "not in it. In at most 110 words: say which strategies agree, what the market structure and nearest "
    "support/resistance or zones suggest, what the live order flow shows if present, what the main risk "
    "is, and how weak the backtest evidence is if trades are few. End with: 'Paper-trade first.'\n\nJSON:\n"
)


NEWS_PROMPT = (
    "You help a beginner trader in India understand one news story. Use ONLY the headline and summary "
    "in the JSON. In at most 70 words, in simple English: say which Indian sectors, stocks or crypto it "
    "could affect and why. Do not invent facts, numbers or price targets, and do not say whether to buy "
    "or sell. If the story is unclear, say so.\n\nJSON:\n"
)


def summarize(facts: dict) -> str | None:
    return generate(PROMPT + json.dumps(facts, default=str))


def news_take(title: str, summary: str, themes: list[str]) -> str | None:
    return generate(NEWS_PROMPT + json.dumps({"headline": title, "summary": summary, "themes_detected": themes}),
                    max_tokens=200)


def generate(prompt: str, max_tokens: int = 300) -> str | None:
    key, model = gemini_key(), gemini_model()
    if not (key and model):
        return None
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": max_tokens},
    }
    global LAST_ERROR
    LAST_ERROR = ""
    try:
        r = requests.post(URL.format(model=model), json=body, timeout=60,
                          headers={"x-goog-api-key": key, "Content-Type": "application/json"})
        data = r.json()
    except (requests.RequestException, ValueError) as exc:
        LAST_ERROR = f"network error ({exc.__class__.__name__})"
        log.warning("Gemini summary skipped: %s", LAST_ERROR)
        return None
    if r.status_code != 200 or "error" in data:
        err = data.get("error", {}) if isinstance(data, dict) else {}
        LAST_ERROR = f"HTTP {r.status_code}: {str(err.get('message', 'unknown error'))[:200]}"
        log.warning("Gemini summary skipped: %s", LAST_ERROR)
        return None
    try:
        parts = data["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts).strip()
    except (KeyError, IndexError, TypeError):
        LAST_ERROR = "reply had no text (blocked or empty)"
        return None
    return text[:900] or None

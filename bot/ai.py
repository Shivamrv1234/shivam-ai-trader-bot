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
URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

PROMPT = (
    "You explain trading-analysis results to a beginner in India, in simple English. "
    "Use ONLY the facts in the JSON below. Do not add any number, price or prediction that is "
    "not in it. In at most 110 words: say which strategies agree, what the market structure and nearest "
    "support/resistance or zones suggest, what the live order flow shows if present, what the main risk "
    "is, and how weak the backtest evidence is if trades are few. End with: 'Paper-trade first.'\n\nJSON:\n"
)


def summarize(facts: dict) -> str | None:
    key, model = gemini_key(), gemini_model()
    if not (key and model):
        return None
    body = {
        "contents": [{"role": "user", "parts": [{"text": PROMPT + json.dumps(facts, default=str)}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 300},
    }
    try:
        r = requests.post(URL.format(model=model), json=body, timeout=60,
                          headers={"x-goog-api-key": key, "Content-Type": "application/json"})
        data = r.json()
        parts = data["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts).strip()
    except (requests.RequestException, ValueError, KeyError, IndexError) as exc:
        log.warning("Gemini summary skipped: %s", exc.__class__.__name__)
        return None
    return text[:900] or None

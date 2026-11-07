# SHIVAM AI TRADER bot — context for AI coding assistants

Telegram bot that runs on GitHub Actions (no server). Crypto (Binance public data, 4h) and NSE stocks
(yfinance, daily). Seven strategies, each with BUY and mirrored SELL versions, in `bot/strategies.py`, each a list of 3 conditions with
step-by-step explanations. Backtests in R multiples in `bot/backtest.py`. Charts in `bot/chart.py`
(mplfinance). Optional Gemini summary in `bot/ai.py`. Angel One read-only client (live NSE, futures, option chain, Greeks) in `bot/angel.py`; F&O report and OI chart in `bot/fno.py`. Commands, buttons and alerts in `bot/main.py`; the numbered list is remembered in `bot/state.py` (cached between runs by the workflow).
Workflows in `.github/workflows/`. Settings in `config.json`; secrets only via GitHub Actions secrets.

Rules:
1. Never add order placement or exchange/broker trading keys.
2. Indicators and signals are computed in Python; the LLM only rewrites computed facts.
3. Signals use completed candles only; backtests enter at the next candle's open (no look-ahead).
4. Every change to strategies/backtest needs a test in `tests/`; run `python -m pytest -q`.
5. Keep GitHub Actions usage light (no 24/7 loops).
6. Do not invent API endpoints or model names; verify in official docs.

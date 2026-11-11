# SHIVAM AI TRADER bot — context for AI coding assistants

Telegram bot (GitHub Actions alerts, Cloud Run webhook, or `python -m bot.main local` on a PC/VM). Crypto
(Binance), NSE stocks/indices/F&O/India VIX/MCX/CDS (Angel One, Yahoo fallback) and forex (`bot/fx.py`: MT5 →
Twelve Data → Yahoo) on 5m/15m/30m/1h/4h/1d. Fourteen strategies, each with BUY and mirrored SELL versions, in
`bot/strategies.py` (registry `FAMILIES`), each a list of 3 conditions with step-by-step explanations; a
strategy can be "not applicable" (`applies()`), define its own target, exit rule and time stop.
Volatility/VIX in `bot/vol.py`; per-strike Greeks (Black-Scholes fallback) in `bot/greeks.py`; F&O planner
(buy / credit spread / iron condor / hedge) in `bot/fno_plan.py`; pairs trading in `bot/pairs.py`; paper
trading in `bot/paper.py` stored via `bot/store.py` (GCS on Cloud Run, local file otherwise); group chats via
the context in `bot/telegram.py` (`Ctx`, per-user state keys). Backtests in R multiples in `bot/backtest.py`. Charts in `bot/chart.py`
(mplfinance). Optional Gemini summary in `bot/ai.py`. Angel One read-only client (live NSE, futures, option chain, Greeks) in `bot/angel.py`; F&O report and OI chart in `bot/fno.py`. Futures report and trade plan in `bot/futures.py`; fundamentals (Yahoo) in `bot/fundamentals.py`; news feeds and the rule-based market links in `bot/news.py`; finding any NSE stock in `bot/symbols.py`. Commands, buttons and alerts in `bot/main.py`; the numbered list is remembered in `bot/state.py` (cached between runs by the workflow).
Workflows in `.github/workflows/`. Optional Cloud Run webhook server in `bot/server.py` (Dockerfile, `deploy_cloudrun.sh`); with a webhook set, GitHub's commands workflow skips itself. Settings in `config.json`; secrets only via GitHub Actions secrets.

Rules:
1. Never add order placement or exchange/broker trading keys (MT5 is read-only: no order_send; paper trades
   are simulated only).
2. Indicators and signals are computed in Python; the LLM only rewrites computed facts.
3. Signals use completed candles only; backtests enter at the next candle's open (no look-ahead).
4. Every change to strategies/backtest needs a test in `tests/`; run `python -m pytest -q`.
5. Keep GitHub Actions usage light (no 24/7 loops).
6. Do not invent API endpoints or model names; verify in official docs.
7. News links to sectors/stocks and the 🟢/🔴 tone come from keyword rules in `bot/news.py`, never from the LLM.
8. Groups: only the owner can /allow a group; never answer ordinary group chat (see `main.wants_reply`).
9. Short-term memory (`bot/state.py`: drafts, lists, waits) is a file on PC/GitHub and, on Cloud Run
   (`BOT_STORE=gs://`), part of the store document: `state.begin(update_id)` claims the update there (no
   double handling, even across servers) and loads fresh memory, `state.flush()` writes only what changed;
   both are called in `main.process_update`. News pages and `report._CACHE` stay per process. Keep Cloud Run
   at `--max-instances 1` (gunicorn 1 worker + threads; `server._work_lock` = one update at a time).
10. Paper ticket: steps edit the tapped message (`telegram.show(..., message_id)`); buttons are tagged with
    their step (`pt:w:<step>:<value>`, see `main._tag`) and late taps are ignored; `main._TAPS` drops double
    taps; typed text counts as ticket input only if `paper.looks_like_input`.
11. Bump `bot/version.py` on every release; `/version`, the health page and `deploy_cloudrun.sh` use it to
    prove which code is live.

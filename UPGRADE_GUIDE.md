# SHIVAM AI TRADER — Upgrade guide (8 Nov 2026)

This zip has everything in one folder:
- the full project, to patch your GitHub repository;
- Google Cloud Run deployment;
- your Windows PC setup (`start_bot.bat`).

Read part A first, then do only the parts you use.

## What's new (one line each)

1. **Live price** on every card, list and chart, with a blue LIVE line on the chart.
2. **Timeframe buttons:** ⚡ 5m · 15m (scalp) · ☀️ 30m · 1h (intraday) · 🌊 4h · 1D (swing), plus **🔄 Re-analyse**.
3. **14 strategies**, with your 12 merged in without overlap. The strategy check numbers them 1–14 with
   🟢 BUY / 🔴 SELL / 🟡 forming / ⚪ none / ➖ n/a.
4. A **cleaner first reply:** ① chart + live trade card, ② strategy check. Everything else sits behind buttons:
   📖 Strategy details · 🧭 Structure & levels · 📊 Volume & order book · 🌡 Volatility / VIX.
5. **India VIX** on the first card of every Indian asset, plus a 🌡 India VIX button that shows:
   - the scale, from 🟢 calm to 🔴 fear;
   - a 1-year chart;
   - what the current level means for stock traders, option buyers and option sellers.
6. **Asset volatility:** HV20, HV60 and their percentile, plus option IV vs HV for F&O stocks.
7. **🧮 Greeks** per strike (delta, gamma, theta, vega, IV) with a chart and IV skew. When Angel One returns
   none (outside market hours), the bot computes them with Black-Scholes.
8. **🧠 F&O plan**, built from the live option chain, India VIX or IV, the chart and OI. It recommends a plan and
   has buttons for ⚡ option buying, 💰 credit spread, 🦅 iron condor and 🛡 hedging. Each plan shows:
   - the legs, credit or debit, maximum profit and loss, and breakevens;
   - the chance of profit and the position Greeks;
   - a payoff chart and an exit plan.
9. **💱 Forex & commodities:**
   - EURUSD and the other majors and crosses, XAU/USD, XAG/USD, WTI, Brent, natural gas, copper and DXY;
   - USDINR, EURINR, GBPINR and JPYINR, plus MCX gold, silver, crude, natural gas and copper.
   You get the same numbered list and buttons as for crypto.
10. **Fundamentals:**
    - first a brief message with 🟢/🔴 on each key ratio;
    - then **📑 Detailed fundamentals**: ROCE, PEG, EV/EBITDA, interest cover, quick ratio, 3-year CAGR,
      cash conversion, holdings, analyst target, and quarterly and yearly tables;
    - links to Screener and Tickertape.
11. **News:**
    - sources: Moneycontrol, ET, Mint, CoinDesk and others;
    - **newest first**, with each story's age ⏱ shown at the top;
    - 🟢 good / 🔴 bad tone, and the names of related assets in **bold**;
    - tabs: 🇮🇳 Indian Markets · 💱 Forex · 🪙 Crypto · 🌐 Other.
12. **Groups:** you send `/allow` in a group. Members get replies to their own message, tagged with their name,
    and each member has their own lists.
13. **💼 Paper trading:**
    - many trades at once, opened at the live price with live P&L %;
    - stop-loss and target are checked automatically, with ❌ Exit, 📜 History and a 🏆 group leaderboard;
    - the daily risk-rule warning (3 losing trades, or 2% lost in a day).
14. **⚖️ Pairs trading:** cointegration test, hedge ratio and z-score signal, e.g. HDFCBANK/ICICIBANK or TCS/INFY.

Still true: the bot **never places orders**. A test fails the build if any order function appears in the code.

---

## A. Patch your GitHub repository (do this first)

The simplest way is to upload the whole folder again. Files with the same name are replaced; your secrets stay safe.

1. Unzip this file. Open the `shivam-ai-trader-bot` folder.
2. On GitHub, open your repository → **Add file → Upload files**.
3. Select **everything inside the folder**, including the `.github` folder, and drag it onto the page.
   (On Windows, to see `.github` and `.env.example`, turn on View → Show → Hidden items.)
4. **Commit changes.** The **Self-test** workflow starts by itself and runs the 116 offline tests. It should go green.
5. Optional new secrets: **Settings → Secrets and variables → Actions → New repository secret**:

| Secret | What it does |
| --- | --- |
| `TWELVE_DATA_API_KEY` | Live forex, gold and silver (free key from twelvedata.com) |
| `ALLOWED_CHATS` | Group IDs allowed to use the bot (comma separated). Optional, because `/allow` also works |
| `ALLOWED_USERS` | Only these Telegram user IDs may use it. Leave empty to allow everyone in allowed groups |
| `ALERT_CHATS` | Extra chats (e.g. your group ID) that also get automatic alerts |

**If you upload files one by one instead**, these are the files:
- **New:** `bot/fx.py`, `bot/vol.py`, `bot/greeks.py`, `bot/fno_plan.py`, `bot/paper.py`, `bot/pairs.py`,
  `bot/store.py`, `requirements-windows.txt`, `tests/test_upgrade.py`, `UPGRADE_GUIDE.md`.
- **Changed in `bot/`:** `angel.py`, `backtest.py`, `chart.py`, `config.py`, `data.py`, `fundamentals.py`,
  `indicators.py`, `main.py`, `news.py`, `report.py`, `state.py`, `strategies.py`, `structure.py`, `telegram.py`.
- **Changed workflows:** `.github/workflows/alerts.yml`, `commands.yml`, `selftest.yml`.
- **Changed tests:** `conftest.py`, `test_bot.py`, `test_features.py`, `test_fno.py`, `test_server.py`,
  `test_structure.py`.
- **Changed elsewhere:** `config.json`, `deploy_cloudrun.sh`, `start_bot.bat`, `.env.example`, `README.md`, `CLAUDE.md`.

## B. Google Cloud Run (instant replies)

In **Cloud Shell**:

```bash
cd shivam-ai-trader-bot && git pull && bash deploy_cloudrun.sh
```

- Press **Enter** to keep each old value. You'll see three new optional questions:
  `TWELVE_DATA_API_KEY`, `ALLOWED_CHATS` and `ALERT_CHATS`.
- The script now also creates a small **Cloud Storage bucket**, `<project>-shivam-bot-data`, that saves paper
  trades and approved groups. It lets only the bot's service account use it. This stays within the free
  5 GB in us-central1.
- After it finishes, send `/start`. Then send `/settings`: it should say
  `Paper trades saved in: Google Cloud Storage (gs://…)`.

## C. Run it on your Windows PC

1. Install **Python 3.12** from python.org and tick **Add python.exe to PATH**.
2. Double-click **`start_bot.bat`**. The first run installs everything and opens `.env` in Notepad.
   Fill in the Telegram and Angel One values (and Twelve Data if you have it), save, and double-click again.
3. Before starting the PC version, make sure **only one place answers messages**:
   - If Cloud Run is connected, disconnect it first with `.venv\Scripts\python.exe -m bot.main webhook delete`,
     or run `bash deploy_cloudrun.sh disconnect` in Cloud Shell.
   - If GitHub Actions also sends alerts, disable GitHub's "Market alerts" workflow, or start the PC with
     `start_bot.bat --no-alerts`.
4. Leave the window open. Paper trades are saved in `.state\store.json` in the same folder. Every 5 minutes the
   PC version checks open trades and messages you when a stop or target is hit.

## D. Exness / MetaTrader 5 for live forex (PC only, optional)

1. Install **MetaTrader 5** from Exness (Personal Area → Trading → Download MT5). Log in once and keep it open.
   A demo account works. If you can, use the **investor (read-only) password**.
2. `start_bot.bat` installs the `MetaTrader5` Python package automatically. The bot finds the terminal itself.
3. Optional settings in `.env`:
   - `MT5_LOGIN`, `MT5_PASSWORD` and `MT5_SERVER`, if you want the bot to log in by itself;
   - `MT5_SUFFIX`, if your symbols have a suffix such as `m` (Exness Standard: `EURUSDm`). The bot detects it anyway.
4. Send `/settings`. It should show `✅ MetaTrader 5 / Exness: connected (live)`. Charts then say
   `MetaTrader 5 · XAUUSDm (live)`.

Without MT5, forex uses Twelve Data (live, if the key is set) and then Yahoo, which is delayed about 10 minutes
for gold and oil futures. Each message names its source.

## E. Groups

1. Add your bot to the group. As the owner, send **`/allow`** there.
2. Members send `/start`, or use commands like `/analyze ETH 15m`, `/paper`, `/news crypto`.
3. In groups the bot answers only:
   - commands;
   - replies to its own messages;
   - @mentions;
   - answers to its own questions;
   - numbers right after one of its lists.

   Ordinary chat is ignored.
4. Want members to type plain `ETH`? In @BotFather send `/setprivacy` → your bot → **Disable**, then remove the
   bot from the group and add it again.

## F. Check everything

On GitHub, go to **Actions → Self-test → Run workflow** (best on a weekday between 09:15 and 15:30 IST). The Telegram
message now also checks:
- 5m crypto candles, 15m stock candles, India VIX;
- EURUSD and XAUUSD;
- MCX GOLD and USDINR (with Angel One);
- Greeks (including gamma and vega), storage and news feeds.

Forward any ⚠️ or ❌ line to Claude.

## Notes and limits

- **Win rates** quoted for these strategies are typical backtest ranges, not guarantees. Each strategy's own
  history on your symbol is shown in 📖 Strategy details. Paper-trade for 8 or more weeks first.
- **Option plans** use live premiums and Greeks. Margin is not computed (check Angel One's margin calculator).
  "Chance of profit" comes from a simple model.
- **Indian contract sizes:**
  - MCX: GOLD = 100 × price, GOLDM = 10, SILVER = 30, SILVERM = 5, CRUDEOIL = 100, NATURALGAS = 1,250,
    COPPER = 2,500;
  - currency futures: 1,000 per lot.

  These are the standard exchange contract specifications. Check them before trading.
- **Tickertape and Screener** have no free API. Their ratios are computed here from Yahoo statements, with links to both sites.
- **Moneycontrol's** own RSS feeds are often stale, so Moneycontrol stories are read through Google News
  (`site:moneycontrol.com`), plus its RSS feeds when they are fresh.
- Not financial advice.

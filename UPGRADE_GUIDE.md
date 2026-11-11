# SHIVAM AI TRADER — Upgrade guide (12 Nov 2026)

## Update of 12 Nov 2026 — lasting memory, no double answers, version check

**First, check what is running.** Send `/version` to the bot.
- "I don't know that command" → the server still runs a version older than 12 Nov. The update has not
  reached Cloud Run (uploading to GitHub alone does not update it). Do part B below.
- It shows **version 12 Nov 2026** and **Google Cloud Run** → you are up to date.

**Updating Cloud Run this time** (Cloud Shell):
```bash
cd ~/shivam-ai-trader-bot && git pull && bash deploy_cloudrun.sh
```
If `git pull` prints an error, start clean (your settings stay in Cloud Run; press Enter at each question):
```bash
cd ~ && rm -rf shivam-ai-trader-bot && git clone https://github.com/<your-name>/shivam-ai-trader-bot.git
cd shivam-ai-trader-bot && bash deploy_cloudrun.sh
```
The last lines must say `OK: version 12 Nov 2026 is live`. From now on `bash deploy_cloudrun.sh` gets the
newest code by itself.

What is new:
1. **Memory that lasts a day.** The open paper ticket, numbered lists and menu questions are saved in Cloud
   Storage next to the paper trades. They survive restarts and are the same for every server. An open
   ticket waits a day until you confirm or cancel it; 💼 Paper trades shows **📝 Continue ticket**.
2. **A copy of the same tap is refused everywhere.** Every Telegram update is marked as taken in Cloud
   Storage before it is handled, so no second chart, even if two servers were running.
3. **The server stays awake.** A free Cloud Scheduler job opens the bot's address every 5 minutes
   (`KEEP_WARM=0` turns it off; `MIN_INSTANCES=1` is the paid guarantee).
4. **`/version`** (also at the bottom of /start and /settings) and a deploy script that updates itself,
   names the version it deploys and checks it is live.

139 offline tests.

---


## Update of 10 Nov 2026 — speed, double charts, "No paper ticket open"

**What was wrong.** Cloud Run was allowed to start up to three servers. The bot keeps open paper tickets,
numbered lists and recent analyses in a server's memory, so whenever a second server took a message it
knew nothing: it answered "No paper ticket open", analysed again from scratch (slow) or sent the same
chart a second time. Every new server also needed 10–20 seconds to start.

1. **One Cloud Run server** (`--max-instances 1`, start-up CPU boost). It accepts several requests at once
   but works on one message at a time, so a message Telegram sends twice is answered "duplicate" at once.
   **You must run `bash deploy_cloudrun.sh` again (part B) for this to take effect.**
2. **Double taps and late taps are ignored.** The same button tapped again while the bot is working gives
   one answer, not two. Ticket buttons know their step, so an old button can't fill the next step.
3. **The paper ticket stays in one message.** Each tap changes the same message to the next step, and the
   trade confirmation replaces the ticket. A wrong value shows the warning and the step together.
4. **Group chat is not read as ticket input.** While a ticket is open, only numbers and amounts
   (`74250`, `$200`, `market`) are taken; ordinary messages between members are left alone.
5. **Fewer, faster network calls.** Connections to Telegram, Binance and Cloud Storage stay open; the paper
   store is kept in memory between reads (one Cloud Storage request per screen instead of five); the paper
   list fetches only the candles a trade's age needs and writes all fills in one go.
6. **PC mode:** messages are marked as taken immediately, so GitHub's "Bot commands" workflow can no longer
   answer the same message a second time. Still disable that workflow when the PC or Cloud Run answers.
7. Optional: `MIN_INSTANCES=1 bash deploy_cloudrun.sh` keeps the server awake (no wake-up wait). Google
   bills idle time for this.


---


## Update of 9 Nov 2026

1. **Fix for "Something went wrong (DataException)" in live market.** Angel One answers with an "access
   denied" page when it is asked too fast, and its SDK crashed on it. Now every Angel One call is spaced to
   its limits, retried when Angel One is busy, logged in again if the session expires, and the NSE scanner
   fetches all live prices in **one** request. Candles are cached for a short time so the chart, F&O and
   VIX screens don't ask twice.
2. **Paper trading ticket:** your own entry (market, limit or stop order), stop-loss, take profit, size and
   leverage, with the bot's suggestions as buttons; confirm screen with risk, reward, margin, liquidation;
   pending orders; ✏️ move SL/TP; 🚫 cancel.
3. **Risk ladder instead of capital:** $10 / $25 / $50 / $100 (₹1,000 / ₹2,000 / ₹4,000 / ₹8,500)
   → size → reward on every card, ticket and plan. The ₹15,000 / $100 capital settings are gone.

4. **Paper ticket v2 — capital, money stop and money target.** Size the trade by **capital** (`$200`,
   `₹10000` or 💼 buttons) and the bot works out the quantity after leverage, or by **quantity / lots** and
   the bot shows the **capital used**. Stop-loss and take profit can be a **price or a money amount**
   (`$25` loss, `₹5000` profit). Steps: entry → capital/quantity → leverage → stop-loss → take profit.
5. **NSE data without errors:** when Angel One is busy or down, stock and index charts fall back to Yahoo
   (labelled "delayed ~15 min — Angel One busy") instead of showing an error. The NSE website itself is not
   used: its terms forbid automated data collection, its robots.txt blocks `/api/`, and it blocks cloud
   servers such as Cloud Run.

Update the same way as before (part A, then part B or C below).

---

## Earlier update (8 Nov 2026)

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

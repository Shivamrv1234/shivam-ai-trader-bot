# SHIVAM AI TRADER — Telegram bot for crypto, NSE, F&O, forex & commodities

# [SETUP Guide](https://drive.google.com/file/d/1X_St2LjzHTm-xF8KKIf1y4MrmUnkOgVK/view?usp=sharing)
# [Bot Features](https://drive.google.com/file/d/1o6NrhzLhjkx_gkkAk07aRCaaFQYsjND-/view?usp=sharing)

Telegram is where you see alerts, ask questions and paper-trade. The analysis runs on **GitHub Actions**
(alerts), **Google Cloud Run** (instant replies) or **your own Windows PC** (`start_bot.bat`). The bot reads
data only: it has **no code that can place orders** with any broker.

## What you get in Telegram

1. `/start` → the button menu: 🪙 Crypto · 📈 NSE Stocks · 💱 Forex · 🔎 Any NSE stock · 🏢 Fundamentals ·
   📊 Option chain · 🧮 Greeks · 🧠 F&O Plan · 📈 Futures · 🔥 OI build-up · 🌡 India VIX · 📰 News ·
   💼 Paper trades · ⚖️ Pairs · 📚 Strategies · ⚙️ Settings · ❓ Help
2. A scanner (`/crypto`, `/stocks`, `/forex`) gives a **numbered list** with live prices:
   🟢 BUY · 🔴 SELL · ⚖️ mixed · 🟡 forming · ⚪ none. **Tap a number** (or send it).
3. For any asset you get two clean messages:
   - ① **Chart + trade card:** 💹 live price, BUY/SELL, 🎯 entry, 💰 take profit, 🛑 stop-loss,
     reward:risk, size for your capital, 🌡 **India VIX** with its good/bad reading (Indian assets)
   - ② **Strategy check:** all **14 strategies** numbered with 🟢 BUY / 🔴 SELL / 🟡 forming / ⚪ none /
     ➖ not for this chart, the market regime (trending/ranging) and the verdict
4. Buttons under the strategy check:
   - ⏱ **Timeframes:** ⚡ 5m · 15m (scalp) · ☀️ 30m · 1h (intraday) · 🌊 4h · 1D (swing)
   - 📖 **Strategy details** (every condition ✅/❌ with numbers, targets, size, history)
   - 🧭 **Structure & levels** · 📊 **Volume & order book** · 🌡 **Volatility / VIX**
   - ⛓ Option chain · 🧮 Greeks · 🧠 F&O plan · 📈 Futures (F&O stocks and indices)
   - 🏢 Fundamentals · 📰 News · 📝 **Paper trade** · 🔄 **Re-analyse**
5. Send anything directly: `ETH`, `RELIANCE 15m`, `NIFTY`, `XAUUSD 1h`, `GOLD` (MCX), `tata motors`.

| Command | What you get |
| --- | --- |
| `/crypto [tf]`, `/stocks [tf]`, `/forex [tf]` | Numbered scanner lists (e.g. `/stocks 15m`) |
| `/analyze X [tf]`, `/stock X [tf]` | Any coin, NSE stock/index, forex pair or commodity |
| `/fundamentals INFY` | Key ratios 🟢/🔴 first, then **📑 Detailed fundamentals** (ROCE, PEG, EV/EBITDA, interest cover, 3-yr CAGR, cash conversion, quarterly & yearly tables) |
| `/fno NIFTY` | Option chain, PCR, max pain, OI chart, option idea |
| `/greeks NIFTY` | **Per-strike** delta, gamma, theta, vega, IV + chart + IV skew |
| `/plan NIFTY` | **F&O plan:** recommended trade + ⚡ option buying, 💰 credit spread, 🦅 iron condor, 🛡 hedging — legs, credit/debit, max profit/loss, breakevens, chance of profit, position Greeks, payoff chart, exit plan |
| `/futures RELIANCE`, `/buildup` | Futures report and market-wide OI build-up |
| `/vix` | India VIX with the scale (🟢 calm … 🔴 fear), 1-year chart and what it means for buyers/sellers |
| `/news [india/forex/crypto/other/SYMBOL]` | Newest first, each story with its age ⏱, 🟢 good / 🔴 bad tone and **bold** asset names; tabs 🇮🇳 Indian Markets · 💱 Forex · 🪙 Crypto · 🌐 Other |
| `/paper`, `/paper buy ETH`, `/exit 3`, `/history`, `/board` | Paper trading: many trades, live P&L %, auto stop/target, exit button, group leaderboard |
| `/pairs`, `/pair TCS INFY` | Pairs trading: cointegration test, hedge ratio, z-score signal and chart |
| `/strategies`, `/settings`, `/help`, `/id` | Reference |

## The 14 strategies (no overlaps)

| # | Strategy | Built from |
| --- | --- | --- |
| 1 | MA Crossover (9/21 intraday, 20/50 swing, 200-EMA + ADX filters) | old EMA Trend Cross + "MA crossover" |
| 2 | 20-Candle Breakout / Breakdown | kept |
| 3 | EMA Pullback 20/50/200 | old RSI Pullback + "EMA pullback" |
| 4 | MACD Momentum | kept |
| 5 | Demand/Supply Zone + Confluence (S/R, Fib 61.8%, 200 EMA) | old S/R Reaction + "S&D + confluence" |
| 6 | Smart Money BOS + Order Block/FVG | kept |
| 7 | Trendline / Round Number | kept |
| 8 | Fibonacci Golden Zone (50–61.8%, targets 0% / 127.2% / 161.8%) | new |
| 9 | Break & Retest | new |
| 10 | RSI(2) Mean Reversion (Connors) | new |
| 11 | Bollinger Band Mean Reversion (ADX < 20) | new |
| 12 | VWAP Pullback (intraday) | new |
| 13 | Opening Range Breakout 09:15–09:30 (NSE 5m/15m) | new |
| 14 | Gap Fill (NSE intraday) | new |

Option selling (iron condor) is in the 🧠 F&O plan and pairs trading in ⚖️ Pairs, because they need an option
chain or two stocks. Every strategy has BUY and mirrored SELL rules, uses completed candles only (tested
for look-ahead) and shows its own backtest on that symbol (entry next open, fees, slippage, exit rules,
time stops). Win rates quoted online are not guarantees: check each strategy's history line.

## Data sources (live where possible)

| Market | Source |
| --- | --- |
| Crypto | Binance public API — live (Kraken fallback) |
| NSE stocks, indices, F&O, futures, India VIX | **Angel One SmartAPI** — live (Yahoo delayed fallback) |
| USDINR, EURINR, GBPINR, JPYINR; MCX gold, silver, crude, natural gas, copper | **Angel One** (CDS/MCX) — live |
| EURUSD & other pairs, XAU/USD, XAG/USD, oil, gas, DXY | **MetaTrader 5 / Exness** (PC mode, live) → **Twelve Data** (free key, live) → Yahoo (fallback) |
| Fundamentals | Yahoo Finance statements, ratios computed Tickertape/Screener-style (links to both) |
| News | Moneycontrol (via Google News + its RSS), Economic Times, Mint, CoinDesk, Cointelegraph, BBC, Google News |

**Exness:** Exness has no separate data API; prices come through the MetaTrader 5 terminal, whose Python
package works only on Windows. So Exness prices are used when the bot runs on your PC (`start_bot.bat`) with
MT5 open and logged in (a demo account is enough; prefer the **investor/read-only** password). On Cloud Run,
use a free Twelve Data key for live forex.

## Groups

1. Add the bot to a Telegram group.
2. You (the owner, `TELEGRAM_CHAT_ID`) send **`/allow`** in that group. `/deny` removes it. `/id` shows IDs.
3. Members use commands (`/analyze ETH`, `/crypto`, `/paper`) or reply to the bot's messages. The bot
   replies to the member's message and names them; each member has their own lists and paper trades.
4. By default Telegram's *privacy mode* lets bots see only commands, replies and @mentions in groups — that
   is what the bot needs, and it ignores ordinary chat. To type plain symbols like `ETH` in a group, either
   reply to the bot, mention it (`@yourbot ETH`), or turn privacy off in @BotFather (`/setprivacy` →
   Disable) and re-add the bot.

## Paper trading

Tap **📝 Paper trade** under any analysis → **🟢 Paper BUY** or **🔴 Paper SELL**. The trade opens at the
live price with the setup's stop and target (or 1.5 ATR / 2R), sized by your risk setting. **💼 Paper
trades** shows every open trade with live price, P&L in % and money, R multiple, distance to stop and
target, and an **❌ Exit** button. Stops and targets hit while you were away are detected from 5-minute
candles. **📜 History** shows win rate and totals; **🏆 Board** ranks group members. It warns after
3 losing trades or a 2% loss in a day. Trades are saved in Google Cloud Storage on Cloud Run (the deploy
script creates the bucket), in `.state/store.json` on your PC, and in the workflow cache on GitHub.

---

## Setup (about 30 minutes, all in a browser)

### 1. Create the Telegram bot
1. In Telegram, open **@BotFather** → send `/newbot` → choose a name and a username ending in `bot`.
2. Copy the **token** BotFather gives you (looks like `123456789:AA...`). Keep it private.
3. Open your new bot, tap **Start**, then send it `/help` (it will answer once setup is finished).
4. In a browser, open `https://api.telegram.org/bot<TOKEN>/getUpdates` (paste your token in place of
   `<TOKEN>`). Find `"chat":{"id":` followed by a number — that number is your **chat id**.

### 2. Get a free Gemini API key (for the AI summary — optional)
1. Go to **aistudio.google.com**, sign in with Google, click **Get API key → Create API key**. Copy it.
2. In AI Studio, open the model list and copy the exact ID of a current **Flash** model.
   That ID is your `GEMINI_MODEL`.

### 3. Put the project on GitHub
1. Create a free account at **github.com**.
2. Click **+ → New repository**. Name: `shivam-ai-trader-bot`. Choose **Public** (see "Costs" below).
   Click **Create repository**.
3. On the new repo page click **uploading an existing file**. Unzip `shivam-ai-trader-bot.zip` on
   your computer, open the folder, select **everything inside it** (including the `.github` folder) and
   drag it onto the page. Click **Commit changes**.
4. Check that the repo now shows a `.github/workflows` folder with 3 files. If it's missing, the
   alerts can't run: upload that folder again.

### 4. Add your secrets (never put them in files)
In the repo: **Settings → Secrets and variables → Actions**.

**Secrets** tab → **New repository secret**, three times:

| Name | Value |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | token from step 1 |
| `TELEGRAM_CHAT_ID` | chat id from step 1 |
| `GEMINI_API_KEY` | key from step 2 (skip if you don't want the AI summary) |

**Variables** tab → **New repository variable**: name `GEMINI_MODEL`, value = model ID from step 2.

### 4b. Angel One (for live NSE data and F&O)
1. Search "Angel One SmartAPI", sign in to the developer portal with your Angel One client ID, and
   **create an app**. Copy its **API key**.
2. Find **Enable TOTP** (in the SmartAPI portal or the Angel One app). When it shows the QR code, also copy
   the **text secret** shown with it.
3. Add four more repository secrets:

| Name | Value |
| --- | --- |
| `ANGEL_API_KEY` | API key from step 1 |
| `ANGEL_CLIENT_CODE` | your Angel One client ID |
| `ANGEL_PIN` | your Angel One login PIN |
| `ANGEL_TOTP_SECRET` | the text secret from step 2 |

Optional secrets (same place): `TWELVE_DATA_API_KEY` (live forex/gold), `ALLOWED_CHATS` (group IDs),
`ALLOWED_USERS` (only these user IDs), `ALERT_CHATS` (extra chats that get alerts).

**Security:** together these four values can log in to your trading account. GitHub encrypts secrets and
hides them in logs, and this bot never places orders. Never share them, and turn on two-factor login for
your GitHub account (Settings → Password and authentication).

### 5. Test it
**Actions** tab → **Self-test** → **Run workflow** → **Run workflow**. In 1–2 minutes you get a
Telegram message showing whether crypto data, stock data, Angel One (login, instrument list, NIFTY
option chain, open interest, Greeks) and Gemini all work. If any Angel One line shows ⚠️ or ❌, forward that
message to Claude: it lists the exact field names Angel One returned so the code can be adjusted.

### 6. Use it
Send `/start` to your bot. GitHub checks for new messages every 5 minutes (its minimum), so replies
usually arrive within 5–10 minutes. GitHub sometimes starts scheduled runs late, and after you first
upload the workflows it can take an hour or more before the schedule starts running at all.

**For a faster answer:** in the GitHub mobile app (or website) go to **Actions → Bot commands → Run
workflow**, type a command such as `/analyze SOL`, and run it. The result reaches Telegram in about
1–2 minutes.

### 7. Change settings
Open `config.json` in the repo, click the pencil icon, edit, **Commit changes**. You can change:
capital (`capital_inr`, `capital_usdt`), `risk_per_trade_pct`, `target_r`, how many coins to scan,
coins always included, and the NSE stock watchlist.

---

## Instant replies with Google Cloud Run (recommended)

GitHub only checks Telegram every 5 minutes (often later). With Cloud Run, Telegram sends each message
straight to your own small server, so replies arrive in seconds. Alerts keep running on GitHub.

1. Create a Google Cloud account at **console.cloud.google.com** (a card is needed for verification;
   new accounts get free trial credit). Create a project.
2. In the console, click the **Cloud Shell** icon (top right, `>_`).
3. In Cloud Shell, run:
   ```bash
   git clone https://github.com/<your-github-username>/shivam-ai-trader-bot.git
   cd shivam-ai-trader-bot
   bash deploy_cloudrun.sh
   ```
4. Type your values when asked (hidden for secrets). The script deploys the bot and connects Telegram.
5. On GitHub: **Actions → Bot commands → ⋯ → Disable workflow** (Cloud Run answers messages now).
6. Send `/start` to your bot.

**Update after code changes:** upload the new files to GitHub, then in Cloud Shell run
`cd shivam-ai-trader-bot && git pull && bash deploy_cloudrun.sh` and press Enter to keep each value.
**Stop instant replies:** `bash deploy_cloudrun.sh disconnect`, then re-enable the Bot commands workflow.

**Cost:** normal personal use fits Cloud Run's free tier (2 million requests and 180,000 vCPU-seconds a
month). Container images are stored in Artifact Registry, which is free up to 0.5 GB. The deploy script
keeps that small: after every deploy it deletes old images (keeping the 2 newest and the one in use), sets
a cleanup policy so Google deletes images older than 1 day automatically (it runs about once a day), and
prints how much storage is used. Run `bash deploy_cloudrun.sh cleanup` any time to clean up without
deploying. When the
free trial ends you must upgrade the billing account to keep the service running; the free tier still
applies after that. Set a budget alert in **Billing → Budgets & alerts**.

## Run on your own PC or a cloud VM

Windows: double-click `start_bot.bat` (first run installs everything and opens `.env` for your values).
Linux/macOS/VM: `./start_bot.sh`. Or manually:

```
python -m venv .venv            # Windows: py -3.12 -m venv .venv
.venv\Scripts\activate          # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env          # Linux/macOS: cp; then fill in your values
python -m bot.main local        # instant replies + scheduled alerts; Ctrl+C to stop
python -m bot.main local --no-alerts   # replies only; GitHub keeps sending alerts
```

Only one place may answer messages: if Cloud Run is connected, run `python -m bot.main webhook delete`
first, and disable GitHub's "Bot commands" workflow. If the PC/VM sends alerts, disable GitHub's
"Market alerts" workflow. The full steps (including a Google Cloud VM as a service) are in
`SHIVAM_AI_TRADER_Setup_Guide_A_to_Z.pdf`.

## Good to know

- **Costs:** public repositories get free GitHub Actions minutes. Your code is visible to others, but
  your secrets are not. A private repo has a monthly minute limit that the 5-minute message check
  would use up; if you go private, change `*/5` to `*/30` in `.github/workflows/commands.yml`.
- **GitHub's terms** say Actions is meant for software projects. This bot keeps usage light, but GitHub
  could still restrict it. If that happens, the same code can move to Google Cloud Run.
- **60-day rule:** in public repos, GitHub pauses scheduled workflows after 60 days with no commits.
  GitHub emails you first; click **Enable workflow** in the Actions tab, or edit any file to reset it.
- **Delays:** GitHub sometimes starts scheduled runs late during busy hours.
- **Data:** crypto uses Binance's public market-data API (Kraken as fallback). NSE uses Angel One when
  configured, otherwise Yahoo Finance (unofficial, delayed). "Live" means fresh at the moment each run
  starts; this is not a streaming tick-by-tick chart. Greeks come from Angel One during market hours.
- **Static IP:** SEBI's 2026 rules require a whitelisted static IP for API *orders*. This bot only reads
  data, so it shouldn't need one; if Angel One rejects logins from GitHub's servers, the self-test will show it.
- **No trading:** this bot never places orders and needs no exchange or broker API keys.

## Run the tests
`python -m pytest -q` runs 84 offline tests with simulated data. They also run automatically on GitHub
after every change, in the Self-test workflow.

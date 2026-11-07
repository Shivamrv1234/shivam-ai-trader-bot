# SHIVAM AI TRADER — Telegram bot on GitHub Actions

No PC and no server to manage. GitHub runs the analysis on a schedule, and Telegram is where you see
alerts and ask questions.

**How it works in Telegram**
1. Send `/start` for the menu, or `/crypto` (or `/stocks`).
2. You get a **numbered list**: 🟢 BUY setups, 🔴 SELL setups, ⚖️ mixed, 🟡 forming, ⚪ no setup.
3. **Tap a number** (or send it, e.g. `2`). You receive three messages:
   - ① **Chart + trade card:** BUY or SELL, 🎯 entry, 💰 take profit, 🛑 stop-loss, reward:risk,
     position size for your capital, order-book reading
   - ② **Strategy check:** the 7 strategies with 🟢 ready / 🟡 forming / ⚪ none
   - ③ **Full explanation:** every condition ✅/❌ with real numbers, history of each strategy on this
     symbol, market structure and levels, live order book, optional Gemini AI summary
4. You can also send a symbol directly: `ETH`, `RELIANCE`, `NIFTY`, or `/fno NIFTY` for F&O.

**Automatic alerts** use the same three-message format: crypto after every 4h candle, stocks at 16:10 IST
on weekdays (only **fresh** BUY/SELL signals), and F&O snapshots for NIFTY and BANKNIFTY at 09:45 and
14:30 IST on weekdays.

**Data:** crypto is live from Binance. Indian stocks, indices and F&O are live from **your Angel One
account** once you add its secrets (step 7). Without them, stocks fall back to delayed Yahoo data and
F&O is off. The bot reads data only; it has no code that can place orders.

**Strategies:** 7, each with a BUY version and a mirrored SELL version: EMA Trend Cross, 20-Candle Breakout, RSI Pullback in Uptrend,
MACD Momentum, **Support/Demand Bounce (price action)**, **Smart Money: BOS + Order Block/FVG Retest**, and
**Trendline / Round-Number Breakout**.

**Market structure on every chart and message:** support and resistance (with touch counts),
psychological round numbers, trend lines, demand and supply zones, order blocks, fair-value gaps,
BOS/CHoCH structure breaks and swing labels (HH/HL/LH/LL). All levels are computed only from candles
that existed at that moment, so the backtests can't see the future.

**Live volume and order book** in every `/analyze`: buy vs sell pressure near the price, the biggest
buy and sell walls, spread, share of aggressive buying, and today's volume pace. Crypto uses Binance's
public order book; NSE uses Angel One market depth (market hours only). It's shown as context, not as a
signal, because order books change in seconds and large orders can be fake. They are well-known textbook setups, not guaranteed winners. That's why every message shows
the backtest record. Treat everything as paper-trading practice, not financial advice.

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
month). Container image storage beyond 0.5 GB and data transfer can cost a few rupees a month. When the
free trial ends you must upgrade the billing account to keep the service running; the free tier still
applies after that. Set a budget alert in **Billing → Budgets & alerts**.

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
`python -m pytest -q` runs 35 offline tests with simulated data. They also run automatically on GitHub
after every change, in the Self-test workflow.

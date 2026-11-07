"""Entry point used by the GitHub Actions workflows.

    python -m bot.main commands           # answer pending Telegram messages and button taps
    python -m bot.main run "/analyze SOL" # run one command (manual GitHub button)
    python -m bot.main alerts crypto      # scan coins, push fresh BUY/SELL signals
    python -m bot.main alerts stocks      # scan NSE watchlist + indices, push fresh signals
    python -m bot.main alerts fno         # push F&O snapshots (NIFTY, BANKNIFTY by default)
    python -m bot.main selftest           # check secrets + data sources, message you
    python -m bot.main webhook set URL    # Cloud Run: Telegram pushes messages to URL (instant replies)
    python -m bot.main webhook delete     # back to GitHub's 5-minute checks
"""
from __future__ import annotations

import html
import logging
import sys
import traceback

from bot import ai, angel, chart, data, fno, report, state, telegram
from bot.config import cfg, gemini_key, gemini_model, telegram_chat_id, telegram_token

log = logging.getLogger("bot")

MENU = [
    [("🪙 Crypto scan", "cmd:/crypto"), ("📈 NSE stocks scan", "cmd:/stocks")],
    [("📊 NIFTY F&O", "fno:NIFTY"), ("📊 BANKNIFTY F&O", "fno:BANKNIFTY")],
    [("📚 Strategies", "cmd:/strategies"), ("⚙️ Settings", "cmd:/settings"), ("❓ Help", "cmd:/help")],
]

WELCOME = (
    "🤖 <b>SHIVAM AI TRADER</b>\n"
    "Crypto · NSE stocks · F&amp;O — 7 strategies, BUY and SELL setups, with charts.\n\n"
    "<b>How to use</b>\n"
    "1️⃣ Tap <b>Crypto scan</b> (or send /crypto)\n"
    "2️⃣ Tap a coin number (or send it, e.g. <code>2</code>)\n"
    "3️⃣ Get: BUY/SELL, entry, take profit, stop-loss → strategy lights → full explanation\n\n"
    "You can also send a symbol directly: <code>ETH</code>, <code>RELIANCE</code>, <code>NIFTY</code>.\n"
    "⏱ Replies arrive in seconds when the bot runs on Google Cloud Run, or within a few minutes "
    "when GitHub checks for messages.\n"
    "<i>For learning and paper trading — not financial advice.</i>"
)

HELP = (
    "❓ <b>HELP</b>\n\n"
    "<b>Scanners</b>\n"
    "/crypto — numbered list of coins with BUY/SELL setups\n"
    "/stocks — same for NSE stocks + NIFTY/BANKNIFTY\n"
    "Then tap a number or send it (e.g. <code>2</code>).\n\n"
    "<b>One symbol</b>\n"
    "<code>ETH</code> · <code>/analyze SOL</code> · <code>RELIANCE</code> · <code>NIFTY</code>\n\n"
    "<b>F&amp;O</b>\n"
    "/fno NIFTY · /fno BANKNIFTY · /fno RELIANCE\n\n"
    "<b>What you get for a symbol</b>\n"
    "① Chart + trade card: BUY/SELL, 🎯 entry, 💰 take profit, 🛑 stop-loss, size\n"
    "② Strategy check: 7 strategies with 🟢 ready / 🟡 forming / ⚪ none\n"
    "③ Full explanation: every condition, history, structure, order book, AI summary\n\n"
    "<b>Automatic alerts</b>\n"
    "Crypto after every 4h candle · stocks 16:10 IST weekdays · F&amp;O 09:45 &amp; 14:30 IST weekdays\n\n"
    "⏱ With Google Cloud Run, replies come in seconds. Without it, GitHub checks every 5 minutes."
)


# ---------------------------------------------------------------- delivering one symbol

def deliver(a: report.Analysis, alert: bool = False) -> None:
    """Three messages: chart + trade card, strategy check, full explanation."""
    best = a.best()
    title = f"{a.pretty} · {a.timeframe}"
    png = chart.render(a.x, title, best, a.snap)
    caption = report.card(a)
    if alert:
        caption = ("🔔 <b>NEW SIGNAL</b>\n" + caption)[:1020]
    telegram.send_photo(png, caption)
    telegram.send_text(report.strategy_list(a))
    summary = ai.summarize(report.facts(a))
    code = "c" if a.market == "crypto" else "s"
    buttons = [[("🔄 Refresh", f"a:{code}:{a.symbol}"),
                ("⬅️ Back to list", "cmd:/crypto" if a.market == "crypto" else "cmd:/stocks")]]
    if a.is_index:
        buttons[0].append(("📊 F&O", f"fno:{a.symbol}"))
    telegram.send_text(report.explanation(a, summary), buttons=buttons)


def scan(market: str) -> tuple[list[report.Analysis], list[str]]:
    results, errors = [], []
    if market == "crypto":
        universe = data.top_crypto_by_volume(cfg()["crypto"]["top_n_by_volume"])
        items = [(u["symbol"], u.get("change_pct")) for u in universe]
    else:
        names = list(cfg()["stocks"]["watchlist"])
        if angel.configured():
            names = cfg()["stocks"].get("indices", []) + names
        items = [(s, None) for s in names]
    for sym, chg in items:
        try:
            results.append(report.analyze(sym, "crypto" if market == "crypto" else "stock", change_pct=chg))
        except data.DataError as exc:
            log.warning("%s", exc)
            errors.append(sym)
    return results, errors


def cmd_list(market: str, chat_id: str) -> None:
    results, errors = scan(market)
    if not results:
        telegram.send_text(f"⚠️ Could not load any {market} data right now. Try again in a few minutes.\n"
                           + html.escape(", ".join(errors)))
        return
    ordered = report.ordered(results)
    state.remember_list(chat_id, "crypto" if market == "crypto" else "stock", [a.symbol for a in ordered])
    telegram.send_text(report.list_text(results, market, errors), buttons=report.list_buttons(results))


# ---------------------------------------------------------------- F&O

def deliver_fno(name: str) -> None:
    if not angel.configured():
        telegram.send_text("📊 F&amp;O needs your Angel One SmartAPI secrets (see the Setup Guide, part 5).")
        return
    try:
        snap = angel.build_snapshot(angel.session(), name, cfg()["fno"]["strikes_each_side"])
    except angel.AngelError as exc:
        telegram.send_text(f"⚠️ F&amp;O data problem: {html.escape(str(exc))}")
        return
    telegram.send_photo(fno.oi_chart(snap), f"📊 <b>{html.escape(snap.name)} · F&amp;O</b>\n"
                                            f"Spot {snap.spot:,.2f} · PCR {snap.pcr} · expiry {snap.expiry:%d %b}")
    text = fno.summary_text(snap)
    try:
        a = report.analyze(snap.name, "stock")
        text += "\n\n" + fno.mapping_text(f"{report.verdict(a)} (send {snap.name} for the full analysis)",
                                           [s.name for s in a.side_trades("BUY")],
                                           [s.name for s in a.side_trades("SELL")])
    except data.DataError as exc:
        text += f"\n\n(Strategy view unavailable: {html.escape(str(exc))})"
    telegram.send_text(text, buttons=[[("🔄 Refresh", f"fno:{snap.name}"), ("📈 Chart analysis", f"a:s:{snap.name}")]])


def cmd_fno_menu() -> None:
    telegram.send_text("📊 <b>F&amp;O</b> — choose an index, or send /fno SYMBOL for an F&amp;O stock "
                       "(e.g. /fno RELIANCE).",
                       buttons=[[("NIFTY", "fno:NIFTY"), ("BANKNIFTY", "fno:BANKNIFTY")],
                                [("FINNIFTY", "fno:FINNIFTY"), ("MIDCPNIFTY", "fno:MIDCPNIFTY")]])


# ---------------------------------------------------------------- reference commands

def cmd_strategies() -> None:
    from bot.strategies import FAMILIES, LONGS, SHORTS

    lines = ["📚 <b>THE 7 STRATEGIES</b> (each has a BUY and a SELL version)"]
    for n, ((fam, title), lo, sh) in enumerate(zip(FAMILIES, LONGS, SHORTS)):
        lines.append(f"\n{report.KEYCAP[n]} <b>{html.escape(title)}</b>\n🟢 {html.escape(lo.idea)}\n"
                     f"🔴 {html.escape(sh.idea)}")
    r = cfg()["risk"]
    lines.append(f"\nStop from ATR or the nearest level; take profit = {r['target_r']:g}× the risk.\n"
                 "🟢 ready = all 3 conditions met · 🟡 forming = 2 of 3.")
    telegram.send_text("\n".join(lines))


def cmd_settings() -> None:
    c = cfg()
    r = c["risk"]
    telegram.send_text(
        "⚙️ <b>SETTINGS</b> (edit config.json on GitHub to change)\n"
        f"💰 Capital: ₹{r['capital_inr']:,} (NSE) · {r['capital_usdt']:,} USDT (crypto)\n"
        f"🛡 Risk per trade: {r['risk_per_trade_pct']}% · take profit {r['target_r']:g}R\n"
        f"🪙 Crypto: top {c['crypto']['top_n_by_volume']} by volume + {', '.join(c['crypto']['always_include'])} "
        f"· {c['crypto']['timeframe']} candles\n"
        f"📈 Stocks: {', '.join(c['stocks']['watchlist'])}\n"
        f"🏛 Indices: {', '.join(c['stocks'].get('indices', []))}\n"
        f"📊 Data: crypto Binance (live) · NSE {'Angel One (live)' if angel.configured() else 'Yahoo (delayed)'}\n"
        f"🤖 AI summary: {'on' if gemini_key() and gemini_model() else 'off'}"
    )


# ---------------------------------------------------------------- routing

def handle(text: str, chat_id: str | None = None) -> None:
    chat_id = chat_id or telegram_chat_id()
    raw = text.strip()
    parts = raw.split()
    if not parts:
        return
    cmd = parts[0].lower().split("@")[0]
    arg = parts[1] if len(parts) > 1 else ""
    if cmd in ("/start", "/menu"):
        telegram.send_text(WELCOME, buttons=MENU)
    elif cmd == "/help":
        telegram.send_text(HELP, buttons=MENU)
    elif cmd == "/crypto":
        cmd_list("crypto", chat_id)
    elif cmd == "/stocks":
        cmd_list("stocks", chat_id)
    elif cmd == "/strategies":
        cmd_strategies()
    elif cmd == "/settings":
        cmd_settings()
    elif cmd == "/fno":
        deliver_fno(arg) if arg else cmd_fno_menu()
    elif cmd == "/analyze":
        if arg:
            deliver(report.analyze_any(arg))
        else:
            telegram.send_text("Which one? Example: <code>/analyze ETH</code> or just send <code>ETH</code>.")
    elif len(parts) == 1 and cmd.isdigit():
        picked = state.pick(chat_id, int(cmd))
        if picked is None:
            telegram.send_text("Send /crypto or /stocks first, then a number from that list.", buttons=MENU)
        else:
            market, symbol = picked
            deliver(report.analyze(symbol, market, with_flow=True))
    elif len(parts) == 1 and not cmd.startswith("/") and cmd.replace("/", "").isalnum() and len(cmd) <= 15:
        deliver(report.analyze_any(parts[0]))
    else:
        telegram.send_text("I didn't understand that. Here's the menu 👇", buttons=MENU)


def handle_callback(data_str: str, chat_id: str) -> None:
    kind, _, rest = data_str.partition(":")
    if kind == "cmd":
        handle(rest, chat_id)
    elif kind == "a":
        code, _, symbol = rest.partition(":")
        deliver(report.analyze(symbol, "crypto" if code == "c" else "stock", with_flow=True))
    elif kind == "fno":
        deliver_fno(rest)


def _safely(what: str, fn, *args) -> None:
    try:
        fn(*args)
    except data.DataError as exc:
        telegram.send_text(f"⚠️ Data problem: {html.escape(str(exc))}")
    except Exception as exc:  # report, never crash the run
        log.error("%s failed: %s\n%s", what, exc, traceback.format_exc())
        telegram.send_text(f"⚠️ Something went wrong with “{html.escape(what[:50])}” "
                           f"({html.escape(exc.__class__.__name__)}). Check the GitHub Actions log.")


def safe_handle(text: str) -> None:
    _safely(text, handle, text)


def process_update(u: dict) -> None:
    """Handle one Telegram update (a message or a button tap). Used by GitHub runs and Cloud Run."""
    allowed = telegram_chat_id()
    if "callback_query" in u:
        cb = u["callback_query"]
        who = str((cb.get("from") or {}).get("id"))
        where = str(((cb.get("message") or {}).get("chat") or {}).get("id"))
        telegram.answer_callback(cb["id"], "Working on it…")
        if who != allowed or where != allowed:
            log.warning("Ignored a button tap from another chat.")
            return
        log.info("Button: %s", cb.get("data", "")[:60])
        telegram.chat_action(allowed)
        _safely(cb.get("data", ""), handle_callback, cb.get("data", ""), allowed)
        return
    msg = u.get("message") or {}
    if str((msg.get("chat") or {}).get("id")) != allowed or not msg.get("text"):
        log.warning("Ignored a message from another chat.")
        return
    log.info("Command: %s", msg["text"][:60])
    telegram.chat_action(allowed)
    _safely(msg["text"], handle, msg["text"], allowed)


def run_commands() -> None:
    telegram.set_commands()
    try:
        updates = telegram.pending_updates()
    except telegram.TelegramError as exc:
        if "webhook" in str(exc).lower():
            log.info("A webhook is active (Cloud Run answers messages); nothing to do here.")
            return
        raise
    if not updates:
        log.info("No new messages.")
        return
    telegram.confirm_updates(updates[-1]["update_id"])  # confirm first: no repeat loops
    for u in updates:
        process_update(u)


def webhook(action: str, url: str = "") -> None:
    """Connect Telegram to a Cloud Run URL (instant replies) or disconnect it (back to GitHub checks)."""
    if action == "set":
        if not url.startswith("https://"):
            sys.exit("Give the full https:// URL of your Cloud Run service")
        telegram.set_webhook(url.rstrip("/") + "/telegram")
        telegram.set_commands()
        print("Webhook set: Telegram now sends messages to", url)
    elif action == "delete":
        telegram.delete_webhook()
        print("Webhook removed: GitHub's 'Bot commands' workflow can answer messages again")
    else:
        print(telegram.webhook_info())


def run_alerts(market: str) -> None:
    if market == "fno":
        if not angel.configured():
            log.info("F&O snapshots skipped: Angel One not configured")
            return
        for name in cfg()["fno"]["auto_snapshot"]:
            _safely(f"F&O {name}", deliver_fno, name)
        return
    results, errors = scan(market)
    fresh = [a for a in results if any(s.fresh for s in a.setups) and a.direction in ("BUY", "SELL")
             and any(s.fresh and s.side == a.direction for s in a.setups)]
    fresh.sort(key=lambda a: a.score, reverse=True)
    limit = cfg()["alerts"]["max_per_run"]
    log.info("%s scan: %d analysed, %d fresh signals, %d errors", market, len(results), len(fresh), len(errors))
    for a in fresh[:limit]:
        _safely(f"alert {a.symbol}", deliver, a, True)
    if len(fresh) > limit:
        telegram.send_text("🔔 More new signals: " + ", ".join(a.short for a in fresh[limit:])
                           + "\nSend /crypto or /stocks to see the full list.")


def selftest() -> None:
    problems = []
    if not telegram_token():
        problems.append("TELEGRAM_BOT_TOKEN secret missing")
    if not telegram_chat_id():
        problems.append("TELEGRAM_CHAT_ID secret missing")
    lines = []
    avg_vol = None
    try:
        btc = data.crypto_candles("BTCUSDT")
        avg_vol = float(btc["volume"].tail(20).mean())
        lines.append(f"✅ Crypto data OK (BTCUSDT {len(btc)} candles, last close {btc['close'].iloc[-1]:,.2f})")
    except data.DataError as exc:
        lines.append(f"❌ Crypto data failed: {exc}")
    flow = data.crypto_orderflow("BTCUSDT", avg_vol)
    lines.append("✅ Crypto order book OK" if flow else "⚠️ Crypto order book unavailable (analysis still works)")
    try:
        rel = data.stock_candles(cfg()["stocks"]["watchlist"][0])
        lines.append(f"✅ Stock data OK ({len(rel)} daily candles, {rel.attrs.get('source', '')})")
    except data.DataError as exc:
        lines.append(f"❌ Stock data failed: {exc}")
    if angel.configured():
        try:
            a = angel.session()
            lines.append("✅ Angel One login OK")
            lines.append(f"✅ Instrument list OK ({len(a.inst):,} NSE/NFO contracts)")
            snap = angel.build_snapshot(a, "NIFTY", 3)
            oi_ok = any(l.oi for l in snap.legs)
            greeks_ok = any(l.iv is not None for l in snap.legs)
            lines.append(f"✅ NIFTY live {snap.spot:,.2f}, expiry {snap.expiry:%d %b}, {len(snap.legs)} option legs")
            lines.append(("✅" if oi_ok else "⚠️") + f" Open interest {'found' if oi_ok else 'all zero'}")
            lines.append(("✅" if greeks_ok else "ℹ️") + f" Greeks {'found' if greeks_ok else 'empty (normal outside market hours)'}")
            lines.append("Field names seen (send these to Claude if anything shows ⚠️): "
                         + "; ".join(f"{k}: {', '.join(v)}" for k, v in a.last_keys.items()))
        except angel.AngelError as exc:
            lines.append(f"❌ Angel One: {exc}")
    else:
        lines.append("ℹ️ Angel One not configured: NSE data is delayed (Yahoo) and F&O is off")
    if gemini_key() and gemini_model():
        ok = ai.summarize({"test": "Reply with one short sentence saying the connection works."})
        lines.append(f"✅ Gemini OK ({gemini_model()})" if ok else
                     f"❌ Gemini failed for model '{gemini_model()}': {ai.LAST_ERROR or 'unknown'}")
    else:
        lines.append("ℹ️ Gemini not configured (optional)")
    print("\n".join(problems + lines))
    if problems:
        sys.exit(1)
    telegram.set_commands()
    telegram.send_text("🩺 <b>SELF-TEST</b>\n" + html.escape("\n".join(lines)), buttons=MENU)


def main(argv: list[str]) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if len(argv) < 2:
        print(__doc__)
        sys.exit(2)
    mode = argv[1]
    try:
        if mode == "commands":
            run_commands()
        elif mode == "run":
            safe_handle(" ".join(argv[2:]) or "/start")
        elif mode == "alerts":
            run_alerts(argv[2] if len(argv) > 2 else "crypto")
        elif mode == "selftest":
            selftest()
        elif mode == "webhook":
            webhook(argv[2] if len(argv) > 2 else "info", argv[3] if len(argv) > 3 else "")
        else:
            print(__doc__)
            sys.exit(2)
    finally:
        state.touch()  # the workflow caches this file between runs


if __name__ == "__main__":
    main(sys.argv)

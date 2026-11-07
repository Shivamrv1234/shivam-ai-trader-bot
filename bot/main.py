"""Entry point used by the GitHub Actions workflows.

    python -m bot.main commands          # answer pending Telegram messages
    python -m bot.main run "/analyze SOL" # run one command (manual GitHub button)
    python -m bot.main alerts crypto     # scan coins, push fresh signals
    python -m bot.main alerts stocks     # scan NSE watchlist + indices, push fresh signals
    python -m bot.main alerts fno        # push F&O snapshots (NIFTY, BANKNIFTY by default)
    python -m bot.main selftest          # check secrets + data sources, message you
"""
from __future__ import annotations

import html
import logging
import sys
import traceback

from bot import ai, angel, chart, data, fno, report, telegram
from bot.config import cfg, gemini_key, gemini_model, telegram_chat_id, telegram_token

log = logging.getLogger("bot")

HELP = (
    "<b>SHIVAM AI TRADER bot</b>\n"
    "/crypto – rank the most-traded coins by how many strategies agree\n"
    "/stocks – same for your NSE watchlist and indices\n"
    "/analyze SOL – full step-by-step analysis + chart (any coin, NSE stock or index, e.g. /analyze RELIANCE, /analyze NIFTY)\n"
    "/fno NIFTY – futures + option chain: OI, PCR, max pain, support/resistance, ATM premium and Greeks, OI chart "
    "(also BANKNIFTY, FINNIFTY or any F&amp;O stock, e.g. /fno RELIANCE)\n"
    "/strategies – what each of the 7 strategies looks for\n"
    "/settings – capital, risk and watchlists in use\n\n"
    "Replies arrive within about 15–20 minutes (GitHub checks for new messages every 15 min). "
    "For a faster answer, use the GitHub app: Actions → Bot commands → Run workflow.\n"
    "Automatic alerts: crypto after every 4h candle, stocks after the NSE close on weekdays, "
    "F&amp;O snapshots at 09:45 and 14:30 IST on weekdays (needs Angel One)."
)


def deliver(a: report.Analysis) -> None:
    best = a.best()
    title = f"{a.symbol} · {a.timeframe}"
    png = chart.render(a.x, title, best, a.snap)
    telegram.send_photo(png, report.caption(a))
    summary = ai.summarize(report.facts(a))
    telegram.send_text(report.full_text(a, summary))


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


def cmd_rank(market: str) -> None:
    results, errors = scan(market)
    if not results:
        telegram.send_text(f"Could not load any {market} data right now: {html.escape(', '.join(errors))}")
        return
    title = ("Crypto: strongest setups right now" if market == "crypto"
             else "NSE watchlist: strongest setups (daily candles)")
    hint = "/analyze SOL" if market == "crypto" else "/analyze RELIANCE"
    telegram.send_text(report.ranking_text(results, title, errors, hint))
    top = max(results, key=lambda a: a.score)
    if top.trades:
        deliver(top)


def deliver_fno(name: str) -> None:
    if not angel.configured():
        telegram.send_text("F&amp;O needs your Angel One SmartAPI secrets (see README step 7).")
        return
    try:
        snap = angel.build_snapshot(angel.session(), name, cfg()["fno"]["strikes_each_side"])
    except angel.AngelError as exc:
        telegram.send_text(f"F&amp;O data problem: {html.escape(str(exc))}")
        return
    telegram.send_photo(fno.oi_chart(snap), f"<b>{html.escape(snap.name)}</b> option OI · PCR {snap.pcr} · "
                                            f"spot {snap.spot:,.2f}")
    text = fno.summary_text(snap)
    try:
        a = report.analyze(snap.name, "stock")
        text += "\n\n" + fno.mapping_text(f"{report.verdict(a)} (send /analyze {snap.name} for the details)",
                                           [s.name for s in a.trades])
    except data.DataError as exc:
        text += f"\n\n(Strategy view unavailable: {html.escape(str(exc))})"
    telegram.send_text(text)


def cmd_strategies() -> None:
    from bot.strategies import ALL

    lines = ["<b>Strategies</b> (all long/buy setups, judged on completed candles)"]
    for n, s in enumerate(ALL, 1):
        lines.append(f"\n<b>{n}. {html.escape(s.name)}</b>\n{html.escape(s.idea)}")
    r = cfg()["risk"]
    lines.append(f"\nEvery setup: stop from ATR or recent swing, target = {r['target_r']}× the risk. "
                 "TRADE = all conditions met; WATCH = all but one.")
    telegram.send_text("\n".join(lines))


def cmd_settings() -> None:
    c = cfg()
    r = c["risk"]
    telegram.send_text(
        "<b>Settings</b> (edit config.json in GitHub to change)\n"
        f"Capital: {r['capital_inr']:,} INR (stocks), {r['capital_usdt']:,} USDT (crypto)\n"
        f"Risk per trade: {r['risk_per_trade_pct']}% · target {r['target_r']}R\n"
        f"Crypto: top {c['crypto']['top_n_by_volume']} by volume + {', '.join(c['crypto']['always_include'])}, "
        f"{c['crypto']['timeframe']} candles\n"
        f"Stocks: {', '.join(c['stocks']['watchlist'])}\n"
        f"AI summary: {'on' if gemini_key() and gemini_model() else 'off'}"
    )


def handle(text: str) -> None:
    parts = text.strip().split()
    if not parts:
        return
    cmd = parts[0].lower().split("@")[0]
    arg = parts[1] if len(parts) > 1 else ""
    if cmd in ("/start", "/help"):
        telegram.send_text(HELP)
    elif cmd == "/crypto":
        cmd_rank("crypto")
    elif cmd == "/stocks":
        cmd_rank("stocks")
    elif cmd == "/fno":
        deliver_fno(arg or "NIFTY")
    elif cmd == "/strategies":
        cmd_strategies()
    elif cmd == "/settings":
        cmd_settings()
    elif cmd == "/analyze" and arg:
        deliver(report.analyze_any(arg))
    elif cmd == "/analyze":
        telegram.send_text("Which one? Example: /analyze SOL or /analyze RELIANCE")
    elif not cmd.startswith("/") and len(parts) == 1 and parts[0].isalnum() and len(parts[0]) <= 15:
        deliver(report.analyze_any(parts[0]))  # plain "SOL" works too
    else:
        telegram.send_text("Unknown command.\n\n" + HELP)


def safe_handle(text: str) -> None:
    try:
        handle(text)
    except data.DataError as exc:
        telegram.send_text(f"Data problem: {html.escape(str(exc))}")
    except Exception as exc:  # report, never crash the run
        log.error("Command failed: %s\n%s", exc, traceback.format_exc())
        telegram.send_text(f"Something went wrong with “{html.escape(text[:50])}”: "
                           f"{html.escape(exc.__class__.__name__)}. Check the GitHub Actions log.")


def run_commands() -> None:
    updates = telegram.pending_updates()
    if not updates:
        log.info("No new messages.")
        return
    telegram.confirm_updates(updates[-1]["update_id"])  # confirm first: no repeat loops
    allowed = telegram_chat_id()
    for u in updates:
        msg = u.get("message") or {}
        if str((msg.get("chat") or {}).get("id")) != allowed or not msg.get("text"):
            log.warning("Ignored a message from another chat.")
            continue
        log.info("Command: %s", msg["text"][:60])
        safe_handle(msg["text"])


def run_alerts(market: str) -> None:
    if market == "fno":
        if not angel.configured():
            log.info("F&O snapshots skipped: Angel One not configured")
            return
        for name in cfg()["fno"]["auto_snapshot"]:
            deliver_fno(name)
        return
    results, errors = scan(market)
    fresh = [a for a in results if any(s.fresh for s in a.setups)]
    fresh.sort(key=lambda a: a.score, reverse=True)
    limit = cfg()["alerts"]["max_per_run"]
    log.info("%s scan: %d analysed, %d fresh signals, %d errors", market, len(results), len(fresh), len(errors))
    for a in fresh[:limit]:
        names = ", ".join(s.name for s in a.setups if s.fresh)
        telegram.send_text(f"🔔 <b>New signal: {html.escape(a.symbol)}</b> ({html.escape(names)})")
        deliver(a)
    if len(fresh) > limit:
        telegram.send_text("More signals: " + ", ".join(a.symbol for a in fresh[limit:]))


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
        lines.append(f"✅ Stock data OK ({len(rel)} daily candles)")
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
        lines.append("✅ Gemini OK" if ok else "❌ Gemini call failed (check GEMINI_MODEL name and key)")
    else:
        lines.append("ℹ️ Gemini not configured (optional)")
    print("\n".join(problems + lines))
    if problems:
        sys.exit(1)
    telegram.send_text("<b>Self-test</b>\n" + html.escape("\n".join(lines)) + "\n\nSend /help to see commands.")


def main(argv: list[str]) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if len(argv) < 2:
        print(__doc__)
        sys.exit(2)
    mode = argv[1]
    if mode == "commands":
        run_commands()
    elif mode == "run":
        safe_handle(" ".join(argv[2:]) or "/help")
    elif mode == "alerts":
        run_alerts(argv[2] if len(argv) > 2 else "crypto")
    elif mode == "selftest":
        selftest()
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main(sys.argv)

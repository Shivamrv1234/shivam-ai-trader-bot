"""Entry point used by the GitHub Actions workflows, Cloud Run and your own PC.

    python -m bot.main commands           # answer pending Telegram messages and button taps
    python -m bot.main run "/analyze SOL" # run one command (manual GitHub button)
    python -m bot.main alerts crypto      # scan coins, push fresh BUY/SELL signals
    python -m bot.main alerts stocks      # scan NSE watchlist + indices, push fresh signals
    python -m bot.main alerts fno         # push F&O snapshots (NIFTY, BANKNIFTY by default)
    python -m bot.main selftest           # check secrets + data sources, message you
    python -m bot.main webhook set URL    # Cloud Run: Telegram pushes messages to URL (instant replies)
    python -m bot.main webhook delete     # back to GitHub's 5-minute checks
    python -m bot.main local              # own PC / VM: instant replies + scheduled alerts, runs until stopped
    python -m bot.main local --no-alerts  # own PC / VM: instant replies only (alerts stay on GitHub)
"""
from __future__ import annotations

import html
import logging
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

from bot import (ai, angel, chart, data, fno, fundamentals, futures, fx, news, paper, report, state, store,
                 strategies, symbols, telegram, vol)
from bot.config import (allowed_chats, allowed_users, cfg, gemini_key, gemini_model, secret, telegram_chat_id,
                        telegram_token)

log = logging.getLogger("bot")

HOME = ("🏠 Menu", "cmd:/start")
MENU = [
    [("🪙 Crypto", "cmd:/crypto"), ("📈 NSE Stocks", "cmd:/stocks"), ("💱 Forex", "m:fx")],
    [("🔎 Any NSE stock", "m:stock"), ("🏢 Fundamentals", "m:fund")],
    [("📊 Option chain", "m:fno"), ("🧮 Greeks", "m:grk"), ("🧠 F&O Plan", "m:fop")],
    [("📈 Futures", "m:fut"), ("🔥 OI build-up", "bu"), ("🌡 India VIX", "vix")],
    [("📰 News", "n:c:all"), ("💼 Paper trades", "pt:l"), ("⚖️ Pairs", "pr")],
    [("📚 Strategies", "cmd:/strategies"), ("⚙️ Settings", "cmd:/settings"), ("❓ Help", "cmd:/help")],
]

WELCOME = (
    "🤖 <b>SHIVAM AI TRADER</b>\n"
    "Crypto · NSE · F&amp;O · Forex &amp; Commodities · News · Paper trading\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "🪙 <b>Crypto</b> · 📈 <b>NSE</b> · 💱 <b>Forex</b> — numbered list of BUY/SELL setups; tap a number\n"
    "⏱ Every chart: ⚡ 5m/15m scalp · ☀️ 30m/1h intraday · 🌊 4h/1D swing\n"
    "📋 14 strategies with 🟢 BUY / 🔴 SELL lights, live price and India VIX\n"
    "📊 Option chain · 🧮 Greeks · 🧠 F&amp;O plan (buy / sell / hedge)\n"
    "🏢 Fundamentals · 📰 News 🟢/🔴 · 💼 Paper trades · ⚖️ Pairs\n\n"
    "Tip: just send <code>ETH</code>, <code>RELIANCE 15m</code>, <code>XAUUSD</code> or <code>tata motors</code>.\n"
    "<i>For learning and paper trading — not financial advice.</i>"
)

HELP = (
    "❓ <b>HELP</b>\n━━━━━━━━━━━━━━━━━━\n"
    "<b>Scanners</b>\n"
    "/crypto · /stocks · /forex — numbered list; tap a number or send it (e.g. <code>2</code>)\n"
    "Add a timeframe: <code>/crypto 15m</code> · <code>/stocks 1h</code>\n\n"
    "<b>Any asset</b>\n"
    "<code>ETH</code> · <code>RELIANCE 5m</code> · <code>EURUSD 1h</code> · <code>GOLD</code> (MCX) · /analyze BTC 4h\n"
    "You get ① chart + live price + trade card (+ India VIX) ② strategy check (14 strategies, 🟢/🔴), then "
    "buttons: ⏱ timeframes · 📖 details · 🧭 structure &amp; levels · 📊 volume &amp; order book · 🌡 volatility · "
    "📝 paper trade · 🔄 re-analyse\n\n"
    "<b>F&amp;O</b> (Angel One)\n"
    "/fno NIFTY · /greeks NIFTY · /plan NIFTY · /futures RELIANCE · /buildup · /vix\n\n"
    "<b>Company</b> /fundamentals INFY (brief, then 📑 detailed)\n"
    "<b>News</b> /news · /news india · /news forex · /news crypto · /news other · /news RELIANCE\n"
    "<b>Paper trading</b> /paper · /paper buy ETH · /exit 3 · /history · /board\n"
    "<b>Pairs</b> /pairs · /pair HDFCBANK ICICIBANK\n\n"
    "<b>Groups</b> add the bot, then the owner sends /allow in the group. Members use commands "
    "(<code>/analyze@yourbot ETH</code>) or reply to the bot. /id shows chat and user IDs.\n\n"
    "<b>Automatic alerts</b> crypto after every 4h candle · stocks 16:10 IST · F&amp;O 09:45 &amp; 14:30 IST"
)

PROMPTS = {
    "stock": ("🔎 <b>ANALYSE ANY NSE STOCK</b>\nType the NSE symbol or part of the company name — e.g. "
              "<code>TATAMOTORS</code>, <code>tata motors</code>, <code>M&amp;M</code> — or tap one below 👇", "a:s:"),
    "fund": ("🏢 <b>FUNDAMENTALS</b>\nType any NSE symbol or company name (e.g. <code>INFY</code>), "
             "or tap one below 👇", "fd:"),
    "fno": ("📊 <b>OPTION CHAIN</b>\nChoose an index, or type any F&amp;O stock (e.g. <code>RELIANCE</code>) 👇",
            "fno:"),
    "fut": ("📈 <b>FUTURES</b>\nChoose an index, or type any F&amp;O stock (e.g. <code>SBIN</code>) 👇", "fu:"),
    "grk": ("🧮 <b>OPTION GREEKS</b>\nChoose an index, or type any F&amp;O stock 👇", "gk:"),
    "fop": ("🧠 <b>F&amp;O PLAN</b> — option buying, option selling (iron condor / spreads) and hedging from the "
            "live option chain\nChoose an index, or type any F&amp;O stock 👇", "fp:"),
}
INDEX_PICKS = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"]
BACK = {"crypto": "cmd:/crypto", "stock": "cmd:/stocks", "fx": "m:fx"}


def _rows(buttons: list, per_row: int = 3) -> list:
    return [buttons[k:k + per_row] for k in range(0, len(buttons), per_row)]


def key_of(chat_id: str | None) -> str:
    """Per-user state key inside groups (so two members' lists don't mix)."""
    c = telegram.ctx()
    if c and (chat_id is None or str(chat_id) == c.chat_id):
        return c.key
    return str(chat_id or telegram_chat_id())


def who() -> tuple[str, str, str]:
    """(user id, user name, chat id) of whoever this reply is for."""
    c = telegram.ctx()
    owner = telegram_chat_id()
    if c:
        return c.user_id or c.chat_id, c.user_name or "you", c.chat_id
    return owner, "you", owner


def split_tf(parts: list[str]) -> tuple[list[str], str | None]:
    """'/analyze ETH 15m' -> (['ETH'], '15m')."""
    if parts:
        t = parts[-1].lower()
        if t in data.TF_SECONDS or t in ("1D".lower(), "d", "60m", "240m", "1hr", "4hr", "5min", "15min", "30min"):
            return parts[:-1], data.norm_tf(t)
    return parts, None


# ---------------------------------------------------------------- delivering one symbol

def tf_buttons(a: report.Analysis) -> list:
    out = []
    for tf in data.TIMEFRAMES:
        icon = "✅" if tf == a.timeframe else data.TF_MODE[tf].split()[0]
        out.append((f"{icon} {tf.upper() if tf == '1d' else tf}", f"a:{a.code}:{a.symbol}:{tf}"))
    return _rows(out, 3)


def asset_buttons(a: report.Analysis) -> list:
    """Buttons under the strategy check: timeframes, details, and the next step for this asset."""
    sym, cb = a.symbol, a.cb
    rows = tf_buttons(a)
    rows.append([("📖 Strategy details", f"d:{cb}"), ("🧭 Structure & levels", f"st:{cb}")])
    rows.append([("📊 Volume & order book", f"ob:{cb}"), ("🌡 Volatility / VIX", f"vx:{cb}")])
    if a.market == "stock":
        if symbols.has_fno(sym):
            rows.append([("⛓ Option chain", f"fno:{sym}"), ("🧮 Greeks", f"gk:{sym}"), ("🧠 F&O plan", f"fp:{sym}")])
            rows.append([("📈 Futures", f"fu:{sym}"), ("🔥 OI build-up", "bu")])
        if a.is_index:
            rows.append([("📰 Market news", "n:c:india"), ("🌡 India VIX", "vix")])
        else:
            rows.append([("🏢 Fundamentals", f"fd:{sym}"), ("📰 News", f"n:q:{sym}")])
    elif a.market == "crypto":
        rows.append([("📰 Crypto news", "n:c:crypto")])
    else:
        rows.append([("📰 Forex & commodity news", "n:c:forex")])
    rows.append([("📝 Paper trade", f"pt:o:{cb}"), ("🔄 Re-analyse", f"a:{cb}")])
    rows.append([("⬅️ Back to list", BACK[a.market]), HOME])
    return rows


def detail_buttons(a: report.Analysis, here: str) -> list:
    cb = a.cb
    opts = [("📖 Strategy details", f"d:{cb}"), ("🧭 Structure & levels", f"st:{cb}"),
            ("📊 Volume & order book", f"ob:{cb}"), ("🌡 Volatility / VIX", f"vx:{cb}")]
    rows = _rows([o for o in opts if not o[1].startswith(here + ":")], 2)
    rows.append([("📝 Paper trade", f"pt:o:{cb}"), ("🔄 Re-analyse", f"a:{cb}"), HOME])
    return rows


def deliver(a: report.Analysis, alert: bool = False) -> None:
    """Two messages: chart + live trade card, then the strategy check with all the buttons."""
    if not a.vol_line:
        try:
            a.vol_line = vol.card_line(a)
        except Exception as exc:  # VIX is a bonus, never block the analysis
            log.info("VIX line skipped: %s", exc)
    best = a.best()
    title = f"{a.pretty} · {a.timeframe} · {data.TF_MODE.get(a.timeframe, '')}"
    png = chart.render(a.x, title, best, a.snap, live=a.now_price)
    caption = report.card(a)
    if alert:
        caption = "🔔 <b>NEW SIGNAL</b>\n" + caption
    if len(caption) <= telegram.MAX_CAPTION - 60:
        telegram.send_photo(png, caption)
    else:
        telegram.send_photo(png, caption.split("\n\n")[0])
        telegram.send_text(caption)
    telegram.send_text(report.strategy_list(a, alert), buttons=asset_buttons(a))


def deliver_details(market: str, sym: str, tf: str, what: str) -> None:
    a = report.get(market, sym, tf)
    if what == "d":
        summary = ai.summarize(report.facts(a))
        telegram.send_text(report.details(a, summary), buttons=detail_buttons(a, "d"))
    elif what == "st":
        telegram.send_text(report.structure_text(a), buttons=detail_buttons(a, "st"))
    elif what == "ob":
        if a.flow is None:
            a.flow = report.fetch_flow(a, float(a.x["vol_avg20"].iloc[-1]) if a.x["vol_avg20"].notna().any() else None)
        telegram.send_text(report.flow_text(a), buttons=detail_buttons(a, "ob"))
    elif what == "vx":
        parts = []
        v = vol.india_vix() if a.is_indian else None
        if v is not None:
            parts.append(vol.vix_text(v))
        iv, src = None, ""
        if a.market == "stock" and symbols.has_fno(a.symbol):
            try:
                snap = angel.build_snapshot(angel.session(), a.symbol, 3)
                from bot import greeks
                greeks.fill(snap)
                atm = [l for l in snap.legs if l.strike == snap.atm and l.iv]
                if atm:
                    iv, src = sum(l.iv for l in atm) / len(atm), atm[0].greeks_src or "Angel One"
            except angel.AngelError as exc:
                log.info("ATM IV %s: %s", a.symbol, exc)
        parts.append(vol.asset_text(a, iv, src))
        telegram.send_text("\n\n".join(parts), buttons=detail_buttons(a, "vx"))


def analyze_and_deliver(market: str, sym: str, tf: str | None = None) -> None:
    deliver(report.analyze(sym, market, with_flow=True, tf=tf))


def scan(market: str, tf: str | None = None) -> tuple[list[report.Analysis], list[str]]:
    results, errors = [], []
    if market == "crypto":
        universe = data.top_crypto_by_volume(cfg()["crypto"]["top_n_by_volume"])
        items = [(u["symbol"], u.get("change_pct")) for u in universe]
        mkt = "crypto"
    elif market == "fx":
        names = [n for n in fx.watchlist() if fx.find(n) and (angel.configured() or not fx.find(n).indian
                                                              or fx.find(n).yahoo)]
        items, mkt = [(n, None) for n in names], "fx"
    else:
        names = list(cfg()["stocks"]["watchlist"])
        if angel.configured():
            names = cfg()["stocks"].get("indices", []) + names
        items, mkt = [(s, None) for s in names], "stock"
    tf = data.norm_tf(tf, report.default_tf(mkt))

    def load(item):
        sym, _ = item
        try:
            return item, report.load_candles(sym, mkt, tf, for_scan=True), None
        except data.DataError as exc:
            return item, None, exc

    parallel = mkt == "crypto" or (mkt == "fx" and not angel.configured())
    if parallel:
        with ThreadPoolExecutor(max_workers=6) as pool:
            loaded = list(pool.map(load, items))
    else:
        loaded = [load(i) for i in items]
    if mkt == "stock" and angel.configured():
        # one quote request for every symbol (instead of one each): live price + today's change
        try:
            quotes = angel.session().ltp_many([sym for (sym, _), c, _ in loaded if c is not None])
        except angel.AngelError as exc:
            log.warning("Live prices for the list: %s", exc)
            quotes = {}
        for (sym, _), c, _ in loaded:
            q = quotes.get(sym.upper())
            if c is not None and q:
                c.attrs["live_price"] = angel.num(angel.pick(q, "ltp")) or None
                c.attrs["live_change_pct"] = angel.num(angel.pick(q, "percentChange"))
    for (sym, chg), candles, err in loaded:
        if err is not None:
            log.warning("%s", err)
            errors.append(sym)
            continue
        try:
            results.append(report.analyze(sym, mkt, candles=candles, change_pct=chg, tf=tf))
        except data.DataError as exc:
            log.warning("%s", exc)
            errors.append(sym)
    return results, errors


def cmd_list(market: str, chat_id: str, tf: str | None = None) -> None:
    results, errors = scan(market, tf)
    mkt = {"stocks": "stock"}.get(market, market)
    if not results:
        telegram.send_text(f"⚠️ Could not load any {market} data right now. Try again in a few minutes.\n"
                           + html.escape(", ".join(errors)), buttons=[[HOME]])
        return
    ordered = report.ordered(results)
    state.remember_list(key_of(chat_id), mkt, [a.symbol for a in ordered], tf=ordered[0].timeframe)
    buttons = report.list_buttons(results)
    cur = ordered[0].timeframe
    tfs = [(("✅ " if t == cur else "") + (t.upper() if t == "1d" else t),
            f"cmd:/{'stocks' if mkt == 'stock' else 'forex' if mkt == 'fx' else 'crypto'} {t}") for t in data.TIMEFRAMES]
    extra = {"crypto": [("📰 Crypto news", "n:c:crypto"), HOME],
             "fx": [("💱 Pick an asset", "m:fx"), ("📰 Forex news", "n:c:forex"), HOME]}.get(
        mkt, [("🔎 Any NSE stock", "m:stock"), HOME])
    telegram.send_text(report.list_text(results, mkt, errors), buttons=buttons + _rows(tfs, 6) + [extra])


# ---------------------------------------------------------------- forex menu

def fx_menu(chat_id: str) -> None:
    groups = [("💱 Majors &amp; crosses", [a for a in fx.ASSETS.values() if a.kind == "fx"]),
              ("🥇 Metals · 🛢 Energy · 💵 DXY", [a for a in fx.ASSETS.values() if a.kind in ("metal", "energy", "index")]),
              ("🇮🇳 Rupee futures (NSE)", [a for a in fx.ASSETS.values() if a.kind == "inr"]),
              ("🏭 MCX commodities", [a for a in fx.ASSETS.values() if a.kind == "mcx"])]
    lines = ["💱 <b>FOREX &amp; COMMODITIES</b>", "Tap an asset for its chart, live price and strategy check, or "
             "📡 scan them all for setups.", ""]
    lines += fx.source_status()
    if not angel.configured():
        lines.append("ℹ️ Rupee futures and MCX need your Angel One login (USDINR &amp; co fall back to Yahoo spot).")
    rows = [[("📡 Scan all (numbered list)", "cmd:/forex")]]
    for title, assets in groups:
        rows.append([(f"— {html.unescape(title)} —", "n:x")])
        rows += _rows([(f"{a.icon} {a.code}", f"a:f:{a.code}") for a in assets], 4)
    rows.append([("📰 Forex news", "n:c:forex"), HOME])
    telegram.send_text("\n".join(lines), buttons=rows)
    state.set_wait(key_of(chat_id), "fx")


# ---------------------------------------------------------------- any NSE stock

def prompt(chat_id: str, mode: str) -> None:
    """Menu button: remember what the next typed text means, and offer quick picks."""
    key = key_of(chat_id)
    state.set_wait(key, mode)
    text, prefix = PROMPTS[mode]
    picks = list(cfg()["stocks"]["watchlist"])
    if mode in ("fno", "fut", "grk", "fop"):
        if not angel.configured():
            telegram.send_text("📊 Option chain, Greeks, F&amp;O plans and futures need your Angel One SmartAPI "
                               "secrets (see the Setup Guide, part 5).", buttons=[[HOME]])
            state.set_wait(key, None)
            return
        picks = INDEX_PICKS + picks[:5]
    rows = _rows([(p, prefix + p) for p in picks])
    if mode == "fut":
        rows.append([("🔥 Today's OI build-up", "bu")])
    rows.append([HOME])
    telegram.send_text(text, buttons=rows)


def open_stock(query: str, target: str, tf: str | None = None) -> None:
    """Resolve what the user typed to one NSE symbol, then run the target screen."""
    if target == "fx":
        a = fx.find(query)
        if a:
            analyze_and_deliver("fx", a.code, tf)
        else:
            telegram.send_text(f"💱 “{html.escape(query[:30])}” isn't in the forex list. Try EURUSD, XAUUSD, USOIL, "
                               "USDINR or GOLD.", buttons=[[("💱 Forex menu", "m:fx"), HOME]])
        return
    sym, matches = symbols.find_stock(query)
    if sym:
        run_target(target, sym, tf)
    elif matches:
        show_matches(query, matches, target)
    else:
        telegram.send_text(f"🔎 Nothing on NSE matches “{html.escape(query[:40])}”. Try part of the symbol, "
                           "e.g. <code>/search tata</code>.", buttons=[[("🔎 Try again", "m:stock"), HOME]])


def run_target(target: str, sym: str, tf: str | None = None) -> None:
    if target == "fund":
        deliver_fundamentals(sym)
    elif target == "fut":
        deliver_futures(sym)
    elif target == "fno":
        deliver_fno(sym)
    elif target == "grk":
        deliver_greeks(sym)
    elif target == "fop":
        deliver_plan(sym)
    else:
        analyze_and_deliver("stock", sym, tf)


def show_matches(query: str, matches: list[str], target: str = "stock") -> None:
    prefix = PROMPTS.get(target, PROMPTS["stock"])[1]
    telegram.send_text(f"🔎 <b>NSE matches for “{html.escape(query[:40])}”</b>\nTap the one you mean 👇",
                       buttons=_rows([(m, prefix + m) for m in matches], 2) + [[HOME]])


def cmd_search(query: str) -> None:
    sym, matches = symbols.find_stock(query)
    if sym and not matches:
        matches = [sym]
    if not matches:
        telegram.send_text(f"🔎 Nothing on NSE matches “{html.escape(query[:40])}”.", buttons=[[HOME]])
        return
    show_matches(query, matches)


# ---------------------------------------------------------------- fundamentals

_FUND: dict = {}


def _fundamentals(sym: str):
    hit = _FUND.get(sym)
    if hit and time.time() - hit[0] < 900:
        return hit[1]
    f = fundamentals.fetch(sym)
    _FUND[sym] = (time.time(), f)
    return f


def deliver_fundamentals(sym: str, detailed: bool = False) -> None:
    sym = sym.upper()
    if sym in angel.INDEX_ALIASES:
        telegram.send_text("🏢 Indices have no company fundamentals. Pick a stock instead.",
                           buttons=[[("🏢 Choose a stock", "m:fund"), HOME]])
        return
    f = _fundamentals(sym)
    rows = []
    if not detailed:
        rows.append([("📑 Detailed fundamentals", f"fdd:{sym}")])
    rows.append([("📈 Technical analysis", f"a:s:{sym}"), ("📰 News", f"n:q:{sym}")])
    if symbols.has_fno(sym):
        rows.append([("📊 Option chain", f"fno:{sym}"), ("📈 Futures", f"fu:{sym}")])
    rows.append(fundamentals.links(sym))
    rows.append([("🔄 Refresh", f"fd:{sym}"), HOME])
    text = fundamentals.detailed_text(f) if detailed else fundamentals.brief_text(f)
    telegram.send_text(text, buttons=rows)


# ---------------------------------------------------------------- F&O, Greeks, plan, futures

NEED_ANGEL = "📊 F&amp;O and futures need your Angel One SmartAPI secrets (see the Setup Guide, part 5)."


def _snapshot(name: str, strikes: int | None = None):
    if not angel.configured():
        telegram.send_text(NEED_ANGEL, buttons=[[HOME]])
        return None
    try:
        return angel.build_snapshot(angel.session(), name, strikes or cfg()["fno"]["strikes_each_side"])
    except angel.AngelError as exc:
        telegram.send_text(f"⚠️ F&amp;O data problem: {html.escape(str(exc))}", buttons=[[HOME]])
        return None


def fno_buttons(name: str, here: str) -> list:
    opts = [("⛓ Option chain", f"fno:{name}"), ("🧮 Greeks", f"gk:{name}"), ("🧠 F&O plan", f"fp:{name}"),
            ("📈 Futures", f"fu:{name}"), ("📉 Chart analysis", f"a:s:{name}"), ("🌡 India VIX", "vix")]
    rows = _rows([o for o in opts if not o[1].startswith(here)], 3)
    rows.append([("🔄 Refresh", f"{here}{name}"), HOME])
    return rows


def deliver_fno(name: str) -> None:
    snap = _snapshot(name)
    if snap is None:
        return
    from bot import greeks
    greeks.fill(snap)
    telegram.send_photo(fno.oi_chart(snap), f"📊 <b>{html.escape(snap.name)} · F&amp;O</b>\n"
                                            f"Spot {snap.spot:,.2f} · PCR {snap.pcr} · expiry {snap.expiry:%d %b}")
    text = fno.summary_text(snap)
    try:
        a = report.analyze(snap.name, "stock", tf="1d")
        text += "\n\n" + fno.option_idea(snap, a)
    except data.DataError as exc:
        text += f"\n\n(Option idea unavailable: {html.escape(str(exc))})"
    telegram.send_text(text, buttons=fno_buttons(snap.name, "fno:"))


def deliver_greeks(name: str) -> None:
    snap = _snapshot(name)
    if snap is None:
        return
    from bot import greeks
    greeks.fill(snap)
    png = greeks.chart(snap)
    if png:
        telegram.send_photo(png, f"🧮 <b>{html.escape(snap.name)} · Greeks by strike</b> · expiry {snap.expiry:%d %b}")
    telegram.send_text(greeks.report_text(snap), buttons=fno_buttons(snap.name, "gk:"))


PLAN_LABEL = {"buy": "⚡ Option buying", "credit": "💰 Credit spread", "condor": "🦅 Iron condor",
              "hedge": "🛡 Hedging"}


def deliver_plan(name: str, which: str = "auto") -> None:
    snap = _snapshot(name)
    if snap is None:
        return
    from bot import fno_plan
    try:
        a = report.analyze(snap.name, "stock", tf="1d")
    except data.DataError:
        a = None
    rec, alts = fno_plan.recommend(snap, a)
    if which == "auto":
        plan, recommended = rec, True
    else:
        vv = fno_plan.vol_view(snap, a)
        side = a.best().side if a is not None and a.best() else ("BUY" if (snap.pcr or 1) >= 1 else "SELL")
        plan = {"buy": lambda: fno_plan.plan_buy(snap, a, vv),
                "credit": lambda: fno_plan.plan_credit(snap, a, vv, side),
                "condor": lambda: fno_plan.plan_condor(snap, vv, a),
                "hedge": lambda: fno_plan.plan_hedge(snap, a, vv)}.get(which, lambda: rec)()
        recommended = plan.key == rec.key
    png = fno_plan.payoff_chart(plan, snap)
    if png:
        telegram.send_photo(png, f"🧠 <b>{html.escape(snap.name)} · F&amp;O plan</b> · payoff per lot at expiry")
    rows = [[(("✅ " if plan.key == k else "") + lbl, f"fp:{snap.name}:{k}") for k, lbl in list(PLAN_LABEL.items())[:2]],
            [(("✅ " if plan.key == k else "") + lbl, f"fp:{snap.name}:{k}") for k, lbl in list(PLAN_LABEL.items())[2:]],
            [("⭐ Recommended", f"fp:{snap.name}"), ("⛓ Option chain", f"fno:{snap.name}"), ("🧮 Greeks", f"gk:{snap.name}")],
            [("🔄 Refresh", f"fp:{snap.name}:{which}"), HOME]]
    telegram.send_text(fno_plan.plan_text(plan, snap, recommended), buttons=rows)


def deliver_futures(name: str) -> None:
    if not angel.configured():
        telegram.send_text(NEED_ANGEL, buttons=[[HOME]])
        return
    try:
        rep = angel.build_futures(angel.session(), name)
    except angel.AngelError as exc:
        telegram.send_text(f"⚠️ Futures data problem: {html.escape(str(exc))}", buttons=[[HOME]])
        return
    png = futures.chart(rep)
    near = rep.legs[0]
    if png:
        telegram.send_photo(png, f"📈 <b>{html.escape(rep.name)} · FUTURES</b>\n{html.escape(near.symbol)} "
                                 f"{near.ltp:,.2f} ({near.change_pct:+.2f}%) · spot {rep.spot:,.2f}")
    text = futures.report_text(rep)
    try:
        text += "\n\n" + futures.trade_plan(rep, report.analyze(rep.name, "stock", tf="1d"))
    except data.DataError as exc:
        text += f"\n\n(Trade plan unavailable: {html.escape(str(exc))})"
    telegram.send_text(text, buttons=[[("📊 Option chain", f"fno:{rep.name}"), ("🧠 F&O plan", f"fp:{rep.name}"),
                                       ("📉 Chart analysis", f"a:s:{rep.name}")],
                                      [("🔄 Refresh", f"fu:{rep.name}"), ("🔥 OI build-up", "bu"), HOME]])


def cmd_buildup() -> None:
    if not angel.configured():
        telegram.send_text(NEED_ANGEL, buttons=[[HOME]])
        return
    sess = angel.session()
    lists = angel.market_buildup(sess)
    names = futures.buildup_symbols(lists, sess.inst)[:9]
    rows = _rows([(f"📈 {n}", f"fu:{n}") for n in names]) + [[("🔄 Refresh", "bu"), HOME]]
    telegram.send_text(futures.buildup_text(lists), buttons=rows)


def cmd_vix() -> None:
    v = vol.india_vix()
    if v is None:
        telegram.send_text("🌡 India VIX is unavailable right now (Angel One and Yahoo both failed). Try again soon.",
                           buttons=[[HOME]])
        return
    telegram.send_photo(vol.chart(v), f"🌡 <b>India VIX {v.value:.2f}</b> · {v.icon} {v.label}")
    telegram.send_text(vol.vix_text(v), buttons=[[("📈 NIFTY chart", "a:s:NIFTY"), ("🧠 NIFTY F&O plan", "fp:NIFTY")],
                                                 [("🔄 Refresh", "vix"), HOME]])


# ---------------------------------------------------------------- pairs

def cmd_pairs() -> None:
    pairs_ = cfg().get("pairs", [])
    rows = _rows([(f"{a} / {b}", f"pr:{a}:{b}") for a, b in pairs_], 2) + [[HOME]]
    telegram.send_text("⚖️ <b>PAIRS TRADING</b> (statistical arbitrage)\nTwo related stocks that usually move together: "
                       "when the gap between them stretches (z-score beyond ±2), buy the cheap one and sell the rich "
                       "one, then exit when the gap closes.\nTap a pair, or send <code>/pair TCS INFY</code> 👇",
                       buttons=rows)


def deliver_pair(a: str, b: str) -> None:
    from bot import pairs

    r = pairs.analyze(a, b)
    telegram.send_photo(pairs.chart(r), f"⚖️ <b>{r.a} / {r.b}</b> · z-score {r.z_now:+.2f}")
    telegram.send_text(pairs.report_text(r), buttons=[[(f"📈 {r.a}", f"a:s:{r.a}"), (f"📈 {r.b}", f"a:s:{r.b}")],
                                                      [("🔄 Refresh", f"pr:{r.a}:{r.b}"), ("⚖️ Other pairs", "pr"), HOME]])


# ---------------------------------------------------------------- paper trading

def paper_buttons(trades: list[dict], pending: list[dict]) -> list:
    rows = []
    for t in trades[:10]:
        rows.append([(f"❌ Exit #{t['id']} {t['symbol'][:9]}", f"pt:x:{t['id']}"), (f"✏️ SL/TP #{t['id']}", f"pt:e:{t['id']}")])
    for t in pending[:6]:
        rows.append([(f"🚫 Cancel #{t['id']} {t['symbol'][:9]}", f"pt:n:{t['id']}"), (f"✏️ SL/TP #{t['id']}", f"pt:e:{t['id']}")])
    c = telegram.ctx()
    rows.append([("🔄 Refresh", "pt:l"), ("📜 History", "pt:h")] + ([("🏆 Board", "pt:bd")] if c and c.is_group else []))
    rows.append([("🪙 Crypto", "cmd:/crypto"), ("📈 NSE", "cmd:/stocks"), ("💱 Forex", "m:fx"), HOME])
    return rows


def paper_status(message_id: int | None = None) -> None:
    uid, name, chat = who()
    events = paper.check_stops(chat, uid)
    trades, pending = paper.open_trades(chat, uid), paper.pending_orders(chat, uid)
    prices = {t["id"]: paper.live_price(t["market"], t["symbol"]) for t in trades + pending}
    title = "PAPER TRADES" + (f" · {name}" if telegram.ctx() and telegram.ctx().is_group else "")
    text = paper.status_text(trades, prices, title, events, pending)
    warn = paper.risk_warning(chat, uid)
    if warn:
        text += "\n\n" + warn
    telegram.show(text, None, paper_buttons(trades, pending), message_id)


def paper_ticket(market: str, sym: str, tf: str) -> None:
    a = report.get(market, sym, tf, with_flow=False)
    a.live_price = paper.live_price(market, a.symbol) or a.now_price
    uid, _, chat = who()
    text = paper.ticket_text(a)
    warn = paper.risk_warning(chat, uid)
    if warn:
        text += "\n\n" + warn
    cb = a.cb
    telegram.send_text(text, buttons=[[("🟢 BUY · 🤖 bot levels", f"pt:r:b:{cb}"), ("🔴 SELL · 🤖 bot levels", f"pt:r:s:{cb}")],
                                      [("✍️ BUY · my own", f"pt:c:b:{cb}"), ("✍️ SELL · my own", f"pt:c:s:{cb}")],
                                      [("💼 My paper trades", "pt:l"), HOME]])


def _tag(rows: list, step: str) -> list:
    """Ticket buttons carry their step, so a late or repeated tap can never land on the next step."""
    return [[(lbl, f"pt:w:{step}:{cb[5:]}" if cb.startswith("pt:v:") else cb) for lbl, cb in row] for row in rows]


def paper_step(mid: int | None = None, note: str = "") -> None:
    """Show the current step of the ticket (in the tapped message when there is one) and wait for a typed number."""
    key = key_of(None)
    d = state.draft(key)
    if d is None:
        telegram.send_text("That paper ticket expired. Tap 📝 Paper trade again.", buttons=[[("💼 My paper trades", "pt:l"), HOME]])
        return
    text, rows = paper.step_prompt(d)
    if note:
        text = f"⚠️ {html.escape(note)}\n\n{text}"
    state.set_wait(key, "pt" if d["step"] != "confirm" else None)
    telegram.show(text, None, _tag(rows, d["step"]), mid)


def paper_start(market: str, sym: str, tf: str, side: str, recommended: bool, mid: int | None = None) -> None:
    a = report.get(market, sym, tf, with_flow=False)
    a.live_price = paper.live_price(market, a.symbol) or a.now_price
    state.set_draft(key_of(None), paper.new_draft(a, side, recommended))
    paper_step(mid)


def paper_input(raw: str, mid: int | None = None, step: str | None = None) -> None:
    """A typed number or a tapped suggestion for the ticket."""
    key = key_of(None)
    d = state.draft(key)
    if d is None:
        telegram.send_text("No paper ticket open. Tap 📝 Paper trade under any analysis.", buttons=[[HOME]])
        return
    if d.get("edit_id"):
        paper_edit_input(d, raw, mid)
        return
    if step and d["step"] != step:
        log.info("Ignored a late tap for step %s (ticket is at %s).", step, d["step"])
        return
    err = paper.apply_value(d, raw.strip())
    state.set_draft(key, d)
    paper_step(mid, err or "")


def paper_confirm(mid: int | None = None) -> None:
    key = key_of(None)
    d = state.draft(key)
    if d is None or d.get("step") != "confirm":
        telegram.send_text("Nothing to confirm. Tap 📝 Paper trade again.", buttons=[[HOME]])
        return
    uid, name, chat = who()
    d["live"] = paper.live_price(d["market"], d["symbol"]) or d["live"]
    t = paper.place(d, uid, name, chat)
    state.set_draft(key, None)
    state.set_wait(key, None)
    icon = "🟢" if t["side"] == "BUY" else "🔴"
    what = "opened" if t["status"] == "open" else f"placed ({t['order']} order, waits for {t['entry']:,.6g})"
    telegram.show(f"{icon} <b>Paper {t['side']} {what} · #{t['id']}</b>\n" + paper.trade_block(t, d["live"]),
                  None, [[("💼 My paper trades", "pt:l")], [HOME]], mid)


def paper_edit_start(trade_id: int) -> None:
    uid, _, chat = who()
    t = paper.find(trade_id, chat)
    if t["user_id"] != uid and uid != telegram_chat_id():
        raise paper.PaperError(f"Trade #{trade_id} belongs to {t['user_name']}.")
    d = {"edit_id": trade_id, "step": "sl", "side": t["side"], "entry": t["entry"], "cur": t["currency"],
         "pretty": t["pretty"], "sl": t["stop"], "tp": t.get("target")}
    state.set_draft(key_of(None), d)
    paper_edit_prompt(d)


def paper_edit_prompt(d: dict, mid: int | None = None, note: str = "") -> None:
    key = key_of(None)
    state.set_wait(key, "pt")
    cur = d["cur"]
    f = paper._fmt
    if d["step"] == "sl":
        text = (f"✏️ <b>#{d['edit_id']} {html.escape(d['pretty'])}</b> · entry {f(d['entry'], cur)}\n"
                f"Send a new <b>stop-loss</b>, or tap:")
        rows = [[(f"Keep {f(d['sl'], cur)}", "pt:v:keep"), (f"Breakeven {f(d['entry'], cur)}", "pt:v:be")]]
    else:
        text = f"✏️ <b>#{d['edit_id']}</b> stop-loss {f(d['sl'], cur)}. Send a new <b>take profit</b>, or tap:"
        rows = [[(f"Keep {f(d['tp'], cur)}", "pt:v:keep")]]
    rows.append([("✖ Cancel", "pt:q")])
    if note:
        text = f"⚠️ {html.escape(note)}\n\n{text}"
    telegram.show(text, None, _tag(rows, "e" + d["step"]), mid)


def paper_edit_input(d: dict, raw: str, mid: int | None = None) -> None:
    key = key_of(None)
    raw = paper.WORDS.get(raw.strip().lower(), raw.strip())
    if raw == "keep":
        v = d["sl"] if d["step"] == "sl" else d["tp"]
    elif raw == "be":
        v = d["entry"]
    else:
        v = paper.parse_number(raw)
    up = d["side"] == "BUY"
    err = None
    if v is None or v <= 0:
        err = "Please send a number."
    elif d["step"] == "tp" and ((up and v <= d["entry"]) or (not up and v >= d["entry"])):
        err = f"The target must be {'above' if up else 'below'} the entry."
    if err:
        paper_edit_prompt(d, mid, err)
        return
    if d["step"] == "sl":
        d["sl"], d["step"] = v, "tp"
        state.set_draft(key, d)
        paper_edit_prompt(d, mid)
        return
    uid, _, chat = who()
    t = paper.modify(d["edit_id"], uid, chat, telegram_chat_id(), stop=d["sl"], target=v)
    state.set_draft(key, None)
    state.set_wait(key, None)
    telegram.show(f"✏️ <b>#{t['id']} updated</b> · 🛑 {paper._fmt(t['stop'], t['currency'])} · "
                  f"🎯 {paper._fmt(t['target'], t['currency'])}", None, [[("💼 My paper trades", "pt:l"), HOME]], mid)


def paper_exit(trade_id: int) -> None:
    uid, _, chat = who()
    t = paper.exit_trade(trade_id, uid, chat, telegram_chat_id())
    from bot import risk
    icon = "🟢" if t["pnl"] >= 0 else "🔴"
    roe = f" · on margin {t['roe']:+.1f}%" if t.get("roe") is not None and t.get("lev") else ""
    telegram.send_text(f"{icon} <b>Paper trade #{t['id']} closed</b> · {html.escape(t['pretty'])} {t['side']}\n"
                       f"Entry {paper._fmt(t['entry'], t['currency'])} → exit {paper._fmt(t['exit'], t['currency'])}\n"
                       f"<b>{risk.money(t['pnl'], t['currency'], True)}</b> · price {t['pnl_pct']:+.2f}%{roe}"
                       + (f" · {t['r']:+.2f}R" if t.get("r") is not None else ""),
                       buttons=[[("💼 My paper trades", "pt:l"), ("📜 History", "pt:h")], [HOME]])


def paper_callback(rest: str, message_id: int | None) -> None:
    sub, _, val = rest.partition(":")
    uid, name, chat = who()
    if sub == "l":
        paper_status(message_id)
    elif sub == "h":
        telegram.send_text(paper.history_text(paper.closed_trades(chat, uid), name),
                           buttons=[[("💼 My paper trades", "pt:l"), HOME]])
    elif sub == "bd":
        telegram.send_text(paper.board_text(chat), buttons=[[("💼 My paper trades", "pt:l"), HOME]])
    elif sub == "x" and val.isdigit():
        paper_exit(int(val))
    elif sub == "n" and val.isdigit():
        t = paper.cancel_order(int(val), uid, chat, telegram_chat_id())
        telegram.send_text(f"🚫 Pending order #{t['id']} {html.escape(t['pretty'])} cancelled.",
                           buttons=[[("💼 My paper trades", "pt:l"), HOME]])
    elif sub == "e" and val.isdigit():
        paper_edit_start(int(val))
    elif sub == "v":
        paper_input(val, message_id)
    elif sub == "w":                       # a ticket button that knows which step it belongs to
        step, _, val = val.partition(":")
        d = state.draft(key_of(None))
        editing = step in ("esl", "etp")       # ✏️ SL/TP of a trade that is already open
        if d and (bool(d.get("edit_id")) != editing or (editing and step != "e" + d["step"])):
            log.info("Ignored a late tap for step %s.", step)
            return
        paper_input(val, message_id, None if editing else step)
    elif sub == "g":                       # change one field from the confirm screen
        d = state.draft(key_of(None))
        if d:
            d["step"], d["edit_back"] = ("size" if val == "qty" else val), True
            state.set_draft(key_of(None), d)
        paper_step(message_id if d else None)
    elif sub == "k":
        paper_confirm(message_id)
    elif sub == "q":
        had = state.draft(key_of(None)) is not None
        state.set_draft(key_of(None), None)
        state.set_wait(key_of(None), None)
        telegram.show("✖ Paper ticket cancelled.", None, [[("💼 My paper trades", "pt:l"), HOME]],
                      message_id if had else None)
    elif sub in ("o", "b", "s", "r", "c"):
        if sub in ("r", "c"):
            side_code, _, val = val.partition(":")
        else:
            side_code = sub                # older buttons: pt:b / pt:s = bot levels
        code, _, rest2 = val.partition(":")
        sym, _, tf = rest2.partition(":")
        market = report.CODE_MARKET.get(code, "crypto")
        tf = data.norm_tf(tf, report.default_tf(market))
        if sub == "o":
            paper_ticket(market, sym, tf)
        else:
            paper_start(market, sym, tf, "BUY" if side_code == "b" else "SELL", recommended=sub != "c",
                        mid=message_id if sub in ("r", "c") else None)


def cmd_paper(args: list[str]) -> None:
    if not args:
        paper_status()
        return
    act = args[0].lower()
    if act in ("buy", "sell") and len(args) >= 2:
        rest, tf = split_tf(args[1:])
        sym, market = report.resolve(rest[0])
        try:
            a = report.analyze(sym, market, tf=tf)
        except data.DataError:
            if market != "crypto":
                raise
            a = report.analyze(sym, "stock", tf=tf)
        paper_start(a.market, a.symbol, a.timeframe, act.upper(), recommended=False)
    elif act in ("history", "h"):
        paper_callback("h", None)
    elif act in ("board", "leaderboard"):
        paper_callback("bd", None)
    else:
        telegram.send_text("Use <code>/paper</code> (status) or <code>/paper buy ETH</code> / "
                           "<code>/paper sell RELIANCE 15m</code> to open a ticket; <code>/exit 3</code> closes #3.",
                           buttons=[[HOME]])


# ---------------------------------------------------------------- news

def _news_title(cat: str) -> str | None:
    return f"📰 <b>{html.escape(cat[2:])} NEWS</b>" if cat.startswith("q:") else None


def news_buttons(total_items: int, page: int, cat: str) -> list:
    total = news.pages(list(range(total_items)))
    start = (page - 1) * news.PAGE
    nums = [(str(n), f"n:d:{n}") for n in range(start + 1, min(start + news.PAGE, total_items) + 1)]
    rows = _rows(nums, 5)
    nav = []
    if page > 1:
        nav.append(("◀️ Prev", f"n:p:{page - 1}"))
    nav.append((f"· {page}/{total} ·", "n:x"))
    if page < total:
        nav.append(("Next ▶️", f"n:p:{page + 1}"))
    rows.append(nav)
    tabs = [(("• " if c == cat else "") + label, f"n:t:{c}") for c, label in news.CATEGORIES.items()]
    rows += [tabs[:3], tabs[3:]]
    rows.append([("🔄 Refresh", "n:r"), HOME])
    return rows


def cmd_news(chat_id: str, cat: str = "all", message_id: int | None = None) -> None:
    """cat: all / india / forex / crypto / other, or 'q:SYMBOL' for one stock's news."""
    query = cat[2:] if cat.startswith("q:") else None
    if not query:
        cat = news.CAT_ALIASES.get(cat, cat)
    items = news.collect("all" if query else cat, query=query)
    state.remember_news(key_of(chat_id), cat, [i.to_dict() for i in items], 1)
    show_news_page(chat_id, 1, message_id)


def show_news_page(chat_id: str, page: int, message_id: int | None = None) -> None:
    entry = state.news(key_of(chat_id))
    if not entry:
        cmd_news(chat_id, "all", message_id)
        return
    items = [news.Item.from_dict(d) for d in entry["items"]]
    page = min(max(page, 1), news.pages(items))
    state.set_news_page(key_of(chat_id), page)
    cat = entry["cat"]
    telegram.show(news.page_text(items, page, cat, _news_title(cat)), None,
                  news_buttons(len(items), page, cat), message_id)


def news_detail(chat_id: str, n: int) -> None:
    entry = state.news(key_of(chat_id))
    items = entry["items"] if entry else []
    if not (1 <= n <= len(items)):
        telegram.send_text("Send /news first, then a story number from that list.",
                           buttons=[[("📰 News", "n:c:all"), HOME]])
        return
    it = news.Item.from_dict(items[n - 1])
    extra = news.article_description(it.link) if len(it.summary) < 120 else ""
    take = ai.news_take(it.title, it.summary or extra, [r.link for r in news.rules_of(it)])
    stocks, coins = news.watch_symbols(it)
    rows = []
    watch = [(f"📈 {s}", f"a:s:{s}") for s in stocks] + [(f"🪙 {c}", f"a:c:{c}USDT") for c in coins]
    if "forex" in (it.cats or []):
        watch += [("🥇 XAUUSD", "a:f:XAUUSD"), ("🛢 USOIL", "a:f:USOIL")] if any(
            r in it.rules for r in ("gold", "crude")) else [("🇮🇳 USDINR", "a:f:USDINR")]
    if watch:
        rows += _rows(watch[:6], 3)
    if it.link.startswith(("https://", "http://")):
        rows.append([("🔗 Read full article", it.link)])
    nav = [("⬅️ Back to list", f"n:p:{(n - 1) // news.PAGE + 1}")]
    if n < len(items):
        nav.append(("Next story ➡️", f"n:d:{n + 1}"))
    rows += [nav, [HOME]]
    telegram.send_text(news.detail_text(it, n, len(items), extra, take), buttons=rows)


def news_callback(rest: str, chat_id: str, message_id: int | None) -> None:
    sub, _, val = rest.partition(":")
    if sub == "c":                       # from a menu: new message
        cmd_news(chat_id, val or "all")
    elif sub == "t":                     # category tab on a news page: replace the page
        cmd_news(chat_id, val or "all", message_id)
    elif sub == "q":
        cmd_news(chat_id, f"q:{val.upper()}")
    elif sub == "r":
        entry = state.news(key_of(chat_id))
        cmd_news(chat_id, entry["cat"] if entry else "all", message_id)
    elif sub == "p":
        show_news_page(chat_id, int(val) if val.isdigit() else 1, message_id)
    elif sub == "d" and val.isdigit():
        news_detail(chat_id, int(val))


# ---------------------------------------------------------------- reference commands

def cmd_strategies() -> None:
    from bot.strategies import FAMILIES, LONGS, SHORTS

    lines = [f"📚 <b>THE {len(FAMILIES)} STRATEGIES</b> (each has a 🟢 BUY and a 🔴 SELL version)",
             "<i>Trend → 1, 2, 3, 4, 7, 8, 12, 13 · Range → 10, 11, 14 · Any → 5, 6, 9</i>"]
    for n, ((fam, title), lo, sh) in enumerate(zip(FAMILIES, LONGS, SHORTS), 1):
        extra = f"\n🚪 {html.escape(lo.exit_note)}" if lo.exit_note else ""
        lines.append(f"\n{report.num_label(n)} <b>{html.escape(title)}</b> · {lo.regime}\n🟢 {html.escape(lo.idea)}\n"
                     f"🔴 {html.escape(sh.idea)}{extra}")
    r = cfg()["risk"]
    lines.append("\n<b>Not on the chart list:</b> 🦅 option selling (iron condor) → 🧠 F&amp;O plan · "
                 "⚖️ pairs trading → /pairs")
    lines.append(f"\nStops from ATR, swings or the level; default take profit = {r['target_r']:g}× the risk.\n"
                 "🟢/🔴 ready = all 3 conditions met · 🟡 forming = 2 of 3 · ➖ not for this chart.\n"
                 f"Risk ladder: ${', $'.join(str(x) for x in r['ladder_usd'])} · "
                 f"₹{', ₹'.join(f'{x:,}' for x in r['ladder_inr'])} per trade · stop after "
                 f"{r.get('max_losing_trades_per_day', 3)} losing trades a day.\n"
                 "<i>Win rates quoted online are backtest ranges, not guarantees. Expectancy = win% × avg win − "
                 "loss% × avg loss: check each strategy's own history line.</i>")
    telegram.send_text("\n".join(lines), buttons=[[HOME]])


def cmd_settings() -> None:
    c = cfg()
    r = c["risk"]
    groups = sorted(allowed_chats() - {telegram_chat_id()} | approved_groups())
    telegram.send_text(
        "⚙️ <b>SETTINGS</b> (edit config.json on GitHub to change)\n"
        f"🪜 Risk ladder per trade: ${' · $'.join(str(x) for x in r['ladder_usd'])} (crypto, forex) · "
        f"₹{' · ₹'.join(f'{x:,}' for x in r['ladder_inr'])} (NSE, MCX)\n"
        f"🎯 Default take profit {r['target_r']:g}R · daily stop after {r.get('max_losing_trades_per_day', 3)} losses\n"
        f"⏱ Default timeframes: crypto {report.default_tf('crypto')} · NSE {report.default_tf('stock')} · "
        f"forex {report.default_tf('fx')}\n"
        f"🪙 Crypto: top {c['crypto']['top_n_by_volume']} by volume + {', '.join(c['crypto']['always_include'])}\n"
        f"📈 Stocks: {', '.join(c['stocks']['watchlist'])}\n"
        f"🏛 Indices: {', '.join(c['stocks'].get('indices', []))}\n"
        f"📊 Data: crypto Binance (live) · NSE/MCX {'Angel One (live)' if angel.configured() else 'Yahoo (delayed)'}\n"
        + "\n".join(fx.source_status()) + "\n"
        "🏢 Fundamentals: Yahoo Finance · 📰 News: Moneycontrol, ET, Mint, CoinDesk, Google News\n"
        f"💾 Paper trades saved in: {html.escape(store.describe())}\n"
        f"👥 Groups allowed: {len(groups)}"
        + (f" ({html.escape(', '.join(groups))})" if groups else " — add the bot to a group and send /allow there") + "\n"
        f"🤖 AI summary: {'on' if gemini_key() and gemini_model() else 'off'}",
        buttons=[[HOME]],
    )


# ---------------------------------------------------------------- routing

SYMBOL_CHARS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789&-./")


def _looks_like_symbol(token: str) -> bool:
    t = token.upper().removeprefix("$")
    return 1 <= len(t) <= 20 and set(t) <= SYMBOL_CHARS and any(ch.isalpha() for ch in t)


def handle(text: str, chat_id: str | None = None) -> None:
    chat_id = str(chat_id or telegram_chat_id())
    key = key_of(chat_id)
    raw = text.strip()
    parts = raw.split()
    if not parts:
        return
    cmd = parts[0].lower().split("@")[0]
    args = parts[1:]
    arg = " ".join(args)
    if cmd.startswith("/"):
        state.set_wait(key, None)
    if cmd in ("/start", "/menu"):
        telegram.send_text(WELCOME, buttons=MENU)
    elif cmd == "/help":
        telegram.send_text(HELP, buttons=MENU)
    elif cmd in ("/crypto", "/stocks", "/forex", "/fx"):
        _, tf = split_tf(args)
        market = {"/crypto": "crypto", "/stocks": "stocks"}.get(cmd, "fx")
        cmd_list(market, chat_id, tf)
    elif cmd == "/stock":
        rest, tf = split_tf(args)
        open_stock(" ".join(rest), "stock", tf) if rest else prompt(chat_id, "stock")
    elif cmd == "/search":
        cmd_search(arg) if arg else prompt(chat_id, "stock")
    elif cmd in ("/fundamentals", "/fundamental", "/fund"):
        open_stock(arg, "fund") if arg else prompt(chat_id, "fund")
    elif cmd == "/fno":
        open_stock(arg, "fno") if arg else prompt(chat_id, "fno")
    elif cmd in ("/greeks", "/greek"):
        open_stock(arg, "grk") if arg else prompt(chat_id, "grk")
    elif cmd in ("/plan", "/fnoplan"):
        if args and args[-1].lower() in PLAN_LABEL:
            sym, _ = symbols.find_stock(" ".join(args[:-1]))
            deliver_plan(sym or args[0].upper(), args[-1].lower())
        else:
            open_stock(arg, "fop") if arg else prompt(chat_id, "fop")
    elif cmd in ("/futures", "/future", "/fut"):
        open_stock(arg, "fut") if arg else prompt(chat_id, "fut")
    elif cmd == "/buildup":
        cmd_buildup()
    elif cmd == "/vix":
        cmd_vix()
    elif cmd == "/news":
        a = news.CAT_ALIASES.get(arg.lower(), arg.lower())
        cmd_news(chat_id, a if a in news.CATEGORIES else (f"q:{symbols.clean(arg)}" if arg else "all"))
    elif cmd == "/strategies":
        cmd_strategies()
    elif cmd == "/settings":
        cmd_settings()
    elif cmd == "/paper":
        cmd_paper(args)
    elif cmd == "/exit":
        if args and args[0].lstrip("#").isdigit():
            paper_exit(int(args[0].lstrip("#")))
        else:
            telegram.send_text("Which trade? Example: <code>/exit 3</code> (numbers are in /paper).")
    elif cmd == "/history":
        paper_callback("h", None)
    elif cmd == "/board":
        paper_callback("bd", None)
    elif cmd == "/pairs":
        cmd_pairs()
    elif cmd == "/pair":
        if len(args) >= 2:
            deliver_pair(symbols.clean(args[0]), symbols.clean(args[1]))
        else:
            cmd_pairs()
    elif cmd == "/analyze":
        rest, tf = split_tf(args)
        if rest:
            deliver(report.analyze_any(rest[0], tf))
        else:
            telegram.send_text("Which one? Example: <code>/analyze ETH 15m</code> or just send <code>ETH</code>.")
    elif not cmd.startswith("/") and state.wait_mode(key) == "pt" and paper.looks_like_input(raw):
        state.take_wait(key)
        paper_input(raw)                       # a number typed into the paper-trade ticket
    elif len(parts) == 1 and cmd.isdigit():
        pick_number(chat_id, int(cmd))
    elif cmd.startswith("/"):
        telegram.send_text("I don't know that command. Here's the menu 👇", buttons=MENU)
    else:
        mode = state.take_wait(key)
        rest, tf = split_tf(parts)
        if mode == "pt":
            state.set_wait(key, "pt")          # not a ticket value: a normal message; the ticket keeps waiting
            mode = None
        if mode:
            open_stock(" ".join(rest) or raw, mode, tf)
        elif len(rest) == 1 and _looks_like_symbol(rest[0]):
            quick_symbol(rest[0], tf)
        elif len(rest) <= 4 and all(p.replace("&", "").replace(".", "").isalnum() for p in rest):
            open_stock(" ".join(rest), "stock", tf)  # e.g. "tata motors": opens it if only one stock matches
        else:
            telegram.send_text("I didn't understand that. Here's the menu 👇", buttons=MENU)


def quick_symbol(token: str, tf: str | None = None) -> None:
    """A bare symbol: forex/commodity, then crypto, then NSE; if none exists, offer the closest NSE matches."""
    try:
        a = report.analyze_any(token, tf)
    except data.DataError:
        sym, matches = symbols.find_stock(token)
        if matches:
            show_matches(token, matches)
            return
        if sym and sym != symbols.clean(token):
            analyze_and_deliver("stock", sym, tf)
            return
        raise
    deliver(a)


def pick_number(chat_id: str, number: int) -> None:
    """A typed number refers to whichever numbered list was sent last: news or a scanner."""
    key = key_of(chat_id)
    if state.last_kind(key) == "news":
        news_detail(chat_id, number)
        return
    picked = state.pick(key, number)
    if picked is None:
        telegram.send_text("Send /crypto, /stocks or /forex first (or /news), then a number from that list.",
                           buttons=MENU)
        return
    market, symbol, tf = picked
    analyze_and_deliver(market, symbol, tf)


def _parse_asset_cb(rest: str) -> tuple[str, str, str | None]:
    code, _, tail = rest.partition(":")
    sym, _, tf = tail.partition(":")
    market = report.CODE_MARKET.get(code, "crypto")
    return market, sym, (data.norm_tf(tf) if tf else None)


def handle_callback(data_str: str, chat_id: str, message_id: int | None = None) -> None:
    kind, _, rest = data_str.partition(":")
    if kind == "cmd":
        handle(rest, chat_id)
    elif kind == "a":
        market, sym, tf = _parse_asset_cb(rest)
        analyze_and_deliver(market, sym, tf)
    elif kind in ("d", "st", "ob", "vx"):
        market, sym, tf = _parse_asset_cb(rest)
        deliver_details(market, sym, tf or report.default_tf(market), kind)
    elif kind == "fno":
        deliver_fno(rest)
    elif kind == "gk":
        deliver_greeks(rest)
    elif kind == "fp":
        name, _, which = rest.partition(":")
        deliver_plan(name, which or "auto")
    elif kind == "fu":
        deliver_futures(rest)
    elif kind == "fd":
        deliver_fundamentals(rest)
    elif kind == "fdd":
        deliver_fundamentals(rest, detailed=True)
    elif kind == "bu":
        cmd_buildup()
    elif kind == "vix":
        cmd_vix()
    elif kind == "pr":
        if rest:
            a, _, b = rest.partition(":")
            deliver_pair(a, b)
        else:
            cmd_pairs()
    elif kind == "pt":
        paper_callback(rest, message_id)
    elif kind == "m" and rest == "fx":
        fx_menu(chat_id)
    elif kind == "m" and rest in PROMPTS:
        prompt(chat_id, rest)
    elif kind == "n":
        news_callback(rest, chat_id, message_id)


def _safely(what: str, fn, *args) -> None:
    try:
        fn(*args)
    except data.DataError as exc:
        telegram.send_text(f"⚠️ Data problem: {html.escape(str(exc))}", buttons=[[HOME]])
    except angel.AngelError as exc:
        telegram.send_text(f"⚠️ Angel One: {html.escape(str(exc))}", buttons=[[HOME]])
    except paper.PaperError as exc:
        telegram.send_text(f"💼 {html.escape(str(exc))}", buttons=[[("💼 My paper trades", "pt:l"), HOME]])
    except store.StoreError as exc:
        telegram.send_text(f"💾 Storage problem: {html.escape(str(exc))}", buttons=[[HOME]])
    except Exception as exc:  # report, never crash the run
        log.error("%s failed: %s\n%s", what, exc, traceback.format_exc())
        telegram.send_text(f"⚠️ Something went wrong with “{html.escape(what[:50])}” "
                           f"({html.escape(exc.__class__.__name__)}). Check the logs (GitHub Actions, Cloud Run "
                           "or the PC window).", buttons=[[HOME]])


def safe_handle(text: str) -> None:
    _safely(text, handle, text)


# ---------------------------------------------------------------- who may use the bot

_GROUPS: dict = {}


def approved_groups() -> set[str]:
    hit = _GROUPS.get("g")
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    g = store.approved_groups()
    _GROUPS["g"] = (time.time(), g)
    return g


def authorized(chat_id: str, is_group: bool, user_id: str) -> bool:
    owner = telegram_chat_id()
    users = allowed_users()
    if not is_group:
        return chat_id == owner or user_id in users
    if chat_id not in allowed_chats() and chat_id not in approved_groups():
        return False
    return not users or user_id in users or user_id == owner


def _chat_info(chat: dict, user: dict) -> tuple[str, bool, str, str]:
    cid = str(chat.get("id"))
    ctype = chat.get("type") or ("group" if cid.startswith("-") else "private")
    name = " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x) or user.get("username") or "member"
    return cid, ctype in ("group", "supergroup"), str(user.get("id", cid)), name


def owner_command(text: str, chat_id: str, is_group: bool, user_id: str) -> bool:
    """/id anywhere; /allow and /deny in a group (owner only). Returns True if handled."""
    cmd = text.strip().split()[0].lower().split("@")[0] if text.strip() else ""
    owner = telegram_chat_id()
    if cmd == "/id":
        if user_id == owner or authorized(chat_id, is_group, user_id):
            telegram.send_text(f"🆔 Chat ID <code>{html.escape(chat_id)}</code> · your user ID "
                               f"<code>{html.escape(user_id)}</code>")
        return True
    if cmd in ("/allow", "/deny") and is_group:
        if user_id != owner:
            return True     # silently ignore: only the owner can approve a group
        store.set_group(chat_id, cmd == "/allow")
        _GROUPS.clear()
        if cmd == "/allow":
            telegram.send_text("✅ This group can now use SHIVAM AI TRADER. Members: send /start, or use commands "
                               "like <code>/analyze ETH</code>. Each member has their own lists and paper trades.",
                               buttons=MENU)
        else:
            telegram.send_text("🚫 This group can no longer use the bot.")
        return True
    return False


def wants_reply(msg: dict, text: str, key: str) -> tuple[bool, str]:
    """In groups the bot only answers commands, replies to its own messages, @mentions, a pending
    menu question, or a bare number right after one of its lists — never ordinary chat."""
    me = telegram.bot_username().lower()
    t = text.strip()
    if t.startswith("/"):
        first = t.split()[0]
        if "@" in first and me and first.split("@", 1)[1].lower() != me:
            return False, t           # a command for another bot
        return True, t
    if me and f"@{me}" in t.lower():
        import re
        return True, re.sub(rf"@{re.escape(me)}\b", "", t, flags=re.I).strip()
    reply = (msg.get("reply_to_message") or {}).get("from") or {}
    if reply.get("is_bot") and (not me or str(reply.get("username", "")).lower() == me):
        return True, t
    if state.pending_wait(key):
        if state.wait_mode(key) == "pt" and not paper.looks_like_input(t):
            return False, t           # ordinary group chat while a paper ticket is open: not for the bot
        return True, t
    if t.isdigit() and state.recent_list(key):
        return True, t
    return False, t


_TAPS: dict = {}                 # (chat, user, message, button) -> when it was last handled
DOUBLE_TAP_SECONDS = 3
QUICK_KINDS = ("pt", "n", "m")


def process_update(u: dict) -> None:
    """Handle one Telegram update (a message or a button tap). Used by GitHub runs, Cloud Run and the PC."""
    if "callback_query" in u:
        cb = u["callback_query"]
        msg = cb.get("message") or {}
        cid, is_group, uid, name = _chat_info(msg.get("chat") or {}, cb.get("from") or {})
        if not authorized(cid, is_group, uid):
            telegram.answer_callback(cb["id"], "This bot is private.")
            log.warning("Ignored a button tap from chat %s user %s.", cid, uid)
            return
        data_str = cb.get("data", "")
        tap = (cid, uid, msg.get("message_id"), data_str)
        if time.time() - _TAPS.get(tap, 0) < DOUBLE_TAP_SECONDS:
            telegram.answer_callback(cb["id"], "Already done ✔")
            log.info("Ignored a double tap: %s", data_str[:60])
            return
        quick = data_str.split(":")[0] in QUICK_KINDS          # screens that need no market data
        telegram.answer_callback(cb["id"], "" if quick else "Working on it…")
        log.info("Button: %s", data_str[:60])
        if data_str == "n:x":   # page indicator / section title: nothing to do
            return
        thread = msg.get("message_thread_id") if msg.get("is_topic_message") else None
        token = telegram.set_ctx(telegram.Ctx(cid, uid, name, is_group, None, thread))
        try:
            if not quick:
                telegram.chat_action(cid)
            _safely(data_str, handle_callback, data_str, cid, msg.get("message_id"))
        finally:
            telegram.reset_ctx(token)
            if len(_TAPS) > 300:
                _TAPS.clear()
            _TAPS[tap] = time.time()      # the same button tapped again while this ran arrives right after
        return
    msg = u.get("message") or {}
    text = msg.get("text") or ""
    if not text:
        return
    cid, is_group, uid, name = _chat_info(msg.get("chat") or {}, msg.get("from") or {})
    thread = msg.get("message_thread_id") if msg.get("is_topic_message") else None
    token = telegram.set_ctx(telegram.Ctx(cid, uid, name, is_group, msg.get("message_id") if is_group else None,
                                          thread))
    try:
        if owner_command(text, cid, is_group, uid):
            return
        if not authorized(cid, is_group, uid):
            log.warning("Ignored a message from chat %s user %s.", cid, uid)
            return
        if is_group:
            ok, text = wants_reply(msg, text, telegram.ctx().key)
            if not ok or not text:
                return
        log.info("Command: %s", text[:60])
        telegram.chat_action(cid)
        _safely(text, handle, text, cid)
    finally:
        telegram.reset_ctx(token)


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


# Same times as .github/workflows/alerts.yml (UTC): (market, weekdays only, hours, minute)
ALERT_SLOTS = [
    ("crypto", False, (0, 4, 8, 12, 16, 20), 7),
    ("stocks", True, (10,), 40),
    ("fno", True, (4,), 15),
    ("fno", True, (9,), 0),
]
ALERT_WINDOW_MIN = 30


def due_alerts(now, fired: set) -> list[str]:
    """Alert scans whose time has come in the last 30 minutes and haven't run yet (now is UTC)."""
    out = []
    for market, weekdays, hours, minute in ALERT_SLOTS:
        if weekdays and now.weekday() >= 5:
            continue
        for h in hours:
            slot = now.replace(hour=h, minute=minute, second=0, microsecond=0)
            key = f"{market}@{slot:%Y-%m-%d %H:%M}"
            if key not in fired and 0 <= (now - slot).total_seconds() < ALERT_WINDOW_MIN * 60:
                fired.add(key)
                out.append(market)
    return out


def check_paper_all() -> None:
    """PC/VM mode: close paper trades whose stop/target was hit and tell their owners."""
    try:
        doc = store.load()
    except store.StoreError:
        return
    chats = {t["chat_id"] for t in doc.get("paper", {}).get("trades", []) if t["status"] == "open"}
    for chat in chats:
        try:
            closed = paper.check_stops(chat)
        except Exception as exc:
            log.info("Paper check %s: %s", chat, exc)
            continue
        from bot import risk
        for c in closed:
            mention = f"<a href=\"tg://user?id={c['user_id']}\">{html.escape(c['user_name'])}</a>"
            if c["status"] == "open":
                text = (f"✅ {mention}: paper order #{c['id']} {html.escape(c['pretty'])} {c['side']} filled at "
                        f"{c['entry']:,.6g}")
            else:
                icon = {"target hit": "🎯", "liquidated": "💀"}.get(c["reason"], "🛑")
                text = (f"{icon} {mention}: paper trade #{c['id']} {html.escape(c['pretty'])} {c['side']} — "
                        f"{c['reason']} at {c['exit']:,.6g} → <b>{risk.money(c['pnl'], c['currency'], True)}</b> "
                        f"({c['pnl_pct']:+.2f}%)")
            telegram.send_text(text, chat, buttons=[[("💼 Paper trades", "pt:l")]])


def _confirm_quietly(last_update_id: int) -> None:
    try:
        telegram.confirm_updates(last_update_id)
    except telegram.TelegramError as exc:
        log.info("Confirm skipped: %s", exc)


def run_local(with_alerts: bool = True, max_loops: int | None = None) -> None:
    """Own PC or a cloud VM: answer messages instantly (long polling) and, optionally, send the
    scheduled alerts. Stop with Ctrl+C. Don't also run Cloud Run or GitHub's alerts, or you get doubles."""
    from datetime import datetime, timezone

    if not telegram_token() or not telegram_chat_id():
        sys.exit("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are missing: put them in the .env file")
    hook = telegram.webhook_info().get("url")
    if hook:
        sys.exit(f"Telegram is connected to a webhook ({hook}), so this PC can't receive messages.\n"
                 "Run:  python -m bot.main webhook delete   (then start this again)")
    telegram.set_commands()
    print("SHIVAM AI TRADER is running" + (" with alerts" if with_alerts else "") + ". Press Ctrl+C to stop.")
    if fx.mt5_ready():
        print("MetaTrader 5 connected: forex prices are live from your broker.")
    offset, fired, loops, last_paper = None, set(), 0, time.time()
    while max_loops is None or loops < max_loops:
        loops += 1
        try:
            updates = telegram.poll_updates(offset)
        except telegram.TelegramError as exc:
            clash = "conflict" in str(exc).lower()      # another getUpdates (e.g. GitHub's "Bot commands" check)
            log.warning("Telegram: %s (retrying in %s s)", exc, 1 if clash else 5)
            time.sleep(1 if clash else 5)
            continue
        if updates:
            offset = updates[-1]["update_id"] + 1
            # Tell Telegram right away that these are taken, so nothing else (GitHub's "Bot commands"
            # workflow) can pick the same messages up while this PC is still working on them.
            threading.Thread(target=_confirm_quietly, args=(updates[-1]["update_id"],), daemon=True).start()
        for u in updates:
            process_update(u)
        if with_alerts:
            for market in due_alerts(datetime.now(timezone.utc), fired):
                log.info("Scheduled alert scan: %s", market)
                _safely(f"alerts {market}", run_alerts, market)
        if time.time() - last_paper > 300:
            last_paper = time.time()
            check_paper_all()
        state.touch()


def alert_chats() -> list[str]:
    extra = [c.strip() for c in secret("ALERT_CHATS").split(",") if c.strip()]
    return [telegram_chat_id()] + [c for c in extra if c != telegram_chat_id()]


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
    for chat in alert_chats():
        token = telegram.set_ctx(telegram.Ctx(chat, chat, "", chat.startswith("-")))
        try:
            for a in fresh[:limit]:
                _safely(f"alert {a.symbol}", deliver, a, True)
            if len(fresh) > limit:
                telegram.send_text("🔔 More new signals: " + ", ".join(a.short for a in fresh[limit:])
                                   + "\nSend /crypto or /stocks to see the full list.")
        finally:
            telegram.reset_ctx(token)


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
        lines.append(f"✅ Crypto data OK (BTCUSDT {len(btc)} candles, live {btc.attrs.get('live_price', 0):,.2f})")
        m5 = data.crypto_candles("BTCUSDT", "5m", limit=300)
        lines.append(f"✅ Crypto 5m candles OK ({len(m5)})")
    except data.DataError as exc:
        lines.append(f"❌ Crypto data failed: {exc}")
    flow = data.crypto_orderflow("BTCUSDT", avg_vol)
    lines.append("✅ Crypto order book OK" if flow else "⚠️ Crypto order book unavailable (analysis still works)")
    try:
        rel = data.stock_candles(cfg()["stocks"]["watchlist"][0])
        lines.append(f"✅ Stock data OK ({len(rel)} daily candles, {rel.attrs.get('source', '')})")
        rel15 = data.stock_candles(cfg()["stocks"]["watchlist"][0], "15m")
        lines.append(f"✅ Stock 15m candles OK ({len(rel15)})")
    except data.DataError as exc:
        lines.append(f"❌ Stock data failed: {exc}")
    v = vol.india_vix()
    lines.append(f"✅ India VIX {v.value:.2f} ({v.label}, {v.source})" if v else "⚠️ India VIX unavailable")
    for code in ("EURUSD", "XAUUSD"):
        try:
            d = fx.candles(code, "1h")
            lines.append(f"✅ {code} OK ({len(d)} 1h candles, {d.attrs.get('source', '')})")
        except data.DataError as exc:
            lines.append(f"⚠️ {code}: {exc}")
    lines += fx.source_status()
    if angel.configured():
        try:
            a = angel.session()
            lines.append("✅ Angel One login OK")
            lines.append(f"✅ Instrument list OK ({len(a.inst):,} NSE/NFO/MCX/CDS contracts)")
            snap = angel.build_snapshot(a, "NIFTY", 3)
            oi_ok = any(l.oi for l in snap.legs)
            greeks_ok = any(l.gamma is not None for l in snap.legs)
            lines.append(f"✅ NIFTY live {snap.spot:,.2f}, expiry {snap.expiry:%d %b}, {len(snap.legs)} option legs")
            lines.append(("✅" if oi_ok else "⚠️") + f" Open interest {'found' if oi_ok else 'all zero'}")
            lines.append(("✅" if greeks_ok else "ℹ️") + " Greeks from Angel One "
                         + ("found (delta, gamma, theta, vega, IV)" if greeks_ok else
                            "empty (normal outside market hours; the bot computes them)"))
            for code in ("GOLD", "USDINR"):
                try:
                    d = fx.candles(code, "1h")
                    lines.append(f"✅ {code} OK ({d.attrs.get('source', '')})")
                except data.DataError as exc:
                    lines.append(f"⚠️ {code}: {exc}")
            try:
                rep = angel.build_futures(a, "NIFTY")
                lines.append(f"✅ Futures OK ({len(rep.legs)} NIFTY expiries, near {rep.legs[0].ltp:,.2f}); "
                             f"OI history {'found' if rep.oi_hist is not None else 'not returned'}; "
                             f"build-up {rep.legs[0].buildup or 'not in top lists'}")
            except angel.AngelError as exc:
                lines.append(f"⚠️ Futures: {exc}")
            lines.append("Field names seen (send these to Claude if anything shows ⚠️): "
                         + "; ".join(f"{k}: {', '.join(v)}" for k, v in a.last_keys.items()))
        except angel.AngelError as exc:
            lines.append(f"❌ Angel One: {exc}")
    else:
        lines.append("ℹ️ Angel One not configured: NSE data is delayed (Yahoo); F&O and MCX are off")
    working = [src for c, feeds in news.FEEDS.items() if c != "world" for src, url in feeds
               if news.fetch_feed(url, src, c)]
    total = sum(len(f) for c, f in news.FEEDS.items() if c != "world")
    lines.append(("✅" if working else "❌") + f" News feeds: {len(working)} of {total} working")
    try:
        f = fundamentals.fetch(cfg()["stocks"]["watchlist"][0])
        lines.append(f"✅ Fundamentals OK ({f.symbol}: {sum(len(r) for r in fundamentals.ratios(f).values())} ratios)")
    except data.DataError as exc:
        lines.append(f"⚠️ Fundamentals: {exc}")
    try:
        store.load()
        lines.append(f"✅ Storage OK: {store.describe()}")
    except store.StoreError as exc:
        lines.append(f"❌ Storage: {exc}")
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
        elif mode == "local":
            run_local(with_alerts="--no-alerts" not in argv)
        elif mode == "webhook":
            webhook(argv[2] if len(argv) > 2 else "info", argv[3] if len(argv) > 3 else "")
        else:
            print(__doc__)
            sys.exit(2)
    finally:
        state.touch()  # the workflow caches this file between runs


if __name__ == "__main__":
    main(sys.argv)

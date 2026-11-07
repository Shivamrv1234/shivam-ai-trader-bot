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

from bot import ai, angel, chart, data, fno, fundamentals, futures, news, report, state, symbols, telegram
from bot.config import cfg, gemini_key, gemini_model, telegram_chat_id, telegram_token

log = logging.getLogger("bot")

HOME = ("🏠 Menu", "cmd:/start")
MENU = [
    [("🪙 Crypto scan", "cmd:/crypto"), ("📈 NSE stocks scan", "cmd:/stocks")],
    [("🔎 Any NSE stock", "m:stock"), ("🏢 Fundamentals", "m:fund")],
    [("📊 Option chain", "m:fno"), ("📈 Futures", "m:fut")],
    [("📰 News & geopolitics", "n:c:all"), ("🔥 OI build-up", "bu")],
    [("📚 Strategies", "cmd:/strategies"), ("⚙️ Settings", "cmd:/settings"), ("❓ Help", "cmd:/help")],
]

WELCOME = (
    "🤖 <b>SHIVAM AI TRADER</b>\n"
    "Crypto · NSE stocks · F&amp;O · Futures · News\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "🪙 <b>Crypto scan</b> / 📈 <b>NSE scan</b> — numbered list of BUY/SELL setups; tap a number\n"
    "🔎 <b>Any NSE stock</b> — type a symbol or company name\n"
    "🏢 <b>Fundamentals</b> — valuation, profits, debt, growth\n"
    "📊 <b>Option chain</b> · 📈 <b>Futures</b> · 🔥 <b>OI build-up</b>\n"
    "📰 <b>News &amp; geopolitics</b> — 10 stories a page, each linked to the sectors it moves\n\n"
    "Tip: just send <code>ETH</code>, <code>RELIANCE</code>, <code>NIFTY</code> or <code>tata motors</code>.\n"
    "<i>For learning and paper trading — not financial advice.</i>"
)

HELP = (
    "❓ <b>HELP</b>\n━━━━━━━━━━━━━━━━━━\n"
    "<b>Scanners</b>\n"
    "/crypto · /stocks — numbered list; tap a number or send it (e.g. <code>2</code>)\n\n"
    "<b>Any NSE stock</b>\n"
    "/stock TATAMOTORS · /search tata · or just send <code>tata motors</code>\n"
    "You get: ① chart + trade card ② strategy check ③ full explanation, then buttons for "
    "📊 Option chain · 📈 Futures · 🏢 Fundamentals · 📰 News\n\n"
    "<b>Derivatives</b> (needs Angel One)\n"
    "/fno NIFTY — option chain, PCR, max pain, option trade idea\n"
    "/futures RELIANCE — all expiries, basis, carry, OI build-up, futures trade plan\n"
    "/buildup — today's long/short build-up across F&amp;O stocks\n\n"
    "<b>Company</b>\n/fundamentals INFY — valuation, profitability, debt, growth, quarters\n\n"
    "<b>News</b>\n/news · /news india · /news world · /news crypto · /news RELIANCE\n"
    "Each story has a serial number: send the number (e.g. <code>4</code>) for the full story.\n\n"
    "<b>Automatic alerts</b>\n"
    "Crypto after every 4h candle · stocks 16:10 IST weekdays · F&amp;O 09:45 &amp; 14:30 IST weekdays\n\n"
    "⏱ With Google Cloud Run, replies come in seconds. Without it, GitHub checks every 5 minutes."
)

PROMPTS = {
    "stock": ("🔎 <b>ANALYSE ANY NSE STOCK</b>\nType the NSE symbol or part of the company name — e.g. "
              "<code>TATAMOTORS</code>, <code>tata motors</code>, <code>M&amp;M</code> — or tap one below 👇", "a:s:"),
    "fund": ("🏢 <b>FUNDAMENTALS</b>\nType any NSE symbol or company name (e.g. <code>INFY</code>), "
             "or tap one below 👇", "fd:"),
    "fno": ("📊 <b>OPTION CHAIN</b>\nChoose an index, or type any F&amp;O stock (e.g. <code>RELIANCE</code>) 👇",
            "fno:"),
    "fut": ("📈 <b>FUTURES</b>\nChoose an index, or type any F&amp;O stock (e.g. <code>SBIN</code>) 👇", "fu:"),
}
INDEX_PICKS = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"]


def _rows(buttons: list, per_row: int = 3) -> list:
    return [buttons[k:k + per_row] for k in range(0, len(buttons), per_row)]


# ---------------------------------------------------------------- delivering one symbol

def action_buttons(a: report.Analysis) -> list:
    """Buttons under the explanation: what to look at next for this symbol."""
    sym = a.symbol
    rows = []
    if a.market == "stock":
        if symbols.has_fno(sym):
            rows.append([("📊 Option chain", f"fno:{sym}"), ("📈 Futures", f"fu:{sym}")])
        if a.is_index:
            rows.append([("📰 Market news", "n:c:india"), ("🔥 OI build-up", "bu")])
        else:
            rows.append([("🏢 Fundamentals", f"fd:{sym}"), ("📰 News", f"n:q:{sym}")])
        back = "cmd:/stocks"
    else:
        rows.append([("📰 Crypto news", "n:c:crypto")])
        back = "cmd:/crypto"
    code = "c" if a.market == "crypto" else "s"
    rows.append([("🔄 Refresh", f"a:{code}:{sym}"), ("⬅️ Back to list", back), HOME])
    return rows


def deliver(a: report.Analysis, alert: bool = False) -> None:
    """Three messages: chart + trade card, strategy check, full explanation (+ next-step buttons)."""
    best = a.best()
    title = f"{a.pretty} · {a.timeframe}"
    png = chart.render(a.x, title, best, a.snap)
    caption = report.card(a)
    if alert:
        caption = ("🔔 <b>NEW SIGNAL</b>\n" + caption)[:1020]
    telegram.send_photo(png, caption)
    telegram.send_text(report.strategy_list(a))
    summary = ai.summarize(report.facts(a))
    telegram.send_text(report.explanation(a, summary), buttons=action_buttons(a))


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
                           + html.escape(", ".join(errors)), buttons=[[HOME]])
        return
    ordered = report.ordered(results)
    state.remember_list(chat_id, "crypto" if market == "crypto" else "stock", [a.symbol for a in ordered])
    buttons = report.list_buttons(results)
    extra = [("🔎 Any NSE stock", "m:stock"), HOME] if market != "crypto" else [("📰 Crypto news", "n:c:crypto"), HOME]
    telegram.send_text(report.list_text(results, market, errors), buttons=buttons + [extra])


# ---------------------------------------------------------------- any NSE stock

def prompt(chat_id: str, mode: str) -> None:
    """Menu button: remember what the next typed text means, and offer quick picks."""
    state.set_wait(chat_id, mode)
    text, prefix = PROMPTS[mode]
    picks = list(cfg()["stocks"]["watchlist"])
    if mode in ("fno", "fut"):
        if not angel.configured():
            telegram.send_text("📊 Option chain and futures need your Angel One SmartAPI secrets "
                               "(see the Setup Guide, part 5).", buttons=[[HOME]])
            state.set_wait(chat_id, None)
            return
        picks = INDEX_PICKS + picks[:5]
    rows = _rows([(p, prefix + p) for p in picks])
    if mode == "fut":
        rows.append([("🔥 Today's OI build-up", "bu")])
    rows.append([HOME])
    telegram.send_text(text, buttons=rows)


def open_stock(query: str, target: str) -> None:
    """Resolve what the user typed to one NSE symbol, then run the target screen."""
    sym, matches = symbols.find_stock(query)
    if sym:
        run_target(target, sym)
    elif matches:
        show_matches(query, matches, target)
    else:
        telegram.send_text(f"🔎 Nothing on NSE matches “{html.escape(query[:40])}”. Try part of the symbol, "
                           "e.g. <code>/search tata</code>.", buttons=[[("🔎 Try again", "m:stock"), HOME]])


def run_target(target: str, sym: str) -> None:
    if target == "fund":
        deliver_fundamentals(sym)
    elif target == "fut":
        deliver_futures(sym)
    elif target == "fno":
        deliver_fno(sym)
    else:
        deliver(report.analyze(sym, "stock", with_flow=True))


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

def deliver_fundamentals(sym: str) -> None:
    sym = sym.upper()
    if sym in angel.INDEX_ALIASES:
        telegram.send_text("🏢 Indices have no company fundamentals. Pick a stock instead.",
                           buttons=[[("🏢 Choose a stock", "m:fund"), HOME]])
        return
    f = fundamentals.fetch(sym)
    rows = [[("📈 Technical analysis", f"a:s:{sym}"), ("📰 News", f"n:q:{sym}")]]
    if symbols.has_fno(sym):
        rows.append([("📊 Option chain", f"fno:{sym}"), ("📈 Futures", f"fu:{sym}")])
    rows.append([("🔄 Refresh", f"fd:{sym}"), HOME])
    telegram.send_text(fundamentals.report_text(f), buttons=rows)


# ---------------------------------------------------------------- F&O and futures

NEED_ANGEL = "📊 F&amp;O and futures need your Angel One SmartAPI secrets (see the Setup Guide, part 5)."


def deliver_fno(name: str) -> None:
    if not angel.configured():
        telegram.send_text(NEED_ANGEL, buttons=[[HOME]])
        return
    try:
        snap = angel.build_snapshot(angel.session(), name, cfg()["fno"]["strikes_each_side"])
    except angel.AngelError as exc:
        telegram.send_text(f"⚠️ F&amp;O data problem: {html.escape(str(exc))}", buttons=[[HOME]])
        return
    telegram.send_photo(fno.oi_chart(snap), f"📊 <b>{html.escape(snap.name)} · F&amp;O</b>\n"
                                            f"Spot {snap.spot:,.2f} · PCR {snap.pcr} · expiry {snap.expiry:%d %b}")
    text = fno.summary_text(snap)
    try:
        a = report.analyze(snap.name, "stock")
        text += "\n\n" + fno.option_idea(snap, a)
    except data.DataError as exc:
        text += f"\n\n(Option idea unavailable: {html.escape(str(exc))})"
    telegram.send_text(text, buttons=[[("📈 Futures", f"fu:{snap.name}"), ("📉 Chart analysis", f"a:s:{snap.name}")],
                                      [("🔄 Refresh", f"fno:{snap.name}"), HOME]])


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
        text += "\n\n" + futures.trade_plan(rep, report.analyze(rep.name, "stock"))
    except data.DataError as exc:
        text += f"\n\n(Trade plan unavailable: {html.escape(str(exc))})"
    telegram.send_text(text, buttons=[[("📊 Option chain", f"fno:{rep.name}"), ("📉 Chart analysis", f"a:s:{rep.name}")],
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
    rows.append([(("• " if c == cat else "") + label, f"n:t:{c}") for c, label in news.CATEGORIES.items()])
    rows.append([("🔄 Refresh", "n:r"), HOME])
    return rows


def cmd_news(chat_id: str, cat: str = "all", message_id: int | None = None) -> None:
    """cat: all / india / world / crypto, or 'q:SYMBOL' for one stock's news."""
    query = cat[2:] if cat.startswith("q:") else None
    items = news.collect("all" if query else cat, query=query)
    state.remember_news(chat_id, cat, [i.to_dict() for i in items], 1)
    show_news_page(chat_id, 1, message_id)


def show_news_page(chat_id: str, page: int, message_id: int | None = None) -> None:
    entry = state.news(chat_id)
    if not entry:
        cmd_news(chat_id, "all", message_id)
        return
    items = [news.Item.from_dict(d) for d in entry["items"]]
    page = min(max(page, 1), news.pages(items))
    state.set_news_page(chat_id, page)
    cat = entry["cat"]
    telegram.show(news.page_text(items, page, cat, _news_title(cat)), chat_id,
                  news_buttons(len(items), page, cat), message_id)


def news_detail(chat_id: str, n: int) -> None:
    entry = state.news(chat_id)
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
    if watch:
        rows += _rows(watch, 3)
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
        entry = state.news(chat_id)
        cmd_news(chat_id, entry["cat"] if entry else "all", message_id)
    elif sub == "p":
        show_news_page(chat_id, int(val) if val.isdigit() else 1, message_id)
    elif sub == "d" and val.isdigit():
        news_detail(chat_id, int(val))


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
    telegram.send_text("\n".join(lines), buttons=[[HOME]])


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
        "🏢 Fundamentals: Yahoo Finance · 📰 News: public RSS feeds\n"
        f"🤖 AI summary: {'on' if gemini_key() and gemini_model() else 'off'}",
        buttons=[[HOME]],
    )


# ---------------------------------------------------------------- routing

SYMBOL_CHARS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789&-.")


def _looks_like_symbol(token: str) -> bool:
    t = token.upper().removeprefix("$")
    return 1 <= len(t) <= 20 and set(t) <= SYMBOL_CHARS and any(ch.isalpha() for ch in t)


def handle(text: str, chat_id: str | None = None) -> None:
    chat_id = chat_id or telegram_chat_id()
    raw = text.strip()
    parts = raw.split()
    if not parts:
        return
    cmd = parts[0].lower().split("@")[0]
    arg = " ".join(parts[1:])
    if cmd.startswith("/"):
        state.set_wait(chat_id, None)
    if cmd in ("/start", "/menu"):
        telegram.send_text(WELCOME, buttons=MENU)
    elif cmd == "/help":
        telegram.send_text(HELP, buttons=MENU)
    elif cmd == "/crypto":
        cmd_list("crypto", chat_id)
    elif cmd == "/stocks":
        cmd_list("stocks", chat_id)
    elif cmd == "/stock":
        open_stock(arg, "stock") if arg else prompt(chat_id, "stock")
    elif cmd == "/search":
        cmd_search(arg) if arg else prompt(chat_id, "stock")
    elif cmd in ("/fundamentals", "/fundamental", "/fund"):
        open_stock(arg, "fund") if arg else prompt(chat_id, "fund")
    elif cmd == "/fno":
        open_stock(arg, "fno") if arg else prompt(chat_id, "fno")
    elif cmd in ("/futures", "/future", "/fut"):
        open_stock(arg, "fut") if arg else prompt(chat_id, "fut")
    elif cmd == "/buildup":
        cmd_buildup()
    elif cmd == "/news":
        a = arg.lower()
        cmd_news(chat_id, a if a in news.CATEGORIES else (f"q:{symbols.clean(arg)}" if arg else "all"))
    elif cmd == "/strategies":
        cmd_strategies()
    elif cmd == "/settings":
        cmd_settings()
    elif cmd == "/analyze":
        if arg:
            deliver(report.analyze_any(parts[1]))
        else:
            telegram.send_text("Which one? Example: <code>/analyze ETH</code> or just send <code>ETH</code>.")
    elif len(parts) == 1 and cmd.isdigit():
        pick_number(chat_id, int(cmd))
    elif cmd.startswith("/"):
        telegram.send_text("I don't know that command. Here's the menu 👇", buttons=MENU)
    else:
        mode = state.take_wait(chat_id)
        if mode:
            open_stock(raw, mode)
        elif len(parts) == 1 and _looks_like_symbol(parts[0]):
            quick_symbol(parts[0])
        elif len(parts) <= 4 and all(p.replace("&", "").replace(".", "").isalnum() for p in parts):
            open_stock(raw, "stock")  # e.g. "tata motors": opens it if only one stock matches
        else:
            telegram.send_text("I didn't understand that. Here's the menu 👇", buttons=MENU)


def quick_symbol(token: str) -> None:
    """A bare symbol: crypto first, then NSE; if neither exists, offer the closest NSE matches."""
    try:
        a = report.analyze_any(token)
    except data.DataError:
        sym, matches = symbols.find_stock(token)
        if matches:
            show_matches(token, matches)
            return
        if sym and sym != symbols.clean(token):
            deliver(report.analyze(sym, "stock", with_flow=True))
            return
        raise
    deliver(a)


def pick_number(chat_id: str, number: int) -> None:
    """A typed number refers to whichever numbered list was sent last: news or a scanner."""
    if state.last_kind(chat_id) == "news":
        news_detail(chat_id, number)
        return
    picked = state.pick(chat_id, number)
    if picked is None:
        telegram.send_text("Send /crypto or /stocks first (or /news), then a number from that list.", buttons=MENU)
        return
    market, symbol = picked
    deliver(report.analyze(symbol, market, with_flow=True))


def handle_callback(data_str: str, chat_id: str, message_id: int | None = None) -> None:
    kind, _, rest = data_str.partition(":")
    if kind == "cmd":
        handle(rest, chat_id)
    elif kind == "a":
        code, _, symbol = rest.partition(":")
        deliver(report.analyze(symbol, "crypto" if code == "c" else "stock", with_flow=True))
    elif kind == "fno":
        deliver_fno(rest)
    elif kind == "fu":
        deliver_futures(rest)
    elif kind == "fd":
        deliver_fundamentals(rest)
    elif kind == "bu":
        cmd_buildup()
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
    except Exception as exc:  # report, never crash the run
        log.error("%s failed: %s\n%s", what, exc, traceback.format_exc())
        telegram.send_text(f"⚠️ Something went wrong with “{html.escape(what[:50])}” "
                           f"({html.escape(exc.__class__.__name__)}). Check the logs (GitHub Actions or "
                           "Cloud Run).", buttons=[[HOME]])


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
        if cb.get("data") == "n:x":   # page indicator: nothing to do
            return
        telegram.chat_action(allowed)
        mid = (cb.get("message") or {}).get("message_id")
        _safely(cb.get("data", ""), handle_callback, cb.get("data", ""), allowed, mid)
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
        lines.append("ℹ️ Angel One not configured: NSE data is delayed (Yahoo) and F&O is off")
    working = [src for c, feeds in news.FEEDS.items() for src, url in feeds if news.fetch_feed(url, src, c)]
    total = sum(len(f) for f in news.FEEDS.values())
    lines.append(("✅" if working else "❌") + f" News feeds: {len(working)} of {total} working")
    try:
        f = fundamentals.fetch(cfg()["stocks"]["watchlist"][0])
        lines.append(f"✅ Fundamentals OK ({f.symbol}: {len(f.checks)} health checks from Yahoo)")
    except data.DataError as exc:
        lines.append(f"⚠️ Fundamentals: {exc}")
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

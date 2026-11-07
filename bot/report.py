"""Turn candles into a multi-strategy analysis and the Telegram messages.

Message layout for one symbol:
  1. Chart photo + "trade card" caption: BUY / SELL / no setup, entry, take profit, stop-loss, size
  2. Strategy check: the 7 strategies with 🟢 / 🟡 / ⚪ lights
  3. Full explanation: every condition, history, market structure, order book, AI summary
"""
from __future__ import annotations

import html
import math
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from bot import backtest, data, strategies
from bot.config import capital, cfg, cost_pct

MIN_BARS = 220  # enough for EMA200
LINE = "━━━━━━━━━━━━━━━━━━"
THIN = "────────────"
KEYCAP = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
SIDE_ICON = {"BUY": "🟢", "SELL": "🔴"}
RANK = {"TRADE": 2, "WATCH": 1, "NO": 0}


@dataclass
class Analysis:
    symbol: str
    market: str           # crypto | stock
    timeframe: str
    x: pd.DataFrame
    setups: list = field(default_factory=list)
    change_pct: float | None = None
    source: str = ""
    live_price: float | None = None
    snap: object = None          # structure.Snapshot
    flow: dict | None = None     # live volume + order book

    @property
    def price(self) -> float:
        return float(self.x["close"].iloc[-1])

    @property
    def trades(self) -> list:
        return [s for s in self.setups if s.status == "TRADE"]

    @property
    def watches(self) -> list:
        return [s for s in self.setups if s.status == "WATCH"]

    def side_trades(self, side: str) -> list:
        return [s for s in self.trades if s.side == side]

    def family_view(self, family: str):
        """The setup to show for a strategy: the side that is further along (BUY wins a tie)."""
        pair = [s for s in self.setups if s.family == family]
        return max(pair, key=lambda s: (RANK[s.status], s.passed, s.side == "BUY"))

    @property
    def direction(self) -> str:
        """BUY, SELL, MIXED, FORMING or NONE."""
        b, s = len(self.side_trades("BUY")), len(self.side_trades("SELL"))
        if b > s:
            return "BUY"
        if s > b:
            return "SELL"
        if b:
            return "MIXED"
        return "FORMING" if self.watches else "NONE"

    @property
    def agree(self) -> int:
        d = self.direction
        return len(self.side_trades(d)) if d in ("BUY", "SELL") else 0

    @property
    def score(self) -> int:
        return 3 * max(len(self.side_trades("BUY")), len(self.side_trades("SELL"))) + len(self.watches)

    @property
    def forming_side(self) -> str | None:
        w = self.watches
        if not w:
            return None
        b = sum(s.side == "BUY" for s in w)
        return "BUY" if b >= len(w) - b else "SELL"

    def best(self):
        """The setup whose levels go on the trade card and chart."""
        def quality(s):
            r = s.backtest.get("avg_r")
            return r if r is not None else -99
        d = self.direction
        if d in ("BUY", "SELL"):
            group = [s for s in self.side_trades(d) if s.entry is not None]
        elif d == "FORMING":
            group = [s for s in self.watches if s.side == self.forming_side and s.entry is not None]
        else:
            group = []
        return max(group, key=quality) if group else None

    @property
    def is_index(self) -> bool:
        from bot.angel import INDEX_ALIASES
        return self.market == "stock" and self.symbol in INDEX_ALIASES

    @property
    def pretty(self) -> str:
        if self.market == "crypto" and self.symbol.endswith("USDT"):
            return f"{self.symbol[:-4]}/USDT"
        return self.symbol

    @property
    def short(self) -> str:
        return self.symbol[:-4] if self.market == "crypto" and self.symbol.endswith("USDT") else self.symbol


def analyze(symbol: str, market: str, candles: pd.DataFrame | None = None,
            change_pct: float | None = None, with_flow: bool = False) -> Analysis:
    live = candles is None
    if candles is None:
        candles = data.crypto_candles(symbol) if market == "crypto" else data.stock_candles(symbol)
    if len(candles) < MIN_BARS:
        raise data.DataError(f"{symbol}: only {len(candles)} candles, need {MIN_BARS}")
    attrs = dict(candles.attrs)
    x, snap = strategies.prepare(candles)
    tf = cfg()["crypto"]["timeframe"] if market == "crypto" else "1d"
    name = data.norm_crypto(symbol) if market == "crypto" else symbol.upper()
    source = attrs.get("source") or ("Binance (live)" if market == "crypto" else "")
    if market == "stock" and change_pct is None:
        change_pct = attrs.get("live_change_pct")
    a = Analysis(name, market, tf, x, change_pct=change_pct, source=source,
                 live_price=attrs.get("live_price"), snap=snap)
    if live and with_flow:
        a.flow = fetch_flow(name, market, float(x["vol_avg20"].iloc[-1]))
    for strat in strategies.ALL:
        s = strategies.evaluate(strat, x)
        s.backtest = backtest.run(strat, x, market)
        a.setups.append(s)
    return a


def fetch_flow(name: str, market: str, avg_volume: float) -> dict | None:
    if market == "crypto":
        return data.crypto_orderflow(name, avg_volume)
    from bot import angel

    if not angel.configured():
        return None
    try:
        return angel.orderflow(angel.session(), name, avg_volume)
    except angel.AngelError:
        return None


def resolve(arg: str) -> tuple[str, str]:
    """'SOL' / 'solusdt' -> crypto; 'RELIANCE', a watchlist name or an index -> stock."""
    a = arg.upper().strip().removeprefix("$").removeprefix("/")
    from bot.angel import INDEX_ALIASES

    if a.endswith(".NS") or a in cfg()["stocks"]["watchlist"] or a in INDEX_ALIASES:
        return a.removesuffix(".NS"), "stock"
    return a, "crypto"


def analyze_any(arg: str) -> Analysis:
    sym, market = resolve(arg)
    try:
        return analyze(sym, market, with_flow=True)
    except data.DataError:
        if market == "crypto":  # maybe it's an NSE stock not in the watchlist
            return analyze(sym, "stock", with_flow=True)
        raise


# ---------------------------------------------------------------- small helpers

def _num(v: float) -> str:
    return strategies._fmt(v)


def _pct(a: float, b: float) -> str:
    return f"{(b - a) / a * 100:+.2f}%" if a else ""


def _now() -> str:
    return datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%d %b, %H:%M IST")


def _candle_time(a: Analysis) -> str:
    return a.x.index[-1].tz_convert("Asia/Kolkata").strftime("%d %b %H:%M IST")


def size_info(s, market: str, unit: str = "") -> tuple[str, str] | None:
    """(quantity text, max-loss text) for the configured capital and risk %, or None if too small."""
    cap, cur = capital(market)
    risk_pct = cfg()["risk"]["risk_per_trade_pct"]
    unit_risk = abs(s.entry - s.stop) + s.entry * cost_pct(market) / 100 * 2
    qty = min(cap * risk_pct / 100 / unit_risk, cap / s.entry)
    if market == "stock":
        qty = math.floor(qty)
        if qty < 1:
            return None
        qty_txt = f"{qty} share{'s' if qty > 1 else ''}"
    else:
        qty_txt = f"{qty:.4g} {unit}".strip()
    return qty_txt, f"{qty * unit_risk:,.2f} {cur}"


def position_size(s, market: str, unit: str = "") -> str:
    cap, cur = capital(market)
    risk_pct = cfg()["risk"]["risk_per_trade_pct"]
    info = size_info(s, market, unit)
    if info is None:
        return f"Size: 1 share risks more than {risk_pct}% of {cap:,.0f} {cur} — skip, or raise capital."
    return f"Size for {cap:,.0f} {cur} at {risk_pct}% risk: {info[0]} (max loss ≈ {info[1]})"


def _bt_line(s) -> str:
    b = s.backtest
    if not b.get("trades"):
        return f"📈 History ({b.get('from')} – {b.get('to')}): no completed trades yet"
    pf = b["profit_factor"] if b["profit_factor"] is not None else "n/a"
    line = (f"📈 History ({b['from']} – {b['to']}): {b['trades']} trades · win {b['win_rate']}% · "
            f"avg {b['avg_r']:+}R · PF {pf}")
    if not b["enough_data"]:
        line += " · ⚠️ few trades, weak evidence"
    return line


def verdict(a: Analysis) -> str:
    d = a.direction
    b, s = len(a.side_trades("BUY")), len(a.side_trades("SELL"))
    if d in ("BUY", "SELL"):
        return f"{d} — {a.agree} of 7 strategies agree"
    if d == "MIXED":
        return f"MIXED — {b} BUY vs {s} SELL"
    if d == "FORMING":
        return f"FORMING ({a.forming_side}) — {len(a.watches)} setup(s) need one more condition"
    return "NO SETUP"


# ---------------------------------------------------------------- message 1: trade card

def card(a: Analysis) -> str:
    """Caption for the chart photo (Telegram limit 1024 characters)."""
    icon = "🪙" if a.market == "crypto" else ("🏛" if a.is_index else "📈")
    unit = "USDT" if a.market == "crypto" else "₹"
    chg_lbl = "24h" if a.market == "crypto" else "today"
    price = a.live_price or a.price
    price_txt = f"{_num(price)} {unit}" if a.market == "crypto" else f"₹{_num(price)}"
    chg = f" · {chg_lbl} {a.change_pct:+.2f}%" if a.change_pct is not None else ""
    lines = [f"{icon} <b>{html.escape(a.pretty)}</b> · {a.timeframe}", f"Price <b>{price_txt}</b>{chg}", ""]
    d, best = a.direction, a.best()

    if d in ("BUY", "SELL") and best:
        lines.append(f"{SIDE_ICON[d]} <b>{d} SETUP</b> · {a.agree} of 7 strategies agree")
    elif d == "FORMING" and best:
        lines.append(f"🟡 <b>{best.side} SETUP FORMING</b> · not confirmed yet")
    elif d == "MIXED":
        lines.append(f"⚖️ <b>MIXED SIGNALS</b> · {len(a.side_trades('BUY'))} BUY vs "
                     f"{len(a.side_trades('SELL'))} SELL → no trade")
    else:
        lines.append("⚪ <b>NO SETUP RIGHT NOW</b>")

    if best and d in ("BUY", "SELL", "FORMING"):
        prefix = "" if d != "FORMING" else "If it triggers: "
        lines += [LINE,
                  f"{prefix}🎯 Entry <b>{_num(best.entry)}</b>",
                  f"💰 Take profit <b>{_num(best.target)}</b> ({_pct(best.entry, best.target)})",
                  f"🛑 Stop-loss <b>{_num(best.stop)}</b> ({_pct(best.entry, best.stop)})",
                  f"⚖️ Reward : Risk = {cfg()['risk']['target_r']:g} : 1"]
        if a.is_index:
            lines.append(f"📦 Index — trade it via F&amp;O: /fno {a.symbol}")
        else:
            info = size_info(best, a.market, a.short)
            lines.append(f"📦 Size {info[0]} · max loss ≈ {info[1]}" if info else
                         "📦 1 share risks more than your limit — skip")
        lines += [LINE, f"Levels from: {html.escape(best.name)}"]
        bt = best.backtest
        if bt.get("trades"):
            lines.append(f"History: {bt['trades']} trades · win {bt['win_rate']}% · avg {bt['avg_r']:+}R")
    else:
        sn = a.snap
        if sn is not None and (sn.supports or sn.resistances):
            lines.append(LINE)
            if sn.resistances:
                lines.append(f"🔺 Resistance {_num(sn.resistances[0][0])}")
            if sn.supports:
                lines.append(f"🔻 Support {_num(sn.supports[0][0])}")
        lines.append("Wait for a setup — see the strategy check below.")

    if a.flow:
        lines.append(f"📊 Order book: {data.flow_verdict(a.flow)}")
    if d == "SELL" or (d == "FORMING" and best and best.side == "SELL"):
        lines.append("<i>SELL = exit/avoid if you hold it. Shorting needs futures/options"
                     + (" (crypto: futures)" if a.market == "crypto" else "") + ".</i>")
    lines.append(f"<i>Candle closed {_candle_time(a)} · {html.escape(a.source or 'data')}</i>")
    text = "\n".join(lines)
    return text[:1020]


# ---------------------------------------------------------------- message 2: strategy check

def strategy_list(a: Analysis) -> str:
    lines = [f"📋 <b>STRATEGY CHECK</b> · {html.escape(a.pretty)}", LINE]
    for n, (fam, title) in enumerate(strategies.FAMILIES):
        s = a.family_view(fam)
        if s.status == "TRADE":
            state = f"🟢 {html.escape(title)} — <b>{s.side} setup</b>" + (" ✨ new" if s.fresh else "")
        elif s.status == "WATCH":
            state = f"🟡 {html.escape(title)} — forming {s.side} ({s.passed}/3)"
        else:
            state = f"⚪ {html.escape(title)} — no setup"
        lines.append(f"{KEYCAP[n]} {state}")
    lines += [LINE, "🟢 ready · 🟡 forming · ⚪ none · ✨ new this candle",
              f"<b>Verdict:</b> {html.escape(verdict(a))}", "Full explanation below 👇"]
    return "\n".join(lines)


# ---------------------------------------------------------------- message 3: explanation

def _strategy_block(n: int, title: str, s, a: Analysis) -> str:
    if s.status == "TRADE":
        tag = f"🟢 {s.side} setup" + (" (new this candle)" if s.fresh else "")
    elif s.status == "WATCH":
        tag = f"🟡 forming {s.side} ({s.passed}/3)"
    else:
        tag = f"⚪ no setup (closest: {s.side} {s.passed}/3)"
    lines = [f"<b>{n}. {html.escape(title)}</b> · {tag}", f"<i>{html.escape(s.idea)}</i>"]
    lines += [f"{'✅' if ok else '❌'} {html.escape(t)}" for ok, t in s.checks]
    if s.entry is not None and s.status in ("TRADE", "WATCH"):
        prefix = "" if s.status == "TRADE" else "If it triggers: "
        lines.append(f"{prefix}🎯 {_num(s.entry)} · 💰 {_num(s.target)} · 🛑 {_num(s.stop)}")
        if not a.is_index:
            lines.append("📦 " + position_size(s, a.market, a.short))
    if s.status in ("TRADE", "WATCH"):
        lines.append(_bt_line(s))
    return "\n".join(lines)


def structure_text(a: Analysis) -> str:
    from bot.structure import ZONE_NAMES, line_value

    sn = a.snap
    if sn is None:
        return ""
    st = {1: "bullish (higher highs / higher lows)", -1: "bearish (lower highs / lower lows)"}.get(
        sn.structure, "not established yet")
    lines = ["🧭 <b>MARKET STRUCTURE &amp; KEY LEVELS</b>", f"Structure: {st}"]
    if sn.breaks:
        i, lvl, kind, d = sn.breaks[-1]
        ago = len(a.x) - 1 - i
        lines.append(f"Last break: {'bullish' if d > 0 else 'bearish'} {kind} through {_num(lvl)}, {ago} candle(s) ago")
    if sn.swings:
        lines.append("Recent swings: " + " → ".join(lab for _, _, lab in sn.swings[-6:]))
    if sn.resistances:
        lines.append("🔺 Resistance: " + ", ".join(f"{_num(p)} ({t}×)" for p, t in sn.resistances))
    if sn.supports:
        lines.append("🔻 Support: " + ", ".join(f"{_num(p)} ({t}×)" for p, t in sn.supports))
    if sn.round_levels:
        lines.append("🔘 Round numbers: " + ", ".join(_num(v) for v in sn.round_levels))
    n = len(a.x) - 1
    if sn.tl_down:
        lines.append(f"📉 Falling trend line now at {_num(line_value(sn.tl_down, n))} (break above = bullish)")
    if sn.tl_up:
        lines.append(f"📈 Rising trend line now at {_num(line_value(sn.tl_up, n))} (break below = bearish)")
    for z in sorted(sn.zones, key=lambda z: -z.top):
        where = "above price" if z.bot > a.price else ("below price" if z.top < a.price else "price inside")
        lines.append(f"▫️ {ZONE_NAMES[z.kind].capitalize()}: {_num(z.bot)}–{_num(z.top)} ({where}, tested {z.tests}×)")
    return "\n".join(lines)


def flow_text(a: Analysis) -> str:
    f = a.flow
    head = "📊 <b>LIVE VOLUME &amp; ORDER BOOK</b>"
    if not f:
        if a.market == "stock":
            return f"{head}\nNot available (needs Angel One; NSE order book exists only in market hours)."
        return f"{head}\nNot available right now."
    unit = f.get("unit", "")
    lines = [f"{head} · {html.escape(f.get('venue', ''))}", f"Reading: <b>{data.flow_verdict(f)}</b>"]
    if f.get("imbalance") is not None:
        if "tot_buy_qty" in f:
            lines.append(f"Pending buy vs sell: {f['imbalance']:.2f}× ({f['tot_buy_qty']:,.0f} vs {f['tot_sell_qty']:,.0f})")
        else:
            lines.append(f"Bids vs asks within ±1%: {f['imbalance']:.2f}× "
                         f"({f['bid_value_1pct']:,.0f} vs {f['ask_value_1pct']:,.0f} {unit})")
    if f.get("bid_wall"):
        p, q = f["bid_wall"]
        lines.append(f"🧱 Biggest buy wall: {_num(p)} (qty {q:,.4g})")
    if f.get("ask_wall"):
        p, q = f["ask_wall"]
        lines.append(f"🧱 Biggest sell wall: {_num(p)} (qty {q:,.4g})")
    if f.get("spread_pct") is not None:
        lines.append(f"Spread: {f['spread_pct']:.3f}%")
    if f.get("taker_buy_pct") is not None:
        lines.append(f"Last candle: {f['taker_buy_pct']:.0f}% aggressive buying")
    if f.get("volume_pace") is not None:
        lines.append(f"Volume pace now: {f['volume_pace']:.2f}× normal")
    lines.append("<i>Order books change in seconds and can be spoofed — context only.</i>")
    return "\n".join(lines)


def explanation(a: Analysis, summary: str | None = None) -> str:
    parts = [f"📖 <b>FULL EXPLANATION</b> · {html.escape(a.pretty)}\n{LINE}"]
    for n, (fam, title) in enumerate(strategies.FAMILIES, 1):
        parts.append(_strategy_block(n, title, a.family_view(fam), a))
    parts.append(THIN)
    st = structure_text(a)
    if st:
        parts.append(st)
    parts.append(flow_text(a))
    if summary:
        parts.append(f"🤖 <b>AI SUMMARY</b> (Gemini, from the facts above)\n{html.escape(summary)}")
    parts.append("<i>Rules-based analysis for learning and paper trading — not financial advice. "
                 "Signals use completed candles; check the live price before acting.</i>")
    return "\n\n".join(parts)


def full_text(a: Analysis, summary: str | None = None) -> str:
    """All three messages as one text (used by tests and logs)."""
    return "\n\n".join([card(a), strategy_list(a), explanation(a, summary)])


def facts(a: Analysis) -> dict:
    return {
        "symbol": a.symbol, "market": a.market, "timeframe": a.timeframe, "last_close": a.price,
        "verdict": verdict(a),
        "structure": structure_text(a).replace("<b>", "").replace("</b>", "").replace("&amp;", "&"),
        "order_flow": ({k: v for k, v in a.flow.items() if not isinstance(v, tuple)} | {"reading": data.flow_verdict(a.flow)})
                      if a.flow else None,
        "strategies": [
            {"name": s.name, "side": s.side, "status": s.status,
             "failed_checks": [t for ok, t in s.checks if not ok],
             "entry": s.entry, "stop": s.stop, "target": s.target,
             "backtest": {k: s.backtest.get(k) for k in ("trades", "win_rate", "avg_r", "profit_factor")}}
            for s in (a.family_view(f) for f, _ in strategies.FAMILIES)
        ],
    }


# ---------------------------------------------------------------- the numbered list

GROUP_ORDER = [("BUY", "🟢 <b>BUY SETUPS</b>"), ("SELL", "🔴 <b>SELL SETUPS</b>"),
               ("MIXED", "⚖️ <b>MIXED (BUY vs SELL)</b>"), ("FORMING", "🟡 <b>FORMING</b>")]


def ordered(items: list[Analysis]) -> list[Analysis]:
    rank = {"BUY": 0, "SELL": 0, "MIXED": 1, "FORMING": 2, "NONE": 3}
    return sorted(items, key=lambda a: (rank[a.direction], -a.score))


def list_text(items: list[Analysis], market: str, errors: list[str]) -> str:
    """Numbered scanner list; the numbers match ordered(items)."""
    items = ordered(items)
    if market == "crypto":
        head = f"🪙 <b>CRYPTO SCANNER</b> · {cfg()['crypto']['timeframe']} candles"
    else:
        head = "📈 <b>NSE SCANNER</b> · daily candles"
    lines = [head, f"<i>{_now()} · {len(items)} scanned · 7 strategies × BUY/SELL</i>", LINE]
    numbered = {a.symbol: i for i, a in enumerate(items, 1)}
    for key, title in GROUP_ORDER:
        group = [a for a in items if a.direction == key]
        if not group:
            continue
        lines.append(title)
        for a in group:
            chg = f" ({a.change_pct:+.1f}%)" if a.change_pct is not None else ""
            if key in ("BUY", "SELL"):
                what = f"{a.agree}/7 agree"
            elif key == "MIXED":
                what = f"{len(a.side_trades('BUY'))} BUY · {len(a.side_trades('SELL'))} SELL"
            else:
                what = f"{a.forming_side} forming"
            lines.append(f"<b>{numbered[a.symbol]}.</b> {html.escape(a.short)} — {what} · {_num(a.price)}{chg}")
        lines.append("")
    rest = [a for a in items if a.direction == "NONE"]
    if rest:
        lines.append("⚪ <b>NO SETUP</b>")
        lines.append("  ".join(f"<b>{numbered[a.symbol]}.</b> {html.escape(a.short)}" for a in rest))
        lines.append("")
    if not any(a.direction in ("BUY", "SELL") for a in items):
        lines.append("No confirmed setup right now — that's normal; good setups are rare.")
    if errors:
        lines.append("Skipped (no data): " + html.escape(", ".join(errors)))
    lines += [LINE, "👇 <b>Tap a number</b> (or send it, e.g. <code>2</code>) for entry, take profit, "
                    "stop-loss and the full analysis."]
    return "\n".join(lines)


def list_buttons(items: list[Analysis]) -> list[list[tuple[str, str]]]:
    items = ordered(items)
    icon = {"BUY": "🟢", "SELL": "🔴", "MIXED": "⚖️", "FORMING": "🟡", "NONE": "⚪"}
    btns = []
    for i, a in enumerate(items, 1):
        code = "c" if a.market == "crypto" else "s"
        btns.append((f"{i}. {a.short} {icon[a.direction]}", f"a:{code}:{a.symbol}"))
    return [btns[k:k + 3] for k in range(0, len(btns), 3)]

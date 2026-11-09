"""Turn candles into a multi-strategy analysis and the Telegram messages.

Message layout for one symbol (clean UI: the long parts sit behind buttons):
  1. Chart photo + "trade card" caption: live price, BUY / SELL / no setup, entry, take profit,
     stop-loss, size, India VIX (Indian assets)
  2. Strategy check: all 14 strategies, numbered, 🟢 BUY / 🔴 SELL / 🟡 forming / ⚪ none / ➖ n/a,
     with buttons: timeframes, strategy details, structure & levels, volume & order book, ...
  3. On request (buttons): strategy details, market structure & key levels, live volume & order book
"""
from __future__ import annotations

import html
import math
import time
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from bot import backtest, data, strategies
from bot.config import cfg, cost_pct

MIN_BARS = 220      # enough for EMA200
MIN_BARS_SHORT = 120  # futures contracts with a short life (MCX/CDS daily): EMA200 rules are approximate
LINE = "━━━━━━━━━━━━━━━━━━"
THIN = "────────────"
KEYCAP = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
SIDE_ICON = {"BUY": "🟢", "SELL": "🔴"}
RANK = {"TRADE": 2, "WATCH": 1, "NO": 0, "NA": -1}
MARKET_CODE = {"crypto": "c", "stock": "s", "fx": "f"}
CODE_MARKET = {v: k for k, v in MARKET_CODE.items()}
CACHE_SECONDS = 15 * 60
_CACHE: dict = {}


def num_label(n: int) -> str:
    """1️⃣ … 🔟 then 11, 12 … in bold (keycaps stop at 10)."""
    return KEYCAP[n - 1] if 1 <= n <= 10 else f"<b>{n}.</b>"


@dataclass
class Analysis:
    symbol: str
    market: str           # crypto | stock | fx
    timeframe: str
    x: pd.DataFrame
    setups: list = field(default_factory=list)
    change_pct: float | None = None
    source: str = ""
    live_price: float | None = None
    snap: object = None          # structure.Snapshot
    flow: dict | None = None     # live volume + order book
    venue: str = ""
    currency: str = ""
    notes: list = field(default_factory=list)
    made: float = field(default_factory=time.time)
    vol_line: str = ""           # India VIX / volatility line for the card (filled by bot.vol)

    @property
    def price(self) -> float:
        return float(self.x["close"].iloc[-1])

    @property
    def now_price(self) -> float:
        return self.live_price or self.price

    @property
    def applicable(self) -> list:
        return [s for s in self.setups if s.status != "NA"]

    @property
    def n_applicable(self) -> int:
        return len({s.family for s in self.applicable})

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
    def is_indian(self) -> bool:
        return self.market == "stock" or self.currency == "INR"

    @property
    def asset(self):
        if self.market != "fx":
            return None
        from bot import fx
        return fx.find(self.symbol)

    @property
    def pretty(self) -> str:
        if self.market == "crypto" and self.symbol.endswith("USDT"):
            return f"{self.symbol[:-4]}/USDT"
        if self.market == "fx" and self.asset:
            return self.asset.name
        return self.symbol

    @property
    def short(self) -> str:
        return self.symbol[:-4] if self.market == "crypto" and self.symbol.endswith("USDT") else self.symbol

    @property
    def code(self) -> str:
        return MARKET_CODE[self.market]

    @property
    def cb(self) -> str:
        """Callback suffix that identifies this analysis: c:BTCUSDT:4h"""
        return f"{self.code}:{self.symbol}:{self.timeframe}"

    @property
    def cost_market(self) -> str:
        if self.market == "fx":
            return "mcx" if self.venue in ("mcx", "cds") else "fx"
        return self.market


def default_tf(market: str) -> str:
    c = cfg()
    if market == "crypto":
        return data.norm_tf(c["crypto"].get("timeframe"), "4h")
    if market == "fx":
        return data.norm_tf(c.get("forex", {}).get("timeframe"), "1h")
    return data.norm_tf(c["stocks"].get("timeframe"), "1d")


def load_candles(symbol: str, market: str, tf: str, for_scan: bool = False) -> pd.DataFrame:
    if market == "crypto":
        return data.crypto_candles(symbol, tf)
    if market == "fx":
        from bot import fx
        return fx.candles(symbol, tf, for_scan=for_scan)
    return data.stock_candles(symbol, tf, live=not for_scan)


def analyze(symbol: str, market: str, candles: pd.DataFrame | None = None,
            change_pct: float | None = None, with_flow: bool = False, tf: str | None = None,
            for_scan: bool = False) -> Analysis:
    tf = data.norm_tf(tf or (candles.attrs.get("tf") if candles is not None else None), default_tf(market))
    live = candles is None
    if candles is None:
        candles = load_candles(symbol, market, tf, for_scan)
    notes = []
    if len(candles) < MIN_BARS:
        if len(candles) >= MIN_BARS_SHORT and market == "fx":
            notes.append(f"Only {len(candles)} candles (short contract history): 200-EMA rules are approximate.")
        else:
            raise data.DataError(f"{symbol}: only {len(candles)} {tf} candles, need {MIN_BARS}")
    attrs = dict(candles.attrs)
    tz = attrs.get("tz") or ("Asia/Kolkata" if market == "stock" else "UTC")
    venue = attrs.get("venue") or {"crypto": "binance", "stock": "nse", "fx": "fx"}[market]
    from bot.angel import INDEX_ALIASES

    if market == "stock" and symbol.upper() in INDEX_ALIASES:
        venue = "nse_index"
    x, snap = strategies.prepare(candles, tf=tf, tz=tz, venue=venue)
    if market == "crypto":
        name = data.norm_crypto(symbol)
    elif market == "fx":
        from bot import fx
        a_ = fx.find(symbol)
        name = a_.code if a_ else symbol.upper()
    else:
        name = symbol.upper()
    source = attrs.get("source") or ("Binance (live)" if market == "crypto" else "")
    if change_pct is None:
        change_pct = attrs.get("live_change_pct")
    currency = attrs.get("currency") or {"crypto": "USDT", "stock": "INR"}.get(market, "USD")
    a = Analysis(name, market, tf, x, change_pct=change_pct, source=source,
                 live_price=attrs.get("live_price"), snap=snap, venue=venue, currency=currency, notes=notes)
    if live and with_flow:
        a.flow = fetch_flow(a, float(x["vol_avg20"].iloc[-1]) if x["vol_avg20"].notna().any() else None)
    cost_mkt = a.cost_market
    for strat in strategies.ALL:
        s = strategies.evaluate(strat, x)
        s.backtest = backtest.run(strat, x, cost_mkt) if s.status != "NA" else {"trades": 0, "na": True}
        a.setups.append(s)
    if live:
        _CACHE[(a.market, a.symbol, a.timeframe)] = a
    return a


def cached(market: str, symbol: str, tf: str) -> Analysis | None:
    a = _CACHE.get((market, symbol, tf))
    if a and time.time() - a.made <= CACHE_SECONDS:
        return a
    return None


def get(market: str, symbol: str, tf: str, with_flow: bool = True) -> Analysis:
    """Cached analysis for detail buttons (re-analyses if older than 15 minutes)."""
    return cached(market, symbol, tf) or analyze(symbol, market, with_flow=with_flow, tf=tf)


def fetch_flow(a: Analysis, avg_volume: float | None) -> dict | None:
    if a.market == "crypto":
        return data.crypto_orderflow(a.symbol, avg_volume, a.timeframe)
    from bot import angel

    if a.market == "fx":
        attrs = a.x.attrs
        if attrs.get("token") and angel.configured():
            try:
                return angel.orderflow(angel.session(), a.symbol, None, attrs["token"], attrs["exch"],
                                       attrs.get("contract"))
            except angel.AngelError:
                return None
        if attrs.get("spread_pct") is not None:
            return {"venue": "MetaTrader 5", "spread_pct": attrs["spread_pct"], "unit": a.currency}
        return None
    if not angel.configured():
        return None
    try:
        daily_avg = None
        if a.timeframe == "1d":
            daily_avg = avg_volume
        return angel.orderflow(angel.session(), a.symbol, daily_avg)
    except angel.AngelError:
        return None


def resolve(arg: str) -> tuple[str, str]:
    """'SOL' -> crypto; 'RELIANCE', a watchlist name or an index -> stock; 'EURUSD', 'XAUUSD' -> fx."""
    a = arg.upper().strip().removeprefix("$").removeprefix("/")
    from bot import fx
    from bot.angel import INDEX_ALIASES

    if fx.find(a):
        return fx.find(a).code, "fx"
    if a.endswith(".NS") or a in cfg()["stocks"]["watchlist"] or a in INDEX_ALIASES:
        return a.removesuffix(".NS"), "stock"
    if a in ("VIX", "INDIAVIX", "INDIA VIX"):
        return "INDIAVIX", "stock"
    return a, "crypto"


def analyze_any(arg: str, tf: str | None = None) -> Analysis:
    sym, market = resolve(arg)
    try:
        return analyze(sym, market, with_flow=True, tf=tf)
    except data.DataError:
        if market == "crypto":  # maybe it's an NSE stock not in the watchlist
            return analyze(sym, "stock", with_flow=True, tf=tf)
        raise


# ---------------------------------------------------------------- small helpers

def _num(v: float | None) -> str:
    return strategies._fmt(v) if v is not None else "–"


def price_txt(a: Analysis, v: float | None) -> str:
    if v is None:
        return "–"
    if a.market == "fx" and a.asset is not None and a.asset.digits is not None and not a.asset.indian:
        return f"{v:,.{a.asset.digits}f}"
    if a.currency == "INR":
        return f"₹{_num(v)}"
    return _num(v)


def _pct(a: float, b: float) -> str:
    return f"{(b - a) / a * 100:+.2f}%" if a else ""


def _now() -> str:
    return datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%d %b, %H:%M IST")


def _candle_time(a: Analysis) -> str:
    return a.x.index[-1].tz_convert("Asia/Kolkata").strftime("%d %b %H:%M IST")


def size_info(s, market: str, unit: str = "", a: Analysis | None = None) -> tuple[str, str] | None:
    """(size, loss) at the smallest risk level of the ladder that fits, for compact displays."""
    if a is None or s.entry is None or s.stop is None:
        return None
    from bot import risk

    sp = risk.spec_for(a, s.entry)
    for r in risk.ladder(a, s.entry, s.stop, s.target, sp):
        if r.fits:
            return risk.fmt_qty(r.qty, sp), risk.money(r.risk, sp.cur)
    return None


def position_size(s, market: str, unit: str = "", a: Analysis | None = None) -> str:
    """One line: the whole risk ladder for this setup."""
    from bot import risk

    if a is None or s.entry is None:
        return ""
    sp = risk.spec_for(a, s.entry)
    parts = []
    for r in risk.ladder(a, s.entry, s.stop, s.target, sp):
        if r.fits:
            parts.append(f"{risk.money(r.budget, sp.cur)} → {risk.fmt_qty(r.qty, sp)} ({risk.money(r.reward, sp.cur, True)})")
    if not parts:
        rows = risk.ladder(a, s.entry, s.stop, s.target, sp)
        if rows:
            return (f"Risk ladder: the smallest size {risk.fmt_qty(rows[0].qty, sp)} risks "
                    f"{risk.money(rows[0].risk, sp.cur)} — above the {risk.money(risk.max_risk(sp.cur), sp.cur)} maximum")
        return ""
    return "Risk ladder: " + " · ".join(parts)


def _bt_line(s) -> str:
    b = s.backtest
    if not b.get("trades"):
        return f"📈 History ({b.get('from', '')} – {b.get('to', '')}): no completed trades yet"
    pf = b["profit_factor"] if b["profit_factor"] is not None else "n/a"
    line = (f"📈 History ({b['from']} – {b['to']}): {b['trades']} trades · win {b['win_rate']}% · "
            f"avg {b['avg_r']:+}R · PF {pf}")
    if not b["enough_data"]:
        line += " · ⚠️ few trades, weak evidence"
    return line


def verdict(a: Analysis) -> str:
    d = a.direction
    b, s = len(a.side_trades("BUY")), len(a.side_trades("SELL"))
    n = a.n_applicable
    if d in ("BUY", "SELL"):
        return f"{d} — {a.agree} of {n} strategies agree"
    if d == "MIXED":
        return f"MIXED — {b} BUY vs {s} SELL"
    if d == "FORMING":
        return f"FORMING ({a.forming_side}) — {len(a.watches)} setup(s) need one more condition"
    return "NO SETUP"


def regime_text(a: Analysis) -> str:
    reg, adx = strategies.regime(a.x)
    if adx is None:
        return "Regime: unknown"
    favour = ("trend strategies (MA, breakout, pullback, Fib, ORB)" if reg == "trending" else
              "mean reversion (RSI(2), Bollinger, gap fill)" if reg == "ranging" else "both, with care")
    return f"Regime: <b>{reg}</b> (ADX {adx:.0f}) → favours {favour}"


# ---------------------------------------------------------------- message 1: trade card

def card(a: Analysis) -> str:
    """Caption for the chart photo (Telegram limit 1024 characters)."""
    icon = {"crypto": "🪙", "fx": (a.asset.icon if a.asset else "💱")}.get(a.market, "🏛" if a.is_index else "📈")
    chg_lbl = "24h" if a.market == "crypto" else "today"
    chg = f" ({a.change_pct:+.2f}% {chg_lbl})" if a.change_pct is not None else ""
    mode = data.TF_MODE.get(a.timeframe, "")
    lines = [f"{icon} <b>{html.escape(a.pretty)}</b> · {a.timeframe} · {mode}",
             f"💹 Live <b>{price_txt(a, a.now_price)}</b>{chg}"]
    if a.vol_line:
        lines.append(a.vol_line)
    lines.append("")
    d, best = a.direction, a.best()
    n = a.n_applicable

    if d in ("BUY", "SELL") and best:
        lines.append(f"{SIDE_ICON[d]} <b>{d} SETUP</b> · {a.agree} of {n} strategies agree")
    elif d == "FORMING" and best:
        lines.append(f"🟡 <b>{best.side} SETUP FORMING</b> · not confirmed yet")
    elif d == "MIXED":
        lines.append(f"⚖️ <b>MIXED SIGNALS</b> · {len(a.side_trades('BUY'))} BUY vs "
                     f"{len(a.side_trades('SELL'))} SELL → no trade")
    else:
        lines.append("⚪ <b>NO SETUP RIGHT NOW</b>")

    if best and d in ("BUY", "SELL", "FORMING"):
        prefix = "" if d != "FORMING" else "If it triggers: "
        rr = best.rr
        lines += [LINE,
                  f"{prefix}🎯 Entry <b>{price_txt(a, best.entry)}</b>",
                  f"💰 Take profit <b>{price_txt(a, best.target)}</b> ({_pct(best.entry, best.target)})",
                  f"🛑 Stop-loss <b>{price_txt(a, best.stop)}</b> ({_pct(best.entry, best.stop)})"]
        if best.targets:
            lines.append("🎯 " + " · ".join(f"{html.escape(lbl.split()[0])} {price_txt(a, p)}"
                                           for lbl, p in best.targets))
        if rr is not None:
            lines.append(f"⚖️ Reward : Risk = {rr:.1f} : 1")
        from bot import risk
        lines += risk.ladder_lines(a, best.entry, best.stop, best.target, compact=True)
        if a.is_index:
            lines.append("   <i>Index: sizes are futures lots — or use the F&amp;O buttons below</i>")
        bt = best.backtest
        hist = f" · history {bt['trades']} trades, win {bt['win_rate']}%" if bt.get("trades") else ""
        lines += [LINE, f"Levels from: {html.escape(best.name)}{hist}"]
    else:
        sn = a.snap
        if sn is not None and (sn.supports or sn.resistances):
            lines.append(LINE)
            if sn.resistances:
                lines.append(f"🔺 Resistance {price_txt(a, sn.resistances[0][0])}")
            if sn.supports:
                lines.append(f"🔻 Support {price_txt(a, sn.supports[0][0])}")
        lines.append("Wait for a setup — see the strategy check below.")

    if a.flow and a.flow.get("imbalance") is not None:
        lines.append(f"📊 Order book: {data.flow_verdict(a.flow)}")
    if d == "SELL" or (d == "FORMING" and best and best.side == "SELL"):
        if a.market in ("stock",) or a.market == "crypto":
            lines.append("<i>SELL = exit/avoid if you hold it. Shorting needs futures/options"
                         + (" (crypto: futures)" if a.market == "crypto" else "") + ".</i>")
    lines.append(f"<i>Candle closed {_candle_time(a)} · {html.escape(a.source or 'data')}</i>")
    return "\n".join(lines)


# ---------------------------------------------------------------- message 2: strategy check

def _state(s, title: str) -> str:
    t = html.escape(title)
    if s.status == "TRADE":
        return f"{SIDE_ICON[s.side]} {t} — <b>{s.side}</b>" + (" ✨" if s.fresh else "")
    if s.status == "WATCH":
        return f"🟡 {t} — forming {s.side} ({s.passed}/3)"
    if s.status == "NA":
        return f"➖ {t} — <i>{html.escape(s.na_reason)}</i>"
    return f"⚪ {t} — no setup"


def strategy_list(a: Analysis, alert: bool = False) -> str:
    head = "🔔 <b>NEW SIGNAL</b> · " if alert else ""
    lines = [f"{head}📋 <b>STRATEGY CHECK</b> · {html.escape(a.pretty)} · {a.timeframe}",
             f"<i>{strategies.N_STRATEGIES} strategies × BUY/SELL · {_now()}</i>", LINE]
    for n, (fam, title) in enumerate(strategies.FAMILIES, 1):
        lines.append(f"{num_label(n)} {_state(a.family_view(fam), title)}")
    b, s = len(a.side_trades("BUY")), len(a.side_trades("SELL"))
    w = len({x.family for x in a.watches})
    lines += [LINE,
              f"🟢 BUY {b} · 🔴 SELL {s} · 🟡 forming {w} · of {a.n_applicable} usable here",
              regime_text(a),
              f"<b>Verdict:</b> {html.escape(verdict(a))}"]
    for note in a.notes:
        lines.append(f"⚠️ {html.escape(note)}")
    lines.append("<i>🟢 BUY · 🔴 SELL · 🟡 forming · ⚪ none · ➖ not for this chart · ✨ new this candle</i>")
    lines.append("👇 Details, levels, order book and other timeframes below")
    return "\n".join(lines)


# ---------------------------------------------------------------- on request: details

def _strategy_block(n: int, title: str, s, a: Analysis) -> str:
    if s.status == "NA":
        return f"{num_label(n)} <b>{html.escape(title)}</b> · ➖ not used here ({html.escape(s.na_reason)})"
    if s.status == "TRADE":
        tag = f"{SIDE_ICON[s.side]} {s.side} setup" + (" (new this candle)" if s.fresh else "")
    elif s.status == "WATCH":
        tag = f"🟡 forming {s.side} ({s.passed}/3)"
    else:
        tag = f"⚪ no setup (closest: {s.side} {s.passed}/3)"
    lines = [f"{num_label(n)} <b>{html.escape(title)}</b> · {tag}", f"<i>{html.escape(s.idea)}</i>"]
    lines += [f"{'✅' if ok else '❌'} {html.escape(t)}" for ok, t in s.checks]
    if s.entry is not None and s.status in ("TRADE", "WATCH"):
        prefix = "" if s.status == "TRADE" else "If it triggers: "
        lines.append(f"{prefix}🎯 {price_txt(a, s.entry)} · 💰 {price_txt(a, s.target)} · 🛑 {price_txt(a, s.stop)}")
        if s.targets:
            lines.append("🎯 " + " · ".join(f"{html.escape(l)} {price_txt(a, p)}" for l, p in s.targets))
        ps = position_size(s, a.market, a.short, a)
        if ps:
            lines.append("📦 " + ps)
    if s.exit_note and s.status in ("TRADE", "WATCH"):
        lines.append(f"🚪 {html.escape(s.exit_note)}")
    if s.status in ("TRADE", "WATCH"):
        lines.append(_bt_line(s))
    return "\n".join(lines)


def details(a: Analysis, summary: str | None = None) -> str:
    parts = [f"📖 <b>STRATEGY DETAILS</b> · {html.escape(a.pretty)} · {a.timeframe}\n{LINE}"]
    for n, (fam, title) in enumerate(strategies.FAMILIES, 1):
        parts.append(_strategy_block(n, title, a.family_view(fam), a))
    if summary:
        parts.append(f"🤖 <b>AI SUMMARY</b> (Gemini, from the facts above)\n{html.escape(summary)}")
    parts.append("<i>Rules-based analysis for learning and paper trading — not financial advice. "
                 "Signals use completed candles; check the live price before acting.</i>")
    return "\n\n".join(parts)


def explanation(a: Analysis, summary: str | None = None) -> str:
    """Everything in one text (strategy details + structure + order book), used by tests and logs."""
    parts = [details(a, summary).replace("📖 <b>STRATEGY DETAILS</b>", "📖 <b>FULL EXPLANATION</b>"), THIN]
    st = structure_text(a)
    if st:
        parts.append(st)
    parts.append(flow_text(a))
    return "\n\n".join(parts)


def structure_text(a: Analysis) -> str:
    from bot.structure import ZONE_NAMES, line_value

    sn = a.snap
    if sn is None:
        return ""
    p = lambda v: price_txt(a, v)  # noqa: E731
    st = {1: "🟢 bullish (higher highs / higher lows)", -1: "🔴 bearish (lower highs / lower lows)"}.get(
        sn.structure, "⚪ not established yet")
    lines = [f"🧭 <b>MARKET STRUCTURE &amp; KEY LEVELS</b> · {html.escape(a.pretty)} · {a.timeframe}", LINE,
             f"Live price: <b>{p(a.now_price)}</b>", f"Structure: {st}", regime_text(a)]
    if sn.breaks:
        i, lvl, kind, d = sn.breaks[-1]
        ago = len(a.x) - 1 - i
        lines.append(f"Last break: {'🟢 bullish' if d > 0 else '🔴 bearish'} {kind} through {p(lvl)}, {ago} candle(s) ago")
    if sn.swings:
        lines.append("Recent swings: " + " → ".join(lab for _, _, lab in sn.swings[-6:]))
    if sn.resistances:
        lines.append("🔺 Resistance: " + ", ".join(f"{p(v)} ({t}×)" for v, t in sn.resistances))
    if sn.supports:
        lines.append("🔻 Support: " + ", ".join(f"{p(v)} ({t}×)" for v, t in sn.supports))
    if sn.round_levels:
        lines.append("🔘 Round numbers: " + ", ".join(p(v) for v in sn.round_levels))
    n = len(a.x) - 1
    if sn.tl_down:
        lines.append(f"📉 Falling trend line now at {p(line_value(sn.tl_down, n))} (break above = bullish)")
    if sn.tl_up:
        lines.append(f"📈 Rising trend line now at {p(line_value(sn.tl_up, n))} (break below = bearish)")
    x = a.x
    hi, lo = x["sw_hi"].iloc[-1], x["sw_lo"].iloc[-1]
    if hi == hi and lo == lo and hi > lo:
        leg = hi - lo
        up = x["sw_lo_i"].iloc[-1] < x["sw_hi_i"].iloc[-1]
        lv = [(r, hi - r * leg) if up else (r, lo + r * leg) for r in (0.236, 0.382, 0.5, 0.618, 0.786)]
        lines.append(f"🌀 Fibonacci ({'swing low→high' if up else 'swing high→low'} {p(lo if up else hi)}→"
                     f"{p(hi if up else lo)}): " + " · ".join(f"{r * 100:.1f}% {p(v)}" for r, v in lv))
    ema = " · ".join(f"EMA{k} {p(float(x[f'ema{k}'].iloc[-1]))}" for k in (20, 50, 200))
    lines.append(f"〰️ {ema}")
    if x["vwap"].notna().iloc[-1]:
        lines.append(f"〰️ VWAP {p(float(x['vwap'].iloc[-1]))} (±1σ {p(float(x['vwap'].iloc[-1] - x['vwap_sd'].iloc[-1]))}"
                     f"–{p(float(x['vwap'].iloc[-1] + x['vwap_sd'].iloc[-1]))})")
    if x["or_high"].notna().iloc[-1]:
        lines.append(f"⏰ Opening range (first 15 min): {p(float(x['or_low'].iloc[-1]))}–{p(float(x['or_high'].iloc[-1]))}")
    for z in sorted(sn.zones, key=lambda z: -z.top):
        where = "above price" if z.bot > a.price else ("below price" if z.top < a.price else "price inside")
        dot = "🟩" if z.bullish else "🟥"
        lines.append(f"{dot} {ZONE_NAMES[z.kind].capitalize()}: {p(z.bot)}–{p(z.top)} ({where}, tested {z.tests}×)")
    return "\n".join(lines)


def flow_text(a: Analysis) -> str:
    f = a.flow
    head = f"📊 <b>LIVE VOLUME &amp; ORDER BOOK</b> · {html.escape(a.pretty)}"
    x = a.x
    vol_lines = []
    if strategies.has_volume(x):
        rv = x["rvol"].iloc[-1]
        vol_lines.append(f"Last {a.timeframe} candle volume: {x['volume'].iloc[-1]:,.0f} = {rv:.2f}× its 20-candle "
                         f"average {'🟢 strong' if rv >= 1.5 else '⚪ normal' if rv >= 0.7 else '🔴 weak'}")
    if not f:
        why = {"stock": "needs Angel One; the NSE order book exists only in market hours",
               "fx": "forex is traded over the counter: no central order book (MT5 shows the spread only)"}.get(
            a.market, "not available right now")
        return "\n".join([head, LINE] + vol_lines + [f"Order book: {why}."])
    unit = f.get("unit", "")
    lines = [f"{head} · {html.escape(f.get('venue', ''))}", LINE] + vol_lines
    if f.get("imbalance") is not None:
        verdict_ = data.flow_verdict(f)
        icon = "🟢" if verdict_.startswith("buyers") else "🔴" if verdict_.startswith("sellers") else "⚪"
        lines.append(f"Reading: {icon} <b>{verdict_}</b>")
        if "tot_buy_qty" in f:
            lines.append(f"Pending buy vs sell: {f['imbalance']:.2f}× ({f['tot_buy_qty']:,.0f} vs {f['tot_sell_qty']:,.0f})")
        else:
            lines.append(f"Bids vs asks within ±1%: {f['imbalance']:.2f}× "
                         f"({f['bid_value_1pct']:,.0f} vs {f['ask_value_1pct']:,.0f} {unit})")
    if f.get("top_bids") and f.get("top_asks"):
        rows = [f"{'BID qty':>10} {'BID':>10} │ {'ASK':<10} {'ASK qty':<10}"]
        for (bp, bq), (ap, aq) in zip(f["top_bids"], f["top_asks"]):
            rows.append(f"{bq:>10,.4g} {_num(bp):>10} │ {_num(ap):<10} {aq:<10,.4g}")
        lines.append("<pre>" + html.escape("\n".join(rows)) + "</pre>")
    if f.get("bid_wall"):
        p_, q = f["bid_wall"]
        lines.append(f"🧱 Biggest buy wall: {_num(p_)} (qty {q:,.4g})")
    if f.get("ask_wall"):
        p_, q = f["ask_wall"]
        lines.append(f"🧱 Biggest sell wall: {_num(p_)} (qty {q:,.4g})")
    if f.get("spread_pct") is not None:
        lines.append(f"Spread: {f['spread_pct']:.3f}%")
    if f.get("taker_buy_pct") is not None:
        lines.append(f"Last candle: {f['taker_buy_pct']:.0f}% aggressive buying")
    if f.get("volume_pace") is not None:
        lines.append(f"Volume pace now: {f['volume_pace']:.2f}× normal")
    lines.append("<i>Order books change in seconds and can be spoofed — context only.</i>")
    return "\n".join(lines)


def full_text(a: Analysis, summary: str | None = None) -> str:
    """All messages as one text (used by tests and logs)."""
    return "\n\n".join([card(a), strategy_list(a), explanation(a, summary)])


def facts(a: Analysis) -> dict:
    return {
        "symbol": a.symbol, "market": a.market, "timeframe": a.timeframe, "last_close": a.price,
        "live_price": a.now_price, "verdict": verdict(a),
        "regime": strategies.regime(a.x)[0],
        "structure": structure_text(a).replace("<b>", "").replace("</b>", "").replace("&amp;", "&"),
        "order_flow": ({k: v for k, v in a.flow.items() if not isinstance(v, (tuple, list))}
                       | {"reading": data.flow_verdict(a.flow)}) if a.flow else None,
        "strategies": [
            {"name": s.name, "side": s.side, "status": s.status,
             "failed_checks": [t for ok, t in s.checks if not ok],
             "entry": s.entry, "stop": s.stop, "target": s.target,
             "backtest": {k: s.backtest.get(k) for k in ("trades", "win_rate", "avg_r", "profit_factor")}}
            for s in (a.family_view(f) for f, _ in strategies.FAMILIES) if s.status != "NA"
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
    tf = items[0].timeframe if items else default_tf("crypto" if market == "crypto" else market)
    heads = {"crypto": "🪙 <b>CRYPTO SCANNER</b>", "fx": "💱 <b>FOREX &amp; COMMODITIES</b>"}
    head = f"{heads.get(market, '📈 <b>NSE SCANNER</b>')} · {tf} candles"
    lines = [head, f"<i>{_now()} · {len(items)} scanned · {strategies.N_STRATEGIES} strategies × BUY/SELL</i>", LINE]
    numbered = {a.symbol: i for i, a in enumerate(items, 1)}
    for key, title in GROUP_ORDER:
        group = [a for a in items if a.direction == key]
        if not group:
            continue
        lines.append(title)
        for a in group:
            chg = f" ({a.change_pct:+.1f}%)" if a.change_pct is not None else ""
            if key in ("BUY", "SELL"):
                what = f"{a.agree}/{a.n_applicable} agree"
            elif key == "MIXED":
                what = f"{len(a.side_trades('BUY'))} BUY · {len(a.side_trades('SELL'))} SELL"
            else:
                what = f"{a.forming_side} forming"
            lines.append(f"<b>{numbered[a.symbol]}.</b> {html.escape(a.short)} — {what} · "
                         f"{price_txt(a, a.now_price)}{chg}")
        lines.append("")
    rest = [a for a in items if a.direction == "NONE"]
    if rest:
        lines.append("⚪ <b>NO SETUP</b>")
        lines.append("\n".join(f"<b>{numbered[a.symbol]}.</b> {html.escape(a.short)} · {price_txt(a, a.now_price)}"
                               for a in rest))
        lines.append("")
    if not any(a.direction in ("BUY", "SELL") for a in items):
        lines.append("No confirmed setup right now — that's normal; good setups are rare.")
    if errors:
        lines.append("Skipped (no data): " + html.escape(", ".join(errors)))
    lines += [LINE, "👇 <b>Tap a number</b> (or send it, e.g. <code>2</code>) for live price, entry, take profit, "
                    "stop-loss and the strategy check."]
    return "\n".join(lines)


def list_buttons(items: list[Analysis]) -> list[list[tuple[str, str]]]:
    items = ordered(items)
    icon = {"BUY": "🟢", "SELL": "🔴", "MIXED": "⚖️", "FORMING": "🟡", "NONE": "⚪"}
    btns = []
    for i, a in enumerate(items, 1):
        btns.append((f"{i}. {a.short} {icon[a.direction]}", f"a:{a.cb}"))
    return [btns[k:k + 3] for k in range(0, len(btns), 3)]

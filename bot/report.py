"""Turn candles into a full multi-strategy analysis and Telegram-ready text."""
from __future__ import annotations

import html
import math
from dataclasses import dataclass, field

import pandas as pd

from bot import backtest, data, strategies
from bot.config import capital, cfg, cost_pct

ICON = {"TRADE": "🟢", "WATCH": "🟡", "NO": "⚪"}
MIN_BARS = 220  # enough for EMA200


@dataclass
class Analysis:
    symbol: str
    market: str
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

    @property
    def score(self) -> int:
        return 3 * len(self.trades) + len(self.watches)

    def best(self):
        def quality(s):
            r = s.backtest.get("avg_r")
            return r if r is not None else -99
        for group in (self.trades, self.watches):
            usable = [s for s in group if s.entry is not None]
            if usable:
                return max(usable, key=quality)
        return None


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
    """'SOL' / 'solusdt' -> crypto; 'RELIANCE' or a watchlist name -> stock."""
    a = arg.upper().strip().removeprefix("$")
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


# ---------------------------------------------------------------- formatting

def _num(v: float) -> str:
    return strategies._fmt(v)


def position_size(s, market: str) -> str:
    cap, cur = capital(market)
    risk_pct = cfg()["risk"]["risk_per_trade_pct"]
    unit_risk = (s.entry - s.stop) + s.entry * cost_pct(market) / 100 * 2
    qty = min(cap * risk_pct / 100 / unit_risk, cap / s.entry)
    if market == "stock":
        qty = math.floor(qty)
        if qty < 1:
            return f"Size: 1 share costs more than your {risk_pct}% risk budget allows - skip or raise capital."
        qty_txt = f"{qty} share(s)"
    else:
        qty_txt = f"{qty:.6g} coins"
    return (f"Size for {cap:,.0f} {cur} at {risk_pct}% risk: {qty_txt} "
            f"(≈{qty * s.entry:,.2f} {cur}, max loss ≈{qty * unit_risk:,.2f} {cur})")


def _bt_line(s, a: Analysis) -> str:
    b = s.backtest
    if not b.get("trades"):
        return f"History ({b.get('from')} to {b.get('to')}): no completed trades - no evidence either way."
    pf = b["profit_factor"] if b["profit_factor"] is not None else "n/a"
    line = (f"History on {a.symbol} {a.timeframe} ({b['from']} to {b['to']}): {b['trades']} trades · "
            f"win {b['win_rate']}% · avg {b['avg_r']:+}R · total {b['total_r']:+}R · PF {pf}")
    if not b["enough_data"]:
        line += "\n⚠️ Fewer than 20 trades: weak evidence."
    return line


def verdict(a: Analysis) -> str:
    t, w = len(a.trades), len(a.watches)
    return f"{t} of {len(a.setups)} strategies say TRADE, {w} WATCH"


def header(a: Analysis) -> str:
    unit = "USDT" if a.market == "crypto" else "INR"
    label = "24h" if a.market == "crypto" else "today"
    chg = f" · {label} {a.change_pct:+.2f}%" if a.change_pct is not None else ""
    when = a.x.index[-1].tz_convert("Asia/Kolkata").strftime("%d %b %H:%M IST")
    live = f" · live {_num(a.live_price)}" if a.live_price else ""
    src = f" · data: {html.escape(a.source)}" if a.source else ""
    return (f"<b>{html.escape(a.symbol)}</b> · {a.timeframe} · last close {_num(a.price)} {unit}{live}{chg}\n"
            f"Last completed candle {when}{src}\n<b>Verdict: {verdict(a)}</b>")


def caption(a: Analysis) -> str:
    b = a.best()
    text = header(a)
    if b:
        text += (f"\nChart levels from <b>{html.escape(b.name)}</b> ({b.status}): entry {_num(b.entry)}, "
                 f"stop {_num(b.stop)}, target {_num(b.target)}")
    return text


def structure_text(a: Analysis) -> str:
    from bot.structure import ZONE_NAMES

    sn = a.snap
    if sn is None:
        return ""
    st = {1: "bullish (higher highs / higher lows)", -1: "bearish (lower highs / lower lows)"}.get(
        sn.structure, "not established yet")
    lines = ["<b>Market structure &amp; key levels</b>", f"Structure: {st}"]
    if sn.breaks:
        i, lvl, kind, d = sn.breaks[-1]
        ago = len(a.x) - 1 - i
        lines.append(f"Last break: {'bullish' if d > 0 else 'bearish'} {kind} through {_num(lvl)}, {ago} candle(s) ago")
    if sn.swings:
        lines.append("Recent swings: " + " → ".join(lab for _, _, lab in sn.swings[-6:]))
    if sn.resistances:
        lines.append("Resistance: " + ", ".join(f"{_num(p)} ({t} touch{'es' if t > 1 else ''})" for p, t in sn.resistances))
    if sn.supports:
        lines.append("Support: " + ", ".join(f"{_num(p)} ({t} touch{'es' if t > 1 else ''})" for p, t in sn.supports))
    if sn.round_levels:
        lines.append("Psychological levels: " + ", ".join(_num(v) for v in sn.round_levels))
    n = len(a.x) - 1
    from bot.structure import line_value

    if sn.tl_down:
        lines.append(f"Falling trend line now at {_num(line_value(sn.tl_down, n))} (break above = bullish)")
    if sn.tl_up:
        lines.append(f"Rising trend line now at {_num(line_value(sn.tl_up, n))} (break below = bearish)")
    for z in sorted(sn.zones, key=lambda z: -z.top):
        where = "above price" if z.bot > a.price else ("below price" if z.top < a.price else "price inside")
        lines.append(f"{ZONE_NAMES[z.kind].capitalize()}: {_num(z.bot)}–{_num(z.top)} ({where}, tested {z.tests}x)")
    return "\n".join(lines)


def flow_text(a: Analysis) -> str:
    f = a.flow
    if not f:
        if a.market == "stock":
            return ("<b>Live volume &amp; order book</b>\nNot available (needs Angel One secrets; "
                    "order book exists only during market hours).")
        return "<b>Live volume &amp; order book</b>\nNot available right now."
    unit = f.get("unit", "")
    lines = [f"<b>Live volume &amp; order book</b> ({html.escape(f.get('venue', ''))})",
             f"Reading: <b>{data.flow_verdict(f)}</b>"]
    if f.get("imbalance") is not None:
        if "tot_buy_qty" in f:
            lines.append(f"Pending buy vs sell quantity: {f['imbalance']:.2f}x "
                         f"({f['tot_buy_qty']:,.0f} vs {f['tot_sell_qty']:,.0f})")
        else:
            lines.append(f"Bids vs asks within ±1% of price: {f['imbalance']:.2f}x "
                         f"({f['bid_value_1pct']:,.0f} vs {f['ask_value_1pct']:,.0f} {unit})")
    if f.get("bid_wall"):
        p, q = f["bid_wall"]
        lines.append(f"Biggest buy wall: {_num(p)} (qty {q:,.4g})")
    if f.get("ask_wall"):
        p, q = f["ask_wall"]
        lines.append(f"Biggest sell wall: {_num(p)} (qty {q:,.4g})")
    if f.get("spread_pct") is not None:
        lines.append(f"Spread: {f['spread_pct']:.3f}%")
    if f.get("taker_buy_pct") is not None:
        lines.append(f"Last candle: {f['taker_buy_pct']:.0f}% of volume was aggressive buying")
    if f.get("volume_pace") is not None:
        lines.append(f"Volume pace right now: {f['volume_pace']:.2f}x normal")
    lines.append("<i>Order books change within seconds and big orders can be fake (spoofing); "
                 "use this as context, not as a signal on its own.</i>")
    return "\n".join(lines)


def full_text(a: Analysis, summary: str | None = None) -> str:
    parts = [header(a)]
    st = structure_text(a)
    if st:
        parts.append(st)
    if a.flow is not None or a.market == "stock" or a.source:
        parts.append(flow_text(a))
    for n, s in enumerate(a.setups, 1):
        lines = [f"<b>{n}. {html.escape(s.name)} — {ICON[s.status]} {s.status}</b>"
                 + (" (fresh signal)" if s.fresh else ""),
                 f"<i>{html.escape(s.idea)}</i>"]
        lines += [f"{'✅' if ok else '❌'} {html.escape(t)}" for ok, t in s.checks]
        if s.entry is not None:
            r = cfg()["risk"]["target_r"]
            prefix = "" if s.status == "TRADE" else "If it triggers now: "
            lines.append(f"{prefix}Entry {_num(s.entry)} · Stop {_num(s.stop)} · Target {_num(s.target)} ({r:g}R)")
            from bot.angel import INDEX_ALIASES

            if a.symbol in INDEX_ALIASES:
                lines.append(f"Index: not directly buyable. See /fno {a.symbol} for futures and options.")
            else:
                lines.append(position_size(s, a.market))
        lines.append(_bt_line(s, a))
        parts.append("\n".join(lines))
    if summary:
        parts.append(f"<b>AI summary</b> (Gemini, from the facts above)\n{html.escape(summary)}")
    parts.append("<i>Rules-based analysis for learning and paper trading, not financial advice. "
                 "Long (buy) setups only.</i>")
    return "\n\n".join(parts)


def facts(a: Analysis) -> dict:
    return {
        "symbol": a.symbol, "market": a.market, "timeframe": a.timeframe, "last_close": a.price,
        "verdict": verdict(a),
        "structure": structure_text(a).replace("<b>", "").replace("</b>", "").replace("&amp;", "&"),
        "order_flow": ({k: v for k, v in a.flow.items() if not isinstance(v, tuple)} | {"reading": data.flow_verdict(a.flow)})
                      if a.flow else None,
        "strategies": [
            {"name": s.name, "status": s.status,
             "failed_checks": [t for ok, t in s.checks if not ok],
             "entry": s.entry, "stop": s.stop, "target": s.target,
             "backtest": {k: s.backtest.get(k) for k in ("trades", "win_rate", "avg_r", "profit_factor")}}
            for s in a.setups
        ],
    }


def ranking_text(items: list[Analysis], title: str, errors: list[str], cmd_hint: str) -> str:
    items = sorted(items, key=lambda a: a.score, reverse=True)
    lines = [f"<b>{html.escape(title)}</b>",
             "Icons per strategy: EMA Cross · Breakout · RSI Pullback · MACD · S/R Bounce · Smart Money · "
             "Trendline/Round Breakout  (🟢 trade, 🟡 watch, ⚪ no)", ""]
    for n, a in enumerate(items, 1):
        icons = "".join(ICON[s.status] for s in a.setups)
        names = ", ".join(s.name for s in a.trades) or "—"
        chg = f" · 24h {a.change_pct:+.1f}%" if a.change_pct is not None else ""
        lines.append(f"{n}. <b>{html.escape(a.symbol)}</b> {icons} {_num(a.price)}{chg}\n    TRADE: {html.escape(names)}")
    if errors:
        lines.append("\nSkipped: " + html.escape(", ".join(errors)))
    lines.append(f"\nSend {cmd_hint} for the full step-by-step breakdown and chart.")
    return "\n".join(lines)

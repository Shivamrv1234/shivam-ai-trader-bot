"""Futures report: every live expiry, premium/discount (basis), cost of carry, open interest,
OI build-up, and the chart analysis translated into futures prices with risk per lot."""
from __future__ import annotations

import html
import io

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from bot import angel  # noqa: E402
from bot.config import cfg  # noqa: E402
from bot.fno import _lakh, _n, market_open_now  # noqa: E402

LINE = "━━━━━━━━━━━━━━━━━━"
TENOR = ["Near month", "Next month", "Far month"]

BUILDUP_MEANING = {
    "Long Built Up": ("🟢", "price ↑ and OI ↑ — new buying positions are being added (bullish reading)"),
    "Short Built Up": ("🔴", "price ↓ and OI ↑ — new selling positions are being added (bearish reading)"),
    "Short Covering": ("🟡", "price ↑ and OI ↓ — sellers are closing positions (rise may be short-lived)"),
    "Long Unwinding": ("🟠", "price ↓ and OI ↓ — buyers are closing positions (weakness, not fresh shorts)"),
}


def chart(rep: angel.FuturesReport) -> bytes | None:
    """Near-month future: daily close line with open-interest bars underneath."""
    c = rep.candles
    if c is None or len(c) < 5:
        return None
    c = c.tail(60)
    has_oi = rep.oi_hist is not None and len(rep.oi_hist) >= 2
    fig, axes = plt.subplots(2 if has_oi else 1, 1, figsize=(11, 6.5 if has_oi else 5), sharex=True,
                             gridspec_kw={"height_ratios": [3, 1]} if has_oi else None)
    ax = axes[0] if has_oi else axes
    ax.plot(c.index, c["close"], color="#2e86de", linewidth=1.8, label=f"{rep.legs[0].symbol} close")
    ax.fill_between(c.index, c["low"], c["high"], color="#2e86de", alpha=0.12, label="Day range")
    ax.axhline(rep.spot, color="#555", linestyle=":", linewidth=1, label=f"Spot now {rep.spot:,.2f}")
    ax.set_title(f"{rep.name} futures · near month · {rep.fetched_at}", fontsize=12, fontweight="bold")
    ax.grid(linestyle=":")
    ax.legend(loc="upper left", fontsize=9)
    if has_oi:
        oi = rep.oi_hist[rep.oi_hist.index >= c.index[0]]
        axes[1].bar(oi.index, oi.values, color="#8e44ad", width=0.7)
        axes[1].set_ylabel("Open interest")
        axes[1].yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: _lakh(v)))
        axes[1].grid(axis="y", linestyle=":")
    fig.autofmt_xdate()
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110)
    plt.close(fig)
    return buf.getvalue()


def _basis_reading(rep: angel.FuturesReport, leg: angel.FutLeg) -> str:
    bp = rep.basis_pct(leg)
    if bp >= 0.05:
        return "premium (futures above spot) — normal: it reflects the cost of carrying the position"
    if bp <= -0.05:
        extra = " or an upcoming dividend" if rep.kind == "stock" else ""
        return f"discount (futures below spot) — can reflect bearish positioning{extra}"
    return "flat (futures ≈ spot) — usual close to expiry"


def report_text(rep: angel.FuturesReport) -> str:
    status = "live" if market_open_now() else "market closed: last traded values"
    lines = [f"📈 <b>{html.escape(rep.name)} · FUTURES REPORT</b>",
             f"<i>{rep.fetched_at} · {status} · Angel One</i>", LINE,
             f"💹 Spot <b>{_n(rep.spot)}</b> ({rep.spot_change_pct:+.2f}%)", ""]
    for i, leg in enumerate(rep.legs):
        carry = rep.carry_annual_pct(leg)
        carry_txt = f" · ≈ {carry:+.1f}%/yr carry" if carry is not None else ""
        lines.append(f"<b>{TENOR[i] if i < len(TENOR) else 'Later'}</b> · expiry {leg.expiry:%d %b} "
                     f"({rep.days_left(leg)} days)")
        lines.append(f"   Price <b>{_n(leg.ltp)}</b> ({leg.change_pct:+.2f}%) · day {_n(leg.low)}–{_n(leg.high)}")
        lines.append(f"   Basis {rep.basis(leg):+,.2f} ({rep.basis_pct(leg):+.2f}%){carry_txt}")
        lines.append(f"   OI {_lakh(leg.oi)} · volume {_lakh(leg.volume)} · lot {leg.lot} · "
                     f"1 lot ≈ ₹{_lakh(leg.ltp * leg.lot)}")
        lines.append("")
    near = rep.legs[0]
    lines.append("🔎 <b>WHAT IT SAYS</b>")
    lines.append(f"• Near month at a {_basis_reading(rep, near)}.")
    if len(rep.legs) > 1 and rep.legs[1].ltp:
        spread = rep.legs[1].ltp - near.ltp
        lines.append(f"• Next − near month spread {spread:+,.2f}: the cost of rolling 1 unit to next month.")
    if near.buildup in BUILDUP_MEANING:
        icon, meaning = BUILDUP_MEANING[near.buildup]
        oi_txt = f" (OI {near.oi_change_pct:+.1f}% vs previous day)" if near.oi_change_pct is not None else ""
        lines.append(f"• {icon} <b>{near.buildup}</b>{oi_txt}: {meaning}.")
    elif near.oi_change_pct is not None:
        lines.append(f"• OI {near.oi_change_pct:+.1f}% vs previous day; price change too small to classify.")
    else:
        lines.append("• OI build-up: not in Angel One's top build-up lists today (no strong positioning signal).")
    if near.volume and near.oi:
        turn = near.volume / near.oi
        lines.append(f"• Volume / OI = {turn:.2f} → "
                     + ("heavy intraday trading compared with positions held" if turn > 1 else
                        "most positions are being carried, not day-traded"))
    lines.append("<i>Margin is a fraction of the notional value; check Angel One's margin calculator. "
                 "Futures losses are not limited to the margin.</i>")
    return "\n".join(lines)


def trade_plan(rep: angel.FuturesReport, a) -> str:
    """The chart analysis of the underlying (report.Analysis), shifted into near-month futures prices."""
    from bot import report

    near = rep.legs[0]
    s = a.best()
    head = f"🧭 <b>FUTURES TRADE PLAN</b> (from the daily chart of {html.escape(rep.name)})\n" \
           f"{html.escape(report.verdict(a))}"
    if s is None or a.direction not in ("BUY", "SELL", "FORMING"):
        return head + "\n⚪ No BUY or SELL setup on the chart, so no futures trade. Waiting is a position too."
    side = s.side
    shift = rep.basis(near)
    entry, stop, target = s.entry + shift, s.stop + shift, s.target + shift
    risk_lot = abs(s.entry - s.stop) * near.lot
    reward_lot = abs(s.target - s.entry) * near.lot
    from bot import risk
    action = "BUY (go long)" if side == "BUY" else "SELL (go short)"
    pre = "If it triggers: " if a.direction == "FORMING" else ""
    lines = [head, LINE,
             f"{pre}{'🟢' if side == 'BUY' else '🔴'} <b>{action} {html.escape(near.symbol)}</b>",
             f"🎯 Entry ≈ <b>{_n(entry)}</b> (spot {_n(s.entry)} + basis {shift:+,.2f})",
             f"💰 Take profit ≈ <b>{_n(target)}</b>",
             f"🛑 Stop-loss ≈ <b>{_n(stop)}</b>",
             f"📦 1 lot = {near.lot} units · risk ≈ ₹{risk_lot:,.0f} · reward ≈ ₹{reward_lot:,.0f}",
             risk.fits_text(risk_lot, "INR").replace("Max loss", "1-lot loss at the stop")]
    if risk_lot > risk.max_risk("INR"):
        lines.append("💡 A defined-risk option trade needs far less (tap 🧠 F&amp;O plan).")
    lines.append(f"Levels from: {html.escape(s.name)} · basis stays roughly constant until expiry.")
    lines.append("<i>Educational plan, not a recommendation. Not financial advice.</i>")
    return "\n".join(lines)


def buildup_text(lists: dict[str, list[dict]]) -> str:
    lines = ["🔥 <b>F&amp;O OI BUILD-UP TODAY</b> (near-month futures, Angel One)", LINE]
    any_rows = False
    for kind, rows in lists.items():
        icon, meaning = BUILDUP_MEANING[kind]
        lines.append(f"{icon} <b>{kind}</b> — {meaning}")
        if not rows:
            lines.append("   (no data)")
        for r in rows:
            any_rows = True
            sym = html.escape(str(angel.pick(r, "tradingSymbol", default="?")))
            pc = angel.num(angel.pick(r, "percentChange"))
            lines.append(f"   • {sym} · {_n(angel.num(angel.pick(r, 'ltp')))} ({pc:+.2f}%)")
        lines.append("")
    if not any_rows:
        lines.append("Angel One returned no build-up data (normal outside market hours).")
    lines.append("<i>Tap a stock below, or send /futures SYMBOL, for its full futures report.</i>")
    return "\n".join(lines)


def buildup_symbols(lists: dict[str, list[dict]], inst) -> list[str]:
    """Underlying names (e.g. TATASTEEL) of the contracts in the lists, for buttons."""
    names = []
    if inst is None:
        return names
    by_sym = dict(zip(inst["symbol_u"], inst["name_u"]))
    by_tok = dict(zip(inst["token"].astype(str), inst["name_u"]))
    for rows in lists.values():
        for r in rows:
            n = (by_sym.get(str(angel.pick(r, "tradingSymbol", default="")).upper())
                 or by_tok.get(str(angel.pick(r, "symbolToken", default=""))))
            if n and n not in names:
                names.append(n)
    return names

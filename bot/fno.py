"""F&O report: option chain summary, OI chart, and strategy view on the underlying."""
from __future__ import annotations

import html
import io
from datetime import datetime

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from bot import angel  # noqa: E402

IST = angel.IST


def market_open_now() -> bool:
    now = datetime.now(IST)
    return now.weekday() < 5 and (9, 15) <= (now.hour, now.minute) <= (15, 30)


def _n(v: float | None, d: int = 2) -> str:
    if v is None:
        return "–"
    return f"{v:,.{d}f}"


def _rs(v: float) -> str:
    """Signed rupees: +₹15,000 / -₹7,496."""
    return f"{'+' if v >= 0 else '-'}₹{abs(v):,.0f}"


def _lakh(v: float) -> str:
    if v >= 1e7:
        return f"{v / 1e7:.2f}Cr"
    if v >= 1e5:
        return f"{v / 1e5:.2f}L"
    return f"{v:,.0f}"


def oi_chart(s: angel.FnoSnapshot) -> bytes:
    ce = {l.strike: l.oi for l in s.side("CE")}
    pe = {l.strike: l.oi for l in s.side("PE")}
    ks = s.strikes
    width = (ks[1] - ks[0]) * 0.38 if len(ks) > 1 else 1
    fig, ax = plt.subplots(figsize=(11, 6))
    ax.bar([k - width / 2 for k in ks], [ce.get(k, 0) for k in ks], width, color="#e74c3c", label="Call OI (CE)")
    ax.bar([k + width / 2 for k in ks], [pe.get(k, 0) for k in ks], width, color="#27ae60", label="Put OI (PE)")
    ax.axvline(s.spot, color="#2e86de", linewidth=1.8, label=f"Spot {s.spot:,.2f}")
    if s.max_pain:
        ax.axvline(s.max_pain, color="#8e44ad", linestyle="--", linewidth=1.2, label=f"Max pain {s.max_pain:,.0f}")
    ax.set_title(f"{s.name} option open interest · expiry {s.expiry:%d %b %Y} · PCR {s.pcr} · {s.fetched_at}",
                 fontsize=12, fontweight="bold")
    ax.set_xlabel("Strike")
    ax.set_ylabel("Open interest")
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: _lakh(v)))
    ax.grid(axis="y", linestyle=":")
    ax.legend(loc="upper right", fontsize=9)
    plt.xticks(ks[:: max(1, len(ks) // 12)], rotation=45)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110)
    plt.close(fig)
    return buf.getvalue()


def chain_table(s: angel.FnoSnapshot, rows_each_side: int = 4) -> str:
    ks = s.strikes
    i = ks.index(s.atm)
    near = ks[max(0, i - rows_each_side): i + rows_each_side + 1]
    ce = {l.strike: l for l in s.side("CE")}
    pe = {l.strike: l for l in s.side("PE")}
    lines = [f"{'CE OI':>8} {'CE LTP':>8} │{'STRIKE':^9}│ {'PE LTP':<8} {'PE OI':<8}"]
    for k in near:
        c, p = ce.get(k), pe.get(k)
        mark = "*" if k == s.atm else " "
        lines.append(f"{_lakh(c.oi) if c else '–':>8} {_n(c.ltp) if c else '–':>8} │"
                     f"{mark}{k:^7,.0f}{mark}│ {_n(p.ltp) if p else '–':<8} {_lakh(p.oi) if p else '–':<8}")
    return "<pre>" + html.escape("\n".join(lines)) + "</pre>"


def summary_text(s: angel.FnoSnapshot) -> str:
    status = "live" if market_open_now() else "market closed: last traded values"
    ce_max, pe_max = s.max_oi("CE"), s.max_oi("PE")
    atm_ce = next((l for l in s.side("CE") if l.strike == s.atm), None)
    atm_pe = next((l for l in s.side("PE") if l.strike == s.atm), None)
    lines = [
        f"📊 <b>{html.escape(s.name)} · F&amp;O REPORT</b>",
        f"<i>{s.fetched_at} · {status} · Angel One</i>",
        "━━━━━━━━━━━━━━━━━━",
        f"💹 Spot <b>{_n(s.spot)}</b> ({s.spot_change_pct:+.2f}%) · expiry {s.expiry:%d %b %Y} · lot {s.lot}",
    ]
    if s.fut_ltp:
        lines.append(f"Future {html.escape(s.fut_symbol or '')}: {_n(s.fut_ltp)} · basis {s.basis:+,.2f} · "
                     f"OI {_lakh(s.fut_oi or 0)} · 1 lot ≈ ₹{_lakh(s.fut_ltp * s.lot)} notional (margin: check Angel One)")
    lines.append("")
    lines.append("🔎 <b>OPTION CHAIN READING</b> (nearest expiry, strikes around ATM)")
    if s.pcr is not None:
        if s.pcr >= 1.05:
            tone = "more put OI than call OI — often read as put writers defending lower levels"
        elif s.pcr <= 0.95:
            tone = "more call OI than put OI — often read as call writers capping the upside"
        else:
            tone = "put and call OI roughly balanced — no clear tilt from writers"
        lines.append(f"• PCR (OI) {s.pcr}: {tone}")
    if ce_max:
        lines.append(f"• Highest call OI at {ce_max.strike:,.0f} ({_lakh(ce_max.oi)}) → commonly watched as resistance")
    if pe_max:
        lines.append(f"• Highest put OI at {pe_max.strike:,.0f} ({_lakh(pe_max.oi)}) → commonly watched as support")
    if s.max_pain:
        lines.append(f"• Max pain {s.max_pain:,.0f} (spot is {s.spot - s.max_pain:+,.0f} away)")
    for leg in (atm_ce, atm_pe):
        if leg:
            greeks = []
            if leg.iv is not None:
                greeks.append(f"IV {leg.iv:.1f}%")
            if leg.delta is not None:
                greeks.append(f"delta {leg.delta:+.2f}")
            if leg.theta is not None:
                greeks.append(f"theta {leg.theta:.2f}/day")
            per_lot = leg.ltp * s.lot
            lines.append(f"• ATM {leg.strike:,.0f} {leg.kind}: ₹{_n(leg.ltp)} · "
                         + (", ".join(greeks) + " · " if greeks else "")
                         + f"1 lot ≈ ₹{_n(per_lot, 0)}")
    if not any(l.iv is not None for l in s.legs):
        lines.append("• Greeks unavailable right now (Angel One returns them during market hours)")
    lines.append("")
    lines.append(chain_table(s))
    return "\n".join(lines)


def option_idea(s: angel.FnoSnapshot, a) -> str:
    """Defined-risk option idea from the chart analysis (report.Analysis) of the underlying:
    BUY setup -> ATM call, SELL setup -> ATM put. Premium moves are estimated with delta only."""
    from bot import report

    head = f"🎯 <b>OPTION TRADE IDEA</b> (from the daily chart)\n{html.escape(report.verdict(a))}"
    best = a.best()
    if best is None or a.direction not in ("BUY", "SELL", "FORMING"):
        return (head + "\n⚪ No BUY or SELL setup on the chart, so no option idea. Buying options without a "
                "setup mostly pays time decay.")
    kind = "CE" if best.side == "BUY" else "PE"
    leg = next((l for l in s.side(kind) if l.strike == s.atm), None)
    if leg is None or not leg.ltp:
        return head + f"\nNo live ATM {kind} price right now."
    delta = abs(leg.delta) if leg.delta is not None else 0.5
    move_tp = abs(best.target - best.entry)
    move_sl = abs(best.entry - best.stop)
    at_tp = leg.ltp + delta * move_tp
    at_sl = max(leg.ltp - delta * move_sl, 0.05)
    cost = leg.ltp * s.lot
    days = max((s.expiry - datetime.now(IST).date()).days, 0)
    pre = "If it triggers: " if a.direction == "FORMING" else ""
    lines = [head, "━━━━━━━━━━━━━━━━━━",
             f"{pre}{'🟢' if kind == 'CE' else '🔴'} <b>BUY {html.escape(s.name)} {s.atm:,.0f} {kind}</b> "
             f"(expiry {s.expiry:%d %b}, {days} days)",
             f"💵 Premium ₹{_n(leg.ltp)} × lot {s.lot} = <b>₹{cost:,.0f}</b> per lot",
             f"💰 If {html.escape(s.name)} reaches take profit {_n(best.target)}: premium ≈ ₹{_n(at_tp)} "
             f"(≈ {_rs((at_tp - leg.ltp) * s.lot)} per lot)",
             f"🛑 If it hits the stop {_n(best.stop)}: premium ≈ ₹{_n(at_sl)} → exit "
             f"(≈ {_rs((at_sl - leg.ltp) * s.lot)} per lot)",
             f"🧯 Worst case: the whole premium ₹{cost:,.0f} if it expires worthless."]
    if leg.theta is not None:
        lines.append(f"⏳ Time decay ≈ ₹{abs(leg.theta) * s.lot:,.0f} per lot per day (theta {leg.theta:.2f}).")
    lines.append(f"Estimate uses delta {delta:.2f}" + ("" if leg.delta is not None else " (assumed for ATM)")
                 + "; it ignores time decay and IV changes, so real premiums will differ.")
    if days <= 2:
        lines.append("⚠️ Expiry is very close: premiums can collapse within hours.")
    from bot import risk
    lines.append(risk.fits_text(cost, "INR").replace("Max loss", "Worst-case loss"))
    lines.append("<i>Options can lose 100% of the premium quickly. Paper-trade first; not financial advice.</i>")
    return "\n".join(lines)


def mapping_text(verdict_line: str, bullish: list[str], bearish: list[str] | None = None) -> str:
    bearish = bearish or []
    lines = [f"🧭 <b>STRATEGY VIEW</b> (daily candles of the underlying)\n{html.escape(verdict_line)}"]
    if bullish:
        lines.append("🟢 BUY setups: " + html.escape(", ".join(bullish)) + ".\n"
                     "Typical bullish F&amp;O expressions (educational): long future (large margin, big losses "
                     "possible), buy an ATM/near call (loss limited to the premium, decays daily), or a bull call "
                     "spread (cheaper, capped profit).")
    if bearish:
        lines.append("🔴 SELL setups: " + html.escape(", ".join(bearish)) + ".\n"
                     "Typical bearish F&amp;O expressions (educational): short future (large margin, big losses "
                     "possible), buy an ATM/near put (loss limited to the premium), or a bear put spread.")
    if bullish and bearish:
        lines.append("⚖️ Strategies disagree — standing aside is a valid choice.")
    if not bullish and not bearish:
        lines.append("⚪ No BUY or SELL setup on the underlying right now, so there's no directional F&amp;O idea.")
    else:
        lines.append("Use the underlying's stop-loss from the chart analysis as your exit level.")
    from bot import risk
    lines.append(risk.fits_text(cost, "INR").replace("Max loss", "Worst-case loss"))
    lines.append("<i>Options can lose 100% of the premium quickly. Paper-trade first; not financial advice.</i>")
    return "\n\n".join(lines)

"""F&O report: option chain summary, OI chart, and strategy view on the underlying."""
from __future__ import annotations

import html
import io
from datetime import datetime

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from bot import angel  # noqa: E402
from bot.config import capital  # noqa: E402

IST = angel.IST


def market_open_now() -> bool:
    now = datetime.now(IST)
    return now.weekday() < 5 and (9, 15) <= (now.hour, now.minute) <= (15, 30)


def _n(v: float | None, d: int = 2) -> str:
    if v is None:
        return "–"
    return f"{v:,.{d}f}"


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
    cap, _ = capital("stock")
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
            afford = "fits" if per_lot and per_lot <= cap else "exceeds"
            lines.append(f"• ATM {leg.strike:,.0f} {leg.kind}: ₹{_n(leg.ltp)} · "
                         + (", ".join(greeks) + " · " if greeks else "")
                         + f"1 lot ≈ ₹{_n(per_lot, 0)} ({afford} your ₹{cap:,.0f} capital)")
    if not any(l.iv is not None for l in s.legs):
        lines.append("• Greeks unavailable right now (Angel One returns them during market hours)")
    lines.append("")
    lines.append(chain_table(s))
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
    lines.append("<i>Options can lose 100% of the premium quickly. Paper-trade first; not financial advice.</i>")
    return "\n\n".join(lines)

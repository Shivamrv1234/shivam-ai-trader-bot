"""Option Greeks per strike: delta, gamma, theta, vega and implied volatility.

Angel One's optionGreek API returns them for the nearest expiry during market hours. When a value is
missing (outside market hours, or a strike Angel One skipped), it is computed here with the
Black-Scholes model from the live premium: implied volatility is solved from the option's last price,
then the Greeks follow from it. Units (same as Angel One / NSE practice):
  delta  change in premium per 1-point move of the underlying (calls 0..1, puts -1..0)
  gamma  change in delta per 1-point move
  theta  premium lost per calendar day (negative for buyers)
  vega   premium change per 1 percentage point change in IV
  IV     % a year
"""
from __future__ import annotations

import html
import io
import math
from datetime import datetime, time as dtime

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from bot import angel  # noqa: E402
from bot.config import cfg  # noqa: E402

IST = angel.IST
LINE = "━━━━━━━━━━━━━━━━━━"


def _ncdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _npdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def years_to_expiry(expiry, now: datetime | None = None) -> float:
    """Time to 15:30 IST on expiry day, in years (at least ~1 hour so the maths stays finite)."""
    now = now or datetime.now(IST)
    exp_dt = datetime.combine(expiry, dtime(15, 30), tzinfo=IST)
    return max((exp_dt - now).total_seconds(), 3600) / (365 * 86400)


def bs_price(S: float, K: float, T: float, r: float, sigma: float, kind: str) -> float:
    if sigma <= 0 or T <= 0:
        return max(0.0, S - K) if kind == "CE" else max(0.0, K - S)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if kind == "CE":
        return S * _ncdf(d1) - K * math.exp(-r * T) * _ncdf(d2)
    return K * math.exp(-r * T) * _ncdf(-d2) - S * _ncdf(-d1)


def implied_vol(price: float, S: float, K: float, T: float, r: float, kind: str) -> float | None:
    """Bisection between 1% and 400% a year; None if the premium is below intrinsic value."""
    if price <= 0 or S <= 0 or K <= 0:
        return None
    intrinsic = max(0.0, S - K * math.exp(-r * T)) if kind == "CE" else max(0.0, K * math.exp(-r * T) - S)
    if price < intrinsic - 1e-6:
        return None
    lo, hi = 0.01, 4.0
    if bs_price(S, K, T, r, hi, kind) < price:
        return None
    for _ in range(80):
        mid = (lo + hi) / 2
        if bs_price(S, K, T, r, mid, kind) > price:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def bs_greeks(S: float, K: float, T: float, r: float, sigma: float, kind: str) -> dict:
    sq = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * sq)
    d2 = d1 - sigma * sq
    pdf = _npdf(d1)
    gamma = pdf / (S * sigma * sq)
    vega = S * pdf * sq / 100
    if kind == "CE":
        delta = _ncdf(d1)
        theta = (-S * pdf * sigma / (2 * sq) - r * K * math.exp(-r * T) * _ncdf(d2)) / 365
    else:
        delta = _ncdf(d1) - 1
        theta = (-S * pdf * sigma / (2 * sq) + r * K * math.exp(-r * T) * _ncdf(-d2)) / 365
    return {"delta": delta, "gamma": gamma, "theta": theta, "vega": vega, "iv": sigma * 100}


def fill(snap: angel.FnoSnapshot, now: datetime | None = None) -> int:
    """Compute any missing Greek on every leg. Returns how many legs were computed."""
    r = float(cfg()["fno"].get("risk_free_rate", 0.065))
    T = years_to_expiry(snap.expiry, now)
    S = snap.fut_ltp * math.exp(-r * T) if snap.fut_ltp else snap.spot   # futures-implied forward, if known
    S = S or snap.spot
    n = 0
    for leg in snap.legs:
        if None not in (leg.delta, leg.gamma, leg.theta, leg.vega, leg.iv):
            continue
        sigma = leg.iv / 100 if leg.iv else implied_vol(leg.ltp, S, leg.strike, T, r, leg.kind)
        if not sigma:
            continue
        g = bs_greeks(S, leg.strike, T, r, sigma, leg.kind)
        for k, v in g.items():
            if getattr(leg, k) is None:
                setattr(leg, k, v)
        if not leg.greeks_src:
            leg.greeks_src = "computed"
        n += 1
    return n


def _g(v, d=2) -> str:
    return "–" if v is None else f"{v:.{d}f}"


def table(snap: angel.FnoSnapshot, kind: str, rows_each_side: int = 6) -> str:
    ks = snap.strikes
    if not ks:
        return ""
    i = ks.index(snap.atm)
    near = ks[max(0, i - rows_each_side): i + rows_each_side + 1]
    legs = {l.strike: l for l in snap.side(kind)}
    head = f"{'Strike':>7} {'LTP':>7} {'IV':>5} {'Δ':>5} {'Γ':>7} {'Θ':>6} {'Vega':>5}"
    out = [head]
    for k in near:
        l = legs.get(k)
        mark = "*" if k == snap.atm else " "
        if not l:
            continue
        out.append(f"{k:>6,.0f}{mark} {l.ltp:>7.1f} {_g(l.iv, 1):>5} {_g(l.delta):>5} "
                   f"{_g(l.gamma, 4):>7} {_g(l.theta, 1):>6} {_g(l.vega, 1):>5}")
    return "<pre>" + html.escape("\n".join(out)) + "</pre>"


def report_text(snap: angel.FnoSnapshot) -> str:
    computed = sum(l.greeks_src == "computed" for l in snap.legs)
    src = "Angel One" if not computed else (
        "computed with Black-Scholes from live premiums" if computed == len(snap.legs)
        else f"Angel One + {computed} legs computed (Black-Scholes)")
    atm_ce = next((l for l in snap.side("CE") if l.strike == snap.atm), None)
    atm_pe = next((l for l in snap.side("PE") if l.strike == snap.atm), None)
    days = max((snap.expiry - datetime.now(IST).date()).days, 0)
    lines = [f"🧮 <b>{html.escape(snap.name)} · OPTION GREEKS</b> (per strike)",
             f"<i>{snap.fetched_at} · expiry {snap.expiry:%d %b} ({days} days) · {html.escape(src)}</i>", LINE,
             f"Spot <b>{snap.spot:,.2f}</b> · ATM {snap.atm:,.0f} (*) · lot {snap.lot}"]
    for leg in (atm_ce, atm_pe):
        if leg and leg.delta is not None:
            lines.append(
                f"• ATM {leg.kind}: Δ {leg.delta:+.2f} · Γ {_g(leg.gamma, 4)} · Θ {_g(leg.theta, 1)}/day "
                f"(₹{abs(leg.theta or 0) * snap.lot:,.0f}/lot) · vega {_g(leg.vega, 1)} "
                f"(₹{(leg.vega or 0) * snap.lot:,.0f}/lot per 1% IV) · IV {_g(leg.iv, 1)}%")
    lines += ["", "🟢 <b>CALLS (CE)</b>", table(snap, "CE"), "🔴 <b>PUTS (PE)</b>", table(snap, "PE")]
    skew = _skew(snap)
    if skew:
        lines.append(skew)
    lines += ["", "<b>HOW TO READ</b>",
              "Δ delta = premium change per 1-point move (also ≈ chance of expiring in the money)",
              "Γ gamma = how fast delta changes — highest at the money, and near expiry",
              "Θ theta = premium lost per day (option buyers pay it, sellers earn it)",
              "Vega = premium change if IV moves 1 point · IV = expected yearly volatility",
              "<i>Greeks change every second with price, time and IV. Not financial advice.</i>"]
    return "\n".join(lines)


def _skew(snap: angel.FnoSnapshot) -> str:
    """Put IV vs call IV a few strikes out: puts richer = traders paying up for downside protection."""
    ks = snap.strikes
    if len(ks) < 7:
        return ""
    i = ks.index(snap.atm)
    up, dn = ks[min(i + 3, len(ks) - 1)], ks[max(i - 3, 0)]
    ce = next((l for l in snap.side("CE") if l.strike == up), None)
    pe = next((l for l in snap.side("PE") if l.strike == dn), None)
    if not (ce and pe and ce.iv and pe.iv):
        return ""
    diff = pe.iv - ce.iv
    tone = ("🔴 puts pricier: traders hedging the downside" if diff > 1.5 else
            "🟢 calls pricier: traders chasing the upside" if diff < -1.5 else "⚪ balanced")
    return f"IV skew: {dn:,.0f} PE {pe.iv:.1f}% vs {up:,.0f} CE {ce.iv:.1f}% → {tone}"


def chart(snap: angel.FnoSnapshot) -> bytes | None:
    ce, pe = snap.side("CE"), snap.side("PE")
    if not any(l.delta is not None for l in snap.legs):
        return None
    fig, axes = plt.subplots(2, 2, figsize=(11, 7.5), sharex=True)
    spec = [("delta", "Delta", axes[0][0]), ("gamma", "Gamma", axes[0][1]), ("theta", "Theta (per day)", axes[1][0]),
            ("iv", "Implied volatility %", axes[1][1])]
    for attr, title, ax in spec:
        for legs, col, lbl in ((ce, "#27ae60", "CE"), (pe, "#e74c3c", "PE")):
            pts = [(l.strike, getattr(l, attr)) for l in legs if getattr(l, attr) is not None]
            if pts:
                ax.plot([p[0] for p in pts], [p[1] for p in pts], marker="o", markersize=3, color=col, label=lbl)
        ax.axvline(snap.spot, color="#2e86de", linestyle="--", linewidth=1)
        ax.set_title(title, fontsize=10, fontweight="bold")
        ax.grid(linestyle=":")
    axes[0][0].legend(fontsize=8)
    fig.suptitle(f"{snap.name} Greeks by strike · expiry {snap.expiry:%d %b %Y} · spot {snap.spot:,.0f} · "
                 f"{snap.fetched_at}", fontsize=12, fontweight="bold")
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=105)
    plt.close(fig)
    return buf.getvalue()

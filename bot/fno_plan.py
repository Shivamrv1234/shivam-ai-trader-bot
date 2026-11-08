"""F&O trade planner: option buying, option selling (defined risk) and hedging, from the live chain.

Inputs: the live option chain (premiums, OI, Greeks), the chart analysis of the underlying (BUY / SELL /
range), volatility (India VIX for indices, option IV vs historical volatility for stocks) and the
days to expiry. Decision table (educational, rules-based):

  chart          volatility   plan
  BUY / SELL     low/normal   BUY an ATM option (call / put) — loss limited to the premium
  BUY / SELL     high         credit spread in the chart's direction (bull put / bear call) — defined risk
  no setup/range high/normal  IRON CONDOR: sell ~16-delta call + put, buy wings further out
  no setup/range low          stand aside (premiums too small for the risk); the condor is shown for reference
Hedging is always offered: protective put (or call for shorts) and a collar for holders.

Exit rules follow the common playbook: take 50% of the maximum profit; stop at a loss of 2x the credit
(credit trades) or 50% of the premium / the chart stop (bought options); close before expiry day.
Everything is per lot, with the live lot size. Margin is not computed (check Angel One's calculator).
"""
from __future__ import annotations

import html
import io
import math
from dataclasses import dataclass, field
from datetime import datetime

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

from bot import angel, greeks  # noqa: E402
from bot.config import capital, cfg  # noqa: E402

IST = angel.IST
LINE = "━━━━━━━━━━━━━━━━━━"


@dataclass
class LegPlan:
    action: str       # BUY / SELL
    kind: str         # CE / PE
    strike: float
    premium: float
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    iv: float | None = None

    @property
    def sign(self) -> int:
        return 1 if self.action == "BUY" else -1


@dataclass
class Plan:
    key: str                  # buy / sell / hedge / condor / credit / wait
    title: str
    why: list = field(default_factory=list)
    legs: list = field(default_factory=list)
    lot: int = 1
    spot: float = 0.0
    notes: list = field(default_factory=list)
    exit_rules: list = field(default_factory=list)
    iv: float | None = None
    days: int = 0

    @property
    def net(self) -> float:
        """Premium paid (+) or received (-) per unit."""
        return sum(l.sign * l.premium for l in self.legs)

    def payoff(self, s: float) -> float:
        """Profit/loss per lot at expiry if the underlying ends at s."""
        val = 0.0
        for l in self.legs:
            intrinsic = max(0.0, s - l.strike) if l.kind == "CE" else max(0.0, l.strike - s)
            val += l.sign * (intrinsic - l.premium)
        return val * self.lot

    def grid(self) -> np.ndarray:
        ks = [l.strike for l in self.legs] + [self.spot]
        lo, hi = min(ks), max(ks)
        pad = max((hi - lo) * 0.6, self.spot * 0.04)
        return np.linspace(lo - pad, hi + pad, 600)

    def _values(self) -> list[float]:
        """Payoff on the grid plus at zero (a long put's best case is the underlying going to 0)."""
        return [self.payoff(s) for s in self.grid()] + [self.payoff(0.0)]

    def max_profit(self) -> float | None:
        if self._unbounded(up=True):
            return None
        return max(self._values())

    def max_loss(self) -> float | None:
        if self._unbounded(up=False):
            return None
        return min(self._values())

    def _unbounded(self, up: bool) -> bool:
        """Net calls bought (sold) -> unlimited profit (loss) to the upside."""
        calls = sum(l.sign for l in self.legs if l.kind == "CE")
        return calls > 0 if up else calls < 0

    def breakevens(self) -> list[float]:
        g = self.grid()
        v = np.array([self.payoff(s) for s in g])
        out = []
        for i in range(1, len(g)):
            if (v[i - 1] < 0) != (v[i] < 0):
                out.append(float(g[i - 1] + (g[i] - g[i - 1]) * (-v[i - 1]) / (v[i] - v[i - 1])))
        return out

    def net_greeks(self) -> dict:
        tot = {}
        for k in ("delta", "gamma", "theta", "vega"):
            vals = [getattr(l, k) for l in self.legs]
            if any(v is None for v in vals):
                tot[k] = None
            else:
                tot[k] = sum(l.sign * getattr(l, k) for l in self.legs) * self.lot
        return tot

    def pop(self) -> float | None:
        """Chance of a profit at expiry, from a lognormal model with the ATM IV (rough)."""
        if not self.iv or self.days < 0:
            return None
        T = max(self.days, 0.5) / 365
        sig = self.iv / 100 * math.sqrt(T)
        if sig <= 0:
            return None
        g = self.grid()
        prof = np.array([self.payoff(s) > 0 for s in g])
        # probability mass of each grid cell under the lognormal
        z = (np.log(g / self.spot) + 0.5 * sig * sig) / sig
        cdf = 0.5 * (1 + np.vectorize(math.erf)(z / math.sqrt(2)))
        mass = np.diff(np.r_[0.0, cdf])
        mass[-1] += 1 - cdf[-1]
        return float((mass * prof).sum() * 100)


# ---------------------------------------------------------------- picking strikes

def _leg(snap, kind: str, strike: float, action: str) -> LegPlan | None:
    l = next((x for x in snap.side(kind) if x.strike == strike), None)
    if l is None or not l.ltp:
        return None
    return LegPlan(action, kind, strike, l.ltp, l.delta, l.gamma, l.theta, l.vega, l.iv)


def _by_delta(snap, kind: str, target: float, room: int = 0) -> float | None:
    """OTM strike whose |delta| is closest to target, leaving `room` strikes beyond it for a wing."""
    ks = snap.strikes
    ok = set(ks[room: len(ks) - room]) if room and len(ks) > 2 * room else set(ks)
    legs = [l for l in snap.side(kind) if l.delta is not None and l.ltp > 0 and l.strike in ok]
    otm = [l for l in legs if (l.strike > snap.spot if kind == "CE" else l.strike < snap.spot)]
    pool = otm or legs
    if not pool:
        return None
    return min(pool, key=lambda l: abs(abs(l.delta) - target)).strike


def _shift(snap, strike: float, steps: int) -> float:
    ks = snap.strikes
    i = ks.index(strike)
    return ks[max(0, min(len(ks) - 1, i + steps))]


# ---------------------------------------------------------------- volatility reading

@dataclass
class VolView:
    level: str           # low / normal / high
    text: str
    iv: float | None


def vol_view(snap, a=None) -> VolView:
    from bot import vol

    atm_ce = next((l for l in snap.side("CE") if l.strike == snap.atm), None)
    atm_pe = next((l for l in snap.side("PE") if l.strike == snap.atm), None)
    ivs = [l.iv for l in (atm_ce, atm_pe) if l and l.iv]
    iv = sum(ivs) / len(ivs) if ivs else None
    if snap.kind == "index":
        v = vol.india_vix()
        if v is not None:
            pct = v.percentile
            lvl = "high" if v.value >= 18 or (pct is not None and pct >= 60) else (
                "low" if v.value < 12.5 and (pct is None or pct < 35) else "normal")
            extra = f", higher than {pct:.0f}% of the last year" if pct is not None else ""
            return VolView(lvl, f"India VIX {v.value:.2f} {v.icon} {v.label}{extra}", iv)
    if iv and a is not None:
        hv = vol.annual_hv(a.x["close"], 252, 20) if a.timeframe == "1d" else None
        if hv:
            ratio = iv / hv
            lvl = "high" if ratio >= 1.25 else "low" if ratio <= 0.9 else "normal"
            return VolView(lvl, f"ATM IV {iv:.1f}% vs HV20 {hv:.1f}% ({ratio:.2f}×)", iv)
    if iv:
        lvl = "high" if iv >= 25 else "low" if iv < 13 else "normal"
        return VolView(lvl, f"ATM IV {iv:.1f}%", iv)
    return VolView("normal", "volatility unknown (no IV right now)", None)


# ---------------------------------------------------------------- the plans

def _base(snap, key, title, why, vv: VolView) -> Plan:
    days = max((snap.expiry - datetime.now(IST).date()).days, 0)
    return Plan(key, title, why, lot=snap.lot, spot=snap.spot, iv=vv.iv, days=days)


def plan_buy(snap, a, vv: VolView) -> Plan:
    best = a.best() if a is not None else None
    side = best.side if best is not None else ("BUY" if (snap.pcr or 1) >= 1 else "SELL")
    no_setup = best is None
    kind = "CE" if side == "BUY" else "PE"
    why = [f"Chart: {html.escape(_verdict(a))}", f"Volatility: {html.escape(vv.text)}"]
    p = _base(snap, "buy", f"⚡ OPTION BUYING · BUY {snap.name} {snap.atm:,.0f} {kind}", why, vv)
    leg = _leg(snap, kind, snap.atm, "BUY")
    if leg:
        p.legs = [leg]
    if best is not None and best.entry:
        d = abs(leg.delta) if leg and leg.delta is not None else 0.5
        at_tp = (leg.premium + d * abs(best.target - best.entry)) if leg else None
        at_sl = max((leg.premium - d * abs(best.entry - best.stop)), 0.05) if leg else None
        if leg:
            p.notes.append(f"Underlying target {best.target:,.2f} → premium ≈ ₹{at_tp:,.1f}; underlying stop "
                           f"{best.stop:,.2f} → premium ≈ ₹{at_sl:,.1f} (delta estimate)")
    p.exit_rules = ["🎯 Take profit at +50–100% on the premium, or when the underlying reaches the chart target",
                    "🛑 Exit if the premium falls 40–50%, or the underlying hits the chart stop",
                    "⏳ Don't hold bought options into the last 1–2 days: time decay speeds up"]
    if vv.level == "high":
        p.notes.append("⚠️ Volatility is high: bought options are expensive; a bull/bear spread costs less.")
    if no_setup:
        p.notes.append("⚠️ No chart setup: direction here comes only from the PCR tilt. Buying options without a "
                       "setup mostly pays time decay.")
    return p


def plan_credit(snap, a, vv: VolView, side: str) -> Plan:
    """Bull put spread (bullish) or bear call spread (bearish), short leg ~30 delta."""
    wing = int(cfg()["fno"].get("wing_strikes", 2))
    if side == "BUY":
        k = _by_delta(snap, "PE", 0.30, wing) or _shift(snap, snap.atm, -1)
        legs = [_leg(snap, "PE", k, "SELL"), _leg(snap, "PE", _shift(snap, k, -wing), "BUY")]
        title = f"💰 BULL PUT SPREAD · sell {k:,.0f} PE / buy {_shift(snap, k, -wing):,.0f} PE"
    else:
        k = _by_delta(snap, "CE", 0.30, wing) or _shift(snap, snap.atm, 1)
        legs = [_leg(snap, "CE", k, "SELL"), _leg(snap, "CE", _shift(snap, k, wing), "BUY")]
        title = f"💰 BEAR CALL SPREAD · sell {k:,.0f} CE / buy {_shift(snap, k, wing):,.0f} CE"
    why = [f"Chart: {html.escape(_verdict(a))}", f"Volatility: {html.escape(vv.text)} → rich premiums to sell",
           "Defined risk: the bought option caps the loss"]
    p = _base(snap, "credit", title, why, vv)
    p.legs = [l for l in legs if l]
    p.exit_rules = _credit_exits()
    return p


def plan_condor(snap, vv: VolView, a=None) -> Plan:
    d = float(cfg()["fno"].get("short_delta", 0.16))
    wing = int(cfg()["fno"].get("wing_strikes", 2))
    kc = _by_delta(snap, "CE", d, wing) or _shift(snap, snap.atm, 4)
    kp = _by_delta(snap, "PE", d, wing) or _shift(snap, snap.atm, -4)
    legs = [_leg(snap, "CE", kc, "SELL"), _leg(snap, "CE", _shift(snap, kc, wing), "BUY"),
            _leg(snap, "PE", kp, "SELL"), _leg(snap, "PE", _shift(snap, kp, -wing), "BUY")]
    why = [f"Chart: {html.escape(_verdict(a))}", f"Volatility: {html.escape(vv.text)}",
           f"Sell ~{d * 100:.0f}-delta call and put (≈{(1 - 2 * d) * 100:.0f}% chance both expire worthless), "
           "buy wings to cap the loss"]
    mx_c, mx_p = snap.max_oi("CE"), snap.max_oi("PE")
    if mx_c and mx_p:
        why.append(f"OI walls: calls {mx_c.strike:,.0f} · puts {mx_p.strike:,.0f}"
                   + (" (short strikes sit outside them ✅)" if kc >= mx_c.strike and kp <= mx_p.strike else ""))
    p = _base(snap, "condor", f"💰 IRON CONDOR · sell {kp:,.0f} PE + {kc:,.0f} CE · wings {_shift(snap, kp, -wing):,.0f} "
                              f"/ {_shift(snap, kc, wing):,.0f}", why, vv)
    p.legs = [l for l in legs if l]
    if len({(l.kind, l.strike) for l in p.legs}) < len(p.legs):
        p.notes.append("⚠️ The option chain window is too narrow for wings this wide; widen fno.strikes_each_side.")
    p.exit_rules = _credit_exits()
    return p


def plan_hedge(snap, a, vv: VolView) -> Plan:
    """Protective put + collar for someone holding the stock/index (or long futures)."""
    kp = _by_delta(snap, "PE", 0.30) or _shift(snap, snap.atm, -1)
    kc = _by_delta(snap, "CE", 0.25) or _shift(snap, snap.atm, 2)
    why = ["For holders of the shares / long futures: insure the downside for a fixed cost",
           f"Chart: {html.escape(_verdict(a))}", f"Volatility: {html.escape(vv.text)}"]
    p = _base(snap, "hedge", f"🛡 HEDGE · buy {kp:,.0f} PE (protective put) · collar: also sell {kc:,.0f} CE", why, vv)
    put, call = _leg(snap, "PE", kp, "BUY"), _leg(snap, "CE", kc, "SELL")
    p.legs = [l for l in (put, call) if l]
    if put:
        cost = put.premium * snap.lot
        notional = snap.spot * snap.lot
        p.notes.append(f"Put alone costs ₹{cost:,.0f} per lot = {cost / notional * 100:.2f}% of the ₹{notional:,.0f} "
                       f"position; below {kp:,.0f} every further fall is covered.")
        if call:
            net = (put.premium - call.premium) * snap.lot
            p.notes.append(f"Collar (put − call) costs ₹{net:,.0f} per lot; upside is capped at {kc:,.0f}.")
        p.notes.append("Short futures instead? Hedge with a bought call (same idea, mirrored).")
    p.exit_rules = ["🛡 Keep the hedge while you hold the position; roll it to the next expiry before this one ends",
                    "If the market rallies, the put loses value — that is the insurance cost",
                    "The payoff shown is the options only (without your shares)"]
    return p


def _credit_exits() -> list[str]:
    return ["🎯 Take profit when the spread can be bought back for 50% of the credit received",
            "🛑 Stop when the loss reaches 2× the credit received (or the underlying breaks a short strike)",
            "⏳ Close or roll 1 day before expiry (weekly options: don't carry into expiry day)"]


def _verdict(a) -> str:
    if a is None:
        return "chart analysis unavailable"
    from bot import report
    return report.verdict(a)


def recommend(snap, a) -> tuple[Plan, list[Plan]]:
    """The recommended plan plus the alternatives (buying, selling, hedging), all from the live chain."""
    greeks.fill(snap)
    vv = vol_view(snap, a)
    d = a.direction if a is not None else "NONE"
    side = (a.best().side if a is not None and a.best() else None) if d in ("BUY", "SELL", "FORMING") else None
    condor = plan_condor(snap, vv, a)
    buy = plan_buy(snap, a, vv)
    hedge = plan_hedge(snap, a, vv)
    if side and vv.level != "high":
        rec = buy
    elif side:
        rec = plan_credit(snap, a, vv, side)
    elif vv.level in ("high", "normal"):
        rec = condor
    else:
        rec = Plan("wait", "⏸ STAND ASIDE", [f"Chart: {html.escape(_verdict(a))}",
                                             f"Volatility: {html.escape(vv.text)} → premiums too small to sell, "
                                             "and no direction to buy"], lot=snap.lot, spot=snap.spot)
    alts = [p for p in (buy, condor, hedge) if p.key != rec.key]
    if rec.key == "credit":
        alts = [buy, condor, hedge]
    return rec, alts


# ---------------------------------------------------------------- text + payoff chart

def _rs(v: float | None) -> str:
    if v is None:
        return "unlimited"
    return f"{'+' if v >= 0 else '−'}₹{abs(v):,.0f}"


def plan_text(p: Plan, snap, recommended: bool = False) -> str:
    cap, _ = capital("stock")
    lines = [("⭐ <b>RECOMMENDED PLAN</b>\n" if recommended else "") + f"<b>{html.escape(p.title)}</b>",
             f"<i>{snap.name} spot {snap.spot:,.2f} · expiry {snap.expiry:%d %b} ({p.days} days) · lot {snap.lot} · "
             f"{snap.fetched_at}</i>", LINE]
    lines += [f"• {w}" for w in p.why]
    if not p.legs:
        lines.append("No trade. Waiting is a position too.")
        return "\n".join(lines)
    lines += ["", "<b>LEGS</b> (live premiums)"]
    for l in p.legs:
        dt = f" · Δ {l.delta:+.2f}" if l.delta is not None else ""
        lines.append(f"{'🟢 BUY ' if l.action == 'BUY' else '🔴 SELL'} {l.strike:,.0f} {l.kind} @ ₹{l.premium:,.2f}{dt}")
    net = p.net * p.lot
    lines.append(f"{'💵 Net debit (you pay)' if net > 0 else '💵 Net credit (you receive)'}: ₹{abs(net):,.0f} per lot")
    mp, ml = p.max_profit(), p.max_loss()
    lines.append(f"📈 Max profit: {_rs(mp)} · 📉 Max loss: {_rs(ml)} per lot")
    be = p.breakevens()
    if be:
        lines.append("⚖️ Breakeven at expiry: " + " · ".join(f"{b:,.0f}" for b in be))
    pop = p.pop()
    if pop is not None:
        lines.append(f"🎲 Chance of profit at expiry ≈ {pop:.0f}% (lognormal model, IV {p.iv:.1f}%)")
    if ml is not None and mp is not None and ml < 0 and any(l.action == "SELL" for l in p.legs):
        lines.append(f"⚖️ Reward : risk = {mp / abs(ml):.2f} : 1")
    g = p.net_greeks()
    if g.get("delta") is not None:
        lines.append(f"🧮 Position Greeks per lot: Δ {g['delta']:+.1f} · Γ {g['gamma']:+.3f} · "
                     f"Θ ₹{g['theta']:+,.0f}/day · vega ₹{g['vega']:+,.0f} per 1% IV")
    if ml is not None:
        pct = abs(ml) / cap * 100
        lines.append(("✅" if pct <= 10 else "⚠️") + f" Max loss = {pct:.0f}% of your ₹{cap:,.0f} capital"
                     + ("" if pct <= 10 else " — too big for one trade; paper-trade it or skip"))
    else:
        lines.append("⚠️ Unlimited risk on one side — only with a strict stop.")
    if any(l.action == "SELL" for l in p.legs):
        lines.append("🏦 Selling options needs margin (hedged spreads need much less). Check Angel One's margin "
                     "calculator before trading.")
    lines += [f"• {n}" for n in p.notes]
    lines += ["", "<b>EXIT PLAN</b>"] + p.exit_rules
    lines.append("<i>Educational plan from live data; premiums move every second. Paper-trade first. "
                 "Not financial advice.</i>")
    return "\n".join(lines)


def payoff_chart(p: Plan, snap) -> bytes | None:
    if not p.legs:
        return None
    g = p.grid()
    v = np.array([p.payoff(s) for s in g])
    fig, ax = plt.subplots(figsize=(11, 5.5))
    ax.plot(g, v, color="#2c3e50", linewidth=2, label="At expiry")
    # today's value (Black-Scholes with each leg's IV)
    r = float(cfg()["fno"].get("risk_free_rate", 0.065))
    T = greeks.years_to_expiry(snap.expiry)
    if all(l.iv for l in p.legs):
        today = []
        for s in g:
            val = sum(l.sign * (greeks.bs_price(s, l.strike, T, r, l.iv / 100, l.kind) - l.premium) for l in p.legs)
            today.append(val * p.lot)
        ax.plot(g, today, color="#8e44ad", linestyle="--", linewidth=1.2, label="Today (model)")
    ax.fill_between(g, v, 0, where=v >= 0, color="#27ae60", alpha=0.18)
    ax.fill_between(g, v, 0, where=v < 0, color="#e74c3c", alpha=0.18)
    ax.axhline(0, color="#555", linewidth=0.8)
    ax.axvline(snap.spot, color="#2e86de", linestyle="--", linewidth=1.2, label=f"Spot {snap.spot:,.0f}")
    for b in p.breakevens():
        ax.axvline(b, color="#7f8c8d", linestyle=":", linewidth=1)
        ax.text(b, ax.get_ylim()[1] * 0.9, f" BE {b:,.0f}", fontsize=8, color="#555")
    for l in p.legs:
        ax.axvline(l.strike, color="#27ae60" if l.action == "BUY" else "#c0392b", linewidth=0.6, alpha=0.6)
    from bot.chart import plain
    ax.set_title(plain(f"{snap.name} · {p.title} · P&L per lot ({snap.lot})"), fontsize=11, fontweight="bold")
    ax.set_xlabel("Underlying at expiry")
    ax.set_ylabel("₹ per lot")
    ax.grid(linestyle=":")
    ax.legend(fontsize=9, loc="best")
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=105)
    plt.close(fig)
    return buf.getvalue()

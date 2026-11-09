"""Risk ladder: instead of a fixed capital, show what each risk amount buys.

For a setup (entry, stop, target) and a risk amount (e.g. $10, $25, $50, $100 — the most is $100, or the
rupee ladder for Indian assets), the size is risk ÷ loss per unit, rounded down to what the market allows
(whole shares, whole lots, 0.01 forex lot, ...). The reward is that size × the distance to the target.

Contract details per market (money per 1 point of price, smallest step):
  crypto       1 coin                          step: 4 significant digits      USDT ($)
  NSE stock    1 share                         step: 1 share                    ₹
  NSE index    1 futures lot (lot size)        step: 1 lot                      ₹
  forex/metal  1 lot = lot units × quote→USD   step: 0.01 lot                   $
  MCX / CDS    1 lot = contract multiplier     step: 1 lot                      ₹
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from bot.config import cfg

DEFAULT_USD = [10, 25, 50, 100]
DEFAULT_INR = [1000, 2000, 4000, 8500]   # about $12 / $24 / $47 / $100


@dataclass
class Spec:
    unit: str          # share / BTC / lot ...
    mult: float        # money per 1 point of price for ONE unit
    step: float        # smallest tradable size (0 = any size, crypto)
    cur: str           # INR or USD
    kind: str          # shares / coins / lots

    @property
    def sym(self) -> str:
        return "₹" if self.cur == "INR" else "$"


def tiers(cur: str) -> list[float]:
    r = cfg()["risk"]
    return list(r.get("ladder_inr", DEFAULT_INR) if cur == "INR" else r.get("ladder_usd", DEFAULT_USD))


def max_risk(cur: str) -> float:
    return tiers(cur)[-1]


def money(v: float, cur: str, signed: bool = False) -> str:
    sym = "₹" if cur == "INR" else "$"
    sign = ("+" if v >= 0 else "−") if signed else ("−" if v < 0 else "")
    av = abs(v)
    whole = abs(av - round(av)) < 0.005 and av >= 10
    txt = f"{av:,.0f}" if (cur == "INR" and av >= 100) or whole else f"{av:,.2f}"
    return f"{sign}{sym}{txt}"


def index_lot(name: str) -> int | None:
    """Futures lot size of an NSE index (Angel One instrument list), or None."""
    from bot import angel

    if not angel.configured():
        return None
    try:
        futs, _, _ = angel.derivatives(angel.session().inst, name)
        if len(futs):
            return int(futs.iloc[0]["lot"])
    except Exception:
        return None
    return None


def spec_for(a, price: float | None = None) -> Spec:
    """How sizes and money work for this analysed asset."""
    price = price or a.now_price
    if a.market == "crypto":
        return Spec(a.short, 1.0, 0.0, "USD", "coins")
    if a.market == "fx" and a.asset is not None:
        asset = a.asset
        if asset.indian:
            return Spec("lot", float(asset.lot_units or 1), 1.0, "INR", "lots")
        from bot import fx
        q = fx.quote_to_usd(asset, price) or 1.0
        if not asset.lot_units:
            return Spec("unit", q, 0.0, "USD", "units")
        return Spec("lot", float(asset.lot_units) * q, 0.01, "USD", "lots")
    if a.is_index:
        lot = index_lot(a.symbol)
        if lot:
            return Spec(f"lot ({lot})", float(lot), 1.0, "INR", "lots")
        return Spec("unit", 1.0, 1.0, "INR", "units")
    return Spec("share", 1.0, 1.0, "INR", "shares")


def round_qty(q: float, step: float) -> float:
    if step <= 0:
        if q <= 0:
            return 0.0
        digits = 3 - int(math.floor(math.log10(q)))      # 4 significant digits, rounded down
        f = 10 ** digits
        return math.floor(q * f) / f
    return math.floor(q / step + 1e-9) * step


def fmt_qty(q: float, sp: Spec) -> str:
    if sp.step >= 1:
        n = int(q)
        unit = sp.unit if n == 1 else (sp.unit + "s" if sp.unit in ("share", "lot", "unit") else sp.unit)
        return f"{n} {unit}"
    if sp.step == 0.01:
        return f"{q:.2f} {sp.unit}"
    return f"{q:g} {sp.unit}"


@dataclass
class Rung:
    budget: float
    qty: float
    risk: float
    reward: float
    notional: float
    fits: bool           # the smallest size still fits the budget


def ladder(a, entry: float, stop: float, target: float | None, sp: Spec | None = None) -> list[Rung]:
    sp = sp or spec_for(a, entry)
    per_unit = abs(entry - stop) * sp.mult
    gain_unit = abs(target - entry) * sp.mult if target is not None else 0.0
    rows = []
    for b in tiers(sp.cur):
        if per_unit <= 0:
            break
        q = round_qty(b / per_unit, sp.step)
        fits = q > 0
        if not fits:
            q = sp.step if sp.step > 0 else 0.0
        rows.append(Rung(b, q, q * per_unit, q * gain_unit, q * entry * sp.mult, fits))
    return rows


def ladder_lines(a, entry: float, stop: float, target: float | None, compact: bool = False) -> list[str]:
    """Card / details text: 'Risk $10 → 0.0042 BTC → reward +$20.00'."""
    sp = spec_for(a, entry)
    rows = ladder(a, entry, stop, target, sp)
    if not rows:
        return []
    rr = abs(target - entry) / abs(entry - stop) if target is not None and entry != stop else None
    head = f"📦 <b>Risk → size → reward</b>" + (f" (R:R 1:{rr:.1f})" if rr else "")
    out = [head]
    seen = set()
    for r in rows:
        if not r.fits:
            if "min" in seen:
                continue
            seen.add("min")
            out.append(f"   {money(r.budget, sp.cur)} → smallest size {fmt_qty(r.qty, sp)} risks "
                       f"{money(r.risk, sp.cur)} (more than {money(r.budget, sp.cur)})")
            continue
        out.append(f"   {money(r.budget, sp.cur)} → <b>{fmt_qty(r.qty, sp)}</b> → 🎯 {money(r.reward, sp.cur, True)}")
    if compact:
        return out
    return out + [f"   <i>Max shown: {money(max_risk(sp.cur), sp.cur)} per trade. Sizes rounded down; fees not "
                  "included.</i>"]


def fits_text(loss: float, cur: str) -> str:
    """One line for F&O plans: does a max loss fit the ladder?"""
    top = max_risk(cur)
    fit = next((t for t in tiers(cur) if loss <= t), None)
    if fit is not None:
        return f"✅ Max loss {money(loss, cur)} fits the {money(fit, cur)} risk level"
    return f"⚠️ Max loss {money(loss, cur)} is above your {money(top, cur)} maximum risk — paper-trade it or skip"

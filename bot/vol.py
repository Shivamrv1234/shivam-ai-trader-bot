"""Volatility: India VIX (the market's fear gauge) and the chosen asset's own volatility.

India VIX = the market's expected NIFTY volatility over the next 30 days, in % a year, from NIFTY option
prices. Scale used here (common reading of its history; not an official NSE scale):
  below 11   very low   🟢 calm, cheap options (complacency can precede a jump)
  11 - 14    low        🟢 calm, healthy market
  14 - 18    normal     🟡 normal swings
  18 - 24    high       🟠 nervous market, big daily moves, expensive options
  above 24   very high  🔴 fear / panic
Expected NIFTY move ≈ VIX / √252 % per day (one standard deviation, about 2 days in 3).

For a single stock there is no VIX, so the bot uses its option IV (implied volatility, F&O stocks) and
its HV (historical volatility of daily closes): IV well above HV = options are expensive.
"""
from __future__ import annotations

import html
import io
import logging
import math
import time
from dataclasses import dataclass

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from bot import data  # noqa: E402

log = logging.getLogger(__name__)
LINE = "━━━━━━━━━━━━━━━━━━"
BANDS = [(11, "very low", "🟢"), (14, "low / calm", "🟢"), (18, "normal", "🟡"), (24, "high", "🟠"),
         (float("inf"), "very high / fear", "🔴")]
CACHE_S = 300
_cache: dict = {}


def band(v: float) -> tuple[str, str]:
    for hi, label, icon in BANDS:
        if v < hi:
            return label, icon
    return BANDS[-1][1], BANDS[-1][2]


@dataclass
class Vix:
    value: float
    change_pct: float | None
    history: pd.Series            # daily closes, oldest first
    source: str

    @property
    def label(self) -> str:
        return band(self.value)[0]

    @property
    def icon(self) -> str:
        return band(self.value)[1]

    @property
    def percentile(self) -> float | None:
        h = self.history.tail(252)
        return float((h < self.value).mean() * 100) if len(h) >= 50 else None

    @property
    def daily_move_pct(self) -> float:
        return self.value / math.sqrt(252)

    @property
    def weekly_move_pct(self) -> float:
        return self.value / math.sqrt(52)


def india_vix() -> Vix | None:
    """Live India VIX (Angel One) or Yahoo's ^INDIAVIX; cached for 5 minutes."""
    hit = _cache.get("vix")
    if hit and time.time() - hit[0] < CACHE_S:
        return hit[1]
    try:
        df = data.stock_candles("INDIAVIX", "1d")
    except data.DataError as exc:
        log.info("India VIX from the main source failed (%s); trying Yahoo", exc)
        try:
            df = data.yahoo_candles("^INDIAVIX", "1d", "INDIAVIX")
            df = data.drop_forming(df, "1d", (15, 30), "Asia/Kolkata")
            df.attrs["source"] = "Yahoo (delayed)"
        except data.DataError as exc2:
            log.info("India VIX unavailable: %s", exc2)
            return None
    if df.empty:
        return None
    live = df.attrs.get("live_price") or df.attrs.get("forming_close") or float(df["close"].iloc[-1])
    prev = float(df["close"].iloc[-1])
    chg = df.attrs.get("live_change_pct")
    if chg is None and prev:
        chg = (live - prev) / prev * 100
    v = Vix(float(live), chg, df["close"], df.attrs.get("source", ""))
    _cache["vix"] = (time.time(), v)
    return v


def card_line(a) -> str:
    """One line for the first message of an Indian asset."""
    if not a.is_indian:
        return ""
    v = india_vix()
    if v is None:
        return ""
    chg = f" ({v.change_pct:+.1f}%)" if v.change_pct is not None else ""
    return f"🌡 India VIX <b>{v.value:.2f}</b>{chg} {v.icon} {v.label} · NIFTY ±{v.daily_move_pct:.1f}%/day"


def scale_text(v: float) -> str:
    rows = []
    lo = 0
    for hi, label, icon in BANDS:
        rng = f"&lt;{hi:g}" if lo == 0 else (f"&gt;{lo:g}" if hi == float("inf") else f"{lo:g}–{hi:g}")
        here = lo <= v < hi
        rows.append(f"{'👉' if here else '   '} {icon} {rng:<7} {label}" + ("  ◀ <b>now</b>" if here else ""))
        lo = hi
    return "\n".join(rows)


def meaning(v: float) -> list[str]:
    label = band(v)[0]
    if v < 11:
        return ["🟢 Stocks: very calm market; trends tend to be smooth.",
                "🟢 Option buyers: premiums are cheap (good for buying options).",
                "🔴 Option sellers: little premium for the risk; a sudden VIX jump hurts sellers.",
                "⚠️ Very low VIX can come before a sharp move (complacency)."]
    if v < 14:
        return ["🟢 Stocks: calm, healthy market (good for swing trades).",
                "🟢 Option buyers: premiums fairly cheap.",
                "🟡 Option sellers: modest premiums; keep positions hedged."]
    if v < 18:
        return ["🟡 Stocks: normal volatility.",
                "🟡 Option buyers/sellers: premiums fair; strategy depends on the chart.",
                "🟢 Defined-risk option selling (iron condor) is reasonable when the chart is range-bound."]
    if v < 24:
        return ["🔴 Stocks: nervous market, bigger daily swings — use smaller size and wider stops.",
                "🔴 Option buyers: premiums expensive; time decay and IV fall hurt.",
                "🟢 Option sellers: rich premiums, but only with hedged (defined-risk) spreads."]
    return [f"🔴 Stocks: {label} — panic conditions, gaps and violent moves are common.",
            "🔴 Option buyers: very expensive premiums.",
            "🔴 Naked option selling is dangerous now; only small, hedged positions.",
            "⚠️ Consider standing aside until VIX cools off."]


def chart(v: Vix) -> bytes:
    h = v.history.tail(250)
    fig, ax = plt.subplots(figsize=(11, 5))
    idx = h.index.tz_convert("Asia/Kolkata").tz_localize(None)
    ax.plot(idx, h.values, color="#2c3e50", linewidth=1.8, label="India VIX")
    lo = 0
    colours = ["#27ae60", "#2ecc71", "#f1c40f", "#e67e22", "#e74c3c"]
    top = max(float(h.max()) * 1.1, 26)
    bottom = max(float(h.min()) * 0.8, 0)
    for (hi, label, _), col in zip(BANDS, colours):
        ax.axhspan(lo, min(hi, top), color=col, alpha=0.10)
        a_, b_ = max(lo, bottom), min(hi, top)
        if b_ - a_ > (top - bottom) * 0.04:          # label only the visible part of each band
            ax.text(idx[-1], (a_ + b_) / 2, f"{label} ", fontsize=8, color=col, va="center", ha="right")
        lo = hi
    ax.axhline(v.value, color="#2e86de", linestyle="--", linewidth=1.2, label=f"Now {v.value:.2f}")
    ax.set_ylim(bottom, top)
    pct = v.percentile
    ax.set_title(f"India VIX · now {v.value:.2f} ({v.label})" + (f" · higher than {pct:.0f}% of the last year"
                                                                 if pct is not None else ""),
                 fontsize=12, fontweight="bold")
    ax.grid(linestyle=":")
    ax.legend(loc="lower left", fontsize=9)
    fig.autofmt_xdate()
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110)
    plt.close(fig)
    return buf.getvalue()


def vix_text(v: Vix) -> str:
    pct = v.percentile
    chg = f" ({v.change_pct:+.2f}% today)" if v.change_pct is not None else ""
    h = v.history.tail(252)
    lines = [f"🌡 <b>INDIA VIX</b> · {html.escape(v.source)}", LINE,
             f"Now <b>{v.value:.2f}</b>{chg} → {v.icon} <b>{v.label}</b>"]
    if pct is not None:
        lines.append(f"1-year range {h.min():.1f} – {h.max():.1f} · higher than {pct:.0f}% of the last year "
                     f"(IV-rank proxy for NIFTY options)")
    lines += [f"Expected NIFTY move: ±{v.daily_move_pct:.2f}% a day · ±{v.weekly_move_pct:.2f}% a week "
              "(1 standard deviation)", "", "<b>SCALE</b>", "<pre>" + scale_text(v.value) + "</pre>",
              "<b>WHAT IT MEANS NOW</b>"] + meaning(v.value)
    lines.append("<i>VIX measures expected volatility, not direction. It usually rises when markets fall.</i>")
    return "\n".join(lines)


# ---------------------------------------------------------------- the chosen asset

def annual_hv(close: pd.Series, periods_per_year: float, window: int) -> float | None:
    r = np.log(close).diff().dropna().tail(window)
    if len(r) < max(10, window // 2):
        return None
    return float(r.std(ddof=0) * math.sqrt(periods_per_year) * 100)


def hv_percentile(close: pd.Series, periods_per_year: float, window: int = 20) -> float | None:
    r = np.log(close).diff()
    hv = r.rolling(window).std(ddof=0) * math.sqrt(periods_per_year) * 100
    hv = hv.dropna().tail(252)
    if len(hv) < 60:
        return None
    return float((hv < hv.iloc[-1]).mean() * 100)


def asset_text(a, atm_iv: float | None = None, atm_src: str = "") -> str:
    """Volatility of the chosen asset from its daily candles (+ option IV for F&O stocks)."""
    from bot import report

    ppy = 365 if a.market == "crypto" else 252 if a.market == "stock" else 260
    try:
        daily = a.x if a.timeframe == "1d" else report.load_candles(a.symbol, a.market, "1d")
    except data.DataError:
        daily = None
    lines = [f"📉 <b>{html.escape(a.pretty)} · VOLATILITY</b>"]
    if daily is None or len(daily) < 30:
        return "\n".join(lines + ["Not enough daily candles to measure volatility."])
    close = daily["close"]
    hv20, hv60 = annual_hv(close, ppy, 20), annual_hv(close, ppy, 60)
    pct = hv_percentile(close, ppy)
    if hv20 is not None:
        tag = ""
        if pct is not None:
            tag = (" 🟢 calm for this asset" if pct < 35 else " 🟡 normal for this asset" if pct < 70 else
                   " 🔴 unusually volatile for this asset")
        lines.append(f"HV20 (last month) <b>{hv20:.1f}%</b>/yr · HV60 {hv60:.1f}%/yr" if hv60 else
                     f"HV20 (last month) <b>{hv20:.1f}%</b>/yr")
        if pct is not None:
            lines.append(f"Today's HV20 is higher than {pct:.0f}% of the last year →{tag}")
        lines.append(f"Typical daily move ≈ ±{hv20 / math.sqrt(ppy):.2f}%")
    if atm_iv is not None and hv20:
        ratio = atm_iv / hv20
        tone = ("🔴 options look expensive vs actual movement (favours option sellers / spreads)" if ratio >= 1.25 else
                "🟢 options look cheap vs actual movement (favours option buyers)" if ratio <= 0.9 else
                "🟡 options fairly priced")
        lines.append(f"ATM option IV <b>{atm_iv:.1f}%</b> ({html.escape(atm_src)}) = {ratio:.2f}× HV20 → {tone}")
    elif a.market == "stock" and not a.is_index:
        lines.append("Option IV: not available (not an F&amp;O stock, or outside market hours).")
    return "\n".join(lines)

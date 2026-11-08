"""Pairs trading / statistical arbitrage on two related NSE stocks (e.g. HDFCBANK / ICICIBANK).

1. Last ~1 year of daily closes, in logs.
2. Hedge ratio β from a least-squares fit: log(A) = α + β·log(B).   spread = log(A) − β·log(B) − α
3. Cointegration test (Engle-Granger): an ADF test on the spread. t-statistic below the critical value
   (-3.34 at 5%, -3.04 at 10%, two series with a constant) = the spread keeps returning to its mean.
4. z-score = (spread − mean) / standard deviation.
   z > +2  → A is rich vs B: SELL A, BUY B (market neutral)
   z < −2  → A is cheap vs B: BUY A, SELL B
   exit when z returns to 0 · stop when |z| > 3.5 or the relationship breaks (test fails)
Short selling cash shares overnight is not allowed in India: the short leg needs futures (both stocks
must be in F&O) — the plan says so. Educational; not financial advice.
"""
from __future__ import annotations

import html
import io
import math
from dataclasses import dataclass

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from bot import data  # noqa: E402

LINE = "━━━━━━━━━━━━━━━━━━"
CRIT = {1: -3.90, 5: -3.34, 10: -3.04}   # MacKinnon (2010), 2 variables with constant


def adf_t(series: np.ndarray, lags: int = 1) -> float:
    """t-statistic of γ in Δs_t = c + γ s_{t-1} + Σ φ_i Δs_{t-i} + e."""
    s = np.asarray(series, dtype=float)
    ds = np.diff(s)
    y = ds[lags:]
    cols = [np.ones_like(y), s[lags:-1]]
    for i in range(1, lags + 1):
        cols.append(ds[lags - i:-i])
    X = np.column_stack(cols)
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ beta
    dof = max(len(y) - X.shape[1], 1)
    sigma2 = resid @ resid / dof
    cov = sigma2 * np.linalg.inv(X.T @ X)
    return float(beta[1] / math.sqrt(cov[1, 1]))


@dataclass
class PairResult:
    a: str
    b: str
    beta: float
    alpha: float
    spread: pd.Series
    z: pd.Series
    t_stat: float
    half_life: float | None
    corr: float
    price_a: float
    price_b: float
    source: str

    @property
    def z_now(self) -> float:
        return float(self.z.iloc[-1])

    @property
    def coint_level(self) -> int | None:
        for lvl in (1, 5, 10):
            if self.t_stat < CRIT[lvl]:
                return lvl
        return None

    @property
    def signal(self) -> str:
        if self.coint_level is None:
            return "NO_COINT"
        z = self.z_now
        if abs(z) > 3.5:
            return "STOP"
        if z > 2:
            return "SHORT_A"
        if z < -2:
            return "LONG_A"
        if abs(z) < 0.25:
            return "AT_MEAN"
        return "WAIT"


def analyze(a: str, b: str, closes: tuple[pd.Series, pd.Series] | None = None, window: int = 252) -> PairResult:
    a, b = a.upper(), b.upper()
    if closes is None:
        da, db = data.stock_candles(a, "1d"), data.stock_candles(b, "1d")
        src = da.attrs.get("source", "")
        sa, sb = da["close"], db["close"]
        pa = da.attrs.get("live_price") or float(sa.iloc[-1])
        pb = db.attrs.get("live_price") or float(sb.iloc[-1])
    else:
        sa, sb = closes
        src, pa, pb = "test data", float(sa.iloc[-1]), float(sb.iloc[-1])
    df = pd.concat([sa.rename("a"), sb.rename("b")], axis=1, join="inner").dropna().tail(window)
    if len(df) < 120:
        raise data.DataError(f"Only {len(df)} common days for {a}/{b}; need 120+")
    la, lb = np.log(df["a"].to_numpy()), np.log(df["b"].to_numpy())
    beta, alpha = np.polyfit(lb, la, 1)
    spread = pd.Series(la - beta * lb - alpha, index=df.index)
    z = (spread - spread.mean()) / spread.std(ddof=0)
    t = adf_t(spread.to_numpy())
    ds, lag = spread.diff().dropna(), spread.shift(1).dropna()
    gamma = np.polyfit(lag.loc[ds.index].to_numpy(), ds.to_numpy(), 1)[0]
    hl = -math.log(2) / math.log(1 + gamma) if -1 < gamma < 0 else None
    corr = float(np.corrcoef(np.diff(la), np.diff(lb))[0, 1])
    return PairResult(a, b, float(beta), float(alpha), spread, z, t, hl, corr, float(pa), float(pb), src)


def report_text(r: PairResult) -> str:
    lvl = r.coint_level
    test = (f"🟢 cointegrated at {lvl}% (t = {r.t_stat:.2f} vs {CRIT[lvl]:.2f})" if lvl else
            f"🔴 NOT cointegrated (t = {r.t_stat:.2f}; needs below {CRIT[10]:.2f}) → no trade")
    z = r.z_now
    lines = [f"⚖️ <b>PAIRS TRADE · {html.escape(r.a)} / {html.escape(r.b)}</b>",
             f"<i>{len(r.spread)} daily closes · {html.escape(r.source)}</i>", LINE,
             f"Prices: {r.a} ₹{r.price_a:,.2f} · {r.b} ₹{r.price_b:,.2f}",
             f"Correlation of daily returns: {r.corr:.2f} " + ("🟢" if r.corr >= 0.6 else "🟡" if r.corr >= 0.4 else "🔴"),
             f"Hedge ratio β = {r.beta:.2f} (₹1 of {r.a} ≈ ₹{r.beta:.2f} of {r.b})",
             f"Cointegration test: {test}"]
    if r.half_life:
        lines.append(f"Half-life of the spread: {r.half_life:.0f} days (how fast gaps usually close)")
    lines.append(f"Spread z-score now: <b>{z:+.2f}</b>")
    lines.append("")
    sig = r.signal
    notional = 10000
    if sig == "SHORT_A":
        lines += [f"🔴 <b>SELL {r.a}</b> + 🟢 <b>BUY {r.b}</b> — {r.a} is rich vs {r.b} (z > +2)",
                  f"Size idea: for ₹{notional:,} of {r.a}, ₹{notional * r.beta:,.0f} of {r.b}"]
    elif sig == "LONG_A":
        lines += [f"🟢 <b>BUY {r.a}</b> + 🔴 <b>SELL {r.b}</b> — {r.a} is cheap vs {r.b} (z < −2)",
                  f"Size idea: for ₹{notional:,} of {r.a}, ₹{notional * r.beta:,.0f} of {r.b}"]
    elif sig == "STOP":
        lines.append("🛑 |z| above 3.5: the relationship may be breaking — stop / don't enter.")
    elif sig == "NO_COINT":
        lines.append("⚪ No trade: the two prices don't reliably move back together right now.")
    elif sig == "AT_MEAN":
        lines.append("🎯 Spread is at its mean (z ≈ 0): exit point for an open pair trade; no new entry.")
    else:
        lines.append("⚪ Waiting: enter only when |z| goes above 2.")
    lines += ["", "<b>RULES</b>", "Entry |z| > 2 · exit at z = 0 · stop at |z| > 3.5 or if the test fails",
              "The short leg needs futures (cash shares can't be shorted overnight in India).",
              "<i>Market-neutral, but not risk-free: relationships break. Not financial advice.</i>"]
    return "\n".join(lines)


def chart(r: PairResult) -> bytes:
    fig, ax = plt.subplots(figsize=(11, 5))
    idx = r.z.index.tz_convert("Asia/Kolkata").tz_localize(None) if r.z.index.tz is not None else r.z.index
    ax.plot(idx, r.z.values, color="#2c3e50", linewidth=1.6, label="spread z-score")
    for lvl, col in ((2, "#e67e22"), (-2, "#e67e22"), (3.5, "#c0392b"), (-3.5, "#c0392b"), (0, "#27ae60")):
        ax.axhline(lvl, color=col, linestyle="--" if lvl else "-", linewidth=1)
    ax.fill_between(idx, 2, np.maximum(r.z.values, 2), color="#e74c3c", alpha=0.15)
    ax.fill_between(idx, -2, np.minimum(r.z.values, -2), color="#27ae60", alpha=0.15)
    ax.set_title(f"{r.a} / {r.b} spread z-score · now {r.z_now:+.2f} · β {r.beta:.2f} · ADF t {r.t_stat:.2f}",
                 fontsize=12, fontweight="bold")
    ax.grid(linestyle=":")
    ax.legend(loc="upper left", fontsize=9)
    fig.autofmt_xdate()
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110)
    plt.close(fig)
    return buf.getvalue()

"""Fundamental analysis of an NSE stock from Yahoo Finance (via yfinance; free, unofficial).

Yahoo's data can be late or incomplete for Indian stocks, so every figure is optional and the
message says where it came from. Notes on Yahoo's units:
- debtToEquity is a percentage (35 means a ratio of 0.35)
- margins, growth, ROE and holdings are fractions (0.12 = 12%)
- dividendYield changed units in 2025, so the yield is computed as dividendRate / price instead
"""
from __future__ import annotations

import html
import logging
from dataclasses import dataclass, field

import pandas as pd

from bot.data import DataError

log = logging.getLogger(__name__)
LINE = "━━━━━━━━━━━━━━━━━━"
FINANCIAL_SECTORS = ("financial", "bank")


@dataclass
class Fundamentals:
    symbol: str
    info: dict
    quarters: pd.DataFrame | None = None     # rows: revenue, net_income; columns: quarter end dates
    checks: list = field(default_factory=list)  # (status "✅"/"⚠️"/"❌", label, explanation)

    def get(self, *keys):
        for k in keys:
            v = self.info.get(k)
            if isinstance(v, (int, float)) and not pd.isna(v):
                return float(v)
        return None

    @property
    def name(self) -> str:
        return str(self.info.get("longName") or self.info.get("shortName") or self.symbol)

    @property
    def is_financial(self) -> bool:
        s = f"{self.info.get('sector', '')} {self.info.get('industry', '')}".lower()
        return any(w in s for w in FINANCIAL_SECTORS)

    @property
    def price(self):
        return self.get("currentPrice", "regularMarketPrice", "previousClose")

    @property
    def dividend_yield_pct(self):
        rate, price = self.get("dividendRate", "trailingAnnualDividendRate"), self.price
        if rate is not None and price:
            return rate / price * 100
        y = self.get("trailingAnnualDividendYield")
        return y * 100 if y is not None else None

    @property
    def debt_to_equity(self):
        d = self.get("debtToEquity")
        return d / 100 if d is not None else None


def fetch(symbol: str) -> Fundamentals:
    import yfinance as yf

    sym = symbol.upper().removesuffix(".NS")
    t = yf.Ticker(f"{sym}.NS")
    try:
        info = t.info or {}
    except Exception as exc:
        raise DataError(f"Yahoo fundamentals {sym}: {exc.__class__.__name__}") from None
    if not any(k in info for k in ("marketCap", "trailingPE", "longName", "shortName")):
        raise DataError(f"Yahoo has no fundamentals for {sym} (check the NSE symbol)")
    f = Fundamentals(sym, info)
    try:
        f.quarters = quarterly(t.quarterly_income_stmt)
    except Exception as exc:  # quarterly results are a bonus
        log.info("Quarterly results %s unavailable: %s", sym, exc.__class__.__name__)
    f.checks = score(f)
    return f


def quarterly(stmt: pd.DataFrame | None) -> pd.DataFrame | None:
    if stmt is None or not isinstance(stmt, pd.DataFrame) or stmt.empty:
        return None
    rows = {}
    for key, names in (("revenue", ["Total Revenue", "Operating Revenue"]),
                       ("net_income", ["Net Income", "Net Income Common Stockholders"])):
        for n in names:
            if n in stmt.index:
                rows[key] = pd.to_numeric(stmt.loc[n], errors="coerce")
                break
    if not rows:
        return None
    q = pd.DataFrame(rows).T
    q = q[sorted(q.columns)[-5:]]  # oldest -> newest, up to 5 quarters (for year-on-year)
    return q


def _pct(v):
    return f"{v * 100:.1f}%" if v is not None else "–"


def _cr(v):
    """Rupees -> crore / lakh crore text."""
    if v is None:
        return "–"
    cr = v / 1e7
    if abs(cr) >= 1e5:
        return f"₹{cr / 1e5:.2f} L Cr"
    return f"₹{cr:,.0f} Cr"


def score(f: Fundamentals) -> list:
    out = []

    def add(ok, warn, label, why):
        out.append(("✅" if ok else ("⚠️" if warn else "❌"), label, why))

    roe = f.get("returnOnEquity")
    if roe is not None:
        add(roe >= 0.15, roe >= 0.10, f"Return on equity {_pct(roe)}",
            "profit made on shareholders' money; 15%+ is strong")
    de = f.debt_to_equity
    if de is not None and not f.is_financial:
        add(de <= 0.5, de <= 1.5, f"Debt / equity {de:.2f}", "borrowing vs own money; below 0.5 is comfortable")
    pm = f.get("profitMargins")
    if pm is not None:
        add(pm >= 0.10, pm >= 0.05, f"Net profit margin {_pct(pm)}", "share of sales kept as profit; 10%+ is healthy")
    rg = f.get("revenueGrowth")
    if rg is not None:
        add(rg >= 0.10, rg >= 0.0, f"Revenue growth {_pct(rg)}", "sales vs the same quarter last year")
    eg = f.get("earningsGrowth", "earningsQuarterlyGrowth")
    if eg is not None:
        add(eg >= 0.10, eg >= 0.0, f"Earnings growth {_pct(eg)}", "profit vs the same quarter last year")
    pe = f.get("trailingPE")
    if pe is not None:
        add(0 < pe <= 30, 30 < pe <= 60, f"P/E {pe:.1f}",
            "price paid per ₹1 of yearly profit; compare with sector peers")
    elif f.get("trailingEps") is not None and f.get("trailingEps") < 0:
        add(False, False, "Loss-making (negative EPS)", "the company lost money over the last year")
    cr = f.get("currentRatio")
    if cr is not None and not f.is_financial:
        add(cr >= 1.2, cr >= 1.0, f"Current ratio {cr:.2f}", "short-term assets vs bills due; 1.2+ is safe")
    fcf = f.get("freeCashflow")
    if fcf is not None and not f.is_financial:
        add(fcf > 0, False, f"Free cash flow {_cr(fcf)}", "cash left after running and investing in the business")
    return out


def verdict(f: Fundamentals) -> str:
    if not f.checks:
        return "Not enough data from Yahoo to score this stock."
    good = sum(c[0] == "✅" for c in f.checks)
    bad = sum(c[0] == "❌" for c in f.checks)
    n = len(f.checks)
    tone = "Strong" if good >= 0.7 * n and bad == 0 else ("Weak" if bad >= n / 2 else "Mixed")
    return f"{tone} fundamentals — {good} of {n} checks pass"


def report_text(f: Fundamentals) -> str:
    i = f.info
    sector = " · ".join(html.escape(str(x)) for x in (i.get("sector"), i.get("industry")) if x)
    lines = [f"🏢 <b>{html.escape(f.symbol)} · FUNDAMENTALS</b>",
             f"<i>{html.escape(f.name)}{(' · ' + sector) if sector else ''}</i>", LINE]
    price, hi, lo = f.price, f.get("fiftyTwoWeekHigh"), f.get("fiftyTwoWeekLow")
    if f.get("marketCap") is not None:
        lines.append(f"💰 Market cap <b>{_cr(f.get('marketCap'))}</b>")
    if price is not None:
        rng = ""
        if hi and lo and hi > lo:
            rng = f" · 52W {lo:,.2f} – {hi:,.2f} (at {(price - lo) / (hi - lo) * 100:.0f}% of the range)"
        lines.append(f"💹 Price ₹{price:,.2f}{rng}")

    def row(title, items):
        vals = [f"{k} {v}" for k, v in items if v not in (None, "–")]
        if vals:
            lines.extend(["", f"<b>{title}</b>", " · ".join(vals)])

    g = f.get
    row("📊 VALUATION", [("P/E", f"{g('trailingPE'):.1f}" if g("trailingPE") else None),
                        ("Forward P/E", f"{g('forwardPE'):.1f}" if g("forwardPE") else None),
                        ("P/B", f"{g('priceToBook'):.2f}" if g("priceToBook") else None),
                        ("EPS", f"₹{g('trailingEps'):,.2f}" if g("trailingEps") is not None else None),
                        ("Book value", f"₹{g('bookValue'):,.2f}" if g("bookValue") else None)])
    row("📈 PROFITABILITY", [("ROE", _pct(g("returnOnEquity"))), ("ROA", _pct(g("returnOnAssets"))),
                            ("Operating margin", _pct(g("operatingMargins"))),
                            ("Net margin", _pct(g("profitMargins")))])
    row("🚀 GROWTH (vs last year's quarter)", [("Revenue", _pct(g("revenueGrowth"))),
                                              ("Earnings", _pct(g("earningsGrowth", "earningsQuarterlyGrowth")))])
    row("🏦 BALANCE SHEET", [("Debt/Equity", f"{f.debt_to_equity:.2f}" if f.debt_to_equity is not None else None),
                            ("Current ratio", f"{g('currentRatio'):.2f}" if g("currentRatio") else None),
                            ("Cash", _cr(g("totalCash")) if g("totalCash") else None),
                            ("Debt", _cr(g("totalDebt")) if g("totalDebt") else None)])
    dy = f.dividend_yield_pct
    row("💵 DIVIDEND", [("Yield", f"{dy:.2f}%" if dy is not None else None),
                       ("Payout", _pct(g("payoutRatio")) if g("payoutRatio") else None)])
    row("👥 HOLDINGS (Yahoo)", [("Insiders/promoters", _pct(g("heldPercentInsiders"))),
                               ("Institutions", _pct(g("heldPercentInstitutions"))),
                               ("Beta", f"{g('beta'):.2f}" if g("beta") is not None else None)])
    if f.quarters is not None and f.quarters.shape[1] >= 2:
        q = f.quarters
        tbl = [f"{'Quarter':<9}{'Revenue':>12}{'Net profit':>12}"]
        for col in q.columns[-4:]:
            rev, ni = q.at["revenue", col] if "revenue" in q.index else None, \
                q.at["net_income", col] if "net_income" in q.index else None
            tbl.append(f"{pd.Timestamp(col):%b %Y}".ljust(9)
                       + f"{_cr(rev) if rev is not None and not pd.isna(rev) else '–':>12}"
                       + f"{_cr(ni) if ni is not None and not pd.isna(ni) else '–':>12}")
        lines += ["", "<b>🧾 LAST QUARTERS</b>", "<pre>" + html.escape("\n".join(tbl)) + "</pre>"]
    if f.checks:
        lines += ["", "<b>✅ HEALTH CHECK</b>"]
        lines += [f"{st} {html.escape(lbl)} — <i>{html.escape(why)}</i>" for st, lbl, why in f.checks]
        if f.is_financial:
            lines.append("ℹ️ Bank/finance company: debt, current ratio and cash flow are not meaningful, so skipped.")
        lines += ["", f"<b>Verdict:</b> {html.escape(verdict(f))}"]
    lines.append("<i>Source: Yahoo Finance (unofficial, can be late or incomplete). Check the annual report or "
                 "NSE filings before investing. Not financial advice.</i>")
    return "\n".join(lines)

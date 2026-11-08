"""Fundamental analysis of an NSE stock from Yahoo Finance (via yfinance; free, unofficial).

Two messages:
  brief    - the key ratios, each marked 🟢 good / 🟡 average / 🔴 weak, the verdict, then a button for
  detailed - every ratio Tickertape / Screener show (valuation, profitability incl. ROCE, growth incl.
             3-year CAGR, balance sheet incl. interest cover and quick ratio, cash flow quality, holdings,
             price vs moving averages, analyst view), quarterly and yearly tables, strengths & weaknesses.
Tickertape and Screener have no free public API, so the ratios are computed here from Yahoo's statements
and the message links to both sites for checking.

Yahoo's data can be late or incomplete for Indian stocks, so every figure is optional. Units:
- debtToEquity is a percentage (35 means a ratio of 0.35)
- margins, growth, ROE and holdings are fractions (0.12 = 12%)
- dividendYield changed units in 2025, so the yield is computed as dividendRate / price instead
"""
from __future__ import annotations

import html
import logging
from dataclasses import dataclass, field
from urllib.parse import quote_plus

import pandas as pd

from bot.data import DataError

log = logging.getLogger(__name__)
LINE = "━━━━━━━━━━━━━━━━━━"
FINANCIAL_SECTORS = ("financial", "bank", "insurance", "capital markets", "credit services")
GOOD, MID, BAD = "🟢", "🟡", "🔴"


@dataclass
class Fundamentals:
    symbol: str
    info: dict
    quarters: pd.DataFrame | None = None     # rows: revenue, net_income; columns: quarter end dates
    years: pd.DataFrame | None = None        # rows: revenue, net_income, ebit, ...; columns: year ends
    checks: list = field(default_factory=list)  # (status "✅"/"⚠️"/"❌", label, explanation)
    extra: dict = field(default_factory=dict)   # ratios computed from the statements

    def get(self, *keys):
        for k in keys:
            v = self.info.get(k)
            if isinstance(v, (int, float)) and not isinstance(v, bool) and not pd.isna(v):
                return float(v)
            v = self.extra.get(k)
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
    try:
        f.years = yearly(t.income_stmt, t.balance_sheet, t.cashflow)
    except Exception as exc:
        log.info("Annual statements %s unavailable: %s", sym, exc.__class__.__name__)
    f.extra = derived(f)
    f.checks = score(f)
    return f


def _row(stmt: pd.DataFrame | None, *names) -> pd.Series | None:
    if stmt is None or not isinstance(stmt, pd.DataFrame) or stmt.empty:
        return None
    for n in names:
        if n in stmt.index:
            return pd.to_numeric(stmt.loc[n], errors="coerce")
    return None


def quarterly(stmt: pd.DataFrame | None) -> pd.DataFrame | None:
    rows = {}
    for key, names in (("revenue", ["Total Revenue", "Operating Revenue"]),
                       ("net_income", ["Net Income", "Net Income Common Stockholders"])):
        r = _row(stmt, *names)
        if r is not None:
            rows[key] = r
    if not rows:
        return None
    q = pd.DataFrame(rows).T
    return q[sorted(q.columns)[-5:]]  # oldest -> newest, up to 5 quarters (for year-on-year)


def yearly(inc, bal, cf) -> pd.DataFrame | None:
    spec = {
        "revenue": (inc, ["Total Revenue", "Operating Revenue"]),
        "net_income": (inc, ["Net Income", "Net Income Common Stockholders"]),
        "ebit": (inc, ["EBIT", "Operating Income"]),
        "interest": (inc, ["Interest Expense", "Interest Expense Non Operating"]),
        "total_assets": (bal, ["Total Assets"]),
        "current_liab": (bal, ["Current Liabilities", "Total Current Liabilities"]),
        "equity": (bal, ["Stockholders Equity", "Total Equity Gross Minority Interest", "Common Stock Equity"]),
        "ocf": (cf, ["Operating Cash Flow", "Cash Flow From Continuing Operating Activities"]),
        "capex": (cf, ["Capital Expenditure"]),
    }
    rows = {k: r for k, (st, names) in spec.items() if (r := _row(st, *names)) is not None}
    if not rows:
        return None
    y = pd.DataFrame(rows).T
    return y[sorted(y.columns)[-4:]]


def _last(y: pd.DataFrame | None, key: str):
    if y is None or key not in y.index:
        return None
    s = y.loc[key].dropna()
    return float(s.iloc[-1]) if len(s) else None


def _cagr(y: pd.DataFrame | None, key: str):
    if y is None or key not in y.index:
        return None
    s = y.loc[key].dropna()
    if len(s) < 3 or s.iloc[0] <= 0 or s.iloc[-1] <= 0:
        return None
    n = len(s) - 1
    return (s.iloc[-1] / s.iloc[0]) ** (1 / n) - 1


def derived(f: Fundamentals) -> dict:
    y = f.years
    out = {}
    ebit, ta, cl = _last(y, "ebit"), _last(y, "total_assets"), _last(y, "current_liab")
    if ebit is not None and ta and cl is not None and ta - cl > 0:
        out["roce"] = ebit / (ta - cl)
    intr = _last(y, "interest")
    if ebit is not None and intr:
        out["interest_cover"] = ebit / abs(intr)
    ocf, ni = _last(y, "ocf"), _last(y, "net_income")
    if ocf is not None and ni and ni > 0:
        out["cash_conversion"] = ocf / ni
    out["rev_cagr"] = _cagr(y, "revenue")
    out["profit_cagr"] = _cagr(y, "net_income")
    pe = f.info.get("trailingPE")
    if isinstance(pe, (int, float)) and pe > 0:
        out["earnings_yield"] = 1 / pe
    fcf, rev = f.info.get("freeCashflow"), f.info.get("totalRevenue")
    if isinstance(fcf, (int, float)) and isinstance(rev, (int, float)) and rev:
        out["fcf_margin"] = fcf / rev
    return {k: v for k, v in out.items() if v is not None}


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


def grade(v, good, ok, higher_better=True):
    """🟢 / 🟡 / 🔴 for a value against thresholds."""
    if v is None:
        return None
    if higher_better:
        return GOOD if v >= good else MID if v >= ok else BAD
    return GOOD if v <= good else MID if v <= ok else BAD


def score(f: Fundamentals) -> list:
    """The 8-point health check (kept from the first version; also used for the verdict)."""
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
    icon = GOOD if tone == "Strong" else BAD if tone == "Weak" else MID
    return f"{icon} {tone} fundamentals — {good} of {n} checks pass"


# ---------------------------------------------------------------- ratio rows (label, value text, emoji, why)

def ratios(f: Fundamentals) -> dict[str, list[tuple[str, str, str | None, str]]]:
    g, fin = f.get, f.is_financial
    pe, pb = g("trailingPE"), g("priceToBook")
    sec: dict[str, list] = {}

    def put(section, label, val, emoji, why=""):
        if val is not None:
            sec.setdefault(section, []).append((label, val, emoji, why))

    # valuation
    put("📊 VALUATION", "P/E", f"{pe:.1f}" if pe else None, grade(pe, 25, 45, False) if pe and pe > 0 else BAD,
        "price per ₹1 of profit (lower = cheaper)")
    fpe = g("forwardPE")
    put("📊 VALUATION", "Forward P/E", f"{fpe:.1f}" if fpe else None,
        (GOOD if pe and fpe < pe else MID) if fpe and fpe > 0 else None, "on next year's expected profit")
    peg = g("trailingPegRatio", "pegRatio")
    put("📊 VALUATION", "PEG", f"{peg:.2f}" if peg else None, grade(peg, 1.0, 2.0, False) if peg and peg > 0 else None,
        "P/E ÷ growth; below 1 = growth at a fair price")
    put("📊 VALUATION", "P/B", f"{pb:.2f}" if pb else None, grade(pb, 3, 8, False) if pb else None,
        "price vs book value")
    ps = g("priceToSalesTrailing12Months")
    put("📊 VALUATION", "P/S", f"{ps:.2f}" if ps else None, grade(ps, 3, 8, False) if ps else None, "price vs sales")
    ev = g("enterpriseToEbitda")
    if not fin:
        put("📊 VALUATION", "EV/EBITDA", f"{ev:.1f}" if ev else None, grade(ev, 15, 30, False) if ev and ev > 0 else None,
            "whole-company value vs operating profit")
    ey = g("earnings_yield")
    put("📊 VALUATION", "Earnings yield", _pct(ey) if ey else None, grade(ey, 0.05, 0.025) if ey else None,
        "profit ÷ price (compare with ~7% bond yields)")
    dy = f.dividend_yield_pct
    put("📊 VALUATION", "Dividend yield", f"{dy:.2f}%" if dy is not None else None, grade(dy, 1.5, 0.5) if dy is not None
        else None, "cash paid out ÷ price")

    # profitability
    roe, roa, roce = g("returnOnEquity"), g("returnOnAssets"), g("roce")
    put("📈 PROFITABILITY", "ROE", _pct(roe) if roe is not None else None, grade(roe, 0.15, 0.10), "return on shareholders' money")
    if not fin:
        put("📈 PROFITABILITY", "ROCE", _pct(roce) if roce is not None else None, grade(roce, 0.15, 0.10),
            "return on all capital used (debt + equity)")
    put("📈 PROFITABILITY", "ROA", _pct(roa) if roa is not None else None, grade(roa, 0.07 if not fin else 0.012,
                                                                             0.03 if not fin else 0.007),
        "profit on total assets")
    for key, label, good, ok in (("grossMargins", "Gross margin", 0.40, 0.20), ("ebitdaMargins", "EBITDA margin", 0.20, 0.10),
                                 ("operatingMargins", "Operating margin", 0.15, 0.08),
                                 ("profitMargins", "Net margin", 0.10, 0.05)):
        v = g(key)
        if fin and key in ("grossMargins", "ebitdaMargins"):
            continue
        put("📈 PROFITABILITY", label, _pct(v) if v is not None else None, grade(v, good, ok))

    # growth
    for key, label in (("revenueGrowth", "Revenue (quarter, YoY)"), ("earningsGrowth", "Profit (quarter, YoY)"),
                       ("rev_cagr", "Revenue 3-yr CAGR"), ("profit_cagr", "Profit 3-yr CAGR")):
        v = g(key) if key != "earningsGrowth" else g("earningsGrowth", "earningsQuarterlyGrowth")
        put("🚀 GROWTH", label, _pct(v) if v is not None else None, grade(v, 0.12, 0.0))

    # balance sheet
    de = f.debt_to_equity
    if not fin:
        put("🏦 BALANCE SHEET", "Debt / equity", f"{de:.2f}" if de is not None else None, grade(de, 0.5, 1.5, False),
            "borrowing vs own money")
        cr, qr = g("currentRatio"), g("quickRatio")
        put("🏦 BALANCE SHEET", "Current ratio", f"{cr:.2f}" if cr else None, grade(cr, 1.5, 1.0) if cr else None,
            "short-term assets ÷ bills due")
        put("🏦 BALANCE SHEET", "Quick ratio", f"{qr:.2f}" if qr else None, grade(qr, 1.0, 0.7) if qr else None,
            "same without inventory")
        ic = g("interest_cover")
        put("🏦 BALANCE SHEET", "Interest cover", f"{ic:.1f}×" if ic else None, grade(ic, 5, 2) if ic else None,
            "operating profit ÷ interest")
    cash, debt = g("totalCash"), g("totalDebt")
    if cash is not None and debt is not None and not fin:
        net = debt - cash
        put("🏦 BALANCE SHEET", "Net debt", _cr(net), GOOD if net <= 0 else (MID if de is not None and de < 1 else BAD),
            "debt minus cash (negative = net cash)")
    elif cash is not None:
        put("🏦 BALANCE SHEET", "Cash", _cr(cash), None)

    # cash flow
    if not fin:
        fcf, fm, conv = g("freeCashflow"), g("fcf_margin"), g("cash_conversion")
        put("💵 CASH FLOW", "Free cash flow", _cr(fcf) if fcf is not None else None, GOOD if fcf and fcf > 0 else BAD
            if fcf is not None else None, "cash left after capex")
        put("💵 CASH FLOW", "FCF margin", _pct(fm) if fm is not None else None, grade(fm, 0.08, 0.0))
        put("💵 CASH FLOW", "Cash conversion (OCF ÷ profit)", f"{conv:.2f}×" if conv else None,
            grade(conv, 0.9, 0.6) if conv else None, "profits backed by real cash")
    payout = g("payoutRatio")
    put("💵 CASH FLOW", "Dividend payout", _pct(payout) if payout else None,
        (GOOD if 0.1 <= payout <= 0.7 else MID) if payout else None)

    # ownership + price
    ins, inst = g("heldPercentInsiders"), g("heldPercentInstitutions")
    put("👥 HOLDINGS", "Promoters / insiders", _pct(ins) if ins is not None else None, grade(ins, 0.45, 0.25),
        "skin in the game")
    put("👥 HOLDINGS", "Institutions (FII + DII)", _pct(inst) if inst is not None else None, grade(inst, 0.25, 0.10))
    beta = g("beta")
    put("👥 HOLDINGS", "Beta", f"{beta:.2f}" if beta is not None else None, grade(beta, 1.0, 1.4, False)
        if beta is not None else None, "1 = moves with the market")
    price, d50, d200 = f.price, g("fiftyDayAverage"), g("twoHundredDayAverage")
    if price and d200:
        put("💹 PRICE", "vs 200-day average", f"{(price / d200 - 1) * 100:+.1f}%", GOOD if price > d200 else BAD,
            "above = long-term uptrend")
    if price and d50:
        put("💹 PRICE", "vs 50-day average", f"{(price / d50 - 1) * 100:+.1f}%", GOOD if price > d50 else BAD)
    hi, lo = g("fiftyTwoWeekHigh"), g("fiftyTwoWeekLow")
    if price and hi and lo and hi > lo:
        pos = (price - lo) / (hi - lo)
        put("💹 PRICE", "52-week position", f"{pos * 100:.0f}% ({lo:,.0f}–{hi:,.0f})",
            GOOD if pos >= 0.6 else MID if pos >= 0.3 else BAD)
    tgt, rec = g("targetMeanPrice"), f.info.get("recommendationKey")
    if price and tgt:
        up = tgt / price - 1
        put("💹 PRICE", "Analyst target (mean)", f"₹{tgt:,.0f} ({up * 100:+.0f}%"
            + (f", {html.escape(str(rec)).replace('_', ' ')}" if rec else "") + ")", grade(up, 0.10, 0.0))
    return sec


def pros_cons(f: Fundamentals) -> tuple[list[str], list[str]]:
    pros, cons = [], []
    for rows in ratios(f).values():
        for label, val, emoji, _ in rows:
            if emoji == GOOD:
                pros.append(f"{label} {val}")
            elif emoji == BAD:
                cons.append(f"{label} {val}")
    return pros, cons


def _head(f: Fundamentals, title: str) -> list[str]:
    i = f.info
    sector = " · ".join(html.escape(str(x)) for x in (i.get("sector"), i.get("industry")) if x)
    lines = [f"🏢 <b>{html.escape(f.symbol)} · {title}</b>",
             f"<i>{html.escape(f.name)}{(' · ' + sector) if sector else ''}</i>", LINE]
    if f.get("marketCap") is not None:
        mc = f.get("marketCap")
        size = "large cap" if mc >= 2e11 else "mid cap" if mc >= 5e10 else "small cap"
        lines.append(f"💰 Market cap <b>{_cr(mc)}</b> ({size})")
    if f.price is not None:
        lines.append(f"💹 Price ₹{f.price:,.2f}")
    return lines


BRIEF_KEYS = ["P/E", "PEG", "P/B", "ROE", "ROCE", "Net margin", "Revenue (quarter, YoY)", "Profit (quarter, YoY)",
              "Debt / equity", "Free cash flow", "Promoters / insiders", "vs 200-day average"]


def brief_text(f: Fundamentals) -> str:
    """First message: the key ratios with 🟢 / 🔴, the verdict and the top strengths and weaknesses."""
    lines = _head(f, "FUNDAMENTALS")
    rows = {lbl: (val, em) for sec in ratios(f).values() for lbl, val, em, _ in sec}
    lines.append("")
    for k in BRIEF_KEYS:
        if k in rows:
            val, em = rows[k]
            lines.append(f"{em or '⚪'} {html.escape(k)}: <b>{html.escape(val)}</b>")
    pros, cons = pros_cons(f)
    if pros:
        lines += ["", f"{GOOD} <b>Strengths:</b> " + html.escape(", ".join(pros[:5]))]
    if cons:
        lines.append(f"{BAD} <b>Weaknesses:</b> " + html.escape(", ".join(cons[:5])))
    if f.is_financial:
        lines.append("ℹ️ Bank/finance company: debt, current ratio and cash flow are not meaningful, so skipped.")
    lines += ["", f"<b>Verdict:</b> {html.escape(verdict(f))}",
              "👇 Tap <b>📑 Detailed fundamentals</b> for every ratio, quarterly &amp; yearly results."]
    lines.append("<i>Source: Yahoo Finance (unofficial, can be late). Not financial advice.</i>")
    return "\n".join(lines)


def detailed_text(f: Fundamentals) -> str:
    lines = _head(f, "DETAILED FUNDAMENTALS")
    for title, rows in ratios(f).items():
        lines += ["", f"<b>{title}</b>"]
        for label, val, emoji, why in rows:
            w = f" — <i>{html.escape(why)}</i>" if why else ""
            lines.append(f"{emoji or '⚪'} {html.escape(label)}: <b>{html.escape(val)}</b>{w}")
    if f.quarters is not None and f.quarters.shape[1] >= 2:
        q = f.quarters
        tbl = [f"{'Quarter':<9}{'Revenue':>12}{'Profit':>11}"]
        for col in q.columns[-4:]:
            rev = q.at["revenue", col] if "revenue" in q.index else None
            ni = q.at["net_income", col] if "net_income" in q.index else None
            tbl.append(f"{pd.Timestamp(col):%b %Y}".ljust(9)
                       + f"{_cr(rev) if rev is not None and not pd.isna(rev) else '–':>12}"
                       + f"{_cr(ni) if ni is not None and not pd.isna(ni) else '–':>11}")
        lines += ["", "<b>🧾 LAST QUARTERS</b>", "<pre>" + html.escape("\n".join(tbl)) + "</pre>"]
    if f.years is not None and f.years.shape[1] >= 2:
        y = f.years
        tbl = [f"{'Year':<6}{'Revenue':>12}{'Profit':>11}{'OCF':>11}"]
        for col in y.columns:
            def cell(k):
                v = y.at[k, col] if k in y.index else None
                return _cr(v) if v is not None and not pd.isna(v) else "–"
            tbl.append(f"{pd.Timestamp(col):%Y}".ljust(6) + f"{cell('revenue'):>12}{cell('net_income'):>11}"
                       f"{cell('ocf'):>11}")
        lines += ["", "<b>📅 YEARLY</b>", "<pre>" + html.escape("\n".join(tbl)) + "</pre>"]
    if f.checks:
        lines += ["", "<b>✅ HEALTH CHECK</b>"]
        icon = {"✅": GOOD, "⚠️": MID, "❌": BAD}
        lines += [f"{icon.get(st, st)} {html.escape(lbl)} — <i>{html.escape(why)}</i>" for st, lbl, why in f.checks]
        if f.is_financial:
            lines.append("ℹ️ Bank/finance company: debt, current ratio and cash flow are not meaningful, so skipped.")
    pros, cons = pros_cons(f)
    lines += ["", f"{GOOD} <b>STRENGTHS</b>"] + [f"{GOOD} {html.escape(p)}" for p in pros] if pros else []
    if cons:
        lines += ["", f"{BAD} <b>WEAKNESSES</b>"] + [f"{BAD} {html.escape(c)}" for c in cons]
    lines += ["", f"<b>Verdict:</b> {html.escape(verdict(f))}",
              "<i>Ratios computed from Yahoo Finance statements (Tickertape/Screener style). Cross-check on the "
              "links below or in the annual report. Not financial advice.</i>"]
    return "\n".join(lines)


def report_text(f: Fundamentals) -> str:
    """Kept for older callers and tests: the detailed text."""
    return detailed_text(f)


def links(sym: str) -> list[tuple[str, str]]:
    return [("🔗 Screener", f"https://www.screener.in/company/{quote_plus(sym)}/"),
            ("🔗 Tickertape", f"https://www.google.com/search?q={quote_plus('tickertape ' + sym + ' share')}")]

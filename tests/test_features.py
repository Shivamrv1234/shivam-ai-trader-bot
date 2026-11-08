"""Any-stock search, futures, option idea, fundamentals, news pages, menus. All offline with fakes."""
import time
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pandas as pd
import pytest

from bot import angel, data, fno, fundamentals, futures, main, news, state, strategies, symbols, telegram
from bot.config import ROOT
from tests.test_bot import candles
from tests.test_fno import FAR, NEAR, SPOT, TODAY, FakeApi, fake_instruments

EXTRA = ["TATAMOTORS", "TATASTEEL", "TATAPOWER", "M&M", "INFY"]


def instruments_raw():
    rows = fake_instruments()
    for i, sym in enumerate(EXTRA):
        rows.append({"token": str(3000 + i), "symbol": f"{sym}-EQ", "name": sym, "expiry": "", "strike": "-1.000000",
                     "lotsize": "1", "instrumenttype": "", "exch_seg": "NSE", "tick_size": "5"})
    rows.append({"token": "50002", "symbol": f"NIFTY{FAR}FUT", "name": "NIFTY", "expiry": FAR, "strike": "-1.000000",
                 "lotsize": "75", "instrumenttype": "FUTIDX", "exch_seg": "NFO", "tick_size": "10"})
    rows.append({"token": "51000", "symbol": f"RELIANCE{NEAR}FUT", "name": "RELIANCE", "expiry": NEAR,
                 "strike": "-1.000000", "lotsize": "500", "instrumenttype": "FUTSTK", "exch_seg": "NFO",
                 "tick_size": "10"})
    return rows


class FakeApi2(FakeApi):
    """Adds futures quotes, OI build-up lists and historical OI."""

    def getMarketData(self, mode, tokens):
        resp = super().getMarketData(mode, tokens)
        for q in resp["data"]["fetched"]:
            r = self.inst[q["symbolToken"]]
            if r["instrumenttype"] in ("FUTIDX", "FUTSTK"):
                far = r["expiry"] == FAR
                q.update({"ltp": SPOT + (180 if far else 65), "percentChange": 0.8, "opnInterest": 1.2e7,
                          "tradeVolume": 3.4e6, "high": SPOT + 120, "low": SPOT - 60, "close": SPOT - 100})
        return resp

    def oIBuildup(self, params):
        assert params["expirytype"] == "NEAR"
        rows = []
        if params["datatype"] == "Long Built Up":
            rows = [{"symbolToken": "50001", "ltp": str(SPOT + 65), "netChange": "190", "percentChange": "0.8",
                     "opnInterest": "12000000", "netChangeOpnInterest": "350000", "tradingSymbol": f"NIFTY{NEAR}FUT"}]
        return {"status": True, "message": "SUCCESS", "data": rows}

    def getOIData(self, params):
        days = pd.bdate_range(end=TODAY - timedelta(days=1), periods=6)
        return {"status": True, "data": [{"time": f"{d:%Y-%m-%d}T00:00:00+05:30", "oi": 1.0e7 + i * 4e5}
                                         for i, d in enumerate(days)]}


def session2():
    a = object.__new__(angel.Angel)
    raw = instruments_raw()
    a.api = FakeApi2(raw)
    a._inst = angel.load_instruments(raw)
    a.last_keys = {}
    return a


@pytest.fixture
def ui(monkeypatch, tmp_path):
    """Capture every Telegram call, including in-place edits."""
    log = []
    monkeypatch.setattr(telegram, "send_text", lambda t, chat_id=None, buttons=None: log.append(("text", t, buttons)))
    monkeypatch.setattr(telegram, "send_photo",
                        lambda png, caption="", chat_id=None, buttons=None: log.append(("photo", caption, buttons)))
    monkeypatch.setattr(telegram, "edit_text",
                        lambda t, mid, chat_id=None, buttons=None: log.append(("edit", t, buttons)))
    monkeypatch.setattr(telegram, "set_commands", lambda: None)
    monkeypatch.setattr(telegram, "answer_callback", lambda cid, text="": None)
    monkeypatch.setattr(main.ai, "summarize", lambda facts: None)
    monkeypatch.setattr(angel.time, "sleep", lambda s: None)
    monkeypatch.setenv("BOT_STATE", str(tmp_path / "state.json"))
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "555")
    return log


@pytest.fixture
def live_angel(monkeypatch):
    sess = session2()
    monkeypatch.setattr(angel.time, "sleep", lambda s: None)
    monkeypatch.setattr(angel, "configured", lambda: True)
    monkeypatch.setattr(angel, "session", lambda: sess)
    return sess


def flat(buttons):
    return [b for row in (buttons or []) for b in row]


# ---------------------------------------------------------------- CI fix

def test_requirements_include_web_server():
    text = (ROOT / "requirements.txt").read_text()
    assert "flask" in text and "gunicorn" in text


# ---------------------------------------------------------------- search any NSE stock

def test_search_exact_partial_and_symbols_with_ampersand():
    inst = angel.load_instruments(instruments_raw())
    assert angel.search_equities(inst, "TATAMOTORS")[0] == "TATAMOTORS"
    assert set(angel.search_equities(inst, "tata")) == {"TATAMOTORS", "TATASTEEL", "TATAPOWER"}
    assert angel.search_equities(inst, "tata motors") == ["TATAMOTORS"]
    assert angel.search_equities(inst, "m&m") == ["M&M"]


def test_find_stock(live_angel):
    assert symbols.find_stock("reliance") == ("RELIANCE", [])
    assert symbols.find_stock("nifty") == ("NIFTY", [])
    sym, matches = symbols.find_stock("tata")
    assert sym is None and len(matches) == 3
    assert symbols.find_stock("tata steel") == ("TATASTEEL", [])
    assert symbols.has_fno("RELIANCE") and not symbols.has_fno("TATAMOTORS")


def test_find_stock_without_instrument_list_uses_yahoo(monkeypatch):
    monkeypatch.setattr(angel, "configured", lambda: False)
    monkeypatch.setattr(symbols, "yahoo_search", lambda q, limit=8: ["TATAMOTORS"] if "tata" in q else [])
    assert symbols.find_stock("tata motors") == ("TATAMOTORS", [])
    assert symbols.find_stock("XYZ") == ("XYZ", [])  # nothing to check against: try as typed


def test_stock_command_lists_matches_then_analyses(monkeypatch, ui, live_angel):
    monkeypatch.setattr(data, "stock_candles", lambda sym, timeframe="1d": candles(500, freq="D", start=900))
    main.handle("/stock tata")
    assert "NSE matches" in ui[0][1]
    assert ("TATASTEEL", "a:s:TATASTEEL") in flat(ui[0][2])
    ui.clear()
    main.handle_callback("a:s:RELIANCE", "555")
    assert [k for k, *_ in ui] == ["photo", "text"]
    btns = flat(ui[1][2])
    for want in [("⛓ Option chain", "fno:RELIANCE"), ("📈 Futures", "fu:RELIANCE"), ("🧮 Greeks", "gk:RELIANCE"),
                 ("🧠 F&O plan", "fp:RELIANCE"), ("🏢 Fundamentals", "fd:RELIANCE"), ("📰 News", "n:q:RELIANCE"),
                 ("🌡 Volatility / VIX", "vx:s:RELIANCE:1d"), main.HOME]:
        assert want in btns


def test_menu_button_then_typed_name(monkeypatch, ui, live_angel):
    monkeypatch.setattr(data, "stock_candles", lambda sym, timeframe="1d": candles(500, freq="D", start=900))
    main.handle_callback("m:stock", "555")
    assert "ANALYSE ANY NSE STOCK" in ui[0][1] and ("RELIANCE", "a:s:RELIANCE") in flat(ui[0][2])
    ui.clear()
    main.handle("tata motors", "555")
    assert [k for k, *_ in ui] == ["photo", "text"] and "TATAMOTORS" in ui[0][1]
    assert state.take_wait("555") is None   # used up


def test_typed_partial_name_offers_matches(monkeypatch, ui, live_angel):
    def no_coin(*a, **k):
        raise data.DataError("not on Binance")

    monkeypatch.setattr(data, "crypto_candles", no_coin)
    main.handle("tata", "555")          # not a coin, not an exact NSE symbol -> choose
    assert "NSE matches" in ui[0][1] and len(flat(ui[0][2])) == 4      # 3 matches + menu
    ui.clear()
    main.handle("tata power", "555")    # company name -> the one match is opened
    assert "TATAPOWER" in ui[0][1] or "NSE matches" in ui[0][1]


def test_wait_mode_expires(monkeypatch, tmp_path):
    monkeypatch.setenv("BOT_STATE", str(tmp_path / "s.json"))
    state.set_wait("1", "fund")
    assert state.take_wait("1") == "fund" and state.take_wait("1") is None
    state.set_wait("1", "fund")
    st = state.load()
    st["wait"]["1"]["ts"] -= state.WAIT_SECONDS + 5
    state.save(st)
    assert state.take_wait("1") is None


# ---------------------------------------------------------------- futures

def test_build_futures_all_expiries_basis_and_buildup(live_angel):
    rep = angel.build_futures(live_angel, "NIFTY")
    assert [l.expiry.strftime("%d%b%Y").upper() for l in rep.legs] == [NEAR, FAR]
    near = rep.legs[0]
    assert rep.basis(near) == pytest.approx(65) and near.lot == 75
    assert near.buildup == "Long Built Up"
    assert rep.oi_hist is not None and near.oi_change_pct == pytest.approx(4e5 / (1e7 + 4 * 4e5) * 100)
    carry = rep.carry_annual_pct(near)
    assert carry == pytest.approx(65 / SPOT * 100 * 365 / 10, rel=1e-6)
    text = futures.report_text(rep)
    assert "Near month" in text and "Next month" in text and "Long Built Up" in text and "premium" in text
    assert futures.chart(rep)[:4] == b"\x89PNG"


def test_classify_buildup():
    assert angel.classify_buildup(1.0, 5) == "Long Built Up"
    assert angel.classify_buildup(-1.0, 5) == "Short Built Up"
    assert angel.classify_buildup(1.0, -5) == "Short Covering"
    assert angel.classify_buildup(-1.0, -5) == "Long Unwinding"
    assert angel.classify_buildup(0, 5) is None


def test_stock_without_futures_explains(live_angel):
    with pytest.raises(angel.AngelError, match="no futures"):
        angel.build_futures(live_angel, "TATAMOTORS")


class StubAnalysis:
    def __init__(self, side="BUY", direction="BUY"):
        self.direction, self.agree, self.watches, self.forming_side = direction, 3, [], None
        self.n_applicable = 11
        if side == "BUY":
            self.s = strategies.Setup("k", "EMA Trend Cross", "i", "TRADE", entry=SPOT, stop=SPOT - 200,
                                      target=SPOT + 400)
        else:
            self.s = strategies.Setup("k", "EMA Trend Cross (SELL)", "i", "TRADE", entry=SPOT, stop=SPOT + 200,
                                      target=SPOT - 400, side="SELL")

    def best(self):
        return self.s

    def side_trades(self, side):
        return [self.s] if side == self.s.side else []


def test_futures_trade_plan_shifts_levels_and_flags_risk(live_angel):
    rep = angel.build_futures(live_angel, "NIFTY")
    text = futures.trade_plan(rep, StubAnalysis())
    assert "BUY (go long)" in text and f"{SPOT + 65:,.2f}" in text and f"{SPOT - 200 + 65:,.2f}" in text
    assert "risk ≈ ₹15,000" in text and "far more than your limit" in text   # 200 x 75
    short = futures.trade_plan(rep, StubAnalysis("SELL", "SELL"))
    assert "SELL (go short)" in short
    none = StubAnalysis()
    none.direction = "NONE"
    assert "no futures trade" in futures.trade_plan(rep, none)


def test_futures_button_end_to_end(monkeypatch, ui, live_angel):
    main.handle_callback("fu:NIFTY", "555")
    kinds = [k for k, *_ in ui]
    assert kinds == ["photo", "text"]
    assert "FUTURES REPORT" in ui[1][1] and "FUTURES TRADE PLAN" in ui[1][1]
    assert ("📊 Option chain", "fno:NIFTY") in flat(ui[1][2])
    assert all(len(c) <= 4096 for c in telegram.split_text(ui[1][1]))


def test_buildup_command(ui, live_angel):
    main.handle("/buildup", "555")
    assert "OI BUILD-UP" in ui[0][1] and f"NIFTY{NEAR}FUT" in ui[0][1]
    assert ("📈 NIFTY", "fu:NIFTY") in flat(ui[0][2])


def test_futures_need_angel(monkeypatch, ui):
    monkeypatch.setattr(angel, "configured", lambda: False)
    main.handle("/futures NIFTY", "555")
    assert "Angel One SmartAPI" in ui[0][1]


# ---------------------------------------------------------------- option idea

def test_option_idea_buy_call_and_sell_put(live_angel):
    snap = angel.build_snapshot(live_angel, "NIFTY", 5)
    buy = fno.option_idea(snap, StubAnalysis())
    assert f"BUY NIFTY {snap.atm:,.0f} CE" in buy and "delta 0.50" in buy and "Worst case" in buy
    ce = next(l for l in snap.side("CE") if l.strike == snap.atm)
    assert f"₹{ce.ltp * 75:,.0f}" in buy
    assert f"≈ ₹{ce.ltp + 0.5 * 400:,.2f}" in buy           # premium at take profit with delta 0.5
    sell = fno.option_idea(snap, StubAnalysis("SELL", "SELL"))
    assert f"{snap.atm:,.0f} PE" in sell
    none = StubAnalysis()
    none.direction = "MIXED"
    assert "no option idea" in fno.option_idea(snap, none)


# ---------------------------------------------------------------- fundamentals

INFO = {"longName": "Infosys Limited", "sector": "Technology", "industry": "Information Technology Services",
        "marketCap": 6.4e12, "currentPrice": 1540.0, "fiftyTwoWeekHigh": 2000.0, "fiftyTwoWeekLow": 1300.0,
        "trailingPE": 23.5, "forwardPE": 21.0, "priceToBook": 7.1, "trailingEps": 65.5, "bookValue": 217.0,
        "returnOnEquity": 0.31, "returnOnAssets": 0.18, "operatingMargins": 0.21, "profitMargins": 0.17,
        "revenueGrowth": 0.08, "earningsGrowth": -0.02, "debtToEquity": 9.0, "currentRatio": 2.3,
        "totalCash": 4.0e11, "totalDebt": 8.0e10, "freeCashflow": 2.5e11, "dividendRate": 43.0,
        "dividendYield": 2.79, "payoutRatio": 0.65, "heldPercentInsiders": 0.14, "heldPercentInstitutions": 0.55,
        "beta": 0.6}


def test_fundamentals_scoring_and_units():
    f = fundamentals.Fundamentals("INFY", dict(INFO))
    f.checks = fundamentals.score(f)
    marks = {lbl.split()[0]: st for st, lbl, _ in f.checks}
    assert marks["Return"] == "✅" and marks["Debt"] == "✅" and marks["Revenue"] == "⚠️" and marks["Earnings"] == "❌"
    assert f.debt_to_equity == pytest.approx(0.09)              # Yahoo gives 9.0 meaning 9%
    assert f.dividend_yield_pct == pytest.approx(43 / 1540 * 100)  # not Yahoo's ambiguous dividendYield
    text = fundamentals.report_text(f)
    assert "₹6.40 L Cr" in text and "P/E: <b>23.5</b>" in text and "Debt / equity: <b>0.09</b>" in text
    assert "Verdict" in text and "52-week position: <b>34%" in text
    assert "🟢 Debt / equity" in text and "🔴 Profit (quarter, YoY)" in text    # green pros, red cons
    brief = fundamentals.brief_text(f)
    assert "🟢 ROE: <b>31.0%</b>" in brief and "Strengths" in brief and "Weaknesses" in brief
    assert "Detailed fundamentals" in brief and len(brief) < len(text)


def test_fundamentals_bank_skips_debt_checks():
    bank = dict(INFO, sector="Financial Services", industry="Banks - Regional", debtToEquity=650.0)
    f = fundamentals.Fundamentals("HDFCBANK", bank)
    f.checks = fundamentals.score(f)
    assert not any(lbl.startswith(("Debt", "Current", "Free")) for _, lbl, _ in f.checks)
    assert "not meaningful" in fundamentals.report_text(f)


def test_fundamentals_fetch_and_button(monkeypatch, ui):
    import yfinance as yf

    q = pd.DataFrame({pd.Timestamp("2026-03-31"): [4.0e11, 7.0e10], pd.Timestamp("2026-06-30"): [4.2e11, 6.9e10]},
                     index=["Total Revenue", "Net Income"])

    class T:
        def __init__(self, sym):
            assert sym == "INFY.NS"
            self.info = dict(INFO)
            self.quarterly_income_stmt = q

    monkeypatch.setattr(yf, "Ticker", T)
    main.handle_callback("fd:INFY", "555")
    text = ui[0][1]
    assert "INFY · FUNDAMENTALS" in text and "LAST QUARTERS" not in text
    assert ("📈 Technical analysis", "a:s:INFY") in flat(ui[0][2])
    assert ("📑 Detailed fundamentals", "fdd:INFY") in flat(ui[0][2])
    assert ("🔗 Screener", "https://www.screener.in/company/INFY/") in flat(ui[0][2])
    ui.clear()
    main.handle_callback("fdd:INFY", "555")
    text = ui[0][1]
    assert "DETAILED FUNDAMENTALS" in text and "LAST QUARTERS" in text and "Jun 2026" in text
    assert ("📑 Detailed fundamentals", "fdd:INFY") not in flat(ui[0][2])


def test_fundamentals_missing_data(monkeypatch, ui):
    import yfinance as yf

    class Empty:
        def __init__(self, sym):
            self.info = {}

    monkeypatch.setattr(yf, "Ticker", Empty)
    main.safe_handle("/fundamentals NOPE")
    assert "Data problem" in ui[0][1]


def test_index_has_no_fundamentals(ui):
    main.handle("/fundamentals NIFTY", "555")
    assert "Indices have no company fundamentals" in ui[0][1]


# ---------------------------------------------------------------- news

def rss(items, source_tag=False):
    body = []
    for title, desc, hours in items:
        when = format_datetime(datetime.now(timezone.utc) - timedelta(hours=hours))
        src = "<source url='https://x.com'>Reuters</source>" if source_tag else ""
        body.append(f"<item><title>{title}{' - Reuters' if source_tag else ''}</title><link>https://ex.com/{abs(hash(title))}"
                    f"</link><description><![CDATA[{desc}]]></description><pubDate>{when}</pubDate>{src}</item>")
    return f"<?xml version='1.0'?><rss><channel>{''.join(body)}</channel></rss>".encode()


def test_parse_rss_google_and_atom():
    items = news.parse_feed(rss([("Crude jumps 4% as Iran tensions flare", "<p>Brent rose to <b>$90</b>.</p>", 1)]),
                            "ET", "india")
    assert items[0].title.startswith("Crude jumps") and items[0].summary == "Brent rose to $90 ." \
        or items[0].summary == "Brent rose to $90."
    assert items[0].ts and time.time() - items[0].ts < 2 * 3600
    g = news.parse_feed(rss([("Fed holds rates steady", "Fed holds rates steady Reuters", 2)], source_tag=True),
                        "Google News", "world")
    assert g[0].title == "Fed holds rates steady" and g[0].source == "Reuters" and g[0].summary == ""
    atom = (b"<feed xmlns='http://www.w3.org/2005/Atom'><entry><title>Bitcoin ETF inflows rise</title>"
            b"<link href='https://c.com/a'/><updated>2026-11-07T10:00:00Z</updated><summary>Flows up.</summary>"
            b"</entry></feed>")
    a = news.parse_feed(atom, "CoinDesk", "crypto")
    assert a[0].link == "https://c.com/a" and a[0].summary == "Flows up." and a[0].ts
    assert news.parse_feed(b"not xml", "x", "india") == []


def test_tagging_ranking_and_link_sentence():
    raw = [
        news.Item("Brent crude jumps 4% after Iran strikes", "u1", "ET", "world", time.time() - 3600),
        news.Item("Celebrity wedding photos go viral", "u2", "BBC", "world", time.time() - 600),
        news.Item("Tata Motors shares rise on strong EV sales", "u3", "ET", "india", time.time() - 7200),
        news.Item("Brent crude jumps 4% after Iran strikes, oil", "u4", "Mint", "india", time.time() - 3500),
        news.Item("Old RBI story", "u5", "ET", "india", time.time() - 72 * 3600),
        news.Item("Bitcoin slips below $90,000", "u6", "CoinDesk", "crypto", time.time() - 300),
    ]
    out = news.rank(raw)
    titles = [i.title for i in out]
    assert "Celebrity wedding photos go viral" not in titles      # world story with no market angle
    assert "Old RBI story" not in titles                          # older than 48h
    assert sum("Brent crude" in t for t in titles) == 1           # near-duplicate removed
    crude = next(i for i in out if "Brent" in i.title)
    assert "crude" in crude.rules and "war" in crude.rules
    assert news.link_sentence(crude).startswith("oil & gas")
    tata = next(i for i in out if "Tata" in i.title)
    assert tata.stocks == ["TATAMOTORS"] and news.link_sentence(tata).startswith("TATAMOTORS directly")
    btc = next(i for i in out if "Bitcoin" in i.title)
    assert btc.coins == ["BTC"] and "crypto" in news.link_sentence(btc)


def fake_feeds(monkeypatch, n=23):
    def fetch(url, source, cat):
        if cat != "india":
            return []
        return [news.Item(f"Story {k}: RBI and banks update number {k} " + "x" * (k % 3), f"https://ex.com/{k}", source,
                          cat, time.time() - k * 60, summary=f"Summary {k}.") for k in range(n)]

    monkeypatch.setattr(news, "fetch_feed", fetch)
    monkeypatch.setattr(news, "rank", lambda items, now=None: [news.tag(i) for i in items][:news.MAX_ITEMS])


def test_news_pages_numbers_and_tabs(monkeypatch, ui):
    fake_feeds(monkeypatch)
    main.handle("/news", "555")
    text, btns = ui[0][1], ui[0][2]
    assert "MARKET NEWS" in text and "page 1/" in text and "<b>1.</b>" in text and "<b>10.</b>" in text
    assert "<b>11.</b>" not in text and "links to" in text
    labels = flat(btns)
    assert ("1", "n:d:1") in labels and ("10", "n:d:10") in labels and ("Next ▶️", "n:p:2") in labels
    assert ("◀️ Prev", "n:p:0") not in labels and ("• 🌍 All", "n:t:all") in labels
    ui.clear()
    main.handle_callback("n:p:2", "555", 42)                       # flips in place like a tab
    assert ui[0][0] == "edit" and "<b>11.</b>" in ui[0][1] and "<b>20.</b>" in ui[0][1]
    assert ("◀️ Prev", "n:p:1") in flat(ui[0][2])


def test_news_number_reply_gives_detail(monkeypatch, ui):
    fake_feeds(monkeypatch)
    main.handle("/news", "555")
    ui.clear()
    main.handle("12", "555")
    text, btns = ui[0][1], flat(ui[0][2])
    assert "STORY 12 of" in text and "Summary 11." in text and "Market link" in text and "RBI" in text
    assert ("🔗 Read full article", "https://ex.com/11") in btns
    assert ("⬅️ Back to list", "n:p:2") in btns and ("Next story ➡️", "n:d:13") in btns
    assert any(d.startswith("a:s:") for _, d in btns)              # stocks to watch are tappable
    ui.clear()
    main.handle("99", "555")
    assert "Send /news first" in ui[0][1]


def test_number_goes_to_the_latest_list(monkeypatch, ui):
    fake_feeds(monkeypatch)
    monkeypatch.setattr(data, "top_crypto_by_volume", lambda n: [{"symbol": "BTCUSDT", "change_pct": 1.0}])
    monkeypatch.setattr(data, "crypto_candles", lambda sym, timeframe=None, limit=3000: candles())
    main.handle("/news", "555")
    main.handle("/crypto", "555")
    ui.clear()
    main.handle("1", "555")
    assert [k for k, *_ in ui] == ["photo", "text"]        # scanner pick, not news
    main.handle("/news", "555")
    ui.clear()
    main.handle("1", "555")
    assert "STORY 1 of" in ui[0][1]


def test_stock_news_and_empty_news(monkeypatch, ui):
    monkeypatch.setattr(news, "fetch_feed", lambda url, source, cat: [
        news.Item("Company posts record quarterly profit", "https://ex.com/r", "ET", cat, time.time())])
    main.handle_callback("n:q:RELIANCE", "555")
    assert "RELIANCE NEWS" in ui[0][1] and "RELIANCE directly" in ui[0][1]
    ui.clear()
    monkeypatch.setattr(news, "fetch_feed", lambda url, source, cat: [])
    main.handle("/news crypto", "555")
    assert "No fresh stories" in ui[0][1]


def test_news_detail_uses_article_description_and_ai(monkeypatch, ui):
    monkeypatch.setattr(news, "fetch_feed", lambda url, source, cat: [
        news.Item("Brent crude surges on OPEC cut", "https://ex.com/o", "ET", cat, time.time())])
    monkeypatch.setattr(news, "article_description", lambda url: "OPEC+ agreed to cut output by 1 million barrels.")
    monkeypatch.setattr(main.ai, "news_take", lambda t, s, th: "Higher oil can hurt OMCs and airlines.")
    main.handle("/news", "555")
    main.handle("1", "555")
    text = ui[-1][1]
    assert "OPEC+ agreed" in text and "AI take" in text and "OMCs" in text
    assert ("📈 ONGC", "a:s:ONGC") in flat(ui[-1][2])


# ---------------------------------------------------------------- telegram helpers + menu

def test_keyboard_url_buttons_and_64_byte_limit():
    import json
    kb = json.loads(telegram.keyboard([[("Open", "https://ex.com/x"), ("Tap", "a:s:" + "X" * 80)], []]))
    row = kb["inline_keyboard"][0]
    assert row[0] == {"text": "Open", "url": "https://ex.com/x"} and len(row[1]["callback_data"].encode()) <= 64
    assert len(kb["inline_keyboard"]) == 1


def test_edit_ignores_not_modified_and_falls_back(monkeypatch):
    calls = []

    def fake_call(method, payload=None, files=None):
        calls.append(method)
        if method == "editMessageText":
            raise telegram.TelegramError("editMessageText: Bad Request: message is not modified")

    monkeypatch.setattr(telegram, "_call", fake_call)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "555")
    telegram.edit_text("hi", 7)
    assert calls == ["editMessageText"]

    def gone(method, payload=None, files=None):
        calls.append(method)
        if method == "editMessageText":
            raise telegram.TelegramError("editMessageText: Bad Request: message to edit not found")

    calls.clear()
    monkeypatch.setattr(telegram, "_call", gone)
    telegram.edit_text("hi", 7)
    assert calls == ["editMessageText", "sendMessage"]


def test_every_menu_button_works(monkeypatch, ui, live_angel):
    monkeypatch.setattr(data, "top_crypto_by_volume", lambda n: [{"symbol": "BTCUSDT", "change_pct": 1.0}])
    monkeypatch.setattr(data, "crypto_candles", lambda sym, timeframe=None, limit=3000: candles())
    monkeypatch.setattr(data, "stock_candles", lambda sym, timeframe="1d": candles(500, freq="D", start=900))
    fake_feeds(monkeypatch, 5)
    for label, cb in flat(main.MENU):
        ui.clear()
        main._safely(cb, main.handle_callback, cb, "555", None)
        assert ui, f"{label} sent nothing"
        assert not any("Something went wrong" in str(m[1]) for m in ui), f"{label} failed: {ui}"


def test_commands_menu_matches_handlers():
    names = [c for c, _ in telegram.COMMANDS]
    assert {"stock", "search", "fundamentals", "futures", "news", "buildup"} <= set(names)
    assert all(len(d) <= 256 for _, d in telegram.COMMANDS)

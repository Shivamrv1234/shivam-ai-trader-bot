"""F&O tests against a fake Angel One that follows the confirmed formats:
instrument list (strike in paise, expiry DDMMMYYYY), quotes in data.fetched,
optionGreek rows by strike/type, candles as [ts, o, h, l, c, v]."""
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from bot import angel, data, fno, main, report, telegram

TODAY = datetime.now(angel.IST).date()
NEAR = (TODAY + timedelta(days=10)).strftime("%d%b%Y").upper()
FAR = (TODAY + timedelta(days=40)).strftime("%d%b%Y").upper()
PAST = (TODAY - timedelta(days=5)).strftime("%d%b%Y").upper()
SPOT = 24020.0
STRIKES = list(range(23000, 25100, 100))


def fake_instruments():
    rows = [
        {"token": "99926000", "symbol": "Nifty 50", "name": "NIFTY", "expiry": "", "strike": "-1.000000",
         "lotsize": "1", "instrumenttype": "AMXIDX", "exch_seg": "NSE", "tick_size": "0"},
        {"token": "2885", "symbol": "RELIANCE-EQ", "name": "RELIANCE", "expiry": "", "strike": "-1.000000",
         "lotsize": "1", "instrumenttype": "", "exch_seg": "NSE", "tick_size": "5"},
        {"token": "50001", "symbol": f"NIFTY{NEAR}FUT", "name": "NIFTY", "expiry": NEAR, "strike": "-1.000000",
         "lotsize": "75", "instrumenttype": "FUTIDX", "exch_seg": "NFO", "tick_size": "10"},
        {"token": "40000", "symbol": f"NIFTY{PAST}24000CE", "name": "NIFTY", "expiry": PAST,
         "strike": "2400000.000000", "lotsize": "75", "instrumenttype": "OPTIDX", "exch_seg": "NFO", "tick_size": "5"},
    ]
    tok = 60000
    for exp in (NEAR, FAR):
        for k in STRIKES:
            for t in ("CE", "PE"):
                tok += 1
                rows.append({"token": str(tok), "symbol": f"NIFTY{exp}{k}{t}", "name": "NIFTY", "expiry": exp,
                             "strike": f"{k * 100}.000000", "lotsize": "75", "instrumenttype": "OPTIDX",
                             "exch_seg": "NFO", "tick_size": "5"})
    return rows


class FakeApi:
    def __init__(self, inst, oi_field="opnInterest"):
        self.inst = {str(r["token"]): r for r in inst}
        self.oi_field = oi_field

    def getMarketData(self, mode, tokens):
        assert mode == "FULL"
        out = []
        for exch, toks in tokens.items():
            assert len(toks) <= 50
            for t in toks:
                r = self.inst[t]
                if r["instrumenttype"] == "AMXIDX" or r["symbol"].endswith("-EQ"):
                    q = {"ltp": SPOT, "percentChange": 0.42}
                elif r["instrumenttype"] == "FUTIDX":
                    q = {"ltp": SPOT + 65, self.oi_field: 1.2e7}
                else:
                    k = float(r["strike"]) / 100
                    kind = r["symbol"][-2:]
                    peak = 24500 if kind == "CE" else 23500
                    oi = 5e6 - abs(k - peak) * 3000
                    intrinsic = max(0, SPOT - k) if kind == "CE" else max(0, k - SPOT)
                    q = {"ltp": round(intrinsic + 80, 2), self.oi_field: max(oi, 1e5), "tradeVolume": 1000}
                q.update({"symbolToken": t, "tradingSymbol": r["symbol"], "exchange": exch})
                out.append(q)
        return {"status": True, "data": {"fetched": out, "unfetched": []}}

    def optionGreek(self, params):
        assert set(params) == {"name", "expirydate"} and params["expirydate"] == NEAR
        rows = []
        for k in STRIKES:
            for t in ("CE", "PE"):
                rows.append({"name": "NIFTY", "expiry": NEAR, "strikePrice": f"{k}.000000", "optionType": t,
                             "delta": "0.5" if t == "CE" else "-0.5", "gamma": "0.001", "theta": "-12.5",
                             "vega": "10", "impliedVolatility": "13.4", "tradeVolume": "1000"})
        return {"status": True, "data": rows}

    def getCandleData(self, params):
        assert params["interval"] == "ONE_DAY"
        rng = np.random.default_rng(5)
        days = pd.bdate_range(end=TODAY - timedelta(days=1), periods=320)
        close = 22000 * np.exp(np.cumsum(rng.normal(0.0006, 0.009, len(days))))
        rows = []
        for d, c in zip(days, close):
            rows.append([f"{d:%Y-%m-%d}T00:00:00+05:30", c * 0.997, c * 1.006, c * 0.992, c, 1e6])
        return {"status": True, "data": rows}


def fake_session(oi_field="opnInterest"):
    a = object.__new__(angel.Angel)
    raw = fake_instruments()
    a.api = FakeApi(raw, oi_field)
    a._inst = angel.load_instruments(raw)
    a.last_keys = {}
    return a


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(angel.time, "sleep", lambda s: None)


def test_instrument_parsing():
    inst = angel.load_instruments(fake_instruments())
    row = inst[inst["symbol"] == f"NIFTY{NEAR}24000CE"].iloc[0]
    assert row["strike_rs"] == 24000 and row["lot"] == 75 and row["exp"].strftime("%d%b%Y").upper() == NEAR


def test_underlying_lookup():
    inst = angel.load_instruments(fake_instruments())
    assert angel.find_underlying(inst, "nifty").spot_token == "99926000"
    assert angel.find_underlying(inst, "RELIANCE").kind == "stock"
    with pytest.raises(angel.AngelError):
        angel.find_underlying(inst, "NOTASTOCK")


def test_nearest_expiry_skips_expired_contracts():
    futs, chain, expiry = angel.derivatives(angel.load_instruments(fake_instruments()), "NIFTY")
    assert expiry.strftime("%d%b%Y").upper() == NEAR
    assert set(chain["type"]) == {"CE", "PE"} and len(futs) == 1


@pytest.mark.parametrize("oi_field", ["opnInterest", "openInterest"])
def test_snapshot_numbers(oi_field):
    s = angel.build_snapshot(fake_session(oi_field), "NIFTY", strikes_each_side=5)
    assert s.spot == SPOT and s.lot == 75 and s.atm == 24000
    assert len(s.legs) == 22  # 11 strikes x CE/PE
    assert s.max_oi("CE").strike == 24500 and s.max_oi("PE").strike == 23500
    assert s.fut_ltp == SPOT + 65 and s.basis == 65
    assert s.pcr is not None and 0.3 < s.pcr < 3
    assert 23500 <= s.max_pain <= 24500
    atm_ce = next(l for l in s.legs if l.kind == "CE" and l.strike == 24000)
    assert atm_ce.iv == 13.4 and atm_ce.delta == 0.5


def test_max_pain_known_case():
    s = angel.FnoSnapshot("X", "index", 100, 0, TODAY, 1)
    s.legs = [angel.Leg(90, "PE", "a", oi=100), angel.Leg(110, "CE", "b", oi=100), angel.Leg(100, "CE", "c", oi=1),
              angel.Leg(100, "PE", "d", oi=1)]
    assert s.max_pain == 100 and s.pcr == pytest.approx(101 / 101)


def test_fno_text_and_chart():
    s = angel.build_snapshot(fake_session(), "NIFTY", 5)
    text = fno.summary_text(s)
    assert "24,500" in text and "23,500" in text and "PCR (OI)" in text and "<pre>" in text
    assert "IV 13.4%" in text and "lot" in text
    png = fno.oi_chart(s)
    assert png[:4] == b"\x89PNG" and len(png) > 15000


def test_fno_command_end_to_end(monkeypatch):
    sent = []
    monkeypatch.setattr(telegram, "send_text", lambda t, chat_id=None, buttons=None: sent.append(("text", t)))
    monkeypatch.setattr(telegram, "send_photo",
                        lambda png, caption="", chat_id=None, buttons=None: sent.append(("photo", caption)))
    monkeypatch.setattr(angel, "configured", lambda: True)
    sess = fake_session()
    monkeypatch.setattr(angel, "session", lambda: sess)
    main.handle("/fno nifty")
    assert [k for k, _ in sent] == ["photo", "text"]
    assert "OPTION TRADE IDEA" in sent[1][1] and "F&amp;O REPORT" in sent[1][1]
    assert all(len(c) <= 4096 for c in telegram.split_text(sent[1][1]))


def test_index_analysis_uses_angel_live_data(monkeypatch):
    monkeypatch.setattr(angel, "configured", lambda: True)
    sess = fake_session()
    monkeypatch.setattr(angel, "session", lambda: sess)
    a = report.analyze_any("NIFTY")
    assert a.market == "stock" and a.source == "Angel One (live)" and a.live_price == SPOT
    text = report.full_text(a)
    assert "Angel One (live)" in report.card(a) and "📦 Size" not in text
    if a.best():
        assert "trade it via F&amp;O" in report.card(a)


def test_fno_without_angel_explains(monkeypatch):
    sent = []
    monkeypatch.setattr(telegram, "send_text", lambda t, chat_id=None, buttons=None: sent.append(t))
    monkeypatch.setattr(angel, "configured", lambda: False)
    main.handle("/fno NIFTY")
    assert "Angel One SmartAPI secrets" in sent[0]


def test_bad_totp_secret_message(monkeypatch):
    for k, v in {"ANGEL_API_KEY": "k", "ANGEL_CLIENT_CODE": "c", "ANGEL_PIN": "1", "ANGEL_TOTP_SECRET": "!!bad!!"}.items():
        monkeypatch.setenv(k, v)

    class NoNetwork:
        def __init__(self, api_key):
            pass

    import SmartApi
    monkeypatch.setattr(SmartApi, "SmartConnect", NoNetwork)
    with pytest.raises(angel.AngelError, match="TOTP"):
        angel.Angel()

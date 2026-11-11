"""Tests for the November 2026 upgrade: 14 strategies, timeframes, live price, VIX, forex, Greeks,
F&O planner, fundamentals detail, news tone, groups, paper trading, storage, pairs. All offline."""
import json
import math
import time
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from bot import (angel, backtest, data, fno_plan, fx, greeks, main, news, paper, pairs, report, state, store,
                 strategies, telegram, vol)
from tests.test_bot import candles, nse_intraday
from tests.test_fno import SPOT, fake_session


@pytest.fixture
def ui(monkeypatch, tmp_path):
    log = []
    monkeypatch.setattr(telegram, "send_text", lambda t, chat_id=None, buttons=None: log.append(("text", t, buttons)))
    monkeypatch.setattr(telegram, "send_photo",
                        lambda png, caption="", chat_id=None, buttons=None: log.append(("photo", caption, buttons)))
    monkeypatch.setattr(telegram, "edit_text",
                        lambda t, mid, chat_id=None, buttons=None: log.append(("edit", t, buttons)))
    monkeypatch.setattr(telegram, "set_commands", lambda: None)
    monkeypatch.setattr(telegram, "answer_callback", lambda cid, text="": None)
    monkeypatch.setattr(telegram, "bot_username", lambda: "shivambot")
    monkeypatch.setattr(main.ai, "summarize", lambda facts: None)
    monkeypatch.setenv("BOT_STATE", str(tmp_path / "state.json"))
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "555")
    return log


def flat(buttons):
    return [b for row in (buttons or []) for b in row]


# ---------------------------------------------------------------- strategies: causality + backtest rules

@pytest.mark.parametrize("kind", ["daily", "intraday"])
def test_no_strategy_looks_ahead(kind):
    """A signal on candle i must be the same whether or not later candles exist."""
    if kind == "daily":
        df, kw = candles(420, seed=21, freq="D"), {"tf": "1d"}
    else:
        df, kw = nse_intraday(days=12, seed=9), {"tf": "5m", "tz": "Asia/Kolkata", "venue": "nse"}
    full, _ = strategies.prepare(df, **kw)
    cut = len(df) - 40
    part, _ = strategies.prepare(df.iloc[:cut].copy(), **kw)
    for s in strategies.ALL:
        if s.applies(full):
            continue
        a = s.signal(full).iloc[:cut].to_numpy()
        b = s.signal(part).to_numpy()
        assert (a == b).all(), f"{s.side} {s.name} changed its past signals when more candles arrived"


class ExitRuleStrat(strategies.Strategy):
    name = "exit-rule test"

    def signal(self, x):
        s = pd.Series(False, index=x.index)
        s.iloc[10] = True
        return s

    def stop(self, x, i, entry):
        return entry - 5.0

    def target(self, x, i, entry, stop):
        return None

    def exit_rule(self, x):
        r = pd.Series(False, index=x.index)
        r.iloc[13] = True
        return r


def _flat_path(n=30):
    idx = pd.date_range("2025-01-01", periods=n, freq="1h", tz="UTC")
    p = np.full(n, 100.0) + np.arange(n) * 0.5           # slow rise
    return pd.DataFrame({"open": p, "high": p + 0.2, "low": p - 0.2, "close": p, "volume": 1.0}, index=idx)


def test_backtest_exit_rule_and_time_stop(monkeypatch):
    from bot.config import cfg

    monkeypatch.setitem(cfg()["costs"], "slippage_pct", 0.0)
    monkeypatch.setitem(cfg()["costs"], "crypto_pct_per_side", 0.0)
    df = _flat_path()
    res = backtest.run(ExitRuleStrat(), df, "crypto")
    # enter at open of 11 (105.5); stop = close of 10 (105) - 5 = 100; rule fires on the close of 13,
    # exit at the open of 14 (107.0): +1.5 / 5.5 = +0.27R
    assert res["trades"] == 1 and res["avg_r"] == pytest.approx(1.5 / 5.5, abs=0.01)

    class TimeStop(ExitRuleStrat):
        max_bars = 2

        def exit_rule(self, x):
            return None

    res = backtest.run(TimeStop(), df, "crypto")
    assert res["trades"] == 1 and res["avg_bars"] == 3          # held 2 candles, out at the next open


def test_regime_and_strategy_check_numbering(monkeypatch):
    a = report.analyze("SOLUSDT", "crypto", candles=candles(), change_pct=1.0)
    text = report.strategy_list(a, alert=True)
    assert text.startswith("🔔 <b>NEW SIGNAL</b>") and "Regime:" in text and "of 11 usable here" in text
    for n in range(1, 11):
        assert report.KEYCAP[n - 1] in text
    assert "<b>11.</b>" in text and "<b>14.</b>" in text and "➖" in text
    assert a.n_applicable == 11                                  # VWAP, ORB, gap fill need intraday NSE


def test_intraday_nse_uses_all_14(monkeypatch):
    df = nse_intraday(days=12, seed=3)
    df.attrs.update(tf="5m", tz="Asia/Kolkata", venue="nse", currency="INR", source="test")
    a = report.analyze("RELIANCE", "stock", candles=df, tf="5m")
    assert a.timeframe == "5m" and a.n_applicable == 14
    st = report.structure_text(a)
    assert "VWAP" in st and "Opening range" in st and "Fibonacci" in st


# ---------------------------------------------------------------- data: timeframes, forming candles, 4h

def test_norm_tf_and_split():
    assert data.norm_tf("15min") == "15m" and data.norm_tf("1D") == "1d" and data.norm_tf("x", "4h") == "4h"
    assert main.split_tf(["ETH", "15m"]) == (["ETH"], "15m")
    assert main.split_tf(["ETH"]) == (["ETH"], None)


def test_drop_forming_candle_and_session_close():
    idx = pd.date_range("2026-11-06 03:45", periods=6, freq="1h", tz="UTC")   # 09:15 IST ... 14:15 IST
    df = pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": range(6), "volume": 1.0}, index=idx)
    during = datetime(2026, 11, 6, 9, 20, tzinfo=timezone.utc)                # 14:50 IST: 14:15 bar still open
    assert len(data.drop_forming(df, "1h", (15, 30), "Asia/Kolkata", during)) == 5
    after = datetime(2026, 11, 6, 10, 5, tzinfo=timezone.utc)                 # 15:35 IST: session closed
    assert len(data.drop_forming(df, "1h", (15, 30), "Asia/Kolkata", after)) == 6


def test_resample_nse_4h():
    idx = pd.date_range("2026-11-06 03:45", periods=7, freq="1h", tz="UTC")   # 09:15..15:15 IST
    df = pd.DataFrame({"open": np.arange(7.0), "high": np.arange(7.0) + 1, "low": np.arange(7.0) - 1,
                       "close": np.arange(7.0) + 0.5, "volume": 1.0}, index=idx)
    out = data.resample_nse_4h(df)
    assert len(out) == 2 and out["volume"].tolist() == [4.0, 3.0]
    assert out["open"].iloc[1] == 4.0 and out["close"].iloc[1] == 6.5
    assert out.index[1].tz_convert("Asia/Kolkata").strftime("%H:%M") == "13:15"


def test_timeframe_button_reanalyses(monkeypatch, ui):
    seen = []
    monkeypatch.setattr(data, "crypto_candles", lambda sym, timeframe=None, limit=None: (seen.append(timeframe),
                                                                                         candles())[1])
    main.handle_callback("a:c:ETHUSDT:15m", "555")
    assert seen == ["15m"] and "ETH/USDT</b> · 15m · ⚡ Scalp" in ui[0][1]
    assert ("✅ 15m", "a:c:ETHUSDT:15m") in flat(ui[1][2])


# ---------------------------------------------------------------- forex

def test_fx_catalogue_and_sizes():
    assert fx.find("xau/usd").code == "XAUUSD" and fx.find("gold").code == "GOLD" and fx.find("wti").code == "USOIL"
    eur = fx.find("EURUSD")
    lots, per_lot = fx.size_lots(eur, 1.1000, 1.0950, 50)        # 50 pips, $50 risk
    assert per_lot == pytest.approx(500) and lots == pytest.approx(0.1)
    gold = fx.find("GOLD")
    lots, per_lot = fx.size_lots(gold, 76000, 75800, 75)
    assert per_lot == pytest.approx(20000)                       # ₹200 x 100 per lot


def test_fx_falls_back_to_yahoo_and_analyses(monkeypatch, ui):
    calls = []

    def yahoo(ticker, tf, label=None):
        calls.append((ticker, tf))
        df = candles(500, freq="1h", start=1.08, vol=0.002)
        df["volume"] = 0.0
        df.attrs["live_price"] = 1.0815
        return df

    monkeypatch.setattr(fx, "yahoo_candles", yahoo)
    monkeypatch.setattr(angel, "configured", lambda: False)
    main.handle("EURUSD 1h", "555")
    assert calls == [("EURUSD=X", "1h")]
    assert "EUR/USD</b> · 1h" in ui[0][1] and "1.08150" in ui[0][1] and "Yahoo" in ui[0][1]
    assert ("📰 Forex & commodity news", "n:c:forex") in flat(ui[1][2])
    ui.clear()
    main.handle_callback("m:fx", "555")
    assert "FOREX &amp; COMMODITIES" in ui[0][1] and ("🥇 XAUUSD", "a:f:XAUUSD") in flat(ui[0][2])


def test_fx_twelve_data_used_when_key_set(monkeypatch):
    monkeypatch.setenv("TWELVE_DATA_API_KEY", "k")

    class R:
        def __init__(self, body):
            self.body = body

        def json(self):
            return self.body

    def get(url, params=None, timeout=None):
        if url.endswith("/price"):
            return R({"price": "2400.5"})
        t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        vals = [{"datetime": (t0 + timedelta(hours=i)).strftime("%Y-%m-%d %H:%M:%S"), "open": "2400", "high": "2401",
                 "low": "2399", "close": "2400.2"} for i in range(400)]
        return R({"status": "ok", "values": vals})

    monkeypatch.setattr(fx.requests, "get", get)
    monkeypatch.setattr(fx.time, "sleep", lambda s: None)
    df = fx.candles("XAUUSD", "1h")
    assert df.attrs["source"] == "Twelve Data (live)" and df.attrs["live_price"] == 2400.5 and len(df) >= 399


# ---------------------------------------------------------------- VIX

def test_vix_scale_text_and_card_line(monkeypatch):
    assert vol.band(10)[0] == "very low" and vol.band(13)[1] == "🟢" and vol.band(20)[1] == "🟠"
    assert vol.band(30)[1] == "🔴"
    h = pd.Series(np.linspace(10, 22, 300), index=pd.date_range("2025-08-01", periods=300, freq="D", tz="UTC"))
    v = vol.Vix(19.5, 4.2, h, "test")
    monkeypatch.setattr(vol, "india_vix", lambda: v)
    text = vol.vix_text(v)
    assert "◀ <b>now</b>" in text and "🟠" in text and "Expected NIFTY move" in text and "Option buyers" in text
    a = report.analyze("RELIANCE", "stock", candles=candles(500, freq="D", start=1400))
    assert "India VIX <b>19.50</b>" in vol.card_line(a) and "🟠" in vol.card_line(a)
    assert vol.chart(v)[:4] == b"\x89PNG"
    crypto = report.analyze("BTCUSDT", "crypto", candles=candles())
    assert vol.card_line(crypto) == ""


def test_card_shows_vix_for_indian_asset(monkeypatch, ui):
    h = pd.Series(np.linspace(11, 14, 300), index=pd.date_range("2025-08-01", periods=300, freq="D", tz="UTC"))
    monkeypatch.setattr(vol, "india_vix", lambda: vol.Vix(12.8, -1.5, h, "test"))
    monkeypatch.setattr(data, "stock_candles", lambda sym, timeframe="1d", **k: candles(500, freq="D", start=1400))
    main.handle("/stock RELIANCE", "555")
    assert "🌡 India VIX <b>12.80</b>" in ui[0][1] and "🟢" in ui[0][1]


# ---------------------------------------------------------------- Greeks + F&O plan

def test_black_scholes_round_trip_and_parity():
    S, K, T, r = 24000, 24200, 7 / 365, 0.065
    c = greeks.bs_price(S, K, T, r, 0.14, "CE")
    p = greeks.bs_price(S, K, T, r, 0.14, "PE")
    assert c - p == pytest.approx(S - K * math.exp(-r * T), abs=1e-6)       # put-call parity
    assert greeks.implied_vol(c, S, K, T, r, "CE") == pytest.approx(0.14, abs=1e-4)
    g = greeks.bs_greeks(S, K, T, r, 0.14, "CE")
    assert 0 < g["delta"] < 0.5 and g["gamma"] > 0 and g["theta"] < 0 and g["vega"] > 0


def test_greeks_fill_missing_and_report(monkeypatch):
    snap = angel.build_snapshot(fake_session(), "NIFTY", 6)
    for l in snap.legs:                      # pretend Angel One returned nothing (outside market hours)
        l.delta = l.gamma = l.theta = l.vega = l.iv = None
        l.greeks_src = ""
    n = greeks.fill(snap)
    assert n == len(snap.legs) and all(l.gamma is not None and l.iv for l in snap.legs)
    ce = {l.strike: l.delta for l in snap.side("CE")}
    assert ce[23500] > ce[24000] > ce[24500]                 # deeper ITM call = higher delta
    text = greeks.report_text(snap)
    assert "OPTION GREEKS" in text and "CALLS (CE)" in text and "PUTS (PE)" in text and "Γ" in text
    assert "computed with Black-Scholes" in text
    assert greeks.chart(snap)[:4] == b"\x89PNG"


def test_condor_payoff_maths():
    snap = angel.build_snapshot(fake_session(), "NIFTY", 10)
    T = greeks.years_to_expiry(snap.expiry)
    for l in snap.legs:                      # the fake API's premiums are flat; use model premiums with a smile
        sig = 0.13 + 0.25 * ((l.strike - SPOT) / SPOT) ** 2 * 100
        l.ltp = round(max(greeks.bs_price(SPOT, l.strike, T, 0.065, sig, l.kind), 0.05), 2)
        l.delta = l.gamma = l.theta = l.vega = l.iv = None
    greeks.fill(snap)
    vv = fno_plan.VolView("high", "test", 14.0)
    p = fno_plan.plan_condor(snap, vv)
    sells = [l for l in p.legs if l.action == "SELL"]
    assert len(p.legs) == 4 and len(sells) == 2 and p.net < 0               # a credit
    width = abs(p.legs[1].strike - p.legs[0].strike)
    assert p.max_profit() == pytest.approx(-p.net * p.lot, rel=1e-6)
    assert p.max_loss() == pytest.approx(-(width + p.net) * p.lot, rel=0.02)
    assert len(p.breakevens()) == 2 and 0 < p.pop() < 100
    text = fno_plan.plan_text(p, snap, True)
    assert "RECOMMENDED PLAN" in text and "Max loss" in text and "EXIT PLAN" in text and "50%" in text
    assert fno_plan.payoff_chart(p, snap)[:4] == b"\x89PNG"


def test_plan_command_and_buttons(monkeypatch, ui):
    sess = fake_session()
    monkeypatch.setattr(angel, "configured", lambda: True)
    monkeypatch.setattr(angel, "session", lambda: sess)
    main.handle("/plan NIFTY", "555")
    text, btns = ui[-1][1], flat(ui[-1][2])
    assert "RECOMMENDED PLAN" in text and ("🛡 Hedging", "fp:NIFTY:hedge") in btns
    ui.clear()
    main.handle_callback("fp:NIFTY:hedge", "555")
    assert "HEDGE" in ui[-1][1] and "protective put" in ui[-1][1]
    ui.clear()
    main.handle_callback("gk:NIFTY", "555")
    assert "OPTION GREEKS" in ui[-1][1]


# ---------------------------------------------------------------- news

def test_news_tone_bold_and_categories():
    assert news.tone_of("Sensex surges 900 points as banks rally") == 1
    assert news.tone_of("Rupee slumps to record low; FIIs pull out") == -1
    assert news.tone_of("Inflation eases to 3.1% in October") == 1
    it = news.tag(news.Item("Gold jumps as rupee weakens; Reliance shares rise", "u", "MC", "india", time.time()))
    assert "forex" in it.cats and "india" in it.cats
    b = news.bold_title(it)
    assert "<b>Gold</b>" in b and "<b>rupee</b>" in b and "<b>Reliance</b>" in b


def test_news_newest_first_and_age_on_top(monkeypatch, ui):
    now = time.time()
    monkeypatch.setattr(news, "fetch_feed", lambda url, source, cat: [
        news.Item("RBI holds repo rate, banks gain", "https://ex.com/1", "MC", cat, now - 5 * 3600),
        news.Item("Bitcoin slumps below $80,000", "https://ex.com/2", "CoinDesk", cat, now - 600),
        news.Item("Celebrity wedding photos", "https://ex.com/3", "BBC", cat, now - 60)] if cat == "india" else [])
    main.handle("/news", "555")
    text = ui[0][1]
    assert "Celebrity" not in text                               # no market angle
    assert text.index("Bitcoin") < text.index("RBI")             # newest first
    assert "⏱ 10m ago" in text and "newest story 10m ago" in text and "🔴" in text and "🟢" in text
    tabs = [d for _, d in flat(ui[0][2]) if d.startswith("n:t:")]
    assert tabs == ["n:t:all", "n:t:india", "n:t:forex", "n:t:crypto", "n:t:other"]
    ui.clear()
    main.handle_callback("n:t:crypto", "555", 7)
    assert "Bitcoin" in ui[0][1] and "RBI holds" not in ui[0][1]


# ---------------------------------------------------------------- storage

def test_store_local_update(tmp_path, monkeypatch):
    monkeypatch.setenv("BOT_STORE", str(tmp_path / "s.json"))
    assert store.update(lambda d: d.setdefault("n", 0)) == 0
    store.update(lambda d: d.__setitem__("n", d["n"] + 1))
    assert store.load()["n"] == 1 and not store.is_gcs()


def test_store_gcs_retries_on_conflict(monkeypatch):
    monkeypatch.setenv("BOT_STORE", "gs://my-bucket/bot/store.json")
    obj = {"gen": 3, "doc": {"groups": []}}
    puts = []

    class R:
        def __init__(self, code, body=None, headers=None):
            self.status_code, self._b, self.headers, self.text = code, body, headers or {}, ""

        def json(self):
            return self._b

    def get(url, headers=None, timeout=None):
        if "metadata" in url:
            return R(200, {"access_token": "tok", "expires_in": 3600})
        assert "my-bucket" in url and "bot%2Fstore.json" in url and headers["Authorization"] == "Bearer tok"
        return R(200, obj["doc"], {"x-goog-generation": str(obj["gen"])})

    def post(url, data=None, headers=None, timeout=None):
        puts.append(url)
        if len(puts) == 1:                    # someone else wrote first
            obj["gen"] += 1
            return R(412)
        assert f"ifGenerationMatch={obj['gen']}" in url
        obj["doc"] = json.loads(data)
        obj["gen"] += 1
        return R(200, {"generation": str(obj["gen"])})

    gets = []
    monkeypatch.setattr(store._HTTP, "get", lambda url, **k: gets.append(url) or get(url, **k))
    monkeypatch.setattr(store._HTTP, "post", post)
    monkeypatch.setattr(store.time, "sleep", lambda s: None)
    store._tok.clear()
    store.forget()
    store.set_group("-100123", True)
    assert len(puts) == 2 and obj["doc"]["groups"] == ["-100123"]
    # the copy in memory answers the next reads, and the next write needs no read at all
    reads = len([u for u in gets if "storage.googleapis" in u])
    assert store.load()["groups"] == ["-100123"] and store.approved_groups() == {"-100123"}
    store.set_group("-100999", True)
    assert len([u for u in gets if "storage.googleapis" in u]) == reads and len(puts) == 3
    assert obj["doc"]["groups"] == ["-100123", "-100999"]
    store.forget()


# ---------------------------------------------------------------- paper trading

def _btn(log, prefix):
    """data of the first button in the last message whose data starts with prefix"""
    for kind, text, buttons in reversed(log):
        for label, d in flat(buttons):
            if d.startswith(prefix):
                return label, d
    raise AssertionError(f"no button {prefix}")


def test_paper_bot_levels_ladder_leverage_confirm_exit(monkeypatch, ui):
    monkeypatch.setattr(data, "crypto_candles", lambda sym, timeframe=None, limit=None: candles())
    prices = {"v": None}
    monkeypatch.setattr(paper, "live_price", lambda m, s: prices["v"])
    monkeypatch.setattr(paper, "candles_since", lambda t: None)
    main.handle_callback("a:c:ETHUSDT:4h", "555")
    a = report.cached("crypto", "ETHUSDT", "4h")
    prices["v"] = a.now_price
    ui.clear()
    main.handle_callback("pt:o:c:ETHUSDT:4h", "555")
    assert "PAPER TRADE · ETH/USDT" in ui[0][1] and "Bot's suggestion" in ui[0][1] and "Risk → size → reward" in ui[0][1]
    assert ("✍️ BUY · my own", "pt:c:b:c:ETHUSDT:4h") in flat(ui[0][2])
    ui.clear()
    main.handle_callback("pt:r:b:c:ETHUSDT:4h", "555")       # bot levels: straight to the size step
    assert "Step 1/2 · Capital or quantity" in ui[-1][1]
    assert ("💼 $100", "pt:w:size:C100") in flat(ui[-1][2])        # capital presets
    label, d = _btn(ui, "pt:w:size:")
    assert label.startswith("🪜 $10 risk →")
    main.handle_callback(d, "555")
    assert "Step 2/2 · Leverage" in ui[-1][1]
    lev5 = [lbl for lbl, cb in flat(ui[-1][2]) if cb == "pt:w:lev:L5"][0]
    assert lev5.startswith("5x → $")                           # qty known: shows the capital it would use
    main.handle_callback("pt:v:L5", "555")
    text = ui[-1][1]
    assert "CONFIRM PAPER BUY" in text and "Market order" in text and "× 5x" in text
    assert "Capital used <b>$" in text and "Liquidation ≈" in text and "loss <b>$" in text and "R:R" in text
    ui.clear()
    main.handle_callback("pt:k", "555")
    assert "Paper BUY opened · #1" in ui[0][1]
    t = paper.open_trades("555")[0]
    assert t["lev"] == 5 and t["liq"] == pytest.approx(t["entry"] * 0.8) and t["stop"] < t["entry"] < t["target"]
    risk_money = (t["entry"] - t["stop"]) * t["qty"]
    assert 5 < risk_money <= 10.0001                          # the $10 rung
    prices["v"] = t["entry"] * 1.02
    ui.clear()
    main.handle("/paper", "555")
    assert "OPEN POSITIONS (1)" in ui[0][1] and "price +2.00%" in ui[0][1] and "on margin +10.0%" in ui[0][1]
    assert ("❌ Exit #1 ETHUSDT", "pt:x:1") in flat(ui[0][2]) and ("✏️ SL/TP #1", "pt:e:1") in flat(ui[0][2])
    ui.clear()
    main.handle_callback("pt:x:1", "555")
    assert "closed" in ui[0][1] and "+2.00%" in ui[0][1] and "on margin +10.0%" in ui[0][1]
    ui.clear()
    main.handle("/history", "555")
    assert "win rate 100%" in ui[0][1]


def test_paper_custom_ticket_typed_values_and_pending_limit(monkeypatch, ui):
    monkeypatch.setattr(data, "crypto_candles", lambda sym, timeframe=None, limit=None: candles())
    monkeypatch.setattr(paper, "live_price", lambda m, s: 100.0)
    monkeypatch.setattr(paper, "candles_since", lambda t: None)
    main.handle_callback("a:c:SOLUSDT:4h", "555")
    main.handle_callback("pt:c:s:c:SOLUSDT:4h", "555")       # my own SELL
    assert "Step 1/5 · Entry" in ui[-1][1] and ("⚡ Market 100.00", "pt:w:entry:mkt") in flat(ui[-1][2])
    main.handle("105", "555")                                 # above live -> sell LIMIT
    assert "Step 2/5 · Capital or quantity" in ui[-1][1]
    main.handle("2.5", "555")                                 # quantity typed
    assert "Step 3/5 · Leverage" in ui[-1][1]
    main.handle_callback("pt:v:L1", "555")
    assert "Step 4/5 · Stop-loss" in ui[-1][1] and ("−$25 → 115.00", "pt:w:sl:M25") in flat(ui[-1][2])
    main.handle("100", "555")                                 # wrong side for a SELL
    assert any("must be above" in m[1] for m in ui[-3:])
    main.handle("110", "555")
    assert "Step 5/5 · Take profit" in ui[-1][1] and ("1:2 → 95.00", "pt:w:tp:95") in flat(ui[-1][2])
    main.handle_callback("pt:w:tp:95", "555")
    text = ui[-1][1]
    assert "SELL LIMIT at 105" in text and "loss <b>$12.50</b>" in text and "R:R <b>1:2.00</b>" in text
    assert "Quantity <b>2.5" in text and "Capital used <b>$262.50</b>" in text
    assert "above your $100" not in text
    main.handle_callback("pt:k", "555")
    assert "placed (limit order" in ui[-1][1]
    o = paper.pending_orders("555")[0]
    assert o["status"] == "pending" and o["order"] == "limit"
    idx = pd.date_range("2026-01-01", periods=3, freq="5min", tz="UTC")
    df = pd.DataFrame({"open": [101, 104, 99], "high": [103, 106, 100], "low": [100, 103, 94], "close": [102, 104, 95]},
                      index=idx)
    ch = paper.advance(o, df)                                  # fills at 105, then hits the 95 target
    assert ch["status"] == "closed" and ch["reason"] == "target hit" and ch["exit"] == 95 and ch["entry"] == 105
    ui.clear()
    main.handle_callback(f"pt:n:{o['id']}", "555")
    assert "cancelled" in ui[0][1] and not paper.pending_orders("555")


def test_paper_capital_entry_with_leverage_gives_quantity(monkeypatch, ui):
    monkeypatch.setattr(data, "crypto_candles", lambda sym, timeframe=None, limit=None: candles())
    monkeypatch.setattr(paper, "live_price", lambda m, s: 100.0)
    monkeypatch.setattr(paper, "candles_since", lambda t: None)
    main.handle_callback("a:c:SOLUSDT:4h", "555")
    main.handle_callback("pt:c:b:c:SOLUSDT:4h", "555")       # my own BUY
    main.handle_callback("pt:v:mkt", "555")
    main.handle("$200", "555")                                # capital, not quantity
    assert "Step 3/5 · Leverage" in ui[-1][1] and "capital $200" in ui[-1][1]
    lev10 = [lbl for lbl, cb in flat(ui[-1][2]) if cb == "pt:w:lev:L10"][0]
    assert lev10 == "10x → 20 SOL"                            # $200 × 10 / $100
    main.handle_callback("pt:w:lev:L10", "555")
    assert "📦 20 SOL (capital $200)" in ui[-1][1]
    main.handle("$50", "555")                                 # stop-loss as money: $50 / 20 = 2.5 below
    assert "Step 5/5 · Take profit" in ui[-1][1] and "🛑 97.50" in ui[-1][1]
    main.handle("150 usd", "555")                             # profit $150 -> 107.5
    text = ui[-1][1]
    assert "Capital entered <b>$200</b> × 10x → <b>BUY 20 SOL</b>" in text
    assert "Capital used <b>$200</b> · position value $2,000" in text
    assert "loss <b>$50</b>" in text and "profit <b>$150</b>" in text and "1:3.00" in text
    main.handle_callback("pt:g:size", "555")                  # ✏️ size from the confirm screen
    main.handle("5", "555")                                   # now a plain quantity
    text = ui[-1][1]
    assert "CONFIRM PAPER BUY" in text and "Quantity <b>5 SOL</b> × 10x" in text and "Capital used <b>$50</b>" in text
    main.handle_callback("pt:g:entry", "555")                 # new entry above the old target: TP asked again
    main.handle("108", "555")
    assert "Take profit" in ui[-1][1]


def _tap(uid, data_str, mid=900, user=555, chat=555, name="Me"):
    return {"update_id": uid, "callback_query": {"id": str(uid), "data": data_str, "from": {"id": user, "first_name": name},
                                                 "message": {"message_id": mid, "chat": {"id": chat}}}}


def test_paper_ticket_updates_in_place_and_ignores_late_or_double_taps(monkeypatch, ui):
    monkeypatch.setattr(data, "crypto_candles", lambda sym, timeframe=None, limit=None: candles())
    monkeypatch.setattr(paper, "live_price", lambda m, s: 100.0)
    monkeypatch.setattr(paper, "candles_since", lambda t: None)
    monkeypatch.setattr(main.time, "time", lambda: clock["t"])
    clock = {"t": 1_800_000_000.0}
    main._TAPS.clear()
    main.handle_callback("a:c:SOLUSDT:4h", "555")
    ui.clear()
    main.process_update(_tap(1, "pt:c:b:c:SOLUSDT:4h"))        # ✍️ BUY · my own, tapped on message 900
    assert [m[0] for m in ui] == ["edit"] and "Step 1/5" in ui[-1][1]     # the same message becomes step 1
    main.process_update(_tap(2, "pt:w:entry:mkt"))
    assert ui[-1][0] == "edit" and "Step 2/5" in ui[-1][1]
    n = len(ui)
    main.process_update(_tap(3, "pt:w:entry:mkt"))             # the same button again right away (double tap)
    assert len(ui) == n
    clock["t"] += 60
    main.process_update(_tap(4, "pt:w:entry:mkt"))             # an old button of a finished step, much later
    assert len(ui) == n and state.draft("555")["step"] == "size"
    main.process_update(_tap(5, "pt:w:size:C200"))
    main.process_update(_tap(6, "pt:w:lev:L10"))
    assert "Step 4/5 · Stop-loss" in ui[-1][1] and state.draft("555")["qty"] == 20
    n = len(ui)
    main.handle("90", "555")                                   # typed stop: one new message with the next step
    assert len(ui) == n + 1 and ui[-1][0] == "text" and "Step 5/5" in ui[-1][1]
    main.handle("80", "555")                                   # wrong side: the warning and the step in ONE message
    assert len(ui) == n + 2 and ui[-1][1].startswith("⚠️ For a BUY the target must be above") and "Step 5/5" in ui[-1][1]
    main.handle("120", "555")
    assert "CONFIRM PAPER BUY" in ui[-1][1]
    clock["t"] += 60
    main.process_update(_tap(7, "pt:k", mid=901))
    assert ui[-1][0] == "edit" and "Paper BUY opened · #1" in ui[-1][1]
    n = len(ui)
    main.process_update(_tap(8, "pt:k", mid=901))              # Confirm tapped twice: one trade, no second message
    assert len(ui) == n and len(paper.open_trades("555")) == 1


def test_group_chat_is_not_read_as_ticket_input(monkeypatch, ui):
    monkeypatch.setattr(data, "crypto_candles", lambda sym, timeframe=None, limit=None: candles())
    monkeypatch.setattr(paper, "live_price", lambda m, s: 100.0)
    store.set_group("-100777", True)
    main._GROUPS.clear()
    main._TAPS.clear()

    def say(uid, text, reply_to_bot=False):
        m = {"message_id": uid, "chat": {"id": -100777, "type": "supergroup"}, "from": {"id": 42, "first_name": "Aman"},
             "text": text}
        if reply_to_bot:
            m["reply_to_message"] = {"from": {"is_bot": True, "username": "shivambot"}}
        main.process_update({"update_id": uid, "message": m})

    main.process_update(_tap(1, "a:c:SOLUSDT:4h", user=42, chat=-100777, name="Aman"))
    main.process_update(_tap(2, "pt:c:b:c:SOLUSDT:4h", user=42, chat=-100777, name="Aman"))
    main.process_update(_tap(3, "pt:w:entry:mkt", user=42, chat=-100777, name="Aman"))
    assert "Step 2/5" in ui[-1][1]
    n = len(ui)
    say(10, "Han toh swing mai hi bn rha hai")                 # ordinary chat between members
    say(11, "ok bhai 2 min")
    assert len(ui) == n and state.draft("-100777:42")["step"] == "size"   # nothing sent, ticket untouched
    say(12, "$200")                                            # a real ticket value
    assert len(ui) == n + 1 and "Step 3/5 · Leverage" in ui[-1][1]
    # in a private chat, other text during a ticket is handled as a normal message, not as a wrong price
    main.handle_callback("pt:c:b:c:SOLUSDT:4h", "555")
    main.handle("yeh kya ho raha hai aaj market mein", "555")
    assert "Please send a price" not in ui[-1][1] and state.draft("555")["step"] == "entry"
    main.handle("101", "555")                                  # the ticket is still waiting for its number
    assert state.draft("555")["entry"] == 101 and "Step 2/5" in ui[-1][1]
    assert paper.looks_like_input("74,250.5") and paper.looks_like_input("market") and paper.looks_like_input("₹5000")
    assert not paper.looks_like_input("sale tune 4 ghante ka chart bheja tha")


def test_same_button_twice_gives_one_chart(monkeypatch, ui):
    monkeypatch.setattr(data, "crypto_candles", lambda sym, timeframe=None, limit=None: candles())
    main._TAPS.clear()
    main.process_update(_tap(1, "a:c:BTCUSDT:30m"))
    main.process_update(_tap(2, "a:c:BTCUSDT:30m"))            # tapped again while the first was still loading
    assert [m[0] for m in ui].count("photo") == 1


def test_paper_check_fetches_only_what_it_needs(monkeypatch):
    asked = []

    def fake(sym, timeframe=None, limit=None):
        asked.append(limit)
        df = candles(40, freq="5min", start=100, vol=0.1)
        df.index = pd.date_range(end=pd.Timestamp.now(tz="UTC").floor("5min"), periods=len(df), freq="5min")
        df.attrs["live_price"] = 101.5
        return df

    monkeypatch.setattr(data, "crypto_candles", fake)
    paper._CANDLES.clear()
    paper._PRICE.clear()
    t = {"market": "crypto", "symbol": "BTCUSDT", "created": time.time() - 1800}
    df = paper.candles_since(t)
    assert asked == [12] and 4 <= len(df) <= 6                 # a 30-minute-old trade: 12 candles, not 1000
    paper.candles_since({**t, "created": time.time() - 600})
    assert asked == [12]                                       # reused for a minute
    assert paper.live_price("crypto", "BTCUSDT") == 101.5      # and the live price came with them
    paper.candles_since({**t, "created": time.time() - 86400})
    assert asked == [12, 291]                                  # an older trade needs more
    paper._CANDLES.clear()
    paper._PRICE.clear()


def test_paper_capital_too_small_and_parsing():
    assert paper.parse_number("$1,250.5") == 1250.5 and paper.parse_number("1:100") == 100
    assert paper.parse_number("5x") == 5 and paper.parse_number("-₹500") == 500 and paper.parse_number("25 usdt") == 25
    assert paper.is_money("₹500") and paper.is_money("50 usd") and not paper.is_money("0.05")
    d = {"side": "BUY", "entry": 22000.0, "live": 22000.0, "market_order": True, "qty": None, "cap": None,
         "mult": 75, "qstep": 1, "cur": "INR", "kind": "lots", "unit": "lot", "lev": None, "lev_choices": [],
         "margin_pct": 0.12, "recommended": False, "step": "size", "rec": {}}
    err = paper.apply_value(d, "₹5000")                       # 1 lot needs ≈ ₹1.98 lakh margin
    assert err and "too small" in err and d["step"] == "size"
    assert paper.apply_value(d, "₹400000") is None and d["qty"] == 2   # 4L ÷ 12% ÷ (22000×75) = 2.02 → 2 lots


def test_paper_edit_sl_tp_and_liquidation_check():
    t = {"side": "BUY", "entry": 100.0, "stop": 70.0, "target": 120.0, "qty": 1, "status": "open"}
    assert paper.liquidation("BUY", 100, 10, "crypto") == pytest.approx(90)
    idx = pd.date_range("2026-01-01", periods=2, freq="5min", tz="UTC")
    df = pd.DataFrame({"open": [100, 95], "high": [101, 96], "low": [99, 89], "close": [100, 90]}, index=idx)
    ch = paper.advance({**t, "liq": 90.0}, df)
    assert ch["reason"] == "liquidated" and ch["exit"] == 90     # liquidation comes before the 70 stop
    d = {"side": "BUY", "entry": 100, "live": 100, "sl": 80, "tp": 130, "qty": 1, "mult": 1, "qstep": 0, "cur": "USD",
         "kind": "coins", "unit": "SOL", "lev": 10, "market": "crypto", "pretty": "SOL/USDT", "tf": "4h",
         "market_order": True, "rec": {"src": "x"}, "margin_pct": None}
    assert "BEFORE your stop" in paper.confirm_text(d)


def test_paper_edit_flow(monkeypatch, ui):
    t = {"id": 7, "user_id": "555", "user_name": "me", "chat_id": "555", "market": "crypto", "symbol": "BTCUSDT",
         "pretty": "BTC/USDT", "tf": "4h", "side": "BUY", "entry": 100.0, "stop": 90.0, "target": 120.0, "qty": 1.0,
         "mult": 1, "unit": "BTC", "currency": "USD", "created": time.time(), "opened": time.time(), "status": "open"}
    store.update(lambda d: d.setdefault("paper", {}).setdefault("trades", []).append(t))
    main.handle_callback("pt:e:7", "555")
    assert "new <b>stop-loss</b>" in ui[-1][1]
    main.handle_callback("pt:w:esl:be", "555")
    main.handle("130", "555")
    assert "#7 updated" in ui[-1][1]
    got = paper.find(7, "555")
    assert got["stop"] == 100 and got["target"] == 130


def test_risk_ladder_for_forex_and_mcx(monkeypatch):
    fx_df = candles(500, freq="1h", start=1.08, vol=0.002)
    fx_df["volume"] = 0.0
    fx_df.attrs.update(tf="1h", venue="fx", currency="USD")
    a = report.analyze("EURUSD", "fx", candles=fx_df)
    lines = "\n".join(report_lines := __import__("bot.risk", fromlist=["x"]).ladder_lines(a, 1.1000, 1.0950, 1.1100))
    assert "$10 → <b>0.02 lot</b>" in lines and "$100 → <b>0.20 lot</b>" in lines and "+$200" in lines


def test_angel_rate_limit_retries_then_fails_cleanly(monkeypatch):
    from tests.test_fno import fake_session

    sess = fake_session()
    calls = {"n": 0}

    class DataException(Exception):
        pass

    real = sess.api.getCandleData

    def flaky(params):
        calls["n"] += 1
        if calls["n"] < 3:
            raise DataException("Couldn't parse the JSON response received from the server: b'Access denied because "
                                "of exceeding access rate'")
        return real(params)

    sess.api.getCandleData = flaky
    df = sess.candles("99926000", "NSE", "ONE_DAY", 700)
    assert calls["n"] == 3 and len(df) > 100
    sess._cache.clear()
    sess.api.getCandleData = lambda p: (_ for _ in ()).throw(DataException("Couldn't parse the JSON response"))
    with pytest.raises(angel.AngelError, match="busy"):
        sess.candles("99926000", "NSE", "ONE_DAY", 700)


def test_paper_stop_hit_while_away():
    t = {"side": "BUY", "entry": 100.0, "stop": 95.0, "target": 110.0, "opened": 0}
    idx = pd.date_range("2026-01-01", periods=4, freq="5min", tz="UTC")
    df = pd.DataFrame({"open": [100, 101, 99, 96], "high": [101, 102, 100, 112], "low": [99, 100, 94.5, 95],
                       "close": [101, 101, 96, 111], "volume": 1}, index=idx)
    reason, price, _ = paper.first_touch(t, df)
    assert reason == "stop-loss hit" and price == 95.0
    short = dict(t, side="SELL", stop=113.0, target=90.0)
    assert paper.first_touch(short, df) is None


def test_paper_only_owner_can_exit(monkeypatch):
    t = {"id": 1, "user_id": "11", "user_name": "Asha", "chat_id": "-100", "market": "crypto", "symbol": "BTCUSDT",
         "pretty": "BTC/USDT", "tf": "4h", "side": "BUY", "entry": 100.0, "stop": 90.0, "target": 120.0, "qty": 1.0,
         "mult": 1, "unit": "BTC", "currency": "USDT", "opened": time.time(), "strategy": "x", "status": "open"}
    store.update(lambda d: d.setdefault("paper", {}).setdefault("trades", []).append(t))
    monkeypatch.setattr(paper, "live_price", lambda m, s: 105.0)
    with pytest.raises(paper.PaperError, match="belongs to Asha"):
        paper.exit_trade(1, "22", "-100", "555")
    assert paper.exit_trade(1, "11", "-100", "555")["pnl_pct"] == pytest.approx(5.0)
    assert "Asha" in paper.board_text("-100") and "🥇" in paper.board_text("-100")


# ---------------------------------------------------------------- groups

def _group_msg(text, uid=11, chat=-100777, reply_bot=False, mid=50):
    msg = {"message_id": mid, "text": text, "chat": {"id": chat, "type": "supergroup"},
           "from": {"id": uid, "first_name": "Asha"}}
    if reply_bot:
        msg["reply_to_message"] = {"from": {"is_bot": True, "username": "shivambot"}}
    return {"update_id": mid, "message": msg}


def test_group_needs_owner_allow_then_answers_members(monkeypatch, ui):
    main.process_update(_group_msg("/help"))
    assert ui == []                                              # unknown group: silent
    main.process_update(_group_msg("/allow", uid=11))
    assert ui == [] and "-100777" not in store.approved_groups()  # only the owner can approve
    main.process_update(_group_msg("/allow", uid=555))
    assert "can now use" in ui[0][1] and "-100777" in store.approved_groups()
    ui.clear()
    main.process_update(_group_msg("/help@shivambot", uid=11))
    assert "HELP" in ui[0][1]
    ui.clear()
    main.process_update(_group_msg("nice weather today", uid=11))
    main.process_update(_group_msg("/start@otherbot", uid=11))
    assert ui == []                                              # ordinary chat / other bots: ignored
    main.process_update(_group_msg("@shivambot /help", uid=11))
    assert ui and "HELP" in ui[0][1]


def test_group_reply_quotes_and_names_the_user(monkeypatch):
    calls = []
    monkeypatch.setattr(telegram, "_call", lambda method, payload=None, files=None: calls.append((method, payload)) or {})
    monkeypatch.setattr(telegram, "bot_username", lambda: "shivambot")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "555")
    monkeypatch.setenv("ALLOWED_CHATS", "-100777")
    main.process_update(_group_msg("/help", uid=11, mid=99))
    sends = [p for m, p in calls if m == "sendMessage"]
    assert sends and sends[0]["chat_id"] == "-100777"
    assert json.loads(sends[0]["reply_parameters"])["message_id"] == 99
    assert sends[0]["text"].startswith('👤 <a href="tg://user?id=11">Asha</a>')


def test_group_members_have_separate_lists(monkeypatch, ui):
    monkeypatch.setenv("ALLOWED_CHATS", "-100777")
    monkeypatch.setattr(data, "top_crypto_by_volume", lambda n: [{"symbol": "BTCUSDT", "change_pct": 1.0}])
    monkeypatch.setattr(data, "crypto_candles", lambda sym, timeframe=None, limit=None: candles())
    main.process_update(_group_msg("/crypto", uid=11))
    ui.clear()
    main.process_update(_group_msg("1", uid=22, mid=51))          # a number from someone with no list: ignored
    assert ui == []
    main.process_update(_group_msg("1", uid=11, mid=52))          # the person who asked: opens item 1
    assert ui and ui[0][0] == "photo" and "BTC/USDT" in ui[0][1]


# ---------------------------------------------------------------- pairs

def test_pairs_detects_cointegration():
    rng = np.random.default_rng(1)
    idx = pd.date_range("2025-01-01", periods=300, freq="B", tz="UTC")
    b = 100 * np.exp(np.cumsum(rng.normal(0, 0.012, 300)))
    noise = np.zeros(300)
    for i in range(1, 300):                      # mean-reverting spread
        noise[i] = 0.8 * noise[i - 1] + rng.normal(0, 0.01)
    noise[-1] = 0.045                            # stretched today -> z between 2 and 3.5
    a = 50 * b ** 1.0 * np.exp(noise)
    r = pairs.analyze("AAA", "BBB", (pd.Series(a, idx), pd.Series(b, idx)))
    assert r.coint_level in (1, 5) and r.beta == pytest.approx(1.0, abs=0.1)
    assert r.signal == "SHORT_A" and "SELL AAA" in pairs.report_text(r)
    walk = 100 * np.exp(np.cumsum(rng.normal(0, 0.012, 300)))
    r2 = pairs.analyze("AAA", "CCC", (pd.Series(walk, idx), pd.Series(b, idx)))
    assert r2.coint_level is None and r2.signal == "NO_COINT"
    assert pairs.chart(r)[:4] == b"\x89PNG"


# ---------------------------------------------------------------- live price + forming candle

def test_live_price_on_card_and_chart(monkeypatch, ui):
    df = candles()
    df.attrs["live_price"] = float(df["close"].iloc[-1]) * 1.01
    monkeypatch.setattr(data, "crypto_candles", lambda sym, timeframe=None, limit=None: df)
    main.handle("/analyze SOL", "555")
    assert f"Live <b>{strategies._fmt(df.attrs['live_price'])}</b>" in ui[0][1]


# ---------------------------------------------------------------- safety

def test_no_order_placement_anywhere():
    """The bot is read-only: no broker/exchange order call may ever appear in the code."""
    from bot.config import ROOT

    forbidden = ["placeOrder", "modifyOrder", "cancelOrder", "order_send", "order_check", "gttCreateRule",
                 "/api/v3/order", "create_order", "convertPosition"]
    for f in (ROOT / "bot").glob("*.py"):
        text = f.read_text(encoding="utf-8")
        for word in forbidden:
            assert word not in text, f"{word} found in {f.name}"


def test_settings_and_strategies_screens(ui):
    main.handle("/settings", "555")
    assert "Paper trades saved in" in ui[0][1] and "Twelve Data" in ui[0][1]
    ui.clear()
    main.handle("/strategies", "555")
    text = ui[0][1]
    assert "THE 14 STRATEGIES" in text and "iron condor" in text and "Fibonacci Golden Zone" in text


def test_nse_falls_back_to_yahoo_when_angel_is_busy(monkeypatch):
    def busy():
        raise angel.AngelError("candles: Angel One is busy right now (rate limit). Try again in a minute.")
    monkeypatch.setattr(angel, "configured", lambda: True)
    monkeypatch.setattr(angel, "session", busy)
    df = candles(300, freq="1D", start=2500, vol=10)
    monkeypatch.setattr(data, "yahoo_candles", lambda ticker, tf, label=None: df.copy())
    got = data.stock_candles("RELIANCE", "1d")
    assert got.attrs["source"] == "Yahoo (delayed ~15 min — Angel One busy)" and got.attrs["currency"] == "INR"
    monkeypatch.setattr(data, "yahoo_candles", lambda *a, **k: (_ for _ in ()).throw(data.DataError("no data")))
    with pytest.raises(data.DataError, match="Angel One RELIANCE: candles: Angel One is busy"):
        data.stock_candles("RELIANCE", "1d")


def test_edited_message_in_a_group_still_names_the_member(monkeypatch):
    calls = []
    monkeypatch.setattr(telegram, "_call", lambda method, payload=None, files=None: calls.append((method, payload)) or {})
    token = telegram.set_ctx(telegram.Ctx("-100777", "42", "Aman", True, None, None))
    try:
        telegram.show("Step 2/5", None, [[("x", "pt:q")]], 900)
    finally:
        telegram.reset_ctx(token)
    method, payload = calls[0]
    assert method == "editMessageText" and payload["message_id"] == 900
    assert payload["text"].startswith("👤 <a href=\"tg://user?id=42\">Aman</a>\nStep 2/5")


def test_cloud_run_is_one_server_with_threads():
    """The bot's memory (open tickets, lists, analyses) lives in one process: never more than one server."""
    from bot.config import ROOT

    script = (ROOT / "deploy_cloudrun.sh").read_text()
    assert "--max-instances 1 " in script and "--max-instances 3" not in script and "--concurrency 8" in script
    docker = (ROOT / "Dockerfile").read_text()
    assert "--workers 1 --threads 8" in docker


def test_telegram_keeps_one_connection_and_retries_a_stale_one(monkeypatch):
    import requests

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:x")
    calls = []

    class R:
        def json(self):
            return {"ok": True, "result": {"message_id": 1}}

    def post(url, data=None, files=None, timeout=None):
        calls.append(url.rsplit("/", 1)[1])
        if len(calls) == 1:
            raise requests.ConnectionError("the kept-open connection was closed by the server")
        return R()

    monkeypatch.setattr(telegram._HTTP, "post", post)
    assert telegram._call("sendMessage", {"chat_id": "1", "text": "hi"}) == {"message_id": 1}
    assert calls == ["sendMessage", "sendMessage"]                 # sent again once on the same session
    calls.clear()
    with pytest.raises(telegram.TelegramError):                    # a photo upload is not replayed
        telegram._call("sendPhoto", {"chat_id": "1"}, files={"photo": ("c.png", b"x", "image/png")})
    assert calls == ["sendPhoto"]

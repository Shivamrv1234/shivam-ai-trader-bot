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
    monkeypatch.setattr(data, "stock_candles", lambda sym, timeframe="1d": candles(500, freq="D", start=1400))
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
        return R(200, {})

    monkeypatch.setattr(store.requests, "get", get)
    monkeypatch.setattr(store.requests, "post", post)
    monkeypatch.setattr(store.time, "sleep", lambda s: None)
    store._tok.clear()
    store.set_group("-100123", True)
    assert len(puts) == 2 and obj["doc"]["groups"] == ["-100123"]


# ---------------------------------------------------------------- paper trading

def test_paper_trade_open_status_exit(monkeypatch, ui):
    monkeypatch.setattr(data, "crypto_candles", lambda sym, timeframe=None, limit=None: candles())
    prices = {"v": None}
    monkeypatch.setattr(paper, "live_price", lambda m, s: prices["v"])
    monkeypatch.setattr(paper, "candles_since", lambda t: None)
    main.handle_callback("a:c:ETHUSDT:4h", "555")
    a = report.cached("crypto", "ETHUSDT", "4h")
    ui.clear()
    main.handle_callback("pt:o:c:ETHUSDT:4h", "555")
    assert "PAPER TRADE · ETH/USDT" in ui[0][1] and ("🟢 Paper BUY", "pt:b:c:ETHUSDT:4h") in flat(ui[0][2])
    ui.clear()
    main.handle_callback("pt:b:c:ETHUSDT:4h", "555")
    assert "Paper BUY opened · #1" in ui[0][1]
    t = paper.open_trades("555")[0]
    assert t["entry"] == pytest.approx(a.now_price) and t["stop"] < t["entry"] < t["target"]
    prices["v"] = t["entry"] * 1.02
    ui.clear()
    main.handle("/paper", "555")
    assert "+2.00%" in ui[0][1] and ("❌ Exit #1 ETHUSDT", "pt:x:1") in flat(ui[0][2])
    ui.clear()
    main.handle_callback("pt:x:1", "555")
    assert "closed" in ui[0][1] and "+2.00%" in ui[0][1]
    assert not paper.open_trades("555") and paper.closed_trades("555")[0]["reason"] == "exited by you"
    ui.clear()
    main.handle("/history", "555")
    assert "win rate 100%" in ui[0][1]


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

import numpy as np
import pandas as pd
import pytest

from bot import backtest, chart, data, main, report, strategies, telegram
from bot.indicators import rsi


def candles(n=600, seed=3, drift=0.0006, vol=0.012, start=100.0, freq="4h"):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2025-01-01", periods=n, freq=freq, tz="UTC")
    close = start * np.exp(np.cumsum(rng.normal(drift, vol, n)))
    open_ = np.r_[start, close[:-1]]
    high = np.maximum(open_, close) * (1 + rng.uniform(0, 0.006, n))
    low = np.minimum(open_, close) * (1 - rng.uniform(0, 0.006, n))
    volume = 1000 * rng.lognormal(0, 0.45, n)  # real volume is spiky
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": volume}, index=idx)


@pytest.fixture
def sent(monkeypatch, tmp_path):
    log = []
    monkeypatch.setattr(telegram, "send_text",
                        lambda t, chat_id=None, buttons=None: log.append(("text", t, buttons)))
    monkeypatch.setattr(telegram, "send_photo",
                        lambda png, caption="", chat_id=None, buttons=None: log.append(("photo", png, caption)))
    monkeypatch.setattr(telegram, "set_commands", lambda: None)
    monkeypatch.setattr(telegram, "answer_callback", lambda cid, text="": None)
    monkeypatch.setattr(main.ai, "summarize", lambda facts: None)
    monkeypatch.setenv("BOT_STATE", str(tmp_path / "state.json"))
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "555")
    return log


def test_rsi_exact_wilder_value():
    vals = [100.0]
    for i in range(400):
        vals.append(vals[-1] + (2 if i % 2 == 0 else -1))
    assert rsi(pd.Series(vals)).iloc[-1] == pytest.approx(100 - 100 / (1 + 2 * 13 / 14), abs=1e-6)


def test_every_strategy_fires_somewhere_and_levels_are_sane():
    up, _ = strategies.prepare(candles(1500, seed=11))
    down, _ = strategies.prepare(candles(1500, seed=11, drift=-0.0006))
    assert len(strategies.ALL) == 14 and len(strategies.FAMILIES) == 7
    for strat in strategies.ALL:
        x = up if strat.side == "BUY" else down
        sig = strat.signal(x)
        assert sig.sum() > 0, f"{strat.side} {strat.name} never fired on 1500 random candles"
        for i in np.flatnonzero(sig.to_numpy())[:20]:
            entry = float(x["close"].iloc[i])
            stop = strat.stop(x, i, entry)
            assert (stop < entry) if strat.side == "BUY" else (stop > entry)


def test_evaluate_status_matches_checks():
    x, _ = strategies.prepare(candles())
    for strat in strategies.ALL:
        s = strategies.evaluate(strat, x)
        passed = sum(ok for ok, _ in s.checks)
        assert len(s.checks) == 3
        if s.status == "TRADE":
            assert passed == 3
            if s.side == "BUY":
                assert s.stop < s.entry < s.target
            else:
                assert s.target < s.entry < s.stop
            assert (s.target - s.entry) / (s.entry - s.stop) == pytest.approx(2.0)
        elif s.status == "WATCH":
            assert passed >= 2
        else:
            assert passed <= 1


class OneShot(strategies.Strategy):
    name = "test"

    def signal(self, x):
        s = pd.Series(False, index=x.index)
        s.iloc[10] = True
        return s

    def stop(self, x, i, entry):
        return entry - 1.0


def _path(bar12):
    n = 30
    idx = pd.date_range("2025-01-01", periods=n, freq="4h", tz="UTC")
    flat = np.full(n, 100.0)
    df = pd.DataFrame({"open": flat, "high": flat + 0.1, "low": flat - 0.1, "close": flat, "volume": 1.0},
                      index=idx)
    df.loc[idx[11], ["open", "high", "low", "close"]] = [100.0, 100.5, 99.8, 100.2]  # entry candle
    df.loc[idx[12], ["open", "high", "low", "close"]] = bar12
    return df


@pytest.mark.parametrize("bar12,expected_r", [
    ([100.2, 102.5, 100.0, 102.3], 2.0),   # trades up through the 102 target -> +2R
    ([103.0, 103.2, 102.9, 103.0], 3.0),   # gaps open above target -> filled at the open, +3R
    ([98.5, 98.9, 98.2, 98.6], -1.5),      # gaps open below the 99 stop -> filled at the open, -1.5R
    ([100.1, 100.3, 98.8, 99.0], -1.0),    # trades down through the stop -> -1R
])
def test_backtest_r_math_on_known_paths(monkeypatch, bar12, expected_r):
    monkeypatch.setitem(report.cfg()["costs"], "slippage_pct", 0.0)
    monkeypatch.setitem(report.cfg()["costs"], "crypto_pct_per_side", 0.0)
    res = backtest.run(OneShot(), _path(bar12), "crypto")
    assert res["trades"] == 1
    assert res["avg_r"] == pytest.approx(expected_r, abs=0.01)


class OneShotShort(OneShot):
    side = "SELL"

    def stop(self, x, i, entry):
        return entry + 1.0


@pytest.mark.parametrize("bar12,expected_r", [
    ([99.8, 100.0, 97.5, 97.7], 2.0),     # trades down through the 98 target -> +2R
    ([97.0, 97.1, 96.8, 97.0], 3.0),      # gaps open below target -> filled at the open, +3R
    ([101.5, 101.8, 101.1, 101.4], -1.5), # gaps open above the 101 stop -> -1.5R
    ([99.9, 101.2, 99.7, 101.0], -1.0),   # trades up through the stop -> -1R
])
def test_short_backtest_mirrors_long(monkeypatch, bar12, expected_r):
    monkeypatch.setitem(report.cfg()["costs"], "slippage_pct", 0.0)
    monkeypatch.setitem(report.cfg()["costs"], "crypto_pct_per_side", 0.0)
    df = _path(bar12)
    df.loc[df.index[11], ["open", "high", "low", "close"]] = [100.0, 100.2, 99.5, 99.8]
    res = backtest.run(OneShotShort(), df, "crypto")
    assert res["trades"] == 1
    assert res["avg_r"] == pytest.approx(expected_r, abs=0.01)


def test_backtest_costs_reduce_r(monkeypatch):
    res = backtest.run(OneShot(), _path([100.2, 102.5, 100.0, 102.3]), "crypto")
    # entry 100.05 (0.05% slip), stop 99, target 102.15, exit 102.099 after slip,
    # fees 0.1% x2 = 0.202 -> pnl 1.847; risk 1.05 + 0.2001 = 1.2501 -> +1.48R
    assert res["avg_r"] == pytest.approx(1.48, abs=0.01)


def test_full_report_text_and_chart(sent):
    a = report.analyze("SOLUSDT", "crypto", candles=candles(), change_pct=1.5)
    text = report.full_text(a, "summary here")
    assert "STRATEGY CHECK" in text and "FULL EXPLANATION" in text and "AI SUMMARY" in text
    for _, title in strategies.FAMILIES:
        assert title in text
    assert len(report.card(a)) <= 1024
    assert all(len(c) <= 4096 for c in telegram.split_text(report.explanation(a, "summary")))
    best = a.best() or a.setups[0]
    png = chart.render(a.x, "SOLUSDT · 4h", best if best.entry else None)
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 20000


def test_stock_position_size_in_shares():
    a = report.analyze("RELIANCE", "stock", candles=candles(500, freq="D", start=1400))
    s = strategies.Setup("k", "n", "i", "TRADE", entry=1400.0, stop=1372.0, target=1456.0)
    line = report.position_size(s, "stock")
    # 15000 * 0.5% = 75 INR risk; unit = 28 + 1400*0.2%*2 = 33.6 -> 2 shares
    assert "2 shares" in line and a.market == "stock"
    short = strategies.Setup("k", "n", "i", "TRADE", entry=1400.0, stop=1428.0, target=1344.0, side="SELL")
    assert "2 shares" in report.position_size(short, "stock")  # same risk distance, mirrored


def test_commands_route_and_deliver(monkeypatch, sent):
    monkeypatch.setattr(data, "crypto_candles", lambda sym, timeframe=None, limit=3000: candles())
    main.handle("/analyze sol")
    kinds = [k[0] for k in sent]
    assert kinds == ["photo", "text", "text"]          # trade card, strategy check, explanation
    assert "SOL/USDT" in sent[0][2]
    assert "STRATEGY CHECK" in sent[1][1] and "FULL EXPLANATION" in sent[2][1]
    flat = [b for row in sent[2][2] for b in row]
    assert ("🔄 Refresh", "a:c:SOLUSDT") in flat and ("📰 Crypto news", "n:c:crypto") in flat
    sent.clear()
    main.handle("/help")
    assert "/crypto" in sent[0][1]
    sent.clear()
    main.handle("/start")
    assert sent[0][2] == main.MENU


def test_crypto_ranking(monkeypatch, sent):
    monkeypatch.setattr(data, "top_crypto_by_volume",
                        lambda n: [{"symbol": f"C{i}USDT", "change_pct": 1.0} for i in range(4)])

    def fake(sym, timeframe=None, limit=3000):
        if sym == "C3USDT":
            raise data.DataError("C3USDT: gone")
        return candles(seed=int(sym[1]))

    monkeypatch.setattr(data, "crypto_candles", fake)
    main.handle("/crypto")
    listing, buttons = sent[0][1], sent[0][2]
    assert "CRYPTO SCANNER" in listing and "Skipped (no data): C3USDT" in listing
    flat = [b for row in buttons[:-1] for b in row]   # last row: news + menu
    assert len(flat) == 3 and flat[0][0].startswith("1. ")
    # numbers in the text match the buttons and the remembered list
    second = flat[1][1].split(":")[2]
    sent.clear()
    main.handle("2")
    assert [k[0] for k in sent] == ["photo", "text", "text"]
    assert second[:-4] in sent[0][2]
    sent.clear()
    main.handle("9")
    assert "Send /crypto or /stocks first" in sent[0][1]


def test_number_without_a_list(sent):
    main.handle("3")
    assert "Send /crypto or /stocks first" in sent[0][1]


def test_button_taps(monkeypatch, sent):
    monkeypatch.setattr(data, "crypto_candles", lambda sym, timeframe=None, limit=3000: candles())
    confirmed = []
    monkeypatch.setattr(telegram, "pending_updates", lambda: [
        {"update_id": 7, "callback_query": {"id": "q1", "data": "a:c:ETHUSDT", "from": {"id": 555},
                                            "message": {"chat": {"id": 555}}}},
        {"update_id": 8, "callback_query": {"id": "q2", "data": "cmd:/help", "from": {"id": 999},
                                            "message": {"chat": {"id": 999}}}},
    ])
    monkeypatch.setattr(telegram, "confirm_updates", lambda uid: confirmed.append(uid))
    main.run_commands()
    assert confirmed == [8]
    assert [k[0] for k in sent] == ["photo", "text", "text"] and "ETH/USDT" in sent[0][2]


def test_commands_only_from_owner(monkeypatch, sent):
    confirmed = []
    monkeypatch.setattr(telegram, "pending_updates", lambda: [
        {"update_id": 1, "message": {"chat": {"id": 999}, "text": "/help"}},
        {"update_id": 2, "message": {"chat": {"id": 555}, "text": "/help"}},
    ])
    monkeypatch.setattr(telegram, "confirm_updates", lambda uid: confirmed.append(uid))
    main.run_commands()
    assert confirmed == [2] and len(sent) == 1


def test_bad_symbol_reports_cleanly(monkeypatch, sent):
    def fail(*a, **k):
        raise data.DataError("XYZ: not available")

    monkeypatch.setattr(data, "crypto_candles", fail)
    monkeypatch.setattr(data, "stock_candles", fail)
    main.safe_handle("/analyze XYZ")
    assert "Data problem" in sent[0][1]


def test_split_text_keeps_blocks():
    text = "\n\n".join(["<b>x</b> " + "y" * 900] * 10)
    chunks = telegram.split_text(text)
    assert all(len(c) <= 4000 for c in chunks) and all(c.count("<b>") == c.count("</b>") for c in chunks)

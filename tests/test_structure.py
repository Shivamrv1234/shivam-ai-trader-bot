import numpy as np
import pandas as pd
import pytest

from bot import angel, data, report, strategies, structure
from bot.indicators import add_all
from tests.test_bot import candles
from tests.test_fno import fake_session

FEATURES = ["sup", "res", "dz_top", "sz_bot", "bz_top", "bzs_top", "tl_down", "tl_up", "tl_up_prev",
            "struct", "bars_since_bull", "bars_since_bear", "round_cross", "round_cross_down"]


def test_no_look_ahead():
    """A level known at candle i must be identical whether or not later candles exist."""
    full = add_all(candles(700, seed=21))
    xf, _ = structure.analyze(full)
    for cut in (250, 333, 480, 699):
        xt, _ = structure.analyze(full.iloc[: cut + 1])
        a, b = xf[FEATURES].iloc[cut], xt[FEATURES].iloc[cut]
        for col in FEATURES:
            va, vb = a[col], b[col]
            assert (va != va and vb != vb) or va == pytest.approx(vb), (cut, col, va, vb)


def test_strategy_signals_have_no_look_ahead():
    full = candles(700, seed=22)
    xf, _ = strategies.prepare(full)
    for strat in strategies.LONGS[4:] + strategies.SHORTS[4:]:
        sig_full = strat.signal(xf)
        for cut in (300, 450, 650):
            xt, _ = strategies.prepare(full.iloc[: cut + 1])
            assert bool(strat.signal(xt).iloc[-1]) == bool(sig_full.iloc[cut]), (strat.name, cut)


def test_round_steps():
    assert structure.round_steps(24020) == (1000, 500)
    assert structure.round_steps(1400) == (100, 50)
    assert structure.round_steps(0.62) == (0.01, 0.005)
    assert structure.nearby_round_levels(24020, 23700, 24400) == [24000.0]


def _frame(rows):
    idx = pd.date_range("2025-01-01", periods=len(rows), freq="D", tz="UTC")
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)
    df["volume"] = 1000.0
    return add_all(df)


def test_bos_and_order_block():
    # up to a swing high at 110, pull back with a red candle, then close above 110
    rows = [[100 + k, 101 + k, 99 + k, 100.5 + k] for k in range(10)]           # rise to ~110
    rows += [[109, 109.5, 106, 106.5], [106.5, 107, 104, 104.5], [104.5, 105, 103, 103.5],
             [103.5, 104.5, 103, 104], [104, 105, 103.5, 104.8]]                  # pull back
    rows += [[104.8, 112, 104.6, 111.5], [111.5, 113, 111, 112.5]]                # break above swing high
    x, snap = structure.analyze(_frame(rows))
    assert any(kind in ("BOS", "CHoCH") and d == 1 for _, _, kind, d in snap.breaks)
    assert x["struct"].iloc[-1] == 1
    assert any(z.kind == "ob_bull" for z in snap.zones)


def test_fvg_detection():
    rows = [[100, 101, 99, 100.5]] * 20 + [[100.5, 101, 100, 100.8], [101, 106, 100.9, 105.5],
                                           [105.6, 107, 103, 106.5], [106.5, 107, 105.5, 106]]
    x, snap = structure.analyze(_frame(rows))
    gaps = [z for z in snap.zones if z.kind == "fvg_bull"]
    assert gaps and gaps[0].bot == pytest.approx(101) and gaps[0].top == pytest.approx(103)


def test_patterns():
    rows = [[100, 101, 99, 100]] * 5 + [[101, 101.2, 99, 99.2], [99, 101.8, 98.9, 101.6],
                                        [101.6, 101.7, 99.0, 101.5]]
    pats = structure.candle_patterns(_frame(rows))
    assert bool(pats["bull_engulf"].iloc[-2]) and bool(pats["hammer"].iloc[-1])


def test_report_has_structure_and_flow_sections():
    a = report.analyze("SOLUSDT", "crypto", candles=candles(700, seed=9))
    a.flow = {"venue": "Binance", "unit": "USDT", "imbalance": 1.5, "bid_value_1pct": 1.5e6,
              "ask_value_1pct": 1e6, "bid_wall": (190.0, 5000.0), "ask_wall": (200.0, 3000.0),
              "spread_pct": 0.01, "taker_buy_pct": 61.0, "volume_pace": 1.8}
    text = report.full_text(a)
    assert "MARKET STRUCTURE &amp; KEY LEVELS" in text and "LIVE VOLUME &amp; ORDER BOOK" in text
    assert "buyers stronger" in text and "Biggest buy wall: 190.00" in text
    assert len(a.setups) == 14 and "Order book: buyers stronger" in report.card(a)


def test_crypto_orderflow_math(monkeypatch):
    from tests.conftest import REAL_CRYPTO_ORDERFLOW

    now_ms = pd.Timestamp.now(tz="UTC").value // 1_000_000

    def fake_get(url, params=None, tries=3):
        if url.endswith("/depth"):
            return {"bids": [["100.0", "10"], ["99.5", "50"], ["90", "999"]],
                    "asks": [["100.1", "5"], ["100.5", "8"]]}
        return [[now_ms - 8 * 3600_000, 0, 0, 0, 0, "100", 0, 0, 0, "60"],
                [now_ms - 4 * 3600_000, 0, 0, 0, 0, "200", 0, 0, 0, "130"],
                [now_ms - 2 * 3600_000, 0, 0, 0, 0, "50", 0, 0, 0, "30"]]

    monkeypatch.setattr(data, "_get", fake_get)
    f = REAL_CRYPTO_ORDERFLOW("BTC", avg_volume=100.0)
    assert f["bid_value_1pct"] == pytest.approx(100 * 10 + 99.5 * 50)   # 90 is beyond 1%
    assert f["ask_value_1pct"] == pytest.approx(100.1 * 5 + 100.5 * 8)
    assert f["bid_wall"] == (90.0, 999.0)
    assert f["taker_buy_pct"] == pytest.approx(65.0)
    assert f["volume_pace"] == pytest.approx(1.0, rel=0.05)   # 50 in half a 4h candle vs avg 100
    assert data.flow_verdict(f) == "buyers stronger"


def test_angel_orderflow_depth(monkeypatch):
    sess = fake_session()
    orig = sess.api.getMarketData

    def with_depth(mode, tokens):
        r = orig(mode, tokens)
        for q in r["data"]["fetched"]:
            q.update({"totBuyQuan": 300000, "totSellQuan": 150000, "tradeVolume": 2e6,
                      "depth": {"buy": [{"price": 1400.0, "quantity": 500, "orders": 3},
                                        {"price": 1399.5, "quantity": 2500, "orders": 9}],
                                "sell": [{"price": 1400.5, "quantity": 800, "orders": 4}]}})
        return r

    sess.api.getMarketData = with_depth
    f = angel.orderflow(sess, "RELIANCE", avg_daily_volume=1e6)
    assert f["imbalance"] == 2.0 and f["bid_wall"] == (1399.5, 2500) and f["ask_wall"] == (1400.5, 800)
    assert data.flow_verdict(f) == "buyers stronger"
    fi = angel.orderflow(sess, "NIFTY")  # index -> uses its near-month future's book
    assert "FUT" in fi["venue"]

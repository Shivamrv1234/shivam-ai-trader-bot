"""Four long-only strategies, each explained step by step.

Every strategy is a list of yes/no conditions. On the latest COMPLETED candle:
  all conditions true      -> TRADE  (entry = close, stop and target computed)
  all but one true         -> WATCH  (setup forming; levels shown for planning)
  otherwise                -> NO
These are well-known textbook setups, not guaranteed winners: every message
also shows how each one performed on this symbol's own history (app backtest).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from bot.config import cfg

WARMUP = 50  # candles


def _v(s: pd.Series, i: int) -> float:
    return float(s.iloc[i])


def _bars_since(cond: pd.Series, i: int) -> int | None:
    """Candles since `cond` was last True at or before i."""
    idx = np.flatnonzero(cond.iloc[: len(cond) + i + 1 if i < 0 else i + 1].to_numpy())
    if not len(idx):
        return None
    last_i = len(cond) + i if i < 0 else i
    return int(last_i - idx[-1])


def _fmt(x: float) -> str:
    if x == 0 or np.isnan(x):
        return f"{x:.2f}"
    mag = abs(x)
    if mag >= 1000:
        return f"{x:,.2f}"
    if mag >= 1:
        return f"{x:.4g}" if mag < 10 else f"{x:.2f}"
    return f"{x:.6g}"


class Strategy:
    key = ""
    name = ""
    idea = ""

    def parts(self, x: pd.DataFrame) -> list[pd.Series]:
        raise NotImplementedError

    def describe(self, x: pd.DataFrame, i: int) -> list[str]:
        raise NotImplementedError

    def stop(self, x: pd.DataFrame, i: int, entry: float) -> float:
        raise NotImplementedError

    def signal(self, x: pd.DataFrame) -> pd.Series:
        """True on candles where ALL conditions just became true (fresh signal)."""
        allp = pd.concat(self.parts(x), axis=1).fillna(False).all(axis=1)
        sig = allp & ~allp.shift(1, fill_value=False) & x["atr14"].notna()
        sig.iloc[:WARMUP] = False  # indicators are unreliable before this
        return sig


class EmaTrendCross(Strategy):
    key, name = "ema", "EMA Trend Cross"
    idea = "Fast EMA 9 crosses above EMA 21 while price is above EMA 50: a new up-swing inside an uptrend."

    def parts(self, x):
        crossed = pd.Series(False, index=x.index)
        for k in (1, 2, 3):
            crossed |= x["ema9"].shift(k) <= x["ema21"].shift(k)
        return [
            (x["ema9"] > x["ema21"]) & crossed,
            x["close"] > x["ema50"],
            x["rsi14"].between(50, 70),
        ]

    def describe(self, x, i):
        since = _bars_since(x["ema9"] <= x["ema21"], i)
        when = f"crossed {since} candle(s) ago" if since is not None else "no recent cross"
        rel = "above" if _v(x["ema9"], i) > _v(x["ema21"], i) else "below"
        return [
            f"EMA9 {_fmt(_v(x['ema9'], i))} is {rel} EMA21 {_fmt(_v(x['ema21'], i))} ({when}; need a cross within 3 candles)",
            f"Close {_fmt(_v(x['close'], i))} vs EMA50 {_fmt(_v(x['ema50'], i))} (need close above = uptrend)",
            f"RSI14 {_v(x['rsi14'], i):.1f} (need 50-70: momentum without being overbought)",
        ]

    def stop(self, x, i, entry):
        return entry - 1.5 * _v(x["atr14"], i)


class Breakout20(Strategy):
    key, name = "breakout", "20-Candle Breakout"
    idea = "Close above the highest high of the last 20 candles on strong volume: buyers overpower a range."

    def parts(self, x):
        return [
            x["close"] > x["hh20_prev"],
            x["rvol"] >= 1.5,
            x["close"] > x["ema50"],
        ]

    def describe(self, x, i):
        return [
            f"Close {_fmt(_v(x['close'], i))} vs 20-candle high {_fmt(_v(x['hh20_prev'], i))} (need close above)",
            f"Volume {_v(x['rvol'], i):.2f}x its 20-candle average (need 1.5x or more)",
            f"Close vs EMA50 {_fmt(_v(x['ema50'], i))} (need close above = trend support)",
        ]

    def stop(self, x, i, entry):
        atr = _v(x["atr14"], i)
        return min(_v(x["hh20_prev"], i) - 0.5 * atr, entry - atr)


class RsiPullback(Strategy):
    key, name = "pullback", "RSI Pullback in Uptrend"
    idea = "In an established uptrend, buy when RSI dips below 40 and recovers: a dip being bought."

    def parts(self, x):
        r = x["rsi14"]
        return [
            (x["ema50"] > x["ema200"]) & (x["close"] > x["ema200"]),
            r.rolling(6).min().shift(1) < 40,
            (r >= 40) & ((r.shift(1) < 40) | (r.shift(2) < 40)),
        ]

    def describe(self, x, i):
        low6 = float(x["rsi14"].rolling(6).min().shift(1).iloc[i])
        return [
            f"EMA50 {_fmt(_v(x['ema50'], i))} vs EMA200 {_fmt(_v(x['ema200'], i))}, close {_fmt(_v(x['close'], i))} "
            f"(need EMA50 above EMA200 and close above EMA200)",
            f"Lowest RSI14 in the previous 6 candles: {low6:.1f} (need a dip below 40)",
            f"RSI14 now {_v(x['rsi14'], i):.1f} (need back above 40 within the last 2 candles)",
        ]

    def stop(self, x, i, entry):
        atr = _v(x["atr14"], i)
        return min(_v(x["ll5"], i) - 0.5 * atr, entry - 0.5 * atr)


class MacdMomentum(Strategy):
    key, name = "macd", "MACD Momentum"
    idea = "MACD line crosses above its signal line while above zero: momentum turning up again in a rising market."

    def parts(self, x):
        m, s = x["macd"], x["macd_signal"]
        crossed = (m.shift(1) <= s.shift(1)) | (m.shift(2) <= s.shift(2))
        return [
            (m > s) & crossed,
            m > 0,
            x["close"] > x["ema21"],
        ]

    def describe(self, x, i):
        since = _bars_since(x["macd"] <= x["macd_signal"], i)
        when = f"crossed {since} candle(s) ago" if since is not None else "no recent cross"
        return [
            f"MACD {_fmt(_v(x['macd'], i))} vs signal {_fmt(_v(x['macd_signal'], i))} ({when}; need a cross within 2 candles)",
            f"MACD {_fmt(_v(x['macd'], i))} (need above 0)",
            f"Close {_fmt(_v(x['close'], i))} vs EMA21 {_fmt(_v(x['ema21'], i))} (need close above)",
        ]

    def stop(self, x, i, entry):
        return entry - 1.5 * _v(x["atr14"], i)


class SupportBounce(Strategy):
    key, name = "srbounce", "Support/Demand Bounce (Price Action)"
    idea = ("Price dips into a tested support level or a demand zone and prints a bullish reversal candle "
            "(engulfing or hammer) while the bigger trend is not down.")

    @staticmethod
    def level(x):
        return np.fmax(x["sup"], x["dz_top"])

    def parts(self, x):
        lvl = self.level(x)
        atr = x["atr14"]
        touch = lvl.notna() & (x["low"] <= lvl + 0.25 * atr) & (x["close"] > lvl - 0.25 * atr)
        return [
            touch,
            x["bull_engulf"] | x["hammer"],
            (x["close"] > x["ema200"]) | (x["struct"] == 1),
        ]

    def describe(self, x, i):
        lvl = float(self.level(x).iloc[i])
        dz = float(x["dz_top"].iloc[i])
        src = ("demand zone top" if dz == dz and abs(dz - lvl) < 1e-12 else
               f"support ({int(x['sup_touches'].iloc[i]) if x['sup_touches'].iloc[i] == x['sup_touches'].iloc[i] else 0} swing touches)")
        pat = ("bullish engulfing" if bool(x["bull_engulf"].iloc[i]) else
               "hammer / pin bar" if bool(x["hammer"].iloc[i]) else "no bullish pattern")
        trend = "bullish structure" if x["struct"].iloc[i] == 1 else (
            "above EMA200" if _v(x["close"], i) > _v(x["ema200"], i) else "below EMA200, bearish structure")
        lvl_txt = _fmt(lvl) if lvl == lvl else "none found"
        return [
            f"Low {_fmt(_v(x['low'], i))} vs nearest {src} {lvl_txt} (need the candle to dip within 0.25 ATR of it and close above)",
            f"Candle pattern: {pat} (need bullish engulfing or hammer)",
            f"Trend: {trend} (need price above EMA200 or bullish structure)",
        ]

    def stop(self, x, i, entry):
        lvl = float(self.level(x).iloc[i])
        base = min(_v(x["low"], i), lvl) if lvl == lvl else _v(x["low"], i)
        return min(base - 0.3 * _v(x["atr14"], i), entry - 0.5 * _v(x["atr14"], i))


class SmartMoneyRetest(Strategy):
    key, name = "smc", "Smart Money: BOS + Order Block/FVG Retest"
    idea = ("After a bullish break of structure (BOS or CHoCH), price returns to the order block or fair-value "
            "gap left behind by the move and closes bullish: buying where larger players bought.")

    def parts(self, x):
        mid = (x["bz_top"] + x["bz_bot"]) / 2
        return [
            (x["struct"] == 1) & (x["bars_since_bull"] <= 30),
            x["bz_top"].notna(),
            (x["close"] > x["open"]) & (x["close"] > mid),
        ]

    def describe(self, x, i):
        st = x["struct"].iloc[i]
        since = x["bars_since_bull"].iloc[i]
        kind = x["bull_break_kind"].iloc[i] or "break"
        if st == 1 and since == since:
            s1 = f"Bullish {kind} {int(since)} candle(s) ago (need a bullish BOS/CHoCH within 30 candles)"
        else:
            s1 = "Market structure is bearish or has no bullish break yet (need a bullish BOS/CHoCH)"
        top, bot = x["bz_top"].iloc[i], x["bz_bot"].iloc[i]
        if top == top:
            from bot.structure import ZONE_NAMES
            s2 = (f"Price retested a {ZONE_NAMES.get(x['bz_kind'].iloc[i], 'zone')} {_fmt(bot)}–{_fmt(top)} "
                  "in the last 3 candles")
            s3 = f"Close {_fmt(_v(x['close'], i))} vs zone middle {_fmt((top + bot) / 2)} (need a bullish candle closing above it)"
        else:
            s2 = "No retest of a bullish order block or fair-value gap in the last 3 candles"
            s3 = "Confirmation candle: waiting for a retest first"
        return [s1, s2, s3]

    def stop(self, x, i, entry):
        bot = x["bz_bot"].iloc[i]
        atr = _v(x["atr14"], i)
        base = bot - 0.3 * atr if bot == bot else entry - 1.5 * atr
        return min(base, entry - 0.5 * atr)


class LevelBreakout(Strategy):
    key, name = "levelbreak", "Trendline / Round-Number Breakout"
    idea = ("Close breaks above a falling trend line or a psychological round number (like 24,000 or 100,000) "
            "with above-average volume and a strong close.")

    def parts(self, x):
        tl = (x["close"] > x["tl_down"]) & (x["close"].shift(1) <= x["tl_down_prev"])
        rnd = x["round_cross"].notna()
        rng = (x["high"] - x["low"]).replace(0, np.nan)
        return [
            tl | rnd,
            x["rvol"] >= 1.3,
            (x["close"] - x["low"]) >= 0.7 * rng,
        ]

    def describe(self, x, i):
        tlv = x["tl_down"].iloc[i]
        rnd = x["round_cross"].iloc[i]
        tl_broke = bool(((x["close"] > x["tl_down"]) & (x["close"].shift(1) <= x["tl_down_prev"])).iloc[i])
        bits = []
        if tl_broke:
            bits.append(f"closed above the falling trend line ({_fmt(tlv)})")
        if rnd == rnd:
            bits.append(f"crossed the round number {_fmt(rnd)}")
        if bits:
            s1 = "Price " + " and ".join(bits)
        else:
            tl_txt = f"falling trend line at {_fmt(tlv)}" if tlv == tlv else "no falling trend line"
            s1 = f"No breakout this candle ({tl_txt}; need a close above it or above a round number)"
        rng = _v(x["high"], i) - _v(x["low"], i)
        pos = (_v(x["close"], i) - _v(x["low"], i)) / rng * 100 if rng > 0 else 0
        return [
            s1,
            f"Volume {_v(x['rvol'], i):.2f}x its 20-candle average (need 1.3x or more)",
            f"Close sits at {pos:.0f}% of the candle's range (need 70% or higher = strong close)",
        ]

    def stop(self, x, i, entry):
        return entry - 1.5 * _v(x["atr14"], i)


ALL: list[Strategy] = [EmaTrendCross(), Breakout20(), RsiPullback(), MacdMomentum(),
                       SupportBounce(), SmartMoneyRetest(), LevelBreakout()]


def prepare(candles: pd.DataFrame):
    """Indicators + market structure. Returns (x, snapshot)."""
    from bot import structure
    from bot.indicators import add_all

    return structure.analyze(add_all(candles))


@dataclass
class Setup:
    key: str
    name: str
    idea: str
    status: str
    checks: list = field(default_factory=list)  # [(ok: bool, text: str)]
    entry: float | None = None
    stop: float | None = None
    target: float | None = None
    fresh: bool = False
    backtest: dict = field(default_factory=dict)


def evaluate(strategy: Strategy, x: pd.DataFrame) -> Setup:
    oks = [bool(p.fillna(False).iloc[-1]) for p in strategy.parts(x)]
    texts = strategy.describe(x, -1)
    passed = sum(oks)
    status = "TRADE" if passed == len(oks) else ("WATCH" if passed == len(oks) - 1 else "NO")
    setup = Setup(strategy.key, strategy.name, strategy.idea, status, list(zip(oks, texts)))
    if status in ("TRADE", "WATCH"):
        entry = _v(x["close"], -1)
        stop = strategy.stop(x, -1, entry)
        if stop < entry:
            r = cfg()["risk"]["target_r"]
            setup.entry, setup.stop, setup.target = entry, stop, entry + r * (entry - stop)
        elif status == "TRADE":
            setup.status = "WATCH"
    setup.fresh = status == "TRADE" and bool(strategy.signal(x).iloc[-1])
    return setup

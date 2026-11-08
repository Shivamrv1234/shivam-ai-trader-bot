"""Fourteen chart strategies, each with a BUY version and a mirrored SELL version, explained step by step.

Every strategy is three yes/no conditions. On the latest COMPLETED candle:
  all 3 true      -> TRADE  (entry = close, stop and target computed)
  2 of 3 true     -> WATCH  (setup forming; levels shown for planning)
  otherwise       -> NO
  not applicable  -> NA     (e.g. VWAP or opening-range rules on a daily chart, or on crypto)

The library merges the original 7 strategies with the 12 "high win-rate" setups without overlap:
  1 MA Crossover ........ upgraded EMA Trend Cross (pair by timeframe, 200-EMA and ADX filters)
  2 20-Candle Breakout .. kept
  3 EMA Pullback ........ upgraded RSI Pullback (20 > 50 > 200 alignment, bounce off EMA20/50)
  4 MACD Momentum ....... kept
  5 Demand/Supply + Confluence .. upgraded S/R Reaction, merges "Supply & Demand + Confluence"
  6 Smart Money (BOS + OB/FVG) .. kept
  7 Trendline / Round Number .... kept
  8 Fibonacci Golden Zone ....... new
  9 Break & Retest .............. new
 10 RSI(2) Mean Reversion ....... new (Connors)
 11 Bollinger Mean Reversion .... new
 12 VWAP Pullback ............... new (intraday)
 13 Opening Range Breakout ...... new (NSE intraday)
 14 Gap Fill .................... new (NSE intraday)
Option selling (iron condor) lives in the F&O planner (bot/fno_plan.py) and pairs trading in
bot/pairs.py, because they need an option chain or two symbols instead of one chart.

These are well-known textbook setups, not guaranteed winners: every message also shows how each
one performed on this symbol's own history (app backtest).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from bot.config import cfg

WARMUP = 50  # candles
INTRADAY_TFS = ("5m", "15m", "30m", "1h")


def _v(s: pd.Series, i: int) -> float:
    return float(s.iloc[i])


def _ok(v) -> bool:
    return v == v and v is not None


def _bars_since(cond: pd.Series, i: int) -> int | None:
    """Candles since `cond` was last True at or before i."""
    idx = np.flatnonzero(cond.iloc[: len(cond) + i + 1 if i < 0 else i + 1].to_numpy())
    if not len(idx):
        return None
    last_i = len(cond) + i if i < 0 else i
    return int(last_i - idx[-1])


def _fmt(x: float) -> str:
    if x is None or x == 0 or np.isnan(x):
        return f"{(x or 0):.2f}"
    mag = abs(x)
    if mag >= 1000:
        return f"{x:,.2f}"
    if mag >= 1:
        return f"{x:.4g}" if mag < 10 else f"{x:.2f}"
    return f"{x:.6g}"


def has_volume(x: pd.DataFrame) -> bool:
    """Spot indices and some forex feeds have no volume; volume rules are then skipped."""
    if "has_volume" in x.attrs:
        return bool(x.attrs["has_volume"])
    v = x["volume"].tail(200)
    return bool(len(v) and (v > 0).mean() > 0.8)


def vol_ok(x: pd.DataFrame, thr: float) -> pd.Series:
    if not has_volume(x):
        return pd.Series(True, index=x.index)
    return x["rvol"] >= thr


def vol_txt(x: pd.DataFrame, i: int, thr: float) -> str:
    if not has_volume(x):
        return "Volume: not available for this instrument (rule skipped)"
    return f"Volume {_v(x['rvol'], i):.2f}x its 20-candle average (need {thr:g}x or more)"


def bar_minutes(x: pd.DataFrame) -> float:
    if len(x) < 3:
        return 1440.0
    return float(pd.Series(x.index[-200:]).diff().dt.total_seconds().median() / 60)


class Strategy:
    key = ""
    name = ""
    idea = ""
    side = "BUY"     # BUY or SELL
    family = ""      # shared id of the BUY/SELL pair
    regime = "any"   # trend | range | any (shown in /strategies)
    max_bars: int | None = None   # time stop (candles) used by the backtest
    exit_note = ""

    @property
    def up(self) -> bool:
        return self.side == "BUY"

    def applies(self, x: pd.DataFrame) -> str | None:
        """None if the strategy can run on this chart, otherwise the reason it can't."""
        return None

    def parts(self, x: pd.DataFrame) -> list[pd.Series]:
        raise NotImplementedError

    def describe(self, x: pd.DataFrame, i: int) -> list[str]:
        raise NotImplementedError

    def stop(self, x: pd.DataFrame, i: int, entry: float) -> float:
        raise NotImplementedError

    def target(self, x: pd.DataFrame, i: int, entry: float, stop: float) -> float | None:
        """Take-profit price; None = no fixed target (exit rule / time stop instead)."""
        return entry + cfg()["risk"]["target_r"] * (entry - stop)

    def targets(self, x: pd.DataFrame, i: int, entry: float, stop: float) -> list[tuple[str, float]]:
        """Extra targets shown to the user (Fibonacci T1/T2/T3)."""
        return []

    def exit_rule(self, x: pd.DataFrame) -> pd.Series | None:
        """True on candles that close the trade at the next open (opposite cross, back to the mean...)."""
        return None

    def signal(self, x: pd.DataFrame) -> pd.Series:
        """True on candles where ALL conditions just became true (fresh signal)."""
        allp = pd.concat(self.parts(x), axis=1).fillna(False).astype(bool).all(axis=1)
        sig = allp & ~allp.shift(1, fill_value=False) & x["atr14"].notna()
        sig.iloc[:WARMUP] = False  # indicators are unreliable before this
        return sig

    def _sgn(self) -> int:
        return 1 if self.up else -1


def _min_gap(stop: float, entry: float, atr: float, up: bool, k: float = 0.5) -> float:
    """Keep the stop at least k ATR away from the entry."""
    return min(stop, entry - k * atr) if up else max(stop, entry + k * atr)


def _crossed_within(fast: pd.Series, slow: pd.Series, up: bool, n: int) -> pd.Series:
    """fast is above (below) slow now and was on the other side within the last n candles."""
    now = fast > slow if up else fast < slow
    before = pd.Series(False, index=fast.index)
    for k in range(1, n + 1):
        before |= (fast.shift(k) <= slow.shift(k)) if up else (fast.shift(k) >= slow.shift(k))
    return now & before


# ---------------------------------------------------------------- 1. MA crossover (trend following)

def ma_pair(x: pd.DataFrame) -> tuple[int, int]:
    """Intraday 9/21 EMA on 5m-15m, swing 20/50 EMA on 30m and above (the 50/200 golden/death cross
    is reported as context on daily charts)."""
    return (9, 21) if bar_minutes(x) <= 15 else (20, 50)


class MaCrossover(Strategy):
    family, regime = "ma", "trend"
    name = "Moving Average Crossover"
    exit_note = "Exit on the opposite crossover or at the target."

    def __init__(self, side="BUY"):
        self.side = side
        self.key = "ma" if side == "BUY" else "ma_s"
        self.idea = ("Fast EMA crosses above the slow EMA (9/21 intraday, 20/50 swing) in the direction of "
                     "the 200 EMA, with ADX above 20 so the market is really trending."
                     if self.up else
                     "Fast EMA crosses below the slow EMA (9/21 intraday, 20/50 swing) under the 200 EMA, with "
                     "ADX above 20 so the market is really trending.")

    def parts(self, x):
        f, s = ma_pair(x)
        fast, slow = x[f"ema{f}"], x[f"ema{s}"]
        return [
            _crossed_within(fast, slow, self.up, 3),
            x["close"] > x["ema200"] if self.up else x["close"] < x["ema200"],
            x["adx14"] > 20,
        ]

    def describe(self, x, i):
        f, s = ma_pair(x)
        fv, sv = _v(x[f"ema{f}"], i), _v(x[f"ema{s}"], i)
        rel = "above" if fv > sv else "below"
        cond = x[f"ema{f}"] <= x[f"ema{s}"] if self.up else x[f"ema{f}"] >= x[f"ema{s}"]
        since = _bars_since(cond, i)
        want = "up" if self.up else "down"
        when = (f"crossed {want} {since} candle(s) ago" if since is not None and
                ((fv > sv) == self.up) else f"not crossed {want}")
        lines = [
            f"EMA{f} {_fmt(fv)} is {rel} EMA{s} {_fmt(sv)} ({when}; need a cross {want} within 3 candles)",
            f"Close {_fmt(_v(x['close'], i))} vs EMA200 {_fmt(_v(x['ema200'], i))} "
            f"(need {'above' if self.up else 'below'}: trade with the long-term trend)",
            f"ADX14 {_v(x['adx14'], i):.1f} (need above 20 = trending market)",
        ]
        if bar_minutes(x) >= 1440 and _ok(_v(x["sma200"], i)):
            gc = "golden cross (SMA50 above SMA200)" if _v(x["sma50"], i) > _v(x["sma200"], i) else \
                "death cross (SMA50 below SMA200)"
            lines[1] += f"; position trend: {gc}"
        return lines

    def stop(self, x, i, entry):
        atr = _v(x["atr14"], i)
        swing = _v(x["ll5"], i) - 0.1 * atr if self.up else _v(x["hh5"], i) + 0.1 * atr
        atr_stop = entry - 1.5 * atr if self.up else entry + 1.5 * atr
        # the closer of "recent swing" and "1.5 ATR", but never nearer than 0.5 ATR
        st = max(swing, atr_stop) if self.up else min(swing, atr_stop)
        return _min_gap(st, entry, atr, self.up)

    def exit_rule(self, x):
        f, s = ma_pair(x)
        return x[f"ema{f}"] < x[f"ema{s}"] if self.up else x[f"ema{f}"] > x[f"ema{s}"]


# ---------------------------------------------------------------- 2. 20-candle breakout (kept)

class Breakout20(Strategy):
    family, regime = "breakout", "trend"
    name = "20-Candle Breakout"

    def __init__(self, side="BUY"):
        self.side = side
        self.key = "breakout" if side == "BUY" else "breakout_s"
        self.idea = ("Close above the highest high of the last 20 candles on strong volume: buyers overpower "
                     "a range." if self.up else
                     "Close below the lowest low of the last 20 candles on strong volume: sellers break the range.")

    def parts(self, x):
        return [
            x["close"] > x["hh20_prev"] if self.up else x["close"] < x["ll20_prev"],
            vol_ok(x, 1.5),
            x["close"] > x["ema50"] if self.up else x["close"] < x["ema50"],
        ]

    def describe(self, x, i):
        lvl = _v(x["hh20_prev"] if self.up else x["ll20_prev"], i)
        word = "high" if self.up else "low"
        side = "above" if self.up else "below"
        return [
            f"Close {_fmt(_v(x['close'], i))} vs 20-candle {word} {_fmt(lvl)} (need close {side})",
            vol_txt(x, i, 1.5),
            f"Close vs EMA50 {_fmt(_v(x['ema50'], i))} (need close {side} = trend support)",
        ]

    def stop(self, x, i, entry):
        atr = _v(x["atr14"], i)
        if self.up:
            return min(_v(x["hh20_prev"], i) - 0.5 * atr, entry - atr)
        return max(_v(x["ll20_prev"], i) + 0.5 * atr, entry + atr)


# ---------------------------------------------------------------- 3. EMA pullback (trend continuation)

class EmaPullback(Strategy):
    family, regime = "pullback", "trend"
    name = "EMA Pullback (20/50/200)"
    exit_note = "Trail: exit when a candle closes back through EMA20, or at the target."

    def __init__(self, side="BUY"):
        self.side = side
        self.key = "pullback" if side == "BUY" else "pullback_s"
        self.idea = ("EMAs stacked 20 > 50 > 200 (strong uptrend); price pulls back to EMA20 or EMA50 and a "
                     "bullish candle closes back above EMA20." if self.up else
                     "EMAs stacked 20 < 50 < 200 (strong downtrend); price rallies to EMA20 or EMA50 and a "
                     "bearish candle closes back below EMA20.")

    def parts(self, x):
        atr = x["atr14"]
        if self.up:
            aligned = (x["ema20"] > x["ema50"]) & (x["ema50"] > x["ema200"])
            touch = (x["low"].rolling(3).min() <= x["ema20"] + 0.2 * atr) & (x["close"] > x["ema50"] - 0.25 * atr)
            trigger = (x["close"] > x["open"]) & (x["close"] > x["ema20"])
        else:
            aligned = (x["ema20"] < x["ema50"]) & (x["ema50"] < x["ema200"])
            touch = (x["high"].rolling(3).max() >= x["ema20"] - 0.2 * atr) & (x["close"] < x["ema50"] + 0.25 * atr)
            trigger = (x["close"] < x["open"]) & (x["close"] < x["ema20"])
        return [aligned, touch, trigger]

    def describe(self, x, i):
        e20, e50, e200 = (_v(x[f"ema{n}"], i) for n in (20, 50, 200))
        if self.up:
            ext = float(x["low"].rolling(3).min().iloc[i])
            return [
                f"EMA20 {_fmt(e20)} · EMA50 {_fmt(e50)} · EMA200 {_fmt(e200)} (need 20 > 50 > 200 = bullish alignment)",
                f"Lowest low of the last 3 candles {_fmt(ext)} (need a dip to EMA20 or EMA50, holding above EMA50)",
                f"Candle {'bullish' if _v(x['close'], i) > _v(x['open'], i) else 'bearish'}, close "
                f"{_fmt(_v(x['close'], i))} (need a bullish close above EMA20)",
            ]
        ext = float(x["high"].rolling(3).max().iloc[i])
        return [
            f"EMA20 {_fmt(e20)} · EMA50 {_fmt(e50)} · EMA200 {_fmt(e200)} (need 20 < 50 < 200 = bearish alignment)",
            f"Highest high of the last 3 candles {_fmt(ext)} (need a rally to EMA20 or EMA50, holding below EMA50)",
            f"Candle {'bearish' if _v(x['close'], i) < _v(x['open'], i) else 'bullish'}, close "
            f"{_fmt(_v(x['close'], i))} (need a bearish close below EMA20)",
        ]

    def stop(self, x, i, entry):
        atr = _v(x["atr14"], i)
        if self.up:
            st = min(_v(x["ll5"], i), _v(x["ema50"], i)) - 0.2 * atr
        else:
            st = max(_v(x["hh5"], i), _v(x["ema50"], i)) + 0.2 * atr
        return _min_gap(st, entry, atr, self.up)

    def exit_rule(self, x):
        if self.up:
            return (x["close"] < x["ema20"]) & (x["close"].shift(1) < x["ema20"].shift(1))
        return (x["close"] > x["ema20"]) & (x["close"].shift(1) > x["ema20"].shift(1))


# ---------------------------------------------------------------- 4. MACD momentum (kept)

class MacdMomentum(Strategy):
    family, regime = "macd", "trend"
    name = "MACD Momentum"

    def __init__(self, side="BUY"):
        self.side = side
        self.key = "macd" if side == "BUY" else "macd_s"
        self.idea = ("MACD line crosses above its signal line while above zero: momentum turning up again in a "
                     "rising market." if self.up else
                     "MACD line crosses below its signal line while below zero: momentum turning down again in a "
                     "falling market.")

    def parts(self, x):
        m, s = x["macd"], x["macd_signal"]
        return [
            _crossed_within(m, s, self.up, 2),
            m > 0 if self.up else m < 0,
            x["close"] > x["ema21"] if self.up else x["close"] < x["ema21"],
        ]

    def describe(self, x, i):
        m, s = _v(x["macd"], i), _v(x["macd_signal"], i)
        cond = x["macd"] <= x["macd_signal"] if self.up else x["macd"] >= x["macd_signal"]
        since = _bars_since(cond, i)
        want = "up" if self.up else "down"
        when = (f"crossed {want} {since} candle(s) ago" if since is not None and ((m > s) == self.up)
                else f"{'below' if self.up else 'above'} the signal line now")
        side = "above" if self.up else "below"
        return [
            f"MACD {_fmt(m)} vs signal {_fmt(s)} ({when}; need a cross {want} within 2 candles)",
            f"MACD {_fmt(m)} (need {side} 0)",
            f"Close {_fmt(_v(x['close'], i))} vs EMA21 {_fmt(_v(x['ema21'], i))} (need close {side})",
        ]

    def stop(self, x, i, entry):
        d = 1.5 * _v(x["atr14"], i)
        return entry - d if self.up else entry + d


# ---------------------------------------------------------------- 5. demand/supply + confluence

def fib_zone(x: pd.DataFrame, up: bool) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Causal Fibonacci leg from the last confirmed swings: (leg size, 50% level, 61.8% level).
    Uptrend leg = swing LOW then swing HIGH; downtrend leg = swing HIGH then swing LOW."""
    hi, lo = x["sw_hi"], x["sw_lo"]
    order = x["sw_lo_i"] < x["sw_hi_i"] if up else x["sw_hi_i"] < x["sw_lo_i"]
    leg = (hi - lo).where(order)
    if up:
        return leg, hi - 0.5 * leg, hi - 0.618 * leg
    return leg, lo + 0.5 * leg, lo + 0.618 * leg


class ZoneConfluence(Strategy):
    family, regime = "zone", "any"
    name = "Demand/Supply Zone + Confluence"

    def __init__(self, side="BUY"):
        self.side = side
        self.key = "zone" if side == "BUY" else "zone_s"
        self.idea = ("Price dips into a fresh demand zone or tested support and prints a bullish reversal candle, "
                     "with confluence: 200 EMA, the Fibonacci golden zone or bullish structure."
                     if self.up else
                     "Price rallies into a fresh supply zone or tested resistance and prints a bearish reversal "
                     "candle, with confluence: 200 EMA, the Fibonacci golden zone or bearish structure.")

    def level(self, x):
        return np.fmax(x["sup"], x["dz_top"]) if self.up else np.fmin(x["res"], x["sz_bot"])

    def confluence(self, x) -> pd.DataFrame:
        atr = x["atr14"]
        leg, f50, f618 = fib_zone(x, self.up)
        lo_band, hi_band = np.fmin(f50, f618), np.fmax(f50, f618)
        if self.up:
            ema = (x["close"] > x["ema200"]) | ((x["low"] - x["ema200"]).abs() <= 0.5 * atr)
            fib = leg.notna() & (x["low"] <= hi_band + 0.2 * atr) & (x["low"] >= lo_band - 0.5 * atr)
            st = x["struct"] == 1
            zone = x["dz_top"].notna() & (x["low"] <= x["dz_top"] + 0.25 * atr)
        else:
            ema = (x["close"] < x["ema200"]) | ((x["high"] - x["ema200"]).abs() <= 0.5 * atr)
            fib = leg.notna() & (x["high"] >= lo_band - 0.2 * atr) & (x["high"] <= hi_band + 0.5 * atr)
            st = x["struct"] == -1
            zone = x["sz_bot"].notna() & (x["high"] >= x["sz_bot"] - 0.25 * atr)
        return pd.DataFrame({"200 EMA": ema, "Fib 50–61.8%": fib, "structure": st, "fresh zone": zone})

    def parts(self, x):
        lvl, atr = self.level(x), x["atr14"]
        if self.up:
            touch = lvl.notna() & (x["low"] <= lvl + 0.25 * atr) & (x["close"] > lvl - 0.25 * atr)
            pattern = x["bull_engulf"] | x["hammer"]
        else:
            touch = lvl.notna() & (x["high"] >= lvl - 0.25 * atr) & (x["close"] < lvl + 0.25 * atr)
            pattern = x["bear_engulf"] | x["shooting_star"]
        return [touch, pattern, self.confluence(x).sum(axis=1) >= 2]

    def describe(self, x, i):
        lvl = float(self.level(x).iloc[i])
        lvl_txt = _fmt(lvl) if lvl == lvl else "none found"
        conf = self.confluence(x).iloc[i]
        have = [k for k, v in conf.items() if bool(v)]
        if self.up:
            dz = float(x["dz_top"].iloc[i])
            src = "demand zone top" if dz == dz and abs(dz - lvl) < 1e-12 else "support"
            pat = ("bullish engulfing" if bool(x["bull_engulf"].iloc[i]) else
                   "hammer / pin bar" if bool(x["hammer"].iloc[i]) else "no bullish pattern")
            return [
                f"Low {_fmt(_v(x['low'], i))} vs nearest {src} {lvl_txt} (need a dip within 0.25 ATR and a close above)",
                f"Candle pattern: {pat} (need bullish engulfing or hammer)",
                f"Confluence: {', '.join(have) if have else 'none'} (need 2+ of 200 EMA, Fib golden zone, bullish "
                "structure, fresh demand zone)",
            ]
        sz = float(x["sz_bot"].iloc[i])
        src = "supply zone bottom" if sz == sz and abs(sz - lvl) < 1e-12 else "resistance"
        pat = ("bearish engulfing" if bool(x["bear_engulf"].iloc[i]) else
               "shooting star" if bool(x["shooting_star"].iloc[i]) else "no bearish pattern")
        return [
            f"High {_fmt(_v(x['high'], i))} vs nearest {src} {lvl_txt} (need a rally within 0.25 ATR and a close below)",
            f"Candle pattern: {pat} (need bearish engulfing or shooting star)",
            f"Confluence: {', '.join(have) if have else 'none'} (need 2+ of 200 EMA, Fib golden zone, bearish "
            "structure, fresh supply zone)",
        ]

    def stop(self, x, i, entry):
        lvl = float(self.level(x).iloc[i])
        atr = _v(x["atr14"], i)
        if self.up:
            zb = float(x["dz_bot"].iloc[i])
            base = min(v for v in (_v(x["low"], i), lvl, zb) if v == v)
            return _min_gap(base - 0.3 * atr, entry, atr, True)
        zt = float(x["sz_top"].iloc[i])
        base = max(v for v in (_v(x["high"], i), lvl, zt) if v == v)
        return _min_gap(base + 0.3 * atr, entry, atr, False)

    def target(self, x, i, entry, stop):
        """The opposite zone / level, but at least 2R."""
        r2 = super().target(x, i, entry, stop)
        opp = np.fmin(x["res"], x["sz_bot"]) if self.up else np.fmax(x["sup"], x["dz_top"])
        o = float(opp.iloc[i])
        if o == o and ((o > r2) if self.up else (o < r2)):
            return o
        return r2


# ---------------------------------------------------------------- 6. smart money (kept)

class SmartMoneyRetest(Strategy):
    family, regime = "smc", "any"
    name = "Smart Money: BOS + Order Block/FVG Retest"

    def __init__(self, side="BUY"):
        self.side = side
        self.key = "smc" if side == "BUY" else "smc_s"
        self.idea = ("After a bullish break of structure (BOS or CHoCH), price returns to the order block or "
                     "fair-value gap left behind by the move and closes bullish: buying where larger players bought."
                     if self.up else
                     "After a bearish break of structure (BOS or CHoCH), price returns to the bearish order block "
                     "or fair-value gap left by the drop and closes bearish: selling where larger players sold.")

    def _cols(self):
        return ("bz_top", "bz_bot", "bz_kind", "bars_since_bull", "bull_break_kind", 1) if self.up else \
            ("bzs_top", "bzs_bot", "bzs_kind", "bars_since_bear", "bear_break_kind", -1)

    def parts(self, x):
        top, bot, _, since, _, d = self._cols()
        mid = (x[top] + x[bot]) / 2
        conf = (x["close"] > x["open"]) & (x["close"] > mid) if self.up else \
            (x["close"] < x["open"]) & (x["close"] < mid)
        return [(x["struct"] == d) & (x[since] <= 30), x[top].notna(), conf]

    def describe(self, x, i):
        topc, botc, kindc, sincec, bkind, d = self._cols()
        word = "Bullish" if self.up else "Bearish"
        st, since = x["struct"].iloc[i], x[sincec].iloc[i]
        kind = x[bkind].iloc[i] or "break"
        if st == d and since == since:
            s1 = f"{word} {kind} {int(since)} candle(s) ago (need a {word.lower()} BOS/CHoCH within 30 candles)"
        else:
            s1 = (f"Market structure is {'bearish' if self.up else 'bullish'} or has no {word.lower()} break yet "
                  f"(need a {word.lower()} BOS/CHoCH)")
        top, bot = x[topc].iloc[i], x[botc].iloc[i]
        if top == top:
            from bot.structure import ZONE_NAMES
            s2 = f"Price retested a {ZONE_NAMES.get(x[kindc].iloc[i], 'zone')} {_fmt(bot)}–{_fmt(top)} in the last 3 candles"
            s3 = (f"Close {_fmt(_v(x['close'], i))} vs zone middle {_fmt((top + bot) / 2)} "
                  f"(need a {'bullish' if self.up else 'bearish'} candle closing {'above' if self.up else 'below'} it)")
        else:
            s2 = f"No retest of a {word.lower()} order block or fair-value gap in the last 3 candles"
            s3 = "Confirmation candle: waiting for a retest first"
        return [s1, s2, s3]

    def stop(self, x, i, entry):
        atr = _v(x["atr14"], i)
        if self.up:
            bot = x["bz_bot"].iloc[i]
            base = bot - 0.3 * atr if bot == bot else entry - 1.5 * atr
            return min(base, entry - 0.5 * atr)
        top = x["bzs_top"].iloc[i]
        base = top + 0.3 * atr if top == top else entry + 1.5 * atr
        return max(base, entry + 0.5 * atr)


# ---------------------------------------------------------------- 7. trendline / round number (kept)

class LevelBreakout(Strategy):
    family, regime = "levelbreak", "trend"
    name = "Trendline / Round-Number Breakout"

    def __init__(self, side="BUY"):
        self.side = side
        self.key = "levelbreak" if side == "BUY" else "levelbreak_s"
        self.idea = ("Close breaks above a falling trend line or a psychological round number (like 24,000 or "
                     "100,000) with above-average volume and a strong close." if self.up else
                     "Close breaks below a rising trend line or a psychological round number with above-average "
                     "volume and a weak close.")

    def _tl(self, x):
        if self.up:
            return (x["close"] > x["tl_down"]) & (x["close"].shift(1) <= x["tl_down_prev"])
        return (x["close"] < x["tl_up"]) & (x["close"].shift(1) >= x["tl_up_prev"])

    def parts(self, x):
        rnd = x["round_cross"].notna() if self.up else x["round_cross_down"].notna()
        rng = (x["high"] - x["low"]).replace(0, np.nan)
        strong = (x["close"] - x["low"]) >= 0.7 * rng if self.up else (x["high"] - x["close"]) >= 0.7 * rng
        return [self._tl(x) | rnd, vol_ok(x, 1.3), strong]

    def describe(self, x, i):
        tlv = x["tl_down" if self.up else "tl_up"].iloc[i]
        rnd = x["round_cross" if self.up else "round_cross_down"].iloc[i]
        broke = bool(self._tl(x).iloc[i])
        bits = []
        if broke:
            bits.append(f"closed {'above the falling' if self.up else 'below the rising'} trend line ({_fmt(tlv)})")
        if rnd == rnd:
            bits.append(f"{'crossed' if self.up else 'fell through'} the round number {_fmt(rnd)}")
        if bits:
            s1 = "Price " + " and ".join(bits)
        else:
            kind = "falling" if self.up else "rising"
            tl_txt = f"{kind} trend line at {_fmt(tlv)}" if tlv == tlv else f"no {kind} trend line"
            s1 = (f"No {'breakout' if self.up else 'breakdown'} this candle ({tl_txt}; need a close "
                  f"{'above' if self.up else 'below'} it or a round number)")
        rng = _v(x["high"], i) - _v(x["low"], i)
        if self.up:
            pos = (_v(x["close"], i) - _v(x["low"], i)) / rng * 100 if rng > 0 else 0
            s3 = f"Close sits at {pos:.0f}% of the candle's range (need 70% or higher = strong close)"
        else:
            pos = (_v(x["high"], i) - _v(x["close"], i)) / rng * 100 if rng > 0 else 0
            s3 = f"Close sits {pos:.0f}% down from the candle's high (need 70% or more = weak close)"
        return [s1, vol_txt(x, i, 1.3), s3]

    def stop(self, x, i, entry):
        d = 1.5 * _v(x["atr14"], i)
        return entry - d if self.up else entry + d


# ---------------------------------------------------------------- 8. Fibonacci golden zone pullback

class FibPullback(Strategy):
    family, regime = "fib", "trend"
    name = "Fibonacci Golden Zone Pullback"
    exit_note = "T1 = previous swing (0%), T2 = 127.2%, T3 = 161.8% extension; stop beyond 78.6%."

    def __init__(self, side="BUY"):
        self.side = side
        self.key = "fib" if side == "BUY" else "fib_s"
        self.idea = ("In an uptrend (above EMA50, higher highs and higher lows), price retraces into the 50–61.8% "
                     "'golden zone' of the last swing low→high and prints a bullish reversal candle."
                     if self.up else
                     "In a downtrend (below EMA50, lower highs and lower lows), price retraces into the 50–61.8% "
                     "'golden zone' of the last swing high→low and prints a bearish reversal candle.")

    def parts(self, x):
        leg, f50, f618 = fib_zone(x, self.up)
        atr = x["atr14"]
        big = leg >= 2 * atr
        if self.up:
            trend = (x["close"] > x["ema50"]) & (x["sw_hi"] > x["sw_hi_prev"]) & (x["sw_lo"] > x["sw_lo_prev"])
            f786 = x["sw_hi"] - 0.786 * leg
            zone = big & (x["low"].rolling(3).min() <= f50) & (x["close"] > f786) & (x["close"] < x["sw_hi"])
            trig = x["bull_engulf"] | x["hammer"]
        else:
            trend = (x["close"] < x["ema50"]) & (x["sw_hi"] < x["sw_hi_prev"]) & (x["sw_lo"] < x["sw_lo_prev"])
            f786 = x["sw_lo"] + 0.786 * leg
            zone = big & (x["high"].rolling(3).max() >= f50) & (x["close"] < f786) & (x["close"] > x["sw_lo"])
            trig = x["bear_engulf"] | x["shooting_star"]
        return [trend, zone, trig]

    def describe(self, x, i):
        leg, f50, f618 = (float(s.iloc[i]) for s in fib_zone(x, self.up))
        hi, lo = _v(x["sw_hi"], i), _v(x["sw_lo"], i)
        if self.up:
            trend = (f"Close {_fmt(_v(x['close'], i))} vs EMA50 {_fmt(_v(x['ema50'], i))}; swing highs "
                     f"{_fmt(_v(x['sw_hi_prev'], i))}→{_fmt(hi)}, lows {_fmt(_v(x['sw_lo_prev'], i))}→{_fmt(lo)} "
                     "(need above EMA50 with higher highs and higher lows)")
            if leg == leg:
                zone = (f"Fib of swing {_fmt(lo)}→{_fmt(hi)}: 50% {_fmt(f50)}, 61.8% {_fmt(f618)}, 78.6% "
                        f"{_fmt(hi - 0.786 * leg)}; last 3 candles' low {_fmt(float(x['low'].rolling(3).min().iloc[i]))} "
                        "(need a dip into the golden zone that holds above 78.6%)")
            else:
                zone = "No completed swing low → swing high leg yet (need one to draw the Fibonacci)"
            pat = ("bullish engulfing" if bool(x["bull_engulf"].iloc[i]) else
                   "hammer / pin bar" if bool(x["hammer"].iloc[i]) else "no bullish pattern")
            return [trend, zone, f"Trigger candle: {pat} (need bullish engulfing or pin bar)"]
        trend = (f"Close {_fmt(_v(x['close'], i))} vs EMA50 {_fmt(_v(x['ema50'], i))}; swing highs "
                 f"{_fmt(_v(x['sw_hi_prev'], i))}→{_fmt(hi)}, lows {_fmt(_v(x['sw_lo_prev'], i))}→{_fmt(lo)} "
                 "(need below EMA50 with lower highs and lower lows)")
        if leg == leg:
            zone = (f"Fib of swing {_fmt(hi)}→{_fmt(lo)}: 50% {_fmt(f50)}, 61.8% {_fmt(f618)}, 78.6% "
                    f"{_fmt(lo + 0.786 * leg)}; last 3 candles' high {_fmt(float(x['high'].rolling(3).max().iloc[i]))} "
                    "(need a rally into the golden zone that holds below 78.6%)")
        else:
            zone = "No completed swing high → swing low leg yet (need one to draw the Fibonacci)"
        pat = ("bearish engulfing" if bool(x["bear_engulf"].iloc[i]) else
               "shooting star" if bool(x["shooting_star"].iloc[i]) else "no bearish pattern")
        return [trend, zone, f"Trigger candle: {pat} (need bearish engulfing or shooting star)"]

    def _leg(self, x, i):
        return float(fib_zone(x, self.up)[0].iloc[i])

    def stop(self, x, i, entry):
        leg, atr = self._leg(x, i), _v(x["atr14"], i)
        if leg != leg:
            return entry - 1.5 * atr if self.up else entry + 1.5 * atr
        if self.up:
            return _min_gap(_v(x["sw_hi"], i) - 0.786 * leg - 0.1 * atr, entry, atr, True)
        return _min_gap(_v(x["sw_lo"], i) + 0.786 * leg + 0.1 * atr, entry, atr, False)

    def targets(self, x, i, entry, stop):
        leg = self._leg(x, i)
        if leg != leg:
            return []
        if self.up:
            hi = _v(x["sw_hi"], i)
            return [("T1 swing high", hi), ("T2 127.2%", hi + 0.272 * leg), ("T3 161.8%", hi + 0.618 * leg)]
        lo = _v(x["sw_lo"], i)
        return [("T1 swing low", lo), ("T2 127.2%", lo - 0.272 * leg), ("T3 161.8%", lo - 0.618 * leg)]

    def target(self, x, i, entry, stop):
        t = self.targets(x, i, entry, stop)
        if len(t) >= 2:
            return t[1][1]          # 127.2% extension
        return super().target(x, i, entry, stop)


# ---------------------------------------------------------------- 9. break & retest

def _value_at_break(x: pd.DataFrame, col: str, since_col: str) -> pd.Series:
    """Value of `col` on the candle of the latest structure break."""
    n = len(x)
    since = x[since_col].to_numpy(dtype=float)
    vals = x[col].to_numpy(dtype=float)
    out = np.full(n, np.nan)
    ok = ~np.isnan(since)
    idx = np.arange(n)[ok] - since[ok].astype(int)
    out[ok] = vals[np.clip(idx, 0, n - 1)]
    return pd.Series(out, index=x.index)


class BreakRetest(Strategy):
    family, regime = "retest", "any"
    name = "Break & Retest"

    def __init__(self, side="BUY"):
        self.side = side
        self.key = "retest" if side == "BUY" else "retest_s"
        self.idea = ("A key level (swing high) breaks with strong volume; price comes back to retest it from above "
                     "and a rejection candle shows the old resistance now acts as support." if self.up else
                     "A key level (swing low) breaks with strong volume; price comes back to retest it from below "
                     "and a rejection candle shows the old support now acts as resistance.")

    def _lvl(self, x):
        return x["bull_lvl"] if self.up else x["bear_lvl"]

    def _since(self, x):
        return x["bars_since_bull"] if self.up else x["bars_since_bear"]

    def parts(self, x):
        lvl, since, atr = self._lvl(x), self._since(x), x["atr14"]
        strong = vol_ok(x, 1.3)
        if has_volume(x):
            strong = _value_at_break(x, "rvol", "bars_since_bull" if self.up else "bars_since_bear") >= 1.3
        structure_ok = (x["struct"] == (1 if self.up else -1))
        broke = structure_ok & since.between(2, 20) & strong
        rng = (x["high"] - x["low"]).replace(0, np.nan)
        if self.up:
            retest = (x["low"] <= lvl + 0.3 * atr) & (x["close"] > lvl)
            wick = (np.fmin(x["open"], x["close"]) - x["low"]) >= 0.5 * rng
            reject = x["bull_engulf"] | x["hammer"] | ((x["close"] > x["open"]) & wick)
        else:
            retest = (x["high"] >= lvl - 0.3 * atr) & (x["close"] < lvl)
            wick = (x["high"] - np.fmax(x["open"], x["close"])) >= 0.5 * rng
            reject = x["bear_engulf"] | x["shooting_star"] | ((x["close"] < x["open"]) & wick)
        return [broke, retest, reject]

    def describe(self, x, i):
        lvl, since = float(self._lvl(x).iloc[i]), self._since(x).iloc[i]
        word = "bullish" if self.up else "bearish"
        if lvl == lvl and since == since:
            vb = _value_at_break(x, "rvol", "bars_since_bull" if self.up else "bars_since_bear").iloc[i]
            vtxt = f", volume {vb:.1f}x" if has_volume(x) and vb == vb else ""
            s1 = (f"Last {word} break of {_fmt(lvl)} was {int(since)} candle(s) ago{vtxt} "
                  "(need a break 2–20 candles ago on 1.3x+ volume, structure still intact)")
        else:
            s1 = f"No recent {word} break of a swing level (need one 2–20 candles ago)"
        lv = _fmt(lvl) if lvl == lvl else "–"
        if self.up:
            s2 = (f"Low {_fmt(_v(x['low'], i))} vs broken level {lv} (need a dip back within 0.3 ATR and a close "
                  "above it)")
            s3 = "Rejection candle: " + ("yes" if bool(self.parts(x)[2].iloc[i]) else "no") + \
                 " (need hammer, bullish engulfing or a long lower wick)"
        else:
            s2 = (f"High {_fmt(_v(x['high'], i))} vs broken level {lv} (need a rally back within 0.3 ATR and a "
                  "close below it)")
            s3 = "Rejection candle: " + ("yes" if bool(self.parts(x)[2].iloc[i]) else "no") + \
                 " (need shooting star, bearish engulfing or a long upper wick)"
        return [s1, s2, s3]

    def stop(self, x, i, entry):
        lvl, atr = float(self._lvl(x).iloc[i]), _v(x["atr14"], i)
        if lvl != lvl:
            return entry - 1.5 * atr if self.up else entry + 1.5 * atr
        return _min_gap(lvl - 0.5 * atr if self.up else lvl + 0.5 * atr, entry, atr, self.up)

    def target(self, x, i, entry, stop):
        r2 = super().target(x, i, entry, stop)
        o = float(x["res" if self.up else "sup"].iloc[i])
        if o == o and ((o > r2) if self.up else (o < r2)):
            return o
        return r2


# ---------------------------------------------------------------- 10. RSI(2) mean reversion (Connors)

class Rsi2Reversion(Strategy):
    family, regime = "rsi2", "range"
    name = "RSI(2) Mean Reversion"
    max_bars = 7
    exit_note = "Exit when price closes back over SMA5 or RSI(2) recovers; time stop 7 candles."

    def __init__(self, side="BUY"):
        self.side = side
        self.key = "rsi2" if side == "BUY" else "rsi2_s"
        self.idea = ("Above the 200 SMA, a sharp 1–3 candle dip pushes RSI(2) under 10: buy the short-term "
                     "oversold dip and sell into the bounce (best on daily charts; avoid panic markets)."
                     if self.up else
                     "Below the 200 SMA, a sharp 1–3 candle rally pushes RSI(2) over 90: sell the short-term "
                     "overbought pop and cover into the drop (best on daily charts; avoid panic markets).")

    def _calm(self, x):
        cap = x["hv20"].rolling(250, min_periods=100).quantile(0.9)
        return (x["hv20"] <= cap) | cap.isna()

    def parts(self, x):
        if self.up:
            return [x["close"] > x["sma200"], x["rsi2"] < 10, self._calm(x)]
        return [x["close"] < x["sma200"], x["rsi2"] > 90, self._calm(x)]

    def describe(self, x, i):
        side = "above" if self.up else "below"
        calm = bool(self._calm(x).iloc[i])
        return [
            f"Close {_fmt(_v(x['close'], i))} vs SMA200 {_fmt(_v(x['sma200'], i))} (need {side}: long-term trend)",
            f"RSI(2) {_v(x['rsi2'], i):.1f} (need {'below 10 = oversold' if self.up else 'above 90 = overbought'})",
            f"Volatility {'normal' if calm else 'in the top 10% of the last year (panic)'} (need no panic/crash)",
        ]

    def stop(self, x, i, entry):
        d = 2 * _v(x["atr14"], i)
        return entry - d if self.up else entry + d

    def target(self, x, i, entry, stop):
        """Shown target: the 5 SMA (where the trade normally exits), at least 1R away."""
        s5 = _v(x["sma5"], i)
        one_r = entry + (entry - stop)
        if self.up:
            return max(s5, entry + 0.5 * (entry - stop)) if s5 > entry else one_r
        return min(s5, entry - 0.5 * (stop - entry)) if s5 < entry else one_r

    def exit_rule(self, x):
        if self.up:
            return (x["close"] > x["sma5"]) | (x["rsi2"] > 70)
        return (x["close"] < x["sma5"]) | (x["rsi2"] < 30)


# ---------------------------------------------------------------- 11. Bollinger band mean reversion

class BollingerReversion(Strategy):
    family, regime = "bb", "range"
    name = "Bollinger Band Mean Reversion"
    exit_note = "Target = middle band (20 SMA)."

    def __init__(self, side="BUY"):
        self.side = side
        self.key = "bb" if side == "BUY" else "bb_s"
        self.idea = ("In a sideways market (ADX below 20), a candle closes below the lower Bollinger band and the "
                     "next closes back inside: buy for a move back to the middle band." if self.up else
                     "In a sideways market (ADX below 20), a candle closes above the upper Bollinger band and the "
                     "next closes back inside: sell for a move back to the middle band.")

    def parts(self, x):
        if self.up:
            return [x["adx14"] < 20, x["close"].shift(1) < x["bb_lo"].shift(1), x["close"] > x["bb_lo"]]
        return [x["adx14"] < 20, x["close"].shift(1) > x["bb_up"].shift(1), x["close"] < x["bb_up"]]

    def describe(self, x, i):
        band = "bb_lo" if self.up else "bb_up"
        pc, pb = float(x["close"].shift(1).iloc[i]), float(x[band].shift(1).iloc[i])
        word = "lower" if self.up else "upper"
        return [
            f"ADX14 {_v(x['adx14'], i):.1f} (need below 20 = no trend, range-bound)",
            f"Previous close {_fmt(pc)} vs {word} band {_fmt(pb)} (need a close {'below' if self.up else 'above'} it)",
            f"Close {_fmt(_v(x['close'], i))} vs {word} band {_fmt(_v(x[band], i))} (need back inside the bands)",
        ]

    def stop(self, x, i, entry):
        atr = _v(x["atr14"], i)
        st = _v(x["ll5"], i) - 0.2 * atr if self.up else _v(x["hh5"], i) + 0.2 * atr
        return _min_gap(st, entry, atr, self.up)

    def target(self, x, i, entry, stop):
        return _v(x["bb_mid"], i)


# ---------------------------------------------------------------- 12. VWAP pullback (intraday)

def _intraday_only(x) -> str | None:
    if x.attrs.get("tf") not in INTRADAY_TFS:
        return "intraday rule (use 5m–1h)"
    return None


def _nse_only(x) -> str | None:
    if x.attrs.get("venue") not in ("nse", "nse_index"):
        return "NSE stocks/indices only"
    if x.attrs.get("tf") not in ("5m", "15m"):
        return "use the 5m or 15m chart"
    return None


class VwapPullback(Strategy):
    family, regime = "vwap", "trend"
    name = "VWAP Pullback (Intraday)"
    exit_note = "Target = day high / 2nd VWAP band; close by the end of the session."

    def __init__(self, side="BUY"):
        self.side = side
        self.key = "vwap" if side == "BUY" else "vwap_s"
        self.idea = ("On a bullish day price holds above VWAP; buy a pullback to VWAP or its first band when a "
                     "bullish candle closes above the 9 EMA." if self.up else
                     "On a bearish day price stays below VWAP; sell a rally to VWAP or its first band when a "
                     "bearish candle closes below the 9 EMA.")

    def applies(self, x):
        return _intraday_only(x) or (None if x["vwap"].notna().any() else "needs intraday candles")

    def parts(self, x):
        vw, sd = x["vwap"], x["vwap_sd"].fillna(0)
        if self.up:
            day = (x["close"] > vw) & (x["ema9"] > vw)
            pull = (x["low"].rolling(2).min() <= vw + sd) & (x["low"].rolling(2).min() >= vw * 0.997)
            trig = (x["close"] > x["open"]) & (x["close"] > x["ema9"])
        else:
            day = (x["close"] < vw) & (x["ema9"] < vw)
            pull = (x["high"].rolling(2).max() >= vw - sd) & (x["high"].rolling(2).max() <= vw * 1.003)
            trig = (x["close"] < x["open"]) & (x["close"] < x["ema9"])
        return [day, pull, trig]

    def describe(self, x, i):
        vw, sd = _v(x["vwap"], i), _v(x["vwap_sd"], i)
        side = "above" if self.up else "below"
        note = "" if x.attrs.get("vwap_volume", True) else " (no volume data: time-weighted)"
        if self.up:
            ext = float(x["low"].rolling(2).min().iloc[i])
            s2 = (f"Pullback low {_fmt(ext)} vs VWAP {_fmt(vw)} / +1σ {_fmt(vw + sd)} "
                  "(need a dip to VWAP–1st band, not more than 0.3% below VWAP)")
        else:
            ext = float(x["high"].rolling(2).max().iloc[i])
            s2 = (f"Pullback high {_fmt(ext)} vs VWAP {_fmt(vw)} / −1σ {_fmt(vw - sd)} "
                  "(need a rally to VWAP–1st band, not more than 0.3% above VWAP)")
        return [
            f"Close {_fmt(_v(x['close'], i))} and EMA9 {_fmt(_v(x['ema9'], i))} vs VWAP {_fmt(vw)}{note} "
            f"(need both {side} = {'bullish' if self.up else 'bearish'} day)",
            s2,
            f"Candle close vs EMA9 (need a {'bullish' if self.up else 'bearish'} candle closing {side} EMA9)",
        ]

    def stop(self, x, i, entry):
        vw, atr = _v(x["vwap"], i), _v(x["atr14"], i)
        if self.up:
            st = min(vw * 0.9975, _v(x["low"], i)) - 0.05 * atr
        else:
            st = max(vw * 1.0025, _v(x["high"], i)) + 0.05 * atr
        return _min_gap(st, entry, atr, self.up, 0.3)

    def target(self, x, i, entry, stop):
        vw, sd = _v(x["vwap"], i), _v(x["vwap_sd"], i)
        r1 = entry + (entry - stop)
        if self.up:
            t = max(_v(x["day_high_sofar"], i), vw + 2 * sd)
            return t if t > r1 else entry + 2 * (entry - stop)
        t = min(_v(x["day_low_sofar"], i), vw - 2 * sd)
        return t if t < r1 else entry - 2 * (stop - entry)

    def exit_rule(self, x):
        return _session_end(x)


def _session_end(x: pd.DataFrame) -> pd.Series:
    """Last candle of each trading day (intraday trades are closed before the session ends)."""
    from bot.indicators import session_keys

    day = session_keys(x.index, x.attrs.get("tz", "UTC"))
    return day != day.shift(-1)


# ---------------------------------------------------------------- 13. opening range breakout

class OpeningRangeBreakout(Strategy):
    family, regime = "orb", "trend"
    name = "Opening Range Breakout (ORB)"
    exit_note = "Stop at the range midpoint; target 2R; close by the end of the session."

    def __init__(self, side="BUY"):
        self.side = side
        self.key = "orb" if side == "BUY" else "orb_s"
        self.idea = ("Mark the high/low of the first 15 minutes (09:15–09:30 IST); buy the first 5m/15m close "
                     "above the range high on above-average volume, in the direction of the 15-minute 20 EMA."
                     if self.up else
                     "Mark the high/low of the first 15 minutes (09:15–09:30 IST); sell the first 5m/15m close "
                     "below the range low on above-average volume, in the direction of the 15-minute 20 EMA.")

    def applies(self, x):
        return _nse_only(x)

    def _ema15(self, x):
        span = max(int(round(20 * 15 / max(bar_minutes(x), 1))), 2)
        return x["close"].ewm(span=span, adjust=False).mean()

    def parts(self, x):
        if self.up:
            brk = (x["close"] > x["or_high"]) & (x["close"].shift(1) <= x["or_high"].shift(1).fillna(np.inf))
            trend = x["close"] > self._ema15(x)
        else:
            brk = (x["close"] < x["or_low"]) & (x["close"].shift(1) >= x["or_low"].shift(1).fillna(-np.inf))
            trend = x["close"] < self._ema15(x)
        return [brk, vol_ok(x, 1.0), trend]

    def describe(self, x, i):
        hi, lo = _v(x["or_high"], i), _v(x["or_low"], i)
        rng = f"{_fmt(lo)}–{_fmt(hi)}" if hi == hi else "not formed yet (first 15 minutes)"
        side = "above the range high" if self.up else "below the range low"
        return [
            f"Opening range {rng}; close {_fmt(_v(x['close'], i))} (need the first close {side})",
            vol_txt(x, i, 1.0),
            f"15-minute EMA20 {_fmt(float(self._ema15(x).iloc[i]))} (need close {'above' if self.up else 'below'})",
        ]

    def stop(self, x, i, entry):
        hi, lo, atr = _v(x["or_high"], i), _v(x["or_low"], i), _v(x["atr14"], i)
        mid = (hi + lo) / 2 if hi == hi else (entry - atr if self.up else entry + atr)
        return _min_gap(mid, entry, atr, self.up, 0.3)

    def exit_rule(self, x):
        return _session_end(x)


# ---------------------------------------------------------------- 14. gap fill

class GapFill(Strategy):
    family, regime = "gap", "range"
    name = "Gap Fill (Intraday)"
    exit_note = "Target = yesterday's close; stop = first-15-minute high/low; close by the session end."

    def __init__(self, side="BUY"):
        self.side = side
        self.key = "gap" if side == "BUY" else "gap_s"
        self.idea = ("A small gap DOWN (0.3%–1%) on no major news often fills: after the first 15 minutes, buy "
                     "a bullish candle above VWAP for a move back to yesterday's close." if self.up else
                     "A small gap UP (0.3%–1%) on no major news often fills: after the first 15 minutes, sell "
                     "a bearish candle below VWAP for a move back to yesterday's close.")

    def applies(self, x):
        return _nse_only(x)

    def parts(self, x):
        g = x["gap_pct"]
        after = x["mins_in_session"] >= 15
        if self.up:
            gap = g.between(-1.0, -0.3)
            unfilled = after & (x["close"] < x["prev_day_close"])
            trig = (x["close"] > x["open"]) & (x["close"] > x["vwap"])
        else:
            gap = g.between(0.3, 1.0)
            unfilled = after & (x["close"] > x["prev_day_close"])
            trig = (x["close"] < x["open"]) & (x["close"] < x["vwap"])
        return [gap, unfilled, trig]

    def describe(self, x, i):
        g, pc = _v(x["gap_pct"], i), _v(x["prev_day_close"], i)
        word = "down" if self.up else "up"
        return [
            f"Today's gap {g:+.2f}% (need a small gap {word}: 0.3%–1%; check there is no major news)",
            f"Minutes since open {int(_v(x['mins_in_session'], i)) if _ok(_v(x['mins_in_session'], i)) else 0}, "
            f"close {_fmt(_v(x['close'], i))} vs yesterday's close {_fmt(pc)} (need 15+ minutes and the gap still open)",
            f"Trigger: {'bullish' if self.up else 'bearish'} candle {'above' if self.up else 'below'} VWAP "
            f"{_fmt(_v(x['vwap'], i))}",
        ]

    def stop(self, x, i, entry):
        atr = _v(x["atr14"], i)
        if self.up:
            return _min_gap(_v(x["or_low"], i) - 0.05 * atr, entry, atr, True, 0.3)
        return _min_gap(_v(x["or_high"], i) + 0.05 * atr, entry, atr, False, 0.3)

    def target(self, x, i, entry, stop):
        return _v(x["prev_day_close"], i)

    def exit_rule(self, x):
        return _session_end(x)


# ---------------------------------------------------------------- registry

CLASSES = [MaCrossover, Breakout20, EmaPullback, MacdMomentum, ZoneConfluence, SmartMoneyRetest,
           LevelBreakout, FibPullback, BreakRetest, Rsi2Reversion, BollingerReversion, VwapPullback,
           OpeningRangeBreakout, GapFill]
LONGS: list[Strategy] = [c("BUY") for c in CLASSES]
SHORTS: list[Strategy] = [c("SELL") for c in CLASSES]
ALL: list[Strategy] = LONGS + SHORTS
# Display names for each BUY/SELL pair, in order (the serial numbers used everywhere)
FAMILIES: list[tuple[str, str]] = [
    ("ma", "MA Crossover"), ("breakout", "20-Candle Breakout"), ("pullback", "EMA Pullback 20/50/200"),
    ("macd", "MACD Momentum"), ("zone", "Demand/Supply + Confluence"), ("smc", "Smart Money BOS + OB/FVG"),
    ("levelbreak", "Trendline / Round Number"), ("fib", "Fibonacci Golden Zone"), ("retest", "Break & Retest"),
    ("rsi2", "RSI(2) Mean Reversion"), ("bb", "Bollinger Mean Reversion"), ("vwap", "VWAP Pullback"),
    ("orb", "Opening Range Breakout"), ("gap", "Gap Fill"),
]
N_STRATEGIES = len(FAMILIES)
TREND_FAMILIES = {"ma", "breakout", "pullback", "macd", "levelbreak", "fib", "vwap", "orb"}
RANGE_FAMILIES = {"rsi2", "bb", "gap"}


def prepare(candles: pd.DataFrame, tf: str | None = None, tz: str = "UTC", venue: str = "") -> tuple:
    """Indicators + market structure. Returns (x, snapshot). attrs carry tf/tz/venue for the rules."""
    from bot import structure
    from bot.indicators import add_all

    tf = tf or candles.attrs.get("tf") or "1d"
    attrs = dict(candles.attrs)
    x = add_all(candles, tz=tz, intraday=tf in INTRADAY_TFS)
    x, snap = structure.analyze(x)
    x.attrs.update(attrs)
    x.attrs.update(tf=tf, tz=tz, venue=venue or attrs.get("venue", ""))
    v = x["volume"].tail(200)
    x.attrs["has_volume"] = bool(len(v) and (v > 0).mean() > 0.8)
    return x, snap


def regime(x: pd.DataFrame) -> tuple[str, float | None]:
    """'trending' (ADX >= 25), 'weak trend' (20-25) or 'ranging' (< 20)."""
    a = float(x["adx14"].iloc[-1]) if "adx14" in x and len(x) else float("nan")
    if a != a:
        return "unknown", None
    return ("trending" if a >= 25 else "weak trend" if a >= 20 else "ranging"), a


@dataclass
class Setup:
    key: str
    name: str
    idea: str
    status: str                         # TRADE / WATCH / NO / NA
    checks: list = field(default_factory=list)  # [(ok: bool, text: str)]
    entry: float | None = None
    stop: float | None = None
    target: float | None = None
    fresh: bool = False
    backtest: dict = field(default_factory=dict)
    side: str = "BUY"
    family: str = ""
    targets: list = field(default_factory=list)  # extra targets [(label, price)]
    exit_note: str = ""
    na_reason: str = ""

    @property
    def passed(self) -> int:
        return sum(ok for ok, _ in self.checks)

    @property
    def rr(self) -> float | None:
        if self.entry is None or self.stop is None or self.target is None or self.entry == self.stop:
            return None
        return abs(self.target - self.entry) / abs(self.entry - self.stop)


def evaluate(strategy: Strategy, x: pd.DataFrame) -> Setup:
    family = strategy.family or strategy.key
    reason = strategy.applies(x)
    if reason:
        return Setup(strategy.key, strategy.name, strategy.idea, "NA", side=strategy.side, family=family,
                     na_reason=reason, exit_note=strategy.exit_note)
    oks = [bool(p.fillna(False).astype(bool).iloc[-1]) for p in strategy.parts(x)]
    texts = strategy.describe(x, -1)
    passed = sum(oks)
    status = "TRADE" if passed == len(oks) else ("WATCH" if passed == len(oks) - 1 else "NO")
    setup = Setup(strategy.key, strategy.name, strategy.idea, status, list(zip(oks, texts)),
                  side=strategy.side, family=family, exit_note=strategy.exit_note)
    if status in ("TRADE", "WATCH"):
        entry = _v(x["close"], -1)
        try:
            stop = strategy.stop(x, -1, entry)
            target = strategy.target(x, -1, entry, stop)
        except (ValueError, TypeError):
            stop = target = float("nan")
        ok = (stop < entry if strategy.up else stop > entry) and stop == stop
        t_ok = target is not None and target == target and ((target > entry) if strategy.up else (target < entry))
        if ok and t_ok:
            setup.entry, setup.stop, setup.target = entry, stop, target
            setup.targets = strategy.targets(x, -1, entry, stop)
        elif status == "TRADE":
            setup.status = "WATCH"   # levels don't make sense (e.g. target already reached)
    setup.fresh = setup.status == "TRADE" and bool(strategy.signal(x).iloc[-1])
    return setup


def by_key(key: str) -> Strategy | None:
    return next((s for s in ALL if s.key == key), None)

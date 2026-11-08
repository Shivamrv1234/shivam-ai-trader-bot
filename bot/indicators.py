"""Deterministic indicators (pure pandas). Input columns: open, high, low, close, volume.

Everything here is causal: the value on a candle uses only that candle and earlier ones.
Session values (VWAP, opening range, gaps) reset at the start of each trading day in the
market's own time zone (IST for NSE/MCX, UTC for crypto and forex).
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    """Wilder's RSI: 100 with no losses, 50 when flat."""
    d = close.diff()
    gain = d.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    loss = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    out = 100 - 100 / (1 + gain / loss)
    out = out.where(loss != 0, 100.0)
    out = out.where(~((gain == 0) & (loss == 0)), 50.0)
    return out.where(gain.notna())


def true_range(df: pd.DataFrame) -> pd.Series:
    prev = df["close"].shift(1)
    return pd.concat([df["high"] - df["low"], (df["high"] - prev).abs(), (df["low"] - prev).abs()],
                     axis=1).max(axis=1)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    return true_range(df).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def adx(df: pd.DataFrame, n: int = 14) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Wilder's ADX with +DI / -DI. ADX above 20-25 = trending, below 20 = ranging."""
    up = df["high"].diff()
    down = -df["low"].diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=df.index)
    tr = true_range(df).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1 / n, adjust=False, min_periods=n).mean() / tr
    minus_di = 100 * minus_dm.ewm(alpha=1 / n, adjust=False, min_periods=n).mean() / tr
    dx = (100 * (plus_di - minus_di).abs() / (plus_di + minus_di)).replace([np.inf, -np.inf], np.nan)
    return dx.ewm(alpha=1 / n, adjust=False, min_periods=n).mean(), plus_di, minus_di


def session_keys(index: pd.DatetimeIndex, tz: str) -> pd.Series:
    """Trading-day label of every candle in the market's time zone."""
    return pd.Series(index.tz_convert(tz).date, index=index)


def add_session(x: pd.DataFrame, tz: str, intraday: bool, or_minutes: int = 15) -> pd.DataFrame:
    """VWAP (+/- 1 and 2 standard deviations), today's open, yesterday's close, opening range.
    Only meaningful on intraday candles; on daily candles the columns are NaN."""
    nan = pd.Series(np.nan, index=x.index)
    for k in ("vwap", "vwap_sd", "day_open", "prev_day_close", "or_high", "or_low", "mins_in_session",
              "day_high_sofar", "day_low_sofar", "gap_pct"):
        x[k] = nan
    if not intraday or not len(x):
        return x
    day = session_keys(x.index, tz)
    tp = (x["high"] + x["low"] + x["close"]) / 3
    vol = x["volume"].where(x["volume"] > 0, 0.0)
    has_vol = bool((vol > 0).mean() > 0.5)
    w = vol if has_vol else pd.Series(1.0, index=x.index)   # forex from Yahoo has no volume: equal weights
    cum_w = w.groupby(day).cumsum()
    cum_pv = (tp * w).groupby(day).cumsum()
    cum_p2v = (tp * tp * w).groupby(day).cumsum()
    vwap = cum_pv / cum_w.replace(0, np.nan)
    var = (cum_p2v / cum_w.replace(0, np.nan) - vwap ** 2).clip(lower=0)
    x["vwap"], x["vwap_sd"] = vwap, np.sqrt(var)
    x.attrs["vwap_volume"] = has_vol
    first = x.groupby(day)["open"].transform("first")
    x["day_open"] = first
    last_close = x.groupby(day)["close"].last()
    prev_close = last_close.shift(1)
    x["prev_day_close"] = day.map(prev_close)
    local = x.index.tz_convert(tz)
    start = pd.Series(local, index=x.index).groupby(day).transform("min")
    mins = (pd.Series(local, index=x.index) - start).dt.total_seconds() / 60
    x["mins_in_session"] = mins
    in_or = mins < or_minutes
    or_hi = x["high"].where(in_or).groupby(day).transform("max")
    or_lo = x["low"].where(in_or).groupby(day).transform("min")
    # the opening range is known only once its candles have closed
    bar_minutes = _bar_minutes(x.index)
    known = mins + bar_minutes >= or_minutes
    x["or_high"], x["or_low"] = or_hi.where(known & ~in_or), or_lo.where(known & ~in_or)
    x["day_high_sofar"] = x["high"].groupby(day).cummax()
    x["day_low_sofar"] = x["low"].groupby(day).cummin()
    x["gap_pct"] = (first - x["prev_day_close"]) / x["prev_day_close"] * 100
    return x


def _bar_minutes(index: pd.DatetimeIndex) -> float:
    if len(index) < 3:
        return 0.0
    return float(pd.Series(index).diff().dt.total_seconds().median() / 60)


def add_all(df: pd.DataFrame, tz: str = "UTC", intraday: bool = False) -> pd.DataFrame:
    x = df.copy()
    for n in (9, 20, 21, 50, 200):
        x[f"ema{n}"] = ema(x["close"], n)
    for n in (5, 20, 50, 200):
        x[f"sma{n}"] = sma(x["close"], n)
    x["rsi14"] = rsi(x["close"], 14)
    x["rsi2"] = rsi(x["close"], 2)
    x["atr14"] = atr(x, 14)
    x["adx14"], x["pdi14"], x["mdi14"] = adx(x, 14)
    x["vol_avg20"] = x["volume"].rolling(20).mean()
    x["rvol"] = (x["volume"] / x["vol_avg20"]).replace([np.inf, -np.inf], np.nan)
    x["hh20_prev"] = x["high"].rolling(20).max().shift(1)
    x["ll5"] = x["low"].rolling(5).min()
    x["hh5"] = x["high"].rolling(5).max()
    x["ll20_prev"] = x["low"].rolling(20).min().shift(1)
    macd = ema(x["close"], 12) - ema(x["close"], 26)
    x["macd"] = macd
    x["macd_signal"] = ema(macd, 9)
    x["macd_hist"] = macd - x["macd_signal"]
    sd = x["close"].rolling(20).std(ddof=0)
    x["bb_mid"] = x["sma20"]
    x["bb_up"] = x["sma20"] + 2 * sd
    x["bb_lo"] = x["sma20"] - 2 * sd
    ret = np.log(x["close"]).diff()
    x["hv20"] = ret.rolling(20).std(ddof=0)   # per candle; annualised where shown
    return add_session(x, tz, intraday)

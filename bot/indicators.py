"""Deterministic indicators (pure pandas). Input columns: open, high, low, close, volume."""
from __future__ import annotations

import pandas as pd


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    """Wilder's RSI: 100 with no losses, 50 when flat."""
    d = close.diff()
    gain = d.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    loss = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    out = 100 - 100 / (1 + gain / loss)
    out = out.where(loss != 0, 100.0)
    out = out.where(~((gain == 0) & (loss == 0)), 50.0)
    return out.where(gain.notna())


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    prev = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - prev).abs(), (df["low"] - prev).abs()],
                   axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def add_all(df: pd.DataFrame) -> pd.DataFrame:
    x = df.copy()
    for n in (9, 21, 50, 200):
        x[f"ema{n}"] = ema(x["close"], n)
    x["rsi14"] = rsi(x["close"], 14)
    x["atr14"] = atr(x, 14)
    x["vol_avg20"] = x["volume"].rolling(20).mean()
    x["rvol"] = x["volume"] / x["vol_avg20"]
    x["hh20_prev"] = x["high"].rolling(20).max().shift(1)
    x["ll5"] = x["low"].rolling(5).min()
    x["hh5"] = x["high"].rolling(5).max()
    x["ll20_prev"] = x["low"].rolling(20).min().shift(1)
    macd = ema(x["close"], 12) - ema(x["close"], 26)
    x["macd"] = macd
    x["macd_signal"] = ema(macd, 9)
    x["macd_hist"] = macd - x["macd_signal"]
    return x

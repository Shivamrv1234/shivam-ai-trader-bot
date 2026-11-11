"""Market data from free public endpoints. Candle functions return a DataFrame
(UTC index, oldest first) with open/high/low/close/volume of COMPLETED candles only;
the live (still forming) price is returned separately in df.attrs["live_price"].

Crypto: Binance public market-data API (data-api.binance.vision, live). Kraken is the fallback.
NSE stocks and indices: Angel One SmartAPI (official, live) when its secrets are set, otherwise
Yahoo (unofficial, delayed). Timeframes: 5m, 15m, 30m, 1h, 4h, 1d. Angel One and Yahoo have no
4h candles for NSE, so 4h is built from 1h candles: 09:15-13:15 and 13:15-15:30 IST.
Forex and commodities live in bot/fx.py.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from bot.config import cfg

log = logging.getLogger(__name__)
COLS = ["open", "high", "low", "close", "volume"]
BINANCE = "https://data-api.binance.vision/api/v3"
KRAKEN = "https://api.kraken.com/0/public"
IST = ZoneInfo("Asia/Kolkata")
TIMEFRAMES = ["5m", "15m", "30m", "1h", "4h", "1d"]
TF_SECONDS = {"5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "1d": 86400}
KRAKEN_MINUTES = {"5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}
TF_LABEL = {"5m": "5 min", "15m": "15 min", "30m": "30 min", "1h": "1 hour", "4h": "4 hours", "1d": "1 day"}
# Trading style for each timeframe (shown on the buttons and the card)
TF_MODE = {"5m": "⚡ Scalp", "15m": "⚡ Scalp", "30m": "☀️ Intraday", "1h": "☀️ Intraday",
           "4h": "🌊 Swing", "1d": "🌊 Swing"}
CRYPTO_LIMIT = {"5m": 1500, "15m": 1500, "30m": 1500, "1h": 2000, "4h": 3000, "1d": 1500}
# Angel One interval and how many calendar days to request (inside Angel One's per-call limits)
ANGEL_TF = {"5m": ("FIVE_MINUTE", 25), "15m": ("FIFTEEN_MINUTE", 45), "30m": ("THIRTY_MINUTE", 80),
            "1h": ("ONE_HOUR", 120), "4h": ("ONE_HOUR", 380), "1d": ("ONE_DAY", 700)}
YAHOO_TF = {"5m": ("5m", "30d"), "15m": ("15m", "60d"), "30m": ("30m", "60d"), "1h": ("1h", "180d"),
            "4h": ("1h", "730d"), "1d": ("1d", "2y")}


class DataError(RuntimeError):
    pass


def norm_tf(tf: str | None, default: str = "1d") -> str:
    t = (tf or default).lower().replace("min", "m").replace("hr", "h").replace("day", "d")
    t = {"1": "1m", "60m": "1h", "240m": "4h", "d": "1d", "1D": "1d", "24h": "1d"}.get(t, t)
    return t if t in TF_SECONDS else default


_HTTP = requests.Session()     # kept-open connections to the data servers (saves a TLS handshake per request)


def _get(url: str, params: dict | None = None, tries: int = 3, headers: dict | None = None) -> object:
    last = None
    for attempt in range(tries):
        try:
            r = _HTTP.get(url, params=params, timeout=20, headers=headers)
            if r.status_code == 200:
                return r.json()
            last = f"HTTP {r.status_code}"
            if r.status_code in (400, 401, 403, 404, 451):
                break
        except (requests.RequestException, ValueError) as exc:
            last = exc.__class__.__name__
        time.sleep(1.5 * (attempt + 1))
    raise DataError(f"{url.split('/')[2]}: {last}")


def _frame(rows: list, ts_unit: str) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=["ts", *COLS])
    df.index = pd.to_datetime(df.pop("ts").astype("int64"), unit=ts_unit, utc=True)
    df = df.astype(float)
    return df[~df.index.duplicated(keep="last")].sort_index()


def drop_forming(df: pd.DataFrame, tf: str, session_close: tuple[int, int] | None = None,
                 tz: str = "UTC", now: datetime | None = None) -> pd.DataFrame:
    """Strategies use completed candles only. A candle is complete when its time is up or, for markets
    with a closing bell (NSE 15:30 IST), when the session has closed."""
    if not len(df):
        return df
    now = now or datetime.now(timezone.utc)
    start = df.index[-1].to_pydatetime()
    end = start.timestamp() + TF_SECONDS[tf]
    done = end <= now.timestamp()
    if not done and session_close is not None:
        loc_now, loc_start = now.astimezone(ZoneInfo(tz)), start.astimezone(ZoneInfo(tz))
        closed = loc_now.date() > loc_start.date() or (loc_now.hour, loc_now.minute) >= session_close
        done = closed
    if done:
        return df
    attrs = dict(df.attrs)
    out = df.iloc[:-1].copy()
    out.attrs.update(attrs)
    out.attrs.setdefault("forming_close", float(df["close"].iloc[-1]))
    return out


# ---------------------------------------------------------------- crypto

def norm_crypto(symbol: str) -> str:
    s = symbol.upper().replace("/", "").replace("-", "").strip()
    return s if s.endswith("USDT") else s + "USDT"


def crypto_candles(symbol: str, timeframe: str | None = None, limit: int | None = None) -> pd.DataFrame:
    """Up to `limit` candles (Binance returns 1000 per request, so this pages backwards)."""
    tf = norm_tf(timeframe, cfg()["crypto"]["timeframe"])
    limit = limit or CRYPTO_LIMIT[tf]
    sym = norm_crypto(symbol)
    try:
        rows: list = []
        end_time = None
        while len(rows) < limit:
            params = {"symbol": sym, "interval": tf, "limit": min(1000, limit - len(rows))}
            if end_time is not None:
                params["endTime"] = end_time
            batch = _get(f"{BINANCE}/klines", params)
            if not batch:
                break
            rows = [r[:6] for r in batch] + rows
            end_time = int(batch[0][0]) - 1
            if len(batch) < params["limit"]:
                break  # reached the coin's listing date
        df = _frame(rows, "ms")
        df.attrs["source"] = "Binance (live)"
    except DataError as first:
        log.warning("Binance %s failed (%s); trying Kraken", sym, first)
        df = _kraken(sym, tf)
        df.attrs["source"] = "Kraken (live)"
    if df.empty:
        raise DataError(f"No candles for {sym}")
    last_close = float(df["close"].iloc[-1])
    df = drop_forming(df, tf)
    df.attrs.update(tf=tf, tz="UTC", venue="binance", currency="USDT")
    df.attrs.setdefault("live_price", crypto_price(sym) or last_close)
    return df


def crypto_price(sym: str) -> float | None:
    try:
        return float(_get(f"{BINANCE}/ticker/price", {"symbol": norm_crypto(sym)}, tries=2)["price"])
    except (DataError, KeyError, TypeError, ValueError):
        return None


def _kraken(sym: str, tf: str) -> pd.DataFrame:
    base = sym[:-4]
    base = {"BTC": "XBT"}.get(base, base)
    data = _get(f"{KRAKEN}/OHLC", {"pair": f"{base}USDT", "interval": KRAKEN_MINUTES[tf]})
    if data.get("error"):
        data = _get(f"{KRAKEN}/OHLC", {"pair": f"{base}USD", "interval": KRAKEN_MINUTES[tf]})
    result = data.get("result") or {}
    key = next((k for k in result if k != "last"), None)
    if data.get("error") or not key:
        raise DataError(f"{sym}: not available on Binance public data or Kraken")
    rows = [[r[0], r[1], r[2], r[3], r[4], r[6]] for r in result[key]]
    return _frame(rows, "s")


def top_crypto_by_volume(n: int) -> list[dict]:
    """Most traded USDT spot pairs over 24h (stablecoins and leveraged tokens removed)."""
    c = cfg()["crypto"]
    excluded = set(c.get("exclude", []))
    try:
        tickers = _get(f"{BINANCE}/ticker/24hr")
    except DataError as exc:
        log.warning("24h tickers unavailable (%s); using always_include list", exc)
        return [{"symbol": s, "change_pct": None, "quote_volume": None, "price": None} for s in c["always_include"]]
    rows = []
    for t in tickers:
        s = t.get("symbol", "")
        if not s.endswith("USDT") or s in excluded:
            continue
        base = s[:-4]
        if len(base) >= 5 and base.endswith(("UP", "DOWN", "BULL", "BEAR")):
            continue  # old leveraged tokens such as BTCUP / ETHDOWN
        try:
            qv = float(t["quoteVolume"])
            if qv <= 0 or int(t.get("count", 0)) == 0:
                continue
            rows.append({"symbol": s, "change_pct": float(t["priceChangePercent"]), "quote_volume": qv,
                         "price": float(t.get("lastPrice") or 0) or None})
        except (KeyError, ValueError):
            continue
    rows.sort(key=lambda r: r["quote_volume"], reverse=True)
    picked = rows[:n]
    have = {r["symbol"] for r in picked}
    by_sym = {r["symbol"]: r for r in rows}
    for s in c["always_include"]:
        if s not in have:
            picked.append(by_sym.get(s, {"symbol": s, "change_pct": None, "quote_volume": None, "price": None}))
    return picked


# ---------------------------------------------------------------- NSE

def resample_nse_4h(df: pd.DataFrame) -> pd.DataFrame:
    """1h NSE candles -> two candles a day: 09:15-13:15 and 13:15-15:30 IST."""
    if df.empty:
        return df
    local = df.index.tz_convert(IST)
    half = [(t.hour * 60 + t.minute) >= 13 * 60 + 15 for t in local]
    key = pd.Series([f"{t.date()}-{int(h)}" for t, h in zip(local, half)], index=df.index)
    g = df.groupby(key, sort=False)
    out = pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(),
                        "close": g["close"].last(), "volume": g["volume"].sum()})
    out.index = pd.DatetimeIndex([df.index[key == k][0] for k in out.index])
    out.attrs.update(df.attrs)
    return out.sort_index()


def stock_candles(symbol: str, timeframe: str = "1d", live: bool = True) -> pd.DataFrame:
    """NSE candles (stocks or indices such as NIFTY / BANKNIFTY) for any timeframe.
    Angel One (official, live) when its secrets are set; otherwise Yahoo (delayed).
    df.attrs carries 'source', 'live_price' and 'live_change_pct'."""
    from bot import angel

    tf = norm_tf(timeframe, "1d")
    sym = symbol.upper().removesuffix(".NS")
    venue = "nse_index" if sym in angel.INDEX_ALIASES else "nse"
    if angel.configured():
        interval, days = ANGEL_TF[tf]
        try:
            a = angel.session()
            hist = a.spot_candles(sym, interval, days)
            q = a.ltp(sym) if live else {}   # scanners fetch all live prices in one request instead
        except angel.AngelError as exc:
            # Angel One busy / down: fall back to Yahoo (delayed) rather than show an error.
            try:
                hist = _yahoo_nse(sym, tf, venue)
            except DataError:
                raise DataError(f"Angel One {sym}: {exc}") from None
            why = "Angel One busy" if "busy" in str(exc) else "Angel One unavailable"
            hist.attrs["source"] = f"Yahoo (delayed ~15 min — {why})"
            return hist
        if tf == "4h":
            hist = resample_nse_4h(hist)
        hist = drop_forming(hist, tf, (15, 30), "Asia/Kolkata")
        hist.attrs.update(source="Angel One (live)",
                          live_price=angel.num(angel.pick(q, "ltp")) or None,
                          live_change_pct=angel.num(angel.pick(q, "percentChange")),
                          tf=tf, tz="Asia/Kolkata", venue=venue, currency="INR")
        return hist
    return _yahoo_nse(sym, tf, venue)


def _yahoo_nse(sym: str, tf: str, venue: str) -> pd.DataFrame:
    from bot import angel

    ticker = angel.YAHOO_INDEX.get(sym, f"{sym}.NS")
    hist = yahoo_candles(ticker, tf, sym)
    hist = drop_forming(hist, tf, (15, 30), "Asia/Kolkata")
    hist.attrs.update(source="Yahoo (delayed ~15 min)", tf=tf, tz="Asia/Kolkata", venue=venue, currency="INR")
    return hist


def yahoo_candles(ticker: str, tf: str, label: str | None = None) -> pd.DataFrame:
    import yfinance as yf

    interval, period = YAHOO_TF[tf]
    try:
        hist = yf.Ticker(ticker).history(period=period, interval=interval, auto_adjust=False)
    except Exception as exc:
        raise DataError(f"Yahoo {label or ticker}: {exc.__class__.__name__}") from None
    if hist is None or hist.empty:
        raise DataError(f"Yahoo {label or ticker}: no data (check the symbol)")
    hist = hist.rename(columns=str.lower)[COLS].astype(float)
    hist.index = pd.to_datetime(hist.index, utc=True)
    hist = hist.dropna()
    live = float(hist["close"].iloc[-1])
    if tf == "4h":
        hist = resample_nse_4h(hist) if ticker.endswith((".NS",)) or ticker.startswith("^NSE") \
            else hist.resample("4h", origin="epoch").agg({"open": "first", "high": "max", "low": "min",
                                                          "close": "last", "volume": "sum"}).dropna()
    hist.attrs["live_price"] = live
    return hist


def _drop_forming_day(df: pd.DataFrame) -> pd.DataFrame:
    """Kept for older callers: daily NSE candle is complete after 15:30 IST."""
    return drop_forming(df, "1d", (15, 30), "Asia/Kolkata")


# ---------------------------------------------------------------- live volume + order book

def crypto_orderflow(symbol: str, avg_volume: float | None = None, timeframe: str | None = None) -> dict | None:
    """Live order book (top 500 levels) and the volume of the candle forming right now.
    Returns None if the data can't be fetched (the analysis still works without it)."""
    sym = norm_crypto(symbol)
    tf = norm_tf(timeframe, cfg()["crypto"]["timeframe"])
    try:
        book = _get(f"{BINANCE}/depth", {"symbol": sym, "limit": 500}, tries=2)
        kl = _get(f"{BINANCE}/klines", {"symbol": sym, "interval": tf, "limit": 3}, tries=2)
    except DataError as exc:
        log.warning("Order flow %s unavailable: %s", sym, exc)
        return None
    bids = [(float(p), float(q)) for p, q in book.get("bids", [])]
    asks = [(float(p), float(q)) for p, q in book.get("asks", [])]
    if not bids or not asks:
        return None
    best_bid, best_ask = bids[0][0], asks[0][0]
    mid = (best_bid + best_ask) / 2
    bid_1 = sum(p * q for p, q in bids if p >= mid * 0.99)
    ask_1 = sum(p * q for p, q in asks if p <= mid * 1.01)
    flow = {
        "venue": "Binance", "mid": mid,
        "spread_pct": (best_ask - best_bid) / mid * 100,
        "bid_value_1pct": bid_1, "ask_value_1pct": ask_1,
        "imbalance": bid_1 / ask_1 if ask_1 else None,
        "bid_wall": max(bids, key=lambda b: b[0] * b[1]),
        "ask_wall": max(asks, key=lambda a: a[0] * a[1]),
        "unit": "USDT",
        "top_bids": bids[:5], "top_asks": asks[:5],
    }
    if len(kl) >= 2:
        done, forming = kl[-2], kl[-1]
        vol_done, buy_done = float(done[5]), float(done[9])
        flow["taker_buy_pct"] = buy_done / vol_done * 100 if vol_done else None
        elapsed = (datetime.now(timezone.utc).timestamp() * 1000 - float(forming[0])) / (TF_SECONDS[tf] * 1000)
        if avg_volume and 0.05 < elapsed <= 1.2:
            flow["volume_pace"] = float(forming[5]) / min(elapsed, 1.0) / avg_volume
            flow["forming_buy_pct"] = (float(forming[9]) / float(forming[5]) * 100) if float(forming[5]) else None
    return flow


def flow_verdict(flow: dict) -> str:
    imb = flow.get("imbalance")
    buy = flow.get("taker_buy_pct") or flow.get("forming_buy_pct")
    bull = (imb is not None and imb >= 1.2) + (buy is not None and buy >= 55)
    bear = (imb is not None and imb <= 1 / 1.2) + (buy is not None and buy <= 45)
    if bull >= 1 and bear == 0:
        return "buyers stronger"
    if bear >= 1 and bull == 0:
        return "sellers stronger"
    return "mixed / balanced"

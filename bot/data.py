"""Market data from free public endpoints. All functions return a DataFrame
(UTC index, oldest first) with open/high/low/close/volume of COMPLETED candles only.

Crypto: Binance public market-data API (data-api.binance.vision), which is meant
for public data access. If it is unreachable, Kraken's public API is the fallback.
Stocks: yfinance (unofficial Yahoo data, daily candles, for research only).
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
TF_SECONDS = {"1h": 3600, "4h": 14400, "1d": 86400}
KRAKEN_MINUTES = {"1h": 60, "4h": 240, "1d": 1440}


class DataError(RuntimeError):
    pass


def _get(url: str, params: dict | None = None, tries: int = 3) -> object:
    last = None
    for attempt in range(tries):
        try:
            r = requests.get(url, params=params, timeout=20)
            if r.status_code == 200:
                return r.json()
            last = f"HTTP {r.status_code}"
            if r.status_code in (400, 404, 451, 403):
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


def norm_crypto(symbol: str) -> str:
    s = symbol.upper().replace("/", "").replace("-", "").strip()
    return s if s.endswith("USDT") else s + "USDT"


def crypto_candles(symbol: str, timeframe: str | None = None, limit: int = 3000) -> pd.DataFrame:
    """Up to `limit` candles (Binance returns 1000 per request, so this pages backwards)."""
    tf = timeframe or cfg()["crypto"]["timeframe"]
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
    except DataError as first:
        log.warning("Binance %s failed (%s); trying Kraken", sym, first)
        df = _kraken(sym, tf)
    if df.empty:
        raise DataError(f"No candles for {sym}")
    # Drop the candle that is still forming.
    now = datetime.now(timezone.utc).timestamp()
    if df.index[-1].timestamp() + TF_SECONDS[tf] > now:
        df = df.iloc[:-1]
    return df


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
        return [{"symbol": s, "change_pct": None, "quote_volume": None} for s in c["always_include"]]
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
            rows.append({"symbol": s, "change_pct": float(t["priceChangePercent"]), "quote_volume": qv})
        except (KeyError, ValueError):
            continue
    rows.sort(key=lambda r: r["quote_volume"], reverse=True)
    picked = rows[:n]
    have = {r["symbol"] for r in picked}
    by_sym = {r["symbol"]: r for r in rows}
    for s in c["always_include"]:
        if s not in have:
            picked.append(by_sym.get(s, {"symbol": s, "change_pct": None, "quote_volume": None}))
    return picked


def stock_candles(symbol: str) -> pd.DataFrame:
    """Daily NSE candles (stocks or indices such as NIFTY / BANKNIFTY).
    Angel One (official, live) when its secrets are set; otherwise Yahoo (delayed).
    df.attrs carries 'source' and, with Angel One, 'live_price' / 'live_change_pct'."""
    from bot import angel

    sym = symbol.upper().removesuffix(".NS")
    if angel.configured():
        try:
            a = angel.session()
            hist = a.spot_candles(sym)
            q = a.ltp(sym)
        except angel.AngelError as exc:
            raise DataError(f"Angel One {sym}: {exc}") from None
        hist = _drop_forming_day(hist)
        hist.attrs.update(source="Angel One (live)",
                          live_price=angel.num(angel.pick(q, "ltp")) or None,
                          live_change_pct=angel.num(angel.pick(q, "percentChange")))
        return hist

    import yfinance as yf

    ticker = angel.YAHOO_INDEX.get(sym, f"{sym}.NS")
    try:
        hist = yf.Ticker(ticker).history(period="2y", interval="1d", auto_adjust=False)
    except Exception as exc:
        raise DataError(f"Yahoo {sym}: {exc.__class__.__name__}") from None
    if hist is None or hist.empty:
        raise DataError(f"Yahoo {sym}: no data (check the NSE symbol)")
    hist = hist.rename(columns=str.lower)[COLS].astype(float)
    hist.index = pd.to_datetime(hist.index, utc=True)
    hist = _drop_forming_day(hist.dropna())
    hist.attrs.update(source="Yahoo (delayed)")
    return hist


def _drop_forming_day(df: pd.DataFrame) -> pd.DataFrame:
    """Strategies use completed candles only: drop today's bar before 15:30 IST."""
    ist = ZoneInfo("Asia/Kolkata")
    now = datetime.now(ist)
    if len(df) and df.index[-1].astimezone(ist).date() == now.date() and (now.hour, now.minute) < (15, 30):
        attrs = dict(df.attrs)
        df = df.iloc[:-1].copy()
        df.attrs.update(attrs)
    return df


# ---------------------------------------------------------------- live volume + order book

def crypto_orderflow(symbol: str, avg_volume: float | None = None) -> dict | None:
    """Live order book (top 500 levels) and the volume of the candle forming right now.
    Returns None if the data can't be fetched (the analysis still works without it)."""
    sym = norm_crypto(symbol)
    tf = cfg()["crypto"]["timeframe"]
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

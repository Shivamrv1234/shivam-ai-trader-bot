"""Forex, metals, energy and Indian currency/commodity futures.

Where the candles come from, best first (each message says which one was used):
  Global pairs, gold, silver, oil, gas, DXY
    1. MetaTrader 5 (e.g. an Exness account, demo is fine) - LIVE. Needs the MT5 terminal on Windows
       and `pip install MetaTrader5`; works only in the PC mode (start_bot.bat), never on Cloud Run.
    2. Twelve Data (free key, 800 calls/day, 8/minute) - live forex, XAU/USD, XAG/USD. Works anywhere.
    3. Yahoo Finance - free fallback; forex is near-live, futures (gold/oil) about 10 minutes late.
  Indian contracts (rupee prices)
    USDINR / EURINR / GBPINR / JPYINR (NSE currency futures) and MCX gold, silver, crude, natural gas,
    copper - Angel One SmartAPI, LIVE (the same login as NSE). USDINR & co fall back to Yahoo spot rates.
This module only reads prices. It never calls an order function of MT5, Twelve Data or Angel One.
"""
from __future__ import annotations

import logging
import os
import sys
import time
from dataclasses import dataclass

import pandas as pd
import requests

from bot.config import cfg, secret
from bot.data import (ANGEL_TF, COLS, TF_SECONDS, DataError, drop_forming, norm_tf, yahoo_candles)

log = logging.getLogger(__name__)
TD_URL = "https://api.twelvedata.com"
TD_TF = {"5m": "5min", "15m": "15min", "30m": "30min", "1h": "1h", "4h": "4h", "1d": "1day"}


@dataclass(frozen=True)
class Asset:
    code: str             # what the user types / sees: EURUSD, XAUUSD, GOLD
    name: str             # label: "EUR/USD"
    kind: str             # fx | metal | energy | index | inr | mcx
    yahoo: str | None
    td: str | None        # Twelve Data symbol
    mt5: tuple = ()       # MT5 base names to try (broker suffixes such as Exness "m" are added)
    lot_units: float = 0  # units per standard lot / contract multiplier for P&L
    quote: str = "USD"    # price currency
    angel_exch: str = ""  # MCX / CDS for Indian contracts
    icon: str = "💱"
    digits: int = 5

    @property
    def indian(self) -> bool:
        return self.kind in ("inr", "mcx")

    @property
    def currency(self) -> str:
        return "INR" if self.indian else "USD"


def _fx(code, quote="USD", digits=5, mt5=None):
    return Asset(code, f"{code[:3]}/{code[3:]}", "fx", f"{code}=X", f"{code[:3]}/{code[3:]}",
                 tuple(mt5 or [code]), 100_000, quote, digits=digits)


ASSETS: dict[str, Asset] = {a.code: a for a in [
    _fx("EURUSD"), _fx("GBPUSD"), _fx("AUDUSD"), _fx("NZDUSD"),
    _fx("USDJPY", "JPY", 3), _fx("USDCAD", "CAD"), _fx("USDCHF", "CHF"),
    _fx("EURJPY", "JPY", 3), _fx("GBPJPY", "JPY", 3), _fx("EURGBP", "GBP"),
    Asset("XAUUSD", "Gold (XAU/USD)", "metal", "GC=F", "XAU/USD", ("XAUUSD", "GOLD"), 100, "USD", icon="🥇", digits=2),
    Asset("XAGUSD", "Silver (XAG/USD)", "metal", "SI=F", "XAG/USD", ("XAGUSD", "SILVER"), 5000, "USD", icon="🥈",
          digits=3),
    Asset("USOIL", "WTI crude oil", "energy", "CL=F", "WTI/USD", ("USOIL", "XTIUSD", "WTI"), 1000, "USD", icon="🛢",
          digits=2),
    Asset("UKOIL", "Brent crude oil", "energy", "BZ=F", None, ("UKOIL", "XBRUSD", "BRENT"), 1000, "USD", icon="🛢",
          digits=2),
    Asset("NATGAS", "Natural gas", "energy", "NG=F", "NG/USD", ("XNGUSD", "NATGAS", "NGAS"), 10_000, "USD", icon="🔥",
          digits=3),
    Asset("COPPER", "Copper (COMEX)", "metal", "HG=F", None, ("XCUUSD", "COPPER"), 25_000, "USD", icon="🟫", digits=4),
    Asset("DXY", "US Dollar Index", "index", "DX-Y.NYB", None, ("DXY", "USDX"), 0, "USD", icon="💵", digits=3),
    # Indian currency futures (NSE CDS) - quoted in rupees; 1 lot = 1,000 units (JPYINR: per 100 yen)
    Asset("USDINR", "USD/INR", "inr", "INR=X", "USD/INR", (), 1000, "INR", "CDS", "🇮🇳", 4),
    Asset("EURINR", "EUR/INR", "inr", "EURINR=X", "EUR/INR", (), 1000, "INR", "CDS", "🇮🇳", 4),
    Asset("GBPINR", "GBP/INR", "inr", "GBPINR=X", "GBP/INR", (), 1000, "INR", "CDS", "🇮🇳", 4),
    Asset("JPYINR", "JPY/INR (per 100)", "inr", None, None, (), 1000, "INR", "CDS", "🇮🇳", 4),
    # MCX commodities (rupee prices; multiplier = P&L per 1 rupee move for one lot)
    Asset("GOLD", "MCX Gold (₹/10 g)", "mcx", None, None, (), 100, "INR", "MCX", "🥇", 0),
    Asset("GOLDM", "MCX Gold Mini (₹/10 g)", "mcx", None, None, (), 10, "INR", "MCX", "🥇", 0),
    Asset("SILVER", "MCX Silver (₹/kg)", "mcx", None, None, (), 30, "INR", "MCX", "🥈", 0),
    Asset("SILVERM", "MCX Silver Mini (₹/kg)", "mcx", None, None, (), 5, "INR", "MCX", "🥈", 0),
    Asset("CRUDEOIL", "MCX Crude oil (₹/bbl)", "mcx", None, None, (), 100, "INR", "MCX", "🛢", 0),
    Asset("NATURALGAS", "MCX Natural gas (₹/mmBtu)", "mcx", None, None, (), 1250, "INR", "MCX", "🔥", 1),
    Asset("COPPERMCX", "MCX Copper (₹/kg)", "mcx", None, None, (), 2500, "INR", "MCX", "🟫", 2),
]}
ALIASES = {"GOLD/USD": "XAUUSD", "XAU": "XAUUSD", "SILVER/USD": "XAGUSD", "XAG": "XAGUSD", "WTI": "USOIL",
           "CRUDE": "CRUDEOIL", "BRENT": "UKOIL", "OIL": "USOIL", "NG": "NATGAS", "USDX": "DXY",
           "DOLLARINDEX": "DXY", "MCXGOLD": "GOLD", "MCXSILVER": "SILVER", "MCXCRUDE": "CRUDEOIL"}
ANGEL_NAME = {"COPPERMCX": "COPPER"}
# Market hours for "candle complete" and 4h buckets (IST)
SESSION_CLOSE = {"MCX": (23, 30), "CDS": (17, 0)}


def find(code: str) -> Asset | None:
    c = code.upper().replace("/", "").replace(" ", "").replace("-", "").removesuffix("=X")
    c = ALIASES.get(c, c)
    return ASSETS.get(c)


def watchlist() -> list[str]:
    f = cfg().get("forex", {})
    return list(f.get("watchlist", [])) + list(f.get("inr", [])) + list(f.get("mcx", []))


# ---------------------------------------------------------------- MetaTrader 5 (Exness) - Windows PC only

_MT5: dict = {}


def mt5_ready() -> bool:
    """True when the MetaTrader5 package is installed and the terminal is running and logged in."""
    if "ok" in _MT5:
        return _MT5["ok"]
    ok = False
    if secret("MT5_ENABLED").lower() not in ("0", "false", "no", "off") and sys.platform.startswith("win"):
        try:
            import MetaTrader5 as mt5  # type: ignore

            kwargs = {}
            if secret("MT5_PATH"):
                kwargs["path"] = secret("MT5_PATH")
            if secret("MT5_LOGIN") and secret("MT5_PASSWORD") and secret("MT5_SERVER"):
                kwargs.update(login=int(secret("MT5_LOGIN")), password=secret("MT5_PASSWORD"),
                              server=secret("MT5_SERVER"))
            ok = bool(mt5.initialize(**kwargs))
            if not ok:
                log.warning("MT5 initialize failed: %s", mt5.last_error())
            _MT5["mod"] = mt5
        except Exception as exc:  # package missing or terminal not installed
            log.info("MT5 unavailable: %s", exc.__class__.__name__)
    _MT5["ok"] = ok
    return ok


def mt5_symbol(asset: Asset) -> str | None:
    mt5 = _MT5.get("mod")
    if not mt5 or not asset.mt5:
        return None
    key = f"sym:{asset.code}"
    if key in _MT5:
        return _MT5[key]
    suffixes = [secret("MT5_SUFFIX")] if secret("MT5_SUFFIX") else []
    suffixes += ["", "m", "c", ".", "z", "#", "_i", "pro"]
    found = None
    for base in asset.mt5:
        for suf in dict.fromkeys(suffixes):
            info = mt5.symbol_info(base + suf)
            if info is not None:
                found = base + suf
                break
        if found:
            break
    if not found:  # search by name, e.g. XAUUSDm / XAUUSD.r
        for base in asset.mt5:
            hits = mt5.symbols_get(group=f"*{base}*") or []
            if hits:
                found = hits[0].name
                break
    if found:
        mt5.symbol_select(found, True)
    _MT5[key] = found
    return found


def mt5_candles(asset: Asset, tf: str, n: int = 1500) -> pd.DataFrame:
    mt5 = _MT5["mod"]
    sym = mt5_symbol(asset)
    if not sym:
        raise DataError(f"MT5: no symbol for {asset.code} at your broker")
    frame = {"5m": mt5.TIMEFRAME_M5, "15m": mt5.TIMEFRAME_M15, "30m": mt5.TIMEFRAME_M30,
             "1h": mt5.TIMEFRAME_H1, "4h": mt5.TIMEFRAME_H4, "1d": mt5.TIMEFRAME_D1}[tf]
    rates = mt5.copy_rates_from_pos(sym, frame, 0, n)
    if rates is None or len(rates) == 0:
        raise DataError(f"MT5 {sym}: no candles ({mt5.last_error()})")
    df = pd.DataFrame(rates)
    offset_h = float(secret("MT5_SERVER_UTC_OFFSET") or 0)   # Exness server time is UTC
    df.index = pd.to_datetime(df["time"] - offset_h * 3600, unit="s", utc=True)
    df["volume"] = df.get("real_volume", 0)
    if not (df["volume"] > 0).any():
        df["volume"] = df["tick_volume"]
    df = df[COLS].astype(float)
    tick = mt5.symbol_info_tick(sym)
    live = None
    if tick is not None and tick.bid and tick.ask:
        live = (tick.bid + tick.ask) / 2
        df.attrs["spread_pct"] = (tick.ask - tick.bid) / live * 100
    df.attrs.update(source=f"MetaTrader 5 · {sym} (live)", live_price=live or float(df["close"].iloc[-1]))
    return df


# ---------------------------------------------------------------- Twelve Data

_TD_CALLS: list[float] = []


def td_key() -> str:
    return secret("TWELVE_DATA_API_KEY") or secret("TWELVEDATA_API_KEY")


def _td_wait() -> None:
    """Free plan: 8 calls a minute."""
    now = time.time()
    while _TD_CALLS and now - _TD_CALLS[0] > 60:
        _TD_CALLS.pop(0)
    if len(_TD_CALLS) >= 8:
        time.sleep(max(0.0, 61 - (now - _TD_CALLS[0])))
    _TD_CALLS.append(time.time())


def td_candles(asset: Asset, tf: str, n: int = 1500) -> pd.DataFrame:
    if not (td_key() and asset.td):
        raise DataError("Twelve Data not configured for this asset")
    _td_wait()
    try:
        r = requests.get(f"{TD_URL}/time_series", timeout=20, params={
            "symbol": asset.td, "interval": TD_TF[tf], "outputsize": min(n, 5000), "timezone": "UTC",
            "order": "ASC", "apikey": td_key()})
        body = r.json()
    except (requests.RequestException, ValueError) as exc:
        raise DataError(f"Twelve Data {asset.td}: {exc.__class__.__name__}") from None
    if body.get("status") != "ok" or not body.get("values"):
        raise DataError(f"Twelve Data {asset.td}: {str(body.get('message', 'no data'))[:120]}")
    df = pd.DataFrame(body["values"])
    df.index = pd.to_datetime(df.pop("datetime"), utc=True)
    if "volume" not in df:
        df["volume"] = 0.0
    df = df[COLS].astype(float).sort_index()
    df.attrs.update(source="Twelve Data (live)", live_price=td_price(asset) or float(df["close"].iloc[-1]))
    return df


def td_price(asset: Asset) -> float | None:
    if not (td_key() and asset.td):
        return None
    _td_wait()
    try:
        r = requests.get(f"{TD_URL}/price", params={"symbol": asset.td, "apikey": td_key()}, timeout=15)
        return float(r.json()["price"])
    except (requests.RequestException, ValueError, KeyError, TypeError):
        return None


# ---------------------------------------------------------------- Angel One MCX / CDS

def angel_contract(asset: Asset):
    from bot import angel

    a = angel.session()
    row = angel.near_future(a.inst, ANGEL_NAME.get(asset.code, asset.code), asset.angel_exch)
    return a, row


def angel_candles(asset: Asset, tf: str) -> pd.DataFrame:
    from bot import angel

    a, row = angel_contract(asset)
    interval, days = ANGEL_TF[tf]
    token, exch = str(row["token"]), asset.angel_exch
    try:
        df = a.candles(token, exch, interval, days)
        q = a.quotes({exch: [token]})
    except angel.AngelError as exc:
        raise DataError(f"Angel One {asset.code}: {exc}") from None
    if tf == "4h":
        df = resample_4h(df, "Asia/Kolkata", "9h")
    q = q[0] if q else {}
    df.attrs.update(source=f"Angel One {exch} · {row['symbol']} (live)", contract=str(row["symbol"]),
                    expiry=str(row["expiry"]), token=token, exch=exch,
                    lot=int(row["lot"]) if row["lot"] == row["lot"] else 1,
                    live_price=angel.num(angel.pick(q, "ltp")) or float(df["close"].iloc[-1]),
                    live_change_pct=angel.num(angel.pick(q, "percentChange")))
    return df


def resample_4h(df: pd.DataFrame, tz: str, offset: str) -> pd.DataFrame:
    attrs = dict(df.attrs)
    loc = df.tz_convert(tz)
    out = loc.resample("4h", offset=offset).agg({"open": "first", "high": "max", "low": "min", "close": "last",
                                                 "volume": "sum"}).dropna()
    out.index = out.index.tz_convert("UTC")
    out.attrs.update(attrs)
    return out


# ---------------------------------------------------------------- one entry point

def candles(code: str, timeframe: str | None = None, for_scan: bool = False) -> pd.DataFrame:
    """Completed candles for a forex/commodity asset, best live source first."""
    from bot import angel

    asset = find(code)
    if asset is None:
        raise DataError(f"{code}: not a forex/commodity symbol I know (try EURUSD, XAUUSD, USOIL, USDINR, GOLD)")
    tf = norm_tf(timeframe, cfg().get("forex", {}).get("timeframe", "1h"))
    errors = []
    df = None
    if asset.indian and angel.configured():
        try:
            df = angel_candles(asset, tf)
            close = SESSION_CLOSE.get(asset.angel_exch)
            df = drop_forming(df, tf, close, "Asia/Kolkata")
        except (DataError, angel.AngelError) as exc:
            errors.append(str(exc))
    if df is None and not asset.indian and mt5_ready():
        try:
            df = drop_forming(mt5_candles(asset, tf), tf)
        except DataError as exc:
            errors.append(str(exc))
    if df is None and td_key() and asset.td and not for_scan:
        try:
            df = drop_forming(td_candles(asset, tf), tf)
        except DataError as exc:
            errors.append(str(exc))
    if df is None and asset.yahoo:
        try:
            df = yahoo_candles(asset.yahoo, tf, asset.code)
            delay = "near-live" if asset.kind in ("fx", "inr") else "about 10 min late"
            df.attrs["source"] = f"Yahoo ({delay})"
            df = drop_forming(df, tf)
        except DataError as exc:
            errors.append(str(exc))
    if df is None:
        hint = " (MCX needs your Angel One login)" if asset.kind == "mcx" else ""
        raise DataError(f"{asset.code}: no data source worked{hint}. " + "; ".join(errors)[:300])
    if df.empty:
        raise DataError(f"{asset.code}: no candles")
    venue = asset.angel_exch.lower() if asset.indian and "Angel" in df.attrs.get("source", "") else "fx"
    df.attrs.update(tf=tf, tz="Asia/Kolkata" if asset.indian else "UTC", venue=venue, currency=asset.currency,
                    asset=asset.code)
    return df


def live_price(code: str) -> float | None:
    asset = find(code)
    if asset is None:
        return None
    try:
        return candles(code, "5m").attrs.get("live_price")
    except DataError:
        return None


def pip_size(asset: Asset) -> float:
    if asset.kind == "fx":
        return 0.01 if asset.quote == "JPY" else 0.0001
    if asset.kind == "inr":
        return 0.0025
    return 10 ** -max(asset.digits, 0) if asset.digits else 1.0


def quote_to_usd(asset: Asset, price: float) -> float | None:
    """How many USD one unit of the quote currency is worth (for position sizing in USD)."""
    if asset.quote == "USD":
        return 1.0
    if asset.code.startswith("USD") and price:
        return 1.0 / price           # USDJPY, USDCAD, USDCHF: quote per USD
    cross = {"JPY": "USDJPY", "GBP": "GBPUSD", "CAD": "USDCAD", "CHF": "USDCHF"}.get(asset.quote)
    if not cross:
        return None
    p = live_price(cross)
    if not p:
        return None
    return p if cross.endswith("USD") else 1.0 / p


def size_lots(asset: Asset, entry: float, stop: float, risk_money: float) -> tuple[float, float] | None:
    """(lots, money at risk per lot) for a stop distance; None if the contract size is unknown."""
    if not asset.lot_units:
        return None
    dist = abs(entry - stop)
    if asset.indian:
        per_lot = dist * asset.lot_units
    else:
        q = quote_to_usd(asset, entry)
        if q is None:
            return None
        per_lot = dist * asset.lot_units * q
    if per_lot <= 0:
        return None
    return risk_money / per_lot, per_lot


def source_status() -> list[str]:
    """For /settings and the self-test."""
    lines = []
    lines.append(("✅" if mt5_ready() else "ℹ️") + " MetaTrader 5 / Exness: " +
                 ("connected (live)" if mt5_ready() else "off (PC mode on Windows only)"))
    lines.append(("✅" if td_key() else "ℹ️") + " Twelve Data: " + ("key set (live forex, gold, silver)" if td_key()
                                                                  else "no key (optional)"))
    lines.append("ℹ️ Yahoo: fallback for global pairs and commodities (delayed for futures)")
    return lines


def tf_seconds(tf: str) -> int:
    return TF_SECONDS[tf]


def env_flag(name: str) -> bool:
    return os.getenv(name, "").lower() in ("1", "true", "yes", "on")

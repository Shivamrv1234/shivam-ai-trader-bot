"""Angel One SmartAPI: live NSE data, futures, option chain, OI, Greeks. READ-ONLY.

Uses the official SDK (pip install smartapi-python). This module never calls any
order, GTT, position or funds method.

Confirmed from Angel One's SDK source / forum:
- login: generateSession(clientcode, PIN, TOTP)
- candles: getCandleData({exchange, symboltoken, interval, fromdate, todate})
- quotes: getMarketData("FULL", {"NSE": [...], "NFO": [...]}) -> data.fetched[]
- greeks: optionGreek({"name": ..., "expirydate": ...})
- instrument list: OpenAPIScripMaster.json (strike in paise, expiry like 26NOV2026)
Response field names behind Angel One's login are read with fallbacks (see pick()).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from bot.config import secret

log = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")
SCRIP_MASTER = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
QUOTE_BATCH = 50
INDEX_ALIASES = {
    "NIFTY": ["NIFTY 50", "NIFTY"],
    "BANKNIFTY": ["NIFTY BANK", "BANKNIFTY"],
    "FINNIFTY": ["NIFTY FIN SERVICE", "FINNIFTY"],
    "MIDCPNIFTY": ["NIFTY MID SELECT", "MIDCPNIFTY"],
    "SENSEX": ["SENSEX"],
}
YAHOO_INDEX = {"NIFTY": "^NSEI", "BANKNIFTY": "^NSEBANK"}


class AngelError(RuntimeError):
    pass


def configured() -> bool:
    return all(secret(k) for k in ("ANGEL_API_KEY", "ANGEL_CLIENT_CODE", "ANGEL_PIN", "ANGEL_TOTP_SECRET"))


def pick(d: dict, *keys, default=None):
    """First present, non-empty value among candidate field names."""
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return default


def num(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def parse_expiry(s: str) -> date | None:
    try:
        return datetime.strptime(s.strip().upper(), "%d%b%Y").date()
    except (ValueError, AttributeError):
        return None


# ---------------------------------------------------------------- instruments

def load_instruments(raw: list | None = None) -> pd.DataFrame:
    if raw is None:
        try:
            r = requests.get(SCRIP_MASTER, timeout=120)
            r.raise_for_status()
            raw = r.json()
        except (requests.RequestException, ValueError) as exc:
            raise AngelError(f"Instrument list download failed: {exc.__class__.__name__}") from None
    df = pd.DataFrame(raw)
    need = {"token", "symbol", "name", "expiry", "strike", "lotsize", "instrumenttype", "exch_seg"}
    missing = need - set(df.columns)
    if missing:
        raise AngelError(f"Instrument list format changed, missing {sorted(missing)}")
    df = df[df["exch_seg"].isin(["NSE", "NFO"])].copy()
    df["strike_rs"] = pd.to_numeric(df["strike"], errors="coerce") / 100  # paise -> rupees
    df["lot"] = pd.to_numeric(df["lotsize"], errors="coerce")
    df["exp"] = df["expiry"].map(parse_expiry)
    df["name_u"] = df["name"].astype(str).str.upper()
    df["symbol_u"] = df["symbol"].astype(str).str.upper()
    return df


@dataclass
class Underlying:
    name: str
    kind: str            # "index" | "stock"
    spot_token: str
    spot_exchange: str = "NSE"


def find_underlying(inst: pd.DataFrame, name: str) -> Underlying:
    n = name.upper().strip()
    if n in INDEX_ALIASES:
        idx = inst[(inst["exch_seg"] == "NSE") & (inst["instrumenttype"] == "AMXIDX")]
        for alias in INDEX_ALIASES[n]:
            hit = idx[(idx["symbol_u"] == alias) | (idx["name_u"] == alias)]
            if len(hit):
                return Underlying(n, "index", str(hit.iloc[0]["token"]))
        raise AngelError(f"Index {n} not found in Angel One's instrument list")
    eq = inst[(inst["exch_seg"] == "NSE") & (inst["symbol_u"] == f"{n}-EQ")]
    if not len(eq):
        raise AngelError(f"NSE stock {n} not found (use the NSE symbol, e.g. RELIANCE)")
    return Underlying(n, "stock", str(eq.iloc[0]["token"]))


def derivatives(inst: pd.DataFrame, name: str, today: date | None = None) -> tuple[pd.DataFrame, pd.DataFrame, date]:
    """Nearest-expiry futures row(s) and options chain for an underlying."""
    today = today or datetime.now(IST).date()
    n = name.upper()
    nfo = inst[(inst["exch_seg"] == "NFO") & (inst["name_u"] == n)]
    nfo = nfo[nfo["exp"].notna() & (nfo["exp"] >= today)]
    futs = nfo[nfo["instrumenttype"].isin(["FUTIDX", "FUTSTK"])].sort_values("exp")
    opts = nfo[nfo["instrumenttype"].isin(["OPTIDX", "OPTSTK"])]
    if not len(opts):
        raise AngelError(f"{n} has no F&O contracts on NSE")
    expiry = min(opts["exp"])
    chain = opts[opts["exp"] == expiry].copy()
    chain["type"] = chain["symbol_u"].str[-2:]  # ...CE / ...PE
    return futs.head(1), chain[chain["type"].isin(["CE", "PE"])], expiry


# ---------------------------------------------------------------- session

class Angel:
    def __init__(self) -> None:
        if not configured():
            raise AngelError("Angel One secrets are not set (ANGEL_API_KEY, ANGEL_CLIENT_CODE, "
                             "ANGEL_PIN, ANGEL_TOTP_SECRET)")
        import pyotp
        from SmartApi import SmartConnect

        self.api = SmartConnect(api_key=secret("ANGEL_API_KEY"))
        try:
            totp = pyotp.TOTP(secret("ANGEL_TOTP_SECRET").replace(" ", "")).now()
        except Exception:
            raise AngelError("ANGEL_TOTP_SECRET is not a valid TOTP secret (copy the text key, not the QR)") from None
        resp = self.api.generateSession(secret("ANGEL_CLIENT_CODE"), secret("ANGEL_PIN"), totp)
        if not resp or not resp.get("status"):
            raise AngelError(f"Angel One login failed: {(resp or {}).get('message', 'no response')}")
        self._inst: pd.DataFrame | None = None
        self.last_keys: dict[str, list] = {}

    @property
    def inst(self) -> pd.DataFrame:
        if self._inst is None:
            self._inst = load_instruments()
        return self._inst

    def _ok(self, resp, what: str):
        if not resp or not resp.get("status"):
            raise AngelError(f"{what}: {(resp or {}).get('message', 'no response')}")
        return resp.get("data")

    def candles(self, token: str, exchange: str = "NSE", interval: str = "ONE_DAY", days: int = 700) -> pd.DataFrame:
        to_dt = datetime.now(IST)
        params = {"exchange": exchange, "symboltoken": token, "interval": interval,
                  "fromdate": (to_dt - timedelta(days=days)).strftime("%Y-%m-%d 09:15"),
                  "todate": to_dt.strftime("%Y-%m-%d %H:%M")}
        rows = self._ok(self.api.getCandleData(params), "Candles")
        if not rows:
            raise AngelError("Candles: empty response")
        df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
        df.index = pd.to_datetime(df.pop("ts"), utc=True)
        return df.astype(float).sort_index()

    def quotes(self, tokens: dict[str, list[str]]) -> list[dict]:
        out = []
        for exch, toks in tokens.items():
            for i in range(0, len(toks), QUOTE_BATCH):
                data = self._ok(self.api.getMarketData("FULL", {exch: toks[i:i + QUOTE_BATCH]}), "Quote")
                fetched = (data or {}).get("fetched") or []
                if fetched:
                    self.last_keys["quote"] = sorted(fetched[0].keys())
                out += fetched
                time.sleep(1.1)  # stay under Angel One's rate limit
        return out

    def greeks(self, name: str, expiry: date) -> list[dict]:
        params = {"name": name.upper(), "expirydate": expiry.strftime("%d%b%Y").upper()}
        try:
            data = self._ok(self.api.optionGreek(params), "Option Greeks")
        except AngelError as exc:
            log.warning("%s", exc)
            return []
        rows = data if isinstance(data, list) else []
        if rows:
            self.last_keys["greeks"] = sorted(rows[0].keys())
        return rows

    def spot_candles(self, name: str) -> pd.DataFrame:
        u = find_underlying(self.inst, name)
        return self.candles(u.spot_token, u.spot_exchange)

    def ltp(self, name: str) -> dict:
        u = find_underlying(self.inst, name)
        q = self.quotes({"NSE": [u.spot_token]})
        if not q:
            raise AngelError(f"No live quote for {name}")
        return q[0]


_session: Angel | None = None


def session() -> Angel:
    global _session
    if _session is None:
        _session = Angel()
    return _session


# ---------------------------------------------------------------- F&O snapshot

@dataclass
class Leg:
    strike: float
    kind: str           # CE / PE
    symbol: str
    ltp: float = 0.0
    oi: float = 0.0
    volume: float = 0.0
    change_pct: float = 0.0
    delta: float | None = None
    iv: float | None = None
    theta: float | None = None


@dataclass
class FnoSnapshot:
    name: str
    kind: str
    spot: float
    spot_change_pct: float
    expiry: date
    lot: int
    fut_symbol: str | None = None
    fut_ltp: float | None = None
    fut_oi: float | None = None
    legs: list = field(default_factory=list)
    fetched_at: str = ""

    @property
    def strikes(self) -> list[float]:
        return sorted({l.strike for l in self.legs})

    @property
    def atm(self) -> float:
        return min(self.strikes, key=lambda k: abs(k - self.spot))

    def side(self, kind: str) -> list:
        return sorted([l for l in self.legs if l.kind == kind], key=lambda l: l.strike)

    @property
    def pcr(self) -> float | None:
        ce = sum(l.oi for l in self.side("CE"))
        pe = sum(l.oi for l in self.side("PE"))
        return round(pe / ce, 2) if ce else None

    def max_oi(self, kind: str) -> Leg | None:
        legs = self.side(kind)
        return max(legs, key=lambda l: l.oi) if legs and any(l.oi for l in legs) else None

    @property
    def max_pain(self) -> float | None:
        """Strike where option buyers' total payoff (writers' payout) is smallest."""
        if not any(l.oi for l in self.legs):
            return None
        best, best_cost = None, None
        for k in self.strikes:
            cost = sum(l.oi * max(0.0, k - l.strike) for l in self.side("CE"))
            cost += sum(l.oi * max(0.0, l.strike - k) for l in self.side("PE"))
            if best_cost is None or cost < best_cost:
                best, best_cost = k, cost
        return best

    @property
    def basis(self) -> float | None:
        return round(self.fut_ltp - self.spot, 2) if self.fut_ltp else None


def build_snapshot(a: Angel, name: str, strikes_each_side: int = 10) -> FnoSnapshot:
    u = find_underlying(a.inst, name)
    futs, chain, expiry = derivatives(a.inst, u.name)
    spot_q = a.ltp(u.name)
    spot = num(pick(spot_q, "ltp", "lastTradedPrice"))
    if not spot:
        raise AngelError(f"No live price for {u.name}")

    all_strikes = sorted(chain["strike_rs"].dropna().unique())
    atm_i = min(range(len(all_strikes)), key=lambda i: abs(all_strikes[i] - spot))
    window = set(all_strikes[max(0, atm_i - strikes_each_side): atm_i + strikes_each_side + 1])
    sel = chain[chain["strike_rs"].isin(window)]

    tokens = [str(t) for t in sel["token"]]
    fut_row = futs.iloc[0] if len(futs) else None
    if fut_row is not None:
        tokens.append(str(fut_row["token"]))
    fetched = a.quotes({"NFO": tokens})
    quotes = {str(pick(q, "symbolToken", "token", default="")): q for q in fetched}
    by_symbol = {str(pick(q, "tradingSymbol", "symbol", default="")).upper(): q for q in fetched}
    sym_of = {str(t): str(s).upper() for t, s in zip(sel["token"], sel["symbol"])}
    if fut_row is not None:
        sym_of[str(fut_row["token"])] = str(fut_row["symbol"]).upper()
    for tok, sym in sym_of.items():  # fall back to matching by trading symbol
        if tok not in quotes and sym in by_symbol:
            quotes[tok] = by_symbol[sym]

    snap = FnoSnapshot(
        name=u.name, kind=u.kind, spot=spot,
        spot_change_pct=num(pick(spot_q, "percentChange", "pChange")),
        expiry=expiry, lot=int(sel["lot"].iloc[0]) if len(sel) else 0,
        fetched_at=datetime.now(IST).strftime("%d %b %H:%M IST"),
    )
    if fut_row is not None:
        fq = quotes.get(str(fut_row["token"]), {})
        snap.fut_symbol = str(fut_row["symbol"])
        snap.fut_ltp = num(pick(fq, "ltp")) or None
        snap.fut_oi = num(pick(fq, "opnInterest", "openInterest", "oi")) or None

    greeks = {}
    for g in a.greeks(u.name, expiry):
        k = (round(num(pick(g, "strikePrice", "strike")), 2), str(pick(g, "optionType", default="")).upper())
        greeks[k] = g
    for _, row in sel.iterrows():
        q = quotes.get(str(row["token"]), {})
        leg = Leg(strike=float(row["strike_rs"]), kind=row["type"], symbol=str(row["symbol"]),
                  ltp=num(pick(q, "ltp")), oi=num(pick(q, "opnInterest", "openInterest", "oi")),
                  volume=num(pick(q, "tradeVolume", "volume")), change_pct=num(pick(q, "percentChange")))
        g = greeks.get((round(leg.strike, 2), leg.kind))
        if g:
            leg.delta = num(pick(g, "delta"), None)
            leg.iv = num(pick(g, "impliedVolatility", "iv"), None)
            leg.theta = num(pick(g, "theta"), None)
        snap.legs.append(leg)
    return snap


# ---------------------------------------------------------------- live volume + order book (NSE)

def orderflow(a: Angel, name: str, avg_daily_volume: float | None = None) -> dict | None:
    """Market depth (best 5 bids/asks), total pending buy/sell quantity and today's volume.
    Indices have no order book, so their near-month future is used."""
    u = find_underlying(a.inst, name)
    token, exch, label = u.spot_token, "NSE", u.name
    if u.kind == "index":
        futs, _, _ = derivatives(a.inst, u.name)
        if not len(futs):
            return None
        token, exch, label = str(futs.iloc[0]["token"]), "NFO", str(futs.iloc[0]["symbol"])
    try:
        q = a.quotes({exch: [token]})
    except AngelError as exc:
        log.warning("Order flow %s unavailable: %s", name, exc)
        return None
    if not q:
        return None
    q = q[0]
    depth = pick(q, "depth", default={}) or {}
    if depth:
        a.last_keys["depth"] = sorted(depth.keys()) if isinstance(depth, dict) else ["(not a dict)"]

    def side(key):
        rows = depth.get(key, []) if isinstance(depth, dict) else []
        out = []
        for r in rows:
            p, qty = num(pick(r, "price")), num(pick(r, "quantity", "qty"))
            if p > 0 and qty > 0:
                out.append((p, qty))
        return out

    bids, asks = side("buy"), side("sell")
    tot_buy = num(pick(q, "totBuyQuan", "totalBuyQuantity"))
    tot_sell = num(pick(q, "totSellQuan", "totalSellQuantity"))
    flow = {"venue": f"Angel One ({label})", "unit": "INR"}
    if tot_buy and tot_sell:
        flow["imbalance"] = tot_buy / tot_sell
        flow["tot_buy_qty"], flow["tot_sell_qty"] = tot_buy, tot_sell
    if bids and asks:
        mid = (bids[0][0] + asks[0][0]) / 2
        flow["mid"] = mid
        flow["spread_pct"] = (asks[0][0] - bids[0][0]) / mid * 100
        flow["bid_wall"] = max(bids, key=lambda b: b[1])
        flow["ask_wall"] = max(asks, key=lambda x: x[1])
    vol = num(pick(q, "tradeVolume", "volume"))
    if vol and avg_daily_volume:
        now = datetime.now(IST)
        mins = (now.hour * 60 + now.minute) - (9 * 60 + 15)
        frac = min(max(mins / 375, 0.05), 1.0) if now.weekday() < 5 else 1.0
        flow["volume_pace"] = vol / frac / avg_daily_volume
        flow["volume_today"] = vol
    if len(flow) <= 2:
        return None
    return flow

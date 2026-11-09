"""Angel One SmartAPI: live NSE data, futures, option chain, OI, Greeks. READ-ONLY.

Uses the official SDK (pip install smartapi-python). This module never calls any
order, GTT, position or funds method.

Confirmed from Angel One's SDK source / forum:
- login: generateSession(clientcode, PIN, TOTP)
- candles: getCandleData({exchange, symboltoken, interval, fromdate, todate})
- quotes: getMarketData("FULL", {"NSE": [...], "NFO": [...]}) -> data.fetched[]
- greeks: optionGreek({"name": ..., "expirydate": ...})
- instrument list: OpenAPIScripMaster.json (strike in paise, expiry like 26NOV2026); public, no login
- OI build-up: oIBuildup({"expirytype": "NEAR", "datatype": "Long Built Up"}) -> rows with
  tradingSymbol, ltp, percentChange, opnInterest, netChangeOpnInterest (Angel One forum post 15207)
- historical OI: getOIData({exchange, symboltoken, interval, fromdate, todate}); row format is not
  documented, so it is parsed defensively and simply skipped if unreadable
Response field names behind Angel One's login are read with fallbacks (see pick()).
"""
from __future__ import annotations

import logging
import threading
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
    "INDIAVIX": ["INDIA VIX", "INDIAVIX"],
}
YAHOO_INDEX = {"NIFTY": "^NSEI", "BANKNIFTY": "^NSEBANK", "INDIAVIX": "^INDIAVIX", "FINNIFTY": "NIFTY_FIN_SERVICE.NS",
               "SENSEX": "^BSESN"}
NO_FNO_INDEX = {"INDIAVIX"}
BUILDUP_TYPES = ["Long Built Up", "Short Built Up", "Short Covering", "Long Unwinding"]


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
    df = df[df["exch_seg"].isin(["NSE", "NFO", "MCX", "CDS"])].copy()
    df["strike_rs"] = pd.to_numeric(df["strike"], errors="coerce") / 100  # paise -> rupees
    df["lot"] = pd.to_numeric(df["lotsize"], errors="coerce")
    df["exp"] = df["expiry"].map(parse_expiry)
    df["name_u"] = df["name"].astype(str).str.upper()
    df["symbol_u"] = df["symbol"].astype(str).str.upper()
    return df


_INST_CACHE: dict = {}


def instruments() -> pd.DataFrame:
    """The public instrument list, downloaded once per process (it is large)."""
    if "df" not in _INST_CACHE:
        _INST_CACHE["df"] = load_instruments()
    return _INST_CACHE["df"]


def nse_equities(inst: pd.DataFrame) -> pd.DataFrame:
    eq = inst[(inst["exch_seg"] == "NSE") & inst["symbol_u"].str.endswith("-EQ")].copy()
    eq["sym"] = eq["symbol_u"].str[:-3]
    return eq


def search_equities(inst: pd.DataFrame, query: str, limit: int = 8) -> list[str]:
    """NSE symbols matching a query: exact symbol first, then symbols starting with it, then containing it.
    The instrument list has symbols only (no company names), so 'tata motors' becomes 'TATAMOTORS'."""
    q = "".join(ch for ch in query.upper() if ch.isalnum() or ch in "&-")
    if not q:
        return []
    eq = nse_equities(inst)
    syms = sorted(set(eq["sym"]))
    exact = [x for x in syms if x == q]
    starts = sorted([x for x in syms if x.startswith(q) and x != q], key=len)
    contains = sorted([x for x in syms if q in x and not x.startswith(q)], key=len)
    return (exact + starts + contains)[:limit]


def has_fno(inst: pd.DataFrame, name: str) -> bool:
    n = name.upper()
    if n in NO_FNO_INDEX:
        return False
    nfo = inst[(inst["exch_seg"] == "NFO") & (inst["name_u"] == n)]
    return bool(len(nfo))


def all_futures(inst: pd.DataFrame, name: str, today: date | None = None) -> pd.DataFrame:
    """Every live futures contract of an underlying, nearest expiry first (near / next / far)."""
    today = today or datetime.now(IST).date()
    n = name.upper()
    f = inst[(inst["exch_seg"] == "NFO") & (inst["name_u"] == n) & inst["instrumenttype"].isin(["FUTIDX", "FUTSTK"])]
    f = f[f["exp"].notna() & (f["exp"] >= today)]
    return f.sort_values("exp")


def near_future(inst: pd.DataFrame, name: str, exch: str, today: date | None = None, roll_days: int = 3) -> pd.Series:
    """Near-month MCX commodity or CDS currency future (rolls to the next month a few days before expiry,
    when trading moves there)."""
    today = today or datetime.now(IST).date()
    n = name.upper()
    kinds = ["FUTCOM"] if exch == "MCX" else ["FUTCUR"]
    f = inst[(inst["exch_seg"] == exch) & (inst["name_u"] == n) & inst["instrumenttype"].isin(kinds)]
    f = f[f["exp"].notna() & (f["exp"] >= today)].sort_values("exp")
    if not len(f):
        raise AngelError(f"No live {exch} future for {n}")
    if len(f) > 1 and (f.iloc[0]["exp"] - today).days <= roll_days:
        return f.iloc[1]
    return f.iloc[0]


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

# Minimum seconds between two calls of one kind. Angel One allows about 3 candle requests a second
# (180 a minute), 10 quote requests a second and 1 a second for option Greeks / OI build-up. When it is
# asked faster it answers with an HTML "access denied" page, which the SDK reports as DataException.
SPACING = {"candles": 0.4, "quote": 0.25, "greeks": 1.1, "buildup": 1.1, "oi": 0.5, "login": 1.0}
_call_lock = threading.Lock()
_last_call: dict[str, float] = {}


def _throttle(kind: str) -> None:
    gap = SPACING.get(kind, 0.4)
    wait = _last_call.get(kind, 0.0) + gap - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_call[kind] = time.monotonic()


def _busy(text: str) -> bool:
    t = text.lower()
    return any(w in t for w in ("access rate", "rate limit", "too many", "couldn't parse", "exceeding",
                                "unknown content-type", "timed out", "timeout", "connection", "temporarily"))


def _expired(text: str) -> bool:
    t = text.lower()
    return any(w in t for w in ("invalid token", "token expired", "tokenexception", "ag8001", "ag8002", "ag8003",
                                "unauthorized"))


class Angel:
    def __init__(self) -> None:
        if not configured():
            raise AngelError("Angel One secrets are not set (ANGEL_API_KEY, ANGEL_CLIENT_CODE, "
                             "ANGEL_PIN, ANGEL_TOTP_SECRET)")
        from SmartApi import SmartConnect

        self.api = SmartConnect(api_key=secret("ANGEL_API_KEY"))
        self._inst: pd.DataFrame | None = None
        self.last_keys: dict[str, list] = {}
        self._cache: dict = {}
        self._login()

    def _login(self) -> None:
        import pyotp

        try:
            totp_obj = pyotp.TOTP(secret("ANGEL_TOTP_SECRET").replace(" ", ""))
            totp_obj.now()
        except Exception:
            raise AngelError("ANGEL_TOTP_SECRET is not a valid TOTP secret (copy the text key, not the QR)") from None
        last = "no response"
        for attempt in range(3):
            with _call_lock:
                _throttle("login")
                try:
                    resp = self.api.generateSession(secret("ANGEL_CLIENT_CODE"), secret("ANGEL_PIN"), totp_obj.now())
                except Exception as exc:  # SDK DataException when Angel One is busy
                    resp, last = None, f"{exc.__class__.__name__}: {str(exc)[:100]}"
            if resp and resp.get("status"):
                return
            if resp:
                last = resp.get("message", last)
                if not _busy(str(last)):
                    break
            time.sleep(1.5 * (attempt + 1))
        raise AngelError(f"Angel One login failed: {last}")

    def call(self, kind: str, what: str, fn, *args):
        """Every SmartAPI request goes through here: spaced to Angel One's rate limits, retried when Angel One
        is busy (it then returns a non-JSON page -> SDK DataException), and logged in again if the session
        expired. Always raises AngelError (never an SDK exception) when it finally fails."""
        last = ""
        for attempt in range(4):
            with _call_lock:
                _throttle(kind)
                try:
                    resp = fn(*args)
                    err = None
                except Exception as exc:
                    resp, err = None, f"{exc.__class__.__name__}: {str(exc)[:160]}"
            if err is None and resp and resp.get("status"):
                return resp.get("data")
            text = err or str((resp or {}).get("message") or (resp or {}).get("errorcode") or "no response")
            last = text
            if _expired(text) or (resp and str(resp.get("errorcode", "")).startswith("AG80")):
                log.info("Angel One session expired; logging in again")
                self._login()
                continue
            if err is None and not _busy(text):
                break                      # a real answer like "invalid symbol": retrying won't help
            log.warning("%s: Angel One busy (%s); retry %d", what, text[:80], attempt + 1)
            time.sleep(0.8 * (attempt + 1))
        if _busy(last):
            raise AngelError(f"{what}: Angel One is busy right now (rate limit). Try again in a minute.")
        raise AngelError(f"{what}: {last}")

    @property
    def inst(self) -> pd.DataFrame:
        if self._inst is None:
            self._inst = instruments()
        return self._inst

    def _ok(self, resp, what: str):
        if not resp or not resp.get("status"):
            raise AngelError(f"{what}: {(resp or {}).get('message', 'no response')}")
        return resp.get("data")

    def candles(self, token: str, exchange: str = "NSE", interval: str = "ONE_DAY", days: int = 700) -> pd.DataFrame:
        """Candles, cached briefly so several screens in a row (chart, F&O, VIX...) don't re-ask Angel One."""
        key = (token, exchange, interval, days)
        ttl = 120 if interval == "ONE_DAY" else 30
        cache = self.__dict__.setdefault("_cache", {})
        hit = cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1].copy()
        to_dt = datetime.now(IST)
        params = {"exchange": exchange, "symboltoken": token, "interval": interval,
                  "fromdate": (to_dt - timedelta(days=days)).strftime("%Y-%m-%d 00:00"),
                  "todate": to_dt.strftime("%Y-%m-%d %H:%M")}
        rows = self.call("candles", "Candles", self.api.getCandleData, params)
        if not rows:
            raise AngelError("Candles: empty response")
        df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
        df.index = pd.to_datetime(df.pop("ts"), utc=True)
        df = df.astype(float).sort_index()
        cache[key] = (time.time(), df)
        return df.copy()

    def quotes(self, tokens: dict[str, list[str]]) -> list[dict]:
        out = []
        for exch, toks in tokens.items():
            for i in range(0, len(toks), QUOTE_BATCH):
                data = self.call("quote", "Quote", self.api.getMarketData, "FULL", {exch: toks[i:i + QUOTE_BATCH]})
                fetched = (data or {}).get("fetched") or []
                if fetched:
                    self.last_keys["quote"] = sorted(fetched[0].keys())
                out += fetched
        return out

    def ltp_many(self, names: list[str]) -> dict[str, dict]:
        """Live quotes for many NSE symbols in one request (scanner lists)."""
        toks = {}
        for n in names:
            try:
                u = find_underlying(self.inst, n)
                toks[str(u.spot_token)] = n.upper()
            except AngelError:
                continue
        out = {}
        for q in self.quotes({"NSE": list(toks)}) if toks else []:
            name = toks.get(str(pick(q, "symbolToken", "token", default="")))
            if name:
                out[name] = q
        return out

    def greeks(self, name: str, expiry: date) -> list[dict]:
        params = {"name": name.upper(), "expirydate": expiry.strftime("%d%b%Y").upper()}
        try:
            data = self.call("greeks", "Option Greeks", self.api.optionGreek, params)
        except AngelError as exc:
            log.warning("%s", exc)
            return []
        rows = data if isinstance(data, list) else []
        if rows:
            self.last_keys["greeks"] = sorted(rows[0].keys())
        return rows

    def oi_buildup(self, expirytype: str = "NEAR") -> dict[str, tuple[str, dict]]:
        """{trading symbol: (build-up type, row)} from Angel One's four OI build-up lists.
        The lists hold the day's top contracts only, so a symbol may be missing."""
        out = {}
        for kind in BUILDUP_TYPES:
            try:
                rows = self.call("buildup", f"OI build-up {kind}", self.api.oIBuildup,
                                 {"expirytype": expirytype, "datatype": kind})
            except AngelError as exc:
                log.warning("%s", exc)
                continue
            except Exception as exc:  # older SDK without the method, network errors
                log.warning("OI build-up unavailable: %s", exc.__class__.__name__)
                return out
            for r in rows if isinstance(rows, list) else []:
                sym = str(pick(r, "tradingSymbol", "symbol", default="")).upper()
                tok = str(pick(r, "symbolToken", "token", default=""))
                if sym:
                    out.setdefault(sym, (kind, r))
                if tok:
                    out.setdefault("#" + tok, (kind, r))
            if rows and isinstance(rows, list):
                self.last_keys["oi_buildup"] = sorted(rows[0].keys())
        return out

    def oi_history(self, token: str, exchange: str = "NFO", days: int = 90) -> pd.Series | None:
        """Daily open interest of a contract (oldest first), or None if Angel One returns nothing readable."""
        to_dt = datetime.now(IST)
        params = {"exchange": exchange, "symboltoken": token, "interval": "ONE_DAY",
                  "fromdate": (to_dt - timedelta(days=days)).strftime("%Y-%m-%d 09:15"),
                  "todate": to_dt.strftime("%Y-%m-%d %H:%M")}
        try:
            rows = self.call("oi", "OI history", self.api.getOIData, params)
        except Exception as exc:
            log.warning("OI history unavailable: %s", exc)
            return None
        pts = []
        for r in rows if isinstance(rows, list) else []:
            if isinstance(r, dict):
                t, v = pick(r, "time", "timestamp", "date"), pick(r, "oi", "openInterest", "opnInterest")
            elif isinstance(r, (list, tuple)) and len(r) >= 2:
                t, v = r[0], r[-1]
            else:
                continue
            ts = pd.to_datetime(t, errors="coerce", utc=True)
            if ts is not pd.NaT and v is not None:
                pts.append((ts, num(v)))
        if rows and isinstance(rows, list):
            self.last_keys["oi_history"] = sorted(rows[0].keys()) if isinstance(rows[0], dict) else ["(list rows)"]
        if len(pts) < 2:
            return None
        ser = pd.Series(dict(pts)).sort_index()
        return ser[~ser.index.duplicated(keep="last")]

    def spot_candles(self, name: str, interval: str = "ONE_DAY", days: int = 700) -> pd.DataFrame:
        u = find_underlying(self.inst, name)
        return self.candles(u.spot_token, u.spot_exchange, interval, days)

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
    gamma: float | None = None
    vega: float | None = None
    greeks_src: str = ""         # "Angel One" or "computed" (Black-Scholes from the live premium)


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
            leg.gamma = num(pick(g, "gamma"), None)
            leg.vega = num(pick(g, "vega"), None)
            leg.greeks_src = "Angel One"
        snap.legs.append(leg)
    return snap


# ---------------------------------------------------------------- live volume + order book (NSE)

def orderflow(a: Angel, name: str, avg_daily_volume: float | None = None, token: str | None = None,
              exch: str | None = None, label: str | None = None) -> dict | None:
    """Market depth (best 5 bids/asks), total pending buy/sell quantity and today's volume.
    Indices have no order book, so their near-month future is used. MCX/CDS futures pass their own token."""
    if token is None:
        u = find_underlying(a.inst, name)
        token, exch, label = u.spot_token, "NSE", u.name
        if u.kind == "index":
            if u.name in NO_FNO_INDEX:
                return None
            futs, _, _ = derivatives(a.inst, u.name)
            if not len(futs):
                return None
            token, exch, label = str(futs.iloc[0]["token"]), "NFO", str(futs.iloc[0]["symbol"])
    exch, label = exch or "NSE", label or name
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
    flow = {"venue": f"Angel One ({label})", "unit": "INR", "top_bids": bids[:5], "top_asks": asks[:5]}
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
    if len(flow) <= 4:
        return None
    return flow


# ---------------------------------------------------------------- futures (all expiries)

@dataclass
class FutLeg:
    symbol: str
    token: str
    expiry: date
    lot: int
    ltp: float = 0.0
    change_pct: float = 0.0
    oi: float = 0.0
    volume: float = 0.0
    high: float = 0.0
    low: float = 0.0
    prev_close: float = 0.0
    buildup: str | None = None        # from Angel One's OI build-up lists
    oi_change_pct: float | None = None


@dataclass
class FuturesReport:
    name: str
    kind: str
    spot: float
    spot_change_pct: float
    legs: list = field(default_factory=list)
    fetched_at: str = ""
    candles: pd.DataFrame | None = None   # near-month future, daily
    oi_hist: pd.Series | None = None      # near-month future, daily OI

    @staticmethod
    def days_left(leg: FutLeg) -> int:
        return max((leg.expiry - datetime.now(IST).date()).days, 0)

    def basis(self, leg: FutLeg) -> float:
        return leg.ltp - self.spot

    def basis_pct(self, leg: FutLeg) -> float:
        return self.basis(leg) / self.spot * 100 if self.spot else 0.0

    def carry_annual_pct(self, leg: FutLeg) -> float | None:
        d = self.days_left(leg)
        return self.basis_pct(leg) * 365 / d if d >= 1 else None


def classify_buildup(price_change_pct: float, oi_change: float) -> str | None:
    """Standard reading of price vs open-interest change."""
    if not oi_change or not price_change_pct:
        return None
    if price_change_pct > 0:
        return "Long Built Up" if oi_change > 0 else "Short Covering"
    return "Short Built Up" if oi_change > 0 else "Long Unwinding"


def build_futures(a: Angel, name: str) -> FuturesReport:
    u = find_underlying(a.inst, name)
    futs = all_futures(a.inst, u.name).head(3)
    if not len(futs):
        raise AngelError(f"{u.name} has no futures on NSE (only F&O stocks and indices do)")
    spot_q = a.ltp(u.name)
    spot = num(pick(spot_q, "ltp", "lastTradedPrice"))
    if not spot:
        raise AngelError(f"No live price for {u.name}")
    fetched = a.quotes({"NFO": [str(t) for t in futs["token"]]})
    by_tok = {str(pick(q, "symbolToken", "token", default="")): q for q in fetched}
    by_sym = {str(pick(q, "tradingSymbol", "symbol", default="")).upper(): q for q in fetched}
    rep = FuturesReport(name=u.name, kind=u.kind, spot=spot,
                        spot_change_pct=num(pick(spot_q, "percentChange", "pChange")),
                        fetched_at=datetime.now(IST).strftime("%d %b %H:%M IST"))
    buildups = a.oi_buildup("NEAR")
    for _, row in futs.iterrows():
        sym = str(row["symbol"]).upper()
        q = by_tok.get(str(row["token"])) or by_sym.get(sym) or {}
        leg = FutLeg(symbol=str(row["symbol"]), token=str(row["token"]), expiry=row["exp"],
                     lot=int(row["lot"]) if pd.notna(row["lot"]) else 0,
                     ltp=num(pick(q, "ltp")), change_pct=num(pick(q, "percentChange")),
                     oi=num(pick(q, "opnInterest", "openInterest", "oi")),
                     volume=num(pick(q, "tradeVolume", "volume")), high=num(pick(q, "high")),
                     low=num(pick(q, "low")), prev_close=num(pick(q, "close")))
        hit = buildups.get(sym) or buildups.get("#" + str(row["token"]))
        if hit:
            leg.buildup = hit[0]
        rep.legs.append(leg)
    if not any(l.ltp for l in rep.legs):
        raise AngelError(f"No live futures prices for {u.name}")
    near = rep.legs[0]
    try:
        rep.candles = a.candles(near.token, "NFO", "ONE_DAY", days=120)
    except AngelError as exc:
        log.warning("Futures candles %s: %s", near.symbol, exc)
    rep.oi_hist = a.oi_history(near.token)
    if rep.oi_hist is not None and len(rep.oi_hist) >= 2 and rep.oi_hist.iloc[-2]:
        near.oi_change_pct = (rep.oi_hist.iloc[-1] - rep.oi_hist.iloc[-2]) / rep.oi_hist.iloc[-2] * 100
        if near.buildup is None:
            near.buildup = classify_buildup(near.change_pct, near.oi_change_pct)
    return rep


def market_buildup(a: Angel, expirytype: str = "NEAR", top: int = 5) -> dict[str, list[dict]]:
    """Top contracts in each OI build-up list (market-wide futures positioning today)."""
    out = {}
    for kind in BUILDUP_TYPES:
        try:
            rows = a.call("buildup", f"OI build-up {kind}", a.api.oIBuildup,
                          {"expirytype": expirytype, "datatype": kind})
        except AngelError as exc:
            log.warning("%s", exc)
            rows = []
        out[kind] = [r for r in (rows if isinstance(rows, list) else [])][:top]
    return out

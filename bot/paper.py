"""Paper trading inside Telegram (private chat or a group): no real orders, ever.

Opening a trade is a short guided ticket (you type numbers or tap the bot's suggestions):
  1 Entry      ⚡ market (live price) · 🤖 the bot's entry · or type a price
               (below/above the live price = a pending LIMIT or STOP order that fills when price gets there)
  2 Stop-loss  🤖 bot · 1 / 1.5 / 2 ATR · or type
  3 Target     🤖 bot · 1:1.5 / 1:2 / 1:3 · or type
  4 Size       risk ladder buttons ($10 / $25 / $50 / $100 or the rupee ladder) · or type a quantity / lots
  5 Leverage   crypto 1x–20x · forex 1:10–1:500 · stocks 1x delivery / 5x intraday · futures: exchange margin
  ✅ Confirm   summary with risk, reward, R:R, notional, margin, liquidation (crypto) and checks
Then: live status with P&L in money, % move and % on margin (ROE), R multiple, distance to stop/target;
stops, targets, liquidations and pending orders are checked on 5-minute candles since the order was placed;
✏️ move SL/TP, ❌ exit, 🚫 cancel pending, 📜 history, 🏆 group board. Each group member has their own trades.
"""
from __future__ import annotations

import html
import logging
import math
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from bot import data, risk, store
from bot.config import cfg

log = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")
LINE = "━━━━━━━━━━━━━━━━━━"
_PRICE: dict = {}
FUTURES_MARGIN = {"index": 0.12, "mcx": 0.10, "cds": 0.03}   # rough exchange margin as a share of notional


class PaperError(RuntimeError):
    pass


# ---------------------------------------------------------------- prices

def live_price(market: str, symbol: str) -> float | None:
    hit = _PRICE.get((market, symbol))
    if hit and time.time() - hit[0] < 20:
        return hit[1]
    p = None
    try:
        if market == "crypto":
            p = data.crypto_price(symbol) or data.crypto_candles(symbol, "5m", limit=60).attrs.get("live_price")
        elif market == "fx":
            from bot import fx
            p = fx.live_price(symbol)
        else:
            from bot import angel
            if angel.configured():
                q = angel.session().ltp(symbol)
                p = angel.num(angel.pick(q, "ltp")) or None
            else:
                p = data.stock_candles(symbol, "5m").attrs.get("live_price")
    except Exception as exc:  # a price problem must never break the status screen
        log.info("Live price %s %s: %s", market, symbol, exc)
    if p:
        _PRICE[(market, symbol)] = (time.time(), float(p))
    return p


def candles_since(t: dict) -> pd.DataFrame | None:
    """5-minute candles after the order was placed (to see if it filled, or hit its stop/target)."""
    try:
        if t["market"] == "crypto":
            df = data.crypto_candles(t["symbol"], "5m", limit=1000)
        elif t["market"] == "fx":
            from bot import fx
            df = fx.candles(t["symbol"], "5m")
        else:
            df = data.stock_candles(t["symbol"], "5m")
    except Exception as exc:
        log.info("Paper candles %s: %s", t["symbol"], exc)
        return None
    since = pd.Timestamp(t.get("created", t.get("opened", 0)), unit="s", tz="UTC")
    return df[df.index >= since.floor("5min") + pd.Timedelta(minutes=5)]


# ---------------------------------------------------------------- maths

def sign(t: dict) -> int:
    return 1 if t["side"] == "BUY" else -1


def notional(t: dict, price: float | None = None) -> float:
    return abs((price or t["entry"]) * t["qty"] * t.get("mult", 1))


def margin(t: dict) -> float:
    lev = t.get("lev")
    if lev:
        return notional(t) / lev
    pct = t.get("margin_pct")
    return notional(t) * pct if pct else notional(t)


def liquidation(side: str, entry: float, lev: float | None, market: str) -> float | None:
    """Rough isolated-margin liquidation price for leveraged crypto (ignores maintenance margin and fees)."""
    if market != "crypto" or not lev or lev <= 1:
        return None
    return entry * (1 - 1 / lev) if side == "BUY" else entry * (1 + 1 / lev)


def pnl(t: dict, price: float) -> tuple[float, float, float | None]:
    """(money, % price move, R) for a trade at a price."""
    d = sign(t)
    money_ = d * (price - t["entry"]) * t["qty"] * t.get("mult", 1)
    pct = d * (price / t["entry"] - 1) * 100 if t["entry"] else 0.0
    rsk = abs(t["entry"] - t["stop"]) * t["qty"] * t.get("mult", 1)
    return money_, pct, (money_ / rsk if rsk else None)


def roe(t: dict, money_: float) -> float | None:
    m = margin(t)
    return money_ / m * 100 if m else None


def order_type(side: str, entry: float, live: float | None) -> str:
    if live is None or abs(entry - live) / live < 0.0005:
        return "market"
    if side == "BUY":
        return "limit" if entry < live else "stop"
    return "limit" if entry > live else "stop"


def leverage_choices(market: str, spec: risk.Spec, is_index: bool, venue: str) -> list[tuple[str, float | None]]:
    if market == "crypto":
        return [(f"{x}x", float(x)) for x in (1, 2, 3, 5, 10, 20)]
    if market == "fx" and spec.cur == "USD":
        return [(f"1:{x}", float(x)) for x in (10, 30, 100, 200, 500)]
    if market == "stock" and not is_index:
        return [("1x Delivery", 1.0), ("5x Intraday (MIS)", 5.0)]
    return []          # futures-style lots (index, MCX, CDS): margin is set by the exchange


def margin_pct_for(market: str, is_index: bool, venue: str) -> float | None:
    if market == "stock" and is_index:
        return FUTURES_MARGIN["index"]
    if venue in ("mcx", "cds"):
        return FUTURES_MARGIN[venue]
    return None


# ---------------------------------------------------------------- fills, stops, targets

def advance(t: dict, df: pd.DataFrame | None) -> dict | None:
    """Walk the 5-minute candles: fill a pending order, then the first stop / target / liquidation touch.
    Returns the changes to apply, or None. The stop wins when stop and target touch in the same candle."""
    if df is None or df.empty:
        return None
    t = dict(t)
    changes: dict = {}
    d = sign(t)
    for ts, row in df.iterrows():
        if t["status"] == "pending":
            kind = t.get("order", "limit")
            e = t["entry"]
            hit = (row["low"] <= e if kind == "limit" else row["high"] >= e) if d == 1 else \
                  (row["high"] >= e if kind == "limit" else row["low"] <= e)
            if not hit:
                continue
            fill = e
            if kind == "limit" and ((d == 1 and row["open"] < e) or (d == -1 and row["open"] > e)):
                fill = row["open"]           # gapped through a limit: better price
            if kind == "stop" and ((d == 1 and row["open"] > e) or (d == -1 and row["open"] < e)):
                fill = row["open"]           # gapped through a stop entry: worse price
            t.update(status="open", entry=float(fill), opened=ts.timestamp())
            changes.update(status="open", entry=float(fill), opened=ts.timestamp())
            continue                         # stops/targets count from the next candle
        if t["status"] != "open":
            break
        stop = t["stop"]
        liq = t.get("liq")
        reason = "stop-loss hit"
        if liq and ((d == 1 and liq > stop) or (d == -1 and liq < stop)):
            stop, reason = liq, "liquidated"
        adverse = row["low"] if d == 1 else row["high"]
        favour = row["high"] if d == 1 else row["low"]
        if d * (adverse - stop) <= 0:
            changes.update(status="closed", exit=float(stop), exit_ts=ts.timestamp(), reason=reason)
            return changes
        tgt = t.get("target")
        if tgt and d * (favour - tgt) >= 0:
            changes.update(status="closed", exit=float(tgt), exit_ts=ts.timestamp(), reason="target hit")
            return changes
    return changes or None


def first_touch(t: dict, df: pd.DataFrame | None) -> tuple[str, float, float] | None:
    """(reason, price, time) of the first stop/target touch of an open trade (kept for older callers)."""
    ch = advance({**t, "status": t.get("status", "open")}, df)
    if ch and ch.get("status") == "closed":
        return ch["reason"], ch["exit"], ch["exit_ts"]
    return None


# ---------------------------------------------------------------- store

def _trades(doc: dict) -> list[dict]:
    return doc.setdefault("paper", {}).setdefault("trades", [])


def _finish(t: dict) -> dict:
    if t["status"] == "closed" and "exit" in t:
        money_, pct, r = pnl(t, t["exit"])
        t.update(pnl=money_, pnl_pct=pct, r=r, roe=roe(t, money_))
    return t


def place(draft: dict, user_id: str, user_name: str, chat_id: str) -> dict:
    """Save a confirmed ticket as an open trade (market) or a pending order (limit / stop)."""
    limit = int(cfg().get("paper", {}).get("max_open_per_user", 20))
    live = draft.get("live")
    kind = order_type(draft["side"], draft["entry"], live) if not draft.get("market_order") else "market"
    entry = float(live) if kind == "market" and live else float(draft["entry"])
    now = time.time()

    def fn(doc):
        trades = _trades(doc)
        mine = [t for t in trades if t["user_id"] == user_id and t["chat_id"] == chat_id
                and t["status"] in ("open", "pending")]
        if len(mine) >= limit:
            raise PaperError(f"You already have {limit} open or pending paper trades. Close some first.")
        p = doc["paper"]
        p["next_id"] = int(p.get("next_id", 0)) + 1
        t = {"id": p["next_id"], "user_id": user_id, "user_name": user_name[:40], "chat_id": chat_id,
             "market": draft["market"], "symbol": draft["symbol"], "pretty": draft["pretty"], "tf": draft["tf"],
             "side": draft["side"], "entry": entry, "stop": float(draft["sl"]), "target": float(draft["tp"]),
             "qty": float(draft["qty"]), "mult": float(draft["mult"]), "unit": draft["unit"],
             "currency": draft["cur"], "lev": draft.get("lev"), "margin_pct": draft.get("margin_pct"),
             "liq": liquidation(draft["side"], entry, draft.get("lev"), draft["market"]),
             "order": kind, "status": "open" if kind == "market" else "pending",
             "created": now, "opened": now if kind == "market" else None,
             "strategy": draft.get("src", "manual"), "source": draft.get("source", "")}
        trades.append(t)
        return t
    return store.update(fn)


def find(trade_id: int, chat_id: str) -> dict:
    t = next((t for t in _trades(store.load()) if t["id"] == trade_id), None)
    if t is None or t["chat_id"] != chat_id:
        raise PaperError(f"Paper trade #{trade_id} not found in this chat.")
    return t


def _owner_ok(t: dict, user_id: str, owner_id: str) -> None:
    if t["user_id"] != user_id and user_id != owner_id:
        raise PaperError(f"Trade #{t['id']} belongs to {t['user_name']}; only they can change it.")


def _apply(trade_id: int, changes: dict) -> dict | None:
    def fn(doc):
        for t in _trades(doc):
            if t["id"] == trade_id:
                if t["status"] == "closed" or t["status"] == "cancelled":
                    return None
                t.update(changes)
                if "entry" in changes and t.get("lev"):
                    t["liq"] = liquidation(t["side"], t["entry"], t["lev"], t["market"])
                return _finish(t)
        return None
    return store.update(fn)


def exit_trade(trade_id: int, user_id: str, chat_id: str, owner_id: str = "") -> dict:
    t = find(trade_id, chat_id)
    _owner_ok(t, user_id, owner_id)
    if t["status"] == "pending":
        raise PaperError(f"#{trade_id} is a pending order: use 🚫 Cancel instead.")
    if t["status"] != "open":
        raise PaperError(f"Paper trade #{trade_id} is already closed ({t.get('reason', '')}).")
    price = live_price(t["market"], t["symbol"])
    if not price:
        raise PaperError(f"No live price for {t['symbol']} right now; try again in a minute.")
    out = _apply(trade_id, {"status": "closed", "exit": price, "exit_ts": time.time(), "reason": "exited by you"})
    if out is None:
        raise PaperError(f"Paper trade #{trade_id} is already closed.")
    return out


def cancel_order(trade_id: int, user_id: str, chat_id: str, owner_id: str = "") -> dict:
    t = find(trade_id, chat_id)
    _owner_ok(t, user_id, owner_id)
    if t["status"] != "pending":
        raise PaperError(f"#{trade_id} is not a pending order.")
    return _apply(trade_id, {"status": "cancelled", "exit_ts": time.time(), "reason": "cancelled"})


def modify(trade_id: int, user_id: str, chat_id: str, owner_id: str, stop: float | None = None,
           target: float | None = None) -> dict:
    t = find(trade_id, chat_id)
    _owner_ok(t, user_id, owner_id)
    if t["status"] not in ("open", "pending"):
        raise PaperError(f"#{trade_id} is closed.")
    ch = {}
    if stop is not None:
        ch["stop"] = float(stop)
    if target is not None:
        ch["target"] = float(target)
    out = _apply(trade_id, ch)
    if out is None:
        raise PaperError(f"#{trade_id} is closed.")
    return out


def check_stops(chat_id: str, user_id: str | None = None) -> list[dict]:
    """Fill pending orders and close trades whose stop/target/liquidation was touched. Returns events
    (trades that filled or closed), newest state."""
    doc = store.load()
    updates = []
    for t in _trades(doc):
        if t["status"] not in ("open", "pending") or t["chat_id"] != chat_id or (user_id and t["user_id"] != user_id):
            continue
        ch = advance(t, candles_since(t))
        if ch is None:
            price = live_price(t["market"], t["symbol"])
            if price:
                ch = advance(t, pd.DataFrame({"open": [price], "high": [price], "low": [price], "close": [price]},
                                             index=[pd.Timestamp.now(tz="UTC")]))
        if ch:
            updates.append((t["id"], ch))
    events = []
    for tid, ch in updates:
        out = _apply(tid, ch)
        if out:
            events.append(out)
    return events


def _mine(chat_id: str, user_id: str | None, states: tuple) -> list[dict]:
    return [t for t in _trades(store.load()) if t["status"] in states and t["chat_id"] == chat_id
            and (user_id is None or t["user_id"] == user_id)]


def open_trades(chat_id: str, user_id: str | None = None) -> list[dict]:
    return _mine(chat_id, user_id, ("open",))


def pending_orders(chat_id: str, user_id: str | None = None) -> list[dict]:
    return _mine(chat_id, user_id, ("pending",))


def closed_trades(chat_id: str, user_id: str | None = None) -> list[dict]:
    return _mine(chat_id, user_id, ("closed",))


def today_losses(chat_id: str, user_id: str) -> tuple[int, dict]:
    """Losing trades today (IST) and the money lost per currency."""
    today = datetime.now(IST).date()
    n, lost = 0, {}
    for t in closed_trades(chat_id, user_id):
        if datetime.fromtimestamp(t.get("exit_ts", 0), IST).date() != today or t.get("pnl", 0) >= 0:
            continue
        n += 1
        lost[t["currency"]] = lost.get(t["currency"], 0) + abs(t["pnl"])
    return n, lost


def risk_warning(chat_id: str, user_id: str) -> str:
    n, lost = today_losses(chat_id, user_id)
    if n >= int(cfg()["risk"].get("max_losing_trades_per_day", 3)):
        return f"🛑 You have {n} losing trades today — the rule says stop trading for the day."
    for cur, v in lost.items():
        if v >= risk.max_risk(cur):
            return f"🛑 Today's paper losses are {risk.money(v, cur)} — above your {risk.money(risk.max_risk(cur), cur)} " \
                   "maximum; stop for today."
    return ""


# ---------------------------------------------------------------- the ticket (draft)

def _fmt(v: float | None, cur: str = "") -> str:
    if v is None:
        return "–"
    from bot.strategies import _fmt as f
    return ("₹" if cur == "INR" else "") + f(v)


def levels(a, side: str) -> tuple[float, float, float, str]:
    """The bot's own (entry, stop, target, from-what) for a BUY or SELL at the live price."""
    entry = float(a.now_price)
    d = 1 if side == "BUY" else -1
    setups = [s for s in a.setups if s.side == side and s.entry is not None and s.status in ("TRADE", "WATCH")]
    setups.sort(key=lambda s: (s.status == "TRADE", s.backtest.get("avg_r") or -9), reverse=True)
    if setups:
        s = setups[0]
        return entry, entry - d * abs(s.entry - s.stop), entry + d * abs(s.target - s.entry), s.name
    atr = float(a.x["atr14"].iloc[-1])
    r = cfg()["risk"]["target_r"]
    return entry, entry - d * 1.5 * atr, entry + d * r * 1.5 * atr, "1.5 ATR stop / 2R target (no setup on this side)"


CAPITAL_USD = [50, 100, 250, 500]
CAPITAL_INR = [5000, 10000, 25000, 50000]
MONEY_WORDS = ("$", "₹", "rs", "inr", "usd", "usdt", "dollar", "rupee")


def new_draft(a, side: str, recommended: bool) -> dict:
    e, s, t, why = levels(a, side)
    sp = risk.spec_for(a, e)
    lev = leverage_choices(a.market, sp, a.is_index, a.venue)
    d = {"market": a.market, "symbol": a.symbol, "pretty": a.pretty, "tf": a.timeframe, "cb": a.cb,
         "side": side, "live": float(a.now_price), "atr": float(a.x["atr14"].iloc[-1]),
         "rec": {"entry": e, "sl": s, "tp": t, "src": why}, "unit": sp.unit, "mult": sp.mult, "qstep": sp.step,
         "cur": sp.cur, "kind": sp.kind, "lev_choices": lev, "lev": None,
         "margin_pct": margin_pct_for(a.market, a.is_index, a.venue), "source": a.source,
         "entry": None, "sl": None, "tp": None, "qty": None, "cap": None, "size_mode": None,
         "recommended": recommended, "src": "manual"}
    if recommended:
        d.update(entry=e, sl=s, tp=t, market_order=True, src=why)
    d["step"] = steps(d)[0]
    return d


def steps(d: dict) -> list[str]:
    """The ticket's steps in order (depends on how you size the trade)."""
    if d.get("recommended"):
        seq = ["size", "lev", "confirm"]
    else:
        seq = ["entry", "size", "lev", "sl"] + (["risksize"] if d.get("size_mode") == "risk" else []) + ["tp", "confirm"]
    if not d.get("lev_choices"):
        seq.remove("lev")
    return seq


STEP_FIELD = {"entry": "entry", "lev": "lev", "sl": "sl", "risksize": "qty", "tp": "tp"}


def _step_done(d: dict, st: str) -> bool:
    if st == "size":
        return d.get("qty") is not None or d.get("size_mode") in ("risk", "cap")
    return d.get(STEP_FIELD[st]) is not None


def next_step(d: dict) -> None:
    """Move on. After a ✏️ edit from the confirm screen, jump to the first step that still needs a value."""
    if d.get("size_mode") == "cap":
        resolve_capital(d)
    if d.get("edit_back"):
        for f in ("sl", "tp"):                     # a new entry can make the old stop/target the wrong side
            if d.get(f) is not None and d.get("entry") is not None and validate(d, f, d[f]):
                d[f] = None
        todo = [st for st in steps(d) if st != "confirm" and not _step_done(d, st)]
        d["step"] = todo[0] if todo else "confirm"
        if not todo:
            d["edit_back"] = False
        return
    seq = steps(d)
    i = seq.index(d["step"]) if d["step"] in seq else -1
    d["step"] = seq[min(i + 1, len(seq) - 1)]


def entry_price(d: dict) -> float:
    return d["live"] if d.get("market_order") else d["entry"]


def effective_lev(d: dict) -> float:
    """Position value ÷ capital: the leverage, or 1/margin% for futures, or 1."""
    if d.get("lev"):
        return float(d["lev"])
    if d.get("margin_pct"):
        return 1 / d["margin_pct"]
    return 1.0


def capital_used(d: dict) -> float | None:
    if d.get("qty") is None:
        return None
    return abs(entry_price(d) * d["qty"] * d["mult"]) / effective_lev(d)


def qty_from_capital(d: dict, cap: float) -> float:
    return risk.round_qty(cap * effective_lev(d) / (entry_price(d) * d["mult"]), d["qstep"])


def resolve_capital(d: dict) -> str | None:
    """Capital entered + leverage known -> quantity. Error text if the capital is too small."""
    if d.get("size_mode") != "cap" or d.get("cap") is None:
        return None
    if d.get("lev_choices") and not d.get("lev"):
        return None                                    # wait for the leverage step
    q = qty_from_capital(d, d["cap"])
    if q <= 0:
        need = (d["qstep"] or 0) * entry_price(d) * d["mult"] / effective_lev(d)
        d["cap"], d["size_mode"], d["step"] = None, None, "size"
        return (f"That capital is too small for the smallest size" +
                (f" (needs about {risk.money(need, d['cur'])})." if need else "."))
    d["qty"] = q
    return None


def validate(d: dict, field: str, v: float) -> str | None:
    """Error text, or None if the value makes sense for this side."""
    if v <= 0 or not math.isfinite(v):
        return "Please send a positive number."
    up = d["side"] == "BUY"
    e = entry_price(d) if d.get("entry") is not None else None
    if field == "sl" and e is not None and ((up and v >= e) or (not up and v <= e)):
        return f"For a {d['side']} the stop-loss must be {'below' if up else 'above'} the entry {_fmt(e, d['cur'])}."
    if field == "tp" and e is not None and ((up and v <= e) or (not up and v >= e)):
        return f"For a {d['side']} the target must be {'above' if up else 'below'} the entry {_fmt(e, d['cur'])}."
    if field in ("qty", "risksize") and d["qstep"] and d["qstep"] >= 1 and abs(v - round(v)) > 1e-9:
        return f"Use whole {d['unit']}s here."
    if field == "entry" and d.get("live") and abs(v / d["live"] - 1) > 0.5:
        return f"That is more than 50% away from the live price {_fmt(d['live'], d['cur'])}. Check the number."
    return None


def parse_number(text: str) -> float | None:
    """'64,250.5', '$25', '₹5000', '-25', '1:100', '5x' -> number (sign dropped)."""
    t = text.strip().lower().replace(",", "").replace(" ", "")
    for w in sorted(MONEY_WORDS, key=len, reverse=True):
        t = t.replace(w, "")
    t = t.removeprefix("1:").rstrip("x").lstrip("+-−")
    try:
        return float(t)
    except ValueError:
        return None


def is_money(text: str) -> bool:
    t = text.strip().lower()
    return any(w in t for w in MONEY_WORDS)


def risk_of(d: dict) -> tuple[float, float, float | None]:
    """(money at risk, money reward, R:R) for the ticket's current numbers."""
    if None in (d.get("entry"), d.get("sl"), d.get("qty")):
        return 0.0, 0.0, None
    e = entry_price(d)
    rk = abs(e - d["sl"]) * d["qty"] * d["mult"]
    rw = abs(d["tp"] - e) * d["qty"] * d["mult"] if d.get("tp") else 0.0
    return rk, rw, (rw / rk if rk else None)


def _spec(d: dict) -> risk.Spec:
    return risk.Spec(d["unit"], d["mult"], d["qstep"], d["cur"], d["kind"])


def size_buttons(d: dict) -> list[tuple[str, str]]:
    """Risk ladder → quantity buttons (needs a stop-loss)."""
    e = entry_price(d)
    per_unit = abs(e - d["sl"]) * d["mult"]
    out = []
    sp = _spec(d)
    for b in risk.tiers(d["cur"]):
        q = risk.round_qty(b / per_unit, d["qstep"]) if per_unit else 0
        if q <= 0:
            continue
        rw = abs(d["tp"] - e) * q * d["mult"] if d.get("tp") else 0
        out.append((f"🪜 {risk.money(b, d['cur'])} risk → {risk.fmt_qty(q, sp)}"
                    + (f" (🎯 {risk.money(rw, d['cur'], True)})" if rw else ""), f"pt:v:{q:g}"))
    if not out and d["qstep"]:
        out.append((f"Smallest: {risk.fmt_qty(d['qstep'], sp)} (risk {risk.money(per_unit * d['qstep'], d['cur'])})",
                    f"pt:v:{d['qstep']:g}"))
    return out


def summary_line(d: dict) -> str:
    """'So far' line shown on every step."""
    cur, sp = d["cur"], _spec(d)
    bits = []
    if d.get("entry") is not None:
        bits.append(("⚡ market " if d.get("market_order") else "Entry ") + _fmt(entry_price(d), cur))
    if d.get("qty") is not None:
        cu = capital_used(d)
        bits.append(f"📦 {risk.fmt_qty(d['qty'], sp)}" + (f" (capital {risk.money(cu, cur)})" if cu else ""))
    elif d.get("cap") is not None:
        bits.append(f"💼 capital {risk.money(d['cap'], cur)}")
    elif d.get("size_mode") == "risk":
        bits.append("📦 size from risk (after stop-loss)")
    if d.get("lev"):
        bits.append(("1:%g" if d["market"] == "fx" else "%gx") % d["lev"])
    if d.get("sl") is not None:
        bits.append(f"🛑 {_fmt(d['sl'], cur)}")
    if d.get("tp") is not None:
        bits.append(f"🎯 {_fmt(d['tp'], cur)}")
    return " · ".join(bits)


def step_prompt(d: dict) -> tuple[str, list]:
    """Text and buttons for the ticket's current step."""
    if d["step"] == "confirm":
        return confirm_text(d), confirm_buttons()
    cur, side = d["cur"], d["side"]
    icon = "🟢" if side == "BUY" else "🔴"
    sp = _spec(d)
    seq = [s for s in steps(d) if s != "confirm"]
    n = f"{seq.index(d['step']) + 1}/{len(seq)}" if d["step"] in seq else "✏️"
    title = {"entry": "Entry price", "size": "Capital or quantity", "lev": "Leverage", "sl": "Stop-loss",
             "risksize": "Size from risk", "tp": "Take profit"}[d["step"]]
    so_far = summary_line(d)
    head = (f"📝 <b>PAPER {side} · {html.escape(d['pretty'])}</b> {icon} · {d['tf']} · live {_fmt(d['live'], cur)}\n"
            + (f"<i>{so_far}</i>\n" if so_far else "") + f"<b>Step {n} · {title}</b>")
    rec, st, sym = d["rec"], d["step"], ("₹" if cur == "INR" else "$")
    rows: list = []
    e = entry_price(d) if d.get("entry") is not None else None
    k = 1 if side == "BUY" else -1
    q = d.get("qty")
    if st == "entry":
        body = ("Send your <b>entry price</b>, or tap ⚡ market.\n"
                "A price below/above the live price becomes a pending <b>limit</b> or <b>stop</b> order.")
        rows = [[(f"⚡ Market {_fmt(d['live'], cur)}", "pt:v:mkt"), (f"🤖 Bot {_fmt(rec['entry'], cur)}", "pt:v:rec")]]
    elif st == "size":
        unit = {"shares": "shares", "lots": "lots", "coins": d["unit"], "units": "units"}[d["kind"]]
        body = (f"Send a <b>quantity</b> in {unit} (e.g. <code>{'2' if d['qstep'] >= 1 else '0.05'}</code>) "
                f"or the <b>capital</b> to put in with {sym} (e.g. <code>{sym}{CAPITAL_INR[1] if cur == 'INR' else 200}</code>)."
                + (f"\n1 lot = {d['mult']:g} units." if d["kind"] == "lots" and cur == "INR" else "")
                + ("\nCapital × leverage = position value; the bot works out the quantity." if d.get("lev_choices")
                   or d.get("margin_pct") else ""))
        if d.get("sl") is not None:
            rows += [[b] for b in size_buttons(d)]
        else:
            rows.append([("🪜 Size from risk ($10–$100) — after stop-loss" if cur != "INR" else
                          "🪜 Size from risk (₹1,000–₹8,500) — after stop-loss", "pt:v:byrisk")])
        caps = CAPITAL_INR if cur == "INR" else CAPITAL_USD
        rows.append([(f"💼 {risk.money(c, cur)}", f"pt:v:C{c}") for c in caps])
        if d["kind"] == "lots" and d["qstep"] >= 1:
            rows.append([(f"{x} lot{'s' if x > 1 else ''}", f"pt:v:{x}") for x in (1, 2, 5)])
    elif st == "lev":
        body = "Choose <b>leverage</b> (it changes the capital used, not the loss at your stop):"
        btns = []
        for lbl, v in d["lev_choices"]:
            extra = ""
            if d.get("cap") is not None:
                qq = risk.round_qty(d["cap"] * v / (e * d["mult"]), d["qstep"])
                extra = f" → {risk.fmt_qty(qq, sp)}" if qq > 0 else " → too small"
            elif q is not None:
                extra = f" → {risk.money(abs(e * q * d['mult']) / v, cur)}"
            btns.append((lbl + extra, f"pt:v:L{v:g}"))
        rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    elif st == "sl":
        body = f"Send the <b>stop-loss price</b>" + (f", or the most you accept to lose with {sym} (e.g. "
                                                    f"<code>{sym}{risk.tiers(cur)[0]:g}</code>)" if q else "") + ":"
        rows = [[(f"🤖 Bot {_fmt(rec['sl'] - rec['entry'] + e, cur)}", "pt:v:rec")],
                [(f"{m:g} ATR {_fmt(e - k * m * d['atr'], cur)}", f"pt:v:{e - k * m * d['atr']:.10g}") for m in (1, 1.5, 2)]]
        if q:
            per = q * d["mult"]
            rows.append([(f"−{risk.money(b, cur)} → {_fmt(e - k * b / per, cur)}", f"pt:v:M{b:g}")
                         for b in risk.tiers(cur)])
    elif st == "risksize":
        body = (f"Stop {_fmt(d['sl'], cur)}. Pick how much to <b>risk</b> (the bot sets the size) or send a quantity:")
        rows = [[b] for b in size_buttons(d)]
    else:  # tp
        rr = abs(e - d["sl"])
        body = f"Send the <b>take-profit price</b>" + (f", or the profit you want with {sym} (e.g. "
                                                      f"<code>{sym}{risk.tiers(cur)[1] * 2:g}</code>)" if q else "") + ":"
        rows = [[(f"🤖 Bot {_fmt(rec['tp'] - rec['entry'] + e, cur)}", "pt:v:rec")],
                [(f"1:{m:g} → {_fmt(e + k * m * rr, cur)}", f"pt:v:{e + k * m * rr:.10g}") for m in (1.5, 2, 3)]]
        if q:
            per = q * d["mult"]
            rows.append([(f"+{risk.money(b * 2, cur)} → {_fmt(e + k * b * 2 / per, cur)}", f"pt:v:M{b * 2:g}")
                         for b in risk.tiers(cur)])
    rows.append([("✖ Cancel", "pt:q")])
    return f"{head}\n{body}", rows


def _money_to_price(d: dict, amount: float, field: str) -> float | None:
    q = d.get("qty")
    if not q:
        return None
    k = 1 if d["side"] == "BUY" else -1
    move = amount / (q * d["mult"])
    e = entry_price(d)
    return e - k * move if field == "sl" else e + k * move


def apply_value(d: dict, raw: str) -> str | None:
    """Use a typed value or a tapped suggestion for the current step. Returns an error text or None."""
    st, raw = d["step"], raw.strip()
    money_in = is_money(raw) or (raw[:1] in ("C", "M") and parse_number(raw[1:]) is not None)
    if st == "entry":
        if raw == "mkt":
            d.update(entry=d["live"], market_order=True)
        else:
            if money_in and raw != "rec":
                return "Entry needs a price. The capital is asked in the next step."
            v = d["rec"]["entry"] if raw == "rec" else parse_number(raw)
            if v is None:
                return "Please send a price (e.g. 64250.5) or tap ⚡ Market."
            err = validate(d, "entry", v)
            if err:
                return err
            mkt = order_type(d["side"], v, d["live"]) == "market"
            d.update(entry=d["live"] if mkt else v, market_order=mkt)
        next_step(d)
        return None
    if st == "size":
        if raw == "byrisk":
            d.update(size_mode="risk", qty=None, cap=None)
        elif money_in:
            v = parse_number(raw[1:] if raw[:1] == "C" else raw)
            if not v or v <= 0:
                return "Please send the capital as a number, e.g. $200."
            d.update(size_mode="cap", cap=v, qty=None)
            err = resolve_capital(d)
            if err:
                return err
        else:
            v = parse_number(raw)
            if v is None:
                return "Please send a quantity (e.g. 0.05) or a capital amount with $ / ₹ (e.g. $200)."
            err = validate(d, "qty", v)
            if err:
                return err
            q = risk.round_qty(v, d["qstep"]) if d["qstep"] else v
            if q <= 0:
                return f"The smallest size here is {d['qstep']:g}."
            d.update(size_mode="qty", qty=q, cap=None)
        next_step(d)
        return None
    if st == "lev":
        v = parse_number(raw[1:] if raw.startswith("L") else raw)
        if not v or v < 1:
            return "Pick a leverage button or send a number like 5."
        d["lev"] = v
        err = resolve_capital(d)
        if err:
            return err
        next_step(d)
        return None
    if st in ("sl", "tp"):
        if raw == "rec":                    # bot's distance, moved to your entry
            v = d["rec"][st] - d["rec"]["entry"] + entry_price(d)
        elif money_in:
            amt = parse_number(raw[1:] if raw[:1] == "M" else raw)
            if not amt:
                return "Please send the amount as a number, e.g. $25."
            v = _money_to_price(d, amt, st)
            if v is None:
                return "Send a price here: the size is set from your risk after this step."
        else:
            v = parse_number(raw)
        if v is None:
            return "Please send a price (e.g. 64250.5)" + (" or an amount with $ / ₹." if d.get("qty") else ".")
        err = validate(d, st, v)
        if err:
            return err
        d[st] = v
        next_step(d)
        return None
    if st == "risksize":
        v = parse_number(raw)
        if v is None:
            return "Tap a risk button or send a quantity."
        err = validate(d, "risksize", v)
        if err:
            return err
        d["qty"] = risk.round_qty(v, d["qstep"]) if d["qstep"] else v
        next_step(d)
        return None
    return None


def confirm_text(d: dict) -> str:
    cur, side = d["cur"], d["side"]
    icon = "🟢" if side == "BUY" else "🔴"
    e = entry_price(d)
    kind = "market" if d.get("market_order") else order_type(side, e, d["live"])
    sp = _spec(d)
    rk, rw, rr = risk_of(d)
    nt = abs(e * d["qty"] * d["mult"])
    lev = d.get("lev")
    mg = capital_used(d) or nt
    liq = liquidation(side, e, lev, d["market"])
    up = side == "BUY"
    pct = lambda v: (v / e - 1) * 100  # noqa: E731
    order_txt = {"market": f"⚡ Market order at the live price {_fmt(e, cur)}",
                 "limit": f"⏳ {side} LIMIT at {_fmt(e, cur)} — waits until price comes to it (live {_fmt(d['live'], cur)})",
                 "stop": f"⏳ {side} STOP at {_fmt(e, cur)} — triggers when price breaks there (live {_fmt(d['live'], cur)})"}[kind]
    lev_txt = ("1:%g" if d["market"] == "fx" else "%gx") % lev if lev else ""
    lines = [f"📝 <b>CONFIRM PAPER {side}</b> {icon} · {html.escape(d['pretty'])} · {d['tf']}", LINE,
             order_txt,
             f"🛑 Stop-loss <b>{_fmt(d['sl'], cur)}</b> ({pct(d['sl']):+.2f}%) → loss <b>{risk.money(rk, cur)}</b>",
             f"🎯 Take profit <b>{_fmt(d['tp'], cur)}</b> ({pct(d['tp']):+.2f}%) → profit <b>{risk.money(rw, cur)}</b>",
             LINE]
    if d.get("size_mode") == "cap" and d.get("cap") is not None:
        lines.append(f"💼 Capital entered <b>{risk.money(d['cap'], cur)}</b>"
                     + (f" × {lev_txt}" if lev else (f" (futures margin ≈{d['margin_pct'] * 100:.0f}%)"
                                                     if d.get("margin_pct") else ""))
                     + f" → <b>{'BUY' if up else 'SELL'} {risk.fmt_qty(d['qty'], sp)}</b>")
    else:
        lines.append(f"📦 Quantity <b>{risk.fmt_qty(d['qty'], sp)}</b>" + (f" × {lev_txt}" if lev else ""))
    lines.append(f"💼 Capital used <b>{risk.money(mg, cur)}</b> · position value {risk.money(nt, cur)}"
                 + (" (futures margin is approximate; your broker sets it)" if d.get("margin_pct") and not lev else ""))
    lines.append(f"⚖️ R:R <b>1:{rr:.2f}</b>" if rr else "⚖️ R:R –")
    if mg:
        lines.append(f"📈 At target {risk.money(rw, cur, True)} = {rw / mg * 100:+.1f}% on capital · at stop "
                     f"{risk.money(-rk, cur, True)} = {-rk / mg * 100:+.1f}%")
    checks = []
    if liq:
        ok = (liq < d["sl"]) if up else (liq > d["sl"])
        checks.append(("✅" if ok else "⚠️") + f" Liquidation ≈ {_fmt(liq, cur)} "
                      + ("(beyond your stop)" if ok else "— BEFORE your stop: lower the leverage"))
    if rr is not None:
        checks.append(("✅" if rr >= 1.5 else "⚠️") + f" Reward:risk 1:{rr:.2f}" + ("" if rr >= 1.5 else " — below 1:1.5"))
    top = risk.max_risk(cur)
    checks.append(("✅" if rk <= top else "⚠️") + f" Loss at stop {risk.money(rk, cur)} vs your {risk.money(top, cur)} maximum")
    lines += [""] + checks
    lines.append(f"<i>Bot's levels came from: {html.escape(d['rec']['src'])}. Paper money only.</i>")
    return "\n".join(lines)


def confirm_buttons() -> list:
    return [[("✅ Confirm trade", "pt:k")],
            [("✏️ Entry", "pt:g:entry"), ("✏️ Size", "pt:g:size"), ("✏️ SL", "pt:g:sl"), ("✏️ TP", "pt:g:tp")],
            [("✖ Cancel", "pt:q")]]


# ---------------------------------------------------------------- messages

def _age(ts: float | None) -> str:
    if not ts:
        return "–"
    m = int((time.time() - ts) / 60)
    return f"{m}m" if m < 60 else f"{m // 60}h {m % 60}m" if m < 48 * 60 else f"{m // 1440}d"


def _lev_txt(t: dict) -> str:
    lev = t.get("lev")
    if not lev:
        return ""
    return f" · 1:{lev:g}" if t["market"] == "fx" else f" · {lev:g}x"


def _qty_txt(t: dict) -> str:
    sp = risk.Spec(t.get("unit", ""), t.get("mult", 1), 1.0 if t.get("unit", "").startswith(("share", "lot")) else
                   (0.01 if t["market"] == "fx" else 0.0), t["currency"], "")
    return risk.fmt_qty(t["qty"], sp)


def trade_block(t: dict, price: float | None) -> str:
    cur = t["currency"]
    side_icon = "🟢" if t["side"] == "BUY" else "🔴"
    head = (f"<b>#{t['id']}</b> {side_icon} <b>{t['side']} {html.escape(t['pretty'])}</b> · {_qty_txt(t)}"
            f"{_lev_txt(t)} · {t['tf']}")
    if t["status"] == "pending":
        away = f" ({(t['entry'] / price - 1) * 100:+.2f}% away)" if price else ""
        return "\n".join([head,
                          f"   ⏳ {t['order'].upper()} at <b>{_fmt(t['entry'], cur)}</b> · live {_fmt(price, cur)}{away}",
                          f"   🛑 {_fmt(t['stop'], cur)} · 🎯 {_fmt(t.get('target'), cur)} · placed {_age(t['created'])} ago"])
    if price is None:
        return head + f"\n   Entry {_fmt(t['entry'], cur)} · live price unavailable"
    money_, pct, r = pnl(t, price)
    on_m = roe(t, money_)
    tone = "🟢" if money_ > 0 else "🔴" if money_ < 0 else "⚪"
    up = t["side"] == "BUY"
    to_sl = (price - t["stop"]) / price * 100 * (1 if up else -1)
    to_tp = ((t["target"] - price) / price * 100 * (1 if up else -1)) if t.get("target") else None
    lines = [head,
             f"   {tone} <b>{risk.money(money_, cur, True)}</b> · price {pct:+.2f}%"
             + (f" · on margin {on_m:+.1f}%" if on_m is not None and t.get("lev") else "")
             + (f" · {r:+.2f}R" if r is not None else ""),
             f"   Entry {_fmt(t['entry'], cur)} → live <b>{_fmt(price, cur)}</b> · {_age(t.get('opened'))}",
             f"   🛑 {_fmt(t['stop'], cur)} ({to_sl:.2f}% away)"
             + (f" · 🎯 {_fmt(t['target'], cur)} ({to_tp:.2f}% away)" if to_tp is not None else "")
             + (f" · 💀 liq {_fmt(t['liq'], cur)}" if t.get("liq") else "")]
    return "\n".join(lines)


def status_text(trades: list[dict], prices: dict, title: str, events: list[dict], pending=None) -> str:
    pending = pending or []
    now = datetime.now(IST).strftime("%d %b, %H:%M IST")
    lines = [f"💼 <b>{title}</b>", f"<i>{now} · live prices · paper money only</i>", LINE]
    for c in events:
        if c["status"] == "closed":
            icon = {"target hit": "🎯", "liquidated": "💀"}.get(c["reason"], "🛑")
            lines.append(f"{icon} #{c['id']} {html.escape(c['pretty'])} closed — {c['reason']} at "
                         f"{_fmt(c['exit'], c['currency'])} → <b>{risk.money(c['pnl'], c['currency'], True)}</b> "
                         f"({c['pnl_pct']:+.2f}%)")
        elif c["status"] == "open":
            lines.append(f"✅ #{c['id']} {html.escape(c['pretty'])} order filled at {_fmt(c['entry'], c['currency'])}")
    if events:
        lines.append(LINE)
    if not trades and not pending:
        lines.append("No open paper trades. Analyse any coin, stock or pair and tap 📝 Paper trade.")
    tot: dict[str, float] = {}
    if trades:
        lines.append(f"🟢 <b>OPEN POSITIONS ({len(trades)})</b>")
        for t in trades:
            p = prices.get(t["id"])
            lines += [trade_block(t, p), ""]
            if p is not None:
                tot[t["currency"]] = tot.get(t["currency"], 0) + pnl(t, p)[0]
    if pending:
        lines.append(f"⏳ <b>PENDING ORDERS ({len(pending)})</b>")
        for t in pending:
            lines += [trade_block(t, prices.get(t["id"])), ""]
    if tot:
        lines.append(LINE)
        lines.append("Σ Open P&amp;L: " + " · ".join(f"{'🟢' if v >= 0 else '🔴'} {risk.money(v, c, True)}"
                                                      for c, v in tot.items()))
    return "\n".join(lines).rstrip()


def stats(closed: list[dict]) -> dict:
    wins = [t for t in closed if t.get("pnl", 0) > 0]
    by_cur: dict[str, float] = {}
    for t in closed:
        by_cur[t["currency"]] = by_cur.get(t["currency"], 0) + t.get("pnl", 0)
    rs = [t["r"] for t in closed if t.get("r") is not None]
    return {"n": len(closed), "win": (len(wins) / len(closed) * 100) if closed else None, "by_cur": by_cur,
            "avg_r": (sum(rs) / len(rs)) if rs else None}


def history_text(closed: list[dict], who: str) -> str:
    st = stats(closed)
    lines = [f"📜 <b>PAPER TRADE HISTORY</b> · {html.escape(who)}", LINE]
    if not closed:
        return "\n".join(lines + ["No closed paper trades yet."])
    lines.append(f"Trades {st['n']} · win rate {st['win']:.0f}%" + (f" · avg {st['avg_r']:+.2f}R"
                                                                     if st["avg_r"] is not None else ""))
    lines.append("Total: " + " · ".join(f"{'🟢' if v >= 0 else '🔴'} {risk.money(v, c, True)}"
                                        for c, v in st["by_cur"].items()))
    lines.append("")
    for t in sorted(closed, key=lambda t: t.get("exit_ts", 0), reverse=True)[:12]:
        icon = "🟢" if t.get("pnl", 0) > 0 else "🔴"
        when = datetime.fromtimestamp(t.get("exit_ts", 0), IST).strftime("%d %b %H:%M")
        lines.append(f"{icon} #{t['id']} {html.escape(t['pretty'])} {t['side']}{_lev_txt(t)} "
                     f"{_fmt(t['entry'], t['currency'])}→{_fmt(t['exit'], t['currency'])} "
                     f"<b>{risk.money(t['pnl'], t['currency'], True)}</b> ({t['pnl_pct']:+.2f}%) · "
                     f"{html.escape(t['reason'])} · {when}")
    return "\n".join(lines)


def board_text(chat_id: str) -> str:
    people: dict[str, dict] = {}
    for t in _trades(store.load()):
        if t["chat_id"] != chat_id:
            continue
        p = people.setdefault(t["user_id"], {"name": t["user_name"], "open": 0, "closed": [], "pct": 0.0})
        if t["status"] in ("open", "pending"):
            p["open"] += 1
        elif t["status"] == "closed":
            p["closed"].append(t)
            p["pct"] += t.get("pnl_pct", 0)
    lines = ["🏆 <b>PAPER TRADING BOARD</b> (this chat)", LINE]
    if not people:
        return "\n".join(lines + ["Nobody has paper-traded here yet."])
    ranked = sorted(people.values(), key=lambda p: p["pct"], reverse=True)
    medals = ["🥇", "🥈", "🥉"]
    for i, p in enumerate(ranked):
        st = stats(p["closed"])
        win = f"win {st['win']:.0f}%" if st["win"] is not None else "no closed trades"
        lines.append(f"{medals[i] if i < 3 else f'{i + 1}.'} <b>{html.escape(p['name'])}</b> — "
                     f"{'🟢' if p['pct'] >= 0 else '🔴'} {p['pct']:+.2f}% summed · {st['n']} closed · {win} · "
                     f"{p['open']} open")
    lines.append("<i>Sum of % price moves of closed paper trades.</i>")
    return "\n".join(lines)


def ticket_text(a) -> str:
    """First screen after 📝 Paper trade: the bot's levels for both sides + the risk ladder."""
    from bot import report

    lines = [f"📝 <b>PAPER TRADE · {html.escape(a.pretty)}</b> · {a.timeframe}",
             f"Live <b>{report.price_txt(a, a.now_price)}</b> · {html.escape(a.source)}",
             f"Chart verdict: <b>{html.escape(report.verdict(a))}</b>", LINE, "🤖 <b>Bot's suggestion</b>"]
    for side in ("BUY", "SELL"):
        e, s, t, why = levels(a, side)
        rr = abs(t - e) / abs(e - s) if e != s else 0
        icon = "🟢" if side == "BUY" else "🔴"
        lines.append(f"{icon} <b>{side}</b> {report.price_txt(a, e)} · 🛑 {report.price_txt(a, s)} · "
                     f"🎯 {report.price_txt(a, t)} · R:R 1:{rr:.1f}")
        lines.append(f"   <i>{html.escape(why)}</i>")
    best_side = "BUY" if a.direction in ("BUY", "MIXED", "NONE") or (a.direction == "FORMING" and
                                                                     a.forming_side == "BUY") else "SELL"
    e, s, t, _ = levels(a, best_side)
    lines += [""] + risk.ladder_lines(a, e, s, t)
    lines += ["", "Tap <b>🤖 bot levels</b> (you only pick size and leverage), or <b>✍️ my own</b> to set entry, "
                  "stop-loss and target yourself."]
    return "\n".join(lines)

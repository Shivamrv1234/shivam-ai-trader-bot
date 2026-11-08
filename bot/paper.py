"""Paper trading inside Telegram (private chat or a group): no real orders, ever.

- Open from any analysis (📝 Paper trade → 🟢 BUY / 🔴 SELL) at the LIVE price. Stop-loss and target come
  from the strategy setup on that chart (same distances), or 1.5 ATR / 2R when there is no setup on
  that side. Size follows your risk setting (0.5% of capital per trade by default).
- Many trades at once; each member of a group has their own trades.
- Status shows live price, P&L in % and money, R multiple and distance to stop/target. Stops and targets
  are checked on 5-minute candles since the trade was opened, so a stop hit while you were away still
  counts (stop first when both were touched in the same candle).
- Exit any trade at the live price with ❌ Exit. History shows win rate and totals; /board ranks the group.
- Risk rules from the playbook: warns after 3 losing trades in a day or a 2% daily loss.
"""
from __future__ import annotations

import html
import logging
import math
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from bot import data, store
from bot.config import capital, cfg

log = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")
LINE = "━━━━━━━━━━━━━━━━━━"
_PRICE: dict = {}


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
    """5-minute candles after the trade was opened (to see if the stop or target was touched)."""
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
    opened = pd.Timestamp(t["opened"], unit="s", tz="UTC")
    return df[df.index >= opened.floor("5min") + pd.Timedelta(minutes=5)]


def first_touch(t: dict, df: pd.DataFrame | None) -> tuple[str, float, float] | None:
    """(reason, price, time) of the first stop/target touch after opening, stop first on a tie."""
    if df is None or df.empty:
        return None
    up = t["side"] == "BUY"
    for ts, row in df.iterrows():
        hit_stop = row["low"] <= t["stop"] if up else row["high"] >= t["stop"]
        hit_tgt = t.get("target") and (row["high"] >= t["target"] if up else row["low"] <= t["target"])
        if hit_stop:
            return "stop-loss hit", t["stop"], ts.timestamp()
        if hit_tgt:
            return "target hit", t["target"], ts.timestamp()
    return None


# ---------------------------------------------------------------- maths

def pnl(t: dict, price: float) -> tuple[float, float, float | None]:
    """(money, %, R) for a trade at a price."""
    d = 1 if t["side"] == "BUY" else -1
    money = d * (price - t["entry"]) * t["qty"] * t.get("mult", 1)
    pct = d * (price / t["entry"] - 1) * 100 if t["entry"] else 0.0
    risk = abs(t["entry"] - t["stop"]) * t["qty"] * t.get("mult", 1)
    return money, pct, (money / risk if risk else None)


def default_qty(a, entry: float, stop: float) -> tuple[float, float, str]:
    """(qty, multiplier, unit text) from the risk setting."""
    from bot import report

    risk_pct = cfg()["risk"]["risk_per_trade_pct"]
    if a.market == "fx" and a.asset is not None:
        from bot import fx

        cap, cur = capital("fx", a.currency)
        got = fx.size_lots(a.asset, entry, stop, cap * risk_pct / 100)
        if got and a.asset.lot_units:
            lots = max(math.floor(got[0]) if a.asset.indian else math.floor(got[0] * 100) / 100,
                       1 if a.asset.indian else 0.01)
            return lots, a.asset.lot_units, "lot" if a.asset.indian else "lot"
        return 1.0, 1.0, "unit"
    cap, cur = capital(a.market)
    unit_risk = abs(entry - stop)
    if not unit_risk:
        return 1.0, 1.0, "unit"
    qty = min(cap * risk_pct / 100 / unit_risk, cap / entry)
    if a.market == "stock":
        return float(max(math.floor(qty), 1)), 1.0, "share"
    unit = a.short if a.market == "crypto" else "unit"
    return float(f"{qty:.4g}"), 1.0, unit


def levels(a, side: str) -> tuple[float, float, float, str]:
    """(entry, stop, target, from-what) at the live price for a BUY or SELL paper trade."""
    entry = float(a.now_price)
    d = 1 if side == "BUY" else -1
    setups = [s for s in a.setups if s.side == side and s.entry is not None and s.status in ("TRADE", "WATCH")]
    setups.sort(key=lambda s: (s.status == "TRADE", s.backtest.get("avg_r") or -9), reverse=True)
    if setups:
        s = setups[0]
        risk = abs(s.entry - s.stop)
        reward = abs(s.target - s.entry)
        return entry, entry - d * risk, entry + d * reward, s.name
    atr = float(a.x["atr14"].iloc[-1])
    r = cfg()["risk"]["target_r"]
    return entry, entry - d * 1.5 * atr, entry + d * r * 1.5 * atr, "1.5 ATR stop / 2R target (no setup on this side)"


# ---------------------------------------------------------------- store operations

def _trades(doc: dict) -> list[dict]:
    return doc.setdefault("paper", {}).setdefault("trades", [])


def open_trade(a, side: str, user_id: str, user_name: str, chat_id: str, qty: float | None = None) -> dict:
    entry, stop, target, why = levels(a, side)
    q, mult, unit = default_qty(a, entry, stop)
    if qty:
        q = float(qty)
    limit = int(cfg().get("paper", {}).get("max_open_per_user", 20))

    def fn(doc):
        trades = _trades(doc)
        mine = [t for t in trades if t["user_id"] == user_id and t["chat_id"] == chat_id and t["status"] == "open"]
        if len(mine) >= limit:
            raise PaperError(f"You already have {limit} open paper trades. Exit some first.")
        p = doc["paper"]
        p["next_id"] = int(p.get("next_id", 0)) + 1
        t = {"id": p["next_id"], "user_id": user_id, "user_name": user_name[:40], "chat_id": chat_id,
             "market": a.market, "symbol": a.symbol, "pretty": a.pretty, "tf": a.timeframe, "side": side,
             "entry": entry, "stop": stop, "target": target, "qty": q, "mult": mult, "unit": unit,
             "currency": a.currency, "opened": time.time(), "strategy": why, "status": "open",
             "source": a.source}
        trades.append(t)
        return t
    return store.update(fn)


def _close(doc: dict, trade_id: int, price: float, reason: str, when: float | None = None) -> dict | None:
    for t in _trades(doc):
        if t["id"] == trade_id and t["status"] == "open":
            t.update(status="closed", exit=price, exit_ts=when or time.time(), reason=reason)
            money, pct, r = pnl(t, price)
            t.update(pnl=money, pnl_pct=pct, r=r)
            return t
    return None


def exit_trade(trade_id: int, user_id: str, chat_id: str, owner_id: str = "") -> dict:
    doc = store.load()
    t = next((t for t in _trades(doc) if t["id"] == trade_id), None)
    if t is None or t["chat_id"] != chat_id:
        raise PaperError(f"Paper trade #{trade_id} not found in this chat.")
    if t["status"] != "open":
        raise PaperError(f"Paper trade #{trade_id} is already closed ({t.get('reason', '')}).")
    if t["user_id"] != user_id and user_id != owner_id:
        raise PaperError(f"Trade #{trade_id} belongs to {t['user_name']}; only they can exit it.")
    price = live_price(t["market"], t["symbol"])
    if not price:
        raise PaperError(f"No live price for {t['symbol']} right now; try again in a minute.")
    out = store.update(lambda d: _close(d, trade_id, price, "exited by you"))
    if out is None:
        raise PaperError(f"Paper trade #{trade_id} is already closed.")
    return out


def check_stops(chat_id: str, user_id: str | None = None) -> list[dict]:
    """Close open trades whose stop or target was touched since they were opened. Returns those closed."""
    doc = store.load()
    hits = []
    for t in _trades(doc):
        if t["status"] != "open" or t["chat_id"] != chat_id or (user_id and t["user_id"] != user_id):
            continue
        hit = first_touch(t, candles_since(t))
        if hit is None:
            price = live_price(t["market"], t["symbol"])
            up = t["side"] == "BUY"
            if price and ((price <= t["stop"]) if up else (price >= t["stop"])):
                hit = ("stop-loss hit", t["stop"], time.time())
            elif price and t.get("target") and ((price >= t["target"]) if up else (price <= t["target"])):
                hit = ("target hit", t["target"], time.time())
        if hit:
            hits.append((t["id"], hit))
    closed = []
    if hits:
        def fn(d):
            out = []
            for tid, (reason, price, when) in hits:
                c = _close(d, tid, price, reason, when)
                if c:
                    out.append(c)
            return out
        closed = store.update(fn)
    return closed


def open_trades(chat_id: str, user_id: str | None = None) -> list[dict]:
    return [t for t in _trades(store.load()) if t["status"] == "open" and t["chat_id"] == chat_id
            and (user_id is None or t["user_id"] == user_id)]


def closed_trades(chat_id: str, user_id: str | None = None) -> list[dict]:
    return [t for t in _trades(store.load()) if t["status"] == "closed" and t["chat_id"] == chat_id
            and (user_id is None or t["user_id"] == user_id)]


def today_losses(chat_id: str, user_id: str) -> tuple[int, float, str]:
    """Losing trades and loss % of capital today (IST), for the daily risk rules."""
    today = datetime.now(IST).date()
    n, pct, cur = 0, 0.0, ""
    for t in closed_trades(chat_id, user_id):
        if datetime.fromtimestamp(t.get("exit_ts", 0), IST).date() != today or t.get("pnl", 0) >= 0:
            continue
        n += 1
        cap, cur = capital(t["market"], t.get("currency"))
        pct += abs(t["pnl"]) / cap * 100
    return n, pct, cur


def risk_warning(chat_id: str, user_id: str) -> str:
    r = cfg()["risk"]
    n, pct, _ = today_losses(chat_id, user_id)
    if n >= int(r.get("max_losing_trades_per_day", 3)):
        return f"🛑 You have {n} losing trades today — the rule says stop trading for the day."
    if pct >= float(r.get("max_daily_loss_pct", 2.0)):
        return f"🛑 Today's paper losses are {pct:.1f}% of capital — above the daily limit; stop for today."
    return ""


# ---------------------------------------------------------------- messages

def _price(t: dict, v: float | None) -> str:
    if v is None:
        return "–"
    from bot.strategies import _fmt
    return ("₹" if t.get("currency") == "INR" else "") + _fmt(v)


def _money(v: float, cur: str) -> str:
    sym = "₹" if cur == "INR" else ""
    unit = "" if cur == "INR" else f" {cur}"
    return f"{'+' if v >= 0 else '−'}{sym}{abs(v):,.2f}{unit}"


def _age(ts: float) -> str:
    m = int((time.time() - ts) / 60)
    return f"{m}m" if m < 60 else f"{m // 60}h {m % 60}m" if m < 48 * 60 else f"{m // 1440}d"


def trade_line(t: dict, price: float | None) -> str:
    side_icon = "🟢" if t["side"] == "BUY" else "🔴"
    head = (f"<b>#{t['id']}</b> {side_icon} <b>{html.escape(t['pretty'])}</b> {t['side']} "
            f"{t['qty']:g} {html.escape(t.get('unit', ''))} · {t['tf']}")
    if price is None:
        return head + f"\n   Entry {_price(t, t['entry'])} · live price unavailable"
    money, pct, r = pnl(t, price)
    tone = "🟢" if money > 0 else "🔴" if money < 0 else "⚪"
    up = t["side"] == "BUY"
    to_sl = (price - t["stop"]) / price * 100 * (1 if up else -1)
    to_tp = ((t["target"] - price) / price * 100 * (1 if up else -1)) if t.get("target") else None
    rr = f" · {r:+.2f}R" if r is not None else ""
    lines = [head,
             f"   {tone} <b>{pct:+.2f}%</b> ({_money(money, t['currency'])}){rr}",
             f"   Entry {_price(t, t['entry'])} → live <b>{_price(t, price)}</b> · {_age(t['opened'])} ago",
             f"   🛑 SL {_price(t, t['stop'])} ({to_sl:.2f}% away)"
             + (f" · 🎯 TP {_price(t, t['target'])} ({to_tp:.2f}% away)" if to_tp is not None else "")]
    return "\n".join(lines)


def status_text(trades: list[dict], prices: dict, title: str, closed_now: list[dict]) -> str:
    now = datetime.now(IST).strftime("%d %b, %H:%M IST")
    lines = [f"💼 <b>{title}</b>", f"<i>{now} · live prices · paper money only</i>", LINE]
    for c in closed_now:
        icon = "🎯" if c["reason"].startswith("target") else "🛑"
        lines.append(f"{icon} #{c['id']} {html.escape(c['pretty'])} closed: {c['reason']} at {_price(c, c['exit'])} "
                     f"→ {c['pnl_pct']:+.2f}% ({_money(c['pnl'], c['currency'])})")
    if closed_now:
        lines.append(LINE)
    if not trades:
        lines.append("No open paper trades. Analyse any coin, stock or pair and tap 📝 Paper trade.")
    tot: dict[str, float] = {}
    for t in trades:
        p = prices.get(t["id"])
        lines.append(trade_line(t, p))
        lines.append("")
        if p is not None:
            m, _, _ = pnl(t, p)
            tot[t["currency"]] = tot.get(t["currency"], 0) + m
    if tot:
        lines.append("Open P&amp;L: " + " · ".join(f"{'🟢' if v >= 0 else '🔴'} {_money(v, c)}" for c, v in tot.items()))
    return "\n".join(lines)


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
    lines.append(f"Trades {st['n']} · win rate {st['win']:.0f}%" + (f" · avg {st['avg_r']:+.2f}R" if st["avg_r"] is not None
                                                                     else ""))
    lines.append("Total: " + " · ".join(f"{'🟢' if v >= 0 else '🔴'} {_money(v, c)}" for c, v in st["by_cur"].items()))
    lines.append("")
    for t in sorted(closed, key=lambda t: t.get("exit_ts", 0), reverse=True)[:12]:
        icon = "🟢" if t.get("pnl", 0) > 0 else "🔴"
        when = datetime.fromtimestamp(t.get("exit_ts", 0), IST).strftime("%d %b %H:%M")
        lines.append(f"{icon} #{t['id']} {html.escape(t['pretty'])} {t['side']} {_price(t, t['entry'])}→"
                     f"{_price(t, t['exit'])} <b>{t['pnl_pct']:+.2f}%</b> · {html.escape(t['reason'])} · {when}")
    return "\n".join(lines)


def board_text(chat_id: str) -> str:
    doc = store.load()
    people: dict[str, dict] = {}
    for t in _trades(doc):
        if t["chat_id"] != chat_id:
            continue
        p = people.setdefault(t["user_id"], {"name": t["user_name"], "open": 0, "closed": [], "pct": 0.0})
        if t["status"] == "open":
            p["open"] += 1
        else:
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
    lines.append("<i>Sum of % returns of closed paper trades.</i>")
    return "\n".join(lines)


def plan_text(a, user_name: str = "") -> str:
    """The ticket shown before opening: both sides with their levels."""
    from bot import report

    lines = [f"📝 <b>PAPER TRADE · {html.escape(a.pretty)}</b> · {a.timeframe}",
             f"Live price <b>{report.price_txt(a, a.now_price)}</b> · {html.escape(a.source)}", LINE]
    for side in ("BUY", "SELL"):
        e, s, t, why = levels(a, side)
        q, _, unit = default_qty(a, e, s)
        icon = "🟢" if side == "BUY" else "🔴"
        lines.append(f"{icon} <b>{side}</b> at {report.price_txt(a, e)} · 🛑 {report.price_txt(a, s)} · "
                     f"🎯 {report.price_txt(a, t)} · {q:g} {html.escape(unit)}")
        lines.append(f"   <i>levels from: {html.escape(why)}</i>")
    lines += ["", f"Chart verdict: <b>{html.escape(report.verdict(a))}</b>",
              "Tap a side to open it at the live price. Paper money only — no real order is placed."]
    return "\n".join(lines)

"""Backtest one strategy on one symbol's own history, measured in R.

R = the amount risked per trade (distance from entry to stop, plus costs). Works for
BUY and SELL strategies (SELL profits when price falls). A +2R trade made
twice what was risked; a -1R trade lost the full risk.
Rules: signal on candle close -> enter at the NEXT candle's open (no look-ahead);
stop and target checked on later candles; if both touch in one candle the stop wins;
an exit rule (opposite cross, back to the mean, end of session) or a time stop closes the
trade at the next candle's open; fees and slippage on both sides. One trade at a time.
"""
from __future__ import annotations

import pandas as pd

from bot.config import cfg, cost_pct_side
from bot.strategies import Strategy


def run(strategy: Strategy, x: pd.DataFrame, market: str) -> dict:
    base = {"from": x.index[0].strftime("%d %b %Y"), "to": x.index[-1].strftime("%d %b %Y")}
    if strategy.applies(x):
        return {**base, "trades": 0, "na": True, "win_rate": None, "avg_r": None, "total_r": 0.0,
                "profit_factor": None, "enough_data": False}
    fee, slip = cost_pct_side(market)
    sig = strategy.signal(x).to_numpy()
    rule = strategy.exit_rule(x)
    rule = rule.fillna(False).astype(bool).to_numpy() if rule is not None else None
    o, h, l = x["open"].to_numpy(), x["high"].to_numpy(), x["low"].to_numpy()
    close = x["close"].to_numpy()
    n = len(x)
    d = 1 if strategy.side == "BUY" else -1          # +1 long, -1 short
    rs: list[float] = []
    bars_held: list[int] = []
    i = 0
    while i < n - 1:
        if not sig[i]:
            i += 1
            continue
        entry = o[i + 1] * (1 + d * slip)
        try:
            stop = strategy.stop(x, i, float(close[i]))
            target = strategy.target(x, i, entry, stop)
        except (ValueError, TypeError):
            i += 1
            continue
        if not (d * (entry - stop) > 0):
            i += 1
            continue
        if target is not None and not (target == target and d * (target - entry) > 0):
            i += 1
            continue
        risk_unit = d * (entry - stop) + entry * fee * 2
        exit_price, j, pending = None, i + 1, False
        while j < n:
            if pending:                                # exit rule fired on the previous close
                exit_price = o[j]
                break
            adverse = l[j] if d == 1 else h[j]
            favour = h[j] if d == 1 else l[j]
            # Entry candle: only the part after the open counts, so gaps apply from the next candle.
            if j > i + 1 and d * (o[j] - stop) <= 0:
                exit_price = o[j]                      # gapped through the stop
            elif d * (adverse - stop) <= 0:
                exit_price = stop
            elif target is not None and j > i + 1 and d * (o[j] - target) >= 0:
                exit_price = o[j]                      # gapped through the target
            elif target is not None and d * (favour - target) >= 0:
                exit_price = target
            if exit_price is not None:
                break
            held = j - i
            if (rule is not None and rule[j]) or (strategy.max_bars and held >= strategy.max_bars):
                pending = True
            j += 1
        if exit_price is None:
            break  # still open at the end of the data
        fill = exit_price * (1 - d * slip)
        pnl_unit = d * (fill - entry) - (entry + fill) * fee
        rs.append(pnl_unit / risk_unit)
        bars_held.append(j - i)
        i = j + 1

    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    gross_loss = abs(sum(losses))
    return {
        **base,
        "trades": len(rs),
        "win_rate": round(100 * len(wins) / len(rs), 1) if rs else None,
        "avg_r": round(sum(rs) / len(rs), 2) if rs else None,
        "total_r": round(sum(rs), 1) if rs else 0.0,
        "profit_factor": round(sum(wins) / gross_loss, 2) if losses and gross_loss else None,
        "expectancy_r": round(sum(rs) / len(rs), 2) if rs else None,
        "avg_bars": round(sum(bars_held) / len(bars_held), 1) if bars_held else None,
        "enough_data": len(rs) >= 20,
    }

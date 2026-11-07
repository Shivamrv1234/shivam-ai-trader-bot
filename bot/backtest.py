"""Backtest one strategy on one symbol's own history, measured in R.

R = the amount risked per trade (distance from entry to stop, plus costs). Works for
BUY and SELL strategies (SELL profits when price falls). A +2R trade made
twice what was risked; a -1R trade lost the full risk.
Rules: signal on candle close -> enter at the NEXT candle's open (no look-ahead);
stop and target checked on later candles; if both touch in one candle the stop wins;
fees and slippage on both sides. One trade at a time.
"""
from __future__ import annotations

import pandas as pd

from bot.config import cfg
from bot.strategies import Strategy


def run(strategy: Strategy, x: pd.DataFrame, market: str) -> dict:
    c = cfg()["costs"]
    fee = (c["crypto_pct_per_side"] if market == "crypto" else c["stocks_pct_per_side"]) / 100
    slip = c["slippage_pct"] / 100
    target_r = cfg()["risk"]["target_r"]
    sig = strategy.signal(x).to_numpy()
    o, h, l = x["open"].to_numpy(), x["high"].to_numpy(), x["low"].to_numpy()
    n = len(x)
    rs: list[float] = []
    i = 0
    while i < n - 1:
        if not sig[i]:
            i += 1
            continue
        d = 1 if strategy.side == "BUY" else -1          # +1 long, -1 short
        entry = o[i + 1] * (1 + d * slip)
        stop = strategy.stop(x, i, float(x["close"].iloc[i]))
        if not (d * (entry - stop) > 0):
            i += 1
            continue
        target = entry + target_r * (entry - stop)
        risk_unit = d * (entry - stop) + entry * fee * 2
        exit_price, j = None, i + 1
        while j < n:
            # Entry candle: only the part after the open counts, so gaps apply from the next candle.
            adverse = l[j] if d == 1 else h[j]
            favour = h[j] if d == 1 else l[j]
            if j > i + 1 and d * (o[j] - stop) <= 0:
                exit_price = o[j]                      # gapped through the stop
            elif d * (adverse - stop) <= 0:
                exit_price = stop
            elif j > i + 1 and d * (o[j] - target) >= 0:
                exit_price = o[j]                      # gapped through the target
            elif d * (favour - target) >= 0:
                exit_price = target
            if exit_price is not None:
                break
            j += 1
        if exit_price is None:
            break  # still open at the end of the data
        fill = exit_price * (1 - d * slip)
        pnl_unit = d * (fill - entry) - (entry + fill) * fee
        rs.append(pnl_unit / risk_unit)
        i = j + 1

    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    return {
        "trades": len(rs),
        "win_rate": round(100 * len(wins) / len(rs), 1) if rs else None,
        "avg_r": round(sum(rs) / len(rs), 2) if rs else None,
        "total_r": round(sum(rs), 1) if rs else 0.0,
        "profit_factor": round(sum(wins) / abs(sum(losses)), 2) if losses and sum(losses) else None,
        "from": x.index[0].strftime("%d %b %Y"),
        "to": x.index[-1].strftime("%d %b %Y"),
        "enough_data": len(rs) >= 20,
    }

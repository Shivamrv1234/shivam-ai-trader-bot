"""Price action, support/resistance, trend lines, psychological levels and
Smart Money Concepts (BOS, CHoCH, order blocks, fair-value gaps, demand/supply).

Everything is computed CAUSALLY in one pass: the value at candle i uses only
candles 0..i. A swing pivot at candle j needs PIVOT_N candles on each side, so it
only becomes known at candle j + PIVOT_N. This keeps backtests honest.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

PIVOT_N = 3            # candles on each side of a swing high/low
SR_LOOKBACK = 200      # candles of pivots used for support/resistance
SMC_RECENT = 30        # a structure break counts as "recent" for this many candles
KEEP_PER_KIND = 6      # active zones kept per kind

ZONE_NAMES = {
    "demand": "demand zone", "supply": "supply zone",
    "ob_bull": "bullish order block", "ob_bear": "bearish order block",
    "fvg_bull": "bullish fair-value gap", "fvg_bear": "bearish fair-value gap",
}


@dataclass
class Zone:
    kind: str
    top: float
    bot: float
    start: int          # candle where the zone's origin candle is (for drawing)
    born: int           # candle where the zone became known
    tests: int = 0
    last_test: int = -1

    @property
    def bullish(self) -> bool:
        return self.kind in ("demand", "ob_bull", "fvg_bull")


@dataclass
class Snapshot:
    """State at the last candle, for the chart and the message."""
    supports: list = field(default_factory=list)      # [(price, touches)] nearest first
    resistances: list = field(default_factory=list)
    zones: list = field(default_factory=list)          # active Zone objects near price
    tl_up: tuple | None = None                         # (i1, p1, i2, p2)
    tl_down: tuple | None = None
    breaks: list = field(default_factory=list)         # [(i, price, "BOS"/"CHoCH", +1/-1)]
    swings: list = field(default_factory=list)         # [(i, price, "HH"/"HL"/"LH"/"LL")]
    structure: int = 0
    round_levels: list = field(default_factory=list)


def round_steps(price: float) -> tuple[float, float]:
    """Psychological steps: NIFTY 24,020 -> 1,000 and 500; RELIANCE 1,400 -> 100 and 50."""
    if price <= 0:
        return 1.0, 0.5
    major = 10 ** (math.floor(math.log10(price)) - 1)
    return major, major / 2


def nearby_round_levels(price: float, lo: float, hi: float) -> list[float]:
    major, half = round_steps(price)
    levels = set()
    k = math.floor(lo / half)
    while k * half <= hi:
        if k * half >= lo:
            levels.add(round(k * half, 10))
        k += 1
    ordered = sorted(levels, key=lambda v: abs(v - price))
    return sorted(ordered[:4])


def clusters(prices: list[float], tol: float) -> list[tuple[float, int]]:
    if not prices:
        return []
    ps = sorted(prices)
    out, group = [], [ps[0]]
    for p in ps[1:]:
        if p - group[-1] <= tol:
            group.append(p)
        else:
            out.append((sum(group) / len(group), len(group)))
            group = [p]
    out.append((sum(group) / len(group), len(group)))
    return out


def _line(line: tuple, i: float) -> float:
    i1, p1, i2, p2 = line
    return p1 + (p2 - p1) * (i - i1) / (i2 - i1)


def candle_patterns(x: pd.DataFrame) -> pd.DataFrame:
    o, h, l, c = x["open"], x["high"], x["low"], x["close"]
    body = (c - o).abs()
    rng = (h - l).replace(0, np.nan)
    lower = np.minimum(o, c) - l
    upper = h - np.maximum(o, c)
    out = pd.DataFrame(index=x.index)
    out["bull_engulf"] = (c > o) & (c.shift(1) < o.shift(1)) & (c >= o.shift(1)) & (o <= c.shift(1))
    out["hammer"] = (lower >= 2 * body) & (upper <= 0.3 * rng) & ((c - l) >= 0.6 * rng)
    return out.fillna(False)


def analyze(x: pd.DataFrame) -> tuple[pd.DataFrame, Snapshot]:
    """Return x with structure columns added, plus a snapshot of the latest state."""
    n = len(x)
    o, h, l, c = (x[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close"))
    atr = x["atr14"].to_numpy(dtype=float) if "atr14" in x else np.full(n, np.nan)
    win = 2 * PIVOT_N + 1
    ph = (x["high"] == x["high"].rolling(win, center=True).max()).to_numpy()
    pl = (x["low"] == x["low"].rolling(win, center=True).min()).to_numpy()

    cols = {k: np.full(n, np.nan) for k in (
        "sup", "sup_touches", "res", "res_touches", "dz_top", "dz_bot", "sz_top", "sz_bot",
        "bz_top", "bz_bot", "tl_down", "tl_down_prev", "tl_up", "struct", "bars_since_bull",
        "round_cross")}
    bz_kind = [""] * n
    bull_kind = [""] * n

    swings_h: list[tuple[int, float]] = []
    swings_l: list[tuple[int, float]] = []
    labels: list[tuple[int, float, str]] = []
    zones: list[Zone] = []
    breaks: list[tuple[int, float, str, int]] = []
    last_sh = last_sl = None          # [idx, price, broken]
    struct, bull_i, bull_k = 0, None, ""
    used_base: set[int] = set()

    for i in range(n):
        a = atr[i] if atr[i] == atr[i] and atr[i] > 0 else max(h[i] - l[i], 1e-12)

        # 1) pivots confirmed now (they happened PIVOT_N candles ago)
        j = i - PIVOT_N
        if j >= PIVOT_N:
            if ph[j]:
                lab = ("HH" if h[j] > swings_h[-1][1] else "LH") if swings_h else "H"
                swings_h.append((j, h[j]))
                labels.append((j, h[j], lab))
                last_sh = [j, h[j], False]
            if pl[j]:
                lab = ("HL" if l[j] > swings_l[-1][1] else "LL") if swings_l else "L"
                swings_l.append((j, l[j]))
                labels.append((j, l[j], lab))
                last_sl = [j, l[j], False]

        # 2) break of structure / change of character, with the order block left behind
        if last_sh and not last_sh[2] and c[i] > last_sh[1]:
            kind = "BOS" if struct == 1 else "CHoCH"
            struct, bull_i, bull_k = 1, i, kind
            last_sh[2] = True
            breaks.append((i, last_sh[1], kind, 1))
            for k in range(i - 1, max(i - 15, -1), -1):
                if c[k] < o[k]:
                    zones.append(Zone("ob_bull", h[k], l[k], k, i))
                    break
        if last_sl and not last_sl[2] and c[i] < last_sl[1]:
            kind = "BOS" if struct == -1 else "CHoCH"
            struct = -1
            last_sl[2] = True
            breaks.append((i, last_sl[1], kind, -1))
            for k in range(i - 1, max(i - 15, -1), -1):
                if c[k] > o[k]:
                    zones.append(Zone("ob_bear", h[k], l[k], k, i))
                    break

        # 3) fair-value gaps (3-candle imbalance)
        if i >= 2:
            if l[i] > h[i - 2] and l[i] - h[i - 2] >= 0.1 * a:
                zones.append(Zone("fvg_bull", l[i], h[i - 2], i - 1, i))
            if h[i] < l[i - 2] and l[i - 2] - h[i] >= 0.1 * a:
                zones.append(Zone("fvg_bear", l[i - 2], h[i], i - 1, i))

        # 4) demand / supply: a tight base candle followed by a strong move away
        for k in range(max(0, i - 3), i):
            rng = h[k] - l[k]
            if k in used_base or rng <= 0 or abs(c[k] - o[k]) > 0.5 * rng:
                continue
            ak = atr[k] if atr[k] == atr[k] and atr[k] > 0 else a
            if c[i] - h[k] >= 1.5 * ak:
                zones.append(Zone("demand", h[k], l[k], k, i))
                used_base.add(k)
            elif l[k] - c[i] >= 1.5 * ak:
                zones.append(Zone("supply", h[k], l[k], k, i))
                used_base.add(k)

        # 5) test / invalidate zones with this candle (zones born now are untouched)
        alive = []
        for z in zones:
            if z.born < i:
                if z.bullish:
                    broken = l[i] <= z.bot if z.kind == "fvg_bull" else c[i] < z.bot
                    if broken:
                        continue
                    if l[i] <= z.top:
                        z.tests += 1
                        z.last_test = i
                else:
                    broken = h[i] >= z.top if z.kind == "fvg_bear" else c[i] > z.top
                    if broken:
                        continue
                    if h[i] >= z.bot:
                        z.tests += 1
                        z.last_test = i
            alive.append(z)
        zones = []
        for kind in ZONE_NAMES:
            zones += [z for z in alive if z.kind == kind][-KEEP_PER_KIND:]

        # 6) per-candle features
        recent = [p for (jj, p) in swings_l if jj >= i - SR_LOOKBACK] + \
                 [p for (jj, p) in swings_h if jj >= i - SR_LOOKBACK]
        levels = clusters(recent, 0.5 * a)
        below = [lv for lv in levels if lv[0] < c[i]]
        above = [lv for lv in levels if lv[0] > c[i]]
        if below:
            s = max(below)
            cols["sup"][i], cols["sup_touches"][i] = s
        if above:
            r = min(above)
            cols["res"][i], cols["res_touches"][i] = r

        dem = [z for z in zones if z.kind == "demand" and z.bot <= c[i]]
        if dem:
            z = max(dem, key=lambda z: z.top)
            cols["dz_top"][i], cols["dz_bot"][i] = z.top, z.bot
        sup_z = [z for z in zones if z.kind == "supply" and z.top >= c[i]]
        if sup_z:
            z = min(sup_z, key=lambda z: z.bot)
            cols["sz_top"][i], cols["sz_bot"][i] = z.top, z.bot

        retested = [z for z in zones if z.kind in ("ob_bull", "fvg_bull") and z.last_test >= i - 2]
        if retested:
            z = max(retested, key=lambda z: z.last_test)
            cols["bz_top"][i], cols["bz_bot"][i] = z.top, z.bot
            bz_kind[i] = z.kind

        if len(swings_h) >= 2 and swings_h[-1][1] < swings_h[-2][1]:
            line = (swings_h[-2][0], swings_h[-2][1], swings_h[-1][0], swings_h[-1][1])
            cols["tl_down"][i], cols["tl_down_prev"][i] = _line(line, i), _line(line, i - 1)
        if len(swings_l) >= 2 and swings_l[-1][1] > swings_l[-2][1]:
            line = (swings_l[-2][0], swings_l[-2][1], swings_l[-1][0], swings_l[-1][1])
            cols["tl_up"][i] = _line(line, i)

        cols["struct"][i] = struct
        if bull_i is not None:
            cols["bars_since_bull"][i] = i - bull_i
            bull_kind[i] = bull_k

        if i > 0:
            major, half = round_steps(c[i])
            for step in (major, half):
                lvl = math.floor(c[i] / step) * step
                if c[i - 1] < lvl <= c[i]:
                    cols["round_cross"][i] = lvl
                    break

    out = x.copy()
    for k, v in cols.items():
        out[k] = v
    out["bz_kind"] = bz_kind
    out["bull_break_kind"] = bull_kind
    out = out.join(candle_patterns(x))

    # snapshot at the last candle
    snap = Snapshot(structure=struct, breaks=breaks[-6:], swings=labels[-8:])
    last = c[-1] if n else 0
    a_last = atr[-1] if n and atr[-1] == atr[-1] else 0
    recent = [p for (jj, p) in swings_l + swings_h if jj >= n - 1 - SR_LOOKBACK]
    lv = clusters(recent, 0.5 * a_last if a_last else 0)
    snap.supports = sorted([v for v in lv if v[0] < last], key=lambda v: -v[0])[:3]
    snap.resistances = sorted([v for v in lv if v[0] > last], key=lambda v: v[0])[:3]
    near = []
    for kind in ZONE_NAMES:
        ks = [z for z in zones if z.kind == kind]
        ks.sort(key=lambda z: min(abs(z.top - last), abs(z.bot - last)))
        picked: list[Zone] = []
        for z in ks:  # skip zones that mostly overlap one already picked
            if any(min(z.top, p.top) - max(z.bot, p.bot) > 0.5 * min(z.top - z.bot, p.top - p.bot)
                   for p in picked):
                continue
            picked.append(z)
            if len(picked) == 2:
                break
        near += picked
    snap.zones = near
    if len(swings_h) >= 2 and swings_h[-1][1] < swings_h[-2][1]:
        snap.tl_down = (swings_h[-2][0], swings_h[-2][1], swings_h[-1][0], swings_h[-1][1])
    if len(swings_l) >= 2 and swings_l[-1][1] > swings_l[-2][1]:
        snap.tl_up = (swings_l[-2][0], swings_l[-2][1], swings_l[-1][0], swings_l[-1][1])
    if n:
        tail = x.tail(120)
        snap.round_levels = nearby_round_levels(last, float(tail["low"].min()), float(tail["high"].max()))
    return out, snap


def line_value(line: tuple, i: float) -> float:
    return _line(line, i)

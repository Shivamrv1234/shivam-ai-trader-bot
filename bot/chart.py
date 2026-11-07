"""Candlestick chart image: EMAs, volume, RSI, entry/stop/target, plus market
structure: support/resistance, psychological levels, trend lines, demand/supply
zones, order blocks, fair-value gaps and BOS/CHoCH marks."""
from __future__ import annotations

import io

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import mplfinance as mpf  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.patches import Rectangle  # noqa: E402

from bot.strategies import _fmt  # noqa: E402

ZONE_STYLE = {   # colour, label
    "demand": ("#27ae60", "Demand"),
    "supply": ("#c0392b", "Supply"),
    "ob_bull": ("#2e86de", "Bull OB"),
    "ob_bear": ("#e67e22", "Bear OB"),
    "fvg_bull": ("#8e44ad", "Bull FVG"),
    "fvg_bear": ("#d35400", "Bear FVG"),
}


def _draw_structure(ax, snap, offset: int, bars: int, lo: float, hi: float) -> None:
    from bot.structure import line_value

    right = bars + 1.5
    labels: list[tuple[float, str, str]] = []   # (y, text, colour) placed in the right margin
    # zones (rectangles from their origin candle to the right edge)
    for z in snap.zones:
        color, label = ZONE_STYLE[z.kind]
        x0 = max(z.start - offset, -0.5)
        if z.top < lo or z.bot > hi:
            continue
        ax.add_patch(Rectangle((x0, z.bot), right - x0, z.top - z.bot, facecolor=color, alpha=0.13,
                               edgecolor=color, linewidth=0.6, zorder=0))
        labels.append(((z.top + z.bot) / 2, f" {label}", color))
    # support / resistance
    for p, t in snap.supports[:2]:
        if lo <= p <= hi:
            ax.axhline(p, color="#1e8449", linewidth=1.0, alpha=0.8)
            labels.append((p, f" S {_fmt(p)}", "#1e8449"))
    for p, t in snap.resistances[:2]:
        if lo <= p <= hi:
            ax.axhline(p, color="#922b21", linewidth=1.0, alpha=0.8)
            labels.append((p, f" R {_fmt(p)}", "#922b21"))
    # psychological round numbers
    for p in snap.round_levels:
        if lo <= p <= hi:
            ax.axhline(p, color="#7f8c8d", linewidth=0.8, linestyle=":")
            ax.text(-0.5, p, f"{_fmt(p)} ", fontsize=7, color="#7f8c8d", va="bottom", ha="left")
    # trend lines
    for line, color in ((snap.tl_down, "#c0392b"), (snap.tl_up, "#27ae60")):
        if not line:
            continue
        x_start = max(line[0], offset)
        xs = [x_start - offset, bars - 1]
        ys = [line_value(line, x_start), line_value(line, offset + bars - 1)]
        ax.plot(xs, ys, color=color, linewidth=1.4, linestyle="-.", zorder=4)
    # right-margin labels, nudged apart so none overlap
    gap = (hi - lo) * 0.028
    placed: list[float] = []
    for y, text, color in sorted(labels):
        y_draw = max(y, placed[-1] + gap) if placed else y
        if y_draw > hi:
            continue
        placed.append(y_draw)
        ax.text(right, y_draw, text, fontsize=7, color=color, va="center", ha="left", zorder=6)
    # BOS / CHoCH marks
    for i, lvl, kind, d in snap.breaks:
        xi = i - offset
        if 0 <= xi < bars and lo <= lvl <= hi:
            color = "#1e8449" if d > 0 else "#922b21"
            ax.plot([max(xi - 6, 0), xi], [lvl, lvl], color=color, linewidth=1.0, linestyle="--")
            ax.text(xi, lvl, kind, fontsize=7.5, color=color, fontweight="bold",
                    va="bottom" if d > 0 else "top", ha="right")


def render(x: pd.DataFrame, title: str, setup=None, snap=None, bars: int = 120) -> bytes:
    d = x.tail(bars).copy()
    offset = len(x) - len(d)
    d.index = d.index.tz_convert("Asia/Kolkata").tz_localize(None)
    plots = [
        mpf.make_addplot(d["ema21"], color="#f39c12", width=1.0, secondary_y=False),
        mpf.make_addplot(d["ema50"], color="#2e86de", width=1.0, secondary_y=False),
    ]
    if d["ema200"].notna().all():
        plots.append(mpf.make_addplot(d["ema200"], color="#8e44ad", width=1.0, secondary_y=False))
    plots += [
        mpf.make_addplot(d["rsi14"], panel=2, color="#34495e", width=1, ylabel="RSI 14",
                         ylim=(0, 100), secondary_y=False),
        mpf.make_addplot(pd.Series(70.0, index=d.index), panel=2, color="#c0392b", linestyle="--",
                         width=0.7, secondary_y=False),
        mpf.make_addplot(pd.Series(30.0, index=d.index), panel=2, color="#27ae60", linestyle="--",
                         width=0.7, secondary_y=False),
    ]
    lo, hi = float(d["low"].min()), float(d["high"].max())
    kwargs = {}
    if setup is not None and setup.entry is not None:
        kwargs["hlines"] = dict(hlines=[setup.target, setup.entry, setup.stop],
                                colors=["#27ae60", "#2e86de", "#c0392b"], linestyle="--", linewidths=1.4)
        lo, hi = min(lo, setup.stop), max(hi, setup.target)
        label = setup.side if setup.status == "TRADE" else f"{setup.side} (forming)"
        title += (f"\n{label} · {setup.name}: entry {_fmt(setup.entry)} · "
                  f"take profit {_fmt(setup.target)} · stop {_fmt(setup.stop)}")
    pad = (hi - lo) * 0.04
    lo, hi = lo - pad, hi + pad
    style = mpf.make_mpf_style(base_mpf_style="yahoo", gridstyle=":", y_on_right=True)
    fig, axes = mpf.plot(
        d, type="candle", style=style, addplot=plots, volume=True, panel_ratios=(5, 1.3, 1.8),
        figsize=(12, 9), returnfig=True, ylim=(lo, hi), ylabel="", **kwargs,
    )
    if snap is not None:
        _draw_structure(axes[0], snap, offset, len(d), lo, hi)
        axes[0].set_xlim(-1, len(d) + 11)
    fig.suptitle(title, fontsize=12, fontweight="bold", y=0.985)
    legend = "EMA21 orange · EMA50 blue · EMA200 purple"
    if "hlines" in kwargs:
        legend += "  |  dashed: target green · entry blue · stop red"
    if snap is not None:
        legend += ("\nS/R solid lines · round numbers dotted · trend lines dash-dot · shaded: demand/supply "
                   "zones, order blocks (OB), fair-value gaps (FVG) · BOS/CHoCH = structure breaks")
    fig.text(0.01, 0.004, legend, fontsize=8, color="#555555")
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()

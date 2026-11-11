"""Why does v3.1 win or lose? Break a backtest down by strategy, exit, regime and time,
and measure whether the entry signals predict direction at all.

The key table is "after entry, ignoring exits": for every trade it records where
the price was 15/30/60 minutes later and at the flatten time, in R units of that
trade (R = entry * V3_STOP_PCT). If those numbers are positive, the entries have
an edge and the exits are giving it away; if they are around zero or negative,
the entries themselves don't predict direction and no exit rule can fix that.
"""

from __future__ import annotations

from datetime import timedelta

import pandas as pd

from ..config import Settings
from ..strategy import NY
from .backtest import V3BacktestResult

HORIZONS = [("15m", 15), ("30m", 30), ("60m", 60), ("EOD", None)]


def forward_r(result: V3BacktestResult, bars_by_symbol: dict[str, pd.DataFrame], s: Settings) -> pd.DataFrame:
    """Price moves after each entry, in R, at fixed horizons (ignores the bot's exits)."""
    closes = {}
    for sym, bars in bars_by_symbol.items():
        end = (bars.index + timedelta(minutes=5)).tz_convert(NY)
        closes[sym] = pd.Series(bars["close"].values, index=end)

    rows = []
    for t in result.trades:
        series = closes[t.symbol]
        day = series[series.index.date == t.entry_time.date()]
        day = day[day.index.time <= s.flatten_time]
        risk = t.entry_price * s.v3_stop_pct
        row = {"strategy": t.strategy, "symbol": t.symbol}
        for label, minutes in HORIZONS:
            if minutes is None:
                px = day.iloc[-1] if len(day) else float("nan")
            else:
                later = day[day.index >= t.entry_time + timedelta(minutes=minutes)]
                px = later.iloc[0] if len(later) else (day.iloc[-1] if len(day) else float("nan"))
            row[label] = (px - t.entry_price) / risk
        rows.append(row)
    return pd.DataFrame(rows)


def _table(df: pd.DataFrame, by: str, title: str) -> list[str]:
    lines = [title]
    g = df.groupby(by)
    summary = pd.DataFrame({
        "trades": g.size(),
        "win%": g["realized_r"].apply(lambda r: (r > 0).mean() * 100),
        "avg R": g["realized_r"].mean(),
        "total R": g["realized_r"].sum(),
    })
    for key, r in summary.iterrows():
        lines.append(f"  {str(key):<22} {int(r['trades']):>4} trades  win {r['win%']:5.1f}%  avg {r['avg R']:+.2f}R  total {r['total R']:+7.1f}R")
    return lines


def diagnose(result: V3BacktestResult, bars_by_symbol: dict[str, pd.DataFrame], s: Settings) -> str:
    if not result.trades:
        return "No trades to diagnose."
    df = pd.DataFrame([t.__dict__ for t in result.trades])
    df["regime"] = [result.regimes.get(t.entry_time.date()) for t in result.trades]
    df["hour"] = [f"{t.entry_time.hour:02d}:00-{t.entry_time.hour + 1:02d}:00" for t in result.trades]
    df["strategy/exit"] = df["strategy"] + " " + df["exit_reason"]
    df["strategy/symbol"] = df["strategy"] + " " + df["symbol"]
    mid = df["entry_time"].iloc[len(df) // 2]
    df["half"] = ["1st half" if t < mid else "2nd half" for t in df["entry_time"]]

    out = ["=" * 70, "v3.1 DIAGNOSIS", "=" * 70]
    out += _table(df, "strategy", "By strategy:")
    out += [""] + _table(df, "strategy/exit", "By strategy and exit reason:")
    out += [""] + _table(df, "strategy/symbol", "By strategy and fund:")
    out += [""] + _table(df, "regime", "By day type (10:00 regime):")
    out += [""] + _table(df, "hour", "By entry hour (New York time):")
    out += [""] + _table(df, "half", "Consistency (first vs second half of the trades):")

    out += ["", "How far trades got (MFE = best point, MAE = worst point, in R):"]
    for name, g in df.groupby("strategy"):
        out.append(
            f"  {name:<14} reached +0.6R: {(g['mfe_r'] >= 0.6).mean() * 100:4.0f}%   +1R: {(g['mfe_r'] >= 1).mean() * 100:4.0f}%   "
            f"+2R: {(g['mfe_r'] >= 2).mean() * 100:4.0f}%   avg best {g['mfe_r'].mean():+.2f}R   avg worst {g['mae_r'].mean():+.2f}R"
        )
    shield = df[df["exit_reason"] == "SHIELD"]
    if len(shield):
        out.append(f"  {len(shield)} trades reached +0.6R and were then stopped at break-even")

    fwd = forward_r(result, bars_by_symbol, s)
    out += ["", "After entry, ignoring the bot's exits (avg move in R; >0 means the signal points the right way):"]
    out.append(f"  {'':<22} {'15m':>7} {'30m':>7} {'60m':>7} {'EOD':>7}   {'up at EOD':>9}")
    for name, g in list(fwd.groupby("strategy")) + [("ALL", fwd)]:
        cells = " ".join(f"{g[h].mean():+7.2f}" for h, _ in HORIZONS)
        out.append(f"  {name:<22} {cells}   {(g['EOD'] > 0).mean() * 100:8.0f}%")
    out += [
        "",
        "Reading it: if the after-entry numbers are clearly positive but realized R is",
        "negative, the exits are cutting winners. If they're near zero or negative, the",
        "entry signal itself has no edge, and changing exits won't fix it.",
    ]
    return "\n".join(out)

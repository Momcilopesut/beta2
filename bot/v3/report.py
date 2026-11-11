"""v3.1 trade log (CSV) and the per-strategy review ("recursive learning audit")."""

from __future__ import annotations

import os
from dataclasses import asdict

import pandas as pd

from .position import V3Trade

COLUMNS = [
    "entry_time", "exit_time", "strategy", "symbol", "qty", "entry_price", "exit_price",
    "pnl", "realized_r", "mae_r", "mfe_r", "exit_reason", "scaled",
]
DEFAULT_LOG = "logs/v3_trades.csv"


def append_trades(trades: list[V3Trade], path: str = DEFAULT_LOG) -> None:
    if not trades:
        return
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    rows = pd.DataFrame([asdict(t) for t in trades])[COLUMNS]
    rows.to_csv(path, mode="a", header=not os.path.exists(path), index=False)


def audit(path: str = DEFAULT_LOG, last: int | None = None) -> str:
    """Per-strategy win rate, profit factor, average R and MFE/MAE from the trade log."""
    if not os.path.exists(path):
        return f"No trade log at {path} yet."
    df = pd.read_csv(path)
    if df.empty:
        return "The trade log is empty."
    if last:
        df = df.tail(last)

    lines = ["=" * 62, f"v3.1 TRADE REVIEW ({len(df)} trades from {path})", "=" * 62]
    for strategy, g in list(df.groupby("strategy")) + [("ALL STRATEGIES", df)]:
        wins = g[g["realized_r"] > 0]["realized_r"]
        losses = g[g["realized_r"] < 0]["realized_r"]
        pf = wins.sum() / -losses.sum() if len(losses) and losses.sum() < 0 else float("inf")
        lines += [
            f"Strategy          : {strategy}",
            f"Trades            : {len(g)}",
            f"Win rate          : {len(wins) / len(g) * 100:.1f}%",
            f"Profit factor     : {pf:.2f}",
            f"Avg result        : {g['realized_r'].mean():+.2f}R per trade   (P&L ${g['pnl'].sum():+,.2f})",
            f"Avg best / worst  : {g['mfe_r'].mean():+.2f}R / {g['mae_r'].mean():+.2f}R  (MFE / MAE)",
        ]
        if len(g) < 30:
            lines.append(f"Note              : only {len(g)} trades - too few to judge this strategy yet")
        lines.append("-" * 62)
    return "\n".join(lines)

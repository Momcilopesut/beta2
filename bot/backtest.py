"""Bar-by-bar backtester for the intraday strategy.

Execution model (deliberately conservative):
- A signal on bar N is filled at the open of bar N+1 (no look-ahead).
- Stops and targets are checked against each bar's high/low. If a bar touches
  both, the stop is assumed to have hit first. A gap through a level fills at
  the open.
- Positions are closed at the open of the first bar at or after FLATTEN_TIME,
  and at the close of the last bar of each session as a safety net.
- Every fill pays `slippage_bps`.
- After the day's loss reaches MAX_DAILY_LOSS_PCT, no new trades open that day.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from .config import Settings
from .risk import plan_entry
from .strategy import ENTRY, EXIT, NY, EmaCrossVwap


@dataclass
class Trade:
    symbol: str
    entry_time: pd.Timestamp
    entry_price: float
    exit_time: pd.Timestamp
    exit_price: float
    qty: int
    reason: str

    @property
    def pnl(self) -> float:
        return (self.exit_price - self.entry_price) * self.qty

    @property
    def return_pct(self) -> float:
        return (self.exit_price / self.entry_price - 1) * 100


@dataclass
class BacktestResult:
    symbol: str
    starting_equity: float
    trades: list[Trade]
    equity_curve: pd.Series
    stats: dict = field(default_factory=dict)


@dataclass
class _Position:
    qty: int
    entry_price: float
    entry_time: pd.Timestamp
    stop: float
    target: float


def run_backtest(
    bars: pd.DataFrame,
    settings: Settings,
    symbol: str = "SYMBOL",
    starting_equity: float = 100_000.0,
) -> BacktestResult:
    strategy = EmaCrossVwap(settings)
    df = strategy.prepare(bars)

    slip = settings.slippage_bps / 10_000
    local_index = df.index.tz_convert(NY)
    dates = local_index.date
    times = local_index.time

    cash = starting_equity
    position: _Position | None = None
    trades: list[Trade] = []
    equity_points: list[float] = []

    pending_entry: tuple[object, float] | None = None  # (session date, atr at signal)
    pending_exit = False
    current_date = None
    day_start_equity = starting_equity
    halted = False

    def close_position(ts: pd.Timestamp, raw_price: float, reason: str) -> None:
        nonlocal cash, position, halted
        assert position is not None
        fill = raw_price * (1 - slip)
        cash += position.qty * fill
        trades.append(
            Trade(symbol, position.entry_time, position.entry_price, ts, fill, position.qty, reason)
        )
        position = None
        if cash - day_start_equity <= -settings.max_daily_loss_pct * day_start_equity:
            halted = True

    def check_levels(ts: pd.Timestamp, o: float, h: float, l: float) -> None:
        assert position is not None
        if o <= position.stop:
            close_position(ts, o, "stop")
        elif l <= position.stop:
            close_position(ts, position.stop, "stop")
        elif o >= position.target:
            close_position(ts, o, "target")
        elif h >= position.target:
            close_position(ts, position.target, "target")

    n = len(df)
    opens = df["open"].to_numpy()
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    closes = df["close"].to_numpy()
    signals = df["signal"].to_numpy()
    atrs = df["atr"].to_numpy()

    for i in range(n):
        ts = df.index[i]
        o, h, l, c = opens[i], highs[i], lows[i], closes[i]
        bar_date, bar_time = dates[i], times[i]
        past_flatten = bar_time >= settings.flatten_time

        if bar_date != current_date:
            current_date = bar_date
            day_start_equity = cash + (position.qty * o if position else 0.0)
            halted = False

        # 1. Manage an open position at this bar's open / during this bar.
        if position is not None:
            if past_flatten:
                close_position(ts, o, "eod")
            elif pending_exit:
                close_position(ts, o, "signal")
            else:
                check_levels(ts, o, h, l)
        pending_exit = False

        # 2. Open a new position from the previous bar's signal.
        if pending_entry is not None:
            signal_date, signal_atr = pending_entry
            pending_entry = None
            if position is None and not halted and not past_flatten and signal_date == bar_date:
                equity = cash
                plan = plan_entry(settings, equity, o, signal_atr)
                if plan is not None:
                    fill = o * (1 + slip)
                    cash -= plan.qty * fill
                    position = _Position(plan.qty, fill, ts, plan.stop_price, plan.target_price)
                    check_levels(ts, o, h, l)

        # 3. Safety net: never hold through the end of a session.
        last_of_session = i == n - 1 or dates[i + 1] != bar_date
        if position is not None and last_of_session:
            close_position(ts, c, "eod")

        # 4. Read this bar's signal for execution on the next bar.
        if signals[i] == ENTRY and position is None:
            pending_entry = (bar_date, atrs[i])
        elif signals[i] == EXIT and position is not None:
            pending_exit = True

        equity_points.append(cash + (position.qty * c if position else 0.0))

    equity_curve = pd.Series(equity_points, index=df.index, name="equity")
    result = BacktestResult(symbol, starting_equity, trades, equity_curve)
    result.stats = compute_stats(result)
    return result


def compute_stats(result: BacktestResult) -> dict:
    trades = result.trades
    curve = result.equity_curve
    final = curve.iloc[-1] if len(curve) else result.starting_equity
    wins = [t.pnl for t in trades if t.pnl > 0]
    losses = [t.pnl for t in trades if t.pnl <= 0]
    gross_loss = -sum(losses)
    drawdown = (curve / curve.cummax() - 1).min() if len(curve) else 0.0

    return {
        "final_equity": round(float(final), 2),
        "total_return_pct": round((final / result.starting_equity - 1) * 100, 2),
        "trades": len(trades),
        "win_rate_pct": round(len(wins) / len(trades) * 100, 1) if trades else 0.0,
        "avg_trade_pnl": round(sum(t.pnl for t in trades) / len(trades), 2) if trades else 0.0,
        "profit_factor": round(sum(wins) / gross_loss, 2) if gross_loss > 0 else float("inf") if wins else 0.0,
        "max_drawdown_pct": round(float(drawdown) * 100, 2),
        "exits": {r: sum(1 for t in trades if t.reason == r) for r in sorted({t.reason for t in trades})},
    }


def format_report(result: BacktestResult) -> str:
    s = result.stats
    lines = [
        f"Backtest: {result.symbol}",
        f"  Period:          {result.equity_curve.index[0]:%Y-%m-%d} -> {result.equity_curve.index[-1]:%Y-%m-%d}"
        if len(result.equity_curve)
        else "  Period:          (no data)",
        f"  Starting equity: ${result.starting_equity:,.2f}",
        f"  Final equity:    ${s['final_equity']:,.2f}",
        f"  Total return:    {s['total_return_pct']:+.2f}%",
        f"  Max drawdown:    {s['max_drawdown_pct']:.2f}%",
        f"  Trades:          {s['trades']}",
        f"  Win rate:        {s['win_rate_pct']:.1f}%",
        f"  Avg trade P&L:   ${s['avg_trade_pnl']:,.2f}",
        f"  Profit factor:   {s['profit_factor']}",
        f"  Exits:           {s['exits']}",
    ]
    return "\n".join(lines)


def trades_frame(result: BacktestResult) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "symbol": t.symbol,
                "entry_time": t.entry_time,
                "entry_price": round(t.entry_price, 4),
                "exit_time": t.exit_time,
                "exit_price": round(t.exit_price, 4),
                "qty": t.qty,
                "pnl": round(t.pnl, 2),
                "return_pct": round(t.return_pct, 3),
                "reason": t.reason,
            }
            for t in result.trades
        ]
    )

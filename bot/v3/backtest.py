"""Bar-by-bar backtest of the v3.1 system on QQQ / TQQQ / SQQQ 5-minute bars.

Same conservative execution model as the original backtester:
- A signal on a closed bar is filled at the next bar's open.
- Within each bar, prices are visited open -> low -> high -> close, so a stop
  is assumed to hit before any profit level in the same bar. Levels fill at
  the level price, gaps fill at the open.
- Every fill pays SLIPPAGE_BPS.
- Positions are closed at FLATTEN_TIME, and at the last bar of each session.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

import pandas as pd

from ..config import Settings
from ..strategy import NY
from . import DATA_SYMBOLS, QQQ
from .position import V3Position, V3Trade, size_entry
from .rules import BAR, SessionScanner, prepare_session


@dataclass
class V3BacktestResult:
    starting_equity: float
    trades: list[V3Trade]
    daily_equity: pd.Series
    regimes: dict = field(default_factory=dict)
    stats: dict = field(default_factory=dict)


def split_sessions(bars: pd.DataFrame) -> dict:
    dates = bars.index.tz_convert(NY).date
    return {d: bars[dates == d] for d in sorted(set(dates))}


def run_v3_backtest(
    bars_by_symbol: dict[str, pd.DataFrame],
    settings: Settings,
    starting_equity: float = 100_000.0,
    risk_multiplier: float = 1.0,
) -> V3BacktestResult:
    s = settings
    missing = [sym for sym in DATA_SYMBOLS if sym not in bars_by_symbol]
    if missing:
        raise ValueError(f"v3 backtest needs bars for {', '.join(DATA_SYMBOLS)}; missing {missing}")

    slip = s.slippage_bps / 10_000
    fractional = s.use_fractional(starting_equity)
    pdt_limited = s.pdt_guard and s.is_small_account(starting_equity)
    sessions = {sym: split_sessions(bars_by_symbol[sym]) for sym in DATA_SYMBOLS}
    dates = sorted(set(sessions[QQQ]) & set(sessions[DATA_SYMBOLS[1]]) & set(sessions[DATA_SYMBOLS[2]]))

    cash = starting_equity
    trades: list[V3Trade] = []
    daily: dict = {}
    regimes: dict = {}
    trades_per_day: dict = {}
    seen_days: list = []

    for day in dates:
        seen_days.append(day)
        frames = {sym: prepare_session(sessions[sym][day], s.v3_band_sd) for sym in DATA_SYMBOLS}
        scanner = SessionScanner(s)
        day_start = cash
        halted = False
        position: V3Position | None = None
        pending = None
        last_exit = None
        index = frames[QQQ].index

        def close_out(ts_et, price, reason):
            nonlocal cash, position, halted, last_exit
            fill = price * (1 - slip)
            cash += position.qty * fill
            trades.append(position.finish(fill, ts_et, reason))
            trades_per_day[day] = trades_per_day.get(day, 0) + 1
            position = None
            last_exit = ts_et
            if cash - day_start <= -s.v3_max_daily_loss_pct * day_start:
                halted = True

        def handle(events, ts_et):
            nonlocal cash
            for ev in events:
                if ev.kind == "scale":
                    fill = ev.price * (1 - slip)
                    position.fill_scale(ev.qty, fill)
                    cash += ev.qty * fill
                elif ev.kind == "exit":
                    close_out(ts_et, ev.price, ev.reason)
                    return

        for i, ts in enumerate(index):
            start_et = ts.tz_convert(NY).to_pydatetime()
            end_et = (ts + BAR).tz_convert(NY).to_pydatetime()
            past_flatten = start_et.time() >= s.flatten_time

            # 1. Enter from the previous bar's signal.
            if pending is not None and position is None:
                sig = pending
                bar = frames[sig.symbol]
                recent = sum(trades_per_day.get(d, 0) for d in seen_days[-5:])
                blocked = halted or past_flatten or (pdt_limited and recent >= 3)
                if ts in bar.index and not blocked:
                    open_p = float(bar.at[ts, "open"])
                    qty = size_entry(s, cash, open_p, fractional, buying_power=cash, risk_multiplier=risk_multiplier)
                    if qty > 0:
                        fill = open_p * (1 + slip)
                        cash -= qty * fill
                        position = V3Position.open(s, sig.strategy, sig.symbol, fill, qty, start_et, sig.target)
            pending = None

            # 2. Manage the open position through this bar.
            if position is not None:
                bar = frames[position.symbol]
                if ts in bar.index:
                    row = bar.loc[ts]
                    if past_flatten:
                        close_out(start_et, float(row.open), "EOD")
                    else:
                        handle(position.on_price(float(row.open), start_et, s, fill_at_level=False), start_et)
                        for price in (row.low, row.high, row.close):
                            if position is None:
                                break
                            handle(position.on_price(float(price), end_et, s, fill_at_level=True), end_et)

            # 3. Never hold past the session.
            if position is not None and i == len(index) - 1:
                close_out(end_et, float(frames[position.symbol]["close"].iloc[-1]), "EOD")

            # 4. Scan the closed bar for a new entry.
            signal = scanner.on_bar_close(ts, frames)
            cooling = last_exit is not None and end_et - last_exit < timedelta(minutes=s.v3_cooldown_min)
            in_hours = end_et.time() < s.v3_entry_cutoff
            if signal is not None and position is None and not halted and not cooling and in_hours:
                pending = signal

        daily[day] = cash
        regimes[day] = scanner.regime

    result = V3BacktestResult(starting_equity, trades, pd.Series(daily, dtype=float), regimes)
    result.stats = v3_stats(result)
    return result


def v3_stats(result: V3BacktestResult) -> dict:
    trades = result.trades
    curve = result.daily_equity
    final = float(curve.iloc[-1]) if len(curve) else result.starting_equity
    wins = [t.pnl for t in trades if t.pnl > 0]
    losses = [t.pnl for t in trades if t.pnl <= 0]
    peak = pd.concat([pd.Series([result.starting_equity]), curve.reset_index(drop=True)]).cummax().iloc[1:]
    drawdown = float((curve.values / peak.values - 1).min()) * 100 if len(curve) else 0.0
    by_strategy = {}
    for name in sorted({t.strategy for t in trades}):
        group = [t for t in trades if t.strategy == name]
        by_strategy[name] = {
            "trades": len(group),
            "pnl": round(sum(t.pnl for t in group), 2),
            "avg_r": round(sum(t.realized_r for t in group) / len(group), 2),
        }
    return {
        "final_equity": round(final, 2),
        "total_return_pct": round((final / result.starting_equity - 1) * 100, 2),
        "trades": len(trades),
        "win_rate_pct": round(len(wins) / len(trades) * 100, 1) if trades else 0.0,
        "avg_r": round(sum(t.realized_r for t in trades) / len(trades), 3) if trades else 0.0,
        "profit_factor": round(sum(wins) / -sum(losses), 2) if losses and sum(losses) < 0 else (float("inf") if wins else 0.0),
        "max_drawdown_pct": round(drawdown, 2),
        "by_strategy": by_strategy,
        "regime_days": {r: sum(1 for v in result.regimes.values() if v == r) for r in sorted({str(v) for v in result.regimes.values()})},
    }


def format_v3_report(result: V3BacktestResult) -> str:
    s = result.stats
    lines = [
        "Backtest: v3.1 (QQQ regime -> TQQQ / SQQQ)",
        f"  Period:          {result.daily_equity.index[0]} -> {result.daily_equity.index[-1]}" if len(result.daily_equity) else "  Period:          (no data)",
        f"  Starting equity: ${result.starting_equity:,.2f}",
        f"  Final equity:    ${s['final_equity']:,.2f}",
        f"  Total return:    {s['total_return_pct']:+.2f}%",
        f"  Max drawdown:    {s['max_drawdown_pct']:.2f}%",
        f"  Trades:          {s['trades']}",
        f"  Win rate:        {s['win_rate_pct']:.1f}%",
        f"  Avg trade:       {s['avg_r']:+.3f}R",
        f"  Profit factor:   {s['profit_factor']}",
        f"  Regime days:     {s['regime_days']}",
    ]
    for name, st in s["by_strategy"].items():
        lines.append(f"    {name:<14} {st['trades']:>4} trades  P&L ${st['pnl']:>+10,.2f}  avg {st['avg_r']:+.2f}R")
    return "\n".join(lines)

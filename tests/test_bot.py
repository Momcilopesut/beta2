from datetime import time, timedelta

import pandas as pd
import pytest

from bot.backtest import run_backtest
from bot.config import Settings
from bot.data import synthetic_bars
from bot.risk import plan_entry
from bot.strategy import ENTRY, EXIT, NY, EmaCrossVwap


def make_session(closes, date="2025-03-03", minutes=5, volume=1000.0, spread=0.05):
    """One session of bars starting 9:30 ET with the given closes (open = previous close)."""
    start = pd.Timestamp(f"{date} 09:30").tz_localize(NY)
    index = [start + timedelta(minutes=minutes * i) for i in range(len(closes))]
    rows = []
    prev = closes[0]
    for c in closes:
        o = prev
        rows.append((o, max(o, c) + spread, min(o, c) - spread, c, volume))
        prev = c
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"], index=pd.DatetimeIndex(index))
    df.index = df.index.tz_convert("UTC")
    return df


def settings(**overrides):
    s = Settings(ema_fast=3, ema_slow=6, atr_period=3, slippage_bps=0, entry_start=time(9, 30))
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


def trend_with_pullback(n_up=30, start=100.0, step=0.2):
    """Rise for 15 bars, pull back for 5, then rise again for n_up bars.

    The pullback makes the EMAs cross down and then cross back up while price
    is still above the session VWAP, which produces exactly one entry signal.
    """
    rise = [start + step * i for i in range(15)]
    dip = [rise[-1] - step * (i + 1) for i in range(5)]
    up = [dip[-1] + step * (i + 1) for i in range(n_up)]
    return rise + dip + up


def test_entry_signal_after_bullish_cross():
    df = EmaCrossVwap(settings()).prepare(make_session(trend_with_pullback()))
    entries = df.index[df["signal"] == ENTRY]
    assert len(entries) == 1
    row = df.loc[entries[0]]
    assert row.ema_fast > row.ema_slow and row.close > row.vwap


def test_exit_signal_after_bearish_cross():
    closes = trend_with_pullback() + [trend_with_pullback()[-1] - 0.3 * (i + 1) for i in range(15)]
    df = EmaCrossVwap(settings()).prepare(make_session(closes))
    assert (df["signal"] == EXIT).any()


def test_no_entries_outside_window():
    s = settings(entry_start=time(15, 0))
    df = EmaCrossVwap(s).prepare(make_session(trend_with_pullback()))
    assert not (df["signal"] == ENTRY).any()


def test_position_sizing_respects_risk_and_cap():
    s = Settings(risk_per_trade=0.01, max_position_pct=0.25, stop_atr=2.0, target_atr=4.0)
    plan = plan_entry(s, equity=100_000, price=50.0, atr_value=0.5)
    # risk: 1000 / (2*0.5) = 1000 shares; cap: 25000/50 = 500 shares
    assert plan.qty == 500
    assert plan.stop_price == 49.0 and plan.target_price == 52.0
    assert plan_entry(s, equity=100, price=500.0, atr_value=1.0) is None


def test_backtest_fills_next_bar_open_and_flattens_eod():
    s = settings(target_atr=100.0, stop_atr=100.0)  # levels far away
    bars = make_session(trend_with_pullback())
    result = run_backtest(bars, s, starting_equity=10_000)
    df = EmaCrossVwap(s).prepare(bars)
    signal_ts = df.index[df["signal"] == ENTRY][0]
    next_ts = df.index[df.index.get_loc(signal_ts) + 1]

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.entry_time == next_ts
    assert trade.entry_price == pytest.approx(bars.loc[next_ts, "open"])
    assert trade.reason == "eod"  # data ends before flatten time, safety net closes it


def test_backtest_flatten_time_closes_position():
    s = settings(target_atr=100.0, stop_atr=100.0)
    closes = trend_with_pullback(n_up=78 - 20)  # full 6.5h session of 5-min bars
    result = run_backtest(make_session(closes), s, starting_equity=10_000)
    assert result.trades[-1].reason == "eod"
    assert result.trades[-1].exit_time.tz_convert(NY).time() == time(15, 50)


def test_stop_loss_hit():
    s = settings(stop_atr=0.5, target_atr=50.0)
    closes = trend_with_pullback()[:26] + [95.0] * 10
    result = run_backtest(make_session(closes), s, starting_equity=10_000)
    assert result.trades and result.trades[0].reason == "stop"
    assert result.trades[0].pnl < 0


def test_never_holds_overnight():
    s = Settings()
    bars = synthetic_bars(days=15, seed=7)
    result = run_backtest(bars, s)
    for t in result.trades:
        assert t.entry_time.tz_convert(NY).date() == t.exit_time.tz_convert(NY).date()
    # equity curve only marks cash at each session's last bar => no open positions
    assert result.stats["trades"] == len(result.trades)

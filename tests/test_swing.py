from datetime import date

import numpy as np
import pandas as pd
import pytest

from bot.swing import UNIVERSE, prepare, rsi, run_swing_backtest, synthetic_daily, verdict


def test_rsi_extremes():
    up = pd.Series(np.arange(1, 30, dtype=float))
    down = pd.Series(np.arange(30, 1, -1, dtype=float))
    assert rsi(up, 2).iloc[-1] == pytest.approx(100.0)
    assert rsi(down, 2).iloc[-1] < 1


def make_dip_data():
    """Steady uptrend for 220 days, then a 3-day dip and a recovery, for every fund."""
    n = 240
    close = np.linspace(100, 150, n)
    close[225:228] = close[224] * np.array([0.98, 0.96, 0.95])
    close[228:] = close[224] * 1.02
    days = [d.date() for d in pd.bdate_range("2023-01-02", periods=n)]
    df = pd.DataFrame({"open": close, "high": close * 1.001, "low": close * 0.999, "close": close, "volume": 1e6},
                      index=pd.Index(days, name="date"))
    return {s: df.copy() for s in UNIVERSE}, days


def test_buys_next_open_after_dip_and_sells_after_recovery():
    data, days = make_dip_data()
    f = prepare(data["SPY"])
    signal_day = next(d for d in days[200:] if f.loc[d, "rsi"] < 10)
    result = run_swing_backtest(data, days[205], days[-1], 1000.0, 0.0)
    spy = [t for t in result.trades if t.symbol == "SPY"]
    assert len(spy) == 1
    t = spy[0]
    assert t.entry_date == days[days.index(signal_day) + 1]  # next day's open, never the signal day
    assert t.exit_reason == "RECOVERED" and t.ret > 0
    assert t.qty * t.entry_price <= 1000 / 4 + 1e-6  # quarter of the account per fund


def test_random_data_has_no_timing_edge_on_average():
    excess = []
    for seed in range(1, 6):
        data = synthetic_daily(years=3, seed=seed)
        r = run_swing_backtest(data, date(2022, 1, 1), date(2023, 11, 30), 500.0, 2.0)
        excess += r.excess
    assert abs(np.mean(excess)) < 0.01  # within 1% per trade of zero
    passed, msg = verdict(r)
    assert "RESULT" in msg

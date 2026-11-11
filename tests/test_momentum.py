from datetime import date

import numpy as np
import pandas as pd
import pytest

from bot.momentum import UNIVERSE, month_ends, run_momentum_backtest, stats, target_weights, _closes


def build(trends: dict, days=600, start="2020-01-01"):
    """Each ETF grows at a constant daily rate (per `trends`, default 0)."""
    cal = [d.date() for d in pd.bdate_range(start, periods=days)]
    out = {}
    for s in UNIVERSE:
        px = 100 * (1 + trends.get(s, 0.0)) ** np.arange(days)
        out[s] = pd.DataFrame({"open": px, "high": px, "low": px, "close": px, "volume": 1e6},
                              index=pd.Index(cal, name="date"))
    return out, cal


def test_picks_top_three_rising_and_skips_falling():
    data, cal = build({"GLD": 0.002, "QQQ": 0.0015, "TLT": 0.001, "EEM": 0.0005, "SPY": -0.001})
    closes = _closes(data, cal, "close")
    ends = month_ends(cal)
    m = ends[-2]
    w = target_weights(closes, closes.loc[ends], cal.index(m), m)
    assert set(w) == {"GLD", "QQQ", "TLT"} and all(v == pytest.approx(1 / 3) for v in w.values())


def test_goes_to_cash_when_everything_falls():
    data, cal = build({s: -0.001 for s in UNIVERSE})
    r = run_momentum_backtest(data, cal[400], cal[-1], 1000.0, 0.0)
    assert all(w == {} for _, w in r.holdings)
    assert r.equity.iloc[-1] == pytest.approx(1000.0)


def test_trades_on_first_day_after_month_end_only():
    data, cal = build({"GLD": 0.002, "QQQ": 0.0015, "TLT": 0.001})
    r = run_momentum_backtest(data, cal[400], cal[-1], 1000.0, 2.0)
    ends = set(month_ends(cal))
    for d, _ in r.holdings:
        prev = cal[cal.index(d) - 1]
        assert prev in ends  # rebalances happen only at the open right after a month-end


def test_no_lookahead_jump_after_month_end_is_not_used():
    data, cal = build({"GLD": 0.001, "QQQ": 0.0009, "TLT": 0.0008})
    ends = month_ends(cal)
    m = ends[-3]
    i = cal.index(m)
    # IWM explodes the day after month-end: the decision at m must not see it.
    data["IWM"].loc[cal[i + 1]:, ["open", "high", "low", "close"]] *= 3
    closes = _closes(data, cal, "close")
    w = target_weights(closes, closes.loc[ends], i, m)
    assert "IWM" not in w


def test_stats_drawdown_and_sharpe():
    cal = [d.date() for d in pd.bdate_range("2020-01-01", periods=300)]
    curve = pd.Series(np.linspace(100, 130, 300), index=cal)
    curve.iloc[150:160] *= 0.9
    s = stats(curve)
    assert s["max_dd"] == pytest.approx(-0.1, abs=0.01)
    assert s["total"] == pytest.approx(0.3)

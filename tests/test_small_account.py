"""Small-account behaviour: fractional sizing, bot-managed stops, day-trade guard."""

from datetime import datetime, time, timedelta, timezone
from types import SimpleNamespace

import pandas as pd
import pytest

import bot.live as live
from bot.backtest import run_backtest
from bot.config import Settings
from bot.risk import plan_entry
from bot.strategy import ENTRY, NY, EmaCrossVwap
from tests.test_bot import make_session, settings as strat_settings, trend_with_pullback

# ---- sizing -----------------------------------------------------------------


def test_whole_shares_impossible_but_fractional_works():
    s = Settings()
    # $500 account, $757 stock: no whole share fits.
    assert plan_entry(s, equity=500, price=757.0, atr_value=1.0) is None
    plan = plan_entry(s, equity=500, price=757.0, atr_value=1.0, fractional=True)
    assert plan is not None
    assert 0 < plan.qty < 1
    assert plan.qty * 757.0 <= 500 * s.small_account_max_position_pct + 1e-6


def test_buying_power_caps_size():
    s = Settings()
    plan = plan_entry(s, equity=500, price=100.0, atr_value=1.0, fractional=True, buying_power=50)
    assert plan.qty * 100.0 <= 49.0 + 1e-6


def test_fractional_mode_selection():
    s = Settings()
    assert s.use_fractional(500) and not s.use_fractional(100_000)
    s.fractional_shares = "false"
    assert not s.use_fractional(500)
    s.fractional_shares = "true"
    assert s.use_fractional(100_000)


def test_backtest_small_account_trades_fractions():
    s = strat_settings(target_atr=100.0, stop_atr=100.0)
    bars = make_session(trend_with_pullback())
    bars[["open", "high", "low", "close"]] *= 7  # ~$700 stock
    result = run_backtest(bars, s, starting_equity=500)
    assert len(result.trades) == 1
    assert 0 < result.trades[0].qty < 1


def test_backtest_pdt_guard_limits_day_trades():
    s = strat_settings(target_atr=100.0, stop_atr=100.0)
    days = pd.bdate_range("2025-03-03", periods=5)
    bars = pd.concat([make_session(trend_with_pullback(), date=str(d.date())) for d in days])
    small = run_backtest(bars, s, starting_equity=500)
    big = run_backtest(bars, s, starting_equity=100_000)
    assert len(big.trades) > 3
    assert len(small.trades) == 3


# ---- live trader with a fake Alpaca client ----------------------------------


class FakeClient:
    def __init__(self, equity=500.0, daytrade_count=0):
        self.account = SimpleNamespace(
            equity=str(equity), last_equity=str(equity), buying_power=str(equity), daytrade_count=daytrade_count
        )
        self.positions = []
        self.orders = []
        self.submitted = []
        self.closed = []

    def get_clock(self):
        return SimpleNamespace(is_open=True)

    def get_account(self):
        return self.account

    def get_all_positions(self):
        return self.positions

    def get_orders(self, _req):
        return self.orders

    def submit_order(self, req):
        self.submitted.append(req)
        return SimpleNamespace(id="order-1")

    def close_position(self, symbol):
        self.closed.append(symbol)

    def close_all_positions(self, cancel_orders=True):
        self.closed.append("ALL")


def make_trader(client, monkeypatch, bars, now):
    s = strat_settings(symbols=["SPY"], learning_enabled=False)
    trader = live.LiveTrader.__new__(live.LiveTrader)
    trader.base = s
    trader.s = s
    trader.strategy = EmaCrossVwap(s)
    trader.client = client
    trader.last_bar_seen = {}
    trader.levels = {}
    trader.halted_date = trader.flattened_date = trader.traded_date = None
    trader.learned_date = trader.pdt_warned_date = None
    monkeypatch.setattr(live, "fetch_alpaca_bars", lambda *a, **k: bars)

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr(live, "datetime", FixedDatetime)
    return trader


def bars_ending_in_entry():
    s = strat_settings()
    bars = make_session(trend_with_pullback())
    bars[["open", "high", "low", "close"]] *= 7
    df = EmaCrossVwap(s).prepare(bars)
    entry_ts = df.index[df["signal"] == ENTRY][0]
    bars = bars.loc[:entry_ts]
    now = (entry_ts + timedelta(minutes=5, seconds=10)).to_pydatetime()
    return bars, now


def test_live_small_account_buys_fraction_and_watches_stop(monkeypatch):
    bars, now = bars_ending_in_entry()
    client = FakeClient(equity=500)
    trader = make_trader(client, monkeypatch, bars, now)

    trader.tick()
    assert len(client.submitted) == 1
    order = client.submitted[0]
    assert order.order_class is None or str(order.order_class).lower().endswith("simple")
    assert 0 < float(order.qty) < 1
    levels = trader.levels["SPY"]

    # Position filled; price then falls through the stop.
    client.positions = [
        SimpleNamespace(symbol="SPY", qty=str(order.qty), avg_entry_price="700", current_price=str(levels.stop - 0.5))
    ]
    trader.tick()
    assert client.closed == ["SPY"]
    assert "SPY" not in trader.levels


def test_live_day_trade_guard_blocks_entry(monkeypatch):
    bars, now = bars_ending_in_entry()
    client = FakeClient(equity=500, daytrade_count=3)
    trader = make_trader(client, monkeypatch, bars, now)
    trader.tick()
    assert client.submitted == []


def test_live_large_account_uses_bracket(monkeypatch):
    bars, now = bars_ending_in_entry()
    client = FakeClient(equity=100_000, daytrade_count=10)  # guard doesn't apply above $25k
    trader = make_trader(client, monkeypatch, bars, now)
    trader.tick()
    assert len(client.submitted) == 1
    assert str(client.submitted[0].order_class).lower().endswith("bracket")
    assert float(client.submitted[0].qty).is_integer()

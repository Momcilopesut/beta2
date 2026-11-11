from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from bot.config import Settings
from bot.swing_live import SwingTrader

ET = ZoneInfo("America/New_York")


def series(closes, end="2024-06-14"):
    days = [d.date() for d in pd.bdate_range(end=end, periods=len(closes))]
    c = np.array(closes, dtype=float)
    return pd.DataFrame({"open": c, "high": c, "low": c, "close": c, "volume": 1e6}, index=pd.Index(days, name="date"))


UPTREND = list(np.linspace(100, 150, 250))
DIP = UPTREND[:-3] + [UPTREND[-4] * f for f in (0.985, 0.97, 0.955)]  # sharp 3-day dip in an uptrend
FLAT = [100.0] * 250


class FakeAlpaca:
    def __init__(self, equity=500.0):
        self.equity = equity
        self.positions = {}
        self.orders = []
        self.closed = []

    def get_clock(self):
        return SimpleNamespace(is_open=True)

    def get_account(self):
        return SimpleNamespace(equity=str(self.equity), buying_power=str(self.equity))

    def get_all_positions(self):
        return list(self.positions.values())

    def submit_order(self, req):
        self.orders.append(req)
        return SimpleNamespace(id=f"o{len(self.orders)}")

    def get_order_by_id(self, oid):
        req = self.orders[int(oid[1:]) - 1]
        return SimpleNamespace(status=SimpleNamespace(value="filled"), filled_avg_price="140.0",
                               filled_qty=str(float(req.notional) / 140.0))

    def close_position(self, sym):
        self.closed.append(sym)
        p = self.positions.pop(sym)
        return SimpleNamespace(id=None, qty=p.qty)


def make(tmp_path, client, data):
    s = Settings(api_key="k", api_secret="s")
    fetch = lambda _s, sym, _a, _b: data[sym]
    return SwingTrader(s, client=client, fetch=fetch, state_path=str(tmp_path / "swing.json"),
                       trade_log=str(tmp_path / "trades.csv"))


def morning(day="2024-06-17"):
    return datetime.fromisoformat(f"{day}T09:31:00").replace(tzinfo=ET)


def test_buys_dip_with_quarter_of_account_and_runs_once_per_day(tmp_path):
    client = FakeAlpaca(equity=500)
    data = {"SPY": series(DIP), "QQQ": series(UPTREND), "IWM": series(FLAT), "DIA": series(UPTREND)}
    trader = make(tmp_path, client, data)
    result = trader.run_once(morning())
    assert result["bought"] == ["SPY"]
    order = client.orders[0]
    assert order.symbol == "SPY" and float(order.notional) == pytest.approx(125.0)
    assert str(order.time_in_force).lower().endswith("day")
    assert trader.run_once(morning()) == {"sold": [], "bought": []}  # second run same day: nothing
    assert len(client.orders) == 1


def test_sells_after_recovery_and_logs_trade(tmp_path):
    client = FakeAlpaca()
    client.positions["SPY"] = SimpleNamespace(symbol="SPY", qty="0.9", avg_entry_price="139.0", current_price="150")
    recovered = DIP + [UPTREND[-1] * 1.01]
    data = {"SPY": series(recovered, end="2024-06-17"), "QQQ": series(UPTREND, end="2024-06-17"),
            "IWM": series(FLAT, end="2024-06-17"), "DIA": series(UPTREND, end="2024-06-17")}
    trader = make(tmp_path, client, data)
    trader.state["positions"]["SPY"] = {"entry_date": "2024-06-14", "entry_price": 139.0, "days": 0}
    result = trader.run_once(morning("2024-06-18"))
    assert result["sold"] == ["SPY"] and client.closed == ["SPY"]
    log = pd.read_csv(tmp_path / "trades.csv")
    assert log.iloc[0]["reason"] == "RECOVERED"


def test_time_limit_after_ten_days(tmp_path):
    client = FakeAlpaca()
    client.positions["IWM"] = SimpleNamespace(symbol="IWM", qty="1", avg_entry_price="100", current_price="99")
    falling = FLAT[:-12] + [100 - i * 0.5 for i in range(12)]  # never recovers above its 5-day average
    data = {"SPY": series(UPTREND), "QQQ": series(UPTREND), "IWM": series(falling), "DIA": series(UPTREND)}
    trader = make(tmp_path, client, data)
    entry = str(data["IWM"].index[-10])
    trader.state["positions"]["IWM"] = {"entry_date": entry, "entry_price": 100.0, "days": 0}
    result = trader.run_once(morning())
    assert result["sold"] == ["IWM"]
    assert pd.read_csv(tmp_path / "trades.csv").iloc[0]["reason"] == "TIME_LIMIT"

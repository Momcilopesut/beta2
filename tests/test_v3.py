"""Tests for the v3.1 system: regime, trade management, backtest and live trader."""

from datetime import datetime, time, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from bot.config import Settings
from bot.data import synthetic_leveraged
from bot.learning import LearningState, run_daily_learning
from bot.v3 import ROTATIONAL, TREND_BEAR, TREND_BULL
from bot.v3.backtest import run_v3_backtest
from bot.v3.live import V3LiveTrader
from bot.v3.position import V3Position, size_entry
from bot.v3.rules import SessionScanner, classify_regime, prepare_session

ET = ZoneInfo("America/New_York")
S = Settings(strategy="v3", slippage_bps=0)


def at(h, m, day=3):
    return datetime(2026, 3, day, h, m, tzinfo=ET)


def session(closes, start="2026-03-03 09:30", volume=1000.0):
    idx = pd.date_range(pd.Timestamp(start, tz=ET), periods=len(closes), freq="5min").tz_convert("UTC")
    rows, prev = [], closes[0]
    for c in closes:
        rows.append((prev, max(prev, c) * 1.0005, min(prev, c) * 0.9995, c, volume))
        prev = c
    return pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"], index=idx)


# ---- regime ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "closes, expected",
    [
        ([100, 100.2, 100.4, 100.6, 100.8, 101.0], TREND_BULL),
        ([100, 99.8, 99.6, 99.4, 99.2, 99.0], TREND_BEAR),
        ([100, 100.1, 99.9, 100.05, 99.95, 100.0], ROTATIONAL),
    ],
)
def test_regime(closes, expected):
    regime, _, _ = classify_regime(prepare_session(session(closes), 2.0), S)
    assert regime == expected


def test_mixed_signals_are_not_bearish():
    # Price ends up >0.5% above the open, but VWAP slopes down (big early volume at highs).
    closes = [100, 99.0, 98.5, 98.8, 99.5, 100.6]
    df = session(closes)
    df.loc[df.index[0], "volume"] = 1.0
    df.loc[df.index[1:4], "volume"] = 100_000.0
    df.loc[df.index[0], "high"] = 100.0
    regime, pct, slope = classify_regime(prepare_session(df, 2.0), S)
    assert pct > 0.5 and slope < 0
    assert regime == ROTATIONAL


# ---- trade management ---------------------------------------------------------


def test_sizing_one_r_and_cap():
    # $500, 0.5% risk = $2.50, stop 0.8% of $100 = $0.80/share -> 3.125 shares,
    # but the 50% cap allows $250 -> 2.5 shares.
    assert size_entry(S, 500, 100.0, fractional=True) == pytest.approx(2.5)
    assert size_entry(S, 100_000, 100.0, fractional=False) == 500  # 1R: 500/0.8=625 > cap 500


def test_shield_scale_and_profit_lock_pnl():
    pos = V3Position.open(S, "T", "TQQQ", 100.0, 2.0, at(12, 30))
    risk = pos.risk_per_share
    assert risk == pytest.approx(0.8)

    ev = pos.on_price(100 + 0.6 * risk, at(12, 35), S, fill_at_level=True)
    assert [e.kind for e in ev] == ["shield"] and pos.stop_price == 100.0

    ev = pos.on_price(100 + 0.7 * risk, at(12, 40), S, fill_at_level=True)  # afternoon: scale at +0.7R
    assert [e.kind for e in ev] == ["scale"]
    pos.fill_scale(ev[0].qty, ev[0].price)
    assert pos.qty == pytest.approx(1.0)
    assert pos.stop_price == pytest.approx(100 + 0.25 * risk)  # afternoon lock

    ev = pos.on_price(100.0, at(12, 45), S, fill_at_level=True)
    assert ev[-1].kind == "exit" and ev[-1].reason == "PROFIT_LOCK"
    trade = pos.finish(ev[-1].price, at(12, 45), ev[-1].reason)
    # half at +0.7R, half at +0.25R -> +0.475R
    assert trade.realized_r == pytest.approx(0.475)
    assert trade.pnl == pytest.approx(2 * 0.8 * 0.475)
    assert trade.mfe_r == pytest.approx(0.7)


def test_morning_scale_needs_one_r_and_stop_and_time_stop():
    pos = V3Position.open(S, "T", "TQQQ", 100.0, 2.0, at(10, 0))
    assert pos.on_price(100.6, at(10, 5), S, True)[0].kind == "shield"
    assert pos.on_price(100.7, at(10, 6), S, True) == []  # +0.875R < 1R before noon
    pos2 = V3Position.open(S, "T", "TQQQ", 100.0, 2.0, at(10, 0))
    ev = pos2.on_price(99.0, at(10, 5), S, fill_at_level=True)
    assert ev[0].reason == "STOP" and ev[0].price == pytest.approx(99.2)
    pos3 = V3Position.open(S, "T", "TQQQ", 100.0, 2.0, at(10, 0))
    assert pos3.on_price(100.1, at(10, 29), S, False) == []
    assert pos3.on_price(100.1, at(10, 30), S, False)[0].reason == "TIME_STOP"


# ---- backtest -------------------------------------------------------------------


def test_backtest_rules_hold():
    data = synthetic_leveraged(days=30, seed=3)
    small = run_v3_backtest(data, S, starting_equity=500)
    assert small.trades, "expected some trades on 30 days"
    for t in small.trades:
        assert t.entry_time.date() == t.exit_time.date()  # never overnight
        assert t.exit_time.time() <= time(15, 50)
        assert t.symbol in ("TQQQ", "SQQQ")
    # one position at a time
    ordered = sorted(small.trades, key=lambda t: t.entry_time)
    for a, b in zip(ordered, ordered[1:]):
        assert b.entry_time >= a.exit_time
    # day-trade guard: never more than 3 round trips in any 5 sessions
    days = sorted({t.entry_time.date() for t in small.trades} | set(small.daily_equity.index))
    per_day = {d: sum(1 for t in small.trades if t.entry_time.date() == d) for d in days}
    all_days = list(small.daily_equity.index)
    for i in range(len(all_days)):
        assert sum(per_day.get(d, 0) for d in all_days[max(0, i - 4) : i + 1]) <= 3
    big = run_v3_backtest(data, S, starting_equity=100_000)
    assert len(big.trades) >= len(small.trades)


def test_learning_runs_for_v3(tmp_path):
    s = Settings(strategy="v3", state_path=str(tmp_path / "v3.json"), learn_train_days=8, learn_val_days=4,
                 learn_candidates=3, learn_min_trades=2)
    report = run_daily_learning(s, synthetic_leveraged(days=14, seed=5), LearningState(s.state_path))
    assert report.candidates_tested > 0
    assert set(report.params) == {"v3_stop_pct", "v3_shield_r", "v3_scale_r_am", "v3_time_stop_min", "v3_band_sd"}


# ---- live trader with a fake Alpaca -------------------------------------------------


class FakeAlpaca:
    def __init__(self, equity=500.0, last_equity=None, daytrades=0):
        self.account = SimpleNamespace(equity=str(equity), last_equity=str(last_equity or equity),
                                       buying_power=str(equity), daytrade_count=daytrades)
        self.positions = {}
        self.submitted, self.closed = [], []
        self.fill_price = 50.0

    def get_clock(self):
        return SimpleNamespace(is_open=True)

    def get_account(self):
        return self.account

    def get_all_positions(self):
        return list(self.positions.values())

    def submit_order(self, req):
        self.submitted.append(req)
        if str(req.side).lower().endswith("buy"):
            self.positions[req.symbol] = SimpleNamespace(
                symbol=req.symbol, qty=str(req.qty), avg_entry_price=str(self.fill_price), current_price=str(self.fill_price))
        else:
            p = self.positions[req.symbol]
            p.qty = str(float(p.qty) - float(req.qty))
        return SimpleNamespace(id=f"o{len(self.submitted)}", qty=req.qty)

    def get_order_by_id(self, oid):
        req = self.submitted[int(oid[1:]) - 1]
        return SimpleNamespace(status=SimpleNamespace(value="filled"), filled_avg_price=str(self.fill_price), filled_qty=str(req.qty))

    def close_position(self, symbol):
        p = self.positions.pop(symbol)
        self.closed.append(symbol)
        return SimpleNamespace(id=None, qty=p.qty)

    def cancel_order_by_id(self, oid):
        pass

    def set_price(self, symbol, price):
        self.positions[symbol].current_price = str(price)


def make_trader(tmp_path, client, monkeypatch, frames=None):
    s = Settings(strategy="v3", api_key="k", api_secret="s", state_path=str(tmp_path / "v3.json"),
                 learning_enabled=False)
    trader = V3LiveTrader(s, client=client)
    trader.trade_log = str(tmp_path / "trades.csv")
    import bot.v3.live as live_mod

    if frames is not None:
        monkeypatch.setattr(live_mod, "fetch_alpaca_bars", lambda _s, sym, _start: frames[sym])
    else:
        monkeypatch.setattr(live_mod, "fetch_alpaca_bars", lambda *a: pd.DataFrame(
            columns=["open", "high", "low", "close", "volume"], index=pd.DatetimeIndex([], tz="UTC")))
    return trader


def orb_day():
    """A session where QQQ and TQQQ break out at 9:50 -> ORB signal on the bar closing 9:50."""
    q = [100, 100.1, 100.15, 100.4]
    t = [50, 50.1, 50.15, 50.6]
    s_ = [20, 19.97, 19.95, 19.85]
    return {"QQQ": session(q), "TQQQ": session(t), "SQQQ": session(s_)}


def test_live_orb_entry_is_fractional_day_order(tmp_path, monkeypatch):
    client = FakeAlpaca(equity=500)
    trader = make_trader(tmp_path, client, monkeypatch, orb_day())
    trader.tick(now=at(9, 50, 3).replace(second=20).astimezone(ZoneInfo("UTC")))
    assert len(client.submitted) == 1
    order = client.submitted[0]
    assert order.symbol == "TQQQ" and str(order.time_in_force).lower().endswith("day")
    assert 0 < float(order.qty) < 10 and not float(order.qty).is_integer()
    assert trader.position.strategy == "STRAT_1_ORB"
    # Saved for restarts
    assert (tmp_path / "v3_position.json").exists()


def test_live_stop_still_managed_after_loss_limit(tmp_path, monkeypatch):
    client = FakeAlpaca(equity=490, last_equity=500)  # -2% today: loss limit hit
    client.positions["TQQQ"] = SimpleNamespace(symbol="TQQQ", qty="2.0", avg_entry_price="50", current_price="50")
    trader = make_trader(tmp_path, client, monkeypatch)
    now = at(11, 0).astimezone(ZoneInfo("UTC"))
    trader.tick(now=now)
    assert trader.position is not None and trader.position.strategy == "ADOPTED"
    assert trader.halted_date is not None
    client.set_price("TQQQ", 49.0)  # below the 0.8% stop
    client.fill_price = 49.0
    trader.tick(now=now + timedelta(seconds=15))
    assert client.closed == ["TQQQ"] and trader.position is None
    log = pd.read_csv(tmp_path / "trades.csv")
    assert log.iloc[0]["exit_reason"] == "STOP"


def test_live_eod_closes_only_v3_symbols(tmp_path, monkeypatch):
    client = FakeAlpaca()
    client.positions["SQQQ"] = SimpleNamespace(symbol="SQQQ", qty="3", avg_entry_price="20", current_price="20.05")
    client.positions["SPY"] = SimpleNamespace(symbol="SPY", qty="0.2", avg_entry_price="700", current_price="701")
    trader = make_trader(tmp_path, client, monkeypatch)
    trader.tick(now=at(15, 51).astimezone(ZoneInfo("UTC")))
    assert client.closed == ["SQQQ"]
    assert "SPY" in client.positions


def test_live_day_trade_guard_blocks_entry(tmp_path, monkeypatch):
    client = FakeAlpaca(equity=500, daytrades=3)
    trader = make_trader(tmp_path, client, monkeypatch, orb_day())
    trader.tick(now=at(9, 50).replace(second=20).astimezone(ZoneInfo("UTC")))
    assert client.submitted == []


def test_live_survives_data_errors(tmp_path, monkeypatch):
    client = FakeAlpaca()
    trader = make_trader(tmp_path, client, monkeypatch)
    import bot.v3.live as live_mod

    def boom(*a):
        raise RuntimeError("subscription does not permit querying recent SIP data")

    monkeypatch.setattr(live_mod, "fetch_alpaca_bars", boom)
    with pytest.raises(RuntimeError):
        trader.tick(now=at(10, 0).astimezone(ZoneInfo("UTC")))
    # run() catches and logs this and keeps looping; the trader state is still usable
    assert trader.position is None


def test_diagnose_runs():
    from bot.v3.diagnose import diagnose, forward_r

    data = synthetic_leveraged(days=20, seed=4)
    result = run_v3_backtest(data, S, starting_equity=500)
    fwd = forward_r(result, data, S)
    assert len(fwd) == len(result.trades)
    text = diagnose(result, data, S)
    assert "After entry, ignoring the bot's exits" in text and "By strategy and exit reason" in text


def test_trend_days_only_skips_rotational_and_early_orb():
    s = Settings(strategy="v3", slippage_bps=0, v3_trend_days_only=True)
    data = synthetic_leveraged(days=60, seed=2)
    result = run_v3_backtest(data, s, starting_equity=100_000)
    for t in result.trades:
        regime = result.regimes[t.entry_time.date()]
        assert regime in (TREND_BULL, TREND_BEAR)
        assert t.strategy != "STRAT_2_VWAP"
        assert t.entry_time.time() >= time(10, 0)
        assert t.symbol == ("TQQQ" if regime == TREND_BULL else "SQQQ")

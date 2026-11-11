"""v3.1 indicators, regime classifier and entry signals.

Everything here works on closed 5-minute bars of one session, so the live bot
and the backtester make exactly the same decisions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta

import pandas as pd

from ..config import Settings
from ..strategy import NY
from . import BEAR_ETF, BULL_ETF, QQQ, ROTATIONAL, TREND_BEAR, TREND_BULL

BAR = timedelta(minutes=5)
SESSION_OPEN = time(9, 30)
ORB_BARS = 3  # 9:30, 9:35, 9:40 -> the 9:30-9:45 opening range
ORB_FIRST_CLOSE = time(9, 50)
ORB_LAST_CLOSE = time(10, 15)
REGIME_TIME = time(10, 0)
NOON = time(12, 0)
EMA_TOUCH = 1.003  # "near EMA21" = within 0.3%


def prepare_session(bars: pd.DataFrame, band_sd: float) -> pd.DataFrame:
    """Add session VWAP, deviation band and EMAs to one session of 5-minute bars."""
    df = bars.copy()
    typical = (df["high"] + df["low"] + df["close"]) / 3
    vol = df["volume"].cumsum()
    df["vwap"] = (typical * df["volume"]).cumsum() / vol.where(vol > 0)
    df["vwap"] = df["vwap"].fillna(df["close"])
    df["std"] = df["close"].expanding().std().fillna(0.0)
    df["lower_band"] = df["vwap"] - band_sd * df["std"]
    df["ema9"] = df["close"].ewm(span=9, adjust=False).mean()
    df["ema21"] = df["close"].ewm(span=21, adjust=False).mean()
    return df


def bar_end_et(ts: pd.Timestamp) -> datetime:
    return (ts + BAR).tz_convert(NY).to_pydatetime()


def classify_regime(qqq: pd.DataFrame, s: Settings) -> tuple[str, float, float]:
    """Classify the session from QQQ bars so far. Returns (regime, % from open, VWAP slope %/h)."""
    open_p = float(qqq["open"].iloc[0])
    close_p = float(qqq["close"].iloc[-1])
    pct_open = (close_p - open_p) / open_p * 100
    end = bar_end_et(qqq.index[-1])
    session_start = end.replace(hour=SESSION_OPEN.hour, minute=SESSION_OPEN.minute, second=0, microsecond=0)
    hours = max((end - session_start).total_seconds() / 3600, 1 / 12)
    vwap_start, vwap_now = float(qqq["vwap"].iloc[0]), float(qqq["vwap"].iloc[-1])
    slope = (vwap_now - vwap_start) / vwap_start * 100 / hours

    if abs(pct_open) >= s.v3_trend_pct and abs(slope) >= s.v3_trend_slope:
        if pct_open > 0 and slope > 0:
            return TREND_BULL, pct_open, slope
        if pct_open < 0 and slope < 0:
            return TREND_BEAR, pct_open, slope
    return ROTATIONAL, pct_open, slope


@dataclass
class Signal:
    strategy: str
    symbol: str
    price: float  # close of the signal bar
    target: float | None = None  # optional full-exit price (VWAP reversion)
    bar: pd.Timestamp | None = None


class SessionScanner:
    """Tracks one session's regime and ORB state and produces entry signals."""

    def __init__(self, settings: Settings):
        self.s = settings
        self.regime: str | None = None
        self.regime_info: tuple[float, float] | None = None
        self.orb_done = False

    def on_bar_close(self, ts: pd.Timestamp, frames: dict[str, pd.DataFrame]) -> Signal | None:
        """Update state with the bar starting at `ts` and return a signal, if any.

        `frames` hold prepared session bars; only rows up to `ts` are used.
        """
        qqq = frames[QQQ].loc[:ts]
        if qqq.empty:
            return None
        end_t = bar_end_et(ts).time()

        if self.regime is None and end_t >= REGIME_TIME and len(qqq) >= 2:
            self.regime, pct, slope = classify_regime(qqq, self.s)
            self.regime_info = (pct, slope)

        signal = None
        if not self.s.v3_trend_days_only:
            signal = self._orb(ts, end_t, qqq, frames)
        elif self.regime in (TREND_BULL, TREND_BEAR):
            # Trend-days-only: ORB waits for the 10:00 regime and must agree with it.
            signal = self._orb(ts, end_t, qqq, frames)
            expected = BULL_ETF if self.regime == TREND_BULL else BEAR_ETF
            if signal is not None and signal.symbol != expected:
                signal = None
        if signal is None and self.regime is not None and end_t >= REGIME_TIME:
            if self.regime == ROTATIONAL:
                if not self.s.v3_trend_days_only:
                    signal = self._vwap_reversion(ts, frames)
            else:
                signal = self._ema_pullback(ts, end_t, qqq, frames)
        if signal is not None:
            signal.bar = ts
        return signal

    # ---- Strategy 1 -------------------------------------------------------

    def _orb(self, ts, end_t, qqq, frames) -> Signal | None:
        if self.orb_done or not (ORB_FIRST_CLOSE <= end_t <= ORB_LAST_CLOSE) or len(qqq) < ORB_BARS:
            return None
        pct = (qqq["close"].iloc[-1] - qqq["open"].iloc[0]) / qqq["open"].iloc[0] * 100
        if pct >= self.s.v3_orb_qqq_pct:
            target = BULL_ETF
        elif pct <= -self.s.v3_orb_qqq_pct:
            target = BEAR_ETF
        else:
            return None
        etf = frames[target].loc[:ts]
        if len(etf) < ORB_BARS or etf.index[-1] != ts:
            return None
        orb_high = float(etf["high"].iloc[:ORB_BARS].max())
        close = float(etf["close"].iloc[-1])
        if close > orb_high and close > float(etf["vwap"].iloc[-1]):
            self.orb_done = True
            return Signal("STRAT_1_ORB", target, close)
        return None

    # ---- Strategy 2 -------------------------------------------------------

    def _vwap_reversion(self, ts, frames) -> Signal | None:
        for symbol in (BULL_ETF, BEAR_ETF):
            etf = frames[symbol].loc[:ts]
            if len(etf) < 6 or etf.index[-1] != ts:
                continue
            row = etf.iloc[-1]
            gap_pct = (row.vwap - row.close) / row.close * 100
            if row.lower_band > 0 and row.close <= row.lower_band and gap_pct >= self.s.v3_vwap_gap_pct:
                target = float(row.vwap) if self.s.v3_vwap_target else None
                return Signal("STRAT_2_VWAP", symbol, float(row.close), target)
        return None

    # ---- Strategy 3 -------------------------------------------------------

    def _ema_pullback(self, ts, end_t, qqq, frames) -> Signal | None:
        if end_t >= NOON:
            move = abs(qqq["close"].iloc[-1] - qqq["open"].iloc[0]) / qqq["open"].iloc[0] * 100
            if move > self.s.v3_exhaustion_pct:
                return None
        symbol = BULL_ETF if self.regime == TREND_BULL else BEAR_ETF
        etf = frames[symbol].loc[:ts]
        if len(etf) < 21 or etf.index[-1] != ts:
            return None
        last, prev = etf.iloc[-1], etf.iloc[-2]
        touched = last.low <= last.ema21 * EMA_TOUCH or prev.low <= prev.ema21 * EMA_TOUCH
        if last.ema9 > last.ema21 and touched and last.close > last.ema9:
            return Signal("STRAT_3_EMA", symbol, float(last.close))
        return None

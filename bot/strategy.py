"""EMA crossover strategy with a VWAP filter.

Rules (long only):
- Entry: the fast EMA crosses above the slow EMA on a closed bar, the close is
  above the session VWAP, and the bar falls inside the entry window.
- Exit: the fast EMA crosses back below the slow EMA. The stop loss, take
  profit and end-of-day flatten are handled by the backtester / live trader.

All signals are computed from closed bars only, so the backtester fills them on
the *next* bar's open to avoid look-ahead bias.
"""

from __future__ import annotations

import pandas as pd

from .config import Settings

NY = "America/New_York"

ENTRY = 1
EXIT = -1
HOLD = 0


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def atr(bars: pd.DataFrame, period: int) -> pd.Series:
    prev_close = bars["close"].shift(1)
    true_range = pd.concat(
        [
            bars["high"] - bars["low"],
            (bars["high"] - prev_close).abs(),
            (bars["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return true_range.rolling(period, min_periods=period).mean()


def session_vwap(bars: pd.DataFrame) -> pd.Series:
    """VWAP that resets at the start of each trading day (US/Eastern)."""
    session = bars.index.tz_convert(NY).date
    typical = (bars["high"] + bars["low"] + bars["close"]) / 3
    pv = (typical * bars["volume"]).groupby(session).cumsum()
    vol = bars["volume"].groupby(session).cumsum()
    return pv / vol.where(vol > 0)


class EmaCrossVwap:
    name = "ema_cross_vwap"

    def __init__(self, settings: Settings):
        if settings.ema_fast >= settings.ema_slow:
            raise ValueError("EMA_FAST must be smaller than EMA_SLOW")
        self.s = settings

    @property
    def warmup_bars(self) -> int:
        return max(self.s.ema_slow, self.s.atr_period) + 1

    def prepare(self, bars: pd.DataFrame) -> pd.DataFrame:
        """Return a copy of `bars` with indicator columns and a `signal` column.

        `bars` must have a tz-aware DatetimeIndex (bar start time) and columns
        open, high, low, close, volume.
        """
        df = bars.copy()
        df["ema_fast"] = ema(df["close"], self.s.ema_fast)
        df["ema_slow"] = ema(df["close"], self.s.ema_slow)
        df["atr"] = atr(df, self.s.atr_period)
        df["vwap"] = session_vwap(df)

        above = df["ema_fast"] > df["ema_slow"]
        prev_above = above.shift(1, fill_value=False).astype(bool)
        cross_up = above & ~prev_above
        cross_down = ~above & prev_above

        local_time = df.index.tz_convert(NY).time
        in_window = pd.Series(
            [self.s.entry_start <= t < self.s.entry_end for t in local_time], index=df.index
        )
        warmed_up = pd.Series(range(len(df)), index=df.index) >= self.warmup_bars
        has_atr = df["atr"].notna() & (df["atr"] > 0)

        entry = cross_up & (df["close"] > df["vwap"]) & in_window & warmed_up & has_atr
        df["signal"] = HOLD
        df.loc[cross_down, "signal"] = EXIT
        df.loc[entry, "signal"] = ENTRY
        return df

"""Market data: Alpaca historical bars, CSV files, and synthetic bars for testing."""

from __future__ import annotations

from datetime import datetime, time, timedelta

import numpy as np
import pandas as pd

from .config import Settings
from .strategy import NY

COLUMNS = ["open", "high", "low", "close", "volume"]
MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)


def regular_hours(bars: pd.DataFrame) -> pd.DataFrame:
    """Keep only bars that start within regular trading hours (9:30-16:00 ET)."""
    local = bars.index.tz_convert(NY).time
    mask = [(MARKET_OPEN <= t < MARKET_CLOSE) for t in local]
    return bars[mask]


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    df = df.rename(columns=str.lower)
    missing = [c for c in COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"Bars are missing columns: {missing}")
    df = df[COLUMNS].astype(float)
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    return df.sort_index()


def fetch_alpaca_bars(
    settings: Settings,
    symbol: str,
    start: datetime,
    end: datetime | None = None,
) -> pd.DataFrame:
    """Download intraday bars from Alpaca (free IEX feed), regular hours only."""
    from alpaca.data.enums import DataFeed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

    settings.require_credentials()
    client = StockHistoricalDataClient(settings.api_key, settings.api_secret)
    request = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame(settings.timeframe_minutes, TimeFrameUnit.Minute),
        start=start,
        end=end,
        feed=DataFeed.IEX,
    )
    df = client.get_stock_bars(request).df
    if df.empty:
        return pd.DataFrame(columns=COLUMNS, index=pd.DatetimeIndex([], tz="UTC"))
    if isinstance(df.index, pd.MultiIndex):
        df = df.xs(symbol, level="symbol")
    return regular_hours(_normalize(df))


def fetch_history(settings: Settings, sessions: int) -> dict[str, pd.DataFrame]:
    """Download roughly the last `sessions` trading days of bars for every symbol."""
    from datetime import timezone

    start = datetime.now(timezone.utc) - timedelta(days=int(sessions * 1.5) + 7)
    return {s: fetch_alpaca_bars(settings, s, start) for s in data_symbols(settings)}


def data_symbols(settings: Settings) -> list[str]:
    """Symbols whose bars the active strategy needs."""
    if settings.is_v3:
        from .v3 import DATA_SYMBOLS

        return list(DATA_SYMBOLS)
    return settings.symbols


def load_csv(path: str) -> pd.DataFrame:
    """Load bars from a CSV with a timestamp column plus open/high/low/close/volume.

    Timestamps without a timezone are assumed to be UTC.
    """
    df = pd.read_csv(path)
    ts_col = next((c for c in df.columns if c.lower() in ("timestamp", "time", "datetime", "date")), None)
    if ts_col is None:
        raise ValueError("CSV needs a 'timestamp' column")
    df.index = pd.to_datetime(df.pop(ts_col), utc=True)
    return regular_hours(_normalize(df))


def synthetic_bars(
    days: int = 20,
    timeframe_minutes: int = 5,
    start_price: float = 100.0,
    seed: int = 42,
    start_date: str = "2025-01-06",
    daily_drift_sd: float = 0.0004,
) -> pd.DataFrame:
    """Random-walk intraday bars, useful for trying the bot without API keys.

    `daily_drift_sd` gives each day a small persistent drift, which makes
    trends continue within a day; set it to 0 for a pure random walk.

    Synthetic data has no real edge, so results say nothing about live performance.
    """
    rng = np.random.default_rng(seed)
    bars_per_day = int((6.5 * 60) // timeframe_minutes)
    rows, index = [], []
    price = start_price
    day = pd.Timestamp(start_date)
    produced = 0
    while produced < days:
        if day.weekday() < 5:
            session_start = pd.Timestamp.combine(day.date(), MARKET_OPEN).tz_localize(NY)
            drift = rng.normal(0, daily_drift_sd) if daily_drift_sd else 0.0
            for k in range(bars_per_day):
                ts = session_start + timedelta(minutes=k * timeframe_minutes)
                ret = drift + rng.normal(0, 0.0015)
                o = price
                c = max(0.01, o * (1 + ret))
                h = max(o, c) * (1 + abs(rng.normal(0, 0.0007)))
                l = min(o, c) * (1 - abs(rng.normal(0, 0.0007)))
                v = float(rng.integers(5_000, 50_000))
                rows.append((o, h, l, c, v))
                index.append(ts.tz_convert("UTC"))
                price = c
            produced += 1
        day += timedelta(days=1)
    return pd.DataFrame(rows, columns=COLUMNS, index=pd.DatetimeIndex(index, name="timestamp"))


def synthetic_leveraged(days: int = 60, seed: int = 42, start_date: str = "2025-01-06") -> dict[str, pd.DataFrame]:
    """Random-walk QQQ plus TQQQ (3x) and SQQQ (-3x) built bar by bar from it.

    For trying the v3 system without API keys. Like all synthetic data it has
    no real edge, so results say nothing about live performance.
    """
    # No daily drift: drift makes intraday trends persist, which hands breakout
    # strategies a fake edge (ORB averaged +2R on drifting data, about 0R without).
    qqq = synthetic_bars(days=days, timeframe_minutes=5, start_price=400.0, seed=seed,
                         start_date=start_date, daily_drift_sd=0.0)
    rel = qqq.div(qqq["open"], axis=0)

    def lever(factor: float, start: float) -> pd.DataFrame:
        rows, price = [], start
        for r in rel.itertuples():
            o = price
            c = o * (1 + factor * (r.close - 1))
            if factor > 0:
                h, l = o * (1 + factor * (r.high - 1)), o * (1 + factor * (r.low - 1))
            else:
                h, l = o * (1 + factor * (r.low - 1)), o * (1 + factor * (r.high - 1))
            rows.append((o, max(h, o, c), min(l, o, c), c, r.volume))
            price = c
        return pd.DataFrame(rows, columns=COLUMNS, index=qqq.index)

    return {"QQQ": qqq, "TQQQ": lever(3.0, 80.0), "SQQQ": lever(-3.0, 20.0)}

"""Multi-day swing strategy: buy short dips in an uptrend (Connors RSI(2) rules).

Rules (standard published values, fixed before testing; nothing is tuned):
- Universe: SPY, QQQ, IWM, DIA. Up to one position per fund, each sized at a
  quarter of the account (fractional shares).
- Buy at the next open when the fund closed above its 200-day simple moving
  average (long-term uptrend) and its 2-day RSI closed below 10 (sharp dip).
- Sell at the next open when it closes above its 5-day simple moving average,
  or after 10 trading days.
- Every fill pays SLIPPAGE_BPS. Positions are held overnight, so trades never
  count as day trades.

The test also measures timing skill: each trade is compared with buying the same
fund on an average day of the test period and holding it for the same number of
days ("random-day baseline"). In a rising market every long strategy makes money;
beating the baseline is what shows the entries add something.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd

from .config import Settings
from .strategy import NY

UNIVERSE = ("SPY", "QQQ", "IWM", "DIA")
RSI_PERIOD = 2
RSI_BUY = 10.0
TREND_SMA = 200
EXIT_SMA = 5
MAX_HOLD_DAYS = 10


def rsi(close: pd.Series, period: int) -> pd.Series:
    """Wilder's RSI."""
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(100.0)


def prepare(bars: pd.DataFrame) -> pd.DataFrame:
    df = bars.copy()
    df["sma_trend"] = df["close"].rolling(TREND_SMA, min_periods=TREND_SMA).mean()
    df["sma_exit"] = df["close"].rolling(EXIT_SMA, min_periods=EXIT_SMA).mean()
    df["rsi"] = rsi(df["close"], RSI_PERIOD)
    return df


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


def fetch_daily(settings: Settings, symbol: str, start: date, end: date) -> pd.DataFrame:
    """Split- and dividend-adjusted daily bars from Alpaca (free IEX feed), indexed by date."""
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    settings.require_credentials()
    client = StockHistoricalDataClient(settings.api_key, settings.api_secret)
    request = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Day,
        start=datetime.combine(start, datetime.min.time(), timezone.utc),
        end=datetime.combine(end + timedelta(days=1), datetime.min.time(), timezone.utc),
        feed=DataFeed.IEX,
        adjustment=Adjustment.ALL,
    )
    df = client.get_stock_bars(request).df
    if df.empty:
        return _empty()
    if isinstance(df.index, pd.MultiIndex):
        df = df.xs(symbol, level="symbol")
    df.index = pd.Index(df.index.tz_convert(NY).date, name="date")
    return df[["open", "high", "low", "close", "volume"]].astype(float).sort_index()


def fetch_daily_yahoo(_settings: Settings, symbol: str, start: date, end: date) -> pd.DataFrame:
    """Split- and dividend-adjusted daily bars from Yahoo Finance (free, long history)."""
    try:
        import yfinance as yf
    except ImportError as exc:
        raise SystemExit("Yahoo data needs the yfinance package. Install it with:  pip install yfinance") from exc
    df = yf.Ticker(symbol).history(start=str(start), end=str(end + timedelta(days=1)), auto_adjust=True)
    if df is None or df.empty:
        return _empty()
    df = df.rename(columns=str.lower)
    idx = df.index.tz_convert(NY) if df.index.tz is not None else df.index
    df.index = pd.Index([d.date() for d in idx], name="date")
    return df[["open", "high", "low", "close", "volume"]].astype(float).sort_index()


def _empty() -> pd.DataFrame:
    return pd.DataFrame(columns=["open", "high", "low", "close", "volume"], index=pd.Index([], name="date"), dtype=float)


def check_coverage(data: dict[str, pd.DataFrame], start: date) -> list[str]:
    """Problems that would make a test meaningless (missing data or too little history)."""
    problems = []
    for sym, df in data.items():
        if df.empty:
            problems.append(f"{sym}: no data returned for this period")
            continue
        before = int((df.index < start).sum())
        if before < TREND_SMA:
            problems.append(
                f"{sym}: data starts {df.index[0]}, only {before} trading days before {start} "
                f"(the {TREND_SMA}-day average needs {TREND_SMA})"
            )
    return problems


def synthetic_daily(
    years: float = 3, seed: int = 1, start: str = "2021-01-04", symbols: tuple = UNIVERSE
) -> dict[str, pd.DataFrame]:
    """Random-walk daily bars (slight upward drift, no dip-buying edge) for offline tests."""
    rng = np.random.default_rng(seed)
    days = pd.bdate_range(start, periods=int(252 * years))
    out = {}
    for k, sym in enumerate(symbols):
        rets = rng.normal(0.0004, 0.011, len(days))
        close = 100 * (1 + k * 0.5) * np.cumprod(1 + rets)
        open_ = np.concatenate([[close[0]], close[:-1]]) * (1 + rng.normal(0, 0.002, len(days)))
        high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.004, len(days))))
        low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.004, len(days))))
        out[sym] = pd.DataFrame(
            {"open": open_, "high": high, "low": low, "close": close, "volume": 1e6},
            index=pd.Index([d.date() for d in days], name="date"),
        )
    return out


# ---------------------------------------------------------------------------
# Backtest
# ---------------------------------------------------------------------------


@dataclass
class SwingTrade:
    symbol: str
    entry_date: date
    exit_date: date
    entry_price: float
    exit_price: float
    qty: float
    days: int  # trading days from entry open to exit open
    exit_reason: str

    @property
    def ret(self) -> float:
        return self.exit_price / self.entry_price - 1

    @property
    def pnl(self) -> float:
        return (self.exit_price - self.entry_price) * self.qty


@dataclass
class SwingResult:
    start: date
    end: date
    starting_equity: float
    trades: list[SwingTrade]
    equity: pd.Series
    exposure: float  # average fraction of the account invested
    buy_hold_return: float  # equal-weight basket, first open -> last close
    excess: list[float] = field(default_factory=list)  # per-trade return minus random-day baseline


def run_swing_backtest(
    bars_by_symbol: dict[str, pd.DataFrame], start: date, end: date, starting_equity: float, slippage_bps: float
) -> SwingResult:
    slip = slippage_bps / 10_000
    frames = {s: prepare(b) for s, b in bars_by_symbol.items()}
    symbols = list(frames)
    dates = sorted({d for f in frames.values() for d in f.index if start <= d <= end})

    cash = starting_equity
    held: dict[str, dict] = {}
    pending_buy: set[str] = set()
    pending_sell: dict[str, str] = {}
    trades: list[SwingTrade] = []
    curve: dict[date, float] = {}
    invested_frac: list[float] = []
    last_equity = starting_equity

    for i, d in enumerate(dates):
        # Opens: exits first, then entries.
        for sym, reason in list(pending_sell.items()):
            f = frames[sym]
            if d not in f.index:
                continue
            pos = held.pop(sym)
            fill = float(f.at[d, "open"]) * (1 - slip)
            cash += pos["qty"] * fill
            trades.append(SwingTrade(sym, pos["date"], d, pos["price"], fill, pos["qty"], pos["days"], reason))
            del pending_sell[sym]
        for sym in list(pending_buy):
            f = frames[sym]
            if d not in f.index:
                continue
            pending_buy.discard(sym)
            price = float(f.at[d, "open"]) * (1 + slip)
            budget = min(cash, last_equity / len(symbols))
            qty = math.floor(budget / price * 10_000) / 10_000
            if qty * price >= 1.0:
                cash -= qty * price
                held[sym] = {"qty": qty, "price": price, "date": d, "days": 0}

        # Closes: mark to market and look for signals for tomorrow's open.
        value = 0.0
        final_day = i == len(dates) - 1
        for sym in symbols:
            f = frames[sym]
            if d not in f.index:
                if sym in held:
                    value += held[sym]["qty"] * held[sym]["price"]
                continue
            row = f.loc[d]
            if sym in held:
                pos = held[sym]
                value += pos["qty"] * float(row.close)
                pos["days"] += 1
                if not final_day:
                    if row.close > row.sma_exit:
                        pending_sell[sym] = "RECOVERED"
                    elif pos["days"] >= MAX_HOLD_DAYS:
                        pending_sell[sym] = "TIME_LIMIT"
            elif not final_day and not np.isnan(row.sma_trend):
                if row.close > row.sma_trend and row.rsi < RSI_BUY:
                    pending_buy.add(sym)
        last_equity = cash + value
        curve[d] = last_equity
        invested_frac.append(value / last_equity if last_equity else 0.0)

    # Close anything still open at the last close of the test period.
    if dates:
        last = dates[-1]
        for sym, pos in list(held.items()):
            f = frames[sym]
            px = float(f.loc[:last, "close"].iloc[-1]) * (1 - slip)
            cash += pos["qty"] * px
            trades.append(SwingTrade(sym, pos["date"], last, pos["price"], px, pos["qty"], pos["days"], "END_OF_TEST"))
            curve[last] = cash

    bh = []
    for sym, f in frames.items():
        window = f[(f.index >= start) & (f.index <= end)]
        if len(window):
            bh.append(window["close"].iloc[-1] / window["open"].iloc[0] - 1)

    result = SwingResult(
        start, end, starting_equity, trades, pd.Series(curve, dtype=float),
        float(np.mean(invested_frac)) if invested_frac else 0.0, float(np.mean(bh)) if bh else 0.0,
    )
    result.excess = random_day_excess(result, frames, dates, slip)
    return result


def random_day_excess(result: SwingResult, frames, dates, slip) -> list[float]:
    """Each trade's return minus buying the same fund at the open of an average test-period
    day and selling at the open the same number of days later (same costs)."""
    out = []
    cache: dict[tuple, float] = {}
    for t in result.trades:
        key = (t.symbol, t.days)
        if key not in cache:
            opens = frames[t.symbol]["open"]
            opens = opens[(opens.index >= result.start) & (opens.index <= result.end)]
            h = max(1, t.days)
            if len(opens) > h:
                rets = opens.values[h:] / opens.values[:-h] * (1 - slip) / (1 + slip) - 1
                cache[key] = float(np.mean(rets))
            else:
                cache[key] = 0.0
        out.append(t.ret - cache[key])
    return out


def format_swing_report(r: SwingResult) -> str:
    trades = r.trades
    final = float(r.equity.iloc[-1]) if len(r.equity) else r.starting_equity
    peak = r.equity.cummax()
    dd = float((r.equity / peak - 1).min()) * 100 if len(r.equity) else 0.0
    rets = pd.Series([t.ret for t in trades], dtype=float)
    ex = pd.Series(r.excess, dtype=float)
    se = float(ex.std(ddof=1) / math.sqrt(len(ex))) if len(ex) > 1 else float("nan")
    lines = [
        f"Swing test: buy dips in an uptrend (RSI(2) < {RSI_BUY:g}, above {TREND_SMA}-day average) on {', '.join(UNIVERSE)}",
        f"  Period:              {r.start} -> {r.end}",
        f"  Starting equity:     ${r.starting_equity:,.2f}",
        f"  Final equity:        ${final:,.2f} ({(final / r.starting_equity - 1) * 100:+.2f}%)",
        f"  Max drawdown:        {dd:.2f}%",
        f"  Trades:              {len(trades)}",
        f"  Win rate:            {(rets > 0).mean() * 100:.1f}%" if len(rets) else "  Win rate:            -",
        f"  Avg trade:           {rets.mean() * 100:+.2f}%  over {np.mean([t.days for t in trades]):.1f} days" if len(rets) else "  Avg trade:           -",
        f"  Avg vs random day:   {ex.mean() * 100:+.2f}% per trade  (uncertainty +/- {2 * se * 100:.2f}%)" if len(ex) else "  Avg vs random day:   -",
        f"  Time invested:       {r.exposure * 100:.0f}% of the account on average",
        f"  Buy & hold basket:   {r.buy_hold_return * 100:+.2f}% (equal weight, fully invested all period)",
    ]
    for sym in UNIVERSE:
        g = [t for t in trades if t.symbol == sym]
        if g:
            lines.append(f"    {sym:<4} {len(g):>3} trades  avg {np.mean([t.ret for t in g]) * 100:+.2f}%  P&L ${sum(t.pnl for t in g):+.2f}")
    return "\n".join(lines)


def verdict(r: SwingResult) -> tuple[bool, str]:
    final = float(r.equity.iloc[-1]) if len(r.equity) else r.starting_equity
    total = final / r.starting_equity - 1
    ex = pd.Series(r.excess, dtype=float)
    avg_ex = float(ex.mean()) if len(ex) else 0.0
    passed = total > 0 and avg_ex > 0 and len(ex) > 0
    msg = f"RESULT: {'PASS' if passed else 'FAIL'} (total {total * 100:+.2f}%, avg vs random day {avg_ex * 100:+.2f}% per trade, {len(ex)} trades)"
    if passed and len(ex) > 1:
        se = float(ex.std(ddof=1) / math.sqrt(len(ex)))
        if avg_ex - 2 * se <= 0:
            msg += "\nNote: the uncertainty range still includes 0, so this is weak evidence, not proof."
    return passed, msg

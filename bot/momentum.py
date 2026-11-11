"""Monthly ETF momentum with a trend filter (Faber's "relative strength" rules).

Rules (standard published values, fixed before testing; nothing is tuned):
- Universe: 10 ETFs across asset classes (see UNIVERSE).
- At the last close of each month, score every ETF by the average of its
  3-, 6- and 12-month returns (63 / 126 / 252 trading days).
- Hold the top 3, a third of the account each.
- Trend filter: a selected ETF whose close is below its 10-month average
  (of month-end closes) is not bought; that third stays in cash.
- Rebalance at the next trading day's open; every traded dollar pays SLIPPAGE_BPS.

The comparison is holding all 10 ETFs in equal amounts (rebalanced monthly):
momentum aims for better return per unit of risk and smaller crashes, not to
beat the market every year.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd

UNIVERSE = ("SPY", "QQQ", "IWM", "EFA", "EEM", "TLT", "IEF", "GLD", "DBC", "VNQ")
TOP_N = 3
LOOKBACKS = (63, 126, 252)  # ~3, 6 and 12 months of trading days
TREND_MONTHS = 10


@dataclass
class MomentumResult:
    start: date
    end: date
    starting_equity: float
    equity: pd.Series  # daily strategy equity
    benchmark: pd.Series  # daily equal-weight equity, same start value
    spy: pd.Series  # daily SPY buy-and-hold equity, same start value
    holdings: list[tuple[date, dict]] = field(default_factory=list)  # (rebalance date, {symbol: weight})
    turnover: float = 0.0  # total dollars traded / average equity
    cash_fraction: float = 0.0


def _closes(data: dict[str, pd.DataFrame], calendar: list[date], col: str) -> pd.DataFrame:
    return pd.DataFrame({s: df[col].reindex(calendar) for s, df in data.items()}).ffill()


def month_ends(calendar: list[date]) -> list[date]:
    s = pd.Series(calendar, index=pd.to_datetime(calendar))
    return list(s.groupby([s.index.year, s.index.month]).last())


def target_weights(closes: pd.DataFrame, monthly: pd.DataFrame, i: int, m: date) -> dict[str, float]:
    """Weights decided at the close of month-end `m` (row `i` of `closes`)."""
    scores = {}
    for sym in closes.columns:
        col = closes[sym]
        if i < max(LOOKBACKS) or pd.isna(col.iloc[i]) or any(pd.isna(col.iloc[i - lb]) for lb in LOOKBACKS):
            continue
        mcol = monthly[sym].loc[:m].dropna()
        if len(mcol) < TREND_MONTHS:
            continue
        rets = [col.iloc[i] / col.iloc[i - lb] - 1 for lb in LOOKBACKS]
        scores[sym] = (float(np.mean(rets)), bool(col.iloc[i] > mcol.iloc[-TREND_MONTHS:].mean()))
    top = sorted(scores, key=lambda s: scores[s][0], reverse=True)[:TOP_N]
    return {s: 1 / TOP_N for s in top if scores[s][1]}


def run_momentum_backtest(
    data: dict[str, pd.DataFrame], start: date, end: date, starting_equity: float, slippage_bps: float
) -> MomentumResult:
    slip = slippage_bps / 10_000
    calendar = sorted({d for df in data.values() for d in df.index if d <= end})
    closes = _closes(data, calendar, "close")
    opens = _closes(data, calendar, "open").fillna(closes)
    ends = month_ends(calendar)
    monthly = closes.loc[ends]
    pos = {d: i for i, d in enumerate(calendar)}
    test_days = [d for d in calendar if d >= start]
    if not test_days:
        raise ValueError("No data in the test period")

    # Decisions: the month-end just before the test starts, then every month-end inside it.
    decision_days = [m for m in ends if m < test_days[0]][-1:] + [m for m in ends if start <= m < test_days[-1]]
    pending: dict[date, dict] = {}
    for m in decision_days:
        nxt = pos[m] + 1
        if nxt < len(calendar) and calendar[nxt] >= start:
            pending[calendar[nxt]] = target_weights(closes, monthly, pos[m], m)

    cash, shares = starting_equity, {}
    curve, holdings, traded, cash_frac = {}, [], 0.0, []
    for d in test_days:
        if d in pending:
            weights = pending[d]
            value = cash + sum(q * opens.at[d, s] for s, q in shares.items())
            new = {}
            for s in set(shares) | set(weights):
                px = opens.at[d, s]
                have = shares.get(s, 0.0) * px
                want = value * weights.get(s, 0.0)
                delta = want - have
                if abs(delta) < 1e-9:
                    if want > 0:
                        new[s] = shares[s]
                    continue
                traded += abs(delta)
                cash -= delta + abs(delta) * slip
                if want > 0:
                    new[s] = want / px
            shares = new
            holdings.append((d, dict(weights)))
        value = cash + sum(q * closes.at[d, s] for s, q in shares.items())
        curve[d] = value
        cash_frac.append(max(0.0, cash) / value if value else 0.0)

    equity = pd.Series(curve, dtype=float)

    # Benchmark: equal weight in every ETF with data, rebalanced on the same days (no costs).
    bench_val, bench_shares, bench_curve = starting_equity, {}, {}
    for d in test_days:
        if d in pending or not bench_shares:
            avail = [s for s in closes.columns if not pd.isna(opens.at[d, s])]
            bench_val = sum(q * opens.at[d, s] for s, q in bench_shares.items()) if bench_shares else bench_val
            bench_shares = {s: bench_val / len(avail) / opens.at[d, s] for s in avail}
        bench_curve[d] = sum(q * closes.at[d, s] for s, q in bench_shares.items())
    benchmark = pd.Series(bench_curve, dtype=float)

    spy_px = closes["SPY"].loc[test_days]
    spy = starting_equity * spy_px / opens.at[test_days[0], "SPY"]

    return MomentumResult(
        start, end, starting_equity, equity, benchmark, spy, holdings,
        traded / float(equity.mean()) if len(equity) else 0.0, float(np.mean(cash_frac)) if cash_frac else 0.0,
    )


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def stats(curve: pd.Series) -> dict:
    curve = curve.dropna()
    years = (pd.Timestamp(curve.index[-1]) - pd.Timestamp(curve.index[0])).days / 365.25
    total = curve.iloc[-1] / curve.iloc[0] - 1
    monthly = curve.groupby([pd.Timestamp(d).to_period("M") for d in curve.index]).last().pct_change().dropna()
    vol = float(monthly.std(ddof=1) * math.sqrt(12)) if len(monthly) > 1 else float("nan")
    sharpe = float(monthly.mean() / monthly.std(ddof=1) * math.sqrt(12)) if len(monthly) > 1 and monthly.std() > 0 else 0.0
    yearly = curve.groupby([pd.Timestamp(d).year for d in curve.index]).last()
    first = curve.iloc[0]
    yr = (yearly / yearly.shift(1).fillna(first) - 1)
    return {
        "total": total,
        "cagr": (1 + total) ** (1 / years) - 1 if years > 0 else float("nan"),
        "vol": vol,
        "sharpe": sharpe,
        "max_dd": float((curve / curve.cummax() - 1).min()),
        "yearly": yr,
    }


def format_momentum_report(r: MomentumResult) -> str:
    s, b, p = stats(r.equity), stats(r.benchmark), stats(r.spy)
    lines = [
        f"ETF momentum: top {TOP_N} of {len(UNIVERSE)} by 3/6/12-month return, cash if below 10-month average",
        f"  Period: {r.equity.index[0]} -> {r.equity.index[-1]}   starting equity ${r.starting_equity:,.2f}",
        "",
        f"  {'':<24}{'Momentum':>12}{'Equal-weight 10':>17}{'SPY only':>11}",
        f"  {'Final equity':<24}{r.equity.iloc[-1]:>12,.2f}{r.benchmark.iloc[-1]:>17,.2f}{r.spy.iloc[-1]:>11,.2f}",
        f"  {'Total return':<24}{s['total'] * 100:>11.1f}%{b['total'] * 100:>16.1f}%{p['total'] * 100:>10.1f}%",
        f"  {'Return per year':<24}{s['cagr'] * 100:>11.1f}%{b['cagr'] * 100:>16.1f}%{p['cagr'] * 100:>10.1f}%",
        f"  {'Volatility per year':<24}{s['vol'] * 100:>11.1f}%{b['vol'] * 100:>16.1f}%{p['vol'] * 100:>10.1f}%",
        f"  {'Sharpe ratio':<24}{s['sharpe']:>12.2f}{b['sharpe']:>17.2f}{p['sharpe']:>11.2f}",
        f"  {'Max drawdown':<24}{s['max_dd'] * 100:>11.1f}%{b['max_dd'] * 100:>16.1f}%{p['max_dd'] * 100:>10.1f}%",
        f"  Time in cash: {r.cash_fraction * 100:.0f}% of the account on average; "
        f"traded {r.turnover:.1f}x the account over the period",
        "",
        f"  {'Year':<8}{'Momentum':>10}{'Equal-wt':>10}{'SPY':>9}   Held at year end",
    ]
    last_hold = {}
    for d, w in r.holdings:
        last_hold[pd.Timestamp(d).year] = w
    held = {}
    for y in s["yearly"].index:
        if y in last_hold:
            held = last_hold[y]
        names = ", ".join(sorted(held)) if held else "cash"
        lines.append(
            f"  {y:<8}{s['yearly'][y] * 100:>9.1f}%{b['yearly'][y] * 100:>9.1f}%{p['yearly'][y] * 100:>8.1f}%   {names}"
        )
    return "\n".join(lines)


def momentum_verdict(r: MomentumResult) -> tuple[bool, str]:
    s, b = stats(r.equity), stats(r.benchmark)
    better_sharpe = s["sharpe"] > b["sharpe"]
    smaller_dd = s["max_dd"] > b["max_dd"]  # drawdowns are negative
    passed = better_sharpe and smaller_dd
    return passed, (
        f"RESULT: {'PASS' if passed else 'FAIL'} "
        f"(Sharpe {s['sharpe']:.2f} vs {b['sharpe']:.2f} {'better' if better_sharpe else 'NOT better'}; "
        f"max drawdown {s['max_dd'] * 100:.1f}% vs {b['max_dd'] * 100:.1f}% {'smaller' if smaller_dd else 'NOT smaller'})"
    )

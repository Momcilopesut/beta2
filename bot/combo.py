"""Swing + momentum combined: half the account in each strategy.

Rule (fixed before testing): each calendar year starts with the account split
50/50; the momentum half runs bot/momentum.py and the swing half runs bot/swing.py,
each on its own money, with no transfers until the next year's reset.
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from .momentum import run_momentum_backtest, stats
from .swing import UNIVERSE as SWING_UNIVERSE
from .swing import run_swing_backtest


def run_combo_backtest(
    data: dict[str, pd.DataFrame], start: date, end: date, starting_equity: float, slippage_bps: float,
    momentum_universe: tuple,
) -> dict:
    mom_data = {s: data[s] for s in momentum_universe}
    swing_data = {s: data[s] for s in SWING_UNIVERSE}
    calendar = sorted({d for df in data.values() for d in df.index if start <= d <= end})
    years = sorted({d.year for d in calendar})

    equity = starting_equity
    parts, mom_parts, swing_parts, cash_parts = [], [], [], []
    cash_equity = starting_equity
    swing_trades = 0
    for y in years:
        y_start = max(start, date(y, 1, 1))
        y_end = min(end, date(y, 12, 31))
        half = equity / 2
        mom = run_momentum_backtest(mom_data, y_start, y_end, half, slippage_bps).equity
        sw_res = run_swing_backtest(swing_data, y_start, y_end, half, slippage_bps)
        sw = sw_res.equity.reindex(mom.index).ffill().fillna(half)
        swing_trades += len(sw_res.trades)
        combined = mom + sw
        parts.append(combined)
        mom_parts.append(mom)
        swing_parts.append(sw)
        equity = float(combined.iloc[-1])
        # Same, but the swing half just sits in cash: shows what swing adds over idle money.
        c_half = cash_equity / 2
        mom_c = run_momentum_backtest(mom_data, y_start, y_end, c_half, slippage_bps).equity + c_half
        cash_parts.append(mom_c)
        cash_equity = float(mom_c.iloc[-1])

    combo = pd.concat(parts)
    momentum_alone = run_momentum_backtest(mom_data, start, end, starting_equity, slippage_bps)
    return {
        "combo": combo,
        "mom_cash": pd.concat(cash_parts),
        "momentum": momentum_alone.equity,
        "benchmark": momentum_alone.benchmark,
        "spy": momentum_alone.spy,
        "swing_trades": swing_trades,
        "mom_sleeve": pd.concat(mom_parts),
        "swing_sleeve": pd.concat(swing_parts),
    }


def format_combo_report(r: dict, starting_equity: float) -> str:
    c, h, m, p = (stats(r[k]) for k in ("combo", "mom_cash", "momentum", "spy"))
    mom_ret = r["mom_sleeve"].groupby([pd.Timestamp(d).to_period("M") for d in r["mom_sleeve"].index]).last().pct_change()
    sw_ret = r["swing_sleeve"].groupby([pd.Timestamp(d).to_period("M") for d in r["swing_sleeve"].index]).last().pct_change()
    corr = float(pd.concat([mom_ret, sw_ret], axis=1).dropna().corr().iloc[0, 1])
    lines = [
        "Swing + momentum, 50/50, reset each January",
        f"  Period: {r['combo'].index[0]} -> {r['combo'].index[-1]}   starting equity ${starting_equity:,.2f}",
        "",
        f"  {'':<22}{'Swing+Mom':>10}{'Mom+cash':>10}{'Mom only':>10}{'SPY':>9}",
        f"  {'':<22}{'(50/50)':>10}{'(50/50)':>10}{'(100%)':>10}",
        f"  {'Final equity':<22}{r['combo'].iloc[-1]:>10,.0f}{r['mom_cash'].iloc[-1]:>10,.0f}{r['momentum'].iloc[-1]:>10,.0f}{r['spy'].iloc[-1]:>9,.0f}",
        f"  {'Return per year':<22}{c['cagr'] * 100:>9.1f}%{h['cagr'] * 100:>9.1f}%{m['cagr'] * 100:>9.1f}%{p['cagr'] * 100:>8.1f}%",
        f"  {'Volatility per year':<22}{c['vol'] * 100:>9.1f}%{h['vol'] * 100:>9.1f}%{m['vol'] * 100:>9.1f}%{p['vol'] * 100:>8.1f}%",
        f"  {'Sharpe ratio':<22}{c['sharpe']:>10.2f}{h['sharpe']:>10.2f}{m['sharpe']:>10.2f}{p['sharpe']:>9.2f}",
        f"  {'Max drawdown':<22}{c['max_dd'] * 100:>9.1f}%{h['max_dd'] * 100:>9.1f}%{m['max_dd'] * 100:>9.1f}%{p['max_dd'] * 100:>8.1f}%",
        f"  Swing adds over idle cash: {(c['cagr'] - h['cagr']) * 100:+.2f}% per year",
        f"  Swing trades: {r['swing_trades']}   monthly correlation of the two halves: {corr:+.2f}",
        "",
        f"  {'Year':<8}{'Swing+Mom':>10}{'Mom+cash':>10}{'Mom only':>10}{'SPY':>9}",
    ]
    for y in c["yearly"].index:
        lines.append(
            f"  {y:<8}{c['yearly'][y] * 100:>9.1f}%{h['yearly'][y] * 100:>9.1f}%{m['yearly'][y] * 100:>9.1f}%{p['yearly'][y] * 100:>8.1f}%"
        )
    return "\n".join(lines)


def combo_verdict(r: dict) -> str:
    c, m, h = stats(r["combo"]), stats(r["momentum"]), stats(r["mom_cash"])
    better = c["sharpe"] > m["sharpe"]
    no_worse_dd = c["max_dd"] >= m["max_dd"]
    passed = better and no_worse_dd
    return (
        f"RESULT: {'PASS' if passed else 'FAIL'} (Sharpe {c['sharpe']:.2f} vs momentum alone {m['sharpe']:.2f} "
        f"{'better' if better else 'NOT better'}; max drawdown {c['max_dd'] * 100:.1f}% vs {m['max_dd'] * 100:.1f}% "
        f"{'no worse' if no_worse_dd else 'WORSE'})\n"
        f"Return cost vs momentum alone: {(c['cagr'] - m['cagr']) * 100:+.2f}% per year. "
        f"The drawdown test is easy with half the money mostly in cash; the 'Mom+cash' column\n"
        f"shows what swing adds over simply leaving that half idle."
    )

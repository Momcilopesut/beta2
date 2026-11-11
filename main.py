"""Command-line entry point.

Examples:
    python main.py backtest --synthetic
    python main.py backtest --symbol SPY --start 2025-01-01 --end 2025-06-30
    python main.py backtest --csv data/spy_5min.csv
    python main.py paper
    python main.py learn                      # run one self-improvement step now
    python main.py learn --synthetic --days 20  # simulate 20 days of daily learning offline
    python main.py params                     # show learned params and history
    python main.py params --rollback 3        # restore params version 3 (0 = .env defaults)

v3.1 system (QQQ regime -> TQQQ / SQQQ), or set STRATEGY=v3 in .env:
    python main.py backtest --strategy v3 --start 2026-01-01 --equity 500
    python main.py compare --start 2026-01-01 --equity 500   # v3.1 vs the original, same data
    python main.py paper --strategy v3
    python main.py report                     # per-strategy review of v3 trades
"""

from __future__ import annotations

import argparse
import logging
import json
import os
from dataclasses import asdict, replace
from datetime import datetime, timezone

import pandas as pd

from bot.backtest import format_report, run_backtest, trades_frame
from bot.config import Settings
from bot.data import fetch_alpaca_bars, fetch_history, load_csv, synthetic_bars, synthetic_leveraged
from bot.learning import LearningState, run_daily_learning
from bot.strategy import NY


def _date(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def _with_learned(settings: Settings, base_params: bool) -> Settings:
    if settings.learning_enabled and not base_params:
        state = LearningState(settings.state_path)
        if state.version:
            print(f"Using learned params version {state.version} (pass --base-params to use .env values)\n")
        return state.apply(settings)
    return settings


def _v3_data(args: argparse.Namespace, settings: Settings) -> dict:
    from bot.v3 import DATA_SYMBOLS

    if args.synthetic:
        return synthetic_leveraged(days=args.days)
    if not args.start:
        raise SystemExit("--start is required when downloading from Alpaca (or use --synthetic)")
    end = _date(args.end) if args.end else None
    s5 = replace(settings, timeframe_minutes=5)
    return {sym: fetch_alpaca_bars(s5, sym, _date(args.start), end) for sym in DATA_SYMBOLS}


def cmd_backtest(args: argparse.Namespace, settings: Settings) -> None:
    settings = _with_learned(settings, args.base_params)
    if settings.is_v3:
        from bot.v3.backtest import format_v3_report, run_v3_backtest
        from bot.v3.report import COLUMNS

        result = run_v3_backtest(_v3_data(args, settings), settings, starting_equity=args.equity)
        print(format_v3_report(result))
        if args.trades_out:
            pd.DataFrame([asdict(t) for t in result.trades], columns=COLUMNS).to_csv(args.trades_out, index=False)
            print(f"\n  Trades written to {args.trades_out} (review with: python main.py report --csv {args.trades_out})")
        return
    if args.synthetic:
        datasets = {"SYNTHETIC": synthetic_bars(days=args.days, timeframe_minutes=settings.timeframe_minutes)}
    elif args.csv:
        datasets = {args.symbol or os.path.splitext(os.path.basename(args.csv))[0].upper(): load_csv(args.csv)}
    else:
        if not args.start:
            raise SystemExit("--start is required when downloading from Alpaca (or use --csv / --synthetic)")
        symbols = [args.symbol] if args.symbol else settings.symbols
        end = _date(args.end) if args.end else None
        datasets = {s: fetch_alpaca_bars(settings, s, _date(args.start), end) for s in symbols}

    for symbol, bars in datasets.items():
        if bars.empty:
            print(f"{symbol}: no bars returned")
            continue
        result = run_backtest(bars, settings, symbol=symbol, starting_equity=args.equity)
        print(format_report(result))
        print()
        if args.trades_out:
            path = args.trades_out if len(datasets) == 1 else f"{os.path.splitext(args.trades_out)[0]}_{symbol}.csv"
            trades_frame(result).to_csv(path, index=False)
            print(f"  Trades written to {path}\n")


def _load_datasets(args: argparse.Namespace, settings: Settings) -> dict:
    if args.synthetic and settings.is_v3:
        return synthetic_leveraged(days=args.days)
    if args.synthetic:
        return {"SYNTHETIC": synthetic_bars(days=args.days, timeframe_minutes=settings.timeframe_minutes)}
    if args.csv:
        return {os.path.splitext(os.path.basename(args.csv))[0].upper(): load_csv(args.csv)}
    return fetch_history(settings, settings.learn_train_days + settings.learn_val_days + 5)


def cmd_learn(args: argparse.Namespace, settings: Settings) -> None:
    state = LearningState(settings.state_path)
    if not (args.synthetic or args.csv):
        report = run_daily_learning(settings, _load_datasets(args, settings), state)
        print(report.summary())
        return

    # Offline replay: walk through the data one session at a time, running the
    # learning step after each "day" exactly as the live bot would.
    datasets = _load_datasets(args, settings)
    all_dates = sorted({d for b in datasets.values() for d in b.index.tz_convert(NY).date})
    needed = settings.learn_train_days + settings.learn_val_days
    if len(all_dates) < needed:
        raise SystemExit(f"Need at least {needed} sessions of data, got {len(all_dates)}")
    steps = all_dates[needed - 1 :] if args.replay else all_dates[-1:]
    for day in steps:
        upto = {s: b[b.index.tz_convert(NY).date <= day] for s, b in datasets.items()}
        report = run_daily_learning(settings, upto, state, as_of=day)
        print(f"[{day}] " + report.summary())
        print()


def cmd_params(args: argparse.Namespace, settings: Settings) -> None:
    state = LearningState(settings.state_path)
    if args.rollback is not None:
        params = state.rollback(args.rollback)
        state.save()
        print(f"Rolled back to version {args.rollback}; now version {state.version}: {params or '(.env defaults)'}")
        return
    effective = state.apply(settings)
    print(f"Params version: {state.version}   risk multiplier: {state.risk_multiplier}")
    from bot.learning import tunable_for

    keys = list(tunable_for(settings)) + (["v3_risk_pct"] if settings.is_v3 else ["risk_per_trade"])
    print(f"Strategy: {'v3.1' if settings.is_v3 else 'original'}  ({settings.state_path})")
    print("Effective params: " + json.dumps({k: getattr(effective, k) for k in keys}))
    print("\nRecent learning runs:")
    for h in state.data["history"][-args.last:]:
        mark = "ADOPT" if h.get("adopted") else "keep "
        print(f"  v{h['version']:<3} {h.get('date', h['timestamp'][:10])} {mark} {h['reason']}")
    if state.data["journal"]:
        print("\nRecent live days:")
        for j in state.data["journal"][-args.last:]:
            print(f"  {j['date']}  P&L {j['pnl']:+,.2f}  equity {j['equity']:,.2f}  (params v{j['params_version']})")


def cmd_compare(args: argparse.Namespace, settings: Settings) -> None:
    """Backtest the original strategy and v3.1 on the same dates and starting equity."""
    from bot.v3.backtest import format_v3_report, run_v3_backtest

    v2 = replace(settings, strategy="ema_cross_vwap", state_path="")
    v2 = _with_learned(v2, args.base_params)
    v3 = replace(settings, strategy="v3", state_path="")
    v3 = _with_learned(v3, args.base_params)

    v3_result = run_v3_backtest(_v3_data(args, v3), v3, starting_equity=args.equity)

    # The original splits the same starting equity across its symbols.
    if args.synthetic:
        data = synthetic_leveraged(days=args.days)
        v2_data = {"QQQ": data["QQQ"]}
    else:
        end = _date(args.end) if args.end else None
        v2_data = {sym: fetch_alpaca_bars(v2, sym, _date(args.start), end) for sym in v2.symbols}
    share = args.equity / len(v2_data)
    v2_results = {sym: run_backtest(bars, v2, symbol=sym, starting_equity=share) for sym, bars in v2_data.items() if len(bars)}
    v2_final = sum(r.stats["final_equity"] for r in v2_results.values())
    v2_trades = sum(r.stats["trades"] for r in v2_results.values())

    print("=" * 62)
    print(f"ORIGINAL (EMA cross + VWAP) on {', '.join(v2_results)}, ${share:,.2f} each")
    for sym, r in v2_results.items():
        st = r.stats
        print(f"  {sym:<5} return {st['total_return_pct']:+.2f}%  trades {st['trades']:>3}  win {st['win_rate_pct']:.0f}%  max DD {st['max_drawdown_pct']:.2f}%")
    print(f"  TOTAL: ${args.equity:,.2f} -> ${v2_final:,.2f} ({(v2_final / args.equity - 1) * 100:+.2f}%), {v2_trades} trades")
    print("=" * 62)
    print(format_v3_report(v3_result))
    print("=" * 62)
    v3_final = v3_result.stats["final_equity"]
    better = "v3.1" if v3_final > v2_final else "the original"
    print(f"Higher return over this period: {better} (original ${v2_final:,.2f} vs v3.1 ${v3_final:,.2f}).")
    if v3_final < args.equity and v2_final < args.equity:
        print("Both lost money over this period: neither has shown an edge here.")
    print("Fewer than ~100 trades or a few months of data is not enough to call a winner.")


def cmd_report(args: argparse.Namespace, settings: Settings) -> None:
    from bot.v3.report import DEFAULT_LOG, audit

    print(audit(args.csv or DEFAULT_LOG, last=args.last))


def cmd_trade(args: argparse.Namespace, settings: Settings) -> None:
    from bot.live import LiveTrader

    if args.command == "paper":
        settings.paper = True
    elif args.command == "live":
        settings.paper = False
        if not args.i_understand_the_risk:
            raise SystemExit("Live trading uses real money. Re-run with --i-understand-the-risk to confirm.")

    os.makedirs("logs", exist_ok=True)
    if settings.is_v3:
        from bot.v3.live import V3LiveTrader

        handler = logging.FileHandler("logs/v3_trading.log")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logging.getLogger().addHandler(handler)
        V3LiveTrader(settings).run()
        return
    handler = logging.FileHandler("logs/trading.log")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(handler)
    LiveTrader(settings).run()


def main() -> None:
    parser = argparse.ArgumentParser(description="Intraday trading bot for US stocks (Alpaca)")
    sub = parser.add_subparsers(dest="command", required=True)

    bt = sub.add_parser("backtest", help="Simulate the strategy on historical bars")
    bt.add_argument("--symbol", help="Symbol to test (defaults to SYMBOLS from .env)")
    bt.add_argument("--start", help="Start date YYYY-MM-DD (Alpaca download)")
    bt.add_argument("--end", help="End date YYYY-MM-DD (Alpaca download)")
    bt.add_argument("--csv", help="Load bars from a CSV file instead of Alpaca")
    bt.add_argument("--synthetic", action="store_true", help="Use random-walk bars (no API keys needed)")
    bt.add_argument("--days", type=int, default=60, help="Days of synthetic data")
    bt.add_argument("--equity", type=float, default=100_000.0, help="Starting equity")
    bt.add_argument("--trades-out", help="Write the trade list to this CSV file")
    bt.add_argument("--base-params", action="store_true", help="Ignore learned params, use .env values")

    ln = sub.add_parser("learn", help="Run the daily self-improvement step now")
    ln.add_argument("--csv", help="Learn from a CSV file instead of Alpaca")
    ln.add_argument("--synthetic", action="store_true", help="Learn from random-walk bars (offline demo)")
    ln.add_argument("--days", type=int, default=60, help="Days of synthetic data")
    ln.add_argument("--replay", action="store_true",
                    help="With --csv/--synthetic: run the learning step after every session, like the live bot")

    pr = sub.add_parser("params", help="Show learned parameters and history, or roll back")
    pr.add_argument("--rollback", type=int, metavar="VERSION", help="Restore a previous params version (0 = .env)")
    pr.add_argument("--last", type=int, default=15, help="How many history entries to show")

    cmp = sub.add_parser("compare", help="Backtest v3.1 against the original strategy on the same data")
    cmp.add_argument("--start", help="Start date YYYY-MM-DD (Alpaca download)")
    cmp.add_argument("--end", help="End date YYYY-MM-DD")
    cmp.add_argument("--synthetic", action="store_true", help="Use random-walk bars (no API keys needed)")
    cmp.add_argument("--days", type=int, default=60, help="Days of synthetic data")
    cmp.add_argument("--equity", type=float, default=500.0, help="Starting equity (default $500)")
    cmp.add_argument("--base-params", action="store_true", help="Ignore learned params, use .env values")

    rp = sub.add_parser("report", help="Per-strategy review of the v3.1 trade log")
    rp.add_argument("--csv", help="Trade log to review (default logs/v3_trades.csv)")
    rp.add_argument("--last", type=int, help="Only the most recent N trades")

    sub.add_parser("paper", help="Trade on the Alpaca paper account")
    live = sub.add_parser("live", help="Trade with real money")
    live.add_argument("--i-understand-the-risk", action="store_true")

    for p in (bt, ln, pr, sub.choices["paper"], live):
        p.add_argument("--strategy", choices=["ema_cross_vwap", "v3"], help="Override STRATEGY from .env")

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings.from_env()
    if getattr(args, "strategy", None):
        settings = replace(settings, strategy=args.strategy, state_path=os.getenv("STATE_PATH", ""))

    if args.command == "backtest":
        cmd_backtest(args, settings)
    elif args.command == "learn":
        cmd_learn(args, settings)
    elif args.command == "params":
        cmd_params(args, settings)
    elif args.command == "compare":
        cmd_compare(args, settings)
    elif args.command == "report":
        cmd_report(args, settings)
    else:
        cmd_trade(args, settings)


if __name__ == "__main__":
    main()

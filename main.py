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
"""

from __future__ import annotations

import argparse
import logging
import json
import os
from datetime import datetime, timezone

from bot.backtest import format_report, run_backtest, trades_frame
from bot.config import Settings
from bot.data import fetch_alpaca_bars, fetch_history, load_csv, synthetic_bars
from bot.learning import LearningState, run_daily_learning
from bot.strategy import NY


def _date(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def cmd_backtest(args: argparse.Namespace, settings: Settings) -> None:
    if settings.learning_enabled and not args.base_params:
        state = LearningState(settings.state_path)
        if state.version:
            print(f"Using learned params version {state.version} (pass --base-params to use .env values)\n")
        settings = state.apply(settings)
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
    print("Effective params: " + json.dumps({k: getattr(effective, k) for k in
          ["ema_fast", "ema_slow", "atr_period", "stop_atr", "target_atr", "risk_per_trade"]}))
    print("\nRecent learning runs:")
    for h in state.data["history"][-args.last:]:
        mark = "ADOPT" if h.get("adopted") else "keep "
        print(f"  v{h['version']:<3} {h.get('date', h['timestamp'][:10])} {mark} {h['reason']}")
    if state.data["journal"]:
        print("\nRecent live days:")
        for j in state.data["journal"][-args.last:]:
            print(f"  {j['date']}  P&L {j['pnl']:+,.2f}  equity {j['equity']:,.2f}  (params v{j['params_version']})")


def cmd_trade(args: argparse.Namespace, settings: Settings) -> None:
    from bot.live import LiveTrader

    if args.command == "paper":
        settings.paper = True
    elif args.command == "live":
        settings.paper = False
        if not args.i_understand_the_risk:
            raise SystemExit("Live trading uses real money. Re-run with --i-understand-the-risk to confirm.")

    os.makedirs("logs", exist_ok=True)
    logging.getLogger().addHandler(logging.FileHandler("logs/trading.log"))
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

    sub.add_parser("paper", help="Trade on the Alpaca paper account")
    live = sub.add_parser("live", help="Trade with real money")
    live.add_argument("--i-understand-the-risk", action="store_true")

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings.from_env()

    if args.command == "backtest":
        cmd_backtest(args, settings)
    elif args.command == "learn":
        cmd_learn(args, settings)
    elif args.command == "params":
        cmd_params(args, settings)
    else:
        cmd_trade(args, settings)


if __name__ == "__main__":
    main()

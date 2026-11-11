"""Command-line entry point.

Examples:
    python main.py backtest --synthetic
    python main.py backtest --symbol SPY --start 2025-01-01 --end 2025-06-30
    python main.py backtest --csv data/spy_5min.csv
    python main.py paper
"""

from __future__ import annotations

import argparse
import logging
import os
from datetime import datetime, timezone

from bot.backtest import format_report, run_backtest, trades_frame
from bot.config import Settings
from bot.data import fetch_alpaca_bars, load_csv, synthetic_bars


def _date(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def cmd_backtest(args: argparse.Namespace, settings: Settings) -> None:
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

    sub.add_parser("paper", help="Trade on the Alpaca paper account")
    live = sub.add_parser("live", help="Trade with real money")
    live.add_argument("--i-understand-the-risk", action="store_true")

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings.from_env()

    if args.command == "backtest":
        cmd_backtest(args, settings)
    else:
        cmd_trade(args, settings)


if __name__ == "__main__":
    main()

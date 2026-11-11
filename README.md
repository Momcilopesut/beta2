# Day-trading bot

An intraday trading bot for US stocks, built on the [Alpaca](https://alpaca.markets) API.
It backtests on historical bars and paper-trades with the same strategy and risk code,
so what you test is what runs.

> **Warning:** Day trading is risky and most strategies lose money after costs.
> This bot does not guarantee profits. Backtest first, paper-trade for weeks, and
> trade real money only with money you can afford to lose. US accounts under $25k
> are subject to the Pattern Day Trader rule (no more than 3 day trades in 5 days).

## Strategy

`bot/strategy.py` — EMA crossover with a VWAP filter, long only, on 5-minute bars:

| Rule | Default |
| --- | --- |
| **Entry** | Fast EMA (9) crosses above slow EMA (21), close is above session VWAP, time is 09:45–15:30 ET |
| **Exit** | Fast EMA crosses back below slow EMA |
| **Stop loss** | 1.5 × ATR(14) below entry |
| **Take profit** | 3 × ATR(14) above entry |
| **End of day** | Everything is closed at 15:50 ET; no positions are held overnight |

**Risk management** (`bot/risk.py`):

- Each trade is sized so that hitting the stop loses about 1% of equity.
- No single position can be more than 25% of equity.
- After a 3% loss in one day, the bot stops opening trades for the rest of that day.

You can change every number in `.env`.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env   # then add your Alpaca paper-trading keys
```

To get free paper keys, sign up at alpaca.markets, open the **Paper Trading** account,
and generate API keys there.

## Usage

Try the backtester on random data. No API keys are needed:

```bash
python main.py backtest --synthetic
```

Backtest on real historical data from Alpaca:

```bash
python main.py backtest --symbol SPY --start 2025-01-01 --end 2025-06-30 --trades-out spy_trades.csv
```

Backtest a CSV file. It needs a `timestamp` column plus `open,high,low,close,volume`:

```bash
python main.py backtest --csv data/my_bars.csv
```

Paper trade during market hours. Leave it running; it logs to `logs/trading.log`:

```bash
python main.py paper
```

Trade real money (not recommended until you've proven the strategy):

```bash
python main.py live --i-understand-the-risk
```

## How the backtest stays honest

- A signal on one bar is filled at the **next** bar's open, so the backtest never uses
  prices it couldn't have known yet.
- If a bar touches both the stop and the target, the backtest assumes the stop hit first.
- Every fill pays slippage (2 bps by default).
- Only regular-hours bars are used, and positions never carry over to the next day.

## Project layout

```
main.py            command-line interface
bot/config.py      settings from .env
bot/strategy.py    indicators + signal generation
bot/risk.py        position sizing, stop/target prices
bot/backtest.py    bar-by-bar simulator and stats
bot/data.py        Alpaca / CSV / synthetic bars
bot/live.py        paper/live trading loop
tests/             pytest suite
```

## Writing your own strategy

Write a class with a `prepare(bars) -> DataFrame` method. It should return the bars plus
an `atr` column and a `signal` column (`1` = enter long, `-1` = exit, `0` = hold), and
have a `warmup_bars` property. To use it, swap it in where `EmaCrossVwap` is created in
`bot/backtest.py` and `bot/live.py`.

## Tests

```bash
python -m pytest
```

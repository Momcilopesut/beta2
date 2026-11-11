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

## Daily self-improvement

`bot/learning.py` — after each trading day closes, the bot retunes itself:

1. **Journal:** records the day's P&L.
2. **Search:** backtests about 34 variations of its current parameters on recent
   history: EMA lengths, ATR period, stop and target distances. Each variation is one
   or two steps away from the current values, so the parameters drift gradually
   instead of jumping.
3. **Holdout check:** the best variation is picked using the older 30 sessions only.
   It is then tested on the most recent 10 sessions, which were not used to pick it.
   It is adopted only if **all** of these hold:
   - Its Sharpe ratio beats the current parameters by at least `LEARN_MARGIN` (2.0)
     on **both** windows.
   - It made money on the held-out sessions.
   - It made enough trades to be trusted.
   - The last change was at least `LEARN_COOLDOWN_DAYS` (10) sessions ago.
4. **Risk throttle:** after a losing streak (3 days in a row, or a losing 5-day
   stretch), position sizes shrink to 75%, down to a floor of 25%. They grow back as
   results recover.
5. **Builds on itself:** the next day's search starts from whatever was adopted.
   Every run is saved with a version number in `state/learning.json`.

```bash
python main.py learn                                # run one learning step now (Alpaca data)
python main.py learn --synthetic --days 80 --replay # watch it learn day by day, offline
python main.py params                               # current params, history, live journal
python main.py params --rollback 2                  # restore version 2 (0 = .env defaults)
python main.py backtest --symbol SPY --start 2025-01-01 --base-params  # compare against .env values
```

`backtest` uses the learned parameters by default. Set `LEARNING_ENABLED=false` to turn
the feature off.

**A word of caution:** a bot that retunes itself every day can easily fit itself to
noise. It looks better on recent history every time without getting better at
trading. The safeguards above make it change rarely. In testing on random data with no
real pattern, it changed parameters 0–2 times in 41 trading days (about once a
month on average). On real data, a change should be equally rare. Learning can only tune this
strategy's settings; it can't find an edge the strategy doesn't have. Keep an eye on
`python main.py params`, and roll back if a change makes things worse in paper trading.

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
bot/live.py        paper/live trading loop (runs learning after each close)
bot/learning.py    daily self-improvement: parameter search, holdout check, risk throttle
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

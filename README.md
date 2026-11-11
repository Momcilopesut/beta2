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

## v3.1 system (TQQQ / SQQQ)

A second, more complex strategy. You turn it on with `STRATEGY=v3` in `.env`, or with
`--strategy v3` on any command. It watches QQQ and trades the 3x leveraged funds: TQQQ
moves about 3 times as much as QQQ, and SQQQ moves 3 times as much in the opposite
direction. Full rules are in `bot/v3/__init__.py`. In short:

- **10:00 classifier:** QQQ's move since the open and its VWAP slope label the day as
  a bullish trend, a bearish trend, or choppy ("rotational"). Mixed signals count
  as choppy.
- **Strategy 1, Opening Range Breakout (9:50–10:15, once a day):** buy TQQQ or SQQQ
  (whichever matches QQQ's direction) when it breaks above its 9:30–9:45 high and is
  above VWAP.
- **Strategy 2, VWAP Reversion (choppy days):** buy TQQQ or SQQQ when it's stretched
  2 standard deviations and at least 0.6% below VWAP.
- **Strategy 3, EMA Pullback (trend days, from about 11:15):** buy the trend fund when
  it dips to its 21-bar average and closes back above its 9-bar average. After noon
  it's skipped if QQQ has already moved more than 1.75%.
- **Managing a trade:** risk 0.5% per trade (1R), stop 0.8% below entry, stop moves to
  break-even at +0.6R, half sold at +1R (+0.7R after noon, which also locks +0.25R),
  exit after 30 minutes if a trade stalls, one position at a time, no new trades
  after a -1% day, and everything closed by 15:50.

The live bot uses the same rule code as the backtester. It checks the open position
every 15 seconds, saves it so a restart keeps managing it, records real fill prices,
and keeps protecting an open position even after the daily loss limit trips. Every
trade is written to `logs/v3_trades.csv`, including its R-multiple and its MAE/MFE
(how far it went against you and in your favor).

```bash
python main.py compare --start 2026-01-01 --equity 500        # v3.1 vs the original, same data
python main.py backtest --strategy v3 --start 2026-01-01 --equity 500 --trades-out v3_bt.csv
python main.py report --csv v3_bt.csv                         # per-strategy review of those trades
python main.py paper --strategy v3                            # paper trade v3.1
python main.py report                                         # review live v3.1 trades so far
```

Daily learning works for v3.1 too. It has its own history in `state/learning_v3.json`
and tunes only trade management: stop %, shield, scale level, time stop and VWAP band.
The entry rules stay as written.

**Run only one bot per Alpaca account.** Each bot only closes its own symbols, but
the daily loss limit, buying power and day-trade count are shared by the whole
account.

**Leveraged funds are risky.** TQQQ and SQQQ can move 3% or more on a 1% QQQ move,
and they lose value over time in choppy markets (that's why the bot never holds them
overnight). On random test data, v3.1 averaged about 0R per trade. Before trusting it,
compare it with the original on real data using `python main.py compare`.

## Swing strategy test (multi-day)

`bot/swing.py` tests a different kind of strategy: buying short dips in an uptrend and
holding for a few days (Larry Connors' published RSI(2) rules, with standard values and
no tuning):

- Funds: SPY, QQQ, IWM and DIA, each allowed up to a quarter of the account.
- **Buy** at the next open when the fund closed above its 200-day average and its 2-day
  RSI closed below 10.
- **Sell** at the next open after it closes above its 5-day average, or after 10 days.

Positions are held overnight, so these trades never count as day trades. Each trade is
compared with buying the same fund on a random day and holding it for the same
number of days. That shows whether the timing adds anything beyond the market rising.

```bash
python main.py swingtest --start 2024-01-01 --end 2024-12-31 --equity 500
```

It passed on 2024, 2021–2023 and 2025–2026, each tested once with the pass rule
fixed in advance. It's the only strategy in this repo that has shown an edge on data
it wasn't designed on.

**Paper trading it:** `bot/swing_live.py` runs once per trading day, at 9:31 New York
time. It reads the previous day's closing prices, sells funds that recovered (or that
it has held for 10 days), and buys new dips with a quarter of the account each. That
matches the backtest's "signal at the close, trade at the next open" exactly.

```bash
caffeinate -i python main.py swing-paper          # leave running; trades each morning
python main.py swing-paper --once                 # or do today's run by hand (market open)
```

State is kept in `state/swing.json`, trades go to `logs/swing_trades.csv`, and the log
is in `logs/swing.log`. Run it **instead of** the day-trading bots, because the original
also trades SPY and QQQ on the same account. There's no stop-loss (the tested rules
don't use one); the 10-day limit caps how long a losing position is held.

## ETF momentum test (monthly)

`bot/momentum.py` is a monthly strategy across asset classes (Mebane Faber's
"relative strength" rules, with standard values and no tuning):

- Universe: SPY, QQQ, IWM, EFA, EEM, TLT, IEF, GLD, DBC, VNQ.
- At each month-end, score every ETF by the average of its 3-, 6- and 12-month
  returns and hold the top 3, a third of the account each.
- An ETF below its 10-month average isn't bought; that third stays in cash.
- Rebalance at the next open. That's about one trade day a month, with no day trades.

It's judged against simply holding all 10 ETFs equally. It passes if its Sharpe ratio
(return per unit of risk) is higher **and** its worst drop is smaller. It's tested
once, over the whole period, using Yahoo Finance data (`pip install yfinance`):

```bash
python main.py momentumtest --start 2008-01-01 --end 2026-11-10 --equity 500
```

## Small accounts (under $25,000)

The bot switches to small-account mode automatically when equity is below
`SMALL_ACCOUNT_LIMIT`:

- **Fractional shares.** A $500 account can't buy one whole $750 share of SPY, so
  the bot buys fractions instead (for example 0.33 shares).
- **Bigger position cap.** Each position can use up to 50% of the account instead of
  25%, so a trade isn't tiny. The 1%-risk sizing still limits how much is lost if
  the stop is hit.
- **Stops watched by the bot.** Alpaca doesn't allow its automatic stop-loss and
  take-profit orders on fractional shares. The bot checks the price every 30 seconds
  and sells at market when the stop or target is crossed. In a fast drop, the sale
  can fill a bit below the stop price. This only works while the bot is running.
- **Day-trade guard.** US accounts under $25k may make at most 3 day trades in 5
  business days, and Alpaca rejects the order that would make a 4th. That rejected
  order could be a sell, leaving a position stuck overnight. So once 3 day trades have
  been made, the bot opens nothing new until older ones roll out of the 5-day window.
  The backtester follows the same rule, but it counts each symbol on its own, while
  Alpaca counts all symbols together.

Over $25,000, the bot buys whole shares, and Alpaca holds the stop and target as a
bracket order. Settings: `FRACTIONAL_SHARES` (auto/true/false), `SMALL_ACCOUNT_LIMIT`,
`SMALL_ACCOUNT_MAX_POSITION_PCT` and `PDT_GUARD`.

Backtest at your real account size to see realistic numbers:

```bash
python main.py backtest --symbol SPY --start 2026-01-01 --equity 500
```

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
bot/v3/            v3.1 system: rules.py (classifier + 3 strategies), position.py (trade
                   management), backtest.py, live.py, report.py (trade log + review)
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

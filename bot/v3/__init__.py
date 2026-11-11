"""v3.1 multi-strategy system for QQQ-linked 3x ETFs (TQQQ / SQQQ).

Rules, all evaluated on closed 5-minute bars of the current session:

Regime (decided once, when the 10:00 bar closes, from QQQ):
- QQQ moved >= V3_TREND_PCT from the open AND session VWAP slopes >= V3_TREND_SLOPE %/hour:
  both up   -> TREND_BULL (trade TQQQ)
  both down -> TREND_BEAR (trade SQQQ, which rises when QQQ falls)
  mixed     -> ROTATIONAL
- otherwise ROTATIONAL

Strategy 1, Opening Range Breakout (bars closing 9:50-10:15, once per day, any regime):
  QQQ is up (down) >= V3_ORB_QQQ_PCT from the open -> look at TQQQ (SQQQ);
  buy if it closes above its 9:30-9:45 high and above its VWAP.

Strategy 2, VWAP Reversion (ROTATIONAL days, from 10:00):
  buy TQQQ or SQQQ when it closes at or below VWAP - V3_BAND_SD standard
  deviations and at least V3_VWAP_GAP_PCT below VWAP.

Strategy 3, EMA Pullback (TREND days, from 10:00, needs 21 bars, i.e. ~11:15):
  in the trend ETF, EMA9 > EMA21, the last or previous bar dipped to within
  0.3% of EMA21, and the bar closed back above EMA9. After 12:00 it is skipped
  if QQQ has already moved more than V3_EXHAUSTION_PCT (the move is tired).

Trade management (one position at a time, long only):
- Size: 1R = V3_RISK_PCT of equity, stop V3_STOP_PCT below entry, capped at
  V3_MAX_POSITION_PCT of equity; fractional shares on small accounts.
- Shield: at +0.6R the stop moves to break-even.
- Scale: sell half at +1.0R (before noon) or +0.7R (after noon); an afternoon
  scale also locks the stop at +0.25R.
- Time stop: after 30 minutes, exit if below +0.4R and not yet scaled.
- No new entries for 10 minutes after an exit, after 15:45, after a -2R day
  (V3_MAX_DAILY_LOSS_PCT), or (small accounts) after 3 day trades in 5 days.
- Everything is closed at FLATTEN_TIME.
"""

TREND_BULL = "TREND_BULL"
TREND_BEAR = "TREND_BEAR"
ROTATIONAL = "ROTATIONAL"

QQQ = "QQQ"
BULL_ETF = "TQQQ"
BEAR_ETF = "SQQQ"
DATA_SYMBOLS = (QQQ, BULL_ETF, BEAR_ETF)
TRADED_SYMBOLS = (BULL_ETF, BEAR_ETF)

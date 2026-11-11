"""Paper / live trading for the swing strategy in bot/swing.py.

Once per trading day, shortly after the open (SWING_RUN_TIME, default 9:31 New York
time), it reads the previous day's completed daily bars and, exactly like the
backtest's "next open" fills:
1. sells funds that closed above their 5-day average, or were held 10 days;
2. buys funds that closed above their 200-day average with a 2-day RSI below 10,
   a quarter of the account each (fractional shares, market orders).

State (entry dates, last run) is kept in state/swing.json and every closed trade
is appended to logs/swing_trades.csv. A second run on the same day does nothing.
Positions are always held overnight, so trades never count as day trades.
Run it on its own Alpaca account: it only touches SPY, QQQ, IWM and DIA, but the
original day-trading bot also trades SPY and QQQ.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import time as time_mod
from datetime import date, datetime, time, timedelta, timezone

from .config import Settings
from .strategy import NY
from .swing import MAX_HOLD_DAYS, RSI_BUY, UNIVERSE, fetch_daily, prepare

log = logging.getLogger("bot.swing")

STATE_PATH = "state/swing.json"
TRADE_LOG = "logs/swing_trades.csv"
RUN_TIME = time(9, 31)


def _ny():
    from zoneinfo import ZoneInfo

    return ZoneInfo(NY)


class SwingTrader:
    def __init__(self, settings: Settings, client=None, fetch=fetch_daily, state_path=STATE_PATH, trade_log=TRADE_LOG):
        settings.require_credentials()
        if client is None:
            from alpaca.trading.client import TradingClient

            client = TradingClient(settings.api_key, settings.api_secret, paper=settings.paper)
        self.s = settings
        self.client = client
        self.fetch = fetch
        self.state_path = state_path
        self.trade_log = trade_log
        self.state = {"last_run": None, "positions": {}}
        if os.path.exists(state_path):
            with open(state_path) as f:
                self.state.update(json.load(f))

    # ---- loop ----------------------------------------------------------------

    def run_forever(self) -> None:
        log.info("Swing bot started (%s). Runs once per trading day after %s New York time.",
                 "PAPER" if self.s.paper else "LIVE", RUN_TIME.strftime("%H:%M"))
        while True:
            try:
                clock = self.client.get_clock()
                now_et = datetime.now(timezone.utc).astimezone(_ny())
                if clock.is_open and now_et.time() >= RUN_TIME and self.state["last_run"] != str(now_et.date()):
                    self.run_once(now_et)
                elif not clock.is_open:
                    wait = (clock.next_open - clock.timestamp).total_seconds()
                    log.info("Market closed. Next open %s", clock.next_open.astimezone(_ny()))
                    time_mod.sleep(max(60, min(wait, 3600)))
                    continue
            except KeyboardInterrupt:
                raise
            except Exception:
                log.exception("Swing run failed; will retry in a minute")
            time_mod.sleep(60)

    # ---- one daily run -----------------------------------------------------------

    def run_once(self, now_et: datetime | None = None) -> dict:
        now_et = now_et or datetime.now(timezone.utc).astimezone(_ny())
        today = now_et.date()
        if self.state["last_run"] == str(today):
            log.info("Already ran today (%s); nothing to do.", today)
            return {"sold": [], "bought": []}
        clock = self.client.get_clock()
        if not clock.is_open:
            raise RuntimeError("The market is closed; the swing bot trades only after the open.")

        held = {p.symbol: p for p in self.client.get_all_positions() if p.symbol in UNIVERSE}
        frames = {}
        for sym in UNIVERSE:
            bars = self.fetch(self.s, sym, today - timedelta(days=420), today)
            bars = bars[bars.index < today]  # completed days only
            frames[sym] = prepare(bars)

        # Forget saved positions that are gone; adopt ones we didn't know about.
        saved = self.state["positions"]
        for sym in list(saved):
            if sym not in held:
                log.warning("%s is no longer held (closed outside the bot?); forgetting it.", sym)
                del saved[sym]
        for sym, p in held.items():
            if sym not in saved:
                last_day = str(frames[sym].index[-1]) if len(frames[sym]) else str(today)
                saved[sym] = {"entry_date": last_day, "entry_price": float(p.avg_entry_price), "days": 0}
                log.warning("Found an existing %s position; managing it from now on.", sym)

        sold, bought = [], []
        # 1. Exits
        for sym, p in held.items():
            f = frames[sym]
            if f.empty:
                continue
            row = f.iloc[-1]
            pos = saved[sym]
            days = sum(1 for d in f.index if d >= date.fromisoformat(pos["entry_date"]))
            pos["days"] = days
            reason = "RECOVERED" if row.close > row.sma_exit else ("TIME_LIMIT" if days >= MAX_HOLD_DAYS else "")
            log.info("%s held %d day(s): close %.2f vs 5-day avg %.2f%s", sym, days, row.close, row.sma_exit,
                     f" -> SELL ({reason})" if reason else "")
            if reason:
                order = self.client.close_position(sym)
                fill, qty = self._wait_fill(order, float(p.current_price), float(p.qty))
                self._log_trade(sym, pos, fill, qty, today, reason)
                del saved[sym]
                sold.append(sym)

        # 2. Entries
        account = self.client.get_account()
        equity = float(account.equity)
        cash = float(account.buying_power) * 0.98
        for sym in UNIVERSE:
            f = frames[sym]
            if sym in saved or sym in sold or len(f) < 200 or f["sma_trend"].isna().iloc[-1]:
                continue
            row = f.iloc[-1]
            if row.close > row.sma_trend and row.rsi < RSI_BUY:
                dollars = round(min(equity / len(UNIVERSE), cash), 2)
                if dollars < 1:
                    log.info("%s: dip signal but not enough buying power", sym)
                    continue
                order = self._buy(sym, dollars)
                fill, qty = self._wait_fill(order, float(row.close), dollars / float(row.close))
                cash -= dollars
                saved[sym] = {"entry_date": str(today), "entry_price": fill, "days": 0}
                bought.append(sym)
                log.info("%s: BUY $%.2f (%.4f shares @ %.2f) - RSI(2) %.1f, close %.2f above 200-day %.2f",
                         sym, dollars, qty, fill, row.rsi, row.close, row.sma_trend)
            else:
                log.info("%s: no signal (RSI(2) %.1f, %s 200-day average)", sym, row.rsi,
                         "above" if row.close > row.sma_trend else "below")

        self.state["last_run"] = str(today)
        self._save()
        log.info("Swing run for %s done: sold %s, bought %s", today, sold or "nothing", bought or "nothing")
        return {"sold": sold, "bought": bought}

    # ---- helpers -------------------------------------------------------------------

    def _buy(self, symbol: str, dollars: float):
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        return self.client.submit_order(
            MarketOrderRequest(symbol=symbol, notional=dollars, side=OrderSide.BUY, time_in_force=TimeInForce.DAY)
        )

    def _wait_fill(self, order, fallback_price: float, fallback_qty: float, timeout: float = 15) -> tuple[float, float]:
        deadline = time_mod.time() + timeout
        order_id = getattr(order, "id", None)
        while order_id is not None:
            try:
                o = self.client.get_order_by_id(order_id)
                status = str(getattr(o.status, "value", o.status)).lower()
                if status == "filled" and o.filled_avg_price:
                    return float(o.filled_avg_price), float(o.filled_qty)
            except Exception:
                log.exception("Could not check order %s", order_id)
            if time_mod.time() >= deadline:
                break
            time_mod.sleep(1)
        return fallback_price, fallback_qty

    def _log_trade(self, sym, pos, exit_price, qty, exit_date, reason) -> None:
        entry = float(pos["entry_price"])
        ret = exit_price / entry - 1 if entry else 0.0
        log.info("%s: SOLD %.4f @ %.2f (bought @ %.2f) -> %+.2f%%, $%+.2f (%s)",
                 sym, qty, exit_price, entry, ret * 100, (exit_price - entry) * qty, reason)
        os.makedirs(os.path.dirname(self.trade_log) or ".", exist_ok=True)
        new = not os.path.exists(self.trade_log)
        with open(self.trade_log, "a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["symbol", "entry_date", "exit_date", "entry_price", "exit_price", "qty", "return_pct", "pnl", "days", "reason"])
            w.writerow([sym, pos["entry_date"], str(exit_date), round(entry, 4), round(exit_price, 4), round(qty, 6),
                        round(ret * 100, 3), round((exit_price - entry) * qty, 2), pos.get("days", ""), reason])

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
        with open(self.state_path, "w") as f:
            json.dump(self.state, f, indent=2)

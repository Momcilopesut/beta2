"""Live / paper trading loop on Alpaca.

The loop wakes every 30 seconds. Whenever a new bar has closed, it downloads
recent bars for every symbol, runs the same strategy code as the backtester,
and acts on the last closed bar:
- ENTRY with no position: buy.
- EXIT with a position: sell at market.

How protective exits work depends on account size:
- Large accounts buy whole shares with a bracket order, so Alpaca holds the
  stop loss and take profit on its servers.
- Small accounts (under SMALL_ACCOUNT_LIMIT) buy fractional shares. Alpaca does
  not allow bracket orders on fractions, so the bot watches the stop and target
  itself on every 30-second check and sells at market when one is crossed. A
  fast drop can therefore fill somewhat below the stop price.

Small accounts also respect the pattern-day-trader rule: once 3 day trades
were made in the last 5 business days, no new positions are opened, because
Alpaca would reject the closing order of a 4th and leave it stuck overnight.

Everything is closed at FLATTEN_TIME, and no new trades open for the day once
the account is down MAX_DAILY_LOSS_PCT from the previous close.

After the close on each trading day it records the day's P&L and runs the
self-improvement step in bot/learning.py, then trades the next day with
whatever parameters that step settled on.
"""

from __future__ import annotations

import logging
import time as time_mod
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pandas as pd

from .config import Settings
from .data import fetch_alpaca_bars, fetch_history
from .learning import LearningState, run_daily_learning
from .risk import plan_entry
from .strategy import ENTRY, EXIT, NY, EmaCrossVwap

log = logging.getLogger("bot.live")

POLL_SECONDS = 30


@dataclass
class Levels:
    stop: float
    target: float


class LiveTrader:
    def __init__(self, settings: Settings):
        from alpaca.trading.client import TradingClient

        settings.require_credentials()
        self.base = settings  # .env settings; learned values are layered on top
        self.state = LearningState(settings.state_path)
        self.client = TradingClient(settings.api_key, settings.api_secret, paper=settings.paper)
        self._load_params()
        self.last_bar_seen: dict[str, pd.Timestamp] = {}
        self.levels: dict[str, Levels] = {}  # stops/targets the bot watches itself
        self.halted_date = None
        self.flattened_date = None
        self.traded_date = None
        self.learned_date = None
        self.pdt_warned_date = None

    def _load_params(self) -> None:
        self.s = self.state.apply(self.base) if self.base.learning_enabled else self.base
        self.strategy = EmaCrossVwap(self.s)
        log.info(
            "Using params version %d: ema %d/%d, atr %d, stop %.2f ATR, target %.2f ATR, risk/trade %.4f",
            self.state.version if self.base.learning_enabled else 0,
            self.s.ema_fast, self.s.ema_slow, self.s.atr_period, self.s.stop_atr, self.s.target_atr,
            self.s.risk_per_trade,
        )

    # ---- main loop -------------------------------------------------------

    def run(self) -> None:
        mode = "PAPER" if self.s.paper else "LIVE (REAL MONEY)"
        account = self.client.get_account()
        equity = float(account.equity)
        log.info("Starting %s trading on %s, equity $%s", mode, ", ".join(self.s.symbols), account.equity)
        if self.s.is_small_account(equity):
            log.info(
                "Small account (under $%s): %s shares, position cap %.0f%%, day-trade guard %s",
                f"{self.s.small_account_limit:,.0f}",
                "fractional" if self.s.use_fractional(equity) else "whole",
                self.s.position_cap_pct(equity) * 100,
                "on" if self.s.pdt_guard else "OFF",
            )

        while True:
            try:
                self.tick()
            except KeyboardInterrupt:
                raise
            except Exception:
                log.exception("Error during tick; will retry shortly")
            time_mod.sleep(POLL_SECONDS)

    def tick(self) -> None:
        clock = self.client.get_clock()
        if not clock.is_open:
            self._end_of_day()
            wait = (clock.next_open - clock.timestamp).total_seconds()
            log.info("Market closed. Next open %s", clock.next_open.astimezone(_ny()))
            time_mod.sleep(max(0, min(wait, 15 * 60)))
            return

        now = datetime.now(timezone.utc)
        now_et = now.astimezone(_ny())
        today = now_et.date()
        self.traded_date = today

        if now_et.time() >= self.s.flatten_time:
            if self.flattened_date != today:
                log.info("Flatten time reached, closing all positions")
                self._close_own_positions()
                self.levels.clear()
                self.flattened_date = today
            return

        account = self.client.get_account()
        equity = float(account.equity)
        last_equity = float(account.last_equity)
        if self.halted_date != today and equity <= last_equity * (1 - self.s.max_daily_loss_pct):
            log.warning("Daily loss limit hit (equity %.2f vs %.2f). Flattening and halting for today.", equity, last_equity)
            self._close_own_positions()
            self.levels.clear()
            self.halted_date = today

        positions = {p.symbol: p for p in self.client.get_all_positions()}
        open_orders = self._open_orders()

        # Forget levels for positions that are gone (a buy still waiting to fill keeps them).
        for symbol in list(self.levels):
            if symbol not in positions and not open_orders.get(symbol):
                del self.levels[symbol]

        # Bot-managed stops and targets (fractional positions).
        for symbol, position in list(positions.items()):
            if symbol in self.levels and not open_orders.get(symbol):
                if self._check_levels(symbol, position):
                    positions.pop(symbol)

        entries_allowed, why_not = self._entries_allowed(account, equity, today)

        bar_len = timedelta(minutes=self.s.timeframe_minutes)
        step = self.s.timeframe_minutes * 60
        latest_closed_start = pd.Timestamp(
            datetime.fromtimestamp((now.timestamp() // step) * step, timezone.utc)
        ) - bar_len
        for symbol in self.s.symbols:
            if self.last_bar_seen.get(symbol) is not None and self.last_bar_seen[symbol] >= latest_closed_start:
                continue  # nothing new since the last check
            try:
                self._process_symbol(
                    symbol, now, account, equity, positions.get(symbol), open_orders.get(symbol, []),
                    entries_allowed, why_not,
                )
            except Exception:
                log.exception("Failed to process %s", symbol)

    def _entries_allowed(self, account, equity: float, today) -> tuple[bool, str]:
        if self.halted_date == today:
            return False, "daily loss limit active"
        if self.s.pdt_guard and self.s.is_small_account(equity):
            day_trades = int(getattr(account, "daytrade_count", 0) or 0)
            if day_trades >= 3:
                if self.pdt_warned_date != today:
                    log.info("Day-trade guard: %d day trades in the last 5 business days, no new entries today", day_trades)
                    self.pdt_warned_date = today
                return False, f"day-trade guard ({day_trades} day trades in 5 days)"
        return True, ""

    # ---- end of day: journal + self-improvement ----------------------------

    def _end_of_day(self) -> None:
        """Run once after a session the bot traded in has closed."""
        day = self.traded_date
        if day is None or self.learned_date == day:
            return
        self.learned_date = day

        account = self.client.get_account()
        pnl = float(account.equity) - float(account.last_equity)
        self.state.record_day(day, pnl, float(account.equity))
        self.state.save()
        log.info("Day %s closed with P&L %+.2f, equity %s", day, pnl, account.equity)

        if not self.base.learning_enabled:
            return
        log.info("Running daily learning...")
        try:
            history = fetch_history(self.base, self.base.learn_train_days + self.base.learn_val_days + 5)
            report = run_daily_learning(self.base, history, self.state, as_of=day)
            for line in report.summary().splitlines():
                log.info(line)
            self._load_params()
            self.last_bar_seen.clear()
        except Exception:
            log.exception("Daily learning failed; keeping current parameters")

    # ---- per-symbol logic -----------------------------------------------

    def _process_symbol(self, symbol, now, account, equity, position, orders, entries_allowed, why_not) -> None:
        bars = fetch_alpaca_bars(self.s, symbol, start=now - timedelta(days=7))
        bar_len = timedelta(minutes=self.s.timeframe_minutes)
        bars = bars[bars.index + bar_len <= now]  # closed bars only
        if len(bars) < self.strategy.warmup_bars:
            log.info("%s: not enough bars yet (%d)", symbol, len(bars))
            return

        last_ts = bars.index[-1]
        if self.last_bar_seen.get(symbol) == last_ts:
            return  # the newest bar isn't published yet; try again next check
        self.last_bar_seen[symbol] = last_ts
        if now - (last_ts + bar_len) > 3 * bar_len:
            log.info("%s: latest bar %s is stale, skipping", symbol, last_ts)
            return

        row = self.strategy.prepare(bars).iloc[-1]
        log.info(
            "%s %s close=%.2f ema_fast=%.2f ema_slow=%.2f vwap=%.2f signal=%d",
            symbol, last_ts.astimezone(_ny()).strftime("%H:%M"),
            row.close, row.ema_fast, row.ema_slow, row.vwap, row.signal,
        )

        # A position without a bracket and without known levels (e.g. after a
        # restart): rebuild its stop and target from the entry price.
        if position is not None and not orders and symbol not in self.levels and row.atr > 0:
            entry = float(position.avg_entry_price)
            self.levels[symbol] = Levels(
                round(entry - self.s.stop_atr * row.atr, 2), round(entry + self.s.target_atr * row.atr, 2)
            )
            log.info("%s: watching stop %.2f / target %.2f", symbol, self.levels[symbol].stop, self.levels[symbol].target)

        if position is not None and row.signal == EXIT:
            log.info("%s: exit signal, closing %s shares", symbol, position.qty)
            self._close_symbol(symbol, orders)
            return

        if position is None and not orders and row.signal == ENTRY:
            if not entries_allowed:
                log.info("%s: entry signal ignored, %s", symbol, why_not)
                return
            fractional = self.s.use_fractional(equity)
            plan = plan_entry(
                self.s, equity, float(row.close), float(row.atr),
                fractional=fractional, buying_power=float(account.buying_power),
            )
            if plan is None:
                log.info("%s: entry signal but the position size is too small to trade", symbol)
                return
            if fractional:
                self._submit_fractional(symbol, plan)
            else:
                self._submit_bracket(symbol, plan)

    def _check_levels(self, symbol, position) -> bool:
        """Sell if the price crossed the bot-managed stop or target. Returns True if sold."""
        levels = self.levels[symbol]
        price = float(position.current_price)
        if price <= levels.stop:
            reason = f"stop {levels.stop:.2f} hit (price {price:.2f})"
        elif price >= levels.target:
            reason = f"target {levels.target:.2f} hit (price {price:.2f})"
        else:
            return False
        log.info("%s: %s, selling %s shares", symbol, reason, position.qty)
        self.client.close_position(symbol)
        del self.levels[symbol]
        return True

    # ---- order helpers ---------------------------------------------------

    def _submit_fractional(self, symbol, plan) -> None:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        order = self.client.submit_order(
            MarketOrderRequest(symbol=symbol, qty=plan.qty, side=OrderSide.BUY, time_in_force=TimeInForce.DAY)
        )
        self.levels[symbol] = Levels(plan.stop_price, plan.target_price)
        log.info(
            "%s: BUY %s shares (watching stop %.2f / target %.2f), order %s",
            symbol, plan.qty, plan.stop_price, plan.target_price, order.id,
        )

    def _submit_bracket(self, symbol, plan) -> None:
        from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest, StopLossRequest, TakeProfitRequest

        order = self.client.submit_order(
            MarketOrderRequest(
                symbol=symbol,
                qty=int(plan.qty),
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
                order_class=OrderClass.BRACKET,
                take_profit=TakeProfitRequest(limit_price=plan.target_price),
                stop_loss=StopLossRequest(stop_price=plan.stop_price),
            )
        )
        log.info(
            "%s: BUY %d (bracket stop %.2f / target %.2f), order %s",
            symbol, int(plan.qty), plan.stop_price, plan.target_price, order.id,
        )

    def _open_orders(self) -> dict[str, list]:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        orders = self.client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, nested=True))
        by_symbol: dict[str, list] = {}
        for o in orders:
            by_symbol.setdefault(o.symbol, []).append(o)
        return by_symbol

    def _close_own_positions(self) -> None:
        """Close only this bot's symbols, never positions another bot or person opened."""
        open_orders = self._open_orders()
        held = {p.symbol for p in self.client.get_all_positions()}
        for symbol in self.s.symbols:
            if symbol in held or open_orders.get(symbol):
                self._close_symbol(symbol, open_orders.get(symbol, []))
        self.levels.clear()

    def _close_symbol(self, symbol, orders) -> None:
        # Bracket legs reserve the shares, so cancel them before selling.
        for o in orders:
            for oid in [o.id] + [leg.id for leg in (o.legs or [])]:
                try:
                    self.client.cancel_order_by_id(oid)
                except Exception:
                    pass  # already filled or cancelled
        if orders:
            time_mod.sleep(1)
        try:
            self.client.close_position(symbol)
        except Exception:
            if orders:  # cancelled a pending buy that never filled: nothing to close
                log.info("%s: no position to close after cancelling its orders", symbol)
            else:
                raise
        self.levels.pop(symbol, None)


def _ny():
    from zoneinfo import ZoneInfo

    return ZoneInfo(NY)

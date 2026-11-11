"""Live / paper trading loop on Alpaca.

Each time a bar closes, the trader downloads recent bars for every symbol, runs
the same strategy code as the backtester, and acts on the last closed bar:
- ENTRY with no position: buy with a bracket order (stop loss + take profit).
- EXIT with a position: cancel the bracket legs and sell at market.
It closes everything at FLATTEN_TIME and stops opening trades for the day once
the account is down MAX_DAILY_LOSS_PCT from the previous close.
"""

from __future__ import annotations

import logging
import time as time_mod
from datetime import datetime, timedelta, timezone

import pandas as pd

from .config import Settings
from .data import fetch_alpaca_bars
from .risk import plan_entry
from .strategy import ENTRY, EXIT, NY, EmaCrossVwap

log = logging.getLogger("bot.live")


class LiveTrader:
    def __init__(self, settings: Settings):
        from alpaca.trading.client import TradingClient

        settings.require_credentials()
        self.s = settings
        self.client = TradingClient(settings.api_key, settings.api_secret, paper=settings.paper)
        self.strategy = EmaCrossVwap(settings)
        self.last_bar_seen: dict[str, pd.Timestamp] = {}
        self.halted_date = None
        self.flattened_date = None

    # ---- main loop -------------------------------------------------------

    def run(self) -> None:
        mode = "PAPER" if self.s.paper else "LIVE (REAL MONEY)"
        account = self.client.get_account()
        log.info("Starting %s trading on %s, equity $%s", mode, ", ".join(self.s.symbols), account.equity)

        while True:
            try:
                self.tick()
            except KeyboardInterrupt:
                raise
            except Exception:
                log.exception("Error during tick; will retry next bar")
            self._sleep_until_next_bar()

    def tick(self) -> None:
        clock = self.client.get_clock()
        if not clock.is_open:
            wait = (clock.next_open - clock.timestamp).total_seconds()
            log.info("Market closed. Next open %s", clock.next_open.astimezone(_ny()))
            time_mod.sleep(max(0, min(wait, 15 * 60)))
            return

        now = datetime.now(timezone.utc)
        now_et = now.astimezone(_ny())
        today = now_et.date()

        if now_et.time() >= self.s.flatten_time:
            if self.flattened_date != today:
                log.info("Flatten time reached, closing all positions")
                self.client.close_all_positions(cancel_orders=True)
                self.flattened_date = today
            return

        account = self.client.get_account()
        equity = float(account.equity)
        last_equity = float(account.last_equity)
        if self.halted_date != today and equity <= last_equity * (1 - self.s.max_daily_loss_pct):
            log.warning("Daily loss limit hit (equity %.2f vs %.2f). Flattening and halting for today.", equity, last_equity)
            self.client.close_all_positions(cancel_orders=True)
            self.halted_date = today

        positions = {p.symbol: p for p in self.client.get_all_positions()}
        open_orders = self._open_orders()

        for symbol in self.s.symbols:
            try:
                self._process_symbol(symbol, now, equity, positions.get(symbol), open_orders.get(symbol, []))
            except Exception:
                log.exception("Failed to process %s", symbol)

    # ---- per-symbol logic -----------------------------------------------

    def _process_symbol(self, symbol, now, equity, position, orders) -> None:
        bars = fetch_alpaca_bars(self.s, symbol, start=now - timedelta(days=7))
        bar_len = timedelta(minutes=self.s.timeframe_minutes)
        bars = bars[bars.index + bar_len <= now]  # closed bars only
        if len(bars) < self.strategy.warmup_bars:
            log.info("%s: not enough bars yet (%d)", symbol, len(bars))
            return

        last_ts = bars.index[-1]
        if self.last_bar_seen.get(symbol) == last_ts:
            return
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

        if position is not None and row.signal == EXIT:
            log.info("%s: exit signal, closing %s shares", symbol, position.qty)
            self._close_symbol(symbol, orders)
            return

        today = now.astimezone(_ny()).date()
        if position is None and not orders and row.signal == ENTRY:
            if self.halted_date == today:
                log.info("%s: entry signal ignored, daily loss limit active", symbol)
                return
            plan = plan_entry(self.s, equity, float(row.close), float(row.atr))
            if plan is None:
                log.info("%s: entry signal but position size rounds to zero", symbol)
                return
            self._submit_bracket(symbol, plan)

    # ---- order helpers ---------------------------------------------------

    def _submit_bracket(self, symbol, plan) -> None:
        from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest, StopLossRequest, TakeProfitRequest

        order = self.client.submit_order(
            MarketOrderRequest(
                symbol=symbol,
                qty=plan.qty,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
                order_class=OrderClass.BRACKET,
                take_profit=TakeProfitRequest(limit_price=plan.target_price),
                stop_loss=StopLossRequest(stop_price=plan.stop_price),
            )
        )
        log.info(
            "%s: BUY %d (bracket stop %.2f / target %.2f), order %s",
            symbol, plan.qty, plan.stop_price, plan.target_price, order.id,
        )

    def _open_orders(self) -> dict[str, list]:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        orders = self.client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, nested=True))
        by_symbol: dict[str, list] = {}
        for o in orders:
            by_symbol.setdefault(o.symbol, []).append(o)
        return by_symbol

    def _close_symbol(self, symbol, orders) -> None:
        # Bracket legs reserve the shares, so cancel them before selling.
        for o in orders:
            for oid in [o.id] + [leg.id for leg in (o.legs or [])]:
                try:
                    self.client.cancel_order_by_id(oid)
                except Exception:
                    pass  # already filled or cancelled
        time_mod.sleep(1)
        self.client.close_position(symbol)

    def _sleep_until_next_bar(self) -> None:
        """Sleep until a few seconds after the next bar boundary."""
        step = self.s.timeframe_minutes * 60
        now = time_mod.time()
        next_boundary = (now // step + 1) * step
        time_mod.sleep(max(1.0, next_boundary - now + 5))


def _ny():
    from zoneinfo import ZoneInfo

    return ZoneInfo(NY)

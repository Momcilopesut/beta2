"""v3.1 live / paper trading loop on Alpaca.

Every 15 seconds:
- Manages the open position (shield, scale-out, stop, time stop) at the
  current price. This runs even after the daily loss limit trips, so a losing
  position always keeps its stop.
- When a new 5-minute bar has closed, downloads today's QQQ / TQQQ / SQQQ bars
  (free IEX feed), updates the regime and checks the three strategies with the
  same code as the backtester. A signal on the newest bar opens a position.
- Closes its position at FLATTEN_TIME.
After the close it journals the day and runs the daily learning step.

Safety:
- Any error is logged and the loop carries on; one failed request never stops
  the bot or abandons a position.
- Entry and exit prices are the real fill prices from Alpaca.
- The open position is saved to state/v3_position.json, and the bot also
  adopts any TQQQ/SQQQ position it finds, so a restart keeps managing it.
- It only ever closes TQQQ and SQQQ. Still, run one bot per Alpaca account:
  the daily loss limit and day-trade count are shared by the whole account.
"""

from __future__ import annotations

import json
import logging
import os
import time as time_mod
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pandas as pd

from ..config import Settings
from ..data import fetch_alpaca_bars, fetch_history
from ..learning import LearningState, run_daily_learning
from ..strategy import NY
from . import DATA_SYMBOLS, QQQ, TRADED_SYMBOLS
from .position import V3Position, size_entry
from .report import DEFAULT_LOG, append_trades
from .rules import SESSION_OPEN, SessionScanner, bar_end_et, prepare_session

log = logging.getLogger("bot.v3")

POLL_SECONDS = 15
BAR_SECONDS = 300


def _ny():
    from zoneinfo import ZoneInfo

    return ZoneInfo(NY)


class V3LiveTrader:
    def __init__(self, settings: Settings, client=None):
        settings.require_credentials()
        if client is None:
            from alpaca.trading.client import TradingClient

            client = TradingClient(settings.api_key, settings.api_secret, paper=settings.paper)
        self.client = client
        self.base = settings
        self.state = LearningState(settings.state_path)
        self.position_path = os.path.join(os.path.dirname(settings.state_path) or ".", "v3_position.json")
        self.trade_log = DEFAULT_LOG
        self._load_params()
        self.position: V3Position | None = self._load_position()
        self.scanner: SessionScanner | None = None
        self.session_day = None
        self.last_bar: pd.Timestamp | None = None
        self.last_exit: datetime | None = None
        self.regime_logged = False
        self.halted_date = None
        self.traded_date = None
        self.learned_date = None
        self.flattened_date = None
        self.pdt_warned_date = None

    def _load_params(self) -> None:
        self.s = self.state.apply(self.base) if self.base.learning_enabled else self.base
        log.info(
            "v3.1 params version %d: stop %.2f%%, shield +%.1fR, scale +%.2fR am / +%.2fR pm, "
            "time stop %d min, band %.2f SD, risk %.3f%%/trade",
            self.state.version if self.base.learning_enabled else 0,
            self.s.v3_stop_pct * 100, self.s.v3_shield_r, self.s.v3_scale_r_am, self.s.v3_scale_r_pm,
            self.s.v3_time_stop_min, self.s.v3_band_sd, self.s.v3_risk_pct * 100,
        )

    # ---- main loop -------------------------------------------------------

    def run(self) -> None:
        mode = "PAPER" if self.s.paper else "LIVE (REAL MONEY)"
        account = self.client.get_account()
        equity = float(account.equity)
        log.info("Starting v3.1 %s trading (QQQ regime -> TQQQ / SQQQ), equity $%s", mode, account.equity)
        log.info(
            "1R = $%.2f, daily loss limit -$%.2f, %s shares, day-trade guard %s",
            equity * self.s.v3_risk_pct, equity * self.s.v3_max_daily_loss_pct,
            "fractional" if self.s.use_fractional(equity) else "whole",
            "on" if self.s.pdt_guard and self.s.is_small_account(equity) else "off",
        )
        if self.position:
            log.info("Resuming saved position: %s %s @ %.2f", self.position.symbol, self.position.qty, self.position.entry_price)
        while True:
            try:
                self.tick()
            except KeyboardInterrupt:
                raise
            except Exception:
                log.exception("Error during tick; retrying in %ds", POLL_SECONDS)
            time_mod.sleep(POLL_SECONDS)

    def tick(self, now: datetime | None = None) -> None:
        clock = self.client.get_clock()
        if not clock.is_open:
            self._end_of_day()
            wait = (clock.next_open - clock.timestamp).total_seconds()
            log.info("Market closed. Next open %s", clock.next_open.astimezone(_ny()))
            time_mod.sleep(max(0, min(wait, 15 * 60)))
            return

        now = now or datetime.now(timezone.utc)
        now_et = now.astimezone(_ny())
        today = now_et.date()
        self.traded_date = today
        if self.session_day != today:
            self.session_day = today
            self.scanner = SessionScanner(self.s)
            self.last_bar = None
            self.last_exit = None
            self.regime_logged = False

        account = self.client.get_account()
        equity, last_equity = float(account.equity), float(account.last_equity)
        if self.halted_date != today and equity <= last_equity * (1 - self.s.v3_max_daily_loss_pct):
            self.halted_date = today
            log.warning(
                "Daily loss limit hit (equity %.2f vs %.2f): no new trades today. "
                "An open position keeps its stop and is still managed.", equity, last_equity,
            )

        held = {p.symbol: p for p in self.client.get_all_positions() if p.symbol in TRADED_SYMBOLS}
        self._sync_position(held, now_et)

        if now_et.time() >= self.s.flatten_time:
            if self.flattened_date != today:
                # If a close fails it raises, flattened_date stays unset and
                # the next tick tries again.
                if self.position:
                    closed = self.position.symbol
                    self._exit(now_et, float(held[closed].current_price), "EOD")
                    held.pop(closed, None)
                for symbol in held:
                    log.info("Closing leftover %s position", symbol)
                    self._safe_close(symbol)
                self.flattened_date = today
            return

        if self.position:
            price = float(held[self.position.symbol].current_price)
            self._handle(self.position.on_price(price, now_et, self.s, fill_at_level=False), now_et, price)

        self._scan(now, now_et, account, equity, today)

    # ---- position bookkeeping ----------------------------------------------

    def _sync_position(self, held: dict, now_et: datetime) -> None:
        if self.position is not None:
            alpaca = held.get(self.position.symbol)
            if alpaca is None:
                log.warning(
                    "%s position is no longer at Alpaca (closed outside this bot?). Forgetting it.",
                    self.position.symbol,
                )
                self.position = None
                self._save_position()
                return
            self.position.qty = float(alpaca.qty)
        elif held:
            symbol, alpaca = next(iter(held.items()))
            self.position = V3Position.open(
                self.s, "ADOPTED", symbol, float(alpaca.avg_entry_price), float(alpaca.qty), now_et
            )
            log.warning(
                "Found an existing %s position (%s @ %s) and will manage it: stop %.2f.",
                symbol, alpaca.qty, alpaca.avg_entry_price, self.position.stop_price,
            )
            self._save_position()

    def _load_position(self) -> V3Position | None:
        try:
            with open(self.position_path) as f:
                return V3Position.from_dict(json.load(f))
        except FileNotFoundError:
            return None
        except Exception:
            log.exception("Could not read %s; ignoring it", self.position_path)
            return None

    def _save_position(self) -> None:
        if self.position is None:
            if os.path.exists(self.position_path):
                os.remove(self.position_path)
            return
        os.makedirs(os.path.dirname(self.position_path) or ".", exist_ok=True)
        with open(self.position_path, "w") as f:
            json.dump(self.position.to_dict(), f, indent=2)

    # ---- trade management ----------------------------------------------------

    def _handle(self, events, now_et: datetime, price: float) -> None:
        for ev in events:
            pos = self.position
            if ev.kind == "shield":
                log.info("%s: SHIELD at %.2f - stop moved to break-even %.2f", pos.symbol, price, pos.stop_price)
                self._save_position()
            elif ev.kind == "scale":
                order = self._submit(pos.symbol, ev.qty, "sell")
                fill, filled = self._wait_fill(order, price, ev.qty)
                pos.fill_scale(filled, fill)
                log.info("%s: SCALE OUT %s shares at %.2f (%s), stop now %.2f", pos.symbol, filled, fill, ev.reason, pos.stop_price)
                self._save_position()
            elif ev.kind == "exit":
                self._exit(now_et, price, ev.reason)
                return

    def _exit(self, now_et: datetime, price: float, reason: str) -> None:
        pos = self.position
        order = self._safe_close(pos.symbol)
        fill, _ = self._wait_fill(order, price, pos.qty)
        trade = pos.finish(fill, now_et, reason)
        append_trades([trade], self.trade_log)
        log.info(
            "%s: EXIT %s (%s) avg exit %.2f | P&L $%+.2f (%+.2fR) | worst %+.2fR best %+.2fR",
            pos.symbol, trade.qty, reason, trade.exit_price, trade.pnl, trade.realized_r, trade.mae_r, trade.mfe_r,
        )
        self.position = None
        self.last_exit = now_et
        self._save_position()

    # ---- scanning and entries --------------------------------------------------

    def _scan(self, now: datetime, now_et: datetime, account, equity: float, today) -> None:
        latest_closed = pd.Timestamp(
            datetime.fromtimestamp((now.timestamp() // BAR_SECONDS) * BAR_SECONDS - BAR_SECONDS, timezone.utc)
        )
        if self.last_bar is not None and self.last_bar >= latest_closed:
            return

        s5 = replace(self.s, timeframe_minutes=5)
        start = now_et.replace(hour=SESSION_OPEN.hour, minute=SESSION_OPEN.minute, second=0, microsecond=0)
        frames = {}
        for symbol in DATA_SYMBOLS:
            bars = fetch_alpaca_bars(s5, symbol, start)
            bars = bars[(bars.index + timedelta(minutes=5) <= now) & (bars.index.tz_convert(NY).date == today)]
            frames[symbol] = prepare_session(bars, self.s.v3_band_sd) if len(bars) else bars
        qqq = frames[QQQ]
        if qqq.empty:
            return

        new_bars = qqq.index if self.last_bar is None else qqq.index[qqq.index > self.last_bar]
        if len(new_bars) == 0:
            return  # newest bar not published yet; try again next tick
        signal = None
        for ts in new_bars:
            signal = self.scanner.on_bar_close(ts, frames)
        self.last_bar = qqq.index[-1]

        if self.scanner.regime and not self.regime_logged:
            pct, slope = self.scanner.regime_info
            log.info("REGIME: %s (QQQ %+.2f%% from open, VWAP slope %+.2f%%/h)", self.scanner.regime, pct, slope)
            self.regime_logged = True
        last = qqq.iloc[-1]
        log.info(
            "%s QQQ %.2f | regime %s | %s",
            bar_end_et(self.last_bar).strftime("%H:%M"), last.close, self.scanner.regime or "pending (10:00)",
            f"holding {self.position.symbol} {self.position.qty} (stop {self.position.stop_price:.2f})" if self.position else "flat",
        )

        if signal is None or signal.bar != self.last_bar or self.position is not None:
            return
        why_not = self._entry_blocker(signal, now, now_et, account, equity, today)
        if why_not:
            log.info("%s signal on %s ignored: %s", signal.strategy, signal.symbol, why_not)
            return
        self._enter(signal, now_et, account, equity)

    def _entry_blocker(self, signal, now, now_et, account, equity, today) -> str:
        if self.halted_date == today:
            return "daily loss limit"
        if self.s.pdt_guard and self.s.is_small_account(equity):
            day_trades = int(getattr(account, "daytrade_count", 0) or 0)
            if day_trades >= 3:
                return f"day-trade guard ({day_trades} day trades in 5 days)"
        if self.last_exit and now_et - self.last_exit < timedelta(minutes=self.s.v3_cooldown_min):
            return f"cooldown after the last exit ({self.s.v3_cooldown_min} min)"
        bar_end = bar_end_et(signal.bar)
        if bar_end.time() >= self.s.v3_entry_cutoff:
            return "past the entry cutoff"
        if (now - bar_end).total_seconds() > 120:
            return "signal is stale (bar data arrived late)"
        return ""

    def _enter(self, signal, now_et, account, equity) -> None:
        fractional = self.s.use_fractional(equity)
        qty = size_entry(self.s, equity, signal.price, fractional, buying_power=float(account.buying_power))
        if qty <= 0:
            log.info("%s signal on %s: position size too small to trade", signal.strategy, signal.symbol)
            return
        order = self._submit(signal.symbol, qty, "buy")
        fill, filled = self._wait_fill(order, None, qty, timeout=20)
        if fill is None or filled <= 0:
            log.warning("%s buy order did not fill in time; cancelling it", signal.symbol)
            try:
                self.client.cancel_order_by_id(order.id)
            except Exception:
                log.exception("Cancel failed")
            return
        self.position = V3Position.open(self.s, signal.strategy, signal.symbol, fill, filled, now_et, signal.target)
        self._save_position()
        log.info(
            "%s: BUY %s %s at %.2f | stop %.2f | 1R $%.2f%s",
            signal.strategy, filled, signal.symbol, fill, self.position.stop_price,
            filled * self.position.risk_per_share,
            f" | VWAP target {signal.target:.2f}" if signal.target else "",
        )

    # ---- order helpers ---------------------------------------------------------

    def _submit(self, symbol: str, qty: float, side: str):
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        return self.client.submit_order(
            MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
                time_in_force=TimeInForce.DAY,  # fractional orders must be DAY orders
            )
        )

    def _safe_close(self, symbol: str):
        try:
            return self.client.close_position(symbol)
        except Exception:
            log.exception("Closing %s failed; will retry next tick", symbol)
            raise

    def _wait_fill(self, order, fallback_price, fallback_qty, timeout: float = 10) -> tuple[float | None, float]:
        """Wait for an order to fill; return (average fill price, filled qty)."""
        deadline = time_mod.time() + timeout
        order_id = getattr(order, "id", None)
        while order_id is not None:
            try:
                o = self.client.get_order_by_id(order_id)
                status = str(getattr(o.status, "value", o.status)).lower()
                if status == "filled" and getattr(o, "filled_avg_price", None):
                    return float(o.filled_avg_price), float(o.filled_qty)
            except Exception:
                log.exception("Could not check order %s", order_id)
            if time_mod.time() >= deadline:
                break
            time_mod.sleep(1)
        return fallback_price, fallback_qty if fallback_price is not None else 0.0

    # ---- end of day ------------------------------------------------------------

    def _end_of_day(self) -> None:
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
        except Exception:
            log.exception("Daily learning failed; keeping current parameters")

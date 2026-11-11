"""v3.1 position sizing and trade management (shield, scale-out, stops, time stop)."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import datetime

from ..config import Settings
from .rules import NOON

FRACTION_DECIMALS = 4
MIN_NOTIONAL = 1.0


def _floor(value: float, decimals: int) -> float:
    factor = 10**decimals
    return math.floor(value * factor + 1e-9) / factor


def size_entry(
    s: Settings, equity: float, price: float, fractional: bool,
    buying_power: float | None = None, risk_multiplier: float = 1.0,
) -> float:
    """Shares to buy so that the initial stop loses 1R. Returns 0 if too small."""
    if equity <= 0 or price <= 0:
        return 0.0
    one_r = equity * s.v3_risk_pct * risk_multiplier
    stop_dist = price * s.v3_stop_pct
    max_dollars = equity * s.v3_max_position_pct
    if buying_power is not None:
        max_dollars = min(max_dollars, 0.98 * buying_power)
    raw = min(one_r / stop_dist, max_dollars / price)
    if fractional:
        qty = _floor(raw, FRACTION_DECIMALS)
        return qty if qty * price >= MIN_NOTIONAL else 0.0
    return float(math.floor(raw))


@dataclass
class Event:
    kind: str  # "shield", "scale" or "exit"
    price: float  # price used for the fill (level price in backtests)
    qty: float = 0.0
    reason: str = ""


@dataclass
class V3Trade:
    strategy: str
    symbol: str
    entry_time: datetime
    exit_time: datetime
    entry_price: float
    exit_price: float  # average over the scale-out and the final exit
    qty: float
    pnl: float
    realized_r: float
    mae_r: float
    mfe_r: float
    exit_reason: str
    scaled: bool


@dataclass
class V3Position:
    strategy: str
    symbol: str
    entry_price: float
    qty: float
    entry_time: datetime
    stop_price: float = 0.0
    initial_stop: float = 0.0  # defines 1R = entry - initial_stop
    target_price: float | None = None
    initial_qty: float = 0.0
    shield: bool = False
    scaled: bool = False
    high: float = 0.0
    low: float = 0.0
    realized: float = 0.0  # proceeds from shares already sold
    sold_qty: float = 0.0

    @classmethod
    def open(cls, s: Settings, strategy, symbol, entry_price, qty, entry_time, target=None) -> "V3Position":
        pos = cls(strategy, symbol, entry_price, qty, entry_time, target_price=target)
        pos.initial_qty = qty
        pos.stop_price = pos.initial_stop = round(entry_price * (1 - s.v3_stop_pct), 4)
        pos.high = pos.low = entry_price
        return pos

    @property
    def risk_per_share(self) -> float:
        return self.entry_price - self.initial_stop

    def r(self, price: float) -> float:
        return (price - self.entry_price) / self.risk_per_share

    def on_price(self, price: float, now: datetime, s: Settings, fill_at_level: bool) -> list[Event]:
        """Apply the management rules to a new price.

        `now` must be US/Eastern. With `fill_at_level` (backtests) fills happen at
        the level that was crossed; otherwise (live) at `price`.
        """
        risk = self.risk_per_share
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        events: list[Event] = []
        unrealized_r = self.r(price)
        afternoon = now.time() >= NOON

        if not self.shield and unrealized_r >= s.v3_shield_r:
            self.shield = True
            self.stop_price = max(self.stop_price, self.entry_price)
            events.append(Event("shield", price))

        scale_r = s.v3_scale_r_pm if afternoon else s.v3_scale_r_am
        if not self.scaled and unrealized_r >= scale_r:
            whole = float(self.qty).is_integer()
            half = float(math.floor(self.qty / 2)) if whole else _floor(self.qty / 2, FRACTION_DECIMALS)
            self.scaled = True
            if half > 0:
                level = self.entry_price + scale_r * risk
                events.append(Event("scale", level if fill_at_level else price, half, f"+{scale_r:.2f}R"))
            if afternoon:
                self.stop_price = max(self.stop_price, round(self.entry_price + s.v3_pm_lock_r * risk, 4))

        if self.target_price is not None and price >= self.target_price:
            events.append(Event("exit", self.target_price if fill_at_level else price, reason="VWAP_TARGET"))
            return events

        if price <= self.stop_price:
            reason = "STOP" if not self.shield else ("PROFIT_LOCK" if self.stop_price > self.entry_price else "SHIELD")
            events.append(Event("exit", self.stop_price if fill_at_level else price, reason=reason))
            return events

        held_min = (now - self.entry_time).total_seconds() / 60
        if held_min >= s.v3_time_stop_min and unrealized_r < s.v3_time_stop_r and not self.scaled:
            events.append(Event("exit", price, reason="TIME_STOP"))
        return events

    def fill_scale(self, qty: float, price: float) -> None:
        qty = min(qty, self.qty)
        self.qty = round(self.qty - qty, 6)
        self.sold_qty += qty
        self.realized += qty * price

    def finish(self, price: float, when: datetime, reason: str) -> V3Trade:
        proceeds = self.realized + self.qty * price
        total_qty = self.sold_qty + self.qty
        avg_exit = proceeds / total_qty if total_qty else price
        pnl = proceeds - total_qty * self.entry_price
        risk = self.risk_per_share
        return V3Trade(
            strategy=self.strategy,
            symbol=self.symbol,
            entry_time=self.entry_time,
            exit_time=when,
            entry_price=round(self.entry_price, 4),
            exit_price=round(avg_exit, 4),
            qty=round(total_qty, 6),
            pnl=round(pnl, 4),
            realized_r=round(pnl / (total_qty * risk), 3) if total_qty and risk else 0.0,
            mae_r=round(min(0.0, (self.low - self.entry_price) / risk), 3) if risk else 0.0,
            mfe_r=round(max(0.0, (self.high - self.entry_price) / risk), 3) if risk else 0.0,
            exit_reason=reason,
            scaled=self.scaled,
        )

    def to_dict(self) -> dict:
        d = asdict(self)
        d["entry_time"] = self.entry_time.isoformat()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "V3Position":
        d = dict(d)
        d["entry_time"] = datetime.fromisoformat(d["entry_time"])
        return cls(**d)

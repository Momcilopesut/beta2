"""Position sizing and protective price levels."""

from __future__ import annotations

import math
from dataclasses import dataclass

from .config import Settings


@dataclass
class OrderPlan:
    qty: int
    stop_price: float
    target_price: float


def plan_entry(settings: Settings, equity: float, price: float, atr_value: float) -> OrderPlan | None:
    """Size a long entry so that hitting the stop loses about `risk_per_trade` of equity.

    The size is also capped at `max_position_pct` of equity. Returns None when
    the trade is too small to take (zero shares) or the inputs are unusable.
    """
    if equity <= 0 or price <= 0 or not atr_value or atr_value <= 0 or math.isnan(atr_value):
        return None

    stop_distance = settings.stop_atr * atr_value
    risk_dollars = equity * settings.risk_per_trade
    qty_by_risk = math.floor(risk_dollars / stop_distance)
    qty_by_cap = math.floor(equity * settings.max_position_pct / price)
    qty = min(qty_by_risk, qty_by_cap)
    if qty < 1:
        return None

    stop_price = round(price - stop_distance, 2)
    target_price = round(price + settings.target_atr * atr_value, 2)
    if stop_price <= 0:
        return None
    return OrderPlan(qty=qty, stop_price=stop_price, target_price=target_price)

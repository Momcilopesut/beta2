"""Position sizing and protective price levels."""

from __future__ import annotations

import math
from dataclasses import dataclass

from .config import Settings


FRACTION_DECIMALS = 4  # Alpaca accepts up to 9; 4 is plenty
MIN_NOTIONAL = 1.0  # Alpaca's minimum fractional order value, in dollars


@dataclass
class OrderPlan:
    qty: float
    stop_price: float
    target_price: float


def _floor(value: float, decimals: int) -> float:
    factor = 10**decimals
    return math.floor(value * factor) / factor


def plan_entry(
    settings: Settings,
    equity: float,
    price: float,
    atr_value: float,
    fractional: bool = False,
    buying_power: float | None = None,
) -> OrderPlan | None:
    """Size a long entry so that hitting the stop loses about `risk_per_trade` of equity.

    The size is also capped at the position limit (`max_position_pct`, or
    `small_account_max_position_pct` for small accounts) and, when given, at
    98% of `buying_power`. With `fractional`
    the quantity may be a fraction of a share (for small accounts). Returns None
    when the trade is too small to take or the inputs are unusable.
    """
    if equity <= 0 or price <= 0 or not atr_value or atr_value <= 0 or math.isnan(atr_value):
        return None

    stop_distance = settings.stop_atr * atr_value
    risk_dollars = equity * settings.risk_per_trade
    max_dollars = equity * settings.position_cap_pct(equity)
    if buying_power is not None:
        max_dollars = min(max_dollars, 0.98 * buying_power)
    raw_qty = min(risk_dollars / stop_distance, max_dollars / price)
    if fractional:
        qty = _floor(raw_qty, FRACTION_DECIMALS)
        if qty * price < MIN_NOTIONAL:
            return None
    else:
        qty = math.floor(raw_qty)
        if qty < 1:
            return None

    stop_price = round(price - stop_distance, 2)
    target_price = round(price + settings.target_atr * atr_value, 2)
    if stop_price <= 0:
        return None
    return OrderPlan(qty=qty, stop_price=stop_price, target_price=target_price)

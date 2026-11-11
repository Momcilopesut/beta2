"""Settings loaded from environment variables (and an optional .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import time

from dotenv import load_dotenv


def _env(name: str, default: str) -> str:
    return os.getenv(name, default).strip()


def _parse_time(value: str) -> time:
    hours, minutes = value.split(":")
    return time(int(hours), int(minutes))


@dataclass
class Settings:
    # Credentials
    api_key: str | None = None
    api_secret: str | None = None
    paper: bool = True

    # Universe and bar size
    symbols: list[str] = field(default_factory=lambda: ["SPY"])
    timeframe_minutes: int = 5

    # Strategy parameters
    ema_fast: int = 9
    ema_slow: int = 21
    atr_period: int = 14
    stop_atr: float = 1.5  # stop loss distance, in ATRs below entry
    target_atr: float = 3.0  # take profit distance, in ATRs above entry

    # Risk management
    risk_per_trade: float = 0.01  # fraction of equity lost if the stop is hit
    max_position_pct: float = 0.25  # max fraction of equity in a single position
    max_daily_loss_pct: float = 0.03  # stop opening trades after losing this much in a day
    slippage_bps: float = 2.0  # backtest only: cost per fill, in basis points

    # Session rules (US/Eastern)
    entry_start: time = time(9, 45)  # skip the noisy open
    entry_end: time = time(15, 30)  # no new trades late in the day
    flatten_time: time = time(15, 50)  # close everything before the bell

    # Daily self-improvement (see bot/learning.py)
    learning_enabled: bool = True
    learn_train_days: int = 30  # sessions used to search for better parameters
    learn_val_days: int = 10  # most recent sessions held out to confirm a change
    learn_candidates: int = 24  # random variations tried per day (plus +/- 1 step neighbours)
    learn_margin: float = 1.0  # required Sharpe improvement on BOTH training and held-out days
    learn_cooldown_days: int = 5  # minimum sessions between parameter changes
    learn_min_trades: int = 10  # minimum trades in the training window to trust a score
    state_path: str = "state/learning.json"

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        return cls(
            api_key=os.getenv("ALPACA_API_KEY") or None,
            api_secret=os.getenv("ALPACA_API_SECRET") or None,
            paper=_env("ALPACA_PAPER", "true").lower() not in ("false", "0", "no"),
            symbols=[s.strip().upper() for s in _env("SYMBOLS", "SPY").split(",") if s.strip()],
            timeframe_minutes=int(_env("TIMEFRAME_MINUTES", "5")),
            ema_fast=int(_env("EMA_FAST", "9")),
            ema_slow=int(_env("EMA_SLOW", "21")),
            atr_period=int(_env("ATR_PERIOD", "14")),
            stop_atr=float(_env("STOP_ATR", "1.5")),
            target_atr=float(_env("TARGET_ATR", "3.0")),
            risk_per_trade=float(_env("RISK_PER_TRADE", "0.01")),
            max_position_pct=float(_env("MAX_POSITION_PCT", "0.25")),
            max_daily_loss_pct=float(_env("MAX_DAILY_LOSS_PCT", "0.03")),
            slippage_bps=float(_env("SLIPPAGE_BPS", "2")),
            entry_start=_parse_time(_env("ENTRY_START", "09:45")),
            entry_end=_parse_time(_env("ENTRY_END", "15:30")),
            flatten_time=_parse_time(_env("FLATTEN_TIME", "15:50")),
            learning_enabled=_env("LEARNING_ENABLED", "true").lower() not in ("false", "0", "no"),
            learn_train_days=int(_env("LEARN_TRAIN_DAYS", "30")),
            learn_val_days=int(_env("LEARN_VAL_DAYS", "10")),
            learn_candidates=int(_env("LEARN_CANDIDATES", "24")),
            learn_margin=float(_env("LEARN_MARGIN", "1.0")),
            learn_cooldown_days=int(_env("LEARN_COOLDOWN_DAYS", "5")),
            learn_min_trades=int(_env("LEARN_MIN_TRADES", "10")),
            state_path=_env("STATE_PATH", "state/learning.json"),
        )

    def require_credentials(self) -> None:
        if not self.api_key or not self.api_secret:
            raise SystemExit(
                "Missing Alpaca credentials. Copy .env.example to .env and set "
                "ALPACA_API_KEY and ALPACA_API_SECRET."
            )

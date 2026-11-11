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
    # "auto" = fractional shares when equity is under SMALL_ACCOUNT_LIMIT, "true"/"false" to force
    fractional_shares: str = "auto"
    small_account_limit: float = 25_000.0  # US pattern-day-trader threshold
    small_account_max_position_pct: float = 0.5  # position cap used below that limit
    pdt_guard: bool = True  # under the limit, never open a 4th day trade in 5 sessions

    # Session rules (US/Eastern)
    entry_start: time = time(9, 45)  # skip the noisy open
    entry_end: time = time(15, 30)  # no new trades late in the day
    flatten_time: time = time(15, 50)  # close everything before the bell

    # Daily self-improvement (see bot/learning.py)
    learning_enabled: bool = True
    learn_train_days: int = 30  # sessions used to search for better parameters
    learn_val_days: int = 10  # most recent sessions held out to confirm a change
    learn_candidates: int = 24  # random variations tried per day (plus +/- 1 step neighbours)
    learn_margin: float = 2.0  # required Sharpe improvement on BOTH training and held-out days
    learn_cooldown_days: int = 10  # minimum sessions between parameter changes
    learn_min_trades: int = 10  # minimum trades in the training window to trust a score
    state_path: str = ""  # empty = state/learning.json or state/learning_v3.json

    # Which strategy to run: "ema_cross_vwap" (the original) or "v3" (bot/v3)
    strategy: str = "ema_cross_vwap"

    # v3.1 multi-strategy system (see bot/v3/__init__.py)
    v3_risk_pct: float = 0.005  # 1R = 0.5% of equity
    v3_max_daily_loss_pct: float = 0.01  # -2R: no new trades for the rest of the day
    v3_max_position_pct: float = 0.5  # never more than half the account in one position
    v3_stop_pct: float = 0.008  # initial stop 0.8% below entry (= 1R)
    v3_shield_r: float = 0.6  # move the stop to break-even at +0.6R
    v3_scale_r_am: float = 1.0  # sell half at +1.0R before noon...
    v3_scale_r_pm: float = 0.7  # ...or +0.7R after noon
    v3_pm_lock_r: float = 0.25  # after an afternoon scale, lock the stop at +0.25R
    v3_time_stop_min: int = 30  # exit a stalled trade after 30 minutes...
    v3_time_stop_r: float = 0.4  # ...if it is below +0.4R and has not scaled
    v3_cooldown_min: int = 10  # wait 10 minutes after an exit before a new entry
    v3_orb_qqq_pct: float = 0.35  # ORB needs QQQ to have moved 0.35% from the open
    v3_trend_pct: float = 0.5  # trend day: QQQ moved 0.5% from the open by 10:00...
    v3_trend_slope: float = 0.15  # ...and VWAP slopes at least 0.15%/hour the same way
    v3_band_sd: float = 2.0  # VWAP reversion band, in standard deviations
    v3_vwap_gap_pct: float = 0.6  # ...and price at least 0.6% below VWAP
    v3_exhaustion_pct: float = 1.75  # after noon, skip pullbacks if QQQ already moved this much
    v3_vwap_target: bool = False  # VWAP reversion also exits fully when price reaches VWAP
    v3_entry_cutoff: time = time(15, 45)  # no new v3 entries after this time

    @property
    def is_v3(self) -> bool:
        return self.strategy.lower() in ("v3", "v3.1")

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
            fractional_shares=_env("FRACTIONAL_SHARES", "auto").lower(),
            small_account_limit=float(_env("SMALL_ACCOUNT_LIMIT", "25000")),
            small_account_max_position_pct=float(_env("SMALL_ACCOUNT_MAX_POSITION_PCT", "0.5")),
            pdt_guard=_env("PDT_GUARD", "true").lower() not in ("false", "0", "no"),
            entry_start=_parse_time(_env("ENTRY_START", "09:45")),
            entry_end=_parse_time(_env("ENTRY_END", "15:30")),
            flatten_time=_parse_time(_env("FLATTEN_TIME", "15:50")),
            learning_enabled=_env("LEARNING_ENABLED", "true").lower() not in ("false", "0", "no"),
            learn_train_days=int(_env("LEARN_TRAIN_DAYS", "30")),
            learn_val_days=int(_env("LEARN_VAL_DAYS", "10")),
            learn_candidates=int(_env("LEARN_CANDIDATES", "24")),
            learn_margin=float(_env("LEARN_MARGIN", "2.0")),
            learn_cooldown_days=int(_env("LEARN_COOLDOWN_DAYS", "10")),
            learn_min_trades=int(_env("LEARN_MIN_TRADES", "10")),
            strategy=_env("STRATEGY", "ema_cross_vwap").lower(),
            state_path=_env("STATE_PATH", ""),
            v3_risk_pct=float(_env("V3_RISK_PCT", "0.005")),
            v3_max_daily_loss_pct=float(_env("V3_MAX_DAILY_LOSS_PCT", "0.01")),
            v3_max_position_pct=float(_env("V3_MAX_POSITION_PCT", "0.5")),
            v3_stop_pct=float(_env("V3_STOP_PCT", "0.008")),
            v3_shield_r=float(_env("V3_SHIELD_R", "0.6")),
            v3_scale_r_am=float(_env("V3_SCALE_R_AM", "1.0")),
            v3_scale_r_pm=float(_env("V3_SCALE_R_PM", "0.7")),
            v3_pm_lock_r=float(_env("V3_PM_LOCK_R", "0.25")),
            v3_time_stop_min=int(_env("V3_TIME_STOP_MIN", "30")),
            v3_time_stop_r=float(_env("V3_TIME_STOP_R", "0.4")),
            v3_cooldown_min=int(_env("V3_COOLDOWN_MIN", "10")),
            v3_orb_qqq_pct=float(_env("V3_ORB_QQQ_PCT", "0.35")),
            v3_trend_pct=float(_env("V3_TREND_PCT", "0.5")),
            v3_trend_slope=float(_env("V3_TREND_SLOPE", "0.15")),
            v3_band_sd=float(_env("V3_BAND_SD", "2.0")),
            v3_vwap_gap_pct=float(_env("V3_VWAP_GAP_PCT", "0.6")),
            v3_exhaustion_pct=float(_env("V3_EXHAUSTION_PCT", "1.75")),
            v3_vwap_target=_env("V3_VWAP_TARGET", "false").lower() in ("true", "1", "yes"),
            v3_entry_cutoff=_parse_time(_env("V3_ENTRY_CUTOFF", "15:45")),
        )

    def __post_init__(self) -> None:
        # Each strategy keeps its own learning history.
        if not self.state_path:
            self.state_path = "state/learning_v3.json" if self.is_v3 else "state/learning.json"

    def is_small_account(self, equity: float) -> bool:
        return equity < self.small_account_limit

    def position_cap_pct(self, equity: float) -> float:
        return self.small_account_max_position_pct if self.is_small_account(equity) else self.max_position_pct

    def use_fractional(self, equity: float) -> bool:
        if self.fractional_shares in ("true", "1", "yes"):
            return True
        if self.fractional_shares in ("false", "0", "no"):
            return False
        return self.is_small_account(equity)

    def require_credentials(self) -> None:
        if not self.api_key or not self.api_secret:
            raise SystemExit(
                "Missing Alpaca credentials. Copy .env.example to .env and set "
                "ALPACA_API_KEY and ALPACA_API_SECRET."
            )

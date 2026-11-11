"""Daily self-improvement loop.

After every trading day the bot runs `run_daily_learning`, which:

1. Logs the day's live result in a journal.
2. Searches for better strategy parameters near the current ones. It tries
   +/- 1 step on each parameter plus random nearby variations, so parameters
   drift gradually instead of jumping.
3. Picks the best candidate by its score on the *training* window (the
   sessions before the most recent `learn_val_days`).
4. Adopts that candidate only if it beats the current parameters by
   `learn_margin` (Sharpe) on the training window AND on the *held-out
   validation* window (the most recent sessions), is profitable on the
   held-out days, traded enough to be trusted, and the last change was at
   least `learn_cooldown_days` sessions ago. Otherwise nothing changes.
5. Adjusts a risk multiplier from the live journal: it cuts position sizes
   after a losing streak and slowly restores them when results recover.

Each day starts from the previous day's adopted parameters, so improvements
build on each other. Every run is recorded in `state/learning.json` with a
version number, so any change can be inspected or rolled back.

Guardrails against overfitting: the holdout check, the margin requirement on
both windows, minimum trade counts, the cooldown, small steps per day, and hard
bounds on every parameter. Sharpe ratios measured over a few weeks are noisy,
so the bar for changing anything is deliberately high: on random data with no
real pattern the learner should almost always keep its current parameters.
"""

from __future__ import annotations

import json
import math
import os
import random
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timezone

import pandas as pd

from .backtest import run_backtest
from .config import Settings
from .strategy import NY

# ---------------------------------------------------------------------------
# Search space
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParamSpec:
    kind: type
    low: float
    high: float
    step: float

    def clamp(self, value):
        value = min(self.high, max(self.low, value))
        if self.kind is int:
            return int(round(value))
        # snap floats to the step grid to keep values tidy
        return round(round(value / self.step) * self.step, 4)


TUNABLE: dict[str, ParamSpec] = {
    "ema_fast": ParamSpec(int, 3, 20, 1),
    "ema_slow": ParamSpec(int, 10, 60, 2),
    "atr_period": ParamSpec(int, 5, 30, 2),
    "stop_atr": ParamSpec(float, 0.75, 3.0, 0.25),
    "target_atr": ParamSpec(float, 1.0, 6.0, 0.5),
}

RISK_MULT_MIN = 0.25
RISK_MULT_MAX = 1.0


def current_params(settings: Settings) -> dict:
    return {name: getattr(settings, name) for name in TUNABLE}


def is_valid(params: dict) -> bool:
    return params["ema_fast"] < params["ema_slow"] and params["target_atr"] > params["stop_atr"]


def generate_candidates(params: dict, n_random: int, seed: int) -> list[dict]:
    """Neighbours (+/- one step per parameter) plus random nearby variations."""
    seen = {tuple(sorted(params.items()))}
    out: list[dict] = []

    def add(candidate: dict) -> None:
        candidate = {k: TUNABLE[k].clamp(v) for k, v in candidate.items()}
        key = tuple(sorted(candidate.items()))
        if key not in seen and is_valid(candidate):
            seen.add(key)
            out.append(candidate)

    for name, spec in TUNABLE.items():
        for direction in (-1, 1):
            add({**params, name: params[name] + direction * spec.step})

    rng = random.Random(seed)
    for _ in range(n_random * 3):
        if len(out) >= 2 * len(TUNABLE) + n_random:
            break
        candidate = dict(params)
        for name in rng.sample(list(TUNABLE), k=rng.randint(1, len(TUNABLE))):
            spec = TUNABLE[name]
            candidate[name] = params[name] + rng.choice([-2, -1, 1, 2]) * spec.step
        add(candidate)
    return out


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


@dataclass
class WindowScore:
    sharpe: float
    pnl: float
    trades: int
    max_drawdown_pct: float

    def as_dict(self) -> dict:
        return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in asdict(self).items()}


def score_window(daily_pnl: pd.Series, trades: int, equity: float) -> WindowScore:
    if len(daily_pnl) == 0:
        return WindowScore(0.0, 0.0, trades, 0.0)
    std = daily_pnl.std(ddof=1) if len(daily_pnl) > 1 else 0.0
    sharpe = float(daily_pnl.mean() / std * math.sqrt(252)) if std and std > 0 else 0.0
    curve = equity + daily_pnl.cumsum()
    peak = pd.concat([pd.Series([equity]), curve]).cummax().iloc[1:]
    drawdown = float((curve.values / peak.values - 1).min()) * 100
    return WindowScore(sharpe, float(daily_pnl.sum()), trades, drawdown)


def session_dates(bars_by_symbol: dict[str, pd.DataFrame]) -> list[date]:
    dates: set[date] = set()
    for bars in bars_by_symbol.values():
        dates.update(bars.index.tz_convert(NY).date)
    return sorted(dates)


def evaluate(
    settings: Settings,
    bars_by_symbol: dict[str, pd.DataFrame],
    train_dates: list[date],
    val_dates: list[date],
    equity: float = 100_000.0,
) -> tuple[WindowScore, WindowScore]:
    """Backtest `settings` across all symbols; score the train and validation windows."""
    pnl_by_date: dict[date, float] = {}
    trades_by_date: dict[date, int] = {}
    for symbol, bars in bars_by_symbol.items():
        result = run_backtest(bars, settings, symbol=symbol, starting_equity=equity)
        for t in result.trades:
            d = t.exit_time.tz_convert(NY).date()
            pnl_by_date[d] = pnl_by_date.get(d, 0.0) + t.pnl
            trades_by_date[d] = trades_by_date.get(d, 0) + 1

    def window(dates: list[date]) -> WindowScore:
        daily = pd.Series([pnl_by_date.get(d, 0.0) for d in dates], dtype=float)
        return score_window(daily, sum(trades_by_date.get(d, 0) for d in dates), equity * len(bars_by_symbol))

    return window(train_dates), window(val_dates)


# ---------------------------------------------------------------------------
# Persistent state
# ---------------------------------------------------------------------------


class LearningState:
    """Learned parameters, risk multiplier, version history and live journal."""

    def __init__(self, path: str):
        self.path = path
        self.data = {"version": 0, "params": {}, "risk_multiplier": 1.0, "history": [], "journal": []}
        if os.path.exists(path):
            with open(path) as f:
                self.data.update(json.load(f))

    @property
    def version(self) -> int:
        return self.data["version"]

    @property
    def params(self) -> dict:
        return self.data["params"]

    @property
    def risk_multiplier(self) -> float:
        return self.data["risk_multiplier"]

    def apply(self, base: Settings) -> Settings:
        """Return `base` with the learned parameters and risk multiplier applied."""
        learned = {k: v for k, v in self.params.items() if k in TUNABLE}
        return replace(base, **learned, risk_per_trade=base.risk_per_trade * self.risk_multiplier)

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.data, f, indent=2, default=str)
        os.replace(tmp, self.path)

    def record_day(self, day: date, pnl: float, equity: float) -> None:
        journal = [e for e in self.data["journal"] if e["date"] != str(day)]
        journal.append({"date": str(day), "pnl": round(pnl, 2), "equity": round(equity, 2), "params_version": self.version})
        self.data["journal"] = journal[-250:]

    def rollback(self, version: int) -> dict:
        """Restore the parameters that were active at `version` (0 = .env defaults)."""
        if version == 0:
            params = {}
        else:
            match = [h for h in self.data["history"] if h.get("adopted") and h["version"] == version]
            if not match:
                raise ValueError(f"No adopted parameter version {version}")
            params = match[-1]["params"]
        self.data["version"] += 1
        self.data["params"] = params
        self.data["history"].append(
            {
                "version": self.version,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "adopted": True,
                "reason": f"manual rollback to version {version}",
                "params": params,
            }
        )
        return params


# ---------------------------------------------------------------------------
# Risk throttle from live results
# ---------------------------------------------------------------------------


def update_risk_multiplier(state: LearningState, lookback: int = 5) -> tuple[float, str]:
    recent = state.data["journal"][-lookback:]
    mult = state.risk_multiplier
    if len(recent) < 3:
        return mult, "not enough live days to adjust risk"

    streak = 0
    for entry in reversed(recent):
        if entry["pnl"] < 0:
            streak += 1
        else:
            break
    total = sum(e["pnl"] for e in recent)

    if streak >= 3 or total < 0:
        new = max(RISK_MULT_MIN, round(mult * 0.75, 4))
        why = f"cut risk: {streak}-day losing streak, {lookback}-day P&L {total:+.2f}"
    elif total > 0 and streak == 0:
        new = min(RISK_MULT_MAX, round(mult * 1.25, 4))
        why = f"restore risk: {lookback}-day P&L {total:+.2f}"
    else:
        new, why = mult, "risk unchanged"
    state.data["risk_multiplier"] = new
    return new, why


# ---------------------------------------------------------------------------
# The daily learning run
# ---------------------------------------------------------------------------


@dataclass
class LearningReport:
    adopted: bool
    reason: str
    previous_params: dict
    params: dict
    current_train: WindowScore | None = None
    current_val: WindowScore | None = None
    best_train: WindowScore | None = None
    best_val: WindowScore | None = None
    candidates_tested: int = 0
    risk_multiplier: float = 1.0
    risk_note: str = ""
    version: int = 0

    def summary(self) -> str:
        lines = [f"Learning run (params version {self.version}): {'ADOPTED' if self.adopted else 'kept current'} - {self.reason}"]
        if self.current_train is not None:
            lines.append(f"  Candidates tested: {self.candidates_tested}")
            lines.append(f"  Current  train {fmt(self.current_train)} | val {fmt(self.current_val)}")
        if self.best_train is not None:
            lines.append(f"  Best     train {fmt(self.best_train)} | val {fmt(self.best_val)}")
        if self.adopted:
            changes = {k: f"{self.previous_params[k]} -> {v}" for k, v in self.params.items() if self.previous_params.get(k) != v}
            lines.append(f"  Changes: {changes}")
        lines.append(f"  Params: {self.params}")
        lines.append(f"  Risk multiplier: {self.risk_multiplier} ({self.risk_note})")
        return "\n".join(lines)


def fmt(score: WindowScore | None) -> str:
    if score is None:
        return "-"
    return f"sharpe {score.sharpe:+.2f}, pnl {score.pnl:+,.0f}, trades {score.trades}, dd {score.max_drawdown_pct:.1f}%"


def run_daily_learning(
    base: Settings,
    bars_by_symbol: dict[str, pd.DataFrame],
    state: LearningState,
    as_of: date | None = None,
) -> LearningReport:
    """Run one self-improvement step and persist the outcome in `state`."""
    as_of = as_of or datetime.now(timezone.utc).astimezone().date()
    risk_mult, risk_note = update_risk_multiplier(state)

    current = state.apply(base)
    params = current_params(current)
    report = LearningReport(False, "", params, params, risk_multiplier=risk_mult, risk_note=risk_note)

    dates = session_dates(bars_by_symbol)
    needed = base.learn_train_days + base.learn_val_days
    if len(dates) < needed:
        report.reason = f"not enough history ({len(dates)} sessions, need {needed})"
        return _finish(state, report, as_of)

    window = dates[-needed:]
    train_dates, val_dates = window[: base.learn_train_days], window[base.learn_train_days :]
    # Keep a few sessions before the window so indicators are warmed up.
    first = dates[max(0, len(dates) - needed - 3)]
    sliced = {
        s: b[b.index.tz_convert(NY).date >= first] for s, b in bars_by_symbol.items()
    }

    cur_train, cur_val = evaluate(current, sliced, train_dates, val_dates)
    report.current_train, report.current_val = cur_train, cur_val

    seed = int(as_of.strftime("%Y%m%d"))
    candidates = generate_candidates(params, base.learn_candidates, seed)
    report.candidates_tested = len(candidates)

    best = None
    for cand in candidates:
        train, val = evaluate(replace(current, **cand), sliced, train_dates, val_dates)
        if train.trades < base.learn_min_trades:
            continue
        if best is None or train.sharpe > best[1].sharpe:
            best = (cand, train, val)

    if best is None:
        report.reason = "no candidate traded enough to be scored"
        return _finish(state, report, as_of)

    cand, train, val = best
    report.best_train, report.best_val = train, val
    min_val_trades = max(3, base.learn_min_trades * base.learn_val_days // max(1, base.learn_train_days))
    since_change = sessions_since_last_change(state, dates)

    if since_change is not None and since_change < base.learn_cooldown_days:
        report.reason = f"cooldown: last change was {since_change} sessions ago (need {base.learn_cooldown_days})"
    elif train.sharpe < cur_train.sharpe + base.learn_margin and cur_train.trades >= base.learn_min_trades:
        report.reason = (
            f"best training Sharpe {train.sharpe:+.2f} does not beat current {cur_train.sharpe:+.2f} "
            f"by the required margin {base.learn_margin}"
        )
    elif val.trades < min_val_trades:
        report.reason = f"best candidate made only {val.trades} trades on held-out days (need {min_val_trades})"
    elif val.pnl <= 0:
        report.reason = "best candidate lost money on held-out days"
    elif val.sharpe < cur_val.sharpe + base.learn_margin:
        report.reason = (
            f"held-out Sharpe {val.sharpe:+.2f} does not beat current {cur_val.sharpe:+.2f} "
            f"by the required margin {base.learn_margin}"
        )
    else:
        report.adopted = True
        report.params = cand
        report.reason = f"held-out Sharpe improved {cur_val.sharpe:+.2f} -> {val.sharpe:+.2f}"
    return _finish(state, report, as_of)


def sessions_since_last_change(state: LearningState, dates: list[date]) -> int | None:
    adopted = [h for h in state.data["history"] if h.get("adopted") and h.get("date")]
    if not adopted:
        return None
    last = date.fromisoformat(adopted[-1]["date"])
    return sum(1 for d in dates if d > last)


def _finish(state: LearningState, report: LearningReport, as_of: date) -> LearningReport:
    if report.adopted:
        state.data["version"] += 1
        state.data["params"] = report.params
    report.version = state.version
    state.data["history"].append(
        {
            "version": state.version,
            "date": str(as_of),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "adopted": report.adopted,
            "reason": report.reason,
            "params": report.params,
            "previous_params": report.previous_params,
            "current_train": report.current_train.as_dict() if report.current_train else None,
            "current_val": report.current_val.as_dict() if report.current_val else None,
            "best_train": report.best_train.as_dict() if report.best_train else None,
            "best_val": report.best_val.as_dict() if report.best_val else None,
            "candidates_tested": report.candidates_tested,
            "risk_multiplier": report.risk_multiplier,
            "risk_note": report.risk_note,
        }
    )
    state.data["history"] = state.data["history"][-500:]
    state.data["last_run"] = str(as_of)
    state.save()
    return report

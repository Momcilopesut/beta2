from datetime import date

import pytest

from bot.config import Settings
from bot.data import synthetic_bars
from bot.learning import (
    TUNABLE,
    LearningState,
    WindowScore,
    current_params,
    generate_candidates,
    is_valid,
    run_daily_learning,
    update_risk_multiplier,
)
from bot.strategy import NY


def small_settings(tmp_path, **kw):
    s = Settings(
        state_path=str(tmp_path / "state.json"),
        learn_train_days=8,
        learn_val_days=4,
        learn_candidates=4,
        learn_min_trades=2,
    )
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def test_candidates_stay_in_bounds_and_near_current():
    params = current_params(Settings())
    cands = generate_candidates(params, n_random=20, seed=1)
    assert len(cands) > 10
    for c in cands:
        assert is_valid(c)
        for name, spec in TUNABLE.items():
            assert spec.low <= c[name] <= spec.high
            assert abs(c[name] - params[name]) <= 2 * spec.step + 1e-9
    # deterministic for the same seed
    assert cands == generate_candidates(params, n_random=20, seed=1)


def test_state_apply_and_rollback(tmp_path):
    base = Settings(state_path=str(tmp_path / "s.json"), risk_per_trade=0.01)
    st = LearningState(base.state_path)
    st.data.update(version=1, params={"ema_fast": 7}, risk_multiplier=0.5)
    st.data["history"].append({"version": 1, "adopted": True, "params": {"ema_fast": 7}, "date": "2025-01-02"})
    eff = st.apply(base)
    assert eff.ema_fast == 7 and eff.risk_per_trade == pytest.approx(0.005)
    assert base.ema_fast == 9  # base is untouched

    st.rollback(0)
    st.save()
    reloaded = LearningState(base.state_path)
    assert reloaded.params == {} and reloaded.version == 2
    assert reloaded.apply(base).ema_fast == 9


def test_risk_multiplier_cuts_after_losses_and_recovers(tmp_path):
    st = LearningState(str(tmp_path / "s.json"))
    for i, pnl in enumerate([-100, -50, -20]):
        st.record_day(date(2025, 1, 2 + i), pnl, 100_000 + pnl)
    mult, _ = update_risk_multiplier(st)
    assert mult == 0.75
    for i, pnl in enumerate([300, 200, 100, 150, 100]):
        st.record_day(date(2025, 1, 10 + i), pnl, 100_000)
    mult, _ = update_risk_multiplier(st)
    assert mult == pytest.approx(0.9375)


def test_not_enough_history_keeps_params(tmp_path):
    s = small_settings(tmp_path)
    st = LearningState(s.state_path)
    report = run_daily_learning(s, {"X": synthetic_bars(days=5)}, st, as_of=date(2025, 1, 10))
    assert not report.adopted and "not enough history" in report.reason
    assert LearningState(s.state_path).data["history"][-1]["adopted"] is False


def test_learning_run_persists_history(tmp_path):
    s = small_settings(tmp_path)
    st = LearningState(s.state_path)
    report = run_daily_learning(s, {"X": synthetic_bars(days=14, seed=3)}, st, as_of=date(2025, 1, 24))
    assert report.candidates_tested > 0
    saved = LearningState(s.state_path)
    assert saved.data["last_run"] == "2025-01-24"
    entry = saved.data["history"][-1]
    assert entry["adopted"] == report.adopted
    if report.adopted:
        assert saved.params == report.params and saved.version == 1
    else:
        assert saved.params == {} and saved.version == 0


def test_cooldown_blocks_back_to_back_changes(tmp_path, monkeypatch):
    s = small_settings(tmp_path, learn_cooldown_days=5, learn_margin=0.0)
    bars = {"X": synthetic_bars(days=14, seed=3)}
    last_day = max(bars["X"].index.tz_convert(NY).date)

    # Make every candidate look much better than the current params.
    import bot.learning as learning

    def fake_evaluate(settings, *_args, **_kw):
        good = settings.ema_fast != 9
        score = WindowScore(5.0 if good else 0.0, 1000.0 if good else 0.0, 20, -1.0)
        return score, score

    monkeypatch.setattr(learning, "evaluate", fake_evaluate)
    st = LearningState(s.state_path)
    first = run_daily_learning(s, bars, st, as_of=last_day)
    assert first.adopted
    second = run_daily_learning(s, bars, st, as_of=last_day)
    assert not second.adopted and "cooldown" in second.reason

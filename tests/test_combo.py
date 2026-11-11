from datetime import date

import pytest

from bot.combo import run_combo_backtest
from bot.momentum import UNIVERSE as MOM
from bot.swing import UNIVERSE as SWING
from bot.swing import synthetic_daily


def test_combo_resets_each_year_and_is_sum_of_halves():
    data = synthetic_daily(years=4, seed=5, start="2019-01-01", symbols=tuple(dict.fromkeys(MOM + SWING)))
    r = run_combo_backtest(data, date(2020, 3, 1), date(2022, 6, 30), 1000.0, 2.0, MOM)
    combo, mom, sw = r["combo"], r["mom_sleeve"], r["swing_sleeve"]
    assert combo.iloc[0] == pytest.approx(1000.0, rel=0.02)
    assert (combo - (mom + sw)).abs().max() < 1e-6
    # At each January reset both halves start equal.
    for y in (2021, 2022):
        first = min(d for d in combo.index if d.year == y)
        assert mom.loc[first] == pytest.approx(sw.loc[first], rel=0.02)
    assert len(r["mom_cash"]) == len(combo)

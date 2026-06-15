import pandas as pd
import pytest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from helpers import split_folds, fold_table
from tests.conftest import make_trades


CAPITAL = 50_000

EXPECTED_COLUMNS = [
    "Fold", "Train Trades", "Train WR%", "Train PF",
    "OOS Trades", "OOS WR%", "OOS PF", "OOS Net $", "OOS MaxDD%",
]


@pytest.fixture
def folds_fixture():
    trades = make_trades(
        [100, -50, 200, -30, 150, -80, 120, -40, 90, -60,
         80, -20, 300, -100, 50, -70, 110, -90, 140, -30]
    )
    return split_folds(trades, n=4, train_pct=70, min_trades=2)


class TestFoldTable:
    def test_returns_dataframe(self, folds_fixture):
        ft = fold_table(folds_fixture, CAPITAL)
        assert isinstance(ft, pd.DataFrame)

    def test_row_count_matches_folds(self, folds_fixture):
        ft = fold_table(folds_fixture, CAPITAL)
        assert len(ft) == len(folds_fixture)

    def test_column_names_stable(self, folds_fixture):
        ft = fold_table(folds_fixture, CAPITAL)
        assert list(ft.columns) == EXPECTED_COLUMNS

    def test_fold_column_sequential(self, folds_fixture):
        ft = fold_table(folds_fixture, CAPITAL)
        assert list(ft["Fold"]) == list(range(1, len(folds_fixture) + 1))

    def test_win_rate_rounded_to_1dp(self, folds_fixture):
        ft = fold_table(folds_fixture, CAPITAL)
        for val in ft["OOS WR%"]:
            assert val == round(val, 1)

    def test_profit_factor_rounded_to_2dp(self, folds_fixture):
        ft = fold_table(folds_fixture, CAPITAL)
        for val in ft["OOS PF"]:
            assert val == round(val, 2)

    def test_trade_counts_positive(self, folds_fixture):
        ft = fold_table(folds_fixture, CAPITAL)
        assert (ft["Train Trades"] > 0).all()
        assert (ft["OOS Trades"] > 0).all()

    def test_oos_max_dd_non_positive(self, folds_fixture):
        ft = fold_table(folds_fixture, CAPITAL)
        # Max drawdown should be <= 0; allow 0 for all-win folds
        assert (ft["OOS MaxDD%"] <= 0).all()

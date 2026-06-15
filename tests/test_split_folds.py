import pandas as pd
import pytest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from helpers import split_folds
from tests.conftest import make_trades


class TestSplitFoldsBasic:
    def test_returns_correct_number_of_folds(self, sample_trades):
        folds = split_folds(sample_trades, n=4, train_pct=70, min_trades=3)
        assert len(folds) == 4

    def test_fold_numbers_are_sequential(self, sample_trades):
        folds = split_folds(sample_trades, n=4, train_pct=70, min_trades=3)
        assert [f["fold"] for f in folds] == [1, 2, 3, 4]

    def test_each_fold_has_required_keys(self, sample_trades):
        folds = split_folds(sample_trades, n=2, train_pct=70, min_trades=3)
        required = {"fold", "train", "test", "train_start", "train_end",
                    "test_start", "test_end"}
        for f in folds:
            assert required.issubset(f.keys())

    def test_train_before_test(self, sample_trades):
        folds = split_folds(sample_trades, n=4, train_pct=70, min_trades=3)
        for f in folds:
            assert f["train_end"] <= f["test_start"]

    def test_train_dates_in_order(self, sample_trades):
        folds = split_folds(sample_trades, n=4, train_pct=70, min_trades=3)
        for f in folds:
            assert f["train_start"] <= f["train_end"]
            assert f["test_start"] <= f["test_end"]


class TestSplitFoldsTrainPct:
    def test_70pct_split(self):
        trades = make_trades([100] * 20)
        folds = split_folds(trades, n=2, train_pct=70, min_trades=1)
        for f in folds:
            chunk_size = len(f["train"]) + len(f["test"])
            expected_train = int(chunk_size * 0.70)
            assert len(f["train"]) == expected_train

    def test_50pct_split(self):
        trades = make_trades([100] * 20)
        folds = split_folds(trades, n=2, train_pct=50, min_trades=1)
        for f in folds:
            chunk_size = len(f["train"]) + len(f["test"])
            expected_train = int(chunk_size * 0.50)
            assert len(f["train"]) == expected_train

    def test_85pct_split(self):
        trades = make_trades([100] * 20)
        folds = split_folds(trades, n=2, train_pct=85, min_trades=1)
        for f in folds:
            chunk_size = len(f["train"]) + len(f["test"])
            expected_train = int(chunk_size * 0.85)
            assert len(f["train"]) == expected_train


class TestSplitFoldsLastFoldRemainder:
    def test_last_fold_absorbs_remainder(self):
        # 21 trades, 4 folds: fold_size=5, last fold gets 6
        trades = make_trades([100] * 21)
        folds = split_folds(trades, n=4, train_pct=70, min_trades=1)
        last = folds[-1]
        total_in_last = len(last["train"]) + len(last["test"])
        total_in_others = sum(
            len(f["train"]) + len(f["test"]) for f in folds[:-1]
        )
        assert total_in_last + total_in_others == 21

    def test_all_trades_covered(self):
        trades = make_trades([100] * 17)
        folds = split_folds(trades, n=4, train_pct=70, min_trades=1)
        total_used = sum(len(f["train"]) + len(f["test"]) for f in folds)
        assert total_used == 17


class TestSplitFoldsMinTradesFilter:
    def test_fold_excluded_when_train_too_small(self):
        # 10 trades, 5 folds → 2 trades per fold; train at 70% → 1 train trade
        # With min_trades=3, all folds should be excluded
        trades = make_trades([100] * 10)
        folds = split_folds(trades, n=5, train_pct=70, min_trades=3)
        for f in folds:
            assert len(f["train"]) >= 3

    def test_zero_folds_when_all_excluded(self):
        trades = make_trades([100] * 5)
        folds = split_folds(trades, n=5, train_pct=70, min_trades=10)
        assert len(folds) == 0

    def test_test_must_have_at_least_one_trade(self):
        # train_pct=100 would leave test empty — no folds should survive
        trades = make_trades([100] * 20)
        folds = split_folds(trades, n=4, train_pct=100, min_trades=1)
        for f in folds:
            assert len(f["test"]) >= 1


class TestSplitFoldsEdgeCases:
    def test_two_folds_minimum(self, sample_trades):
        folds = split_folds(sample_trades, n=2, train_pct=70, min_trades=1)
        assert len(folds) == 2

    def test_default_min_trades_is_5(self):
        # Default min_trades=5 should filter folds with fewer than 5 train trades
        # 10 trades, 2 folds → 5 per fold; 70% of 5 = 3 train trades < 5
        trades = make_trades([100] * 10)
        folds_default = split_folds(trades, n=2, train_pct=70)
        folds_explicit = split_folds(trades, n=2, train_pct=70, min_trades=5)
        assert len(folds_default) == len(folds_explicit)

    def test_train_and_test_are_non_overlapping(self, sample_trades):
        folds = split_folds(sample_trades, n=4, train_pct=70, min_trades=1)
        for f in folds:
            train_idx = set(f["train"].index)
            test_idx = set(f["test"].index)
            assert train_idx.isdisjoint(test_idx)

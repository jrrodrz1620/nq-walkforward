import numpy as np
import pandas as pd
import pytest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from helpers import calc_metrics
from tests.conftest import make_trades


CAPITAL = 50_000


class TestCalcMetricsEmpty:
    def test_returns_zero_dict(self, empty_trades):
        m = calc_metrics(empty_trades, CAPITAL)
        assert m["n_trades"] == 0
        assert m["net_profit"] == 0
        assert m["win_rate"] == 0
        assert m["profit_factor"] == 0
        assert m["max_dd"] == 0
        assert m["return_pct"] == 0


class TestCalcMetricsMixed:
    def test_n_trades(self, sample_trades):
        m = calc_metrics(sample_trades, CAPITAL)
        assert m["n_trades"] == 20

    def test_net_profit(self, sample_trades):
        expected = sample_trades["profit_usd"].sum()
        m = calc_metrics(sample_trades, CAPITAL)
        assert abs(m["net_profit"] - expected) < 1e-9

    def test_win_rate_range(self, sample_trades):
        m = calc_metrics(sample_trades, CAPITAL)
        assert 0 <= m["win_rate"] <= 100

    def test_win_rate_value(self, sample_trades):
        wins = (sample_trades["profit_usd"] > 0).sum()
        expected = wins / len(sample_trades) * 100
        m = calc_metrics(sample_trades, CAPITAL)
        assert abs(m["win_rate"] - expected) < 1e-9

    def test_profit_factor_positive(self, sample_trades):
        m = calc_metrics(sample_trades, CAPITAL)
        assert m["profit_factor"] > 0

    def test_profit_factor_value(self, sample_trades):
        pnl = sample_trades["profit_usd"].values
        gross_win = pnl[pnl > 0].sum()
        gross_loss = abs(pnl[pnl < 0].sum())
        expected = gross_win / gross_loss
        m = calc_metrics(sample_trades, CAPITAL)
        assert abs(m["profit_factor"] - expected) < 1e-9

    def test_max_dd_non_positive(self, sample_trades):
        m = calc_metrics(sample_trades, CAPITAL)
        assert m["max_dd"] <= 0

    def test_max_dd_percentage(self, sample_trades):
        # max_dd should be expressed as a percentage, typically > -100
        m = calc_metrics(sample_trades, CAPITAL)
        assert m["max_dd"] > -100

    def test_avg_win_positive(self, sample_trades):
        m = calc_metrics(sample_trades, CAPITAL)
        assert m["avg_win"] > 0

    def test_avg_loss_negative(self, sample_trades):
        m = calc_metrics(sample_trades, CAPITAL)
        assert m["avg_loss"] < 0

    def test_return_pct(self, sample_trades):
        m = calc_metrics(sample_trades, CAPITAL)
        expected = m["net_profit"] / CAPITAL * 100
        assert abs(m["return_pct"] - expected) < 1e-9

    def test_equity_array_length(self, sample_trades):
        m = calc_metrics(sample_trades, CAPITAL)
        assert len(m["equity"]) == len(sample_trades)

    def test_equity_starts_near_capital(self, sample_trades):
        m = calc_metrics(sample_trades, CAPITAL)
        first_pnl = sample_trades["profit_usd"].iloc[0]
        assert abs(m["equity"][0] - (CAPITAL + first_pnl)) < 1e-9


class TestCalcMetricsAllWins:
    def test_profit_factor_is_inf(self, all_wins):
        m = calc_metrics(all_wins, CAPITAL)
        assert m["profit_factor"] == np.inf

    def test_win_rate_100(self, all_wins):
        m = calc_metrics(all_wins, CAPITAL)
        assert m["win_rate"] == 100.0

    def test_avg_loss_zero(self, all_wins):
        m = calc_metrics(all_wins, CAPITAL)
        assert m["avg_loss"] == 0

    def test_max_dd_zero(self, all_wins):
        # Equity only rises, so drawdown should be zero
        m = calc_metrics(all_wins, CAPITAL)
        assert m["max_dd"] == 0.0


class TestCalcMetricsAllLosses:
    def test_profit_factor_zero(self, all_losses):
        m = calc_metrics(all_losses, CAPITAL)
        assert m["profit_factor"] == 0

    def test_win_rate_zero(self, all_losses):
        m = calc_metrics(all_losses, CAPITAL)
        assert m["win_rate"] == 0.0

    def test_avg_win_zero(self, all_losses):
        m = calc_metrics(all_losses, CAPITAL)
        assert m["avg_win"] == 0

    def test_net_profit_negative(self, all_losses):
        m = calc_metrics(all_losses, CAPITAL)
        assert m["net_profit"] < 0


class TestCalcMetricsSingleTrade:
    def test_n_trades_one(self, single_trade):
        m = calc_metrics(single_trade, CAPITAL)
        assert m["n_trades"] == 1

    def test_no_drawdown_on_win(self, single_trade):
        m = calc_metrics(single_trade, CAPITAL)
        assert m["max_dd"] == 0.0

    def test_return_pct_correct(self, single_trade):
        m = calc_metrics(single_trade, CAPITAL)
        assert abs(m["return_pct"] - (500 / CAPITAL * 100)) < 1e-9


class TestCalcMetricsDrawdown:
    def test_drawdown_after_peak(self):
        # Equity goes up then down sharply
        profits = [1000, 1000, -3000, 1000]
        trades = make_trades(profits)
        m = calc_metrics(trades, CAPITAL)
        # After two 1000 wins: equity = 52000. Then -3000: equity = 49000.
        # DD = (49000 - 52000) / 52000 * 100 ≈ -5.77%
        assert m["max_dd"] < 0
        assert abs(m["max_dd"] - ((49000 - 52000) / 52000 * 100)) < 0.01

    def test_drawdown_recovers(self):
        # Equity dips then fully recovers; max_dd reflects the dip
        profits = [1000, -500, 1000]
        trades = make_trades(profits)
        m = calc_metrics(trades, CAPITAL)
        assert m["max_dd"] < 0

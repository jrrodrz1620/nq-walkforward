"""Kill switch, drawdown alerts, and the position gate."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from polymarket_arb.risk import RiskManager

START = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


@pytest.fixture
def risk(cfg):
    return RiskManager(cfg, starting_equity=1_000.0, now=START)


# ─────────────────────────────────────────────
# KILL SWITCH
# ─────────────────────────────────────────────

def test_kill_switch_trips_at_the_configured_daily_drawdown(risk, cfg):
    assert cfg.max_daily_drawdown == 0.10
    alerts = risk.update_equity(920.0, now=START)          # -8%, under the limit
    assert risk.halted is False
    assert all(a.kind != "kill_switch" for a in alerts)

    alerts = risk.update_equity(899.0, now=START)          # -10.1%, over the limit
    assert risk.halted is True
    assert any(a.kind == "kill_switch" and a.severity == "critical" for a in alerts)
    assert "daily drawdown" in risk.halt_reason


def test_drawdown_is_measured_from_the_high_water_mark(risk):
    risk.update_equity(2_000.0, now=START)                  # new high
    risk.update_equity(1_900.0, now=START)                  # -5% from the high
    assert risk.daily_drawdown == pytest.approx(0.05)
    assert risk.halted is False
    risk.update_equity(1_700.0, now=START)                  # -15% from the high
    assert risk.halted is True


def test_kill_switch_stays_tripped_when_equity_recovers(risk):
    risk.update_equity(800.0, now=START)
    assert risk.halted is True
    risk.update_equity(1_050.0, now=START)
    assert risk.halted is True, "a kill switch that self-clears is not a kill switch"


def test_session_drawdown_also_halts(cfg):
    cfg.max_daily_drawdown = 0.90        # effectively disable the daily limit
    manager = RiskManager(cfg, starting_equity=1_000.0, now=START)
    manager.update_equity(700.0, now=START)     # -30% session, limit is 25%
    assert manager.halted is True
    assert "session drawdown" in manager.halt_reason


def test_warnings_fire_once_per_level_on_the_way_down(risk, cfg):
    kinds = []
    # Drawdowns of 0%, 3%, 4%, 5%, 7.5% and 9.1% against a 10% limit.
    for equity in (1_000.0, 970.0, 960.0, 950.0, 925.0, 909.0):
        kinds += [a.kind for a in risk.update_equity(equity, now=START)]
    warnings = [k for k in kinds if k == "drawdown_warning"]
    # 50%, 75% and 90% of the 10% limit, each announced exactly once.
    assert len(warnings) == len(cfg.drawdown_alert_levels)


def test_manual_halt_and_resume(risk):
    alert = risk.halt("feed outage", now=START)
    assert risk.halted is True and alert.kind == "kill_switch"
    resumed = risk.resume(now=START)
    assert risk.halted is False and resumed.kind == "resumed"
    # Resuming re-baselines so the same equity does not instantly re-trip.
    assert risk.daily_drawdown == 0.0


def test_day_rollover_clears_the_switch_and_rebaselines(risk):
    risk.update_equity(800.0, now=START)
    assert risk.halted is True
    alerts = risk.update_equity(800.0, now=START + timedelta(days=1))
    assert risk.halted is False
    assert any(a.kind == "day_reset" for a in alerts)
    assert risk.day_start_equity == 800.0
    assert risk.trades_today == 0


# ─────────────────────────────────────────────
# METRICS
# ─────────────────────────────────────────────

def test_pnl_and_headroom(risk, cfg):
    risk.update_equity(1_100.0, now=START)
    assert risk.daily_pnl == pytest.approx(100.0)
    assert risk.session_pnl == pytest.approx(100.0)
    # The switch trips below 90% of the $1,100 high-water mark.
    assert risk.headroom() == pytest.approx(1_100.0 - 990.0)


def test_headroom_is_never_negative(risk):
    risk.update_equity(500.0, now=START)
    assert risk.headroom() == 0.0


def test_consecutive_losses_reset_on_a_win(risk):
    risk.record_trade_result(-5.0)
    risk.record_trade_result(-5.0)
    assert risk.consecutive_losses == 2
    risk.record_trade_result(7.0)
    assert risk.consecutive_losses == 0
    assert risk.trades_today == 3


def test_snapshot_exposes_the_dashboard_fields(risk):
    snapshot = risk.snapshot()
    assert {"equity", "daily_drawdown", "halted", "headroom"} <= set(snapshot)


# ─────────────────────────────────────────────
# POSITION GATE
# ─────────────────────────────────────────────

def _check(risk, **kwargs):
    params = dict(notional=50.0, open_exposure=0.0, open_positions=0)
    params.update(kwargs)
    return risk.check_new_position(**params)


def test_a_reasonable_position_is_allowed(risk):
    assert bool(_check(risk)) is True


def test_halted_trading_blocks_everything(risk):
    risk.halt("test", now=START)
    decision = _check(risk)
    assert bool(decision) is False and "halted" in decision.reason


def test_unhealthy_feeds_block_entry(risk):
    assert "feeds unhealthy" in _check(risk, feeds_healthy=False).reason


def test_position_count_limit(risk, cfg):
    assert "max open positions" in _check(risk, open_positions=cfg.max_open_positions).reason


def test_one_position_per_market(risk):
    assert "already holding" in _check(risk, market_already_held=True).reason


def test_consecutive_loss_limit(risk, cfg):
    for _ in range(cfg.max_consecutive_losses):
        risk.record_trade_result(-1.0)
    assert "consecutive losses" in _check(risk).reason


def test_per_position_cap_is_enforced(risk, cfg):
    over = cfg.max_position_pct * 1_000.0 + 1.0
    assert "per-position cap" in _check(risk, notional=over).reason


def test_total_exposure_budget_is_enforced(risk, cfg):
    exposure = cfg.max_total_exposure_pct * 1_000.0
    decision = _check(risk, notional=10.0, open_exposure=exposure)
    assert bool(decision) is False and "exposure budget" in decision.reason
    assert decision.exposure_budget == 0.0


def test_kill_switch_headroom_caps_the_last_trade(risk, cfg):
    cfg.max_position_pct = 1.0
    cfg.max_total_exposure_pct = 1.0
    risk.update_equity(1_000.0, now=START)
    # $100 of headroom remains before a 10% daily drawdown; $150 must be refused.
    assert "headroom" in _check(risk, notional=150.0).reason
    assert bool(_check(risk, notional=90.0)) is True


def test_zero_notional_is_refused(risk):
    assert bool(_check(risk, notional=0.0)) is False

"""
Drawdown tracking, exposure limits, and the kill switch.

`RiskManager` is the single authority on whether a new position may be opened.
It owns the daily high-water mark, trips the kill switch when the daily
drawdown breaches the configured limit, and emits alert records that the engine
forwards to Telegram. Once tripped it stays tripped until the UTC day rolls
over or an operator calls `resume()` -- an automatic mid-day reset would defeat
the purpose of a kill switch.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from .config import Config

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# EVENTS
# ─────────────────────────────────────────────

@dataclass(frozen=True)
class RiskAlert:
    """Something the operator should know about, in Telegram-ready form."""

    kind: str          # "drawdown_warning" | "kill_switch" | "day_reset" | "resumed"
    severity: str      # "info" | "warning" | "critical"
    message: str
    drawdown: float = 0.0
    equity: float = 0.0
    at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True)
class RiskDecision:
    """Verdict on a proposed position."""

    allowed: bool
    reason: str
    exposure_budget: float = 0.0

    def __bool__(self) -> bool:
        return self.allowed


# ─────────────────────────────────────────────
# RISK MANAGER
# ─────────────────────────────────────────────

class RiskManager:
    """Portfolio-level guardrails. All thresholds come from `Config`."""

    def __init__(self, cfg: Config, *, starting_equity: float | None = None,
                 now: datetime | None = None):
        self.cfg = cfg
        now = now or datetime.now(UTC)
        equity = cfg.starting_bankroll if starting_equity is None else starting_equity

        self.equity = equity
        self.session_start_equity = equity
        self.session_high_water = equity
        self.day = now.date()
        self.day_start_equity = equity
        self.day_high_water = equity

        self.halted = False
        self.halt_reason = ""
        self.halted_at: datetime | None = None

        self.consecutive_losses = 0
        self.trades_today = 0
        self._alerted_levels: set[float] = set()

    # ── state updates ────────────────────────────────────────

    def update_equity(self, equity: float, *, now: datetime | None = None) -> list[RiskAlert]:
        """Record the latest mark-to-market equity and react to it.

        Returns any alerts triggered: drawdown warnings, the kill switch firing,
        or the daily reset.
        """
        now = now or datetime.now(UTC)
        alerts = self._roll_day(now)

        self.equity = equity
        self.session_high_water = max(self.session_high_water, equity)
        self.day_high_water = max(self.day_high_water, equity)

        alerts.extend(self._check_drawdown(now))
        return alerts

    def _roll_day(self, now: datetime) -> list[RiskAlert]:
        """Reset daily counters (and the kill switch) at the UTC day boundary."""
        today: date = now.date()
        if today == self.day:
            return []
        was_halted = self.halted
        self.day = today
        self.day_start_equity = self.equity
        self.day_high_water = self.equity
        self.trades_today = 0
        self._alerted_levels.clear()
        self.halted = False
        self.halt_reason = ""
        self.halted_at = None
        message = f"New UTC day {today.isoformat()}: daily drawdown reset from ${self.equity:,.2f}"
        if was_halted:
            message += " (kill switch cleared)"
        log.info(message)
        return [RiskAlert("day_reset", "info", message, equity=self.equity, at=now)]

    def _check_drawdown(self, now: datetime) -> list[RiskAlert]:
        alerts: list[RiskAlert] = []
        daily = self.daily_drawdown
        total = self.total_drawdown

        if not self.halted and daily >= self.cfg.max_daily_drawdown:
            alerts.append(self._trip(
                f"KILL SWITCH: daily drawdown {daily:.2%} exceeded limit "
                f"{self.cfg.max_daily_drawdown:.2%}",
                daily, now,
            ))
        elif not self.halted and total >= self.cfg.max_total_drawdown:
            alerts.append(self._trip(
                f"KILL SWITCH: session drawdown {total:.2%} exceeded limit "
                f"{self.cfg.max_total_drawdown:.2%}",
                total, now,
            ))
        elif not self.halted:
            # Warn on the way down so the operator sees it coming.
            for level in sorted(self.cfg.drawdown_alert_levels):
                threshold = level * self.cfg.max_daily_drawdown
                if daily >= threshold and level not in self._alerted_levels:
                    self._alerted_levels.add(level)
                    alerts.append(RiskAlert(
                        "drawdown_warning",
                        "warning",
                        f"Daily drawdown {daily:.2%} is at {level:.0%} of the "
                        f"{self.cfg.max_daily_drawdown:.2%} kill-switch limit "
                        f"(equity ${self.equity:,.2f})",
                        drawdown=daily,
                        equity=self.equity,
                        at=now,
                    ))
        return alerts

    def _trip(self, message: str, drawdown: float, now: datetime) -> RiskAlert:
        self.halted = True
        self.halt_reason = message
        self.halted_at = now
        log.critical(message)
        return RiskAlert("kill_switch", "critical", message,
                         drawdown=drawdown, equity=self.equity, at=now)

    def halt(self, reason: str, *, now: datetime | None = None) -> RiskAlert:
        """Trip the kill switch manually (shutdown, feed loss, operator action)."""
        return self._trip(f"KILL SWITCH: {reason}", self.daily_drawdown, now or datetime.now(UTC))

    def resume(self, *, now: datetime | None = None) -> RiskAlert:
        """Clear a tripped kill switch. Intended for explicit operator use."""
        self.halted = False
        self.halt_reason = ""
        self.halted_at = None
        self._alerted_levels.clear()
        # Re-baseline the day so the same drawdown does not immediately re-trip.
        self.day_high_water = self.equity
        self.day_start_equity = self.equity
        message = f"Kill switch cleared by operator; re-baselined at ${self.equity:,.2f}"
        log.warning(message)
        return RiskAlert("resumed", "warning", message, equity=self.equity,
                         at=now or datetime.now(UTC))

    def record_trade_result(self, pnl: float) -> None:
        """Update streak counters after a position closes."""
        self.trades_today += 1
        if pnl < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0

    # ── metrics ──────────────────────────────────────────────

    @property
    def daily_drawdown(self) -> float:
        """Fractional decline from the day's high-water mark."""
        if self.day_high_water <= 0:
            return 0.0
        return max(0.0, (self.day_high_water - self.equity) / self.day_high_water)

    @property
    def total_drawdown(self) -> float:
        if self.session_high_water <= 0:
            return 0.0
        return max(0.0, (self.session_high_water - self.equity) / self.session_high_water)

    @property
    def daily_pnl(self) -> float:
        return self.equity - self.day_start_equity

    @property
    def session_pnl(self) -> float:
        return self.equity - self.session_start_equity

    def headroom(self) -> float:
        """Dollars of loss remaining before the kill switch trips."""
        limit_equity = self.day_high_water * (1.0 - self.cfg.max_daily_drawdown)
        return max(0.0, self.equity - limit_equity)

    # ── gating ───────────────────────────────────────────────

    def exposure_budget(self, open_exposure: float) -> float:
        """Dollars of additional exposure still permitted."""
        cap = self.cfg.max_total_exposure_pct * self.equity
        return max(0.0, cap - open_exposure)

    def check_new_position(
        self,
        *,
        notional: float,
        open_exposure: float,
        open_positions: int,
        market_already_held: bool = False,
        feeds_healthy: bool = True,
    ) -> RiskDecision:
        """Decide whether a proposed position may be opened."""
        budget = self.exposure_budget(open_exposure)

        if self.halted:
            return RiskDecision(False, f"trading halted: {self.halt_reason}", budget)
        if not feeds_healthy:
            return RiskDecision(False, "market data feeds unhealthy", budget)
        if self.equity <= 0:
            return RiskDecision(False, "equity exhausted", budget)
        if open_positions >= self.cfg.max_open_positions:
            return RiskDecision(
                False,
                f"already at max open positions ({self.cfg.max_open_positions})",
                budget,
            )
        if market_already_held and self.cfg.one_position_per_market:
            return RiskDecision(False, "already holding a position in this market", budget)
        if self.consecutive_losses >= self.cfg.max_consecutive_losses:
            return RiskDecision(
                False,
                f"{self.consecutive_losses} consecutive losses "
                f"(limit {self.cfg.max_consecutive_losses})",
                budget,
            )
        if notional <= 0:
            return RiskDecision(False, "zero notional", budget)
        if notional > budget + 1e-9:
            return RiskDecision(
                False,
                f"${notional:,.2f} exceeds remaining exposure budget ${budget:,.2f}",
                budget,
            )
        if notional > self.cfg.max_position_pct * self.equity + 1e-9:
            return RiskDecision(
                False,
                f"${notional:,.2f} exceeds per-position cap "
                f"{self.cfg.max_position_pct:.2%} of ${self.equity:,.2f}",
                budget,
            )
        if notional > self.headroom():
            return RiskDecision(
                False,
                f"${notional:,.2f} exceeds kill-switch headroom ${self.headroom():,.2f}",
                budget,
            )
        return RiskDecision(True, "ok", budget)

    def snapshot(self) -> dict[str, float | bool | str]:
        """Flat dict for the dashboard and the equity-curve table."""
        return {
            "equity": self.equity,
            "daily_pnl": self.daily_pnl,
            "session_pnl": self.session_pnl,
            "daily_drawdown": self.daily_drawdown,
            "total_drawdown": self.total_drawdown,
            "day_high_water": self.day_high_water,
            "session_high_water": self.session_high_water,
            "headroom": self.headroom(),
            "halted": self.halted,
            "halt_reason": self.halt_reason,
            "consecutive_losses": self.consecutive_losses,
            "trades_today": self.trades_today,
        }

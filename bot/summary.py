"""
Daily P/L summary + a tiny in-process scheduler.

``build_summary`` formats a Telegram-ready recap (today's trades, realized PnL,
open positions, and drawdown). ``DailySummaryScheduler`` fires a callback once a
day at a configured UTC time — best-effort, isolated, never touches trading.
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from typing import Callable


def _money(n: float) -> str:
    return f"{'+' if n >= 0 else '-'}${abs(n):,.2f}"


def build_summary(stats: dict, day_pnl_mtm: float, positions: list) -> str:
    """Human-readable daily summary from equity_stats() + live figures."""
    lines = [
        "📊 <b>Daily Summary</b>",
        f"Today: {_money(stats['today_realized'])} realized "
        f"({stats['today_trades']} trades)",
        f"Open MtM PnL: {_money(day_pnl_mtm)}",
        "",
        f"Realized (all): {_money(stats['realized_total'])}",
        f"Peak equity: {_money(stats['peak_equity'])}",
        f"Current drawdown: {_money(stats['current_drawdown'])}",
        f"Max drawdown: {_money(stats['max_drawdown'])}",
        f"Total trades: {stats['total_trades']}",
    ]
    if positions:
        lines.append("")
        lines.append("<b>Open:</b>")
        for p in positions:
            lines.append(f"• {p['symbol']} {p['net_contracts']:+d} @ {p['avg_price']}")
    else:
        lines.append("\nFlat — no open positions.")
    return "\n".join(lines)


class DailySummaryScheduler:
    """Daemon thread that calls ``send_fn`` once per day at ``HH:MM`` UTC."""

    def __init__(self, hhmm_utc: str, send_fn: Callable[[], None]):
        self.hour, self.minute = (int(x) for x in hhmm_utc.split(":"))
        self.send_fn = send_fn
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> "DailySummaryScheduler":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def _seconds_until_next(self) -> float:
        now = datetime.now(timezone.utc)
        target = now.replace(hour=self.hour, minute=self.minute,
                             second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return (target - now).total_seconds()

    def _run(self) -> None:
        while not self._stop.is_set():
            if self._stop.wait(self._seconds_until_next()):
                break
            try:
                self.send_fn()
            except Exception:
                pass

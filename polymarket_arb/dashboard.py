"""
Terminal dashboard: P&L, win rate, open positions, and the last ten trades.

Rendered with `rich` when it is installed, and with a plain-text fallback when
it is not (or when the output is not a TTY, as in a log file or a CI run). The
engine builds a `DashboardState` snapshot each tick; the renderers are pure
functions of that snapshot, so neither can stall or crash the trading loop.
"""
from __future__ import annotations

import logging
import shutil
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime

from .models import TradeStats

log = logging.getLogger(__name__)

try:  # pragma: no cover - presence depends on the environment
    from rich.console import Console, Group
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    RICH_AVAILABLE = True
except ImportError:  # pragma: no cover
    RICH_AVAILABLE = False


# ─────────────────────────────────────────────
# VIEW MODELS
# ─────────────────────────────────────────────

@dataclass
class PositionView:
    label: str
    side: str
    shares: float
    entry: float
    mark: float
    pnl: float
    pnl_pct: float
    seconds_left: float


@dataclass
class TradeView:
    at: datetime | None
    label: str
    side: str
    shares: float
    entry: float
    exit: float
    pnl: float
    reason: str


@dataclass
class SignalView:
    label: str
    side: str
    fair: float
    mid: float
    divergence: float
    edge: float
    confidence: float
    reason: str


@dataclass
class DashboardState:
    """Everything the dashboard draws, snapshotted by the engine each tick."""

    mode: str = "PAPER"
    uptime: float = 0.0
    halted: bool = False
    halt_reason: str = ""

    equity: float = 0.0
    cash: float = 0.0
    starting_equity: float = 0.0
    realized: float = 0.0
    unrealized: float = 0.0
    daily_pnl: float = 0.0
    daily_drawdown: float = 0.0
    max_daily_drawdown: float = 0.0
    exposure: float = 0.0

    stats: TradeStats = field(default_factory=TradeStats)
    positions: list[PositionView] = field(default_factory=list)
    trades: list[TradeView] = field(default_factory=list)
    signals: list[SignalView] = field(default_factory=list)

    markets_tracked: int = 0
    scans: int = 0
    opportunities: int = 0
    orders_sent: int = 0
    binance: dict[str, object] = field(default_factory=dict)
    polymarket: dict[str, object] = field(default_factory=dict)
    telegram: dict[str, object] = field(default_factory=dict)
    last_error: str = ""

    @property
    def total_pnl(self) -> float:
        return self.equity - self.starting_equity


def _fmt_uptime(seconds: float) -> str:
    seconds = int(max(0, seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours:d}h {minutes:02d}m {secs:02d}s"


def _fmt_countdown(seconds: float) -> str:
    if seconds <= 0:
        return "expired"
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes:d}:{secs:02d}"


# ─────────────────────────────────────────────
# RICH RENDERER
# ─────────────────────────────────────────────

class RichDashboard:
    """Live-updating panel layout."""

    def __init__(self, refresh_per_second: float = 4.0):
        self.console = Console()
        self._live: "Live | None" = None
        self.refresh_per_second = refresh_per_second

    def start(self) -> None:
        self._live = Live(
            console=self.console,
            refresh_per_second=self.refresh_per_second,
            screen=False,
            transient=False,
        )
        self._live.start()

    def stop(self) -> None:
        if self._live is not None:
            self._live.stop()
            self._live = None

    def update(self, state: DashboardState) -> None:
        if self._live is None:
            return
        try:
            self._live.update(self._render(state))
        except Exception as exc:  # noqa: BLE001 - drawing must never break trading
            log.debug("dashboard render failed: %s", exc)

    # ── panels ───────────────────────────────────────────────

    def _render(self, state: DashboardState) -> "Group":
        return Group(
            self._header(state),
            self._summary(state),
            self._positions(state),
            self._trades(state),
            self._signals(state),
            self._feeds(state),
        )

    def _header(self, state: DashboardState) -> "Panel":
        mode_style = "bold white on red" if state.mode == "LIVE" else "bold black on green"
        title = Text()
        title.append(f" {state.mode} ", style=mode_style)
        title.append("  Polymarket x Binance latency arb  ", style="bold")
        title.append(f"uptime {_fmt_uptime(state.uptime)}", style="dim")
        if state.halted:
            title.append("   KILL SWITCH ACTIVE ", style="bold white on red")
            title.append(state.halt_reason[:70], style="red")
        return Panel(title, border_style="red" if state.halted else "cyan")

    def _summary(self, state: DashboardState) -> "Table":
        stats = state.stats
        table = Table.grid(expand=True, padding=(0, 2))
        for _ in range(4):
            table.add_column(justify="left")

        def money(value: float) -> Text:
            return Text(f"${value:,.2f}", style="green" if value >= 0 else "red")

        table.add_row(
            Text("Equity", style="dim"), Text(f"${state.equity:,.2f}", style="bold"),
            Text("Total P&L", style="dim"), money(state.total_pnl),
        )
        table.add_row(
            Text("Cash", style="dim"), Text(f"${state.cash:,.2f}"),
            Text("Daily P&L", style="dim"), money(state.daily_pnl),
        )
        table.add_row(
            Text("Exposure", style="dim"), Text(f"${state.exposure:,.2f}"),
            Text("Realized / Unrealized", style="dim"),
            Text(f"${state.realized:,.2f} / ${state.unrealized:,.2f}"),
        )
        dd_style = "red" if state.daily_drawdown >= 0.5 * (state.max_daily_drawdown or 1) else "yellow"
        table.add_row(
            Text("Win rate", style="dim"),
            Text(f"{stats.win_rate:.1%}  ({stats.wins}W / {stats.losses}L)",
                 style="green" if stats.win_rate >= 0.5 else "yellow"),
            Text("Daily drawdown", style="dim"),
            Text(f"{state.daily_drawdown:.2%} of {state.max_daily_drawdown:.2%} limit",
                 style=dd_style),
        )
        table.add_row(
            Text("Trades closed", style="dim"), Text(f"{stats.total}"),
            Text("Profit factor / expectancy", style="dim"),
            Text(f"{stats.profit_factor:.2f} / ${stats.expectancy:,.2f}"),
        )
        return Panel(table, title="Performance", border_style="cyan", title_align="left")

    def _positions(self, state: DashboardState) -> "Panel":
        table = Table(expand=True, box=None, pad_edge=False)
        for name, justify in (
            ("Market", "left"), ("Side", "left"), ("Shares", "right"),
            ("Entry", "right"), ("Mark", "right"), ("P&L", "right"),
            ("%", "right"), ("Expires", "right"),
        ):
            table.add_column(name, justify=justify, no_wrap=True)

        if not state.positions:
            table.add_row(Text("no open positions", style="dim"), "", "", "", "", "", "", "")
        for p in state.positions:
            style = "green" if p.pnl >= 0 else "red"
            table.add_row(
                p.label,
                Text(p.side, style="cyan" if p.side == "UP" else "magenta"),
                f"{p.shares:,.2f}", f"${p.entry:.3f}", f"${p.mark:.3f}",
                Text(f"${p.pnl:,.2f}", style=style),
                Text(f"{p.pnl_pct:+.1%}", style=style),
                _fmt_countdown(p.seconds_left),
            )
        return Panel(table, title=f"Open positions ({len(state.positions)})",
                     border_style="cyan", title_align="left")

    def _trades(self, state: DashboardState) -> "Panel":
        table = Table(expand=True, box=None, pad_edge=False)
        for name, justify in (
            ("Closed", "left"), ("Market", "left"), ("Side", "left"), ("Shares", "right"),
            ("Entry", "right"), ("Exit", "right"), ("P&L", "right"), ("Reason", "left"),
        ):
            table.add_column(name, justify=justify, no_wrap=True)

        if not state.trades:
            table.add_row(Text("no closed trades yet", style="dim"), "", "", "", "", "", "", "")
        for t in state.trades:
            style = "green" if t.pnl >= 0 else "red"
            table.add_row(
                t.at.strftime("%H:%M:%S") if t.at else "-",
                t.label,
                Text(t.side, style="cyan" if t.side == "UP" else "magenta"),
                f"{t.shares:,.2f}", f"${t.entry:.3f}", f"${t.exit:.3f}",
                Text(f"${t.pnl:,.2f}", style=style),
                Text(t.reason[:24], style="dim"),
            )
        return Panel(table, title="Last 10 trades", border_style="cyan", title_align="left")

    def _signals(self, state: DashboardState) -> "Panel":
        table = Table(expand=True, box=None, pad_edge=False)
        for name, justify in (
            ("Market", "left"), ("Side", "left"), ("Fair", "right"), ("Mid", "right"),
            ("Div", "right"), ("Edge", "right"), ("Conf", "right"), ("Status", "left"),
        ):
            table.add_column(name, justify=justify, no_wrap=True)

        if not state.signals:
            table.add_row(Text("scanning...", style="dim"), "", "", "", "", "", "", "")
        for s in state.signals:
            hot = s.reason == "tradeable"
            table.add_row(
                s.label,
                Text(s.side, style="cyan" if s.side == "UP" else "magenta"),
                f"{s.fair:.1%}", f"{s.mid:.1%}",
                Text(f"{s.divergence:+.1%}", style="yellow" if abs(s.divergence) > 0.01 else "dim"),
                Text(f"{s.edge:+.1%}", style="green" if s.edge > 0 else "dim"),
                f"{s.confidence:.0%}",
                Text(s.reason[:34], style="bold green" if hot else "dim"),
            )
        return Panel(table, title=f"Signals ({state.markets_tracked} markets tracked)",
                     border_style="cyan", title_align="left")

    def _feeds(self, state: DashboardState) -> "Panel":
        binance = state.binance
        poly = state.polymarket
        line = Text()
        connected = bool(binance.get("connected"))
        line.append("Binance ", style="dim")
        line.append("● " if connected else "○ ", style="green" if connected else "red")
        prices = binance.get("prices") or {}
        for asset, price in prices.items():
            line.append(f"{asset} ", style="dim")
            line.append(f"{price:,.2f}  " if isinstance(price, (int, float)) else "-  ")
        latency = binance.get("latency_ms") or {}
        if latency:
            line.append("lat ", style="dim")
            line.append(f"{max(latency.values()):.0f}ms  ")
        line.append(f"reconnects {binance.get('reconnects', 0)}   ", style="dim")

        line.append("CLOB ", style="dim")
        circuit_open = bool(poly.get("clob_circuit_open"))
        line.append("● " if not circuit_open else "○ ", style="red" if circuit_open else "green")
        line.append(f"{poly.get('books', 0)} books  ", style="dim")
        line.append(f"age {poly.get('oldest_book_age', 0)}s  ", style="dim")
        line.append(f"{poly.get('clob_calls', 0)} calls / {poly.get('clob_failures', 0)} fail   ",
                    style="dim")
        line.append(f"scans {state.scans}  signals {state.opportunities}  "
                    f"orders {state.orders_sent}", style="dim")
        if state.last_error:
            line.append(f"\nlast error: {state.last_error[:150]}", style="red")
        return Panel(line, title="Feeds", border_style="cyan", title_align="left")


# ─────────────────────────────────────────────
# PLAIN RENDERER
# ─────────────────────────────────────────────

class PlainDashboard:
    """Minimal text output for non-TTY environments or when rich is missing."""

    def __init__(self, stream=None, min_interval: float = 10.0):
        self.stream = stream or sys.stdout
        # Without a live-updating terminal, repainting every second would flood
        # the log, so plain output is throttled.
        self.min_interval = min_interval
        self._last_draw = 0.0

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def update(self, state: DashboardState) -> None:
        now = time.monotonic()
        if now - self._last_draw < self.min_interval:
            return
        self._last_draw = now
        width = shutil.get_terminal_size((100, 30)).columns
        stats = state.stats
        lines = [
            "=" * min(width, 100),
            f"[{state.mode}] uptime {_fmt_uptime(state.uptime)}"
            + ("  *** KILL SWITCH: " + state.halt_reason + " ***" if state.halted else ""),
            f"equity ${state.equity:,.2f} | total P&L ${state.total_pnl:,.2f} | "
            f"daily ${state.daily_pnl:,.2f} | dd {state.daily_drawdown:.2%}/"
            f"{state.max_daily_drawdown:.2%}",
            f"trades {stats.total} | win rate {stats.win_rate:.1%} "
            f"({stats.wins}W/{stats.losses}L) | PF {stats.profit_factor:.2f} | "
            f"open {len(state.positions)} | exposure ${state.exposure:,.2f}",
        ]
        for p in state.positions:
            lines.append(
                f"  OPEN  {p.label:<28} {p.side:<4} {p.shares:>8.2f}sh "
                f"@${p.entry:.3f} mark ${p.mark:.3f} pnl ${p.pnl:,.2f} "
                f"({p.pnl_pct:+.1%}) expires {_fmt_countdown(p.seconds_left)}"
            )
        for t in state.trades[:10]:
            when = t.at.strftime("%H:%M:%S") if t.at else "-"
            lines.append(
                f"  TRADE {when} {t.label:<28} {t.side:<4} "
                f"${t.entry:.3f}->${t.exit:.3f} pnl ${t.pnl:,.2f} [{t.reason}]"
            )
        for s in state.signals[:5]:
            lines.append(
                f"  SIG   {s.label:<28} {s.side:<4} fair {s.fair:.1%} mid {s.mid:.1%} "
                f"div {s.divergence:+.1%} edge {s.edge:+.1%} conf {s.confidence:.0%} "
                f"[{s.reason}]"
            )
        if state.last_error:
            lines.append(f"  ERROR {state.last_error[:120]}")
        print("\n".join(lines), file=self.stream, flush=True)


def build_dashboard(enabled: bool, *, force_plain: bool = False):
    """Pick a renderer: rich on a TTY, plain otherwise, or a no-op when disabled."""
    if not enabled:
        return NullDashboard()
    if force_plain or not RICH_AVAILABLE or not sys.stdout.isatty():
        return PlainDashboard()
    return RichDashboard()


class NullDashboard:
    """Used with --no-dashboard; the engine still logs everything."""

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def update(self, state: DashboardState) -> None:
        return None

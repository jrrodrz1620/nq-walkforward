"""
The trading engine: wires the feeds, model, risk limits, execution, and storage
into one async loop.

Six cooperating tasks run concurrently:

* discovery  -- refresh the universe of live up/down markets and their strikes
* books      -- keep CLOB order books fresh for tracked markets
* scan       -- price every market against Binance and act on tradeable signals
* positions  -- mark, exit early, and settle open positions
* equity     -- mark-to-market, feed the risk manager, persist the equity curve
* dashboard  -- repaint the terminal

Every loop body is individually guarded: a failure in one (a dropped feed, a
CLOB outage) is logged and retried on the next tick rather than taking the bot
down. Only an explicit stop, the run deadline, or a fatal startup error ends the
process.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime

from .config import Config
from .dashboard import DashboardState, PositionView, SignalView, TradeView, build_dashboard
from .execution import Executor, LiveExecutor, build_executor
from .feeds.binance import BinanceFeed
from .feeds.polymarket import ClobGateway, PolymarketFeed
from .kelly import size_position
from .markets import CryptoUpDownMarket
from .models import Position, TradeStats
from .notifier import TelegramNotifier
from .orderbook import round_to_tick
from .pricing import Evaluation, evaluate_market
from .risk import RiskManager
from .storage import TradeStore

log = logging.getLogger(__name__)

#: Keep retrying settlement for this long after expiry before giving up and
#: closing at the last mark.
SETTLEMENT_GRACE_SECONDS = 180.0

#: How often to append a MARK row to a position's history.
MARK_HISTORY_INTERVAL = 30.0


class ArbEngine:
    """Owns all mutable trading state for one bot session."""

    def __init__(
        self,
        cfg: Config,
        *,
        store: TradeStore | None = None,
        notifier: TelegramNotifier | None = None,
        binance: BinanceFeed | None = None,
        polymarket: PolymarketFeed | None = None,
        executor: Executor | None = None,
        dashboard=None,
    ):
        self.cfg = cfg.validate()
        self.store = store or TradeStore(cfg.db_path)
        self.notifier = notifier or TelegramNotifier(cfg)
        self.binance = binance or BinanceFeed(cfg)
        self.gateway = ClobGateway(cfg)
        self.poly = polymarket or PolymarketFeed(cfg, self.gateway)
        self.executor = executor or build_executor(cfg, self.gateway)
        self.dashboard = dashboard if dashboard is not None else build_dashboard(
            cfg.dashboard_enabled
        )

        self.cash = cfg.starting_bankroll
        self.starting_equity = cfg.starting_bankroll
        self.positions: list[Position] = []
        self.realized_total = 0.0
        self.risk = RiskManager(cfg, starting_equity=cfg.starting_bankroll)

        self.persistence: dict[str, int] = {}
        self.latest_signals: list[Evaluation] = []
        self.scans = 0
        self.opportunities = 0
        self.orders_sent = 0
        self.last_error = ""
        self.started_at = time.monotonic()
        self._last_mark_history = 0.0
        self._was_halted = False
        self._stop = asyncio.Event()
        self._settlement_failures: dict[int, int] = {}

    # ─────────────────────────────────────────
    # LIFECYCLE
    # ─────────────────────────────────────────

    async def run(self) -> None:
        """Start every subsystem, run until stopped, then shut down cleanly."""
        await self._startup()
        loops = [
            asyncio.create_task(self._loop(self._discovery_tick, self.cfg.market_refresh_interval,
                                           "discovery"), name="discovery"),
            asyncio.create_task(self._loop(self._books_tick, self.cfg.book_poll_interval,
                                           "books"), name="books"),
            asyncio.create_task(self._loop(self._scan_tick, self.cfg.scan_interval,
                                           "scan"), name="scan"),
            asyncio.create_task(self._loop(self._positions_tick, 1.0, "positions"),
                                name="positions"),
            asyncio.create_task(self._loop(self._equity_tick, 5.0, "equity"), name="equity"),
            asyncio.create_task(self._loop(self._dashboard_tick, self.cfg.dashboard_interval,
                                           "dashboard"), name="dashboard"),
        ]
        if self.cfg.run_seconds > 0:
            loops.append(asyncio.create_task(self._deadline(self.cfg.run_seconds),
                                             name="deadline"))
        try:
            await self._stop.wait()
        finally:
            for task in loops:
                task.cancel()
            await asyncio.gather(*loops, return_exceptions=True)
            await self._shutdown()

    def stop(self, reason: str = "stop requested") -> None:
        """Ask the engine to wind down. Safe to call from a signal handler."""
        if not self._stop.is_set():
            log.info("shutting down: %s", reason)
            self._stop.set()

    async def _deadline(self, seconds: float) -> None:
        await asyncio.sleep(seconds)
        self.stop(f"run deadline of {seconds:.0f}s reached")

    async def _startup(self) -> None:
        log.info("starting engine in %s mode\n%s", self.cfg.mode, self.cfg.describe())
        self.store.record_event("startup", f"engine starting in {self.cfg.mode} mode",
                                payload={"mode": self.cfg.mode})

        await self.notifier.start()
        self.notifier.startup(self.cfg)

        await self.binance.start()
        await self.poly.start()

        if not await self.binance.wait_ready(timeout=30.0):
            log.warning("Binance feed not fully warmed up after 30s; continuing")

        await self._restore_state()

        await self._discovery_tick()
        await self._books_tick()

        self.dashboard.start()
        log.info("engine ready: %d markets tracked, equity $%.2f",
                 len(self.poly.markets), self.equity())

    async def _restore_state(self) -> None:
        """Rebuild cash and open positions from the database (and, live, the chain)."""
        self.positions = self.store.open_positions()
        self.realized_total = self.store.total_realized()

        balance = None
        if isinstance(self.executor, LiveExecutor):
            balance = await self.executor.available_balance()

        if balance is not None:
            self.cash = balance
            log.info("live USDC balance: $%.2f", balance)
        else:
            committed = sum(p.cost_basis for p in self.positions)
            self.cash = self.cfg.starting_bankroll + self.realized_total - committed

        self.starting_equity = self.cfg.starting_bankroll
        self.risk = RiskManager(self.cfg, starting_equity=self.equity())
        if self.positions:
            log.info("restored %d open position(s) worth $%.2f at cost",
                     len(self.positions), sum(p.cost_basis for p in self.positions))

    async def _shutdown(self) -> None:
        log.info("engine stopping")
        try:
            if isinstance(self.executor, LiveExecutor):
                await self.executor.cancel_all()
        except Exception as exc:  # noqa: BLE001 - keep shutting down regardless
            log.error("failed to cancel resting orders on shutdown: %s", exc)

        self.dashboard.stop()
        try:
            self._persist_equity()
        except Exception as exc:  # noqa: BLE001
            log.error("final equity snapshot failed: %s", exc)

        stats = self.store.stats()
        self.notifier.shutdown(stats, self.equity())
        self.store.record_event(
            "shutdown",
            f"engine stopped; equity ${self.equity():,.2f}, {stats.total} closed trades",
        )

        await self.binance.stop()
        await self.poly.stop()
        await self.notifier.stop()
        self.store.close()
        log.info("engine stopped: equity $%.2f, %d closed trades, win rate %.1f%%",
                 self.equity(), stats.total, stats.win_rate * 100)

    # ─────────────────────────────────────────
    # LOOP SCAFFOLDING
    # ─────────────────────────────────────────

    async def _loop(self, tick, interval: float, name: str) -> None:
        """Run `tick` every `interval` seconds until cancelled, surviving errors."""
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                await tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - one bad tick must not stop the bot
                self.last_error = f"{name}: {type(exc).__name__}: {exc}"
                log.exception("error in %s loop", name)
                self.store.record_event("loop_error", self.last_error, severity="warning")
            elapsed = time.monotonic() - started
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=max(0.05, interval - elapsed)
                )
                return
            except asyncio.TimeoutError:
                continue

    # ─────────────────────────────────────────
    # TICKS
    # ─────────────────────────────────────────

    async def _discovery_tick(self) -> None:
        markets = await self.poly.discover()
        if markets:
            await self.poly.resolve_strikes(self._resolve_strike)
        # Drop persistence counters for markets that have rolled off.
        live = {m.condition_id for m in self.poly.markets.values()}
        for key in list(self.persistence):
            if key.split(":", 1)[0] not in live:
                self.persistence.pop(key, None)

    async def _resolve_strike(self, market: CryptoUpDownMarket) -> float | None:
        if market.open_time is None:
            return None
        return await self.binance.window_open_price(market.asset, market.open_time)

    async def _books_tick(self) -> None:
        await self.poly.refresh_books(self._tracked_markets())

    def _tracked_markets(self) -> list[CryptoUpDownMarket]:
        """Markets worth polling: inside the trade horizon, or already held."""
        held = {p.condition_id for p in self.positions}
        now = datetime.now(UTC)
        tracked = []
        for market in self.poly.markets.values():
            remaining = market.seconds_to_expiry(now)
            if market.condition_id in held and remaining > -SETTLEMENT_GRACE_SECONDS:
                tracked.append(market)
            elif 0 < remaining <= self.cfg.max_seconds_to_expiry:
                tracked.append(market)
        return tracked

    async def _scan_tick(self) -> None:
        self.scans += 1
        now = datetime.now(UTC)
        equity = self.equity()
        reference_notional = max(1.0, self.cfg.max_position_pct * equity)
        signals: list[Evaluation] = []

        for market in self._tracked_markets():
            spot = self.binance.price(market.asset)
            if spot is None or market.strike is None:
                continue
            evaluation = evaluate_market(
                market,
                cfg=self.cfg,
                spot=spot,
                sigma=self.binance.sigma(market.asset),
                books=self.poly.books_for(market),
                binance_age=self.binance.age(market.asset),
                vol_progress=self.binance.vol_progress(market.asset),
                reference_notional=reference_notional,
                persistence=self.persistence,
                now=now,
            )
            if evaluation is None:
                continue
            signals.append(evaluation)

            key = evaluation.key
            if evaluation.divergence >= self.cfg.min_divergence:
                self.persistence[key] = self.persistence.get(key, 0) + 1
            else:
                self.persistence.pop(key, None)

            if evaluation.tradeable:
                self.opportunities += 1
                await self._attempt_trade(evaluation)

        signals.sort(key=lambda e: (e.tradeable, e.edge), reverse=True)
        self.latest_signals = signals[:8]

    async def _positions_tick(self) -> None:
        now = datetime.now(UTC)
        record_history = (time.monotonic() - self._last_mark_history) >= MARK_HISTORY_INTERVAL
        if record_history:
            self._last_mark_history = time.monotonic()

        for position in list(self.positions):
            book = self.poly.book(position.token_id)
            mark = book.mid if book is not None else None
            remaining = position.seconds_to_expiry(now)

            if record_history and mark is not None:
                self.store.record_history(
                    position, event="MARK", mark_price=mark,
                    spot=self.binance.price(position.asset),
                )

            if remaining <= 0:
                await self._settle(position, now)
                continue

            if mark is None or book is None:
                continue

            # In live mode, prefer selling out just before expiry: settlement
            # requires on-chain redemption, an exit does not.
            if (
                self.cfg.live
                and remaining <= self.cfg.live_exit_before_expiry
                and book.bids
            ):
                await self._exit(position, "pre_expiry_exit")
                continue

            if mark >= self.cfg.take_profit and mark > position.entry_price:
                await self._exit(position, "take_profit")
            elif mark <= self.cfg.stop_loss and mark < position.entry_price:
                await self._exit(position, "stop_loss")

    async def _equity_tick(self) -> None:
        equity = self.equity()
        alerts = self.risk.update_equity(equity)
        for alert in alerts:
            log.log(
                logging.CRITICAL if alert.severity == "critical" else logging.WARNING,
                "%s", alert.message,
            )
            self.notifier.risk_alert(alert)
            self.store.record_event(alert.kind, alert.message, severity=alert.severity,
                                    payload={"equity": alert.equity, "drawdown": alert.drawdown})

        if self.risk.halted and not self._was_halted:
            self._was_halted = True
            await self._on_halt()
        elif not self.risk.halted and self._was_halted:
            self._was_halted = False

        self._persist_equity()

    async def _on_halt(self) -> None:
        """Kill switch just tripped: stop entering, and pull any resting orders."""
        log.critical("KILL SWITCH ACTIVE -- no new positions will be opened")
        if isinstance(self.executor, LiveExecutor):
            try:
                await self.executor.cancel_all()
            except Exception as exc:  # noqa: BLE001 - already in an alarm state
                log.error("cancel_all failed after kill switch: %s", exc)
                self.notifier.error(f"cancel_all failed after kill switch: {exc}")

    async def _dashboard_tick(self) -> None:
        self.dashboard.update(self.build_state())

    # ─────────────────────────────────────────
    # TRADING
    # ─────────────────────────────────────────

    async def _attempt_trade(self, evaluation: Evaluation) -> None:
        market = evaluation.market
        book = self.poly.book(evaluation.token_id)
        if book is None:
            return

        equity = self.equity()
        exposure = self.open_exposure()
        budget = self.risk.exposure_budget(exposure)

        limit_price = round_to_tick(
            min(0.999, evaluation.entry_price + self.cfg.slippage),
            market.tick_size,
            mode="up",
        )
        available = book.notional_available(limit_price, side="BUY")

        size = size_position(
            prob=evaluation.fair_prob,
            price=min(0.999, evaluation.effective_cost),
            equity=equity,
            cfg=self.cfg,
            available_notional=available,
            exposure_budget=min(budget, self.cash),
            min_order_size=market.min_order_size,
        )
        if not size.ok:
            log.debug("skipping %s: %s", evaluation.describe(), size.reason)
            return

        decision = self.risk.check_new_position(
            notional=size.notional,
            open_exposure=exposure,
            open_positions=len(self.positions),
            market_already_held=any(
                p.condition_id == market.condition_id for p in self.positions
            ),
            feeds_healthy=self.feeds_healthy(),
        )
        if not decision:
            log.info("risk blocked %s: %s", market.label, decision.reason)
            self.store.record_event("risk_block", f"{market.label}: {decision.reason}",
                                    severity="info")
            return

        if size.notional > self.cash + 1e-9:
            log.info("insufficient cash for %s: need $%.2f, have $%.2f",
                     market.label, size.notional, self.cash)
            return

        log.info("ENTER %s | %s", evaluation.describe(), size.describe())
        self.orders_sent += 1
        fill = await self.executor.open(
            token_id=evaluation.token_id,
            shares=size.shares,
            limit_price=limit_price,
            book=book,
        )

        if not fill.filled:
            self.store.record_fill(fill)
            self.store.record_event(
                "order_unfilled",
                f"{market.label} {evaluation.side}: {fill.status} ({fill.detail})",
                severity="info",
            )
            log.info("order not filled for %s: %s (%s)", market.label, fill.status, fill.detail)
            return

        position = Position(
            condition_id=market.condition_id,
            market_label=market.label,
            asset=market.asset,
            window_minutes=market.window_minutes,
            side=evaluation.side,
            token_id=evaluation.token_id,
            shares=fill.shares,
            entry_price=fill.price,
            entry_fees=fill.fees,
            close_time=market.close_time,
            strike=market.strike or 0.0,
            spot_at_entry=evaluation.spot,
            fair_prob=evaluation.fair_prob,
            market_mid=evaluation.market_mid,
            divergence=evaluation.divergence,
            edge=evaluation.edge,
            confidence=evaluation.confidence,
            kelly_fraction=size.kelly_scaled,
            sigma=evaluation.sigma,
            seconds_left_at_entry=evaluation.seconds_left,
            mode=self.executor.mode,
            entry_order_id=fill.order_id,
        )
        self.cash -= fill.cost
        self.store.insert_position(position)
        self.store.record_fill(fill, position.id)
        self.positions.append(position)
        self.persistence.pop(evaluation.key, None)
        self.notifier.trade_opened(position)
        log.info("OPENED %s at $%.3f (cash now $%.2f)",
                 position.describe(), fill.price, self.cash)

    async def _exit(self, position: Position, reason: str) -> None:
        """Sell a position into the book before expiry."""
        book = self.poly.book(position.token_id)
        if book is None or not book.bids:
            return
        limit_price = round_to_tick(
            max(0.001, (book.best_bid or 0.001) - self.cfg.slippage),
            book.tick_size,
            mode="down",
        )
        fill = await self.executor.close(
            position=position, shares=position.shares, limit_price=limit_price, book=book
        )
        self.store.record_fill(fill, position.id)
        if not fill.filled:
            log.debug("exit attempt for %s did not fill: %s", position.describe(), fill.detail)
            return

        # A partial exit is treated as a full close of the filled quantity; the
        # remainder stays open and will settle at expiry.
        if fill.shares < position.shares - 1e-9:
            remainder = position.shares - fill.shares
            log.info("partial exit on %s: sold %.2f, %.2f left to settle",
                     position.describe(), fill.shares, remainder)
            position.shares = fill.shares

        pnl = position.close(
            exit_price=fill.price, reason=reason, fees=fill.fees, order_id=fill.order_id
        )
        self.cash += fill.notional - fill.fees
        self._finalize(position, pnl, spot=self.binance.price(position.asset))

    async def _settle(self, position: Position, now: datetime) -> None:
        """Settle an expired contract against the underlying's closing price."""
        assert position.close_time is not None
        overdue = (now - position.close_time).total_seconds()
        settlement = await self.binance.settlement_price(position.asset, position.close_time)

        if settlement is None:
            failures = self._settlement_failures.get(position.id or 0, 0) + 1
            self._settlement_failures[position.id or 0] = failures
            if overdue < SETTLEMENT_GRACE_SECONDS:
                return  # try again next tick
            book = self.poly.book(position.token_id)
            mark = (book.mid if book is not None else None) or position.entry_price
            pnl = position.close(exit_price=mark, reason="settlement_unavailable")
            self.cash += position.shares * mark
            log.error("could not resolve settlement for %s after %.0fs; closed at mark $%.3f",
                      position.describe(), overdue, mark)
            self.notifier.error(
                f"Settlement price unavailable for {position.market_label}; "
                f"closed at last mark ${mark:.3f}"
            )
            self._finalize(position, pnl, spot=None)
            return

        finished_up = settlement > position.strike
        won = (position.side == "UP") == finished_up
        exit_price = 1.0 if won else 0.0
        pnl = position.close(
            exit_price=exit_price, reason="settled", at=now, settlement_price=settlement
        )
        self.cash += position.shares * exit_price
        if self.cfg.live and won:
            log.info("live position settled in the money; USDC arrives on redemption "
                     "when the market resolves on-chain")
        self._finalize(position, pnl, spot=settlement)

    def _finalize(self, position: Position, pnl: float, *, spot: float | None) -> None:
        """Common bookkeeping for any closed position."""
        self.realized_total += pnl
        self.risk.record_trade_result(pnl)
        self.store.close_position(position, spot=spot)
        self._settlement_failures.pop(position.id or 0, None)
        self.positions = [p for p in self.positions if p is not position]
        self.notifier.trade_closed(position, self.equity())
        log.info("CLOSED %s -> $%.3f (%s) P&L $%.2f | cash $%.2f",
                 position.describe(), position.exit_price or 0.0,
                 position.exit_reason, pnl, self.cash)

    # ─────────────────────────────────────────
    # ACCOUNTING
    # ─────────────────────────────────────────

    def mark_price(self, position: Position) -> float:
        """Best available mark for a position: book mid, else its entry price."""
        book = self.poly.book(position.token_id)
        mid = book.mid if book is not None else None
        return mid if mid is not None else position.entry_price

    def open_exposure(self) -> float:
        return sum(p.cost_basis for p in self.positions)

    def open_value(self) -> float:
        return sum(p.market_value(self.mark_price(p)) for p in self.positions)

    def unrealized(self) -> float:
        return sum(p.unrealized_pnl(self.mark_price(p)) for p in self.positions)

    def equity(self) -> float:
        return self.cash + self.open_value()

    def feeds_healthy(self) -> bool:
        return self.binance.is_healthy() and self.poly.is_healthy()

    def _persist_equity(self) -> None:
        self.store.record_equity(
            equity=self.equity(),
            cash=self.cash,
            open_exposure=self.open_exposure(),
            unrealized=self.unrealized(),
            realized_total=self.realized_total,
            daily_drawdown=self.risk.daily_drawdown,
            open_positions=len(self.positions),
            halted=self.risk.halted,
        )

    # ─────────────────────────────────────────
    # DASHBOARD STATE
    # ─────────────────────────────────────────

    def build_state(self) -> DashboardState:
        now = datetime.now(UTC)
        stats: TradeStats = self.store.stats()
        positions = [
            PositionView(
                label=p.market_label,
                side=p.side,
                shares=p.shares,
                entry=p.entry_price,
                mark=self.mark_price(p),
                pnl=p.unrealized_pnl(self.mark_price(p)),
                pnl_pct=p.unrealized_pct(self.mark_price(p)),
                seconds_left=p.seconds_to_expiry(now),
            )
            for p in self.positions
        ]
        trades = [
            TradeView(
                at=t.exit_time,
                label=t.market_label,
                side=t.side,
                shares=t.shares,
                entry=t.entry_price,
                exit=t.exit_price or 0.0,
                pnl=t.realized_pnl or 0.0,
                reason=t.exit_reason,
            )
            for t in self.store.recent_trades(10)
        ]
        signals = [
            SignalView(
                label=e.market.label,
                side=e.side,
                fair=e.fair_prob,
                mid=e.market_mid,
                divergence=e.divergence,
                edge=e.edge,
                confidence=e.confidence,
                reason=e.reason,
            )
            for e in self.latest_signals
        ]
        return DashboardState(
            mode=self.cfg.mode,
            uptime=time.monotonic() - self.started_at,
            halted=self.risk.halted,
            halt_reason=self.risk.halt_reason,
            equity=self.equity(),
            cash=self.cash,
            starting_equity=self.starting_equity,
            realized=self.realized_total,
            unrealized=self.unrealized(),
            daily_pnl=self.risk.daily_pnl,
            daily_drawdown=self.risk.daily_drawdown,
            max_daily_drawdown=self.cfg.max_daily_drawdown,
            exposure=self.open_exposure(),
            stats=stats,
            positions=positions,
            trades=trades,
            signals=signals,
            markets_tracked=len(self.poly.markets),
            scans=self.scans,
            opportunities=self.opportunities,
            orders_sent=self.orders_sent,
            binance=self.binance.status(),
            polymarket=self.poly.status(),
            telegram=self.notifier.status(),
            last_error=self.last_error,
        )

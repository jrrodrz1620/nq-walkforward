"""
Telegram alerting.

Messages go through a bounded queue drained by a single worker, so a slow or
rate-limited Telegram API can never block the trading loop. Sends are paced by a
token bucket (Telegram allows roughly 20 messages per minute to one chat),
retried with backoff, and honour the `retry_after` hint on HTTP 429.

When no bot token is configured the notifier degrades to logging, so the rest of
the code can call it unconditionally.
"""
from __future__ import annotations

import asyncio
import html
import logging
import time
from dataclasses import dataclass

import httpx

from .config import Config
from .models import Position, TradeStats
from .ratelimit import RetryPolicy, TokenBucket, retry_async
from .risk import RiskAlert

log = logging.getLogger(__name__)

SEVERITY_ICONS = {
    "info": "ℹ️",
    "success": "✅",
    "warning": "⚠️",
    "critical": "\U0001f6a8",
}


@dataclass
class QueuedMessage:
    text: str
    severity: str = "info"
    queued_at: float = 0.0


class TelegramNotifier:
    """Fire-and-forget Telegram alerts with pacing and retries."""

    QUEUE_LIMIT = 200
    #: Identical messages inside this window are suppressed.
    DEDUP_SECONDS = 30.0

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.token = cfg.telegram_token
        self.chat_id = cfg.telegram_chat_id
        self.enabled = cfg.telegram_enabled
        self.bucket = TokenBucket(
            cfg.telegram_per_minute / 60.0, capacity=5.0, name="telegram"
        )
        self.sent = 0
        self.dropped = 0
        self.failed = 0
        self.last_error = ""

        self._queue: asyncio.Queue[QueuedMessage] = asyncio.Queue(maxsize=self.QUEUE_LIMIT)
        self._task: asyncio.Task[None] | None = None
        self._client: httpx.AsyncClient | None = None
        self._recent: dict[str, float] = {}

    # ── lifecycle ────────────────────────────────────────────

    async def start(self) -> None:
        if not self.enabled:
            log.info("Telegram alerts disabled (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID unset)")
            return
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(15.0))
        self._task = asyncio.create_task(self._worker(), name="telegram-worker")
        log.info("Telegram alerts enabled for chat %s", self.chat_id)

    async def stop(self, *, drain_timeout: float = 5.0) -> None:
        if self._task is not None:
            try:
                await asyncio.wait_for(self._queue.join(), timeout=drain_timeout)
            except asyncio.TimeoutError:
                log.warning("Telegram queue did not drain within %.0fs", drain_timeout)
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ── queueing ─────────────────────────────────────────────

    def send(self, text: str, severity: str = "info") -> None:
        """Queue a message. Safe to call from anywhere; never raises."""
        icon = SEVERITY_ICONS.get(severity, "")
        body = f"{icon} {text}".strip()
        if not self.enabled:
            log.info("[telegram:%s] %s", severity, text.replace("\n", " | ")[:400])
            return

        now = time.monotonic()
        last = self._recent.get(body)
        if last is not None and now - last < self.DEDUP_SECONDS:
            return
        self._recent[body] = now
        if len(self._recent) > 512:  # keep the dedup map from growing forever
            cutoff = now - self.DEDUP_SECONDS
            self._recent = {k: v for k, v in self._recent.items() if v >= cutoff}

        try:
            self._queue.put_nowait(QueuedMessage(body, severity, now))
        except asyncio.QueueFull:
            self.dropped += 1
            log.warning("Telegram queue full; dropped a %s alert", severity)

    # ── worker ───────────────────────────────────────────────

    async def _worker(self) -> None:
        while True:
            message = await self._queue.get()
            try:
                await self._deliver(message)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - alerting must not kill the bot
                self.failed += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("Telegram delivery failed: %s", self.last_error)
            finally:
                self._queue.task_done()

    async def _deliver(self, message: QueuedMessage) -> None:
        assert self._client is not None
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": message.text[:4096],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }

        async def _post() -> None:
            await self.bucket.acquire()
            assert self._client is not None
            response = await self._client.post(url, json=payload)
            if response.status_code == 429:
                retry_after = 1.0
                try:
                    retry_after = float(
                        response.json().get("parameters", {}).get("retry_after", 1.0)
                    )
                except Exception:  # noqa: BLE001 - malformed body, use the default
                    pass
                await asyncio.sleep(min(retry_after, 30.0))
                raise ConnectionError("telegram rate limited (429)")
            response.raise_for_status()

        await retry_async(
            _post,
            policy=RetryPolicy(attempts=3, base_delay=1.0, max_delay=10.0),
            description="telegram sendMessage",
        )
        self.sent += 1

    # ── formatted alerts ─────────────────────────────────────

    @staticmethod
    def _esc(text: object) -> str:
        return html.escape(str(text), quote=False)

    def startup(self, cfg: Config) -> None:
        mode = "LIVE TRADING" if cfg.live else "paper trading"
        self.send(
            f"<b>Polymarket arb bot started</b> ({self._esc(mode)})\n"
            f"<pre>{self._esc(cfg.describe())}</pre>",
            "critical" if cfg.live else "info",
        )

    def shutdown(self, stats: TradeStats, equity: float) -> None:
        self.send(
            f"<b>Bot stopped</b>\n"
            f"Equity: ${equity:,.2f}\n"
            f"Closed trades: {stats.total} | Win rate: {stats.win_rate:.1%}\n"
            f"Net P&amp;L: ${stats.net_pnl:,.2f}",
            "info",
        )

    def trade_opened(self, position: Position) -> None:
        self.send(
            f"<b>OPEN {self._esc(position.side)}</b> {self._esc(position.market_label)}"
            f" [{self._esc(position.mode)}]\n"
            f"{position.shares:.2f} shares @ ${position.entry_price:.3f}"
            f" = ${position.cost_basis:,.2f}\n"
            f"Fair {position.fair_prob:.1%} vs mid {position.market_mid:.1%}"
            f" (edge {position.edge:.1%}, conf {position.confidence:.0%})\n"
            f"Spot ${position.spot_at_entry:,.2f} vs strike ${position.strike:,.2f}",
            "success",
        )

    def trade_closed(self, position: Position, equity: float) -> None:
        pnl = position.realized_pnl or 0.0
        icon = "success" if pnl > 0 else "warning"
        settlement = (
            f"\nSettled at ${position.settlement_price:,.2f}"
            if position.settlement_price else ""
        )
        self.send(
            f"<b>CLOSE {self._esc(position.side)}</b> {self._esc(position.market_label)}"
            f" [{self._esc(position.mode)}]\n"
            f"Exit ${(position.exit_price or 0.0):.3f} ({self._esc(position.exit_reason)})\n"
            f"P&amp;L: <b>${pnl:,.2f}</b> ({position.unrealized_pct(position.exit_price or 0.0):+.1%})"
            f"{settlement}\n"
            f"Equity: ${equity:,.2f}",
            icon,
        )

    def risk_alert(self, alert: RiskAlert) -> None:
        self.send(f"<b>{self._esc(alert.kind.replace('_', ' ').title())}</b>\n"
                  f"{self._esc(alert.message)}", alert.severity)

    def error(self, message: str) -> None:
        self.send(f"<b>Error</b>\n<pre>{self._esc(message)[:1500]}</pre>", "warning")

    def status(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "sent": self.sent,
            "failed": self.failed,
            "dropped": self.dropped,
            "queued": self._queue.qsize(),
            "last_error": self.last_error,
        }

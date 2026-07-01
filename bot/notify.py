"""
Telegram notifications (optional).

Best-effort alerts on fills, rejections, and critical errors. Sending is fully
isolated: if Telegram is slow or down it must never affect order processing, so
every failure is swallowed and the trade path continues regardless.

Enable by setting ``BOT_TELEGRAM_TOKEN`` + ``BOT_TELEGRAM_CHAT_ID``.
"""
from __future__ import annotations

from typing import Optional

import httpx


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str,
                 client: Optional[httpx.Client] = None, timeout: float = 5.0,
                 enabled: Optional[bool] = None):
        self.token = token
        self.chat_id = chat_id
        self.enabled = bool(token and chat_id) if enabled is None else enabled
        self._client = client or httpx.Client(timeout=timeout)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def send(self, text: str) -> bool:
        """Post a message. Returns True on success; never raises."""
        if not self.enabled:
            return False
        try:
            resp = self._client.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={"chat_id": self.chat_id, "text": text,
                      "parse_mode": "HTML", "disable_web_page_preview": True},
            )
            return resp.status_code < 400
        except Exception:
            return False


_EMOJI = {"filled": "✅", "rejected": "⛔", "error": "🚨", "duplicate": "♻️"}


def format_result(payload, result) -> str:
    """Human-readable Telegram message for an engine result."""
    emoji = _EMOJI.get(result.status, "ℹ️")
    head = (f"{emoji} <b>{result.status.upper()}</b> — "
            f"{payload.action.upper()} {payload.quantity} {payload.symbol.upper()}")
    lines = [head, f"signal price: {payload.price}"]
    if result.fill_price:
        lines.append(f"fill: {result.fill_price}")
    if result.code and result.code not in ("filled",):
        lines.append(f"reason: {result.code}")
    if result.detail:
        lines.append(result.detail)
    return "\n".join(lines)

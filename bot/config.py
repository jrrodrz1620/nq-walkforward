"""
Hard-coded risk + service configuration.

The safeguards required by the spec are encoded here as defaults so they are
*hard limits*, not knobs that can be quietly disabled. They can still be
overridden per-instance (mostly for tests) but the production defaults are the
contract:

* ``max_contracts_per_instrument = 2``
* ``daily_loss_limit = 1000.0`` (USD, marked-to-market)
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class RiskConfig:
    # Never hold more than this many contracts (absolute net) per instrument.
    max_contracts_per_instrument: int = 2
    # Halt new entries once the day's marked-to-market PnL hits -$1,000.
    daily_loss_limit: float = 1_000.0
    # Reject an order if its initial margin would consume more than this
    # fraction of available equity (a "safe" margin threshold).
    max_margin_utilization: float = 0.50


@dataclass(frozen=True)
class RetryConfig:
    max_retries: int = 3          # truncated exponential backoff: max 3 retries
    base_delay: float = 0.5       # seconds
    max_delay: float = 8.0        # truncated ceiling
    backoff_factor: float = 2.0


@dataclass(frozen=True)
class AppConfig:
    passphrase: str = "change-me"
    # Which execution backend to route approved orders to.
    #   "stub"        -> built-in Tradovate-style mock (demo/testing)
    #   "traderspost" -> POST to a TradersPost webhook (bridges to your broker)
    broker_type: str = "stub"
    broker_base_url: str = "https://demo.tradovate.example/v1"
    broker_token: str = "demo-token"
    # TradersPost webhook URL (https://webhooks.traderspost.io/trading/webhook/...).
    traderspost_webhook_url: str = ""
    # TradersPost doesn't expose live equity on the webhook path, so the margin
    # guardrail runs off this configured account equity (set to your paper bal).
    account_equity: float = 50_000.0
    db_path: str = "bot_state.db"
    error_log_path: str = "error_log.json"
    # Identical webhooks arriving within this window are treated as duplicates.
    dedup_window_seconds: float = 300.0
    risk: RiskConfig = RiskConfig()
    retry: RetryConfig = RetryConfig()

    @classmethod
    def from_env(cls) -> "AppConfig":
        """Build config from environment variables (used by the live server)."""
        return cls(
            passphrase=os.getenv("BOT_PASSPHRASE", cls.passphrase),
            broker_type=os.getenv("BOT_BROKER_TYPE", cls.broker_type),
            broker_base_url=os.getenv("BOT_BROKER_URL", cls.broker_base_url),
            broker_token=os.getenv("BOT_BROKER_TOKEN", cls.broker_token),
            traderspost_webhook_url=os.getenv(
                "BOT_TRADERSPOST_WEBHOOK_URL", cls.traderspost_webhook_url),
            account_equity=float(
                os.getenv("BOT_ACCOUNT_EQUITY", cls.account_equity)),
            db_path=os.getenv("BOT_DB_PATH", cls.db_path),
            error_log_path=os.getenv("BOT_ERROR_LOG", cls.error_log_path),
        )

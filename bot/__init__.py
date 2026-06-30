"""
TradingView → Futures execution bot.

A small, production-shaped FastAPI service that accepts TradingView webhook
alerts and routes them to a futures broker, with hard risk limits, tick/margin
math, SQLite-backed state + idempotency, and exponential-backoff recovery.

See ``bot/README.md`` for the full architecture and the validation gates this
package satisfies.
"""

__all__ = [
    "contracts",
    "pricing",
    "risk",
    "state",
    "broker",
    "errors",
    "engine",
    "models",
]

__version__ = "1.0.0"

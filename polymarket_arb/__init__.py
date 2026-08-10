"""
Polymarket <-> Binance latency arbitrage bot.

Watches Polymarket's short-dated BTC/ETH "up or down" binary contracts and
prices them against a live Binance spot feed. When the Polymarket book lags the
centralised exchange by enough to be worth trading, the engine sizes a position
with a fractional Kelly stake and (in live mode) works an order through the
Polymarket CLOB.

The package defaults to paper trading. Live trading requires three explicit
opt-in flags -- see `polymarket_arb.config.LiveTradingGate`.
"""
from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]

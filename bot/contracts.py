"""
Futures contract specifications.

Each :class:`ContractSpec` carries the numbers an execution layer actually needs:

* ``multiplier``     — USD value of one full point of price movement.
* ``tick_size``      — minimum price increment, expressed in points.
* ``initial_margin`` — exchange/broker initial margin per contract (USD).

The dollar value of a single tick is therefore ``multiplier * tick_size`` and is
exposed as :attr:`ContractSpec.tick_value`.

Values mirror the conventions already used by this repo's backtester
(NQ=20, MNQ=2, ES=50, MES=5) and round out a few common products. Margins are
representative day-trade/initial figures; they are configurable per deployment.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


class UnknownContractError(ValueError):
    """Raised when a symbol has no registered :class:`ContractSpec`."""


@dataclass(frozen=True)
class ContractSpec:
    symbol: str
    name: str
    multiplier: float       # USD per full point
    tick_size: float        # minimum price increment, in points
    initial_margin: float   # USD initial margin per contract

    @property
    def tick_value(self) -> float:
        """USD value of one tick (``multiplier * tick_size``)."""
        return self.multiplier * self.tick_size

    def __post_init__(self) -> None:
        if self.multiplier <= 0:
            raise ValueError("multiplier must be positive")
        if self.tick_size <= 0:
            raise ValueError("tick_size must be positive")
        if self.initial_margin < 0:
            raise ValueError("initial_margin cannot be negative")


# Registry keyed by root symbol.
CONTRACTS: dict[str, ContractSpec] = {
    "ES":  ContractSpec("ES",  "E-mini S&P 500",        50.0, 0.25, 13_200.0),
    "MES": ContractSpec("MES", "Micro E-mini S&P 500",   5.0, 0.25,  1_320.0),
    "NQ":  ContractSpec("NQ",  "E-mini Nasdaq-100",      20.0, 0.25, 16_500.0),
    "MNQ": ContractSpec("MNQ", "Micro E-mini Nasdaq-100", 2.0, 0.25,  1_650.0),
    "YM":  ContractSpec("YM",  "E-mini Dow",              5.0, 1.0,   8_800.0),
    "MYM": ContractSpec("MYM", "Micro E-mini Dow",        0.5, 1.0,     880.0),
    "RTY": ContractSpec("RTY", "E-mini Russell 2000",    50.0, 0.10,  7_700.0),
    "M2K": ContractSpec("M2K", "Micro E-mini Russell",    5.0, 0.10,    770.0),
    "CL":  ContractSpec("CL",  "Crude Oil",           1_000.0, 0.01,  6_600.0),
    "MCL": ContractSpec("MCL", "Micro Crude Oil",       100.0, 0.01,    660.0),
    "GC":  ContractSpec("GC",  "Gold",                  100.0, 0.10, 11_000.0),
    "MGC": ContractSpec("MGC", "Micro Gold",             10.0, 0.10,  1_100.0),
}


# Trailing contract-month / year noise, e.g. "ESM2024", "NQ-Z25", "MNQ!1".
_ROOT_RE = re.compile(r"^([A-Z]+?)(?:[0-9]+|[FGHJKMNQUVXZ][0-9]{1,2}|!.*)?$")


def normalize_symbol(symbol: str) -> str:
    """Return the bare root symbol for a (possibly month-coded) ticker.

    ``"ESM2024" -> "ES"``, ``"MNQ" -> "MNQ"``, ``"nq" -> "NQ"``.
    """
    if not symbol or not symbol.strip():
        raise UnknownContractError("empty symbol")
    raw = symbol.strip().upper().replace("/", "").replace("-", "")
    # Exact match wins (handles roots that *are* registered, e.g. "MES").
    if raw in CONTRACTS:
        return raw
    m = _ROOT_RE.match(raw)
    if m and m.group(1) in CONTRACTS:
        return m.group(1)
    # Longest registered prefix as a final fallback.
    for length in range(len(raw), 0, -1):
        if raw[:length] in CONTRACTS:
            return raw[:length]
    raise UnknownContractError(f"no contract spec registered for symbol {symbol!r}")


def get_contract(symbol: str) -> ContractSpec:
    """Look up the :class:`ContractSpec` for a symbol (month code tolerated)."""
    return CONTRACTS[normalize_symbol(symbol)]

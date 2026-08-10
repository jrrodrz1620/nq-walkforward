"""
Parsing and modelling of Polymarket's short-dated crypto up/down contracts.

The CLOB returns a generic market payload; this module narrows that down to the
handful of BTC/ETH 5- and 15-minute binaries we care about and pulls out the
fields the pricing model needs: which asset, how long the window is, when it
closes, and which token id is the "Up" (YES) side.

The strike of an up/down market is the underlying's price at the *open* of the
window, which the CLOB payload does not carry. `CryptoUpDownMarket.strike` is
therefore filled in later by the engine, from the Binance feed or a kline
lookup -- see `feeds.binance.BinanceFeed.window_open_price`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Iterable

from .config import SUPPORTED_WINDOWS

# ─────────────────────────────────────────────
# TEXT PARSING
# ─────────────────────────────────────────────

_ASSET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("BTC", re.compile(r"\b(btc|xbt|bitcoin)\b", re.I)),
    ("ETH", re.compile(r"\b(eth|ether|ethereum)\b", re.I)),
)

#: "up or down", "up-or-down", "updown", "up/down" -- all seen in the wild.
_UPDOWN_RE = re.compile(r"up[\s_/-]*(or)?[\s_/-]*down", re.I)

#: "5 minute", "5-minute", "5m", "5 min"
_WINDOW_RE = re.compile(r"\b(\d{1,3})\s*[-_]?\s*(m|min|mins|minute|minutes)\b", re.I)

#: "$118,000" / "118000 USD" / "above 3,450.5"
_STRIKE_RE = re.compile(r"\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)")

_YES_OUTCOMES = {"yes", "up", "higher", "above"}
_NO_OUTCOMES = {"no", "down", "lower", "below"}


def detect_asset(*texts: str | None) -> str | None:
    """Return 'BTC'/'ETH' if any of `texts` names that underlying."""
    for text in texts:
        if not text:
            continue
        for asset, pattern in _ASSET_PATTERNS:
            if pattern.search(text):
                return asset
    return None


def is_up_down_question(*texts: str | None) -> bool:
    """True if the text describes an up/down (directional binary) market."""
    return any(_UPDOWN_RE.search(t) for t in texts if t)


def detect_window_minutes(*texts: str | None) -> int | None:
    """Extract a contract window in minutes from free text, if stated."""
    for text in texts:
        if not text:
            continue
        for match in _WINDOW_RE.finditer(text):
            try:
                minutes = int(match.group(1))
            except ValueError:  # pragma: no cover - regex guarantees digits
                continue
            if 0 < minutes <= 1440:
                return minutes
    return None


def detect_strike(*texts: str | None) -> float | None:
    """Extract an explicit dollar strike from free text, if one is quoted."""
    for text in texts:
        if not text:
            continue
        match = _STRIKE_RE.search(text)
        if match:
            try:
                return float(match.group(1).replace(",", ""))
            except ValueError:  # pragma: no cover
                continue
    return None


def parse_iso(value: Any) -> datetime | None:
    """Parse the assorted timestamp shapes the CLOB emits into aware UTC."""
    if value in (None, "", 0):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)):
        # Epoch seconds or milliseconds.
        seconds = float(value)
        if seconds > 1e11:
            seconds /= 1000.0
        try:
            return datetime.fromtimestamp(seconds, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _to_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


# ─────────────────────────────────────────────
# MARKET MODEL
# ─────────────────────────────────────────────

@dataclass
class CryptoUpDownMarket:
    """One BTC/ETH up-or-down binary contract on the Polymarket CLOB."""

    condition_id: str
    question: str
    slug: str
    asset: str
    window_minutes: int
    close_time: datetime
    up_token_id: str
    down_token_id: str
    open_time: datetime | None = None
    strike: float | None = None
    tick_size: float = 0.01
    min_order_size: float = 5.0
    neg_risk: bool = False
    accepting_orders: bool = True
    closed: bool = False

    #: Where `strike` came from: "question", "kline", "feed" or "" if unset.
    strike_source: str = ""

    def __post_init__(self) -> None:
        if self.open_time is None:
            self.open_time = self.close_time - timedelta(minutes=self.window_minutes)

    # ── time ────────────────────────────────────────────────

    def seconds_to_expiry(self, now: datetime | None = None) -> float:
        now = now or datetime.now(UTC)
        return (self.close_time - now).total_seconds()

    def seconds_since_open(self, now: datetime | None = None) -> float:
        now = now or datetime.now(UTC)
        assert self.open_time is not None
        return (now - self.open_time).total_seconds()

    def is_expired(self, now: datetime | None = None) -> bool:
        return self.seconds_to_expiry(now) <= 0

    # ── tokens ──────────────────────────────────────────────

    def token_for(self, side: str) -> str:
        """Token id for 'UP'/'YES' or 'DOWN'/'NO'."""
        key = side.strip().upper()
        if key in ("UP", "YES"):
            return self.up_token_id
        if key in ("DOWN", "NO"):
            return self.down_token_id
        raise ValueError(f"unknown side {side!r}")

    # ── tradeability ─────────────────────────────────────────

    def is_tradeable(
        self, *, min_seconds: float, max_seconds: float, now: datetime | None = None
    ) -> tuple[bool, str]:
        """Whether the contract is in a state we are willing to enter."""
        if self.closed:
            return False, "market closed"
        if not self.accepting_orders:
            return False, "not accepting orders"
        if self.strike is None:
            return False, "strike unresolved"
        remaining = self.seconds_to_expiry(now)
        if remaining <= 0:
            return False, "expired"
        if remaining < min_seconds:
            return False, f"only {remaining:.0f}s to expiry (min {min_seconds:.0f}s)"
        if remaining > max_seconds:
            return False, f"{remaining:.0f}s to expiry (max {max_seconds:.0f}s)"
        return True, "ok"

    @property
    def label(self) -> str:
        return f"{self.asset} {self.window_minutes}m @ {self.close_time:%H:%M:%S}"

    def resolves_up(self, settlement_price: float) -> bool:
        """Polymarket resolves 'Up' when the close is strictly above the open."""
        if self.strike is None:
            raise ValueError("cannot resolve a market with no strike")
        return settlement_price > self.strike


# ─────────────────────────────────────────────
# PARSING FROM THE CLOB PAYLOAD
# ─────────────────────────────────────────────

@dataclass
class ParseReport:
    """Why markets were skipped -- surfaced in logs to explain an empty universe."""

    kept: int = 0
    skipped: dict[str, int] = field(default_factory=dict)

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1

    def summary(self) -> str:
        if not self.skipped:
            return f"kept {self.kept}"
        detail = ", ".join(f"{k}={v}" for k, v in sorted(self.skipped.items()))
        return f"kept {self.kept}, skipped: {detail}"


def _extract_tokens(raw: dict[str, Any]) -> tuple[str, str] | None:
    """Return (up_token_id, down_token_id) from a market's token list."""
    tokens = raw.get("tokens") or []
    up = down = None
    for token in tokens:
        if not isinstance(token, dict):
            continue
        token_id = str(token.get("token_id") or "").strip()
        outcome = str(token.get("outcome") or "").strip().lower()
        if not token_id:
            continue
        if outcome in _YES_OUTCOMES:
            up = token_id
        elif outcome in _NO_OUTCOMES:
            down = token_id
    if up and down:
        return up, down
    # Fall back to positional ordering when outcomes are non-standard but the
    # market is binary; Polymarket lists the affirmative outcome first.
    ids = [str(t.get("token_id") or "") for t in tokens if isinstance(t, dict)]
    ids = [i for i in ids if i]
    if len(ids) == 2:
        return ids[0], ids[1]
    return None


def parse_market(
    raw: dict[str, Any],
    *,
    assets: Iterable[str] = ("BTC", "ETH"),
    windows: Iterable[int] = SUPPORTED_WINDOWS,
    report: ParseReport | None = None,
) -> CryptoUpDownMarket | None:
    """Convert one CLOB market payload into a `CryptoUpDownMarket`, or None.

    Returns None for anything that is not an open BTC/ETH up-down contract in a
    window we trade. `report` accumulates skip reasons for logging.
    """
    rep = report or ParseReport()
    assets = set(assets)
    windows = set(windows)

    if not isinstance(raw, dict):
        rep.skip("not a dict")
        return None

    condition_id = str(raw.get("condition_id") or "").strip()
    if not condition_id:
        rep.skip("no condition_id")
        return None

    question = str(raw.get("question") or "")
    slug = str(raw.get("market_slug") or raw.get("slug") or "")
    description = str(raw.get("description") or "")

    if raw.get("closed") is True:
        rep.skip("closed")
        return None
    if raw.get("active") is False:
        rep.skip("inactive")
        return None

    if not is_up_down_question(question, slug, description):
        rep.skip("not up/down")
        return None

    asset = detect_asset(question, slug, description)
    if asset is None:
        rep.skip("unknown asset")
        return None
    if asset not in assets:
        rep.skip(f"asset {asset} not configured")
        return None

    close_time = parse_iso(raw.get("end_date_iso") or raw.get("end_date"))
    if close_time is None:
        rep.skip("no close time")
        return None
    open_time = parse_iso(raw.get("game_start_time") or raw.get("start_date_iso"))

    window = detect_window_minutes(question, slug)
    if window is None and open_time is not None:
        span = (close_time - open_time).total_seconds() / 60.0
        # Snap to the nearest supported window when the span is close enough;
        # these contracts are minted on exact boundaries.
        for candidate in sorted(windows):
            if abs(span - candidate) <= 0.75:
                window = candidate
                break
    if window is None:
        rep.skip("unknown window")
        return None
    if window not in windows:
        rep.skip(f"window {window}m not configured")
        return None

    tokens = _extract_tokens(raw)
    if tokens is None:
        rep.skip("token ids missing")
        return None
    up_token, down_token = tokens

    market = CryptoUpDownMarket(
        condition_id=condition_id,
        question=question,
        slug=slug,
        asset=asset,
        window_minutes=window,
        close_time=close_time,
        open_time=open_time,
        up_token_id=up_token,
        down_token_id=down_token,
        tick_size=_to_float(raw.get("minimum_tick_size"), 0.01) or 0.01,
        min_order_size=_to_float(raw.get("minimum_order_size"), 5.0) or 5.0,
        neg_risk=bool(raw.get("neg_risk", False)),
        accepting_orders=bool(raw.get("accepting_orders", True)),
        closed=bool(raw.get("closed", False)),
    )

    explicit_strike = detect_strike(question, description)
    if explicit_strike is not None:
        market.strike = explicit_strike
        market.strike_source = "question"

    rep.kept += 1
    return market


def normalize_gamma_market(raw: dict[str, Any]) -> dict[str, Any]:
    """Reshape a Gamma API market into the CLOB payload shape `parse_market` reads.

    Gamma is the efficient way to *find* short-dated markets (it filters by end
    date), but it names its fields differently and encodes `outcomes` and
    `clobTokenIds` as JSON-encoded strings.
    """
    import json

    def _decode(value: Any) -> list[Any]:
        if isinstance(value, list):
            return value
        if isinstance(value, str) and value.strip():
            try:
                decoded = json.loads(value)
            except ValueError:
                return []
            return decoded if isinstance(decoded, list) else []
        return []

    token_ids = [str(t) for t in _decode(raw.get("clobTokenIds"))]
    outcomes = [str(o) for o in _decode(raw.get("outcomes"))]
    tokens = [
        {"token_id": token_id, "outcome": outcomes[i] if i < len(outcomes) else ""}
        for i, token_id in enumerate(token_ids)
    ]

    return {
        "condition_id": raw.get("conditionId") or raw.get("condition_id"),
        "question": raw.get("question"),
        "market_slug": raw.get("slug") or raw.get("market_slug"),
        "description": raw.get("description"),
        "end_date_iso": raw.get("endDate") or raw.get("end_date_iso"),
        "game_start_time": raw.get("gameStartTime") or raw.get("startDate"),
        "tokens": tokens,
        "minimum_tick_size": raw.get("orderPriceMinTickSize"),
        "minimum_order_size": raw.get("orderMinSize"),
        "neg_risk": raw.get("negRisk", False),
        "accepting_orders": raw.get("acceptingOrders", True),
        "closed": raw.get("closed", False),
        "active": raw.get("active", True),
    }


def parse_markets(
    payloads: Iterable[dict[str, Any]],
    *,
    assets: Iterable[str] = ("BTC", "ETH"),
    windows: Iterable[int] = SUPPORTED_WINDOWS,
) -> tuple[list[CryptoUpDownMarket], ParseReport]:
    """Parse a page of CLOB markets, returning the keepers and a skip report."""
    report = ParseReport()
    kept: list[CryptoUpDownMarket] = []
    for raw in payloads:
        market = parse_market(raw, assets=assets, windows=windows, report=report)
        if market is not None:
            kept.append(market)
    return kept, report

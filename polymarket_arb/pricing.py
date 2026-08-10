"""
Fair value, edge detection, and the confidence score.

The model
--------
Polymarket's "up or down" contracts resolve YES when the underlying's price at
the close of the window is strictly above its price at the open. Treating the
underlying as driftless log-normal over the (very short) remaining life of the
contract, the risk-neutral probability of finishing above the strike is

    P(up) = N( ln(S / K) / (sigma * sqrt(T)) )

with S the live Binance mid, K the window's opening price, T the seconds left,
and sigma the realized volatility per square-root-second estimated from the
same feed. The drift correction (-sigma^2 * T / 2) is omitted: over a 5-15
minute horizon it moves the probability by well under a basis point.

The trade signal is the gap between that CEX-implied probability and where
Polymarket's book is actually quoting -- the "latency" the bot is trying to
capture -- filtered by an explicit confidence score so thin books, stale feeds,
or an unwarmed volatility estimate cannot produce a trade.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from datetime import datetime

from .config import Config
from .markets import CryptoUpDownMarket
from .orderbook import BookSnapshot

SECONDS_PER_YEAR = 365.0 * 24.0 * 3600.0

# ─────────────────────────────────────────────
# MATH HELPERS
# ─────────────────────────────────────────────

def norm_cdf(x: float) -> float:
    """Standard normal CDF via the error function."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def prob_up(spot: float, strike: float, sigma_per_sqrt_sec: float, seconds_left: float) -> float:
    """Probability the underlying closes strictly above `strike`.

    Degenerate inputs resolve sensibly: at (or past) expiry the answer is the
    realized outcome, and with zero volatility the price cannot move so the
    current side of the strike is the answer.
    """
    if spot <= 0 or strike <= 0:
        raise ValueError("spot and strike must be positive")
    if seconds_left <= 0 or sigma_per_sqrt_sec <= 0:
        return 1.0 if spot > strike else 0.0
    denom = sigma_per_sqrt_sec * math.sqrt(seconds_left)
    if denom <= 0:  # pragma: no cover - guarded above
        return 1.0 if spot > strike else 0.0
    return norm_cdf(math.log(spot / strike) / denom)


# ─────────────────────────────────────────────
# REALIZED VOLATILITY
# ─────────────────────────────────────────────

class RealizedVol:
    """EWMA estimator of volatility per square-root-second.

    Each update contributes a variance *rate* (squared log return divided by
    elapsed time), so irregularly spaced ticks are handled correctly rather than
    silently biasing the estimate.
    """

    def __init__(
        self,
        *,
        halflife_samples: float = 60.0,
        min_sample_interval: float = 0.5,
        warmup_samples: int = 30,
        floor: float = 1e-6,
        ceiling: float = 5e-2,
    ):
        if halflife_samples <= 0:
            raise ValueError("halflife_samples must be positive")
        self.alpha = 1.0 - 0.5 ** (1.0 / halflife_samples)
        self.min_sample_interval = min_sample_interval
        self.warmup_samples = warmup_samples
        self.floor = floor
        self.ceiling = ceiling
        self._var_rate: float | None = None
        self._last_ts: float | None = None
        self._last_price: float | None = None
        self.samples = 0

    def update(self, ts: float, price: float) -> None:
        """Feed a (monotonic timestamp, price) observation."""
        if price <= 0:
            return
        if self._last_ts is None or self._last_price is None:
            self._last_ts, self._last_price = ts, price
            return
        dt = ts - self._last_ts
        if dt < self.min_sample_interval:
            return  # throttle: sub-sampling avoids microstructure noise
        ret = math.log(price / self._last_price)
        self._last_ts, self._last_price = ts, price
        rate = (ret * ret) / dt
        if self._var_rate is None:
            self._var_rate = rate
        else:
            self._var_rate += self.alpha * (rate - self._var_rate)
        self.samples += 1

    @property
    def ready(self) -> bool:
        return self._var_rate is not None and self.samples >= self.warmup_samples

    @property
    def sigma_per_sqrt_sec(self) -> float:
        """Current estimate, clamped to the configured sanity band."""
        if self._var_rate is None or self._var_rate <= 0:
            return self.floor
        return clamp(math.sqrt(self._var_rate), self.floor, self.ceiling)

    @property
    def annualized(self) -> float:
        """Same estimate expressed as an annualised volatility, for display."""
        return self.sigma_per_sqrt_sec * math.sqrt(SECONDS_PER_YEAR)

    def warmup_progress(self) -> float:
        return clamp(self.samples / max(1, self.warmup_samples))


# ─────────────────────────────────────────────
# CONFIDENCE
# ─────────────────────────────────────────────

#: Component weights for the composite confidence score.
CONFIDENCE_WEIGHTS: dict[str, float] = {
    "freshness": 0.25,
    "liquidity": 0.20,
    "spread": 0.15,
    "volatility": 0.15,
    "timing": 0.10,
    "persistence": 0.10,
    "margin": 0.05,
}


def _decay(age: float, limit: float) -> float:
    """1.0 for a brand-new reading, 0.0 once it hits `limit` seconds old."""
    if limit <= 0:
        return 0.0
    return clamp(1.0 - (max(0.0, age) / limit))


def confidence_components(
    *,
    binance_age: float,
    clob_age: float,
    max_binance_age: float,
    max_clob_age: float,
    spread: float | None,
    reference_spread: float,
    depth_notional: float,
    reference_notional: float,
    vol_progress: float,
    seconds_left: float,
    min_seconds: float,
    max_seconds: float,
    persistence_ticks: int,
    required_ticks: int,
    divergence: float,
    min_divergence: float,
) -> dict[str, float]:
    """Score each input to the trade decision on [0, 1]."""
    freshness = min(
        _decay(binance_age, max_binance_age), _decay(clob_age, max_clob_age)
    )

    liquidity = clamp(depth_notional / reference_notional) if reference_notional > 0 else 0.0

    if spread is None:
        spread_score = 0.0
    else:
        spread_score = clamp(1.0 - (spread / reference_spread)) if reference_spread > 0 else 0.0

    # Prefer the middle of the tradeable window: very close to expiry the
    # probability is hypersensitive to the spot, and model error dominates.
    if seconds_left <= min_seconds or seconds_left >= max_seconds or max_seconds <= min_seconds:
        timing = 0.0
    else:
        span = max_seconds - min_seconds
        position = (seconds_left - min_seconds) / span      # 0 at expiry edge, 1 at far edge
        timing = clamp(math.sin(math.pi * position) ** 0.5)  # peaks mid-window

    persistence = clamp(persistence_ticks / required_ticks) if required_ticks > 0 else 1.0

    if min_divergence <= 0:
        margin = 1.0 if divergence > 0 else 0.0
    else:
        # Full marks at twice the minimum divergence.
        margin = clamp((divergence - min_divergence) / min_divergence)

    return {
        "freshness": freshness,
        "liquidity": liquidity,
        "spread": spread_score,
        "volatility": clamp(vol_progress),
        "timing": timing,
        "persistence": persistence,
        "margin": margin,
    }


def composite_confidence(components: dict[str, float]) -> float:
    """Weighted geometric mean of the components.

    Geometric rather than arithmetic so a single failing input (a stale feed, an
    empty book) drags the score down instead of being averaged away.
    """
    total_weight = 0.0
    accumulator = 0.0
    for name, weight in CONFIDENCE_WEIGHTS.items():
        value = clamp(components.get(name, 0.0))
        if value <= 0.0:
            return 0.0
        accumulator += weight * math.log(value)
        total_weight += weight
    if total_weight <= 0:  # pragma: no cover - weights are constant
        return 0.0
    return clamp(math.exp(accumulator / total_weight))


# ─────────────────────────────────────────────
# EVALUATION
# ─────────────────────────────────────────────

@dataclass
class Evaluation:
    """The full decision record for one market at one instant."""

    market: CryptoUpDownMarket
    side: str                       # "UP" or "DOWN"
    token_id: str
    fair_prob: float                # model probability for `side`
    market_mid: float               # Polymarket's implied probability
    entry_price: float              # price we would actually pay (best ask)
    effective_cost: float           # entry price + slippage + fee, per share
    divergence: float               # fair_prob - market_mid, in probability points
    edge: float                     # expected return on stake
    edge_abs: float                 # fair_prob - effective_cost, in probability points
    confidence: float
    components: dict[str, float] = field(default_factory=dict)
    spot: float = 0.0
    strike: float = 0.0
    sigma: float = 0.0
    seconds_left: float = 0.0
    spread: float | None = None
    depth_notional: float = 0.0
    tradeable: bool = False
    reason: str = ""
    created_at: float = field(default_factory=time.time)

    @property
    def key(self) -> str:
        """Stable identity for signal-persistence tracking."""
        return f"{self.market.condition_id}:{self.side}"

    def describe(self) -> str:
        return (
            f"{self.market.label} {self.side} fair={self.fair_prob:.3f} "
            f"mid={self.market_mid:.3f} ask={self.entry_price:.3f} "
            f"div={self.divergence:+.3f} edge={self.edge:+.2%} "
            f"conf={self.confidence:.2%} [{self.reason}]"
        )


def _evaluate_side(
    market: CryptoUpDownMarket,
    side: str,
    book: BookSnapshot | None,
    fair: float,
    cfg: Config,
    *,
    spot: float,
    sigma: float,
    seconds_left: float,
    binance_age: float,
    reference_notional: float,
    persistence_ticks: int,
    now_monotonic: float,
) -> Evaluation | None:
    """Score buying one side of a market. Returns None if unquotable."""
    token_id = market.token_for(side)
    if book is None:
        return None
    ask = book.best_ask
    mid = book.mid
    if ask is None or mid is None:
        return None

    fee_per_share = ask * (cfg.fee_bps / 10_000.0)
    cost = ask + cfg.slippage + fee_per_share
    divergence = fair - mid
    edge_abs = fair - cost
    edge = edge_abs / cost if cost > 0 else 0.0

    clob_age = book.age(now_monotonic)
    depth = book.notional_available(min(1.0, cost + 0.05), side="BUY")

    components = confidence_components(
        binance_age=binance_age,
        clob_age=clob_age,
        max_binance_age=cfg.max_binance_staleness,
        max_clob_age=cfg.max_clob_staleness,
        spread=book.spread,
        reference_spread=max(4.0 * market.tick_size, 0.04),
        depth_notional=depth,
        reference_notional=reference_notional,
        vol_progress=1.0,  # overwritten by the caller-supplied volatility score
        seconds_left=seconds_left,
        min_seconds=cfg.min_seconds_to_expiry,
        # Score timing against this contract's own window, so a 5-minute market
        # is judged on the 5-minute clock rather than the global ceiling.
        max_seconds=min(cfg.max_seconds_to_expiry, market.window_minutes * 60.0),
        persistence_ticks=persistence_ticks,
        required_ticks=cfg.min_signal_ticks,
        divergence=divergence,
        min_divergence=cfg.min_divergence,
    )

    return Evaluation(
        market=market,
        side=side,
        token_id=token_id,
        fair_prob=fair,
        market_mid=mid,
        entry_price=ask,
        effective_cost=cost,
        divergence=divergence,
        edge=edge,
        edge_abs=edge_abs,
        confidence=0.0,
        components=components,
        spot=spot,
        strike=market.strike or 0.0,
        sigma=sigma,
        seconds_left=seconds_left,
        spread=book.spread,
        depth_notional=depth,
    )


def evaluate_market(
    market: CryptoUpDownMarket,
    *,
    cfg: Config,
    spot: float,
    sigma: float,
    books: dict[str, BookSnapshot | None],
    binance_age: float,
    vol_progress: float,
    reference_notional: float,
    persistence: dict[str, int] | None = None,
    now: datetime | None = None,
    now_monotonic: float | None = None,
) -> Evaluation | None:
    """Pick the better side of `market` and decide whether it is tradeable.

    `books` maps token id to its latest snapshot. Returns the winning
    `Evaluation` -- with `tradeable` and `reason` filled in -- or None when the
    market cannot be evaluated at all.
    """
    now_monotonic = time.monotonic() if now_monotonic is None else now_monotonic
    persistence = persistence or {}

    if market.strike is None:
        return None
    seconds_left = market.seconds_to_expiry(now)
    p_up = prob_up(spot, market.strike, sigma, seconds_left)

    candidates: list[Evaluation] = []
    for side, fair in (("UP", p_up), ("DOWN", 1.0 - p_up)):
        token_id = market.token_for(side)
        evaluation = _evaluate_side(
            market,
            side,
            books.get(token_id),
            fair,
            cfg,
            spot=spot,
            sigma=sigma,
            seconds_left=seconds_left,
            binance_age=binance_age,
            reference_notional=reference_notional,
            persistence_ticks=persistence.get(f"{market.condition_id}:{side}", 0),
            now_monotonic=now_monotonic,
        )
        if evaluation is None:
            continue
        evaluation.components["volatility"] = clamp(vol_progress)
        evaluation.confidence = composite_confidence(evaluation.components)
        candidates.append(evaluation)

    if not candidates:
        return None

    best = max(candidates, key=lambda e: (e.edge, e.divergence))
    best.tradeable, best.reason = _gate(best, cfg, market, now)
    return best


def _gate(
    evaluation: Evaluation, cfg: Config, market: CryptoUpDownMarket, now: datetime | None
) -> tuple[bool, str]:
    """Apply the hard trade filters in order, reporting the first failure."""
    ok, why = market.is_tradeable(
        min_seconds=cfg.min_seconds_to_expiry,
        max_seconds=cfg.max_seconds_to_expiry,
        now=now,
    )
    if not ok:
        return False, why
    if evaluation.effective_cost >= 1.0:
        return False, "cost at or above $1.00"
    if evaluation.divergence < cfg.min_divergence:
        return False, (
            f"divergence {evaluation.divergence:.2%} < {cfg.min_divergence:.2%}"
        )
    if evaluation.edge < cfg.min_edge:
        return False, f"edge {evaluation.edge:.2%} < {cfg.min_edge:.2%}"
    if evaluation.confidence < cfg.min_confidence:
        return False, f"confidence {evaluation.confidence:.2%} < {cfg.min_confidence:.2%}"
    return True, "tradeable"

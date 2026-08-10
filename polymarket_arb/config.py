"""
Configuration, thresholds, and the live-trading safety gate.

Every tunable lives here as a dataclass field with a documented default, so the
trading rules are auditable in one place rather than scattered through the
engine. Secrets are read from the environment only -- never from the CLI, so
they do not end up in shell history or process listings.
"""
from __future__ import annotations

import argparse
import os
from dataclasses import dataclass, field, fields
from pathlib import Path

# ─────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────

CLOB_HOST = "https://clob.polymarket.com"
GAMMA_HOST = "https://gamma-api.polymarket.com"
BINANCE_WS_HOST = "wss://stream.binance.com:9443"
BINANCE_REST_HOST = "https://api.binance.com"

POLYGON_CHAIN_ID = 137

#: Binance symbol used to price each Polymarket underlying.
ASSET_SYMBOLS: dict[str, str] = {"BTC": "btcusdt", "ETH": "ethusdt"}

#: Contract expiries we trade, in minutes.
SUPPORTED_WINDOWS: tuple[int, ...] = (5, 15)

#: Environment variables holding secrets. Never accepted as CLI arguments.
ENV_PRIVATE_KEY = "POLYMARKET_PRIVATE_KEY"
ENV_API_KEY = "POLYMARKET_API_KEY"
ENV_API_SECRET = "POLYMARKET_API_SECRET"
ENV_API_PASSPHRASE = "POLYMARKET_API_PASSPHRASE"
ENV_FUNDER = "POLYMARKET_FUNDER_ADDRESS"
ENV_SIGNATURE_TYPE = "POLYMARKET_SIGNATURE_TYPE"
ENV_TELEGRAM_TOKEN = "TELEGRAM_BOT_TOKEN"
ENV_TELEGRAM_CHAT = "TELEGRAM_CHAT_ID"

#: The third live-trading gate: this exact phrase must be in the environment.
ENV_LIVE_CONFIRM = "POLYMARKET_ARB_LIVE_CONFIRM"
LIVE_CONFIRM_PHRASE = "I ACCEPT FULL RISK OF LOSS"


class ConfigError(ValueError):
    """Raised for an invalid or internally inconsistent configuration."""


# ─────────────────────────────────────────────
# CREDENTIALS
# ─────────────────────────────────────────────

@dataclass(frozen=True)
class Credentials:
    """Polymarket CLOB credentials, sourced entirely from the environment."""

    private_key: str | None = None
    api_key: str | None = None
    api_secret: str | None = None
    api_passphrase: str | None = None
    funder: str | None = None
    signature_type: int | None = None

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Credentials":
        e = os.environ if env is None else env
        raw_sig = e.get(ENV_SIGNATURE_TYPE)
        try:
            sig = int(raw_sig) if raw_sig not in (None, "") else None
        except ValueError as exc:
            raise ConfigError(f"{ENV_SIGNATURE_TYPE} must be an integer, got {raw_sig!r}") from exc
        return cls(
            private_key=e.get(ENV_PRIVATE_KEY) or None,
            api_key=e.get(ENV_API_KEY) or None,
            api_secret=e.get(ENV_API_SECRET) or None,
            api_passphrase=e.get(ENV_API_PASSPHRASE) or None,
            funder=e.get(ENV_FUNDER) or None,
            signature_type=sig,
        )

    @property
    def has_signer(self) -> bool:
        return bool(self.private_key)

    @property
    def has_api_creds(self) -> bool:
        return bool(self.api_key and self.api_secret and self.api_passphrase)

    def missing_for_live(self) -> list[str]:
        """Names of the environment variables still required to trade live."""
        missing: list[str] = []
        if not self.private_key:
            missing.append(ENV_PRIVATE_KEY)
        return missing

    def __repr__(self) -> str:  # pragma: no cover - defensive, avoids secret leaks
        return (
            f"Credentials(private_key={'set' if self.private_key else 'unset'}, "
            f"api_creds={'set' if self.has_api_creds else 'unset'}, "
            f"funder={self.funder or 'unset'}, signature_type={self.signature_type})"
        )


# ─────────────────────────────────────────────
# LIVE TRADING GATE
# ─────────────────────────────────────────────

@dataclass(frozen=True)
class LiveTradingGate:
    """Three independent opt-ins, all required before real money can move.

    1. ``--live``                     : ask for live mode at all
    2. ``--i-understand-the-risks``   : acknowledge the risk explicitly
    3. ``POLYMARKET_ARB_LIVE_CONFIRM``: an env var set to the exact phrase

    Two of the three are CLI flags and the third is an environment variable, so
    no single copy-pasted command line can flip a paper run into a live one.
    """

    live_flag: bool = False
    risk_flag: bool = False
    env_phrase: str | None = None

    @property
    def env_ok(self) -> bool:
        return (self.env_phrase or "").strip() == LIVE_CONFIRM_PHRASE

    @property
    def enabled(self) -> bool:
        return bool(self.live_flag and self.risk_flag and self.env_ok)

    def unmet(self) -> list[str]:
        """Human-readable list of the gates that are still closed."""
        missing: list[str] = []
        if not self.live_flag:
            missing.append("--live")
        if not self.risk_flag:
            missing.append("--i-understand-the-risks")
        if not self.env_ok:
            missing.append(f'{ENV_LIVE_CONFIRM}="{LIVE_CONFIRM_PHRASE}"')
        return missing

    def explain(self) -> str:
        if self.enabled:
            return "LIVE trading enabled: all three confirmations present."
        if not any((self.live_flag, self.risk_flag, self.env_phrase)):
            return "PAPER trading (default). No live confirmations supplied."
        return "PAPER trading. Still required for live: " + ", ".join(self.unmet())


# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

@dataclass
class Config:
    """Full bot configuration. Thresholds are fractions, not percentages."""

    # -- universe -------------------------------------------------------
    assets: tuple[str, ...] = ("BTC", "ETH")
    windows: tuple[int, ...] = SUPPORTED_WINDOWS

    # -- signal thresholds ----------------------------------------------
    #: Polymarket must lag the CEX-implied probability by at least this much.
    min_divergence: float = 0.03          # 3 percentage points
    #: Net expected edge required after fees and slippage.
    min_edge: float = 0.05                # 5%
    #: Composite confidence score required to fire.
    min_confidence: float = 0.85          # 85%
    #: Consecutive scans a divergence must persist before it is tradeable.
    min_signal_ticks: int = 2

    # -- sizing ----------------------------------------------------------
    #: Fraction of full Kelly to stake. 0.5 == half-Kelly.
    kelly_fraction: float = 0.5
    #: Hard cap on a single position as a fraction of portfolio equity.
    max_position_pct: float = 0.08        # 8%
    #: Cap on total open exposure as a fraction of equity.
    max_total_exposure_pct: float = 0.30
    #: Never stake less than this many dollars (below it, fees dominate).
    min_position_usd: float = 5.0
    #: Most simultaneous open positions.
    max_open_positions: int = 6
    #: At most one position per market condition id.
    one_position_per_market: bool = True

    # -- risk ------------------------------------------------------------
    #: Kill switch: halt all trading once daily drawdown exceeds this.
    max_daily_drawdown: float = 0.10      # 10%
    #: Warn over Telegram as drawdown crosses these fractions of the limit.
    drawdown_alert_levels: tuple[float, ...] = (0.5, 0.75, 0.9)
    #: Halt if the session (all-time) drawdown exceeds this.
    max_total_drawdown: float = 0.25
    #: Stop opening new positions once this many losses hit in a row.
    max_consecutive_losses: int = 8

    # -- execution --------------------------------------------------------
    starting_bankroll: float = 1_000.0
    #: Polymarket taker fee in basis points, applied to notional.
    fee_bps: float = 0.0
    #: Assumed adverse slippage when crossing the spread, in probability terms.
    slippage: float = 0.005
    #: Do not open a position with less than this long left on the contract.
    min_seconds_to_expiry: float = 45.0
    #: Do not open a position with more than this long left (signal decays).
    max_seconds_to_expiry: float = 900.0
    #: Close early if the mark moves this far in our favour.
    take_profit: float = 0.92
    #: Close early if the mark collapses this far against us.
    stop_loss: float = 0.15
    #: Live orders are posted as FOK marketable orders by default.
    order_type: str = "FOK"
    #: In live mode, try to sell out this many seconds before expiry rather than
    #: waiting for on-chain settlement. Ignored in paper mode.
    live_exit_before_expiry: float = 20.0

    # -- feeds -------------------------------------------------------------
    #: A quote older than this is treated as stale and blocks trading.
    max_binance_staleness: float = 2.0
    max_clob_staleness: float = 5.0
    #: Seconds between order book refreshes for tracked markets.
    book_poll_interval: float = 1.0
    #: Seconds between market (re)discovery sweeps.
    market_refresh_interval: float = 30.0
    #: Seconds between opportunity scans.
    scan_interval: float = 0.5
    #: Seconds between dashboard repaints.
    dashboard_interval: float = 1.0
    #: Seconds of returns used for the realized volatility estimate.
    vol_window_seconds: float = 300.0
    #: EWMA half-life for realized volatility, in samples.
    vol_halflife: float = 60.0
    #: Floor/ceiling on annualised-equivalent vol, guards against garbage input.
    min_vol_per_sqrt_sec: float = 1e-6
    max_vol_per_sqrt_sec: float = 5e-2

    # -- rate limits --------------------------------------------------------
    clob_requests_per_second: float = 8.0
    clob_burst: float = 16.0
    binance_rest_per_second: float = 4.0
    telegram_per_minute: float = 18.0
    max_concurrent_book_fetches: int = 6

    # -- plumbing ------------------------------------------------------------
    clob_host: str = CLOB_HOST
    gamma_host: str = GAMMA_HOST
    binance_ws_host: str = BINANCE_WS_HOST
    binance_rest_host: str = BINANCE_REST_HOST
    chain_id: int = POLYGON_CHAIN_ID
    db_path: Path = Path("polymarket_arb.sqlite3")
    log_path: Path | None = Path("polymarket_arb.log")
    log_level: str = "INFO"
    dashboard_enabled: bool = True
    #: Stop after this many seconds. 0 == run forever.
    run_seconds: float = 0.0

    # -- gates / secrets --------------------------------------------------------
    gate: LiveTradingGate = field(default_factory=LiveTradingGate)
    credentials: Credentials = field(default_factory=Credentials)
    telegram_token: str | None = None
    telegram_chat_id: str | None = None

    # ── derived ────────────────────────────────────────────

    @property
    def live(self) -> bool:
        """True only when all three live gates are satisfied."""
        return self.gate.enabled

    @property
    def mode(self) -> str:
        return "LIVE" if self.live else "PAPER"

    @property
    def symbols(self) -> dict[str, str]:
        """Binance symbol per configured asset."""
        return {a: ASSET_SYMBOLS[a] for a in self.assets}

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_token and self.telegram_chat_id)

    # ── validation ────────────────────────────────────────

    def validate(self) -> "Config":
        """Check the configuration is internally consistent. Returns self."""
        if not self.assets:
            raise ConfigError("at least one asset is required")
        for a in self.assets:
            if a not in ASSET_SYMBOLS:
                raise ConfigError(f"unsupported asset {a!r}; known: {sorted(ASSET_SYMBOLS)}")
        for w in self.windows:
            if w not in SUPPORTED_WINDOWS:
                raise ConfigError(f"unsupported window {w!r}; known: {SUPPORTED_WINDOWS}")

        unit_ranges = {
            "min_divergence": (0.0, 1.0),
            "min_edge": (0.0, 1.0),
            "min_confidence": (0.0, 1.0),
            "kelly_fraction": (0.0, 1.0),
            "max_position_pct": (0.0, 1.0),
            "max_total_exposure_pct": (0.0, 1.0),
            "max_daily_drawdown": (0.0, 1.0),
            "max_total_drawdown": (0.0, 1.0),
            "slippage": (0.0, 1.0),
        }
        for name, (lo, hi) in unit_ranges.items():
            v = getattr(self, name)
            if not lo <= v <= hi:
                raise ConfigError(f"{name} must be within [{lo}, {hi}], got {v}")
        if self.kelly_fraction <= 0:
            raise ConfigError("kelly_fraction must be > 0")
        if self.max_daily_drawdown <= 0:
            raise ConfigError("max_daily_drawdown must be > 0 (it is the kill switch)")
        if self.max_position_pct > self.max_total_exposure_pct:
            raise ConfigError("max_position_pct cannot exceed max_total_exposure_pct")
        if self.starting_bankroll <= 0:
            raise ConfigError("starting_bankroll must be positive")
        if self.min_seconds_to_expiry >= self.max_seconds_to_expiry:
            raise ConfigError("min_seconds_to_expiry must be below max_seconds_to_expiry")
        if self.order_type not in ("FOK", "FAK", "GTC"):
            raise ConfigError(f"unsupported order_type {self.order_type!r}")
        if self.max_open_positions < 1:
            raise ConfigError("max_open_positions must be at least 1")

        if self.live:
            missing = self.credentials.missing_for_live()
            if missing:
                raise ConfigError(
                    "live trading requested but these environment variables are unset: "
                    + ", ".join(missing)
                )
        return self

    def describe(self) -> str:
        """Multi-line summary printed at startup and sent to Telegram."""
        lines = [
            f"mode                : {self.mode}",
            f"assets/windows      : {','.join(self.assets)} @ {','.join(f'{w}m' for w in self.windows)}",
            f"min divergence      : {self.min_divergence:.2%}",
            f"min edge            : {self.min_edge:.2%}",
            f"min confidence      : {self.min_confidence:.2%}",
            f"kelly fraction      : {self.kelly_fraction:g} (half-Kelly = 0.5)",
            f"max position        : {self.max_position_pct:.2%} of equity",
            f"max total exposure  : {self.max_total_exposure_pct:.2%} of equity",
            f"kill switch         : daily drawdown > {self.max_daily_drawdown:.2%}",
            f"starting bankroll   : ${self.starting_bankroll:,.2f}",
            f"telegram            : {'enabled' if self.telegram_enabled else 'disabled'}",
            f"database            : {self.db_path}",
        ]
        return "\n".join(lines)


# ─────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    d = Config()
    p = argparse.ArgumentParser(
        prog="polymarket-arb",
        description=(
            "Latency arbitrage bot for Polymarket BTC/ETH up-down contracts, "
            "priced against a live Binance spot feed. Paper trades by default."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        epilog=(
            "LIVE TRADING requires all three of: --live, --i-understand-the-risks, "
            f'and {ENV_LIVE_CONFIRM}="{LIVE_CONFIRM_PHRASE}" in the environment.'
        ),
    )

    g = p.add_argument_group("universe")
    g.add_argument("--assets", default=",".join(d.assets),
                   help="comma-separated subset of BTC,ETH")
    g.add_argument("--windows", default=",".join(str(w) for w in d.windows),
                   help="comma-separated contract windows in minutes (5,15)")

    g = p.add_argument_group("signal thresholds")
    g.add_argument("--min-divergence", type=float, default=d.min_divergence,
                   help="minimum CEX-vs-Polymarket gap, as a fraction (0.03 = 3pp)")
    g.add_argument("--min-edge", type=float, default=d.min_edge,
                   help="minimum net edge after fees/slippage, as a fraction")
    g.add_argument("--min-confidence", type=float, default=d.min_confidence,
                   help="minimum composite confidence score, as a fraction")
    g.add_argument("--min-signal-ticks", type=int, default=d.min_signal_ticks,
                   help="scans a signal must persist before it is tradeable")

    g = p.add_argument_group("sizing")
    g.add_argument("--kelly-fraction", type=float, default=d.kelly_fraction,
                   help="fraction of full Kelly (0.5 = half-Kelly)")
    g.add_argument("--max-position-pct", type=float, default=d.max_position_pct,
                   help="per-position cap as a fraction of equity")
    g.add_argument("--max-total-exposure-pct", type=float, default=d.max_total_exposure_pct,
                   help="total open exposure cap as a fraction of equity")
    g.add_argument("--max-open-positions", type=int, default=d.max_open_positions)
    g.add_argument("--bankroll", type=float, default=d.starting_bankroll,
                   help="starting equity for paper mode / sizing baseline")

    g = p.add_argument_group("risk")
    g.add_argument("--max-daily-drawdown", type=float, default=d.max_daily_drawdown,
                   help="kill switch threshold as a fraction of the day's high-water mark")
    g.add_argument("--max-total-drawdown", type=float, default=d.max_total_drawdown,
                   help="session-wide drawdown halt, as a fraction")
    g.add_argument("--max-consecutive-losses", type=int, default=d.max_consecutive_losses)

    g = p.add_argument_group("execution")
    g.add_argument("--fee-bps", type=float, default=d.fee_bps,
                   help="taker fee in basis points applied to notional")
    g.add_argument("--slippage", type=float, default=d.slippage,
                   help="assumed adverse slippage in probability terms")
    g.add_argument("--take-profit", type=float, default=d.take_profit,
                   help="close early once the mark reaches this price")
    g.add_argument("--stop-loss", type=float, default=d.stop_loss,
                   help="close early once the mark falls to this price")
    g.add_argument("--order-type", choices=("FOK", "FAK", "GTC"), default=d.order_type)

    g = p.add_argument_group("plumbing")
    g.add_argument("--db", dest="db_path", default=str(d.db_path), help="SQLite database path")
    g.add_argument("--log-file", dest="log_path", default=str(d.log_path) if d.log_path else "",
                   help="log file path; empty disables file logging")
    g.add_argument("--log-level", default=d.log_level,
                   choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    g.add_argument("--no-dashboard", action="store_true", help="disable the terminal dashboard")
    g.add_argument("--run-seconds", type=float, default=d.run_seconds,
                   help="exit after N seconds (0 = run until interrupted)")
    g.add_argument("--scan-interval", type=float, default=d.scan_interval)
    g.add_argument("--book-poll-interval", type=float, default=d.book_poll_interval)

    g = p.add_argument_group("live trading gates (all three required)")
    g.add_argument("--live", action="store_true",
                   help="gate 1 of 3: request live trading")
    g.add_argument("--i-understand-the-risks", dest="risk_ack", action="store_true",
                   help="gate 2 of 3: acknowledge that real funds are at risk")
    # Gate 3 is the environment variable; it is deliberately not a CLI flag.

    return p


def _split_csv(raw: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def config_from_args(argv: list[str] | None = None, env: dict[str, str] | None = None) -> Config:
    """Parse argv into a validated `Config`, pulling secrets from `env`."""
    e = os.environ if env is None else env
    args = build_parser().parse_args(argv)

    try:
        windows = tuple(int(w) for w in _split_csv(args.windows))
    except ValueError as exc:
        raise ConfigError(f"--windows must be integers, got {args.windows!r}") from exc

    cfg = Config(
        assets=tuple(a.upper() for a in _split_csv(args.assets)),
        windows=windows,
        min_divergence=args.min_divergence,
        min_edge=args.min_edge,
        min_confidence=args.min_confidence,
        min_signal_ticks=args.min_signal_ticks,
        kelly_fraction=args.kelly_fraction,
        max_position_pct=args.max_position_pct,
        max_total_exposure_pct=args.max_total_exposure_pct,
        max_open_positions=args.max_open_positions,
        starting_bankroll=args.bankroll,
        max_daily_drawdown=args.max_daily_drawdown,
        max_total_drawdown=args.max_total_drawdown,
        max_consecutive_losses=args.max_consecutive_losses,
        fee_bps=args.fee_bps,
        slippage=args.slippage,
        take_profit=args.take_profit,
        stop_loss=args.stop_loss,
        order_type=args.order_type,
        db_path=Path(args.db_path),
        log_path=Path(args.log_path) if args.log_path else None,
        log_level=args.log_level,
        dashboard_enabled=not args.no_dashboard,
        run_seconds=args.run_seconds,
        scan_interval=args.scan_interval,
        book_poll_interval=args.book_poll_interval,
        gate=LiveTradingGate(
            live_flag=args.live,
            risk_flag=args.risk_ack,
            env_phrase=e.get(ENV_LIVE_CONFIRM),
        ),
        credentials=Credentials.from_env(e),
        telegram_token=e.get(ENV_TELEGRAM_TOKEN) or None,
        telegram_chat_id=e.get(ENV_TELEGRAM_CHAT) or None,
    )
    return cfg.validate()


def field_names() -> list[str]:
    """Config field names, handy for tests and introspection."""
    return [f.name for f in fields(Config)]

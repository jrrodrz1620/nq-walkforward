"""
Command line entry point.

Sets up logging, prints the mode banner (loudly, if this is a live run), installs
signal handlers for a clean shutdown, and runs the engine.
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys
from logging.handlers import RotatingFileHandler

from .config import Config, ConfigError, config_from_args
from .engine import ArbEngine

log = logging.getLogger("polymarket_arb")


# ─────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────

def setup_logging(cfg: Config) -> None:
    """File logging always; console logging only when the dashboard is off.

    The rich dashboard repaints in place, so interleaved log lines would corrupt
    it -- with the dashboard enabled the log file is the record.
    """
    root = logging.getLogger()
    root.setLevel(getattr(logging, cfg.log_level.upper(), logging.INFO))
    for handler in list(root.handlers):
        root.removeHandler(handler)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)-28s %(message)s", "%Y-%m-%d %H:%M:%S"
    )

    if cfg.log_path is not None:
        try:
            file_handler = RotatingFileHandler(
                cfg.log_path, maxBytes=16 * 1024 * 1024, backupCount=3
            )
            file_handler.setFormatter(fmt)
            root.addHandler(file_handler)
        except OSError as exc:
            print(f"warning: could not open log file {cfg.log_path}: {exc}", file=sys.stderr)

    if not cfg.dashboard_enabled or not sys.stdout.isatty():
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(fmt)
        root.addHandler(console)

    if not root.handlers:  # never leave the root logger without a sink
        root.addHandler(logging.NullHandler())

    # These libraries are chatty at DEBUG and add nothing at INFO.
    for noisy in ("websockets.client", "httpx", "httpcore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# ─────────────────────────────────────────────
# BANNER
# ─────────────────────────────────────────────

def print_banner(cfg: Config) -> None:
    bar = "=" * 72
    if cfg.live:
        print(bar)
        print("  *** LIVE TRADING ENABLED -- REAL FUNDS ARE AT RISK ***")
        print("  All three confirmations were supplied. Ctrl-C stops the bot.")
        print(bar)
    else:
        print(bar)
        print("  PAPER TRADING (no orders will be sent)")
        print(f"  {cfg.gate.explain()}")
        print(bar)
    print(cfg.describe())
    print(bar, flush=True)


# ─────────────────────────────────────────────
# RUN
# ─────────────────────────────────────────────

async def run_engine(cfg: Config) -> int:
    engine = ArbEngine(cfg)
    loop = asyncio.get_running_loop()

    for signame in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, signame, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, engine.stop, f"received {signame}")
        except (NotImplementedError, RuntimeError):  # pragma: no cover - platform dependent
            pass

    try:
        await engine.run()
    except KeyboardInterrupt:  # pragma: no cover - interactive
        engine.stop("keyboard interrupt")
        return 130
    except Exception:
        log.exception("fatal error in engine")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        cfg = config_from_args(argv)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    setup_logging(cfg)
    print_banner(cfg)

    try:
        return asyncio.run(run_engine(cfg))
    except KeyboardInterrupt:  # pragma: no cover - interactive
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

"""Configuration validation and the three-flag live trading gate."""
from __future__ import annotations

import pytest

from polymarket_arb.config import (
    ENV_LIVE_CONFIRM,
    ENV_PRIVATE_KEY,
    LIVE_CONFIRM_PHRASE,
    Config,
    ConfigError,
    Credentials,
    LiveTradingGate,
    config_from_args,
)

ALL_THREE = {
    ENV_LIVE_CONFIRM: LIVE_CONFIRM_PHRASE,
    ENV_PRIVATE_KEY: "0x" + "11" * 32,
}


# ─────────────────────────────────────────────
# LIVE GATE
# ─────────────────────────────────────────────

def test_gate_defaults_to_paper():
    gate = LiveTradingGate()
    assert gate.enabled is False
    assert "PAPER" in gate.explain()


@pytest.mark.parametrize(
    "live, risk, phrase",
    [
        (True, True, None),               # missing env var
        (True, False, LIVE_CONFIRM_PHRASE),  # missing risk flag
        (False, True, LIVE_CONFIRM_PHRASE),  # missing live flag
        (True, True, "i accept full risk of loss"),  # wrong case
        (True, True, "I ACCEPT"),         # truncated phrase
    ],
)
def test_any_missing_confirmation_stays_paper(live, risk, phrase):
    gate = LiveTradingGate(live_flag=live, risk_flag=risk, env_phrase=phrase)
    assert gate.enabled is False
    assert gate.unmet(), "an unmet gate must be reported"


def test_all_three_confirmations_enable_live():
    gate = LiveTradingGate(True, True, LIVE_CONFIRM_PHRASE)
    assert gate.enabled is True
    assert gate.unmet() == []


def test_phrase_tolerates_surrounding_whitespace():
    gate = LiveTradingGate(True, True, f"  {LIVE_CONFIRM_PHRASE}\n")
    assert gate.enabled is True


def test_live_config_requires_a_private_key():
    cfg = Config(gate=LiveTradingGate(True, True, LIVE_CONFIRM_PHRASE))
    with pytest.raises(ConfigError, match=ENV_PRIVATE_KEY):
        cfg.validate()


def test_live_config_validates_with_credentials():
    cfg = Config(
        gate=LiveTradingGate(True, True, LIVE_CONFIRM_PHRASE),
        credentials=Credentials(private_key="0xabc"),
    ).validate()
    assert cfg.live is True
    assert cfg.mode == "LIVE"


# ─────────────────────────────────────────────
# CLI PARSING
# ─────────────────────────────────────────────

def test_cli_defaults_are_the_documented_thresholds():
    cfg = config_from_args([], env={})
    assert cfg.min_divergence == 0.03
    assert cfg.min_edge == 0.05
    assert cfg.min_confidence == 0.85
    assert cfg.kelly_fraction == 0.5
    assert cfg.max_position_pct == 0.08
    assert cfg.max_daily_drawdown == 0.10
    assert cfg.live is False


def test_cli_live_flags_alone_do_not_enable_live():
    cfg = config_from_args(["--live", "--i-understand-the-risks"], env={})
    assert cfg.live is False
    assert ENV_LIVE_CONFIRM in " ".join(cfg.gate.unmet())


def test_cli_with_env_confirmation_enables_live():
    cfg = config_from_args(["--live", "--i-understand-the-risks"], env=dict(ALL_THREE))
    assert cfg.live is True


def test_secrets_are_only_read_from_the_environment():
    cfg = config_from_args([], env={"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "42"})
    assert cfg.telegram_enabled is True
    # No CLI flag exposes the token, so it can never leak into shell history.
    from polymarket_arb.config import build_parser

    flags = {action.dest for action in build_parser()._actions}
    assert "telegram_token" not in flags
    assert "private_key" not in flags


def test_credentials_repr_hides_the_key():
    creds = Credentials(private_key="0xdeadbeef", api_key="k", api_secret="s", api_passphrase="p")
    assert "0xdeadbeef" not in repr(creds)
    assert "set" in repr(creds)


# ─────────────────────────────────────────────
# VALIDATION
# ─────────────────────────────────────────────

@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"assets": ("DOGE",)}, "unsupported asset"),
        ({"windows": (7,)}, "unsupported window"),
        ({"min_edge": 1.5}, "min_edge"),
        ({"max_daily_drawdown": 0.0}, "kill switch"),
        ({"max_position_pct": 0.9}, "max_total_exposure_pct"),
        ({"starting_bankroll": 0.0}, "starting_bankroll"),
        ({"order_type": "IOC"}, "order_type"),
        ({"max_open_positions": 0}, "max_open_positions"),
        ({"min_seconds_to_expiry": 1000.0}, "min_seconds_to_expiry"),
    ],
)
def test_invalid_configs_are_rejected(kwargs, message):
    with pytest.raises(ConfigError, match=message):
        Config(**kwargs).validate()


def test_describe_reports_the_active_mode(cfg):
    text = cfg.describe()
    assert "PAPER" in text
    assert "3.00%" in text     # divergence threshold
    assert "8.00% of equity" in text

"""Parsing CLOB and Gamma market payloads into tradeable up/down contracts."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from polymarket_arb.markets import (
    detect_asset,
    detect_strike,
    detect_window_minutes,
    is_up_down_question,
    normalize_gamma_market,
    parse_iso,
    parse_market,
    parse_markets,
)

from .conftest import make_market

NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


def clob_payload(**overrides):
    payload = {
        "condition_id": "0xabc123",
        "question": "Bitcoin Up or Down - 5 minute",
        "market_slug": "bitcoin-up-or-down-5m-1200",
        "description": "Resolves Up if the price at close exceeds the price at open.",
        "end_date_iso": "2026-08-10T12:05:00Z",
        "game_start_time": "2026-08-10T12:00:00Z",
        "tokens": [
            {"token_id": "111", "outcome": "Up"},
            {"token_id": "222", "outcome": "Down"},
        ],
        "minimum_tick_size": "0.01",
        "minimum_order_size": "5",
        "active": True,
        "closed": False,
        "accepting_orders": True,
        "neg_risk": False,
    }
    payload.update(overrides)
    return payload


# ─────────────────────────────────────────────
# TEXT DETECTION
# ─────────────────────────────────────────────

@pytest.mark.parametrize(
    "text, expected",
    [
        ("Bitcoin Up or Down", "BTC"),
        ("BTC up/down 5m", "BTC"),
        ("Ethereum Up or Down", "ETH"),
        ("eth-up-or-down-15m", "ETH"),
        ("Solana Up or Down", None),
    ],
)
def test_detect_asset(text, expected):
    assert detect_asset(text) == expected


@pytest.mark.parametrize(
    "text",
    ["Up or Down", "up-or-down", "updown", "UP/DOWN", "Bitcoin up  or  down"],
)
def test_up_down_spellings_are_recognised(text):
    assert is_up_down_question(text) is True


def test_non_directional_markets_are_rejected():
    assert is_up_down_question("Will Bitcoin reach $200k in 2026?") is False


@pytest.mark.parametrize(
    "text, expected",
    [("5 minute", 5), ("15-minute", 15), ("5m", 5), ("15 mins", 15), ("hourly", None)],
)
def test_detect_window(text, expected):
    assert detect_window_minutes(text) == expected


def test_detect_strike_handles_formatted_dollars():
    assert detect_strike("Will BTC close above $118,250.50?") == 118_250.50
    assert detect_strike("no strike here") is None


@pytest.mark.parametrize(
    "value, expected_hour",
    [
        ("2026-08-10T12:05:00Z", 12),
        ("2026-08-10T12:05:00+00:00", 12),
        (1_754_827_500, 12),          # epoch seconds
        (1_754_827_500_000, 12),      # epoch milliseconds
    ],
)
def test_parse_iso_accepts_the_shapes_the_api_emits(value, expected_hour):
    parsed = parse_iso(value)
    assert parsed is not None and parsed.tzinfo is not None


def test_parse_iso_rejects_garbage():
    assert parse_iso("not a date") is None
    assert parse_iso(None) is None
    assert parse_iso("") is None


# ─────────────────────────────────────────────
# CLOB PARSING
# ─────────────────────────────────────────────

def test_parse_a_five_minute_bitcoin_market():
    market = parse_market(clob_payload())
    assert market is not None
    assert market.asset == "BTC"
    assert market.window_minutes == 5
    assert market.up_token_id == "111"
    assert market.down_token_id == "222"
    assert market.tick_size == 0.01
    assert market.min_order_size == 5.0


def test_window_is_inferred_from_the_open_and_close_times():
    payload = clob_payload(question="Bitcoin Up or Down", market_slug="btc-up-or-down")
    market = parse_market(payload)
    assert market is not None and market.window_minutes == 5


def test_positional_tokens_are_used_when_outcomes_are_unfamiliar():
    payload = clob_payload(tokens=[
        {"token_id": "111", "outcome": "Affirmative"},
        {"token_id": "222", "outcome": "Negative"},
    ])
    market = parse_market(payload)
    assert market is not None
    assert (market.up_token_id, market.down_token_id) == ("111", "222")


@pytest.mark.parametrize(
    "overrides, reason",
    [
        ({"closed": True}, "closed"),
        ({"active": False}, "inactive"),
        ({"question": "Will BTC hit 200k?", "market_slug": "btc-200k",
          "description": ""}, "not up/down"),
        ({"question": "Solana Up or Down - 5 minute", "market_slug": "sol-up-or-down",
          "description": ""}, "unknown asset"),
        ({"tokens": [{"token_id": "111", "outcome": "Up"}]}, "token ids missing"),
        ({"end_date_iso": None, "end_date": None}, "no close time"),
        ({"condition_id": ""}, "no condition_id"),
    ],
)
def test_unsuitable_markets_are_skipped_with_a_reason(overrides, reason):
    from polymarket_arb.markets import ParseReport

    report = ParseReport()
    assert parse_market(clob_payload(**overrides), report=report) is None
    assert reason in report.skipped


def test_unconfigured_windows_and_assets_are_filtered():
    from polymarket_arb.markets import ParseReport

    report = ParseReport()
    assert parse_market(clob_payload(), windows=(15,), report=report) is None
    assert "window 5m not configured" in report.skipped

    report = ParseReport()
    assert parse_market(clob_payload(), assets=("ETH",), report=report) is None
    assert "asset BTC not configured" in report.skipped


def test_parse_markets_returns_keepers_and_a_summary():
    payloads = [clob_payload(), clob_payload(condition_id="0xdef", closed=True), {}]
    kept, report = parse_markets(payloads)
    assert len(kept) == 1
    assert report.kept == 1
    assert "kept 1" in report.summary()


def test_an_explicit_strike_in_the_question_is_used():
    market = parse_market(clob_payload(
        question="Bitcoin Up or Down - 5 minute (open $118,000)"
    ))
    assert market is not None
    assert market.strike == 118_000.0
    assert market.strike_source == "question"


# ─────────────────────────────────────────────
# GAMMA NORMALIZATION
# ─────────────────────────────────────────────

def test_gamma_payload_normalizes_into_the_clob_shape():
    gamma = {
        "conditionId": "0xfeed",
        "question": "Ethereum Up or Down - 15 minute",
        "slug": "ethereum-up-or-down-15m",
        "endDate": "2026-08-10T12:15:00Z",
        "startDate": "2026-08-10T12:00:00Z",
        "outcomes": '["Up", "Down"]',
        "clobTokenIds": '["999", "888"]',
        "orderPriceMinTickSize": 0.001,
        "orderMinSize": 5,
        "negRisk": False,
        "acceptingOrders": True,
        "closed": False,
        "active": True,
    }
    market = parse_market(normalize_gamma_market(gamma))
    assert market is not None
    assert market.asset == "ETH"
    assert market.window_minutes == 15
    assert market.up_token_id == "999"
    assert market.down_token_id == "888"
    assert market.tick_size == 0.001


def test_gamma_normalization_survives_malformed_json_fields():
    normalized = normalize_gamma_market({"clobTokenIds": "not json", "outcomes": None})
    assert normalized["tokens"] == []


# ─────────────────────────────────────────────
# MARKET BEHAVIOUR
# ─────────────────────────────────────────────

def test_token_lookup_accepts_both_namings():
    market = make_market()
    assert market.token_for("UP") == market.token_for("YES")
    assert market.token_for("down") == market.token_for("no")
    with pytest.raises(ValueError):
        market.token_for("SIDEWAYS")


def test_open_time_is_derived_from_the_window_when_absent():
    market = make_market(window=15, seconds_left=100.0)
    assert market.open_time == market.close_time - timedelta(minutes=15)


@pytest.mark.parametrize(
    "kwargs, ok, reason_fragment",
    [
        (dict(seconds_left=150.0), True, "ok"),
        (dict(seconds_left=10.0), False, "min"),
        (dict(seconds_left=5_000.0), False, "max"),
        (dict(seconds_left=-5.0), False, "expired"),
        (dict(strike=None), False, "strike unresolved"),
    ],
)
def test_tradeability_gate(kwargs, ok, reason_fragment):
    market = make_market(**kwargs)
    allowed, reason = market.is_tradeable(min_seconds=45.0, max_seconds=900.0)
    assert allowed is ok
    assert reason_fragment in reason


def test_closed_markets_are_never_tradeable():
    market = make_market()
    market.closed = True
    assert market.is_tradeable(min_seconds=1.0, max_seconds=9_000.0)[0] is False
    market.closed = False
    market.accepting_orders = False
    assert market.is_tradeable(min_seconds=1.0, max_seconds=9_000.0)[0] is False


def test_resolution_requires_a_strictly_higher_close():
    market = make_market(strike=100.0)
    assert market.resolves_up(100.01) is True
    assert market.resolves_up(100.0) is False    # a flat close resolves Down
    assert market.resolves_up(99.99) is False

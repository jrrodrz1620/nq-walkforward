"""Equity/drawdown stats, summary formatting, and /api/summary endpoint."""
import dataclasses

import pytest
from fastapi.testclient import TestClient

from bot.app import create_app
from bot.engine import TradingEngine
from bot.state import StateStore
from bot.summary import build_summary

from .conftest import FakeBroker, RecordingSleep, make_payload


def test_equity_stats_drawdown(store):
    # +500, -800, +200  → cum: 500, -300, -100; peak 500; maxDD -800; curDD -600
    store.add_transaction("MES", "2025-12", "sell", 1, 5010, 500.0)
    store.add_transaction("MES", "2025-12", "sell", 1, 4990, -800.0)
    store.add_transaction("MES", "2025-12", "sell", 1, 5005, 200.0)
    s = store.equity_stats()
    assert s["realized_total"] == -100.0
    assert s["peak_equity"] == 500.0
    assert s["max_drawdown"] == -800.0
    assert s["current_drawdown"] == -600.0
    assert s["total_trades"] == 3


def test_equity_stats_flat_at_new_high(store):
    store.add_transaction("MES", "2025-12", "sell", 1, 5010, 300.0)
    store.add_transaction("MES", "2025-12", "sell", 1, 5020, 200.0)
    s = store.equity_stats()
    assert s["current_drawdown"] == 0.0        # at a new equity high
    assert s["max_drawdown"] == 0.0


def test_build_summary_text(store):
    store.add_transaction("MES", "2025-12", "sell", 1, 5010, 300.0)
    txt = build_summary(store.equity_stats(), day_pnl_mtm=50.0, positions=[])
    assert "Daily Summary" in txt
    assert "Max drawdown" in txt and "Current drawdown" in txt


@pytest.fixture
def client(config, store):
    cfg = dataclasses.replace(config, dashboard_token="dash")
    eng = TradingEngine(cfg, store, FakeBroker(), sleep=RecordingSleep())
    return TestClient(create_app(engine=eng, config=cfg))


def test_api_summary_requires_token(client):
    assert client.get("/api/summary").status_code == 401
    r = client.get("/api/summary?token=dash")
    assert r.status_code == 200
    assert "summary" in r.json()
    assert r.json()["sent"] is False        # no notifier configured


def test_api_state_includes_drawdown(client):
    client.post("/webhook", json=make_payload(symbol="MES", quantity=1,
                                              alert_id="s1"))
    body = client.get("/api/state?token=dash").json()
    assert "stats" in body
    assert "max_drawdown" in body["stats"]

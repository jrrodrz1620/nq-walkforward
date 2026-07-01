"""Dashboard + /api/state token gating and payload."""
import pytest
from fastapi.testclient import TestClient

from bot.app import create_app
from bot.engine import TradingEngine

from .conftest import FakeBroker, RecordingSleep, make_payload


@pytest.fixture
def client(config, store):
    import dataclasses
    cfg = dataclasses.replace(config, dashboard_token="dash-secret")
    engine = TradingEngine(cfg, store, FakeBroker(), sleep=RecordingSleep())
    return TestClient(create_app(engine=engine, config=cfg))


def test_dashboard_requires_token(client):
    assert client.get("/dashboard").status_code == 401
    assert client.get("/dashboard?token=wrong").status_code == 401
    ok = client.get("/dashboard?token=dash-secret")
    assert ok.status_code == 200
    assert "Futures Bot" in ok.text


def test_api_state_requires_token(client):
    assert client.get("/api/state").status_code == 401
    r = client.get("/api/state?token=dash-secret")
    assert r.status_code == 200
    body = r.json()
    assert set(["day_pnl_mtm", "realized_pnl_today", "positions", "orders",
                "broker_type"]).issubset(body)


def test_api_state_reflects_fills(client):
    client.post("/webhook", json=make_payload(symbol="MES", quantity=1,
                                              alert_id="d1"))
    body = client.get("/api/state?token=dash-secret").json()
    assert len(body["positions"]) == 1
    assert body["positions"][0]["symbol"] == "MES"
    assert body["positions"][0]["multiplier"] == 5.0    # MES = $5/pt
    assert any(o["status"] == "filled" for o in body["orders"])


def test_dashboard_disabled_when_no_token_configured(config, store):
    # Default config has empty dashboard_token → always unauthorized.
    engine = TradingEngine(config, store, FakeBroker(), sleep=RecordingSleep())
    c = TestClient(create_app(engine=engine, config=config))
    assert c.get("/dashboard?token=anything").status_code == 401

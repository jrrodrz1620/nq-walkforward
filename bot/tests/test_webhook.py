"""
HTTP-level tests for the /webhook endpoint via FastAPI's TestClient (the mock
network object simulating TradingView posts): auth, validation, fills,
duplicates, and the network-timeout failure path.
"""
import os

import pytest
from fastapi.testclient import TestClient

from bot.app import create_app
from bot.engine import TradingEngine

from .conftest import FakeBroker, RecordingSleep, make_payload


@pytest.fixture
def client(config, store):
    broker = FakeBroker()
    engine = TradingEngine(config, store, broker, sleep=RecordingSleep())
    app = create_app(engine=engine, config=config)
    c = TestClient(app)
    c.app_broker = broker          # expose for assertions
    return c


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_valid_webhook_fills(client):
    r = client.post("/webhook", json=make_payload(symbol="ES", quantity=1))
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "filled"
    assert body["broker_order_id"] is not None


def test_invalid_passphrase_401(client):
    r = client.post("/webhook", json=make_payload(passphrase="wrong"))
    assert r.status_code == 401
    assert r.json()["status"] == "rejected"
    # Nothing routed to the broker.
    assert client.app_broker.place_calls == 0


def test_malformed_payload_422(client):
    # Missing required fields + bad action.
    r = client.post("/webhook", json={"symbol": "ES", "action": "hold"})
    assert r.status_code == 422


def test_negative_quantity_rejected_422(client):
    r = client.post("/webhook", json=make_payload(quantity=-1))
    assert r.status_code == 422


def test_duplicate_webhook_returns_duplicate(client):
    payload = make_payload(alert_id="dup-1")
    first = client.post("/webhook", json=payload)
    second = client.post("/webhook", json=payload)
    assert first.json()["status"] == "filled"
    assert second.json()["status"] == "duplicate"
    assert client.app_broker.place_calls == 1


def test_network_failure_returns_503_and_logs(config, store, error_log_path):
    broker = FakeBroker(fail_place_times=99)
    engine = TradingEngine(config, store, broker, sleep=RecordingSleep())
    app = create_app(engine=engine, config=config)
    client = TestClient(app, raise_server_exceptions=False)

    r = client.post("/webhook", json=make_payload(alert_id="netfail"))
    assert r.status_code == 503
    assert r.json()["status"] == "error"
    assert os.path.exists(error_log_path)


def test_positions_endpoint(client):
    client.post("/webhook", json=make_payload(symbol="ES", quantity=1,
                                              alert_id="p1"))
    r = client.get("/positions")
    assert r.status_code == 200
    body = r.json()
    assert len(body["open_positions"]) == 1
    assert body["open_positions"][0]["symbol"] == "ES"


def test_unknown_symbol_rejected(client):
    r = client.post("/webhook", json=make_payload(symbol="ZZZ", alert_id="u1"))
    assert r.status_code == 200
    assert r.json()["status"] == "rejected"

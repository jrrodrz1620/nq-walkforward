"""Telegram notifier tests + engine notification hook."""
import httpx

from bot.engine import TradingEngine
from bot.models import WebhookPayload
from bot.notify import TelegramNotifier, format_result
from bot.state import StateStore

from .conftest import FakeBroker, RecordingSleep, make_payload


def test_notifier_disabled_without_credentials():
    n = TelegramNotifier("", "", enabled=None)
    assert n.enabled is False
    assert n.send("hi") is False


def test_notifier_posts_to_telegram():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    n = TelegramNotifier("TOKEN", "CHAT", client=client)
    assert n.send("hello") is True
    assert "botTOKEN/sendMessage" in seen["url"]
    assert seen["body"]["chat_id"] == "CHAT"
    assert seen["body"]["text"] == "hello"


def test_notifier_swallows_errors():
    def handler(request):
        raise httpx.ConnectTimeout("down", request=request)
    n = TelegramNotifier("T", "C", client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert n.send("x") is False        # never raises


class _CapturingNotifier:
    def __init__(self):
        self.messages = []

    def send(self, text):
        self.messages.append(text)
        return True


def test_engine_notifies_on_fill_not_on_duplicate(config):
    store = StateStore(":memory:")
    notifier = _CapturingNotifier()
    eng = TradingEngine(config, store, FakeBroker(), sleep=RecordingSleep(),
                        notifier=notifier)
    p = WebhookPayload(**make_payload(symbol="MES", alert_id="n1"))
    eng.process_webhook(p)          # filled → 1 message
    eng.process_webhook(p)          # duplicate → no message
    assert len(notifier.messages) == 1
    assert "FILLED" in notifier.messages[0]
    store.close()


def test_format_result_includes_key_fields(config, store, broker, sleeper):
    eng = TradingEngine(config, store, broker, sleep=sleeper)
    p = WebhookPayload(**make_payload(symbol="MES", action="buy", quantity=1))
    res = eng.process_webhook(p)
    msg = format_result(p, res)
    assert "MES" in msg and "BUY" in msg

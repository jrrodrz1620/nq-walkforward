"""
FastAPI application exposing the secure ``/webhook`` endpoint  (gate 1).

The HTTP layer is thin: validate the payload (pydantic), check the shared
passphrase in constant time, hand off to :class:`TradingEngine`, and map the
engine result to an HTTP status. All business logic lives in the engine.
"""
from __future__ import annotations

import hmac
from typing import Optional

from fastapi import Depends, FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse

from .broker import TradovateBroker
from .config import AppConfig
from .dashboard import DASHBOARD_HTML
from .engine import TradingEngine
from .models import WebhookPayload, WebhookResponse
from .state import StateStore
from .traderspost import TradersPostBroker

try:
    from .contracts import get_contract as _get_contract
except Exception:  # pragma: no cover
    _get_contract = None


def build_broker(config: AppConfig):
    """Construct the execution backend selected by ``config.broker_type``."""
    if config.broker_type == "traderspost":
        if not config.traderspost_webhook_url:
            raise RuntimeError(
                "BOT_TRADERSPOST_WEBHOOK_URL must be set when "
                "BOT_BROKER_TYPE=traderspost")
        return TradersPostBroker(config.traderspost_webhook_url,
                                 config.account_equity,
                                 stop_loss_points=config.stop_loss_points,
                                 take_profit_points=config.take_profit_points)
    return TradovateBroker(config.broker_base_url, config.broker_token)


def create_app(engine: Optional[TradingEngine] = None,
               config: Optional[AppConfig] = None) -> FastAPI:
    """Application factory.

    Pass an ``engine`` (and matching ``config``) to inject test doubles;
    otherwise a live engine is built from environment configuration.
    """
    config = config or (engine.config if engine else AppConfig.from_env())

    if engine is None:
        store = StateStore(config.db_path)
        broker = build_broker(config)
        notifier = None
        if config.telegram_token and config.telegram_chat_id:
            from .notify import TelegramNotifier
            notifier = TelegramNotifier(config.telegram_token,
                                        config.telegram_chat_id)
        engine = TradingEngine(config, store, broker, notifier=notifier)

    app = FastAPI(title="TradingView Futures Bot", version="1.0.0")
    app.state.engine = engine
    app.state.config = config

    def get_engine() -> TradingEngine:
        return app.state.engine

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "version": "1.0.0"}

    @app.post("/webhook", response_model=WebhookResponse)
    def webhook(payload: WebhookPayload,
                eng: TradingEngine = Depends(get_engine)) -> JSONResponse:
        # ── authenticate via shared passphrase (constant-time) ──────
        if not hmac.compare_digest(payload.passphrase, app.state.config.passphrase):
            return JSONResponse(
                status_code=401,
                content=WebhookResponse(
                    status="rejected", detail="invalid passphrase"
                ).model_dump(),
            )

        result = eng.process_webhook(payload)
        body = WebhookResponse(
            status=result.status,
            detail=result.detail,
            order_id=result.order_id,
            broker_order_id=result.broker_order_id,
            fill_price=result.fill_price,
            idempotency_key=result.idempotency_key,
        )
        return JSONResponse(status_code=result.http_status,
                            content=body.model_dump())

    @app.get("/positions")
    def positions(eng: TradingEngine = Depends(get_engine)) -> dict:
        return {
            "open_positions": [vars(p) for p in eng.store.open_positions()],
            "realized_pnl_today": eng.store.realized_pnl_today(),
            "day_pnl_mtm": eng.risk.marked_to_market_day_pnl(),
        }

    # ── web dashboard (token-guarded) ───────────────────────────────
    def _dash_ok(token: str) -> bool:
        secret = app.state.config.dashboard_token
        return bool(secret) and hmac.compare_digest(token or "", secret)

    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard(token: str = Query("")):
        if not _dash_ok(token):
            return HTMLResponse(
                "<body style='font:16px system-ui;background:#0e1117;color:#e6edf3;"
                "padding:40px'>Unauthorized. Open "
                "<code>/dashboard?token=YOUR_BOT_DASHBOARD_TOKEN</code>"
                " (set <code>BOT_DASHBOARD_TOKEN</code> to enable).</body>",
                status_code=401)
        return HTMLResponse(DASHBOARD_HTML)

    @app.get("/api/state")
    def api_state(token: str = Query(""),
                  eng: TradingEngine = Depends(get_engine)):
        if not _dash_ok(token):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        positions = []
        for p in eng.store.open_positions():
            mult = 1.0
            if _get_contract is not None:
                try:
                    mult = _get_contract(p.symbol).multiplier
                except Exception:
                    pass
            positions.append({**vars(p), "multiplier": mult})
        return {
            "broker_type": app.state.config.broker_type,
            "day_pnl_mtm": eng.risk.marked_to_market_day_pnl(),
            "realized_pnl_today": eng.store.realized_pnl_today(),
            "positions": positions,
            "orders": [vars(o) for o in eng.store.recent_orders(25)],
            "transactions": eng.store.recent_transactions(25),
        }

    return app


# Module-level app for `uvicorn bot.app:app`.
app = create_app()

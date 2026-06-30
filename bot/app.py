"""
FastAPI application exposing the secure ``/webhook`` endpoint  (gate 1).

The HTTP layer is thin: validate the payload (pydantic), check the shared
passphrase in constant time, hand off to :class:`TradingEngine`, and map the
engine result to an HTTP status. All business logic lives in the engine.
"""
from __future__ import annotations

import hmac
from typing import Optional

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse

from .broker import TradovateBroker
from .config import AppConfig
from .engine import TradingEngine
from .models import WebhookPayload, WebhookResponse
from .state import StateStore
from .traderspost import TradersPostBroker


def build_broker(config: AppConfig):
    """Construct the execution backend selected by ``config.broker_type``."""
    if config.broker_type == "traderspost":
        if not config.traderspost_webhook_url:
            raise RuntimeError(
                "BOT_TRADERSPOST_WEBHOOK_URL must be set when "
                "BOT_BROKER_TYPE=traderspost")
        return TradersPostBroker(config.traderspost_webhook_url,
                                 config.account_equity)
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
        engine = TradingEngine(config, store, broker)

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

    return app


# Module-level app for `uvicorn bot.app:app`.
app = create_app()

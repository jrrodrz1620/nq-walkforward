# TradingView → Futures Execution Bot

A small, production-shaped FastAPI service that turns TradingView webhook alerts
into futures orders, with hard risk limits, tick/margin math, SQLite-backed
state + idempotency, and exponential-backoff recovery.

It lives alongside the walk-forward research tooling in this repo: once a
strategy survives out-of-sample validation, this is the layer that actually
routes its signals to a broker.

```
TradingView alert ──HTTP POST /webhook──▶ FastAPI (auth: passphrase)
                                             │
                                             ▼
                                        TradingEngine
        ┌──────────────┬──────────────┬──────────────┬─────────────────┐
        ▼              ▼              ▼              ▼                 ▼
   idempotency     mark / MtM     risk gates     broker route     state +
   (SQLite UNIQUE)  pricing       (margin/cap/    (retry w/        error_log.json
                                   drawdown)       backoff)
```

## Run it

```bash
pip install -r bot/requirements.txt

export BOT_PASSPHRASE="your-tradingview-secret"
export BOT_BROKER_URL="https://live.tradovate.example/v1"
export BOT_BROKER_TOKEN="..."

uvicorn bot.app:app --host 0.0.0.0 --port 8000
```

Point a TradingView alert at `https://your-host/webhook` with a JSON body:

```json
{
  "symbol": "ES",
  "action": "buy",
  "quantity": 1,
  "price": 5000.25,
  "passphrase": "your-tradingview-secret",
  "contract_month": "2025-06",
  "alert_id": "{{timenow}}"
}
```

`alert_id` is optional — when omitted, an idempotency key is derived from the
trade-defining fields so identical rapid-fire posts collapse to a single order.

### Endpoints

| Method | Path         | Purpose                                            |
|--------|--------------|----------------------------------------------------|
| POST   | `/webhook`   | Accept a TradingView alert and route the order     |
| GET    | `/positions` | Open positions, realized PnL, marked-to-market day |
| GET    | `/health`    | Liveness probe                                      |

Response statuses: `filled`, `duplicate`, `rejected` (with a `code`:
`unknown_symbol` / `margin` / `position_cap` / `daily_drawdown` /
`broker_rejected`), or `error` (503, broker unreachable after retries).

## Risk safeguards (hard-coded defaults — `bot/config.py`)

- **Max 2 contracts** net per instrument, ever.
- **$1,000 daily loss limit**, marked-to-market (realized + unrealized at last
  mark); new entries are halted once breached, reducing trades still allowed.
- **Safe margin threshold**: an order is rejected if its initial margin would
  consume more than 50% of available equity.

## Contract math (`bot/pricing.py`, `bot/contracts.py`)

Multipliers and ticks follow the repo's conventions: ES=$50/pt, MES=$5,
NQ=$20, MNQ=$2, plus YM/RTY/CL/GC and their micros. The pricing engine converts
raw price changes into points, ticks and dollars, and validates initial margin
against equity before any order is routed.

## State & idempotency (`bot/state.py`)

SQLite (`orders`, `positions`, `transactions`). The `orders` table has a UNIQUE
constraint on the idempotency key, claimed atomically *before* routing — so
duplicate, rapid-fire webhooks can never execute twice, even concurrently or
across a restart.

## Recovery (`bot/broker.py`, `bot/errors.py`)

Broker calls run through a truncated exponential-backoff retry (max 3 retries:
0.5s → 1s → 2s, capped at 8s). On exhaustion the bot logs a critical alert and
atomically appends the context + a full state snapshot to `error_log.json`, then
returns 503 — no phantom positions are left behind.

## Deploy (Docker / DigitalOcean)

The repo ships a portable `Dockerfile`, a `docker-compose.yml`, and a DO App
Platform spec at `.do/app.yaml`. State + error log default to `/data` — mount a
volume there so they survive restarts.

**Droplet (recommended — durable SQLite state):**
```bash
cp .env.example .env      # fill in BOT_PASSPHRASE etc.
docker compose up -d --build
curl http://<droplet-ip>:8000/health
```
The named `bot_state` volume keeps positions + idempotency history across
restarts and redeploys.

**App Platform:**
```bash
doctl apps create --spec .do/app.yaml
```
⚠️ App Platform's filesystem is **ephemeral** — the SQLite state at `/data` is
wiped on every redeploy, so positions/idempotency reset. For durable state
there, use a Droplet (above) or port `bot/state.py` to a Managed Postgres.

> The default broker URL is a **stub** (`demo.tradovate.example`) that fail-safes
> to `error_log.json`. Set `BOT_BROKER_URL` / `BOT_BROKER_TOKEN` to your real
> broker — and adapt the request/response shapes in `bot/broker.py` — before
> trading live.

## Tests

```bash
python -m pytest bot/tests -v
```

61 tests covering contracts, pricing/margin, risk gates, SQLite state +
idempotency, the broker client + retry helper (mocked network via
`httpx.MockTransport`), the full engine pipeline, and the HTTP layer
(`fastapi.testclient`) — including simulated network-timeout failures.
See `test_results.txt` at the repo root for a captured run.

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

**Droplet (recommended — durable SQLite state + automatic HTTPS):**
```bash
cp .env.example .env      # fill in BOT_PASSPHRASE, BOT_DOMAIN, ACME_EMAIL
docker compose up -d --build
curl https://<BOT_DOMAIN>/health
```
`docker-compose.yml` runs the bot behind **Caddy**, which auto-provisions and
renews a Let's Encrypt certificate — TradingView then POSTs to
`https://<BOT_DOMAIN>/webhook`. The bot's port 8000 is **not** published to the
host; only Caddy (80/443) is exposed. Requirements: `BOT_DOMAIN`'s DNS A record
points at the Droplet and ports 80/443 are open on the firewall.

The named `bot_state` volume keeps positions + idempotency history across
restarts; `caddy_data` persists the issued certificate (avoids LE rate limits).

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

## Connecting TradersPost

[TradersPost](https://traderspost.io) is a webhook → broker bridge. Put this bot
*in front* of it so your hard risk guardrails apply before anything reaches the
broker: `TradingView → bot (risk gates) → TradersPost → paper/live account`.

Set these in `.env` (the bot reads them via docker-compose):
```bash
BOT_BROKER_TYPE=traderspost
BOT_TRADERSPOST_WEBHOOK_URL=https://webhooks.traderspost.io/trading/webhook/<id>/<token>
BOT_ACCOUNT_EQUITY=50000     # your paper balance; drives the margin gate
BOT_STOP_LOSS_POINTS=40      # protective stop in points, 0 = off (MES 40 = $200)
```

**Protective stop**: when `BOT_STOP_LOSS_POINTS > 0`, the bot attaches a
`stopLoss` (`{"type":"stop","stopPrice":...}`) to every TradersPost order,
placed that many points on the losing side of the signal price and snapped to
the contract's tick. Handy when the strategy has no built-in stop and you can't
set one in TradersPost's UI.
Then `docker compose up -d --build`. Approved orders are POSTed to TradersPost as
`{ticker, action, quantity, price, type}`.

Two TradersPost-specific notes (see `bot/traderspost.py`):
- **Equity**: TradersPost doesn't return live equity on the webhook path, so the
  margin guardrail uses `BOT_ACCOUNT_EQUITY`. Keep it roughly in sync with your
  account. (With $50k and the 50% cap, a 2-lot full-size ES is margin-rejected;
  use micros or raise equity to size up.)
- **Fills**: TradersPost acks *receipt*, not a fill, so positions in this bot
  reflect what was **sent**. For exact fills, reconcile via TradersPost's API.
- **Ticker**: the `ticker` sent is the root symbol (e.g. `ES`). Confirm it
  matches the symbol your TradersPost strategy expects.

## Tests

```bash
python -m pytest bot/tests -v
```

61 tests covering contracts, pricing/margin, risk gates, SQLite state +
idempotency, the broker client + retry helper (mocked network via
`httpx.MockTransport`), the full engine pipeline, and the HTTP layer
(`fastapi.testclient`) — including simulated network-timeout failures.
See `test_results.txt` at the repo root for a captured run.

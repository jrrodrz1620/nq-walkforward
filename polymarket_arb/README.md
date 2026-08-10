# Polymarket × Binance latency arbitrage bot

Prices Polymarket's short-dated **BTC/ETH "up or down"** binaries (5- and
15-minute windows) against a live **Binance** spot feed, and trades the gap when
the prediction market's book lags the centralised exchange.

**Paper trading is the default.** Live trading needs three separate
confirmations — see [Going live](#going-live).

---

## How it works

A Polymarket up/down contract resolves **Up** when the underlying's price at the
close of the window is strictly above its price at the open. That makes it a
plain digital option struck at the window's opening price, so given a live spot
price it can be valued directly:

```
P(up) = N( ln(S / K) / (σ · √T) )
```

* `S` — live Binance mid (`@bookTicker` on the combined stream)
* `K` — the strike, i.e. the underlying's price when the window opened
* `T` — seconds left on the contract
* `σ` — realized volatility per √second, an EWMA of squared log returns off the
  same feed (each sample divided by its own elapsed time, so irregular ticks do
  not bias the estimate)

The drift term (`−σ²T/2`) is omitted: over a 5–15 minute horizon it moves the
probability by well under a basis point.

The trade signal is the gap between that CEX-implied probability and where the
Polymarket book is actually quoting. Five filters stand between a gap and an
order:

| Gate | Default | Meaning |
|---|---|---|
| **Divergence** | > 3pp | `fair − book mid`, in probability points — the raw lag |
| **Edge** | > 5% | expected **return on stake**, after slippage and fees |
| **Confidence** | > 85% | composite score, below |
| **Persistence** | 2 scans | the gap must survive consecutive scans, not one tick |
| **Position size** | < 8% of equity | hard cap on top of the Kelly stake |

### The confidence score

A weighted **geometric** mean of seven components, so one bad input sinks the
score instead of being averaged away:

| Component | Weight | Zero when |
|---|---|---|
| `freshness` | 0.25 | either feed is past its staleness budget |
| `liquidity` | 0.20 | no depth at our price |
| `spread` | 0.15 | the book is wider than ~4 ticks |
| `volatility` | 0.15 | the vol estimator has not warmed up |
| `timing` | 0.10 | at the very start or the very end of the window |
| `persistence` | 0.10 | the signal has not repeated |
| `margin` | 0.05 | divergence only just clears the minimum |

### Sizing

Half-Kelly. For a binary bought at price `c` with model probability `p`, the
full-Kelly stake is `(p − c) / (1 − c)`; the bot stakes `kelly_fraction` of it
(0.5 by default), then clips by the smallest of: the 8% per-position cap, the
remaining portfolio exposure budget, available cash, and the dollar depth
resting at our limit price. The binding constraint is recorded on every trade.

---

## Install

Python **3.11+**.

```bash
pip install -r polymarket_arb/requirements.txt
```

## Quick start (paper)

```bash
python -m polymarket_arb
```

That connects to Binance, discovers live BTC/ETH up/down markets, and paper
trades them with a $1,000 notional bankroll, drawing the dashboard as it goes.

Useful variations:

```bash
# BTC only, 5-minute contracts, run for an hour, log to the console
python -m polymarket_arb --assets BTC --windows 5 --run-seconds 3600 --no-dashboard

# Loosen the filters to see more candidate signals while testing
python -m polymarket_arb --min-divergence 0.01 --min-edge 0.02 --min-confidence 0.5

# Full option list
python -m polymarket_arb --help
```

## Environment variables

Secrets are read **only** from the environment — no CLI flag exposes them, so
they never land in shell history.

| Variable | Needed for | Notes |
|---|---|---|
| `POLYMARKET_PRIVATE_KEY` | live trading | signs orders; required for live |
| `POLYMARKET_API_KEY` / `_API_SECRET` / `_API_PASSPHRASE` | live trading | derived automatically from the key if unset |
| `POLYMARKET_FUNDER_ADDRESS` | live trading | proxy/funder wallet, if you use one |
| `POLYMARKET_SIGNATURE_TYPE` | live trading | integer signature type, if you use one |
| `POLYMARKET_ARB_LIVE_CONFIRM` | live trading | the third gate, below |
| `TELEGRAM_BOT_TOKEN` | alerts | from `@BotFather` |
| `TELEGRAM_CHAT_ID` | alerts | target chat |

Reading the CLOB (markets and books) needs no credentials at all, so paper mode
runs with an empty environment.

## Going live

Three independent confirmations are required, and **all three** must be present:

1. `--live`
2. `--i-understand-the-risks`
3. `POLYMARKET_ARB_LIVE_CONFIRM="I ACCEPT FULL RISK OF LOSS"` in the environment

Two are CLI flags and the third is an environment variable, so no single
copy-pasted command line can flip a paper run into a live one. Miss any of them
and the bot prints exactly what is still missing and stays in paper mode.

```bash
export POLYMARKET_PRIVATE_KEY=0x...
export POLYMARKET_ARB_LIVE_CONFIRM="I ACCEPT FULL RISK OF LOSS"
python -m polymarket_arb --live --i-understand-the-risks --bankroll 500
```

Live orders are posted as **fill-or-kill marketable limits**. An arbitrage that
rests on the book is no longer an arbitrage: if it does not fill at our price
immediately, the order dies rather than leaving unwanted exposure.

## Risk controls

| Control | Flag | Default |
|---|---|---|
| Daily drawdown kill switch | `--max-daily-drawdown` | 10% of the day's high-water mark |
| Session drawdown halt | `--max-total-drawdown` | 25% |
| Per-position cap | `--max-position-pct` | 8% of equity |
| Total open exposure | `--max-total-exposure-pct` | 30% of equity |
| Max open positions | `--max-open-positions` | 6 |
| Consecutive-loss brake | `--max-consecutive-losses` | 8 |

The kill switch trips on drawdown from the **day's high-water mark**, not from
the day's open, so giving back a big intraday gain counts. Telegram warnings
fire at 50%, 75% and 90% of the limit on the way down. Once tripped it stays
tripped: new entries stop and resting orders are cancelled. Existing positions
are still marked and settled — settlement is bookkeeping, not trading. The
switch clears only at the UTC day boundary or via `RiskManager.resume()`.

## Telegram alerts

Every open, every close (with P&L and equity), every drawdown warning, the kill
switch, startup and shutdown. Messages go through a bounded queue drained by one
worker, paced at 18/minute, with `retry_after` honoured on HTTP 429 — a
rate-limited or down Telegram can never block the trading loop. With no token
configured, alerts are logged instead.

## Data (SQLite)

Written to `polymarket_arb.sqlite3` (`--db` to relocate), in WAL mode so you can
query it from another process while the bot runs.

| Table | Contents |
|---|---|
| `positions` | one row per position, with the full decision context: fair value, divergence, edge, confidence, Kelly fraction, σ, spot and strike at entry |
| `position_history` | append-only `OPEN` / `MARK` / `CLOSE` snapshots — the whole life of every position |
| `fills` | every execution attempt, including rejections and unfilled orders |
| `equity` | periodic mark-to-market for the P&L curve |
| `events` | signals, risk blocks, kill-switch trips, errors |

```sql
-- P&L by asset and window
SELECT asset, window_minutes, COUNT(*) n,
       SUM(realized_pnl) pnl,
       AVG(realized_pnl > 0) win_rate
FROM positions WHERE status = 'CLOSED'
GROUP BY asset, window_minutes;
```

## Dashboard

`rich` panels on a TTY: mode banner and kill-switch state, equity / P&L /
drawdown / win rate / profit factor, open positions with live marks and expiry
countdowns, the last ten closed trades, the current signal scan, and feed health
(Binance connection and latency, CLOB book ages, call and failure counts).

Off a TTY it degrades to a throttled plain-text summary; `--no-dashboard`
disables it and logs to the console instead. With the dashboard on, the log file
(`--log-file`, default `polymarket_arb.log`, rotated) is the record — log lines
would otherwise corrupt the in-place repaint.

## Reliability

* **Binance WS** — auto-reconnect with exponential backoff and jitter, a
  20-second stall watchdog, and a proactive reconnect before Binance's 24-hour
  server-side limit.
* **CLOB REST** — every call is paced by a token bucket, retried with backoff,
  and guarded by a circuit breaker that opens after 6 consecutive failures.
  Blocking client calls run in a thread so they never stall the event loop.
* **Loops** — each of the six loops catches its own exceptions; a bad tick is
  logged to `events` and retried, it does not take the bot down.
* **Restart** — open positions, realized P&L and cash are rebuilt from SQLite,
  and in live mode cash is re-read from the on-chain USDC balance.

## Tests

```bash
pytest polymarket_arb          # 234 tests, no network required
```

Covers the probability model and volatility estimator, confidence scoring and
every trade gate, Kelly sizing and its caps, the kill switch and risk gates,
market parsing (CLOB and Gamma payload shapes), order book depth arithmetic,
paper fills, live order-response parsing, SQLite round-trips, rate limiting and
retries, and end-to-end engine behaviour against fake feeds (entry → mark →
early exit → settlement).

---

## Assumptions and limitations

Worth knowing before pointing this at real money:

* **Binance is the pricing and settlement reference.** Strikes come from the
  Binance 1-minute kline open (or our own observed tape, when the bot was
  running at the window's open), and paper settlement compares the Binance
  close to that strike. If a given Polymarket series resolves against a
  different oracle, the model's strike and settlement can disagree with the
  actual resolution.
* **Live settlement is not instant cash.** Winning shares pay out on-chain when
  the market resolves and positions are redeemed; the bot books the P&L at
  expiry as accounting. To avoid the gap entirely, live mode tries to sell out
  ~20 seconds before expiry (`live_exit_before_expiry`) and only holds to
  settlement if that does not fill.
* **The edge is only as good as σ.** The volatility estimate drives fair value;
  a regime break or a thin sample makes the "edge" an artefact. The confidence
  score's warmup and freshness components exist to blunt this, not to remove it.
* **This is a competitive game.** Others are running the same trade with better
  colocation. The thresholds default deliberately high — the bot is meant to
  pass on most of what it sees.
* **Fees default to 0 bps.** Set `--fee-bps` to whatever your account is
  actually charged before trusting the edge numbers.

Run it in paper mode long enough to see the fill assumptions hold up on your own
connection before enabling any of the three live gates.

# Prop Firm Readiness Analyzer

A React app that simulates the full prop-firm account lifecycle against your real trading results. Upload a TradingView Strategy Tester CSV export and it will replay your daily P&L through each firm's evaluation and funded-account rules to answer: *Would I have passed? Would I have blown up? What would I actually have netted after fees?*

## Features

- **CSV import** — drag-and-drop a TradingView "List of Trades" export (comma, semicolon, or tab separated; flexible column detection).
- **Multi-account portfolio** — configure any mix of firms/sizes, each with an optional purchase date that filters the dataset from that day forward.
- **Eval simulation** — day-by-day replay against profit target, trailing/EOD drawdown, daily loss limits, minimum trading days, and consistency rules.
- **Funded lifecycle simulation** — 252-day replay with drawdown-floor locking, payout cadence/minimums, profit splits (including Apex's 100%-first-$25K tier), weekly payout caps, and buffer periods. Reports payouts extracted, blow-up day (if any), and net lifetime P&L after eval/activation fees.
- **Monte Carlo** — 1,000 shuffled re-simulations of the eval to estimate pass rate and days-to-pass distribution (p10/median/p90).
- **Chart-click account creation** — click any point on a lifecycle chart to spawn a new account purchased on that date.

## Supported firms

| Firm | Drawdown | Notes |
|---|---|---|
| Apex Trader Funding | EOD trailing | 100% of first $25K, then 90/10 |
| TopStep | EOD trailing | Daily loss limits, 50% consistency |
| Lucid Trading | EOD trailing | Flex + Pro tiers, no daily loss limit, no activation fee |
| BluSky Trading | Trailing | 30% eval consistency, weekly payout cap |

⚠️ Firm rules change frequently — parameters here are a snapshot (as of Mar 2026). Verify current rules on each firm's site before buying an eval.

## Running

```bash
npm install
npm run dev      # dev server
npm run build    # production build to dist/
```

## Getting your CSV

TradingView → Strategy Tester → **List of Trades** tab → export icon (top right of the panel).

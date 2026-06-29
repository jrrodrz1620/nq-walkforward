"""
Synthetic TradingView "List of Trades" generator.

Produces a realistic export for the Phantom Flow SMC strategy so the
walk-forward pipeline can be demonstrated end-to-end without TradingView.

The generated edge intentionally DEGRADES over time (regime shift) so the
walk-forward overfit check has something meaningful to detect.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def generate_sample_trades(n: int = 220, seed: int = 7,
                           contract_multiplier: float = 20.0) -> pd.DataFrame:
    """Return a DataFrame using TradingView export column names.

    Models a 2:1 reward:risk SMC strategy: losers cluster near -1R, winners
    near +2R, with win rate drifting down across the sample.
    """
    rng = np.random.default_rng(seed)

    risk_pts = 25.0          # ~1R in NQ points
    rr = 2.0
    start = pd.Timestamp("2024-01-02 09:35")

    rows = []
    cum = 0.0
    t = start
    for i in range(1, n + 1):
        # Win rate decays from ~52% early to ~38% late (regime degradation).
        frac = i / n
        win_rate = 0.52 - 0.14 * frac
        is_win = rng.random() < win_rate

        # Outcome in points, with noise so trades aren't identical.
        if is_win:
            pts = rng.normal(rr * risk_pts, risk_pts * 0.35)
            pts = max(pts, risk_pts * 0.3)
        else:
            pts = -abs(rng.normal(risk_pts, risk_pts * 0.25))

        direction = "long" if rng.random() < 0.55 else "short"
        signal = "PF Long" if direction == "long" else "PF Short"
        exit_signal = "TP" if is_win else "SL"

        profit_usd = round(pts * contract_multiplier, 2)
        cum = round(cum + profit_usd, 2)
        price = round(rng.uniform(17000, 21000), 2)

        # Advance time 2–30 hours between trades, skip weekends roughly.
        t = t + pd.Timedelta(hours=float(rng.uniform(2, 30)))
        if t.weekday() >= 5:
            t = t + pd.Timedelta(days=2)

        rows.append({
            "Trade #": i,
            "Type": f"Entry {direction}",
            "Signal": f"{signal} / {exit_signal}",
            "Date/Time": t.strftime("%Y-%m-%d %H:%M"),
            "Price": price,
            "Contracts": 1,
            "Profit": profit_usd,
            "Profit %": round(profit_usd / 50000 * 100, 3),
            "Cum. Profit": cum,
        })

    return pd.DataFrame(rows)


def save_sample_xlsx(path: str = "sample_trades.xlsx", **kwargs) -> str:
    """Write a sample export to `path` and return the path."""
    df = generate_sample_trades(**kwargs)
    df.to_excel(path, index=False, sheet_name="List of Trades")
    return path


if __name__ == "__main__":
    p = save_sample_xlsx()
    print(f"Wrote sample export -> {p}")

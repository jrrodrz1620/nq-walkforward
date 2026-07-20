"""Causal universe selection: choose instruments using FIT-era data only,
then evaluate that basket OOS. Rule: keep instruments with fit-era sum_R > 0
(the instrument trended profitably in-sample); cap equity-index count at 4 by
fit-era rank to avoid stacking correlated exposure. Compare against all-25 and
the a-priori 9."""
import sys

sys.path.insert(0, "/home/user/nq-walkforward")
sys.path.insert(0, "/home/user/nq-walkforward/vibe-trading-eval")
from trend_system import (SystemConfig, UNIVERSE, generate_all_trades,
                          load_universe, simulate_portfolio)

NINE = ["NAS100_USD", "SPX500_USD", "JP225_USD", "EUR_USD", "GBP_USD",
        "XAU_USD", "WTICO_USD", "CORN_USD", "USB10Y_USD"]

cfg = SystemConfig(channel_len=55, trail_atr=5.0)
data = load_universe()
trades = generate_all_trades(data, cfg)
fit_tr = trades[trades["entry_time"] < cfg.fit_end]

# fit-era per-instrument performance
perf = fit_tr.groupby("inst")["r_multiple"].agg(["sum", "count"]).rename(columns={"sum": "sum_R"})
perf["class"] = [UNIVERSE[i] for i in perf.index]
print("FIT-era per-instrument sum_R:")
print(perf.sort_values("sum_R", ascending=False).to_string())

# rule: sum_R > 0 in fit era; max 4 equity indices by fit-era rank
positive = perf[perf["sum_R"] > 0].sort_values("sum_R", ascending=False)
eq = positive[positive["class"] == "equity"].head(4).index.tolist()
rest = positive[positive["class"] != "equity"].index.tolist()
selected = sorted(eq + rest)
print(f"\nSelected ({len(selected)}): {selected}")

for label, univ in [("ALL 25", None), ("FIT-SELECTED", selected), ("A-PRIORI 9", NINE)]:
    tr = trades if univ is None else trades[trades["inst"].isin(univ)]
    oos = simulate_portfolio(tr, cfg, start=cfg.fit_end)
    fit = simulate_portfolio(tr, cfg, end=cfg.fit_end)
    print(f"{label:<14} FIT Sharpe {fit['sharpe']:>5}  |  OOS ret {oos['ret_pct']:>6}%  "
          f"DD {oos['max_dd_pct']:>6}%  Sharpe {oos['sharpe']:>5}  ret/DD {oos['ret_over_dd']}")

"""Fit-era grid + OOS diagnosis for trend_system.
Grid (channel_len, trail_atr) on 2005-2012 portfolio Sharpe, then evaluate the
winner OOS. Also decompose the OOS result: full 25-inst universe vs the
original 9, with and without the vol overlay / position cap."""
import itertools
import sys

sys.path.insert(0, "/home/user/nq-walkforward")
sys.path.insert(0, "/home/user/nq-walkforward/vibe-trading-eval")
from trend_system import (SystemConfig, UNIVERSE, generate_all_trades,
                          load_universe, simulate_portfolio)

NINE = ["NAS100_USD", "SPX500_USD", "JP225_USD", "EUR_USD", "GBP_USD",
        "XAU_USD", "WTICO_USD", "CORN_USD", "USB10Y_USD"]

data = load_universe()
print(f"{len(data)} instruments")

results = []
for cl, ta in itertools.product([40, 55, 80, 100], [3.0, 4.0, 5.0]):
    cfg = SystemConfig(channel_len=cl, trail_atr=ta)
    trades = generate_all_trades(data, cfg)
    fit = simulate_portfolio(trades, cfg, end=cfg.fit_end)
    results.append((cl, ta, fit["sharpe"], fit["ret_pct"], trades))
    print(f"  ch{cl:>3} trail{ta}  FIT Sharpe {fit['sharpe']:>5}  ret {fit['ret_pct']}%")

cl, ta, s, _, trades = max(results, key=lambda x: x[2])
print(f"\nBest fit-era: channel {cl}, trail {ta} (Sharpe {s})")
cfg = SystemConfig(channel_len=cl, trail_atr=ta)

oos = simulate_portfolio(trades, cfg, start=cfg.fit_end)
print(f"OOS 25-inst, overlay+cap:   ret {oos['ret_pct']}%  DD {oos['max_dd_pct']}%  Sharpe {oos['sharpe']}")

# decomposition
t9 = trades[trades["inst"].isin(NINE)]
oos9 = simulate_portfolio(t9, cfg, start=cfg.fit_end)
print(f"OOS  9-inst, overlay+cap:   ret {oos9['ret_pct']}%  DD {oos9['max_dd_pct']}%  Sharpe {oos9['sharpe']}")

cfg_nocap = SystemConfig(channel_len=cl, trail_atr=ta, max_positions=999,
                         vol_scale_bounds=(1.0, 1.0))
oos_plain = simulate_portfolio(trades, cfg_nocap, start=cfg.fit_end)
print(f"OOS 25-inst, no overlay/cap: ret {oos_plain['ret_pct']}%  DD {oos_plain['max_dd_pct']}%  Sharpe {oos_plain['sharpe']}")

oos9_plain = simulate_portfolio(t9, cfg_nocap, start=cfg.fit_end)
print(f"OOS  9-inst, no overlay/cap: ret {oos9_plain['ret_pct']}%  DD {oos9_plain['max_dd_pct']}%  Sharpe {oos9_plain['sharpe']}")

# per-instrument OOS R to see which additions drag
print("\nPer-instrument OOS sum_R (fitted params):")
oos_tr = trades[trades["entry_time"] >= cfg.fit_end]
for inst, grp in oos_tr.groupby("inst"):
    tag = "*" if inst in NINE else " "
    print(f"  {tag}{inst:<12} {len(grp):>4} tr  sum_R {grp['r_multiple'].sum():>7.1f}")

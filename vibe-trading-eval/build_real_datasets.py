"""Rebuild the real NAS100 datasets used in REAL_DATA_RESULTS.md.

Source: github.com/FutureSharks/financial-data (Oanda NAS100_USD 1-min CSVs).
Clone it (sparse-checkout pyfinancialdata/data/currencies/oanda/NAS100_USD
keeps it to ~208 MB) and point DATA_DIR at the NAS100_USD directory.
"""
import glob
import sys

import pandas as pd

DATA_DIR = sys.argv[1] if len(sys.argv) > 1 else "financial-data/pyfinancialdata/data/currencies/oanda/NAS100_USD"

SETS = [
    ("nas100_15m_2019_2020.csv", "15min", range(2019, 2021)),
    ("nas100_15m_2016_2018.csv", "15min", range(2016, 2019)),
    ("nas100_1h_2016_2020.csv", "1h", range(2016, 2021)),
]

for out, rule, years in SETS:
    files = sorted(f for y in years for f in glob.glob(f"{DATA_DIR}/{y}/*.csv"))
    if not files:
        print(f"skip {out}: no files under {DATA_DIR}")
        continue
    df = pd.concat([pd.read_csv(f, parse_dates=["time"]) for f in files]).sort_values("time")
    df = df.drop_duplicates(subset="time").set_index("time")
    bars = (df.resample(rule)
              .agg({"open": "first", "high": "max", "low": "min",
                    "close": "last", "volume": "sum"})
              .dropna(subset=["open"]).reset_index())
    bars.to_csv(out, index=False)
    print(f"{out}: {len(bars)} bars  {bars['time'].min()} -> {bars['time'].max()}")

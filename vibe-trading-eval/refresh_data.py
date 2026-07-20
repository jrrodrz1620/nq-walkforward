"""Ingest fresh daily OHLC (e.g. TradingView 'Export chart data') into the
trend system's data layout, so `trend_system.py oos` can re-validate on
2020-2026 markets — the top blocking item in SYSTEM.md.

    python refresh_data.py <export_dir>

Drop one daily-bars CSV per instrument into <export_dir>, named by instrument
(e.g. NAS100_USD.csv) or by a TradingView symbol listed in SYMBOL_MAP (e.g.
"OANDA_EURUSD, 1D.csv" works — matching is fuzzy on the known symbols).
Accepted columns: time/date + open/high/low/close (TradingView's export
format). Each file is sanity-checked (OHLC ordering, monotonic dates, plausible
gap vs the existing series) and appended to data/universe_daily/<inst>.csv,
after which the OOS re-validation instructions are printed.

Suggested TradingView symbols for CORE_9 (daily chart -> Export chart data):
  NAS100_USD  CFD "NAS100USD" (Oanda) or CME_MINI:NQ1!
  SPX500_USD  "SPX500USD" or CME_MINI:ES1!
  JP225_USD   "JP225USD"  or NKD1!
  EUR_USD     OANDA:EURUSD      GBP_USD  OANDA:GBPUSD
  XAU_USD     OANDA:XAUUSD      WTICO_USD "WTICOUSD" or NYMEX:CL1!
  CORN_USD    "CORNUSD" or CBOT:ZC1!    USB10Y_USD "USB10YUSD" or CBOT:ZN1!
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

DATA_DIR = Path("/home/user/vibe-eval/data/universe_daily")

CORE_9 = ["NAS100_USD", "SPX500_USD", "JP225_USD", "EUR_USD", "GBP_USD",
          "XAU_USD", "WTICO_USD", "CORN_USD", "USB10Y_USD"]

SYMBOL_MAP = {  # fuzzy keys (lowercased, non-alnum stripped) -> instrument
    "nas100usd": "NAS100_USD", "nq1": "NAS100_USD", "nas100": "NAS100_USD",
    "spx500usd": "SPX500_USD", "es1": "SPX500_USD", "spx500": "SPX500_USD",
    "jp225usd": "JP225_USD", "nkd1": "JP225_USD", "jp225": "JP225_USD",
    "eurusd": "EUR_USD", "gbpusd": "GBP_USD", "xauusd": "XAU_USD",
    "wticousd": "WTICO_USD", "cl1": "WTICO_USD", "usoil": "WTICO_USD",
    "cornusd": "CORN_USD", "zc1": "CORN_USD",
    "usb10yusd": "USB10Y_USD", "zn1": "USB10Y_USD", "us10y": "USB10Y_USD",
}


def _norm(s: str) -> str:
    return "".join(ch for ch in s.lower() if ch.isalnum())


def match_instrument(filename: str) -> str | None:
    stem = _norm(Path(filename).stem)
    for inst in CORE_9:
        if _norm(inst) in stem:
            return inst
    for key, inst in SYMBOL_MAP.items():
        if key in stem:
            return inst
    return None


def load_export(path: Path) -> pd.DataFrame:
    raw = pd.read_csv(path)
    cols = {c.lower().strip(): c for c in raw.columns}

    def pick(*names):
        for n in names:
            if n in cols:
                return cols[n]
        raise ValueError(f"{path.name}: none of {names} in columns {list(raw.columns)}")

    df = pd.DataFrame({
        "time": pd.to_datetime(raw[pick("time", "date", "datetime")], utc=True).dt.tz_localize(None),
        "open": pd.to_numeric(raw[pick("open", "o")], errors="coerce"),
        "high": pd.to_numeric(raw[pick("high", "h")], errors="coerce"),
        "low": pd.to_numeric(raw[pick("low", "l")], errors="coerce"),
        "close": pd.to_numeric(raw[pick("close", "c")], errors="coerce"),
    }).dropna().sort_values("time").reset_index(drop=True)
    df["time"] = df["time"].dt.normalize()
    df = df.drop_duplicates(subset="time")
    df["volume"] = 0
    return df


def sanity_check(df: pd.DataFrame, name: str) -> list[str]:
    problems = []
    bad = ((df["high"] < df["low"]) | (df["high"] < df["open"]) | (df["high"] < df["close"])
           | (df["low"] > df["open"]) | (df["low"] > df["close"]))
    if bad.any():
        problems.append(f"{name}: {bad.sum()} rows violate OHLC ordering")
    if (df["close"] <= 0).any():
        problems.append(f"{name}: non-positive prices")
    jumps = df["close"].pct_change().abs()
    if (jumps > 0.5).any():
        problems.append(f"{name}: {(jumps > 0.5).sum()} day-over-day moves >50% — check the export")
    return problems


def merge(inst: str, new: pd.DataFrame) -> tuple[int, str]:
    """Append new bars to the existing series; never rewrite history."""
    target = DATA_DIR / f"{inst}.csv"
    if target.exists():
        old = pd.read_csv(target, parse_dates=["time"])
        cutoff = old["time"].max()
        add = new[new["time"] > cutoff]
        overlap = new[(new["time"] <= cutoff) & (new["time"] >= cutoff - pd.Timedelta(days=30))]
        note = ""
        if len(overlap) >= 3:
            joined = overlap.merge(old, on="time", suffixes=("_new", "_old"))
            if len(joined):
                diff = (joined["close_new"] / joined["close_old"] - 1).abs().median()
                note = f"overlap check: median close diff {diff:.2%} on {len(joined)} shared days"
                if diff > 0.05:
                    note += "  <-- LARGE: different price basis (futures vs CFD?) — review before trusting"
        out = pd.concat([old, add]).sort_values("time").drop_duplicates(subset="time")
    else:
        add, out, note = new, new, "new series"
    out.to_csv(target, index=False)
    return len(add), note


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    export_dir = Path(sys.argv[1])
    files = sorted(p for p in export_dir.glob("*.csv"))
    if not files:
        print(f"No CSVs in {export_dir}")
        sys.exit(1)

    seen, all_problems = set(), []
    for f in files:
        inst = match_instrument(f.name)
        if inst is None:
            print(f"  skip {f.name}: no instrument match (rename to e.g. NAS100_USD.csv)")
            continue
        df = load_export(f)
        problems = sanity_check(df, f.name)
        all_problems += problems
        if problems:
            print(f"  REJECT {f.name} -> {inst}: " + "; ".join(problems))
            continue
        added, note = merge(inst, df)
        seen.add(inst)
        print(f"  ok {f.name} -> {inst}: +{added} bars through {df['time'].max().date()}  ({note})")

    missing = [i for i in CORE_9 if i not in seen]
    print(f"\nRefreshed {len(seen)}/9 instruments." +
          (f"  Missing: {', '.join(missing)}" if missing else ""))
    if seen and not all_problems:
        print("Next: python trend_system.py oos   (re-validate incl. the new era)\n"
              "Then: python trend_system.py orders")


if __name__ == "__main__":
    main()

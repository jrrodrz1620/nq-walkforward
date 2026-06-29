"""
Loading / normalization of TradingView "List of Trades" exports.

Extracted from app.py so the loader can be imported and unit-tested without
starting Streamlit. Accepts either a file path or a file-like object (the
Streamlit uploader passes the latter).
"""
from __future__ import annotations

import pandas as pd


TRADINGVIEW_COL_MAP = {
    "Trade #":          "trade_num",
    "Type":             "type",
    "Signal":           "signal",
    "Date/Time":        "entry_time",
    "Price":            "entry_price",
    "Contracts":        "contracts",
    "Profit":           "profit_usd",
    "Profit %":         "profit_pct",
    "Cum. Profit":      "cum_profit",
    "Run-up":           "runup",
    "Run-up %":         "runup_pct",
    "Drawdown":         "drawdown",
    "Drawdown %":       "drawdown_pct",
    "Entry Date/Time":  "entry_time",
    "Exit Date/Time":   "exit_time",
    "Entry Price":      "entry_price",
    "Exit Price":       "exit_price",
    "Net Profit":       "profit_usd",
    "Net Profit %":     "profit_pct",
}


def _is_csv(file) -> bool:
    name = getattr(file, "name", file)
    return isinstance(name, str) and name.lower().endswith(".csv")


def load_and_clean(file, contract_multiplier: float) -> pd.DataFrame:
    """Load XLSX/CSV and normalize to the analyzer's internal schema."""
    if _is_csv(file):
        raw = pd.read_csv(file)
    else:
        xls = pd.ExcelFile(file)
        sheet = xls.sheet_names[0]
        # Find the header row (TradingView sometimes prepends summary rows).
        preview = pd.read_excel(file, sheet_name=sheet, header=None, nrows=10)
        header_row = 0
        for i, row in preview.iterrows():
            if any(str(v).strip() in ("Trade #", "Type", "Signal") for v in row.values):
                header_row = i
                break
        raw = pd.read_excel(file, sheet_name=sheet, header=header_row)

    rename = {k: v for k, v in TRADINGVIEW_COL_MAP.items() if k in raw.columns}
    df = raw.rename(columns=rename)

    if "trade_num" in df.columns:
        df = df[pd.to_numeric(df["trade_num"], errors="coerce").notna()]

    for col in ["entry_time", "exit_time"]:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce")

    for col in ["profit_usd", "cum_profit", "runup", "drawdown"]:
        if col in df.columns:
            df[col] = pd.to_numeric(
                df[col].astype(str).str.replace(r"[$,%]", "", regex=True).str.strip(),
                errors="coerce",
            )

    if "entry_time" not in df.columns:
        time_cols = [c for c in df.columns if "date" in c.lower() or "time" in c.lower()]
        if time_cols:
            df["entry_time"] = pd.to_datetime(df[time_cols[0]], errors="coerce")

    if "entry_time" not in df.columns or "profit_usd" not in df.columns:
        return pd.DataFrame()

    df = df.dropna(subset=["entry_time", "profit_usd"])
    df = df.sort_values("entry_time").reset_index(drop=True)

    if contract_multiplier > 0:
        df["profit_pts"] = df["profit_usd"] / contract_multiplier

    return df

"""
Tests for the TradingView export loader (dataio.load_and_clean).

Covers the old single-row format, the old two-row Entry/Exit format, and the
new 2024+ format ("Trade number" / "Net PnL USD" with PnL repeated on both
rows). Run with `pytest test_dataio.py` or `python test_dataio.py`.
"""
from __future__ import annotations

import io

import pandas as pd

from dataio import load_and_clean


def _csv(text: str):
    buf = io.StringIO(text)
    buf.name = "trades.csv"   # load_and_clean sniffs .csv via .name
    return buf


def test_new_format_collapses_entry_exit_pairs():
    # New format: two rows per trade, Net PnL repeated on both.
    text = (
        "Trade number,Type,Date and time,Signal,Price USD,Net PnL USD,Cumulative PnL USD\n"
        "1,Exit long,2025-06-30 03:10,Short,22860.25,42.5,42.5\n"
        "1,Entry long,2025-06-29 22:10,Long,22839,42.5,42.5\n"
        "2,Exit short,2025-06-30 05:30,Long,22903.25,-86,-43.5\n"
        "2,Entry short,2025-06-30 03:10,Short,22860.25,-86,-43.5\n"
    )
    df = load_and_clean(_csv(text), 2.0)
    assert len(df) == 2                                   # collapsed, not 4
    assert round(df["profit_usd"].sum(), 2) == -43.5      # matches cumulative
    # Entry row's time is kept (22:10 for trade 1, not the 03:10 exit).
    t1 = df[df["trade_num"] == 1]["entry_time"].iloc[0]
    assert str(t1) == "2025-06-29 22:10:00"


def test_old_two_row_format_profit_on_exit_row():
    # Old format: profit lives on the exit row, the entry row is blank.
    text = (
        "Trade #,Type,Date/Time,Signal,Price,Profit\n"
        "1,Exit long,2025-01-02 10:00,Sell,101,50\n"
        "1,Entry long,2025-01-02 09:00,Buy,100,\n"
    )
    df = load_and_clean(_csv(text), 1.0)
    assert len(df) == 1
    assert df["profit_usd"].iloc[0] == 50
    assert str(df["entry_time"].iloc[0]) == "2025-01-02 09:00:00"


def test_single_row_per_trade_passes_through():
    text = (
        "Trade #,Type,Signal,Date/Time,Price,Profit\n"
        "1,Entry long,Long,2025-01-02 09:00,100,50\n"
        "2,Entry short,Short,2025-01-03 09:00,110,-20\n"
        "3,Entry long,Long,2025-01-04 09:00,105,30\n"
    )
    df = load_and_clean(_csv(text), 1.0)
    assert len(df) == 3
    assert round(df["profit_usd"].sum(), 2) == 60


def test_missing_required_columns_returns_empty():
    df = load_and_clean(_csv("foo,bar\n1,2\n"), 1.0)
    assert df.empty


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        try:
            fn(); print(f"  PASS  {fn.__name__}"); passed += 1
        except AssertionError as e:
            print(f"  FAIL  {fn.__name__}  {e}")
        except Exception as e:  # noqa: BLE001
            print(f"  ERROR {fn.__name__}  {type(e).__name__}: {e}")
    print(f"\n{passed}/{len(fns)} tests passed")
    raise SystemExit(0 if passed == len(fns) else 1)

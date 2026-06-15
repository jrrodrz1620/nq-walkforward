import io
import pandas as pd
import pytest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from helpers import load_and_clean
from tests.conftest import CsvFile, XlsxFile


def make_csv(rows: list[dict]) -> CsvFile:
    df = pd.DataFrame(rows)
    return CsvFile(df.to_csv(index=False))


def make_xlsx(rows: list[dict], sheet_name: str = "Sheet1") -> XlsxFile:
    buf = io.BytesIO()
    pd.DataFrame(rows).to_excel(buf, index=False, sheet_name=sheet_name)
    buf.seek(0)
    return XlsxFile(buf.read())


STANDARD_ROWS = [
    {"Trade #": 1, "Date/Time": "2024-01-01", "Profit": 100.0},
    {"Trade #": 2, "Date/Time": "2024-01-02", "Profit": -50.0},
    {"Trade #": 3, "Date/Time": "2024-01-03", "Profit": 200.0},
]


class TestLoadAndCleanCsv:
    def test_returns_dataframe(self):
        f = make_csv(STANDARD_ROWS)
        df = load_and_clean(f, contract_multiplier=20)
        assert isinstance(df, pd.DataFrame)
        assert not df.empty

    def test_entry_time_parsed(self):
        f = make_csv(STANDARD_ROWS)
        df = load_and_clean(f, contract_multiplier=20)
        assert pd.api.types.is_datetime64_any_dtype(df["entry_time"])

    def test_profit_usd_numeric(self):
        f = make_csv(STANDARD_ROWS)
        df = load_and_clean(f, contract_multiplier=20)
        assert pd.api.types.is_numeric_dtype(df["profit_usd"])

    def test_profit_usd_values(self):
        f = make_csv(STANDARD_ROWS)
        df = load_and_clean(f, contract_multiplier=20)
        assert list(df["profit_usd"]) == [100.0, -50.0, 200.0]

    def test_sorted_by_entry_time(self):
        rows = [
            {"Trade #": 2, "Date/Time": "2024-01-03", "Profit": 200.0},
            {"Trade #": 1, "Date/Time": "2024-01-01", "Profit": 100.0},
        ]
        f = make_csv(rows)
        df = load_and_clean(f, contract_multiplier=20)
        assert df["entry_time"].is_monotonic_increasing

    def test_profit_pts_computed(self):
        f = make_csv(STANDARD_ROWS)
        df = load_and_clean(f, contract_multiplier=20)
        assert "profit_pts" in df.columns
        expected = [100 / 20, -50 / 20, 200 / 20]
        assert list(df["profit_pts"]) == expected

    def test_no_profit_pts_when_multiplier_zero(self):
        f = make_csv(STANDARD_ROWS)
        df = load_and_clean(f, contract_multiplier=0)
        assert "profit_pts" not in df.columns


class TestLoadAndCleanCsvSymbols:
    def test_dollar_sign_stripped(self):
        rows = [{"Trade #": 1, "Date/Time": "2024-01-01", "Profit": "$150.00"}]
        f = make_csv(rows)
        df = load_and_clean(f, contract_multiplier=1)
        assert df["profit_usd"].iloc[0] == 150.0

    def test_profit_pct_column_present(self):
        # "Profit %" is renamed to profit_pct but is NOT in the numeric cleaning
        # loop — the raw string value is preserved. This documents current behavior;
        # a future improvement would add profit_pct to the cleaning pass.
        rows = [
            {"Trade #": 1, "Date/Time": "2024-01-01", "Profit": "200.0",
             "Profit %": "4.5%"}
        ]
        f = make_csv(rows)
        df = load_and_clean(f, contract_multiplier=1)
        assert "profit_pct" in df.columns

    def test_non_numeric_trade_num_rows_dropped(self):
        rows = [
            {"Trade #": 1,      "Date/Time": "2024-01-01", "Profit": 100.0},
            {"Trade #": "N/A",  "Date/Time": "2024-01-02", "Profit": -50.0},
            {"Trade #": 3,      "Date/Time": "2024-01-03", "Profit": 200.0},
        ]
        f = make_csv(rows)
        df = load_and_clean(f, contract_multiplier=1)
        assert len(df) == 2
        assert list(df["profit_usd"]) == [100.0, 200.0]


class TestLoadAndCleanMissingColumns:
    def test_missing_date_returns_empty(self):
        rows = [{"Trade #": 1, "Profit": 100.0}]
        f = make_csv(rows)
        df = load_and_clean(f, contract_multiplier=20)
        assert df.empty

    def test_missing_profit_returns_empty(self):
        rows = [{"Trade #": 1, "Date/Time": "2024-01-01"}]
        f = make_csv(rows)
        df = load_and_clean(f, contract_multiplier=20)
        assert df.empty

    def test_fallback_date_column_detected(self):
        rows = [
            {"Trade #": 1, "timestamp": "2024-01-01", "Profit": 100.0},
            {"Trade #": 2, "timestamp": "2024-01-02", "Profit": -50.0},
        ]
        f = make_csv(rows)
        df = load_and_clean(f, contract_multiplier=1)
        assert not df.empty
        assert "entry_time" in df.columns


class TestLoadAndCleanXlsx:
    def test_reads_xlsx(self):
        f = make_xlsx(STANDARD_ROWS)
        df = load_and_clean(f, contract_multiplier=20)
        assert not df.empty
        assert len(df) == 3

    def test_xlsx_profit_values(self):
        f = make_xlsx(STANDARD_ROWS)
        df = load_and_clean(f, contract_multiplier=20)
        assert list(df["profit_usd"]) == [100.0, -50.0, 200.0]

    def test_xlsx_header_row_detection(self):
        # Simulate extra metadata rows before the real header (row index 2)
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            # Two junk rows, then the real data starting at row 3
            meta = pd.DataFrame([["Report Title"], ["Generated by TV"]])
            meta.to_excel(writer, index=False, header=False, sheet_name="Sheet1",
                          startrow=0)
            real = pd.DataFrame(STANDARD_ROWS)
            real.to_excel(writer, index=False, sheet_name="Sheet1", startrow=2)
        buf.seek(0)
        f = XlsxFile(buf.read())
        df = load_and_clean(f, contract_multiplier=20)
        # Header detection finds "Trade #" in row 2 and parses correctly
        assert not df.empty


class TestLoadAndCleanAlternativeColNames:
    def test_net_profit_mapped(self):
        rows = [
            {"Trade #": 1, "Entry Date/Time": "2024-01-01", "Net Profit": 300.0},
            {"Trade #": 2, "Entry Date/Time": "2024-01-02", "Net Profit": -100.0},
        ]
        f = make_csv(rows)
        df = load_and_clean(f, contract_multiplier=1)
        assert not df.empty
        assert list(df["profit_usd"]) == [300.0, -100.0]

import io
import pandas as pd
import numpy as np
import pytest


def make_trades(profits, start="2024-01-01"):
    """Build a minimal trade DataFrame from a list of profit values."""
    n = len(profits)
    times = pd.date_range(start=start, periods=n, freq="D")
    return pd.DataFrame({"entry_time": times, "profit_usd": profits})


class CsvFile(io.StringIO):
    """StringIO with a .name attribute so it looks like a Streamlit uploaded file."""
    def __init__(self, content: str, name: str = "test.csv"):
        super().__init__(content)
        self.name = name


class XlsxFile(io.BytesIO):
    """BytesIO with a .name attribute so it looks like a Streamlit uploaded file."""
    def __init__(self, data: bytes, name: str = "test.xlsx"):
        super().__init__(data)
        self.name = name


@pytest.fixture
def sample_trades():
    profits = [100, -50, 200, -30, 150, -80, 120, -40, 90, -60,
               80, -20, 300, -100, 50, -70, 110, -90, 140, -30]
    return make_trades(profits)


@pytest.fixture
def all_wins():
    return make_trades([100, 200, 150, 80, 120])


@pytest.fixture
def all_losses():
    return make_trades([-100, -200, -150, -80, -120])


@pytest.fixture
def single_trade():
    return make_trades([500])


@pytest.fixture
def empty_trades():
    return pd.DataFrame({"entry_time": pd.Series([], dtype="datetime64[ns]"),
                         "profit_usd": pd.Series([], dtype="float64")})

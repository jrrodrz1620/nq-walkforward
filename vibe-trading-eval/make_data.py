"""Generate the shared NQ dataset both pipelines consume."""
import sys

sys.path.insert(0, "/home/user/nq-walkforward")
from backtest import generate_ohlc  # noqa: E402

df = generate_ohlc(n_bars=6000, seed=11)
df["volume"] = 1000  # local_loader validates OHLCV; constant volume is fine
df.to_csv("/home/user/vibe-eval/data/nq_bars.csv", index=False)
print(df["time"].min(), "->", df["time"].max(), len(df), "bars")

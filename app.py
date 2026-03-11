This error confirms that **explanatory text** (specifically the line `1. **Smart Quotes Syntax Error:**...`) was accidentally pasted into your Python file. Python cannot read Markdown explanations.

Please **delete everything** currently in your `app.py` file and replace it entirely with the clean code below. This version uses only standard Python quotes and fixes the variable scope issues.

```python
import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
import plotly.express as px
import io

# Ensure openpyxl is installed for Excel writing: pip install openpyxl
try:
    import openpyxl
except ImportError:
    pass

# ─────────────────────────────────────────────
# PAGE CONFIG
# ─────────────────────────────────────────────

st.set_page_config(
    page_title="Walk-Forward Analyzer",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded"
)

st.markdown("""
<style>
    .main { background-color: #0e1117; }
    .stMetric { background-color: #1a1d24; border-radius: 8px; padding: 10px; }
    .block-container { padding-top: 1rem; }
</style>
""", unsafe_allow_html=True)

# ─────────────────────────────────────────────
# SIDEBAR
# ─────────────────────────────────────────────

st.sidebar.title("⚙️ Walk-Forward Settings")

uploaded_file = st.sidebar.file_uploader(
    "Upload TradingView Backtest XLSX",
    type=["xlsx", "xls", "csv"],
    help="Export from TradingView → Strategy Tester → Export → List of Trades"
)

st.sidebar.markdown("---")
st.sidebar.subheader("Window Settings")
n_folds       = st.sidebar.slider("Number of Folds",        2, 10, 5)
train_pct     = st.sidebar.slider("Train %",                50, 85, 70)
min_trades    = st.sidebar.slider("Min Trades per Fold",    3, 20, 5)

st.sidebar.markdown("---")
st.sidebar.subheader("Risk Settings")
starting_capital = st.sidebar.number_input("Starting Capital ($)", value=50000, step=1000)
contract_value   = st.sidebar.number_input("Points → $ Multiplier", value=20.0, step=1.0,
    help="NQ=20, MNQ=2, ES=50, MES=5")

st.sidebar.markdown("---")
st.sidebar.subheader("Column Mapping")
st.sidebar.caption("Auto-detected from TradingView export. Override if needed.")

# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

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

def load_and_clean(file, contract_multiplier) -> pd.DataFrame:
    """Load XLSX/CSV and normalize to internal schema."""
    if file.name.endswith(".csv"):
        raw = pd.read_csv(file)
    else:
        xls = pd.ExcelFile(file)
        sheet = xls.sheet_names[0]
        # Try to find header row
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
                errors="coerce"
            )

    if "entry_time" not in df.columns:
        time_cols = [c for c in df.columns if "date" in c.lower() or "time" in c.lower()]
        if time_cols:
            df["entry_time"] = pd.to_datetime(df[time_cols[0]], errors="coerce")

    if "entry_time" not in df.columns or "profit_usd" not in df.columns:
        return pd.DataFrame() # Return empty if critical columns missing

    df = df.dropna(subset=["entry_time", "profit_usd"])
    df = df.sort_values("entry_time").reset_index(drop=True)

    if contract_multiplier > 0:
        df["profit_pts"] = df["profit_usd"] / contract_multiplier

    return df

def split_folds(df: pd.DataFrame, n: int, train_pct: float):
    """Split trades into walk-forward folds."""
    total = len(df)
    fold_size = total // n
    folds = []
    for i in range(n):
        start = i * fold_size
        end   = start + fold_size if i < n - 1 else total
        chunk = df.iloc[start:end].copy()
        split = int(len(chunk) * train_pct / 100)
        train = chunk.iloc[:split]
        test  = chunk.iloc[split:]
        
        if len(train) >= min_trades and len(test) >= 1:
            folds.append({
                "fold":       i + 1,
                "train":      train,
                "test":       test,
                "train_start": train["entry_time"].iloc[0],
                "train_end":   train["entry_time"].iloc[-1],
                "test_start":  test["entry_time"].iloc[0],
                "test_end":    test["entry_time"].iloc[-1],
            })
    return folds

def calc_metrics(trades: pd.DataFrame, capital: float) -> dict:
    """Calculate performance metrics for a set of trades."""
    if len(trades) == 0:
        return {
            "n_trades": 0, "net_profit": 0, "win_rate": 0, 
            "avg_win": 0, "avg_loss": 0, "profit_factor": 0, 
            "max_dd": 0, "return_pct": 0
        }
    
    pnl = trades["profit_usd"].values
    wins  = pnl[pnl > 0]
    losses = pnl[pnl < 0]

    net_profit = pnl.sum()
    win_rate   = len(wins) / len(pnl) * 100 if len(pnl) > 0 else 0
    avg_win    = wins.mean()  if len(wins)   > 0 else 0
    avg_loss   = losses.mean() if len(losses) > 0 else 0
    profit_factor = abs(wins.sum() / losses.sum()) if losses.sum() != 0 else np.inf

    equity = capital + np.cumsum(pnl)
    peak   = np.maximum.accumulate(equity)
    dd     = (equity - peak) / peak * 100
    max_dd = dd.min()

    return {
        "n_trades":      len(pnl),
        "net_profit":    net_profit,
        "win_rate":      win_rate,
        "avg_win":       avg_win,
        "avg_loss":      avg_loss,
        "profit_factor": profit_factor,
        "max_dd":        max_dd,
        "equity":        equity,
        "return_pct":    net_profit / capital * 100,
    }

def equity_curve_fig(folds, capital):
    """Build OOS equity curve across all folds."""
    fig = go.Figure()
    running_capital = capital
    
    for f in folds:
        test = f["test"]
        pnl  = test["profit_usd"].values
        eq   = running_capital + np.cumsum(pnl)
        times = test["entry_time"].values

        fig.add_trace(go.Scatter(
            x=times, y=eq,
            mode="lines",
            name=f"Fold {f['fold']} OOS",
            line=dict(width=2),
        ))
        if len(eq) > 0:
            running_capital = eq[-1]

    fig.update_layout(
        title="Out-of-Sample Equity Curve (All Folds)",
        xaxis_title="Date",
        yaxis_title="Account Value ($)",
        template="plotly_dark",
        height=400,
        legend=dict(orientation="h", yanchor="bottom", y=1.02),
        hovermode="x unified",
    )
    return fig

def gantt_fig(folds):
    """Gantt chart of train/test windows per fold."""
    rows = []
    for f in folds:
        rows.append(dict(Fold=f"Fold {f['fold']}", Start=f["train_start"], End=f["train_end"],   Phase="Train"))
        rows.append(dict(Fold=f"Fold {f['fold']}", Start=f["test_start"],  End=f["test_end"],    Phase="Test"))

    df_g = pd.DataFrame(rows)
    color_map = {"Train": "#1f77b4", "Test": "#ff7f0e"}
    fig = px.timeline(df_g, x_start="Start", x_end="End", y="Fold", color="Phase",
                      color_discrete_map=color_map, title="Walk-Forward Window Layout")
    fig.update_layout(template="plotly_dark", height=300)
    return fig

def fold_table(folds, capital):
    """Per-fold metrics table."""
    rows = []
    for f in folds:
        tm = calc_metrics(f["train"], capital)
        om = calc_metrics(f["test"],  capital)
        rows.append({
            "Fold":          f["fold"],
            "Train Trades":  tm.get("n_trades", 0),
            "Train WR%":     round(tm.get("win_rate", 0), 1),
            "Train PF":      round(tm.get("profit_factor", 0), 2),
            "OOS Trades":    om.get("n_trades", 0),
            "OOS WR%":       round(om.get("win_rate", 0), 1),
            "OOS PF":        round(om.get("profit_factor", 0), 2),
            "OOS Net $":     round(om.get("net_profit", 0), 2),
            "OOS MaxDD%":    round(om.get("max_dd", 0), 2),
        })
    return pd.DataFrame(rows)

# ─────────────────────────────────────────────
# MAIN APP
# ─────────────────────────────────────────────

st.title("Walk-Forward Analyzer — TradingView XLSX")
st.caption("Upload your TradingView backtest export -> get out-of-sample performance analysis")

if uploaded_file is None:
    st.info("Upload a TradingView backtest XLSX file to get started.")
    st.stop()

# ── Load data ──

try:
    df = load_and_clean(uploaded_file, contract_value)
except Exception as e:
    st.error(f"Failed to load file: {e}")
    st.stop()

if df.empty:
    st.error("Could not find required columns (Date/Time, Profit) in the file.")
    st.stop()

if len(df) < 10:
    st.warning(f"Only {len(df)} trades found — need at least 10 to run walk-forward.")
    st.dataframe(df)
    st.stop()

st.success(f"Loaded {len(df)} trades from {uploaded_file.name}")

# ── Show raw data ──

with st.expander("Raw Trade Data", expanded=False):
    st.dataframe(df, use_container_width=True)

# ── Split folds ──

folds = split_folds(df, n_folds, train_pct)
if len(folds) == 0:
    st.error("Not enough trades to create folds. Reduce Min Trades per Fold or increase data.")
    st.stop()

# ── Overall metrics ──

overall = calc_metrics(df, starting_capital)

st.markdown("---")
st.subheader("Overall Performance")
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Total Trades",    overall["n_trades"])
c2.metric("Net Profit",      f"${overall['net_profit']:,.0f}")
c3.metric("Win Rate",        f"{overall['win_rate']:.1f}%")
c4.metric("Profit Factor",   f"{overall['profit_factor']:.2f}")
c5.metric("Max Drawdown",    f"{overall['max_dd']:.1f}%")

# ── Gantt chart ──

st.markdown("---")
st.subheader("Walk-Forward Windows")
st.plotly_chart(gantt_fig(folds), use_container_width=True)

# ── OOS equity curve ──

st.subheader("Out-of-Sample Equity Curve")
st.plotly_chart(equity_curve_fig(folds, starting_capital), use_container_width=True)

# ── Per-fold table ──

st.subheader("Per-Fold Metrics")
ft = fold_table(folds, starting_capital)
st.dataframe(ft.style.background_gradient(
    subset=["OOS WR%", "OOS PF", "OOS Net $"],
    cmap="RdYlGn"
), use_container_width=True)

# ── OOS summary ──

st.markdown("---")
st.subheader("OOS Summary")

oos_all = pd.concat([f["test"] for f in folds])
if len(oos_all) > 0:
    oos_metrics = calc_metrics(oos_all, starting_capital)

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("OOS Trades",       oos_metrics["n_trades"])
    c2.metric("OOS Net Profit",   f"${oos_metrics['net_profit']:,.0f}")
    c3.metric("OOS Win Rate",     f"{oos_metrics['win_rate']:.1f}%")
    c4.metric("OOS Profit Factor",f"{oos_metrics['profit_factor']:.2f}")
    c5.metric("OOS Max DD",       f"{oos_metrics['max_dd']:.1f}%")

    # ── Overfit detection ──

    st.markdown("---")
    st.subheader("Overfit Check")

    train_all = pd.concat([f["train"] for f in folds])
    train_metrics = calc_metrics(train_all, starting_capital)

    ratio_pf = oos_metrics["profit_factor"] / train_metrics["profit_factor"] if train_metrics["profit_factor"] > 0 else 0

    col1, col2 = st.columns(2)
    with col1:
        st.markdown("**Train vs OOS Profit Factor**")
        fig_pf = go.Figure(go.Bar(
            x=["Train PF", "OOS PF"],
            y=[train_metrics["profit_factor"], oos_metrics["profit_factor"]],
            marker_color=["#1f77b4", "#ff7f0e"]
        ))
        fig_pf.update_layout(template="plotly_dark", height=250, margin=dict(t=20))
        st.plotly_chart(fig_pf, use_container_width=True)

    with col2:
        st.markdown("**Train vs OOS Win Rate**")
        fig_wr = go.Figure(go.Bar(
            x=["Train WR%", "OOS WR%"],
            y=[train_metrics["win_rate"], oos_metrics["win_rate"]],
            marker_color=["#1f77b4", "#ff7f0e"]
        ))
        fig_wr.update_layout(template="plotly_dark", height=250, margin=dict(t=20))
        st.plotly_chart(fig_wr, use_container_width=True)

    if ratio_pf >= 0.8:
        st.success(f"OOS/Train PF ratio = {ratio_pf:.2f} — Strategy looks robust.")
    elif ratio_pf >= 0.5:
        st.warning(f"OOS/Train PF ratio = {ratio_pf:.2f} — Some degradation.")
    else:
        st.error(f"OOS/Train PF ratio = {ratio_pf:.2f} — Likely overfit.")

    # ── Monthly breakdown ──

    st.markdown("---")
    st.subheader("Monthly P&L (OOS Only)")

    oos_all["month"] = oos_all["entry_time"].dt.to_period("M").astype(str)
    monthly = oos_all.groupby("month")["profit_usd"].sum().reset_index()
    monthly.columns = ["Month", "Net P&L ($)"]

    fig_monthly = go.Figure(go.Bar(
        x=monthly["Month"],
        y=monthly["Net P&L ($)"],
        marker_color=["#2ecc71" if v >= 0 else "#e74c3c" for v in monthly["Net P&L ($)"]],
    ))
    fig_monthly.update_layout(
        template="plotly_dark", height=300,
        xaxis_title="Month", yaxis_title="Net P&L ($)",
        title="Monthly OOS P&L"
    )
    st.plotly_chart(fig_monthly, use_container_width=True)

    # ── Export results ──

    st.markdown("---")
    st.subheader("Export Results")

    output = io.BytesIO()
    try:
        with pd.ExcelWriter(output, engine="openpyxl") as writer:
            ft.to_excel(writer, sheet_name="Per-Fold Metrics", index=False)
            monthly.to_excel(writer, sheet_name="Monthly OOS PnL", index=False)
            oos_all.to_excel(writer, sheet_name="OOS Trades", index=False)
        
        st.download_button(
            label="Download Results XLSX",
            data=output.getvalue(),
            file_name="walkforward_results.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
    except Exception as e:
        st.error(f"Could not generate Excel file. Ensure 'openpyxl' is installed.")
```

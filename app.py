import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
import io

from helpers import (
    load_and_clean, split_folds, calc_metrics,
    equity_curve_fig, gantt_fig, fold_table,
)

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

folds = split_folds(df, n_folds, train_pct, min_trades)
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

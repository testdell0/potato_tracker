
import streamlit as st
import pandas as pd
import plotly.express as px
import oracledb
from datetime import datetime
import numpy as np
import os
from dotenv import load_dotenv

# --------------------------
# Oracle DB Connection Details
# --------------------------
DB_USER = os.getenv("DB_USER", "your_username")
DB_PASS = os.getenv("DB_PASS", "your_password")
DB_DSN = os.getenv("DB_DSN", "your_dsn")

EXPECTED_WORK_HOURS = 8
EXPECTED_WORK_MINUTES = EXPECTED_WORK_HOURS * 60

# --------------------------
# Load Data from Oracle DB
# --------------------------
def load_data():
    conn = oracledb.connect(user=DB_USER, password=DB_PASS, dsn=DB_DSN)
    # Reads from app_activity_daily — one row per (date, app, user) with
    # time already clubbed across all focus windows for that day.
    query = """
        SELECT
            username,
            TO_CHAR(activity_date, 'YYYY-MM-DD') AS date_col,
            app_name,
            ROUND(total_seconds  / 60, 2) AS total_minutes,
            ROUND(active_seconds / 60, 2) AS active_minutes,
            ROUND((total_seconds - active_seconds) / 60, 2) AS idle_minutes,
            session_count
        FROM app_activity_daily
        ORDER BY activity_date
    """
    df = pd.read_sql(query, conn)
    conn.close()

    df.rename(columns={
        "USERNAME":      "Username",
        "DATE_COL":      "Date",
        "APP_NAME":      "App Name",
        "TOTAL_MINUTES": "Total Minutes",
        "ACTIVE_MINUTES":"Active Minutes",
        "IDLE_MINUTES":  "Idle Minutes",
        "SESSION_COUNT": "Sessions",
    }, inplace=True)

    return df

# --------------------------
# Page Config
# --------------------------
st.set_page_config(page_title="Employee Productivity Dashboard", layout="wide")

# --------------------------
# Refresh Button & Last Updated Time

# --------------------------
if "last_updated" not in st.session_state:
    st.session_state["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

if st.button("🔄 Refresh Data"):
    st.session_state["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

# Load data after refresh
df = load_data()
#copy
# After df = load_data()

# 1) Normalize App Name
df["App Name"] = df["App Name"].fillna("").str.strip()
df["App Upper"] = df["App Name"].str.upper()

# 2) Blacklist background/system
background_blacklist = {
    "SYSTEM", "IDLE", "SVCHOST", "RUNTIMEBROKER", "SHELLEXPERIENCEHOST",
    "STARTMENUEXPERIENCEHOST", "CTFLOADER", "TEXTINPUTHOST", "SEARCHAPP",
    "SEARCHHOST", "WIDGETS", "LOCKAPP", "WINDOWSMEDIAFOUNDATION", "SECURESYSTEM", "DWM"
}
df = df[~df["App Upper"].isin(background_blacklist)]

# 3) Threshold: keep only sessions with >= 10 seconds of **active** time
# You currently have minutes in the dataframe. Convert to seconds solely for filtering OR compute using your minutes.
df = df[df["Active Minutes"] * 60 >= 0]

# 4) (Optional) Allow-list
allow_list = {
    "MICROSOFT EDGE", "GOOGLE CHROME", "VISUAL STUDIO CODE", "EXCEL",
    "POWERPOINT", "WORD", "OUTLOOK", "MS-TEAMS", "WINDOWS TERMINAL"
}
# If you want to enforce allow-list, uncomment:
# df = df[df["App Upper"].isin(allow_list)]

# 5) Drop helper column
df = df.drop(columns=["App Upper"])
#copy

# Show last updated time
st.markdown(f"**Last Updated:** {st.session_state['last_updated']}")

# --------------------------
# Dashboard Title
# --------------------------
st.markdown("<h1 style='color:#D32F2F;'>📊 Employee Productivity Dashboard</h1>", unsafe_allow_html=True)

# --------------------------
# Filters
# --------------------------
usernames = sorted(df["Username"].dropna().unique())
dates = sorted(df["Date"].dropna().unique())
apps = sorted(df["App Name"].dropna().unique())

col1, col2, col3 = st.columns(3)

#    filtered_df = filtered_df[filtered_df["Username"] == selected_user]

#code

# ---------- Data prep ----------
df = df.copy()
df["Date"] = pd.to_datetime(df["Date"]).dt.date  # ensure pure date (no time)

# ---------- Single-row filter bar ----------
# 4 columns, adjust widths as you like
col_user, col_start, col_end, col_app = st.columns([1.2, 1, 1, 1.2], gap="medium")

with col_user:
    user_options = ["All"] + sorted(df["Username"].dropna().unique().tolist())
    selected_user = st.selectbox("Select Username", user_options, index=0, key="user_select_main")

# Compute bounds AFTER user selection (so dates reflect that user’s data)
tmp_df = df if selected_user == "All" else df[df["Username"] == selected_user]
min_date = tmp_df["Date"].min() if not tmp_df.empty else df["Date"].min()
max_date = tmp_df["Date"].max() if not tmp_df.empty else df["Date"].max()

with col_start:
    start_date = st.date_input(
        "Start date",
        value=min_date,
        min_value=min_date,
        max_value=max_date,
        key="start_date_main",
    )

with col_end:
    end_date = st.date_input(
        "End date",
        value=max_date,
        min_value=min_date,
        max_value=max_date,
        key="end_date_main",
    )

with col_app:
    app_options = ["All"] + sorted(df["App Name"].dropna().unique().tolist())
    #selected_app = st.selectbox("Select App", app_options, index=0, key="app_select_main")

# ---------- Apply filters in order ----------
filtered_df = df.copy()

# User filter
if selected_user != "All":
    filtered_df = filtered_df[filtered_df["Username"] == selected_user]

# App filter
# if selected_app != "All":
#     filtered_df = filtered_df[filtered_df["App Name"] == selected_app]

# Date filter (inclusive)
if start_date > end_date:
    st.error("⚠️ Start date must be on or before End date.")
else:
    mask = (filtered_df["Date"] >= start_date) & (filtered_df["Date"] <= end_date)
    filtered_df = filtered_df.loc[mask]

# ---------- Output ----------
#st.dataframe(filtered_df)

#code

#if selected_app != "All":
#    filtered_df = filtered_df[filtered_df["App Name"] == selected_app]

# --------------------------
# KPI Section (Workday Productivity & Time in hrs:mins format)
# --------------------------
def format_time(minutes):
    hours = int(minutes // 60)
    mins = int(minutes % 60)
    return f"{hours} hrs {mins} mins"

total_active_minutes = filtered_df["Active Minutes"].sum()
total_idle_minutes = filtered_df["Idle Minutes"].sum()
actual_total_minutes = filtered_df["Total Minutes"].sum()


# Workday Productivity (Active ÷ 8 hrs)
workday_productivity = round((total_active_minutes / EXPECTED_WORK_MINUTES) * 100, 2)

kpi_col1, kpi_col2, kpi_col3, kpi_col4 = st.columns(4)
with kpi_col1:
    st.metric(label="Workday Productivity (%)", value=f"{workday_productivity}%")
    st.caption("Active ÷ 8 hrs × 100")

with kpi_col2:
    st.metric(label="Total Active Time", value=format_time(total_active_minutes))

with kpi_col3:
    st.metric(label="Total Idle Time", value=format_time(total_idle_minutes))

with kpi_col4:
    st.metric(label="Actual Usage Time", value=format_time(actual_total_minutes))

# --------------------------
# Charts (Dark Theme)
# --------------------------
st.subheader("📈 Visual Insights")

# Group by App Name for productivity
grouped_df = filtered_df.groupby("App Name", as_index=False).agg({
    "Active Minutes": "sum",
    "Total Minutes": "sum"
})
grouped_df["Productivity %"] = (grouped_df["Active Minutes"] / grouped_df["Total Minutes"]) * 100

# App-wise Productivity Bar Chart
# fig_bar = px.bar(
#     grouped_df,
#     x="App Name",
#     y="Productivity %",
#     color="Productivity %",
#     color_continuous_scale=["#BD0808", "#F8F8F8"],
#     title="Average Productivity by App",
#     text="Productivity %",
#     template="plotly_dark"
# )
# fig_bar.update_traces(texttemplate='%{text:.2f}%', textposition='outside')
# st.plotly_chart(fig_bar, width="stretch")

# Productivity Trend Over Time
trend_df = filtered_df.groupby("Date", as_index=False).agg({
    "Active Minutes": "sum",
    "Total Minutes": "sum"
})
trend_df["Productivity %"] = (trend_df["Active Minutes"] / trend_df["Total Minutes"]) * 100

fig_line = px.line(
    trend_df,
    x="Date",
    y="Productivity %",
    title="Productivity Trend Over Time",
    markers=True,
    color_discrete_sequence=["#D32F2F"],
    template="plotly_dark"
)
st.plotly_chart(fig_line, width="stretch")

# --------------------------
# Top 5 Most Used Apps
# --------------------------
# st.subheader("🔥 Top 5 Most Used Apps")
# top_apps = filtered_df.groupby("App Name", as_index=False)["Total Minutes"].sum().sort_values(by="Total Minutes", ascending=False).head(5)

# fig_top = px.bar(
#     top_apps,
#     x="App Name",
#     y="Total Minutes",
#     color="Total Minutes",
#     color_continuous_scale=["#D32F2F", "#F8F8F8"],
#     title="Top 5 Apps by Usage (Minutes)",
#     text="Total Minutes",
#     template="plotly_dark"
# )
# fig_top.update_traces(texttemplate='%{text:.0f}', textposition='outside')
# st.plotly_chart(fig_top, use_container_width=True)

# --------------------------
# Data Table
# --------------------------
#st.subheader("📋 Detailed Productivity Data")
#st.dataframe(filtered_df)
# ---------- Table: show minutes as mm:ss ----------
def mins_to_mmss(m):
    # m is minutes (float). Convert to total whole seconds, then format as mm:ss
    if pd.isna(m):
        return ""
    total_secs = int(round(float(m) * 60))  # e.g., 0.77 min -> 46 sec
    minutes = total_secs // 60
    seconds = total_secs % 60
    return f"{minutes:02d}:{seconds:02d}"

# Build a display copy that shows mm:ss, while keeping numeric minutes for calc
df_display = filtered_df.copy()
time_cols = ["Total Minutes", "Active Minutes", "Idle Minutes"]

# Add formatted columns
for c in time_cols:
    df_display[c + " (mm:ss)"] = df_display[c].apply(mins_to_mmss)

st.subheader("📋 Detailed Productivity Data")

# Show mm:ss formatted time columns + session count (how many focus windows were clubbed)
st.dataframe(
    df_display[
        ["Username", "Date", "App Name",
         "Total Minutes (mm:ss)",
         "Active Minutes (mm:ss)",
         "Idle Minutes (mm:ss)",
         "Sessions"]
    ]
)

# --- If you ever want both numeric and mm:ss for sorting/debugging, use this instead:
# st.dataframe(
#     df_display[
#         ["Username", "Date", "App Name",
#          "Total Minutes", "Total Minutes (mm:ss)",
#          "Active Minutes", "Active Minutes (mm:ss)",
#          "Idle Minutes", "Idle Minutes (mm:ss)"]
#     ]
# )
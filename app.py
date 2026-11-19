"""AI Talent Market Pulse: open roles per company over time, from public Greenhouse job boards."""
from datetime import datetime, timezone
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

DATA_DIR = Path(__file__).parent / "data"
OPENINGS_CSV = DATA_DIR / "openings.csv"
RUNS_CSV = DATA_DIR / "fetch_runs.csv"

st.set_page_config(page_title="AI Talent Market Pulse", page_icon="📈", layout="wide")


@st.cache_data(ttl=3600)  # re-read the files at most once an hour
def load_csv(path):
    return pd.read_csv(path) if path.exists() else pd.DataFrame()


openings = load_csv(OPENINGS_CSV)
runs = load_csv(RUNS_CSV)

st.title("AI Talent Market Pulse")
st.caption("Open roles at selected companies, collected once a day from their public Greenhouse job boards.")

if openings.empty:
    st.warning("No data yet. Run `python pipeline.py` to collect the first snapshot.")
    st.stop()

openings["snapshot_date"] = pd.to_datetime(openings["snapshot_date"])
latest_date = openings["snapshot_date"].max()

# Freshness check: the daily job should never leave the data more than a day old
days_old = (datetime.now(timezone.utc).date() - latest_date.date()).days
if days_old > 1:
    st.warning(f"Data may be stale: the latest snapshot is {days_old} days old. Check the pipeline health below.")

# ---- Controls ----
left, right = st.columns([2, 1])
with left:
    companies = sorted(openings["company"].unique())
    selected = st.multiselect("Companies", companies, default=companies)
with right:
    measure_label = st.radio(
        "Count",
        ["Open jobs", "Open postings"],
        horizontal=True,
        help="One job can be posted in several locations. 'Open jobs' counts each job once; "
             "'Open postings' counts every listing.",
    )
measure = "open_jobs" if measure_label == "Open jobs" else "open_postings"

view = openings[openings["company"].isin(selected)]
if view.empty:
    st.info("Select at least one company.")
    st.stop()

# ---- Latest numbers, with change since the previous snapshot ----
st.subheader(f"As of {latest_date:%b %d, %Y}")
cols = st.columns(len(selected))
for col, company in zip(cols, selected):
    history = view[view["company"] == company].sort_values("snapshot_date")
    current = history[measure].iloc[-1]
    delta = current - history[measure].iloc[-2] if len(history) > 1 else None
    col.metric(company.title(), f"{current:,}", delta=None if delta is None else f"{delta:+,}")

# ---- Trend chart ----
chart = (
    alt.Chart(view)
    .mark_line(point=True)   # points keep the chart readable while there are only a few days of data
    .encode(
        x=alt.X("snapshot_date:T", title="Date"),
        y=alt.Y(f"{measure}:Q", title=measure_label),
        color=alt.Color("company:N", title="Company"),
        tooltip=[
            alt.Tooltip("company:N", title="Company"),
            alt.Tooltip("snapshot_date:T", title="Date"),
            alt.Tooltip(f"{measure}:Q", title=measure_label, format=","),
        ],
    )
    .properties(height=400)
)
st.altair_chart(chart, width="stretch")

if openings["snapshot_date"].nunique() < 3:
    st.caption("History started recently. The trend fills in as the daily job adds snapshots.")

# ---- Pipeline health: owning what you ship after launch ----
with st.expander("Pipeline health"):
    if runs.empty:
        st.write("No runs logged yet.")
    else:
        last_runs = (
            runs.sort_values("started_at")
            .groupby("company")
            .tail(1)[["company", "started_at", "status", "http_status", "postings_count", "error_message"]]
            .sort_values("company")
        )
        failures = (last_runs["status"] != "success").sum()
        if failures:
            st.error(f"{failures} company fetch(es) failed on the last run. Earlier data for them is kept.")
        else:
            st.success("Last run succeeded for every company.")
        st.dataframe(last_runs, hide_index=True, width="stretch")

        latest = openings[openings["snapshot_date"] == latest_date]
        missing = int(latest["missing_requisition_id"].sum())
        st.write(f"Data quality: {missing} posting(s) in the latest snapshot have no requisition ID.")

with st.expander("About this data"):
    st.markdown(
        """
- **Source:** each company's public Greenhouse Job Board API (no login required).
- **Schedule:** a GitHub Actions job runs the pipeline once a day and commits the new snapshot.
- **Reruns are safe:** rerunning on the same day replaces that day's numbers instead of duplicating them.
- **Failures are safe:** if a company's board is unavailable, its earlier data is kept and the failure is logged above.
- **Counting:** a job posted in several locations counts once under *Open jobs*.
"""
    )

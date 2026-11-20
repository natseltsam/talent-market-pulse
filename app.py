"""AI Talent Market Pulse: open roles per company over time, from public Greenhouse job boards."""
from datetime import datetime, timezone
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

DATA_DIR = Path(__file__).parent / "data"
OPENINGS_CSV = DATA_DIR / "openings.csv"
FUNCTIONS_CSV = DATA_DIR / "openings_by_function.csv"
LIFECYCLE_CSV = DATA_DIR / "job_lifecycle.csv"
RUNS_CSV = DATA_DIR / "fetch_runs.csv"

st.set_page_config(page_title="AI Talent Market Pulse", page_icon="📈", layout="wide")


DISPLAY_NAMES = {"openai": "OpenAI"}   # anything not listed is shown title-cased
SOURCE_NAMES = {"greenhouse": "Greenhouse", "ashby": "Ashby"}

# Categorical palette in a fixed order. Colors are assigned by company (alphabetical),
# not by rank, so filtering never repaints a company in a different color.
PALETTE = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
MIN_JOBS_FOR_MEDIAN = 5   # a median of 2 jobs is noise, not a signal


def display_name(company):
    return DISPLAY_NAMES.get(company, company.title())


@st.cache_data
def read_csv_cached(path, modified_time):
    # modified_time is part of the cache key, so the cache refreshes as soon as the file changes
    return pd.read_csv(path)


def load_csv(path):
    return read_csv_cached(path, path.stat().st_mtime) if path.exists() else pd.DataFrame()


openings = load_csv(OPENINGS_CSV)
functions = load_csv(FUNCTIONS_CSV)
lifecycle = load_csv(LIFECYCLE_CSV)
runs = load_csv(RUNS_CSV)

st.title("AI Talent Market Pulse")
st.caption(
    "Open roles at selected companies, collected once a day from their public job boards "
    "on two applicant tracking systems: Greenhouse and Ashby."
)

if openings.empty:
    st.warning("No data yet. Run `python pipeline.py` to collect the first snapshot.")
    st.stop()

openings["snapshot_date"] = pd.to_datetime(openings["snapshot_date"])
if "source" not in openings.columns:   # data saved before multi-ATS support
    openings["source"] = "greenhouse"
latest_date = openings["snapshot_date"].max()

# Freshness check: the daily job should never leave the data more than a day old
days_old = (datetime.now(timezone.utc).date() - latest_date.date()).days
if days_old > 1:
    st.warning(f"Data may be stale: the latest snapshot is {days_old} days old. Check the pipeline health below.")

if not functions.empty:
    functions["snapshot_date"] = pd.to_datetime(functions["snapshot_date"])

all_company_names = [display_name(c) for c in sorted(openings["company"].unique())]
company_colors = alt.Scale(domain=all_company_names, range=PALETTE[: len(all_company_names)])

# ---- Controls ----
left, middle, right = st.columns([2, 2, 1])
with left:
    companies = sorted(openings["company"].unique())
    selected = st.multiselect("Companies", companies, default=companies, format_func=display_name)
    selected_companies = list(selected)   # the full choice, before any view hides companies
with middle:
    chosen_functions = st.multiselect(
        "Functions",
        sorted(functions["function"].unique()) if not functions.empty else [],
        placeholder="All functions",
        help="Each company's departments are mapped to shared functions (see function_map.py), "
             "because every company names its departments differently.",
    )
with right:
    measure_label = st.radio(
        "Count",
        ["Open jobs", "Open postings"],
        horizontal=True,
        help="'Open jobs' counts each job once and is comparable across systems. "
             "'Open postings' counts public listings, which only Greenhouse splits by location.",
    )
measure = "open_jobs" if measure_label == "Open jobs" else "open_postings"

if chosen_functions:
    # Function-level history starts later than company totals, so this view covers fewer days
    keys = ["snapshot_date", "company", "source"]
    company_days = functions[keys].drop_duplicates()
    matching = (
        functions[functions["function"].isin(chosen_functions)]
        .groupby(keys, as_index=False)[["open_jobs", "open_postings"]].sum()
    )
    # A company with no openings in these functions on a day counts as 0, not as missing
    base = company_days.merge(matching, on=keys, how="left").fillna({"open_jobs": 0, "open_postings": 0})
    base = base.astype({"open_jobs": int, "open_postings": int})
    st.caption(f"Function breakdown available from {functions['snapshot_date'].min():%b %d, %Y}.")
else:
    base = openings

view = base[base["company"].isin(selected)]

# Greenhouse lists a multi-city job once per city; Ashby lists it once. Comparing postings
# across the two would undercount Ashby companies, so hide them in that view instead.
if measure == "open_postings":
    hidden = sorted(view.loc[view["source"] != "greenhouse", "company"].unique())
    if hidden:
        st.info(
            f"Hiding {', '.join(display_name(c) for c in hidden)}: postings only compare fairly within "
            "Greenhouse, which lists a multi-city job once per city. Ashby lists it once. "
            "Switch to 'Open jobs' to compare every company."
        )
    view = view[view["source"] == "greenhouse"]

if view.empty:
    st.info("Nothing to show for this selection. Pick at least one company, or switch to 'Open jobs'.")
    st.stop()
selected =[c for c in selected if c in set(view["company"])]
view = view.assign(company_name=view["company"].map(display_name))

# ---- Latest numbers, with change since the previous snapshot ----
st.subheader(f"As of {latest_date:%b %d, %Y}")
cols = st.columns(len(selected))
for col, company in zip(cols, selected):
    history = view[view["company"] == company].sort_values("snapshot_date")
    current = history[measure].iloc[-1]
    delta = current - history[measure].iloc[-2] if len(history) > 1 else None
    col.metric(
        display_name(company),
        f"{current:,}",
        delta=None if delta is None else f"{delta:+,}",
        help=f"Source: {SOURCE_NAMES.get(history['source'].iloc[-1], history['source'].iloc[-1])}",
    )

# ---- Trend chart ----
chart = (
    alt.Chart(view)
    .mark_line(point=True)   # points keep the chart readable while there are only a few days of data
    .encode(
        # Daily data: one tick per day, labeled like "Nov 20", never hours
        x=alt.X(
            "yearmonthdate(snapshot_date):T",
            title="Date",
            axis=alt.Axis(format="%b %d", tickCount="day", labelAngle=0),
        ),
        y=alt.Y(f"{measure}:Q", title=measure_label),
        color=alt.Color("company_name:N", title="Company", scale=company_colors),
        tooltip=[
            alt.Tooltip("company_name:N", title="Company"),
            alt.Tooltip("source:N", title="ATS"),
            alt.Tooltip("snapshot_date:T", title="Date", format="%b %d, %Y"),
            alt.Tooltip(f"{measure}:Q", title=measure_label, format=","),
        ],
    )
    .properties(height=400)
)
st.altair_chart(chart, width="stretch")

if openings["snapshot_date"].nunique() < 3:
    st.caption("History started recently. The trend fills in as the daily job adds snapshots.")

# ---- Posting age ----
st.subheader("Median posting age by function")
if lifecycle.empty:
    st.info("Posting age appears after the next pipeline run.")
else:
    open_now = lifecycle[lifecycle["closed_on"].isna()].copy()
    # Use the earliest publish date ever seen, so a repost that keeps the same job ID doesn't reset its age
    date_column = "earliest_published_at" if "earliest_published_at" in open_now.columns else "published_at"
    published = pd.to_datetime(open_now[date_column], utc=True, format="ISO8601", errors="coerce")
    as_of = pd.to_datetime(open_now["last_seen"]).dt.tz_localize("UTC")
    # Jobs published on the day of the snapshot would come out slightly negative; they're 0 days old
    open_now["age_days"] = (as_of - published).dt.days.clip(lower=0)
    open_now["company_name"] = open_now["company"].map(display_name)

    # Heuristic: a company with many open jobs but none older than 90 days almost certainly
    # reposts on a cycle, which resets the publish date. Its "age" is time since the last repost.
    per_company = open_now.groupby("company_name")["age_days"].agg(["size", "max"])
    reposters = set(per_company[(per_company["size"] >= 50) & (per_company["max"] < 90)].index)

    ages = open_now[open_now["company"].isin(selected_companies)]
    if chosen_functions:
        ages = ages[ages["function"].isin(chosen_functions)]

    summary = (
        ages.groupby(["function", "company_name"])
        .agg(median_age=("age_days", "median"), jobs=("job_id", "size"),
             share_over_90=("age_days", lambda s: round((s > 90).mean() * 100)), oldest=("age_days", "max"))
        .reset_index()
    )
    summary["note"] = summary["company_name"].map(
        lambda c: "Likely reposts on a cycle: age = time since last repost" if c in reposters else ""
    )
    enough = summary["jobs"] >= MIN_JOBS_FOR_MEDIAN

    if not enough.any():
        st.info(f"No function has at least {MIN_JOBS_FOR_MEDIAN} open jobs for this selection.")
    else:
        heat = summary.copy()
        heat["enough"] = enough
        # Column label: reposting companies get a marker so the caveat travels with the chart
        heat["column"] = heat["company_name"].map(lambda c: f"{c}*" if c in reposters else c)
        heat["label"] = heat.apply(lambda r: f"{r.median_age:,.0f}" if r.enough else "–", axis=1)
        # Light text on the darkest cells, dark text everywhere else, so every number stays readable
        dark_cutoff = 0.55 * heat.loc[heat["enough"], "median_age"].max()
        heat["dark_cell"] = heat["enough"] & (heat["median_age"] > dark_cutoff)

        # Rows: oldest functions first. Columns: fixed alphabetical order, never by rank.
        row_order = (ages.groupby("function")["age_days"].median().sort_values(ascending=False).index.tolist())
        column_order = [f"{c}*" if c in reposters else c
                        for c in all_company_names if c in set(heat["company_name"])]

        base_chart = alt.Chart(heat).encode(
            x=alt.X("column:N", sort=column_order, title=None,
                    axis=alt.Axis(orient="top", labelAngle=0, labelLimit=160, ticks=False, domain=False,
                                  labelFontSize=15, labelFontWeight=600, labelColor="#0b0b0b",
                                  labelPadding=10,
                                  # Center each name over its column; Vega's default label
                                  # nudging can shift the outer labels off-center
                                  labelAlign="center", labelFlush=False, labelBound=False,
                                  labelOverlap=False)),
            y=alt.Y("function:N", sort=row_order, title=None,
                    axis=alt.Axis(labelLimit=240, ticks=False, domain=False,
                                  labelFontSize=13, labelColor="#0b0b0b", labelPadding=8)),
            tooltip=[
                alt.Tooltip("company_name:N", title="Company"),
                alt.Tooltip("function:N", title="Function"),
                alt.Tooltip("median_age:Q", title="Median age (days)", format=",.0f"),
                alt.Tooltip("jobs:Q", title="Open jobs"),
                alt.Tooltip("share_over_90:Q", title="% older than 90 days"),
                alt.Tooltip("oldest:Q", title="Oldest (days)", format=","),
                alt.Tooltip("note:N", title="Note"),
            ],
        )
        cells = base_chart.mark_rect(cornerRadius=3, stroke="#ffffff", strokeWidth=2).encode(
            color=alt.condition(
                "datum.enough",
                alt.Color(
                    "median_age:Q",
                    title="Median age (days)",
                    # One hue, light to dark: darker means jobs have stayed posted longer
                    scale=alt.Scale(range=["#cde2fb", "#86b6ef", "#3987e5", "#1c5cab", "#0d366b"], zero=True),
                    legend=alt.Legend(orient="top", gradientLength=220),
                ),
                alt.value("#e6e5e1"),   # neutral gray: too few jobs for a meaningful median
            )
        )
        # Text color is set by a condition on fixed values, not a field. Layers share one color
        # scale, and a second color field here would clash with the cells' age scale (blank chart).
        numbers = base_chart.mark_text(fontSize=13).encode(
            text="label:N",
            color=alt.condition("datum.dark_cell", alt.value("#ffffff"), alt.value("#0b0b0b")),
        )
        age_chart = (cells + numbers).properties(height=max(120, len(row_order) * 34))
        st.altair_chart(age_chart, width="stretch")

        notes = ["Each cell is the median age of a company's open jobs in that function, in days. "
                 "Medians, not averages: a few always-open postings are years old and would skew an average. "
                 f"Gray cells (–) have fewer than {MIN_JOBS_FOR_MEDIAN} open jobs; blank cells have none."]
        flagged = sorted(reposters & set(heat["company_name"]))
        if flagged:
            notes.append(f"\\* {' and '.join(flagged)}: no open job is older than 90 days, which suggests jobs are "
                         "reposted on a cycle, resetting the publish date. Since Nov 20 the pipeline keeps each "
                         "job's earliest publish date, so ages will become more accurate as history builds, "
                         "as long as reposts keep the same job ID.")
        st.caption(" ".join(notes))

# ---- Pipeline health: owning what you ship after launch ----
with st.expander("Pipeline health"):
    if runs.empty:
        st.write("No runs logged yet.")
    else:
        runs = runs.rename(columns={"postings_count": "records_count"})   # older column name
        show = [c for c in ["company", "source", "started_at", "status", "http_status",
                            "records_count", "error_message"] if c in runs.columns]
        last_runs = (
            runs.sort_values("started_at")
            .groupby("company")
            .tail(1)[show]
            .sort_values("company")
        )
        failures = (last_runs["status"] != "success").sum()
        if failures:
            st.error(f"{failures} company fetch(es) failed on the last run. Earlier data for them is kept.")
        else:
            st.success("Last run succeeded for every company.")
        st.dataframe(last_runs, hide_index=True, width="stretch")

        latest = openings[openings["snapshot_date"] == latest_date]
        missing_req = int(latest.loc[latest["source"] == "greenhouse", "missing_requisition_id"].sum())
        st.write(f"Data quality, latest snapshot: {missing_req} Greenhouse job(s) have no requisition ID "
                 "(Ashby's public API doesn't expose requisition IDs).")
        if "missing_department" in latest.columns:
            st.write(f"{int(latest['missing_department'].sum())} job(s) have no department.")
        if not lifecycle.empty:
            closed = int(lifecycle["closed_on"].notna().sum())
            st.write(f"Job tracking: {len(lifecycle):,} jobs seen since {lifecycle['first_seen'].min()}; "
                     f"{closed:,} have closed so far. Time-open analysis needs a few weeks of history.")
            if "times_republished" in lifecycle.columns:
                republished = lifecycle[lifecycle["times_republished"] > 0]
                by_company = republished["company"].map(display_name).value_counts()
                detail = ", ".join(f"{name} {count}" for name, count in by_company.items())
                st.write(f"Reposts detected (same job ID, later publish date): {len(republished):,}"
                         + (f" ({detail})" if detail else "") + ".")
        if "unmapped_function" in latest.columns:
            st.write(f"{int(latest['unmapped_function'].sum())} job(s) have a department that no "
                     "function rule matches, so they count as 'Other'.")

with st.expander("About this data"):
    st.markdown(
        """
- **Sources:** each company's public job board API (no login required). Anthropic, Okta, Binance and Deliveroo use Greenhouse; OpenAI uses Ashby. Each system's data is mapped into one shared format.
- **Schedule:** a GitHub Actions job runs the pipeline once a day and commits the new snapshot.
- **Reruns are safe:** rerunning on the same day replaces that day's numbers instead of duplicating them.
- **Failures are safe:** if a company's board is unavailable, its earlier data is kept and the failure is logged above.
- **Counting:** a job posted in several locations counts once under *Open jobs*. *Open postings* is shown for Greenhouse companies only, because Ashby doesn't split jobs into one listing per city.
"""
    )

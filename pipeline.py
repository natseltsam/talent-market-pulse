"""Fetch open jobs from several applicant tracking systems (ATS) and save a daily snapshot.

Each ATS has its own API and data format. For each one, a *fetcher* pulls the raw data
and a *normalizer* maps it into one shared schema. Everything downstream (storage,
counts, the dashboard) then works the same way whichever system a company uses.

Run it with:  python pipeline.py
Running it again on the same (UTC) day replaces that day's snapshot instead of duplicating it.
"""
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

from function_map import classify_function, clean_department

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "talent_pulse.db"         # local job-level detail, not committed
DATA_DIR = BASE_DIR / "data"                    # daily summaries, committed to git
OPENINGS_CSV = DATA_DIR / "openings.csv"        # one row per company per day
FUNCTIONS_CSV = DATA_DIR / "openings_by_function.csv"   # one row per company per function per day
RUNS_CSV = DATA_DIR / "fetch_runs.csv"          # one row per company per run
LIFECYCLE_CSV = DATA_DIR / "job_lifecycle.csv"  # one row per job ever seen: when it opened and closed

LIFECYCLE_COLUMNS = ["company", "source", "job_id", "title", "function", "published_at",
                     "earliest_published_at", "times_republished",
                     "first_seen", "last_seen", "closed_on", "times_reopened"]


def parse_time(value):
    """Parse an ISO timestamp with any UTC offset; None/NaN/garbage -> None."""
    if value is None or pd.isna(value):
        return None
    try:
        timestamp = pd.Timestamp(value)
    except (ValueError, TypeError):
        return None
    return timestamp.tz_convert("UTC") if timestamp.tzinfo else timestamp.tz_localize("UTC")

# Which ATS each company uses. Adding a company is a one-line change.
COMPANIES = {
    "anthropic": "greenhouse",
    "okta": "greenhouse",
    "binance": "greenhouse",
    "deliveroo": "greenhouse",
    "openai": "ashby",
}

# The shared schema: one row per job, whichever system it came from
JOB_COLUMNS = [
    "snapshot_date", "company", "source", "job_id", "requisition_id", "title",
    "department", "function", "location", "location_count", "posting_count", "published_at",
    "salary_min", "salary_max", "salary_currency", "url",
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    snapshot_date    TEXT    NOT NULL,
    company          TEXT    NOT NULL,
    source           TEXT    NOT NULL,   -- which ATS: 'greenhouse' or 'ashby'
    job_id           TEXT    NOT NULL,   -- TEXT because Greenhouse IDs are numbers and Ashby IDs are UUIDs
    requisition_id   TEXT,               -- Greenhouse only
    title            TEXT    NOT NULL,
    department       TEXT,               -- primary department, as the company names it
    function         TEXT,               -- shared category from function_map.py
    location        TEXT,               -- primary location
    location_count   INTEGER,
    posting_count    INTEGER,            -- public listings for this job; only comparable within one ATS
    published_at     TEXT,
    salary_min       REAL,               -- Ashby only, when the company publishes it
    salary_max       REAL,
    salary_currency  TEXT,
    url              TEXT,
    PRIMARY KEY (snapshot_date, company, job_id)
);
"""


# ---------------------------------------------------------------------------
# Fetchers: one per ATS. Each returns (raw_jobs, http_status, error_message);
# raw_jobs is None if the fetch failed.
# ---------------------------------------------------------------------------

def get_json(url, params):
    try:
        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()
        return response.json(), response.status_code, None
    except requests.exceptions.HTTPError as e:
        return None, e.response.status_code, f"HTTP {e.response.status_code}"
    except requests.exceptions.RequestException as e:   # timeouts, dropped connections, bad JSON
        return None, None, type(e).__name__


def fetch_greenhouse(board_token):
    # content=true is required: without it Greenhouse omits departments
    data, status, error = get_json(
        f"https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs", {"content": "true"}
    )
    if data is not None and "jobs" not in data:
        return None, status, "Response missing 'jobs'"
    return (data or {}).get("jobs"), status, error


def fetch_ashby(board_name):
    # includeCompensation=true is required for salary ranges
    data, status, error = get_json(
        f"https://api.ashbyhq.com/posting-api/job-board/{board_name}", {"includeCompensation": "true"}
    )
    if data is not None and "jobs" not in data:
        return None, status, "Response missing 'jobs'"
    return (data or {}).get("jobs"), status, error


# ---------------------------------------------------------------------------
# Normalizers: map each ATS's format into the shared schema (one dict per job)
# ---------------------------------------------------------------------------

def normalize_greenhouse(company, postings):
    """Greenhouse returns one record per *posting*. A job posted in three cities appears
    three times with the same internal_job_id, so group the postings back into jobs."""
    postings_by_job = {}
    for posting in postings:
        job_id = str(posting.get("internal_job_id") or posting["id"])
        postings_by_job.setdefault(job_id, []).append(posting)

    jobs = []
    for job_id, group in postings_by_job.items():
        first = min(group, key=lambda p: p.get("first_published") or "")
        departments = [d["name"] for d in first.get("departments", [])]
        locations = {(p.get("location") or {}).get("name") for p in group} - {None}
        jobs.append({
            "company": company,
            "source": "greenhouse",
            "job_id": job_id,
            "requisition_id": first.get("requisition_id"),
            "title": first["title"],
            # Business rule: a job can sit in several departments; the first listed is primary
            "department": departments[0] if departments else None,
            "location": (first.get("location") or {}).get("name"),
            "location_count": len(locations),
            "posting_count": len(group),
            "published_at": first.get("first_published"),
            "salary_min": None,
            "salary_max": None,
            "salary_currency": None,
            "url": first.get("absolute_url"),
        })
    return jobs


def ashby_salary(job):
    """Return the annual base salary range, or Nones. When there are several tiers
    (e.g. pay by location), this takes the first one."""
    tiers = (job.get("compensation") or {}).get("compensationTiers") or []
    for tier in tiers:
        for part in tier.get("components") or []:
            if part.get("compensationType") == "Salary" and part.get("interval") == "1 YEAR":
                return part.get("minValue"), part.get("maxValue"), part.get("currencyCode")
    return None, None, None


def normalize_ashby(company, raw_jobs):
    """Ashby returns one record per *job*, with extra cities in secondaryLocations."""
    jobs = []
    for job in raw_jobs:
        if not job.get("isListed", True):   # skip unlisted jobs
            continue
        salary_min, salary_max, currency = ashby_salary(job)
        jobs.append({
            "company": company,
            "source": "ashby",
            "job_id": str(job["id"]),
            "requisition_id": None,
            "title": job["title"],
            "department": job.get("department"),
            "location": job.get("location"),
            "location_count": 1 + len(job.get("secondaryLocations") or []),
            "posting_count": 1,                 # Ashby has one listing per job
            "published_at": job.get("publishedAt"),
            "salary_min": salary_min,
            "salary_max": salary_max,
            "salary_currency": currency,
            "url": job.get("jobUrl"),
        })
    return jobs


SOURCES = {
    "greenhouse": (fetch_greenhouse, normalize_greenhouse),
    "ashby": (fetch_ashby, normalize_ashby),
}


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def migrate_schema(conn):
    """Add columns introduced after the table was first created. CREATE TABLE IF NOT
    EXISTS doesn't change an existing table, so new columns have to be added here."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
    if "function" not in existing:
        conn.execute("ALTER TABLE jobs ADD COLUMN function TEXT")


def save_snapshot(conn, snapshot_date, company, jobs):
    """Replace this company's rows for this date in one transaction, so reruns
    never duplicate rows and a failure partway leaves the old rows in place."""
    rows = [tuple({**job, "snapshot_date": snapshot_date}[col] for col in JOB_COLUMNS) for job in jobs]
    placeholders = ", ".join("?" for _ in JOB_COLUMNS)
    with conn:
        conn.execute("DELETE FROM jobs WHERE snapshot_date = ? AND company = ?", (snapshot_date, company))
        conn.executemany(f"INSERT INTO jobs ({', '.join(JOB_COLUMNS)}) VALUES ({placeholders})", rows)


def write_csv(df, path, int_columns):
    # Nullable integers keep counts as 3, not 3.0, when some values are blank
    df = df.astype({col: "Int64" for col in int_columns if col in df.columns})
    df.to_csv(path, index=False)


def upsert_history(path, rows, columns, int_columns):
    """Replace every row for each (snapshot_date, company) in `rows`, keep everything else.

    Companies that failed today aren't in `rows`, so their earlier history stays untouched.
    Replacing a company-day as a whole also removes rows that no longer apply, such as a
    function that had openings in this morning's run but none now.
    """
    if not rows:
        return
    new = pd.DataFrame(rows)
    if path.exists():
        old = pd.read_csv(path)
        if "source" not in old.columns:     # rows saved before multi-ATS support
            old["source"] = old["company"].map(COMPANIES)
        replaced = old.set_index(["snapshot_date", "company"]).index.isin(
            new.set_index(["snapshot_date", "company"]).index
        )
        new = pd.concat([old[~replaced], new], ignore_index=True)
    sort_by = [c for c in ["snapshot_date", "company", "function"] if c in columns]
    write_csv(new.reindex(columns=columns).sort_values(sort_by), path, int_columns)


def update_lifecycle(snapshot_date, jobs_by_company):
    """Track when each job appears and disappears, for posting age and time-open analysis.

    jobs_by_company holds only companies fetched successfully today. That matters: if a
    company's fetch failed, its jobs are missing from today's data, but they did NOT close.
    Only companies we actually heard from today can have jobs marked closed.

    closed_on is the first snapshot where a job was missing. The job really closed
    somewhere between last_seen and closed_on.

    Some companies repost jobs on a cycle, which resets the publish date. So we keep
    earliest_published_at (the oldest publish date ever seen for the job) and count
    times_republished (how often the date moved later). This only catches reposts that
    keep the same job ID; a repost under a new ID looks like a close plus a new job.
    """
    if not jobs_by_company:
        return
    records = {}
    if LIFECYCLE_CSV.exists():
        old = pd.read_csv(LIFECYCLE_CSV, dtype={"job_id": str})
        for row in old.to_dict("records"):
            # Rows written before republish tracking existed start from what we know today
            if pd.isna(row.get("earliest_published_at")):
                row["earliest_published_at"] = row["published_at"]
            if pd.isna(row.get("times_republished")):
                row["times_republished"] = 0
            records[(row["company"], row["job_id"])] = row

    seen_today = set()
    for company, jobs in jobs_by_company.items():
        for job in jobs:
            key = (company, job["job_id"])
            seen_today.add(key)
            latest = {field: job[field] for field in ["source", "title", "function", "published_at"]}
            record = records.get(key)
            if record is None:
                records[key] = {"company": company, "job_id": job["job_id"], **latest,
                                "earliest_published_at": job["published_at"], "times_republished": 0,
                                "first_seen": snapshot_date, "last_seen": snapshot_date,
                                "closed_on": None, "times_reopened": 0}
                continue

            previous, current = parse_time(record.get("published_at")), parse_time(job["published_at"])
            if previous and current and current > previous:
                record["times_republished"] = int(record.get("times_republished") or 0) + 1
            earliest = parse_time(record.get("earliest_published_at"))
            if current and (earliest is None or current < earliest):
                record["earliest_published_at"] = job["published_at"]
            closed_on = record.get("closed_on")
            if not pd.isna(closed_on):
                # Back after being marked closed. If it was only marked closed earlier today
                # (a rerun), that wasn't a real reopening, so don't count it.
                if closed_on != snapshot_date:
                    record["times_reopened"] = int(record.get("times_reopened") or 0) + 1
                record["closed_on"] = None
            record.update(latest, last_seen=snapshot_date)

    for (company, job_id), record in records.items():
        if company in jobs_by_company and (company, job_id) not in seen_today and pd.isna(record.get("closed_on")):
            record["closed_on"] = snapshot_date

    lifecycle = pd.DataFrame(list(records.values())).reindex(columns=LIFECYCLE_COLUMNS)
    write_csv(lifecycle.sort_values(["company", "first_seen", "job_id"]), LIFECYCLE_CSV,
              ["times_reopened", "times_republished"])


def append_run_log(run_records):
    new = pd.DataFrame(run_records)
    if RUNS_CSV.exists():
        # Older rows called this column postings_count; it measured the same thing (raw API records)
        old = pd.read_csv(RUNS_CSV).rename(columns={"postings_count": "records_count"})
        new = pd.concat([old, new], ignore_index=True)
    write_csv(new, RUNS_CSV, ["http_status", "records_count"])


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    now = datetime.now(timezone.utc)
    started_at = now.isoformat(timespec="seconds")
    snapshot_date = now.date().isoformat()   # UTC on purpose: same date wherever it runs

    DATA_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    migrate_schema(conn)

    summaries, function_rows, run_records = [], [], []
    jobs_by_company = {}
    for company, source in COMPANIES.items():
        fetch, normalize = SOURCES[source]
        raw, http_status, error = fetch(company)
        run = {"started_at": started_at, "snapshot_date": snapshot_date, "company": company,
               "source": source, "http_status": http_status, "records_count": None, "error_message": error}

        if raw is None:
            # Leave existing data alone. A failed fetch must never wipe good data.
            print(f"[{company}] FAILED ({error}), keeping any existing data")
            run_records.append({**run, "status": "error"})
            continue

        jobs = normalize(company, raw)
        for job in jobs:
            job["function"] = classify_function(clean_department(job["department"]))
        save_snapshot(conn, snapshot_date, company, jobs)
        jobs_by_company[company] = jobs

        by_function = pd.DataFrame(jobs).groupby("function").agg(
            open_jobs=("job_id", "size"), open_postings=("posting_count", "sum")
        )
        for function, counts in by_function.iterrows():
            function_rows.append({"snapshot_date": snapshot_date, "company": company, "source": source,
                                  "function": function, **counts.to_dict()})
        run_records.append({**run, "status": "success", "records_count": len(raw)})
        summaries.append({
            "snapshot_date": snapshot_date,
            "company": company,
            "source": source,
            "open_jobs": len(jobs),
            "open_postings": sum(j["posting_count"] for j in jobs),
            "missing_department": sum(1 for j in jobs if not j["department"]),
            "unmapped_function": sum(1 for j in jobs if j["function"] == "Other"),
            # Requisition IDs only exist in Greenhouse; blank means "not applicable", not zero
            "missing_requisition_id": (
                sum(1 for j in jobs if not j["requisition_id"]) if source == "greenhouse" else None
            ),
        })
        print(f"[{company}/{source}] {len(raw)} raw records -> {len(jobs)} jobs")

    conn.close()
    summary_columns = ["snapshot_date", "company", "source", "open_jobs", "open_postings",
                       "missing_department", "unmapped_function", "missing_requisition_id"]
    upsert_history(OPENINGS_CSV, summaries, summary_columns, summary_columns[3:])
    function_columns = ["snapshot_date", "company", "source", "function", "open_jobs", "open_postings"]
    upsert_history(FUNCTIONS_CSV, function_rows, function_columns, function_columns[4:])
    update_lifecycle(snapshot_date, jobs_by_company)
    append_run_log(run_records)
    print(f"Done. Snapshot {snapshot_date}: {len(summaries)} of {len(COMPANIES)} companies succeeded")

    # Exit non-zero only if every company failed, so the scheduled job shows as failed and GitHub emails you
    if not summaries:
        sys.exit(1)


if __name__ == "__main__":
    main()

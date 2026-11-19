"""Fetch Greenhouse job postings and save a daily snapshot to a SQLite database.

Run it with:  python pipeline.py
Running it again on the same day replaces that day's snapshot instead of duplicating it.
"""
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

DB_PATH = Path(__file__).parent / "talent_pulse.db"          # local detail data, not committed
DATA_DIR = Path(__file__).parent / "data"                     # daily summaries, committed to git
OPENINGS_CSV = DATA_DIR / "openings.csv"                      # one row per company per day
RUNS_CSV = DATA_DIR / "fetch_runs.csv"                        # one row per company per run
COMPANIES = ["anthropic", "okta", "binance", "deliveroo"]

SCHEMA = """
-- One row per company per run: a log of what happened, for debugging and monitoring
CREATE TABLE IF NOT EXISTS fetch_runs (
    run_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT    NOT NULL,
    snapshot_date   TEXT    NOT NULL,
    company         TEXT    NOT NULL,
    status          TEXT    NOT NULL,   -- 'success' or 'error'
    http_status     INTEGER,
    postings_count  INTEGER,
    error_message   TEXT
);

-- One row per posting per day. The primary key makes duplicates impossible.
CREATE TABLE IF NOT EXISTS postings (
    snapshot_date    TEXT    NOT NULL,
    company          TEXT    NOT NULL,
    posting_id       INTEGER NOT NULL,
    job_id           INTEGER,
    requisition_id   TEXT,
    title            TEXT    NOT NULL,
    location         TEXT,
    first_published  TEXT,
    updated_at       TEXT,
    url              TEXT,
    PRIMARY KEY (snapshot_date, company, posting_id)
);

-- Departments live in their own table, because one posting can have several
CREATE TABLE IF NOT EXISTS posting_departments (
    snapshot_date  TEXT    NOT NULL,
    company        TEXT    NOT NULL,
    posting_id     INTEGER NOT NULL,
    department     TEXT    NOT NULL,
    PRIMARY KEY (snapshot_date, company, posting_id, department)
);
"""


def fetch_jobs(board_token):
    """Return (jobs, http_status, error_message). jobs is None if the fetch failed."""
    url = f"https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs"
    try:
        # content=true is required: without it Greenhouse omits departments, offices, and descriptions
        response = requests.get(url, params={"content": "true"}, timeout=15)
        response.raise_for_status()
        return response.json()["jobs"], response.status_code, None
    except requests.exceptions.HTTPError as e:
        return None, e.response.status_code, f"HTTP {e.response.status_code}"
    except requests.exceptions.RequestException as e:   # timeouts, DNS failures, dropped connections
        return None, None, type(e).__name__


def save_snapshot(conn, snapshot_date, company, jobs):
    """Replace this company's rows for this date with the fresh pull.

    Deleting and then inserting inside one transaction means a rerun gives the same
    result as a single run (idempotent). It also drops postings that closed since the
    earlier run today. If anything fails partway, the transaction rolls back and the
    old rows stay.
    """
    posting_rows = []
    dept_rows = []
    for job in jobs:
        posting_rows.append((
            snapshot_date,
            company,
            job["id"],
            job.get("internal_job_id"),
            job.get("requisition_id"),
            job["title"],
            (job.get("location") or {}).get("name"),
            job.get("first_published"),
            job.get("updated_at"),
            job.get("absolute_url"),
        ))
        for dept in job.get("departments", []):
            dept_rows.append((snapshot_date, company, job["id"], dept["name"]))

    with conn:  # one transaction: all of it is saved, or none of it
        conn.execute(
            "DELETE FROM postings WHERE snapshot_date = ? AND company = ?",
            (snapshot_date, company),
        )
        conn.execute(
            "DELETE FROM posting_departments WHERE snapshot_date = ? AND company = ?",
            (snapshot_date, company),
        )
        conn.executemany(
            "INSERT INTO postings VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", posting_rows
        )
        conn.executemany(
            "INSERT OR IGNORE INTO posting_departments VALUES (?, ?, ?, ?)", dept_rows
        )


def log_run(conn, started_at, snapshot_date, company, status, http_status, count, error):
    with conn:
        conn.execute(
            """INSERT INTO fetch_runs
               (started_at, snapshot_date, company, status, http_status, postings_count, error_message)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (started_at, snapshot_date, company, status, http_status, count, error),
        )


def update_openings_history(summaries):
    """Upsert today's per-company counts into the history file.

    Rows for (snapshot_date, company) are replaced, so a rerun doesn't duplicate them.
    Companies that failed today aren't in `summaries`, so their history is left untouched.
    """
    if not summaries:
        return
    new = pd.DataFrame(summaries)
    if OPENINGS_CSV.exists():
        old = pd.read_csv(OPENINGS_CSV)
        replaced = old.set_index(["snapshot_date", "company"]).index.isin(
            new.set_index(["snapshot_date", "company"]).index
        )
        new = pd.concat([old[~replaced], new], ignore_index=True)
    new.sort_values(["snapshot_date", "company"]).to_csv(OPENINGS_CSV, index=False)


def append_run_log(run_records):
    df = pd.DataFrame(run_records)
    df.to_csv(RUNS_CSV, mode="a", header=not RUNS_CSV.exists(), index=False)


def main():
    now = datetime.now(timezone.utc)
    started_at = now.isoformat(timespec="seconds")
    snapshot_date = now.date().isoformat()   # UTC on purpose: same date wherever it runs

    DATA_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)

    summaries, run_records = [], []
    for company in COMPANIES:
        jobs, http_status, error = fetch_jobs(company)
        run = {"started_at": started_at, "snapshot_date": snapshot_date, "company": company,
               "http_status": http_status, "postings_count": None, "error_message": error}

        if jobs is None:
            # Leave existing data alone. A failed fetch must never wipe good data.
            print(f"[{company}] FAILED ({error}), keeping any existing data")
            log_run(conn, started_at, snapshot_date, company, "error", http_status, None, error)
            run_records.append({**run, "status": "error"})
            continue

        save_snapshot(conn, snapshot_date, company, jobs)
        log_run(conn, started_at, snapshot_date, company, "success", http_status, len(jobs), None)
        run_records.append({**run, "status": "success", "postings_count": len(jobs)})
        summaries.append({
            "snapshot_date": snapshot_date,
            "company": company,
            "open_postings": len(jobs),
            # One job can be posted in several locations, so distinct jobs is the truer headcount signal
            "open_jobs": len({j.get("internal_job_id") or j["id"] for j in jobs}),
            "missing_requisition_id": sum(1 for j in jobs if not j.get("requisition_id")),
        })
        print(f"[{company}] saved {len(jobs)} postings")

    conn.close()
    update_openings_history(summaries)
    append_run_log(run_records)
    print(f"Done. Snapshot {snapshot_date}: {len(summaries)} of {len(COMPANIES)} companies succeeded")

    # Exit non-zero only if every company failed, so the scheduled job shows as failed and GitHub emails you
    if not summaries:
        sys.exit(1)


if __name__ == "__main__":
    main()

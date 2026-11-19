# AI Talent Market Pulse

A Streamlit dashboard that tracks open roles at several companies over time. Data comes from their public Greenhouse job boards. A scheduled job collects a snapshot every day.

## How it works

```
Greenhouse Job Board API ──► pipeline.py ──► data/openings.csv ──► app.py (Streamlit)
   (one call per company)    (fetch, validate,  data/fetch_runs.csv
                              count, log)
        ▲
GitHub Actions runs it daily and commits the new snapshot
```

| File | Purpose |
|---|---|
| `pipeline.py` | Fetches each company's postings, saves a daily snapshot, appends a run log |
| `app.py` | Dashboard: trend chart, latest counts, pipeline health |
| `.github/workflows/daily_snapshot.yml` | Runs the pipeline every day at 13:00 UTC |
| `data/openings.csv` | One row per company per day: the history the chart reads |
| `data/fetch_runs.csv` | One row per company per run: status, HTTP code, errors |

## Design decisions

- **Reruns are safe.** Each run replaces that day's rows for each company, so rerunning never creates duplicates.
- **A failed fetch never deletes data.** If a company's board is down, its earlier data is kept, the failure is logged, and the dashboard shows it under *Pipeline health*. The scheduled job reports failure only when every company fails, so GitHub sends an alert email.
- **Jobs, not just postings.** One job can be posted in several locations. *Open jobs* counts distinct `internal_job_id`s; *Open postings* counts every listing.
- **Ask for the fields you need.** The list endpoint leaves out departments unless `content=true` is passed. An early version quietly returned empty departments for every posting, so the pipeline now checks that the fields it relies on are filled in.
- **UTC dates.** Snapshots are dated in UTC, so the date doesn't depend on where the job runs.
- **Least privilege.** The workflow token can only write repo contents.

## If this handled employee data

This project uses public data, so committing it to a public repo is fine. For a version built on Greenhouse Harvest or Workday data, I would change:

- **Storage:** a governed warehouse (e.g. BigQuery) with role-based access, not files in git.
- **Credentials:** API keys in a secrets manager, scoped to read-only and only the needed fields; rotated, never in code.
- **Data minimization:** pull only the fields the use case needs; aggregate or mask anything person-level before it reaches a dashboard; suppress small groups that could identify individuals.
- **Access and audit:** viewers sign in, see only what their role allows, and access is logged.
- **Review:** bring security, privacy and legal in at design time, before anything is built.

## Run it locally

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows; on Mac/Linux: source .venv/bin/activate
pip install -r requirements.txt
python pipeline.py              # collect today's snapshot
streamlit run app.py            # open the dashboard
```

Built with Claude Code.

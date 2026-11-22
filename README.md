# AI Talent Market Pulse

A Streamlit dashboard that tracks open roles at several companies over time. Data comes from public job boards on two applicant tracking systems (Greenhouse and Ashby). A scheduled job collects a snapshot every day.

## How it works

```
Greenhouse API ─► fetch_greenhouse ─► normalize_greenhouse ─┐
 (Anthropic, Okta, Binance, Deliveroo)                       ├─► shared schema ─► data/*.csv ─► app.py
Ashby API ──────► fetch_ashby ──────► normalize_ashby ──────┘    (one row per job)
 (OpenAI)
        ▲
GitHub Actions runs it daily and commits the new snapshot
```

| File | Purpose |
|---|---|
| `pipeline.py` | Fetches each company's jobs, maps them into one schema, saves a daily snapshot, appends a run log |
| `app.py` | Dashboard: trend chart, latest counts, pipeline health |
| `alerts.py` | Opens, updates, and closes GitHub Issues for pipeline problems (see *Automated alerts*) |
| `tests/` | Tests for the alert logic, run automatically on every push |
| `.github/workflows/daily_snapshot.yml` | Runs the pipeline every day at 13:00 UTC, then syncs alerts |
| `.github/workflows/tests.yml` | Runs the tests on every push and pull request |
| `function_map.py` | Maps each company's department names to shared functions (Engineering, People…) |
| `data/openings.csv` | One row per company per day: the history the chart reads |
| `data/openings_by_function.csv` | One row per company per function per day: powers the function filter |
| `data/job_lifecycle.csv` | One row per job ever seen: publish date, first/last seen, when it closed. Powers posting age now and time-open analysis later |
| `data/fetch_runs.csv` | One row per company per run: status, HTTP code, errors |

## Design decisions

- **Reruns are safe.** Each run replaces that day's rows for each company, so rerunning never creates duplicates.
- **A failed fetch never deletes data.** If a company's board is down, its earlier data is kept, the failure is logged, and the dashboard shows it under *Pipeline health*. The scheduled job reports failure only when every company fails, so GitHub sends an alert email.
- **One shared schema across systems.** Each ATS gets a fetcher (its API) and a normalizer (its format → the shared schema). Adding a company is one line in `COMPANIES`; adding a new ATS is one fetcher and one normalizer.
- **Jobs, not postings, for comparisons.** Greenhouse returns one record per *posting*, so a job in three cities appears three times under one `internal_job_id`. Ashby returns one record per *job*, with extra cities in `secondaryLocations`. Counting records would undercount Ashby companies, so the pipeline groups Greenhouse postings back into jobs. The dashboard shows *Open postings* for Greenhouse companies only.
- **A shared function taxonomy.** The five companies use 293 different department names (Okta adds cost-center codes like `-674`; Deliveroo includes country names). `function_map.py` cleans each name and applies ordered keyword rules where the first match wins. The order is a business rule: Security comes before Engineering, so "Security Engineering" counts as Security. About 99.6% of jobs match a rule; the rest show as "Other" and are counted under *Pipeline health* rather than hidden. Some calls are judgment calls to confirm with the people who use the dashboard. For example, "Applied AI" is mapped to Engineering, but at some companies it is closer to a customer-facing technical role.
- **An outage isn't a closure.** A job is marked closed only when its company was fetched successfully and the job was missing. If a fetch fails, that company's jobs stay open. Otherwise one API outage would look like every job closing overnight. `closed_on` is the first snapshot where the job was missing, so the real close happened between `last_seen` and `closed_on`.
- **Posting age uses medians, and flags reposting.** Some always-open postings are years old, so the dashboard shows medians and hides medians based on fewer than 5 jobs. A company with 50+ open jobs and none older than 90 days is flagged as likely reposting on a cycle (Binance and Deliveroo today). For those companies the publish date measures time since the last repost, not how long the role has been open. To correct for this over time, the lifecycle file keeps each job's `earliest_published_at` and counts `times_republished` (how often its publish date moved later), and posting age uses the earliest date. This only catches reposts that keep the same job ID. If the counter stays at 0 while the cap persists, the company is reposting under new IDs, and the next step would be matching jobs by title and location.
- **Blank isn't zero.** Requisition IDs exist only in Greenhouse, so Ashby rows leave `missing_requisition_id` blank (not applicable) instead of 0.
- **Ask for the fields you need.** Greenhouse leaves out departments unless `content=true` is passed, and Ashby leaves out pay unless `includeCompensation=true` is passed. An early version quietly returned empty departments for every posting, so the pipeline now checks that the fields it relies on are filled in.
- **UTC dates.** Snapshots are dated in UTC, so the date doesn't depend on where the job runs.
- **Least privilege.** The workflow token can only write repo contents.

## Automated alerts

After each daily run, `alerts.py` checks the data and keeps GitHub Issues (label `pipeline-alert`) in sync with what it finds:

| Check | Kind | Closes itself? |
|---|---|---|
| A company's latest fetch failed | Condition | Yes, after a successful fetch |
| Departments match no function rule | Condition | Yes, when every department is mapped |
| Open jobs fell ≥20% (and ≥10 jobs) overnight | Event | No: a person reviews and closes it |

- **No duplicates.** Each issue holds a hidden key (e.g. `<!-- alert-key: fetch-failed:okta -->`). Every run matches problems to issues by key, so the same problem updates its existing issue instead of opening another.
- **Conditions vs. events.** A condition is a state that can clear, so its issue closes itself with a comment. An event already happened, so its issue waits for a person.
- **Each issue includes a runbook**: what to check and how to fix it.
- **Only its own issues.** Issues without a key, like ones people open by hand, are never touched.
- **Least privilege.** The workflow token can write issues and repo contents only, and it expires when the run ends.
- **Safe to test.** `python alerts.py --dry-run` reads the real open issues but only prints what it would change.

## Fixing unmapped departments: AI suggests, a person approves

When the *unmapped departments* alert opens, the fix goes through a reviewed pull request:

1. **Suggest.** Claude reads the issue, looks at each department's job titles, and proposes a function with a confidence level and a reason. The suggestions become rows in `function_overrides.csv`, opened as a pull request.
2. **Check.** The Tests workflow runs on the pull request. A row with an unknown function or a duplicate fails the check, so it can't be merged by accident.
3. **Approve.** A person reviews the reasons against the job titles, edits any rows they disagree with, and merges. Merging is the approval, and git history records who approved what and when.
4. **Apply and close.** The next daily run uses the approved mappings, finds nothing unmapped, and the alert issue closes itself.

Overrides are keyed by company *and* department, because the same department name can mean different things at different companies, and they take precedence over the keyword rules. Each job records where its function came from (`override`, `rule`, or `none`), so a department a reviewer deliberately mapped to "Other" isn't flagged again.

The suggestion step currently runs through Claude Code on request, which avoids API costs. In production it would call the Claude API on a schedule; the review and approval steps would stay the same.

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

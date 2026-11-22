"""Turn pipeline problems into GitHub Issues, and close them when the problem clears.

After each pipeline run, the checks look at the data and return the problems that exist
right now. sync_issues() then makes GitHub match:
  - a problem with no open issue           -> open one
  - a problem whose details changed        -> update the existing issue (never a duplicate)
  - an issue whose problem is gone         -> close it with a note (conditions only)

Each issue carries a hidden key, e.g. <!-- alert-key: fetch-failed:okta -->, so a problem
is matched to its issue even if the title wording changes.

Two kinds of alerts:
  - Conditions (a failing fetch, unmapped departments) close themselves once fixed.
  - Events (a sudden drop in open jobs) stay open until a person closes them, because
    something that happened yesterday still deserves a look even if today is normal.

Run:  python alerts.py --dry-run   print what would happen; changes nothing, needs no token
      python alerts.py             apply changes (needs GITHUB_TOKEN; GitHub Actions provides it)
"""
import argparse
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = BASE_DIR / "talent_pulse.db"
DEFAULT_REPO = "natseltsam/talent-market-pulse"

LABEL = "pipeline-alert"
DROP_SHARE = 0.20      # flag a drop of 20% or more in open jobs overnight...
DROP_MIN_JOBS = 10     # ...but only if at least 10 jobs, so tiny boards don't cause noise

KEY_PATTERN = re.compile(r"<!-- alert-key: (.+?) -->")
AUTO_RESOLVE_PATTERN = re.compile(r"<!-- auto-resolve: (true|false) -->")
TIMESTAMP_PATTERN = re.compile(r"^_Last checked: .*_$", re.MULTILINE)


@dataclass
class Alert:
    key: str            # stable identity: the same problem always gets the same key
    title: str
    details: str        # markdown: what's happening
    runbook: str        # markdown: what to do about it
    auto_resolve: bool  # True = close the issue when the problem disappears


# ---------------------------------------------------------------------------
# Checks: pure functions from data to alerts, so they're easy to test
# ---------------------------------------------------------------------------

def check_fetch_failures(runs):
    """A company whose most recent fetch failed. An old failure followed by a success is fine."""
    if runs.empty:
        return []
    latest = runs.sort_values("started_at").groupby("company").tail(1)
    alerts = []
    for run in latest[latest["status"] != "success"].itertuples():
        error = run.error_message if isinstance(run.error_message, str) else "unknown error"
        source = getattr(run, "source", "unknown")
        alerts.append(Alert(
            key=f"fetch-failed:{run.company}",
            title=f"Fetch failing: {run.company}",
            details=(f"The most recent fetch of **{run.company}** ({source}) failed at {run.started_at} "
                     f"with `{error}`.\n\nIts earlier data is kept, and its jobs are not marked closed."),
            runbook=("1. Open the company's job board URL in a browser. Does it still exist?\n"
                     "2. A 404 usually means the company changed its board name or left this ATS: "
                     "update `COMPANIES` in `pipeline.py`.\n"
                     "3. A timeout or 5xx is usually temporary: wait for the next run.\n"
                     "4. This issue closes automatically after a successful fetch."),
            auto_resolve=True,
        ))
    return alerts


def check_job_drops(openings):
    """A company whose open jobs fell sharply between the two most recent snapshots."""
    if openings.empty:
        return []
    dates = sorted(openings["snapshot_date"].unique())
    if len(dates) < 2:
        return []
    previous_date, latest_date = dates[-2], dates[-1]
    counts = openings.pivot_table(index="company", columns="snapshot_date", values="open_jobs")
    alerts = []
    for company, row in counts.iterrows():
        before, after = row.get(previous_date), row.get(latest_date)
        if pd.isna(before) or pd.isna(after):
            continue
        drop = before - after
        if drop >= DROP_MIN_JOBS and drop >= before * DROP_SHARE:
            alerts.append(Alert(
                # The date is part of the key: each drop is its own event
                key=f"job-drop:{company}:{latest_date}",
                title=f"Open jobs fell {drop / before:.0%} at {company} ({latest_date})",
                details=(f"**{company}** went from {int(before):,} open jobs on {previous_date} "
                         f"to {int(after):,} on {latest_date} (-{int(drop):,})."),
                runbook=("1. Check the company's careers page. Is the drop real (a hiring freeze, "
                         "a batch of closings) or a data problem?\n"
                         "2. If it's a data problem (e.g. the API now returns fewer fields or pages), "
                         "fix the fetcher and rerun.\n"
                         "3. If it's real, note what happened in a comment.\n"
                         "4. Close this issue once reviewed. It does not close itself."),
                auto_resolve=False,
            ))
    return alerts


def check_unmapped_departments(unmapped):
    """Departments that no rule in function_map.py matches, so their jobs count as 'Other'."""
    if unmapped.empty:
        return []
    lines = "\n".join(f"| {r.company} | {r.department} | {r.jobs} |" for r in unmapped.itertuples())
    return [Alert(
        key="unmapped-departments",
        title=f"{len(unmapped)} department(s) not mapped to a function",
        details=("These departments match no rule in `function_map.py`, so their jobs show as "
                 f"'Other' on the dashboard:\n\n| Company | Department | Open jobs |\n|---|---|---|\n{lines}"),
        runbook=("1. Ask Claude to suggest a function for each department, with reasons, as a pull request "
                 "adding rows to `function_overrides.csv`.\n"
                 "2. Review the pull request: check the reasons against the job titles, edit any rows you "
                 "disagree with, and make sure the Tests check passes.\n"
                 "3. Merge it. That's the approval. The next daily run uses the new mappings.\n"
                 "4. This issue updates as the list changes and closes itself when the list is empty.\n\n"
                 "For a pattern that will recur across many departments, a keyword rule in "
                 "`function_map.py` may fit better than one-off overrides."),
        auto_resolve=True,
    )]


def load_unmapped_departments():
    """Departments with no override and no matching rule in the latest snapshot.

    A department a reviewer deliberately mapped to 'Other' has source 'override', so it
    isn't reported again.
    """
    if not DB_PATH.exists():
        return pd.DataFrame()
    with sqlite3.connect(DB_PATH) as conn:
        return pd.read_sql(
            """SELECT company, department, COUNT(*) AS jobs
               FROM jobs
               WHERE function_source = 'none' AND department IS NOT NULL
                 AND snapshot_date = (SELECT MAX(snapshot_date) FROM jobs)
               GROUP BY company, department
               ORDER BY company, jobs DESC, department""",
            conn,
        )


def collect_alerts():
    def read(name):
        path = DATA_DIR / name
        return pd.read_csv(path) if path.exists() else pd.DataFrame()
    return (check_fetch_failures(read("fetch_runs.csv"))
            + check_job_drops(read("openings.csv"))
            + check_unmapped_departments(load_unmapped_departments()))


# ---------------------------------------------------------------------------
# GitHub Issues clients: a real one, and a dry-run one that only prints
# ---------------------------------------------------------------------------

class GitHubIssues:
    def __init__(self, repo, token=None):
        self.base = f"https://api.github.com/repos/{repo}"
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/vnd.github+json",
                                     "X-GitHub-Api-Version": "2022-11-28"})
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"

    def _request(self, method, path, **kwargs):
        response = self.session.request(method, self.base + path, timeout=30, **kwargs)
        response.raise_for_status()
        return response.json() if response.content else None

    def open_alert_issues(self):
        issues, page = [], 1
        while True:
            batch = self._request("GET", "/issues", params={
                "state": "open", "labels": LABEL, "per_page": 100, "page": page})
            # This endpoint also returns pull requests; skip them
            issues += [i for i in batch if "pull_request" not in i]
            if len(batch) < 100:
                return issues
            page += 1

    def ensure_label(self):
        try:
            self._request("GET", f"/labels/{LABEL}")
        except requests.exceptions.HTTPError as e:
            if e.response.status_code != 404:
                raise
            self._request("POST", "/labels", json={
                "name": LABEL, "color": "d93f0b", "description": "Opened automatically by the daily pipeline"})

    def create(self, title, body):
        return self._request("POST", "/issues", json={"title": title, "body": body, "labels": [LABEL]})["number"]

    def update(self, number, title, body):
        self._request("PATCH", f"/issues/{number}", json={"title": title, "body": body})

    def comment(self, number, body):
        self._request("POST", f"/issues/{number}/comments", json={"body": body})

    def close(self, number):
        self._request("PATCH", f"/issues/{number}", json={"state": "closed", "state_reason": "completed"})


class DryRunIssues(GitHubIssues):
    """Reads real open issues (the repo is public, so no token is needed) but only prints writes."""

    def ensure_label(self):
        print(f"  [dry run] would make sure the '{LABEL}' label exists")

    def create(self, title, body):
        print(f"  [dry run] would OPEN: {title}")
        return 0

    def update(self, number, title, body):
        print(f"  [dry run] would UPDATE #{number}: {title}")

    def comment(self, number, body):
        print(f"  [dry run] would COMMENT on #{number}: {body.splitlines()[0]}")

    def close(self, number):
        print(f"  [dry run] would CLOSE #{number}")


# ---------------------------------------------------------------------------
# Sync: make GitHub's open issues match the current alerts
# ---------------------------------------------------------------------------

def render_body(alert, checked_at):
    closing = ("It will close itself once the problem clears." if alert.auto_resolve
               else "Close it once you've reviewed it.")
    return (f"<!-- alert-key: {alert.key} -->\n"
            f"<!-- auto-resolve: {str(alert.auto_resolve).lower()} -->\n"
            f"**Opened automatically by the daily pipeline.** {closing}\n\n"
            f"### What's happening\n{alert.details}\n\n"
            f"### What to do\n{alert.runbook}\n\n"
            f"_Last checked: {checked_at}_")


def without_timestamp(body):
    """Compare bodies ignoring the 'last checked' line, so unchanged issues aren't rewritten daily."""
    return TIMESTAMP_PATTERN.sub("", body or "").strip()


def sync_issues(client, alerts, checked_at):
    """Returns a list of (action, key, issue_number) for logging and tests."""
    client.ensure_label()
    open_by_key = {}
    for issue in client.open_alert_issues():
        match = KEY_PATTERN.search(issue.get("body") or "")
        if match:
            open_by_key[match.group(1)] = issue

    actions = []
    active = {alert.key: alert for alert in alerts}
    for key, alert in active.items():
        body = render_body(alert, checked_at)
        issue = open_by_key.get(key)
        if issue is None:
            actions.append(("opened", key, client.create(alert.title, body)))
        elif without_timestamp(issue["body"]) != without_timestamp(body) or issue["title"] != alert.title:
            client.update(issue["number"], alert.title, body)
            actions.append(("updated", key, issue["number"]))
        else:
            actions.append(("unchanged", key, issue["number"]))

    for key, issue in open_by_key.items():
        if key in active:
            continue
        auto = AUTO_RESOLVE_PATTERN.search(issue.get("body") or "")
        if auto and auto.group(1) == "true":
            client.comment(issue["number"], f"Resolved: the problem was no longer present on the check at "
                                            f"{checked_at}. Closing automatically.")
            client.close(issue["number"])
            actions.append(("closed", key, issue["number"]))
        else:
            actions.append(("left open for review", key, issue["number"]))
    return actions


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="print planned changes without making them")
    args = parser.parse_args()

    repo = os.environ.get("GITHUB_REPOSITORY", DEFAULT_REPO)
    token = os.environ.get("GITHUB_TOKEN")
    if not args.dry_run and not token:
        print("No GITHUB_TOKEN set, so running as a dry run. (In GitHub Actions the token is provided.)")
        args.dry_run = True

    alerts = collect_alerts()
    print(f"{len(alerts)} active alert(s): {', '.join(a.key for a in alerts) or 'none'}")
    client = DryRunIssues(repo) if args.dry_run else GitHubIssues(repo, token)
    checked_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    for action, key, number in sync_issues(client, alerts, checked_at):
        print(f"  {action}: {key}" + (f" (#{number})" if number else ""))


if __name__ == "__main__":
    main()

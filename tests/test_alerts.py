"""Tests for alerts.py. Run from the project folder with:  python -m unittest discover tests

A fake GitHub keeps issues in memory, so these tests never touch the real repo.
"""
import sys
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import alerts  # noqa: E402


class FakeGitHub:
    """In-memory stand-in for GitHubIssues with the same methods."""

    def __init__(self):
        self.issues = {}        # number -> {"number", "title", "body", "state", "comments"}
        self.next_number = 1

    def ensure_label(self):
        pass

    def open_alert_issues(self):
        return [dict(i) for i in self.issues.values() if i["state"] == "open"]

    def create(self, title, body):
        number = self.next_number
        self.next_number += 1
        self.issues[number] = {"number": number, "title": title, "body": body, "state": "open", "comments": []}
        return number

    def update(self, number, title, body):
        self.issues[number].update(title=title, body=body)

    def comment(self, number, body):
        self.issues[number]["comments"].append(body)

    def close(self, number):
        self.issues[number]["state"] = "closed"

    def open_count(self):
        return sum(1 for i in self.issues.values() if i["state"] == "open")


def condition(key="fetch-failed:okta", details="HTTP 500"):
    return alerts.Alert(key=key, title=f"Problem {key}", details=details, runbook="Fix it.", auto_resolve=True)


def event(key="job-drop:okta:2026-11-21"):
    return alerts.Alert(key=key, title="Drop", details="Fell 30%", runbook="Look.", auto_resolve=False)


class SyncTests(unittest.TestCase):
    def test_opens_one_issue_per_new_problem(self):
        gh = FakeGitHub()
        alerts.sync_issues(gh, [condition("a"), condition("b")], "t1")
        self.assertEqual(gh.open_count(), 2)

    def test_rerun_never_creates_duplicates(self):
        gh = FakeGitHub()
        alerts.sync_issues(gh, [condition()], "t1")
        actions = alerts.sync_issues(gh, [condition()], "t2")   # same problem, later run
        self.assertEqual(gh.open_count(), 1)
        self.assertEqual(actions[0][0], "unchanged")            # timestamp alone doesn't count as a change

    def test_changed_details_update_the_same_issue(self):
        gh = FakeGitHub()
        alerts.sync_issues(gh, [condition(details="HTTP 500")], "t1")
        actions = alerts.sync_issues(gh, [condition(details="HTTP 404")], "t2")
        self.assertEqual(actions[0][0], "updated")
        self.assertEqual(gh.open_count(), 1)
        self.assertIn("HTTP 404", gh.issues[1]["body"])

    def test_condition_closes_itself_with_a_comment(self):
        gh = FakeGitHub()
        alerts.sync_issues(gh, [condition()], "t1")
        alerts.sync_issues(gh, [], "t2")                         # problem gone
        self.assertEqual(gh.issues[1]["state"], "closed")
        self.assertIn("Resolved", gh.issues[1]["comments"][0])

    def test_event_stays_open_for_a_person(self):
        gh = FakeGitHub()
        alerts.sync_issues(gh, [event()], "t1")
        actions = alerts.sync_issues(gh, [], "t2")               # the drop is no longer "current"
        self.assertEqual(gh.issues[1]["state"], "open")
        self.assertEqual(actions[0][0], "left open for review")

    def test_recurring_problem_after_close_opens_a_new_issue(self):
        gh = FakeGitHub()
        alerts.sync_issues(gh, [condition()], "t1")
        alerts.sync_issues(gh, [], "t2")
        alerts.sync_issues(gh, [condition()], "t3")
        self.assertEqual(len(gh.issues), 2)
        self.assertEqual(gh.open_count(), 1)

    def test_ignores_issues_without_a_key(self):
        gh = FakeGitHub()
        gh.create("Someone's manual note", "No hidden key here")
        alerts.sync_issues(gh, [], "t1")
        self.assertEqual(gh.issues[1]["state"], "open")           # never touch issues we didn't create


class CheckTests(unittest.TestCase):
    def test_only_latest_fetch_counts(self):
        runs = pd.DataFrame([
            {"started_at": "2026-11-20T13:00", "company": "okta", "status": "error", "error_message": "HTTP 500", "source": "greenhouse"},
            {"started_at": "2026-11-21T13:00", "company": "okta", "status": "success", "error_message": None, "source": "greenhouse"},
            {"started_at": "2026-11-21T13:00", "company": "binance", "status": "error", "error_message": "Timeout", "source": "greenhouse"},
        ])
        keys = [a.key for a in alerts.check_fetch_failures(runs)]
        self.assertEqual(keys, ["fetch-failed:binance"])

    def test_drop_thresholds(self):
        openings = pd.DataFrame([
            {"snapshot_date": "2026-11-20", "company": "big_drop", "open_jobs": 100},
            {"snapshot_date": "2026-11-21", "company": "big_drop", "open_jobs": 70},     # -30, -30%: alert
            {"snapshot_date": "2026-11-20", "company": "small_board", "open_jobs": 20},
            {"snapshot_date": "2026-11-21", "company": "small_board", "open_jobs": 12},  # -8 jobs: too few
            {"snapshot_date": "2026-11-20", "company": "big_board", "open_jobs": 800},
            {"snapshot_date": "2026-11-21", "company": "big_board", "open_jobs": 760},   # -5%: too small a share
        ])
        keys = [a.key for a in alerts.check_job_drops(openings)]
        self.assertEqual(keys, ["job-drop:big_drop:2026-11-21"])

    def test_no_drop_alert_with_one_day_of_data(self):
        openings = pd.DataFrame([{"snapshot_date": "2026-11-20", "company": "x", "open_jobs": 100}])
        self.assertEqual(alerts.check_job_drops(openings), [])


if __name__ == "__main__":
    unittest.main()

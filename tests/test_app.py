"""Testy bez sieci: kolektor z udawanym klientem GitHub oraz metryki.
Uruchomienie: python -m unittest discover -s tests"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.collector import Collector, is_excluded, select_team_repos  # noqa: E402
from app.config import DEFAULT_EXCLUDES, Config  # noqa: E402
from app.db import Database, to_iso  # noqa: E402
from app.metrics import compute_dashboard  # noqa: E402

START = datetime(2026, 10, 9, 8, 0, tzinfo=timezone.utc)
NOW = START + timedelta(hours=5)


def make_cfg(tmp: Path, **kw) -> Config:
    base = dict(
        github_token="x", github_org="org", repo_prefix="", repo_topic="", poll_interval=600,
        hackathon_start=START, hackathon_end=START + timedelta(hours=48), db_path=tmp / "t.db",
        teams_file=tmp / "teams.json", port=0, timezone="Europe/Warsaw", file_level_stats=True,
        outlier_lines=5000, exclude_patterns=list(DEFAULT_EXCLUDES),
    )
    base.update(kw)
    return Config(**base)


def commit(oid, minutes, add=10, dele=2, parents=1, email="a@x.pl"):
    return {"oid": oid, "committedDate": to_iso(START + timedelta(minutes=minutes)),
            "additions": add, "deletions": dele, "changedFilesIfAvailable": 1,
            "parents": {"totalCount": parents}, "author": {"name": "A", "email": email, "user": None}}


class FakeGitHub:
    rate_remaining = 4999

    def __init__(self):
        self.repos = [
            {"name": "team-a", "archived": False, "default_branch": "main", "topics": ["hackathon"]},
            {"name": "team-b", "archived": False, "default_branch": "main", "topics": []},
            {"name": "old", "archived": True, "default_branch": "main", "topics": []},
        ]
        self.branches = {"team-a": [("main", "h1"), ("feature", "h2")], "team-b": [("main", "h3")]}
        self.history = {
            ("team-a", "main"): [commit("c1", 10), commit("c2", 70, add=20), commit("m1", 80, parents=2)],
            ("team-a", "feature"): [commit("c2", 70, add=20), commit("c3", 90, add=30, email="b@x.pl")],
            ("team-b", "main"): [commit("d1", 30, add=8000)],
        }
        self.files = {
            "c1": [{"filename": "src/app.py", "additions": 10, "deletions": 2}],
            "c2": [{"filename": "src/x.py", "additions": 5, "deletions": 0},
                   {"filename": "package-lock.json", "additions": 15, "deletions": 2}],
            "c3": [{"filename": "src/y.py", "additions": 30, "deletions": 2}],
            "d1": [{"filename": "src/big.py", "additions": 8000, "deletions": 2}],
        }
        self.calls = {"history": 0}

    def list_org_repos(self, org):
        return self.repos

    def throttle_if_needed(self):
        pass

    def list_branches(self, owner, repo):
        return self.branches[repo]

    def branch_history(self, owner, repo, branch, since):
        self.calls["history"] += 1
        return self.history[(repo, branch)]

    def commit_files(self, owner, repo, sha):
        return self.files[sha]

    def pull_requests(self, owner, repo, since):
        if repo != "team-a":
            return []
        return [{"number": 1, "title": "Login", "state": "MERGED", "createdAt": to_iso(START + timedelta(minutes=60)),
                 "mergedAt": to_iso(START + timedelta(minutes=100)), "closedAt": to_iso(START + timedelta(minutes=100)),
                 "updatedAt": to_iso(START + timedelta(minutes=100)), "additions": 25, "deletions": 0},
                {"number": 2, "title": "UI", "state": "OPEN", "createdAt": to_iso(START + timedelta(minutes=200)),
                 "mergedAt": None, "closedAt": None, "updatedAt": to_iso(START + timedelta(minutes=200)),
                 "additions": 30, "deletions": 0}]


class CollectorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = make_cfg(self.tmp)
        self.db = Database(self.cfg.db_path)
        self.gh = FakeGitHub()
        self.collector = Collector(self.cfg, self.db, self.gh)

    def test_exclusions(self):
        self.assertTrue(is_excluded("frontend/package-lock.json", DEFAULT_EXCLUDES))
        self.assertTrue(is_excluded("web/node_modules/react/index.js", DEFAULT_EXCLUDES))
        self.assertTrue(is_excluded("assets/logo.png", DEFAULT_EXCLUDES))
        self.assertFalse(is_excluded("src/main.py", DEFAULT_EXCLUDES))

    def test_repo_filter(self):
        self.assertEqual([r["name"] for r in select_team_repos(self.gh.repos, self.cfg)], ["team-a", "team-b"])
        cfg = make_cfg(self.tmp, repo_topic="hackathon")
        self.assertEqual([r["name"] for r in select_team_repos(self.gh.repos, cfg)], ["team-a"])

    def test_sync_and_idempotency(self):
        self.collector.sync_once()
        rows = {r["sha"]: r for r in self.db.fetch_all("SELECT * FROM commits")}
        self.assertEqual(set(rows), {"c1", "c2", "c3", "m1", "d1"})
        self.assertEqual(rows["c2"]["additions"], 5)          # lockfile pominięty
        self.assertEqual(rows["c2"]["on_default"], 1)
        self.assertEqual(rows["c3"]["on_default"], 0)
        self.assertEqual(rows["m1"]["is_merge"], 1)
        self.assertEqual(rows["d1"]["is_outlier"], 1)
        self.assertNotIn("@", rows["c1"]["author_hash"])

        calls = self.gh.calls["history"]
        self.collector.sync_once()  # te same gałęzie -> brak ponownego pobierania historii
        self.assertEqual(self.gh.calls["history"], calls)
        self.assertEqual(len(self.db.fetch_all("SELECT * FROM commits")), 5)
        self.assertEqual(len(self.db.fetch_all("SELECT * FROM pull_requests")), 2)

    def test_metrics(self):
        self.collector.sync_once()
        (self.tmp / "teams.json").write_text(json.dumps({"team-a": "Alfa", "team-b": {"name": "Beta"}}))
        d = compute_dashboard(self.cfg, self.db, now=NOW)
        a = next(t for t in d["teams"] if t["repo"] == "team-a")
        b = next(t for t in d["teams"] if t["repo"] == "team-b")
        self.assertEqual(a["name"], "Alfa")
        self.assertEqual(b["name"], "Beta")
        self.assertEqual(a["commits"], 3)                    # c1, c2, c3 (bez merge)
        self.assertEqual(a["loc_added"], 15)                 # c1 + c2 z main, bez lockfile
        self.assertEqual(a["prs_merged"], 1)
        self.assertEqual(a["prs_open"], 1)
        self.assertEqual(a["median_merge_min"], 40)
        self.assertEqual(b["loc_added"], 0)                  # commit odstający nie liczy się do linii
        self.assertEqual(a["rank"], 1)
        self.assertEqual(d["totals"]["commits"], 4)
        self.assertEqual(d["series"]["commits"]["team-a"][-1], 3)
        texts = [e["text"] for e in d["events"]]
        self.assertIn("Alfa: pierwszy pull request!", texts)
        self.assertIn("Alfa scala PR #1", texts)

    def test_hidden_team(self):
        self.collector.sync_once()
        (self.tmp / "teams.json").write_text(json.dumps({"team-b": {"hidden": True}}))
        d = compute_dashboard(self.cfg, self.db, now=NOW)
        self.assertEqual([t["repo"] for t in d["teams"]], ["team-a"])


if __name__ == "__main__":
    unittest.main()

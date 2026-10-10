"""Tryb demo: generuje realistyczne dane hackatonu (25 zespołów), żeby przetestować
dashboard bez tokenu GitHub. Dane trafiają do tej samej bazy przez te same funkcje
co prawdziwy kolektor, a w tle co chwilę dochodzą nowe commity i PR-y."""
from __future__ import annotations

import hashlib
import logging
import math
import random
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .config import Config
from .db import Database, to_iso

log = logging.getLogger("demo")

DEMO_TEAMS = [
    "Nocne Marki", "Kod Bez Kawy", "Bajtowe Bobry", "Lambda Lwy", "Pierogi Stack",
    "Null Pointers", "Zielony Build", "Deploy w Piątek", "Rakieta Rust", "Tensor Tygrysy",
    "Python Pirates", "Cloud Kowboje", "Git Gurus", "Async Orły", "Merge Konflikt",
    "Kubernetes Krasnale", "Bug Busters", "API Alchemicy", "Sprint Sowy", "Docker Delfiny",
    "Hash Husaria", "Firestore Feniks", "Commit Komandosi", "Refaktor Rysie", "Pixel Pumy",
]

STEP = timedelta(minutes=10)


def demo_repo_names() -> dict[str, str]:
    return {f"team-{i + 1:02d}": name for i, name in enumerate(DEMO_TEAMS)}


class DemoGenerator:
    def __init__(self, cfg: Config, db: Database, seed: int = 42):
        self.cfg, self.db = cfg, db
        self.rng = random.Random(seed)
        self.tz = ZoneInfo(cfg.timezone)
        self.teams = []
        for repo in demo_repo_names():
            self.teams.append({
                "repo": repo,
                "rate": self.rng.uniform(0.4, 2.2),          # średnio commitów na 10 min
                "warmup": self.rng.uniform(0.3, 3.0),         # godziny do rozkręcenia
                "night_owl": self.rng.random() < 0.35,        # pracuje w nocy
                "loc_scale": self.rng.uniform(15, 70),
                "authors": [hashlib.sha256(f"{repo}-{a}".encode()).hexdigest()[:16]
                            for a in range(self.rng.randint(3, 5))],
                "pr_no": 0,
                "open_prs": [],
                "since_pr": 0,
            })
        self.cursor = cfg.hackathon_start

    def _intensity(self, team: dict, t: datetime) -> float:
        hours_in = (t - self.cfg.hackathon_start).total_seconds() / 3600
        warm = min(1.0, hours_in / team["warmup"]) if team["warmup"] > 0 else 1.0
        local_hour = t.astimezone(self.tz).hour
        if 1 <= local_hour < 7:
            night = 0.7 if team["night_owl"] else 0.12
        else:
            night = 1.0
        # przyspieszenie przed końcem hackatonu
        hours_left = (self.cfg.hackathon_end - t).total_seconds() / 3600
        finale = 1.0 + max(0.0, (6 - hours_left) / 6) * 1.5
        wave = 1 + 0.35 * math.sin(hours_in / 2.7 + hash(team["repo"]) % 7)
        return team["rate"] * warm * night * finale * wave

    def _poisson(self, lam: float) -> int:
        l, k, p = math.exp(-lam), 0, 1.0
        while True:
            p *= self.rng.random()
            if p <= l:
                return k
            k += 1

    def generate_until(self, until: datetime, step: timedelta = STEP) -> int:
        created = 0
        scale = step / STEP
        while self.cursor + step <= until:
            t0 = self.cursor
            for team in self.teams:
                n = self._poisson(self._intensity(team, t0) * scale)
                for _ in range(n):
                    created += 1
                    t = t0 + timedelta(seconds=self.rng.uniform(0, step.total_seconds()))
                    add = int(self.rng.lognormvariate(math.log(team["loc_scale"]), 0.9))
                    dele = int(add * self.rng.uniform(0.05, 0.6))
                    if self.rng.random() < 0.004:  # rzadki "wklejony" duży commit
                        add *= 120
                    sha = hashlib.sha1(f"{team['repo']}-{t.isoformat()}-{created}".encode()).hexdigest()
                    self.db.upsert_commit({
                        "repo": team["repo"], "sha": sha, "committed_at": to_iso(t), "first_seen_at": to_iso(t),
                        "author_hash": self.rng.choice(team["authors"]),
                        "additions": add, "deletions": dele, "raw_additions": add, "raw_deletions": dele,
                        "changed_files": self.rng.randint(1, 8), "is_merge": 0,
                        "on_default": int(self.rng.random() < 0.75),
                        "is_outlier": int(add + dele > self.cfg.outlier_lines), "stats_source": "demo",
                    })
                    team["since_pr"] += 1
                    if team["since_pr"] >= self.rng.randint(4, 10):
                        team["since_pr"] = 0
                        team["pr_no"] += 1
                        team["open_prs"].append((team["pr_no"], t))
                        self._save_pr(team["repo"], team["pr_no"], t, None)
                # scalanie otwartych PR po 20–150 min
                still_open = []
                for no, created_at in team["open_prs"]:
                    if t0 - created_at > timedelta(minutes=self.rng.randint(20, 150)):
                        merged = t0 + self.rng.uniform(0, 1) * step
                        self._save_pr(team["repo"], no, created_at, merged)
                    else:
                        still_open.append((no, created_at))
                team["open_prs"] = still_open
            self.cursor += step
        return created

    def _save_pr(self, repo: str, number: int, created: datetime, merged: datetime | None) -> None:
        self.db.upsert_pr({
            "repo": repo, "number": number, "title": f"Funkcja #{number}",
            "state": "MERGED" if merged else "OPEN", "created_at": to_iso(created),
            "merged_at": to_iso(merged) if merged else None, "closed_at": to_iso(merged) if merged else None,
            "updated_at": to_iso(merged or created), "additions": 0, "deletions": 0,
        })

    def seed(self) -> None:
        for repo in demo_repo_names():
            self.db.upsert_team(repo, "main", False)
        n = self.generate_until(datetime.now(timezone.utc))
        self.db.finish_run(self.db.start_run(), True, f"demo: {n} commitów", None)
        log.info("Demo: wygenerowano %d commitów historycznych", n)


def run_demo(cfg: Config, db: Database, stop: threading.Event, live_every: int = 20) -> None:
    gen = DemoGenerator(cfg, db)
    gen.seed()
    step = timedelta(seconds=live_every)
    while not stop.wait(live_every):
        # dane "na żywo": dogenerowanie aktywności za czas, który właśnie minął
        n = gen.generate_until(datetime.now(timezone.utc), step)
        db.finish_run(db.start_run(), True, f"demo: +{n} commitów", None)

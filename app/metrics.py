"""Agregaty dla dashboardu. Lokalnie liczone w Pythonie z SQLite; w GCP te same
definicje staną się zapytaniami SQL w BigQuery, a wynik trafi do Firestore.

Definicje:
- commity: commity bez merge, ze wszystkich gałęzi, bez duplikatów po SHA,
- linie kodu: commity bez merge z domyślnej gałęzi, bez plików generowanych
  i bez commitów odstających (powyżej OUTLIER_LINES),
- PR: otwarte, scalone, zamknięte bez scalenia.
"""
from __future__ import annotations

import json
import statistics
from bisect import bisect_right
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import Config
from .db import Database, parse_iso, to_iso

COMMIT_MILESTONES = [50, 100, 250, 500, 1000]
LOC_MILESTONES = [1000, 5000, 10000, 25000, 50000, 100000]

PL_DAYS = ["pon", "wt", "śr", "czw", "pt", "sob", "nd"]


def _label(dt: datetime, tz: ZoneInfo, with_minutes: bool = True) -> str:
    local = dt.astimezone(tz)
    return f"{PL_DAYS[local.weekday()]} {local.strftime('%H:%M' if with_minutes else '%H:00')}"


PALETTE = [
    "#4cc9f0", "#f72585", "#7bd389", "#ffb703", "#b388ff", "#ff6b6b", "#2ec4b6", "#f4a261",
    "#90be6d", "#e76f51", "#56cfe1", "#c77dff", "#ffd166", "#06d6a0", "#ef476f", "#a3c4f3",
    "#fb8500", "#8ecae6", "#e9c46a", "#80ffdb", "#ff99c8", "#caffbf", "#9bf6ff", "#ffc6ff", "#fdffb6",
]


def load_team_overrides(path: Path) -> dict[str, dict]:
    """teams.json: {"nazwa-repo": "Nazwa biznesowa"} albo {"nazwa-repo": {"name": ..., "hidden": true, "color": ...}}"""
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    out = {}
    for repo, val in raw.items():
        out[repo] = {"name": val} if isinstance(val, str) else dict(val)
    return out


def _bucket_size(span: timedelta) -> timedelta:
    if span <= timedelta(hours=6):
        return timedelta(minutes=10)
    if span <= timedelta(hours=24):
        return timedelta(minutes=20)
    return timedelta(minutes=30)


def compute_dashboard(cfg: Config, db: Database, extra_names: dict[str, str] | None = None,
                      now: datetime | None = None) -> dict:
    real_now = now or datetime.now(timezone.utc)
    start, end = cfg.hackathon_start, cfg.hackathon_end
    now = min(real_now, end)
    tz = ZoneInfo(cfg.timezone)

    overrides = load_team_overrides(cfg.teams_file)
    teams_rows = db.fetch_all("SELECT repo FROM teams WHERE archived=0 ORDER BY repo")
    teams = []
    for i, row in enumerate(teams_rows):
        repo = row["repo"]
        ov = overrides.get(repo, {})
        if ov.get("hidden"):
            continue
        teams.append({
            "repo": repo,
            "name": ov.get("name") or (extra_names or {}).get(repo) or repo,
            "color": ov.get("color") or PALETTE[i % len(PALETTE)],
        })
    repos = {t["repo"] for t in teams}

    commits = [c for c in db.fetch_all(
        "SELECT repo, sha, committed_at, author_hash, additions, deletions, is_merge, on_default, is_outlier "
        "FROM commits WHERE committed_at >= ? AND committed_at <= ? ORDER BY committed_at",
        (to_iso(start), to_iso(now)),
    ) if c["repo"] in repos]
    for c in commits:
        c["t"] = parse_iso(c["committed_at"])
    prs = [p for p in db.fetch_all("SELECT * FROM pull_requests") if p["repo"] in repos]

    work = [c for c in commits if not c["is_merge"]]
    loc_commits = [c for c in work if c["on_default"] and not c["is_outlier"]]

    # --- osie czasu ---
    bucket = _bucket_size(max(now - start, timedelta(minutes=10)))
    edges = []
    t = start + bucket
    while t < now:
        edges.append(t)
        t += bucket
    edges.append(now)

    def cumulative(rows, value) -> dict[str, list[float]]:
        per_team = {r: [] for r in repos}
        for r in rows:
            per_team[r["repo"]].append((r["t"], value(r)))
        out = {}
        for repo, items in per_team.items():
            times = [x[0] for x in items]
            prefix, s = [], 0
            for _, v in items:
                s += v
                prefix.append(s)
            out[repo] = [prefix[bisect_right(times, e) - 1] if bisect_right(times, e) else 0 for e in edges]
        return out

    cum_commits = cumulative(work, lambda r: 1)
    cum_loc = cumulative(loc_commits, lambda r: r["additions"] - r["deletions"])

    # --- metryki per zespół ---
    hour_ago, two_hours_ago = now - timedelta(hours=1), now - timedelta(hours=2)
    spark_edges = [now - timedelta(minutes=30 * k) for k in range(12, -1, -1)]
    team_stats = []
    for team in teams:
        repo = team["repo"]
        tw = [c for c in work if c["repo"] == repo]
        tl = [c for c in loc_commits if c["repo"] == repo]
        tp = [p for p in prs if p["repo"] == repo]
        added = sum(c["additions"] for c in tl)
        deleted = sum(c["deletions"] for c in tl)
        last_h = [c for c in tw if c["t"] > hour_ago]
        prev_h = [c for c in tw if two_hours_ago < c["t"] <= hour_ago]
        merge_minutes = [
            (parse_iso(p["merged_at"]) - parse_iso(p["created_at"])).total_seconds() / 60
            for p in tp if p["merged_at"]
        ]
        spark = [sum(1 for c in tw if spark_edges[k] < c["t"] <= spark_edges[k + 1]) for k in range(12)]
        team_stats.append({
            **team,
            "commits": len(tw),
            "loc_added": added,
            "loc_deleted": deleted,
            "loc_net": added - deleted,
            "prs_open": sum(1 for p in tp if p["state"] == "OPEN"),
            "prs_merged": sum(1 for p in tp if p["state"] == "MERGED"),
            "prs_closed": sum(1 for p in tp if p["state"] == "CLOSED"),
            "commits_1h": len(last_h),
            "lines_1h": sum(c["additions"] + c["deletions"] for c in tl if c["t"] > hour_ago),
            "authors_1h": len({c["author_hash"] for c in last_h if c["author_hash"]}),
            "momentum": len(last_h) - len(prev_h),
            "churn": round(deleted / added, 2) if added else 0,
            "median_merge_min": round(statistics.median(merge_minutes)) if merge_minutes else None,
            "outliers": sum(1 for c in tw if c["is_outlier"]),
            "spark": spark,
            "last_commit_at": tw[-1]["committed_at"] if tw else None,
        })

    # --- ranking i zmiany pozycji ---
    def rank(values: dict[str, float]) -> dict[str, int]:
        order = sorted(values, key=lambda r: (-values[r], r))
        return {r: i + 1 for i, r in enumerate(order)}

    rank_now = rank({t["repo"]: t["commits"] for t in team_stats})
    idx_hour_ago = max(0, bisect_right(edges, hour_ago) - 1)
    rank_before = rank({r: cum_commits[r][idx_hour_ago] if edges else 0 for r in repos})
    for t in team_stats:
        t["rank"] = rank_now[t["repo"]]
        t["rank_change"] = rank_before.get(t["repo"], t["rank"]) - t["rank"]

    # --- heatmapa: zespół x godzina hackatonu ---
    hours = []
    h = start.replace(minute=0, second=0, microsecond=0)
    while h <= now:
        hours.append(h)
        h += timedelta(hours=1)
    team_index = {t["repo"]: i for i, t in enumerate(teams)}
    heat: dict[tuple[int, int], int] = {}
    for c in work:
        hi = int((c["t"].replace(minute=0, second=0, microsecond=0) - hours[0]).total_seconds() // 3600)
        key = (hi, team_index[c["repo"]])
        heat[key] = heat.get(key, 0) + 1

    # --- wydarzenia do paska na żywo ---
    names = {t["repo"]: t["name"] for t in teams}
    events = []
    for p in prs:
        events.append({"t": p["created_at"], "type": "pr_open", "repo": p["repo"],
                       "text": f"{names[p['repo']]} otwiera PR #{p['number']}"})
        if p["merged_at"]:
            events.append({"t": p["merged_at"], "type": "pr_merged", "repo": p["repo"],
                           "text": f"{names[p['repo']]} scala PR #{p['number']}"})
    for repo in repos:
        team_prs = sorted((p for p in prs if p["repo"] == repo), key=lambda p: p["created_at"])
        if team_prs:
            events.append({"t": team_prs[0]["created_at"], "type": "milestone", "repo": repo,
                           "text": f"{names[repo]}: pierwszy pull request!"})
        n = 0
        for c in (c for c in work if c["repo"] == repo):
            n += 1
            if n in COMMIT_MILESTONES:
                events.append({"t": c["committed_at"], "type": "milestone", "repo": repo,
                               "text": f"{names[repo]} przekracza {n} commitów"})
        total, hit = 0, 0
        for c in (c for c in loc_commits if c["repo"] == repo):
            total += c["additions"] - c["deletions"]
            while hit < len(LOC_MILESTONES) and total >= LOC_MILESTONES[hit]:
                events.append({"t": c["committed_at"], "type": "milestone", "repo": repo,
                               "text": f"{names[repo]} ma już {LOC_MILESTONES[hit]:,} linii kodu".replace(",", " ")})
                hit += 1
    for t in team_stats:
        if t["rank_change"] >= 2 or (t["rank_change"] > 0 and t["rank"] <= 3):
            events.append({"t": to_iso(now), "type": "rank", "repo": t["repo"],
                           "text": f"{t['name']} awansuje na {t['rank']}. miejsce (+{t['rank_change']})"})
    events = [e for e in events if e["t"] <= to_iso(real_now)]
    events.sort(key=lambda e: e["t"], reverse=True)

    hottest = max(team_stats, key=lambda t: (t["commits_1h"], t["lines_1h"]), default=None)
    last_run = db.fetch_all("SELECT * FROM sync_runs WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1")
    last_ok = db.fetch_all("SELECT finished_at FROM sync_runs WHERE ok=1 ORDER BY id DESC LIMIT 1")

    return {
        "meta": {
            "event_name": cfg.event_name,
            "demo": cfg.demo,
            "start": to_iso(start),
            "end": to_iso(end),
            "now": to_iso(real_now),
            "timezone": cfg.timezone,
            "poll_interval": cfg.poll_interval,
            "last_success_at": last_ok[0]["finished_at"] if last_ok else None,
            "last_run_ok": bool(last_run[0]["ok"]) if last_run else None,
            "last_run_message": last_run[0]["message"] if last_run else None,
            "rate_limit_remaining": last_run[0]["rate_limit_remaining"] if last_run else None,
        },
        "totals": {
            "teams": len(teams),
            "commits": len(work),
            "loc_net": sum(t["loc_net"] for t in team_stats),
            "loc_added": sum(t["loc_added"] for t in team_stats),
            "prs_merged": sum(t["prs_merged"] for t in team_stats),
            "prs_open": sum(t["prs_open"] for t in team_stats),
            "commits_1h": sum(t["commits_1h"] for t in team_stats),
            "authors_1h": len({c["author_hash"] for c in work if c["t"] > hour_ago and c["author_hash"]}),
            "active_teams_1h": sum(1 for t in team_stats if t["commits_1h"] > 0),
        },
        "hottest": {"repo": hottest["repo"], "name": hottest["name"], "commits_1h": hottest["commits_1h"],
                    "lines_1h": hottest["lines_1h"]} if hottest and hottest["commits_1h"] else None,
        "teams": sorted(team_stats, key=lambda t: t["rank"]),
        "series": {
            "times": [to_iso(e) for e in edges],
            "labels": [_label(e, tz) for e in edges],
            "commits": cum_commits,
            "loc": cum_loc,
        },
        "heatmap": {
            "hours": [_label(hh, tz, with_minutes=False) for hh in hours],
            "teams": [t["repo"] for t in teams],
            "cells": [[hi, ti, v] for (hi, ti), v in sorted(heat.items())],
        },
        "events": events[:40],
    }

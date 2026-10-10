"""Kolektor: co POLL_INTERVAL_SECONDS pobiera przyrostowo commity i PR-y z repozytoriów
organizacji. Lokalnie działa w pętli; w GCP ten sam kod uruchomi Cloud Run job."""
from __future__ import annotations

import fnmatch
import hashlib
import logging
import threading
from datetime import datetime, timedelta, timezone

from .config import Config
from .db import Database, parse_iso, to_iso
from .github import GitHubClient, GitHubError

log = logging.getLogger("collector")

# Zakładka czasowa przy pobieraniu przyrostowym: łapie commity z opóźnioną datą
# (rebase, przesunięte zegary) bez ponownego pobierania całej historii.
OVERLAP = timedelta(hours=6)


def is_excluded(path: str, patterns: list[str]) -> bool:
    name = path.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(path, p) or fnmatch.fnmatch(name, p) for p in patterns)


def author_hash(author: dict | None) -> str | None:
    """Nie zapisujemy e-maili ani loginów, tylko skrót do liczenia unikalnych autorów."""
    if not author:
        return None
    ident = ((author.get("user") or {}).get("login") or author.get("email") or author.get("name") or "").lower()
    return hashlib.sha256(ident.encode()).hexdigest()[:16] if ident else None


def select_team_repos(repos: list[dict], cfg: Config) -> list[dict]:
    out = []
    for r in repos:
        if r["archived"]:
            continue
        if cfg.repo_prefix and not r["name"].startswith(cfg.repo_prefix):
            continue
        if cfg.repo_topic and cfg.repo_topic not in r["topics"]:
            continue
        out.append(r)
    return out


class Collector:
    def __init__(self, cfg: Config, db: Database, gh: GitHubClient):
        self.cfg, self.db, self.gh = cfg, db, gh

    def sync_once(self) -> bool:
        run_id = self.db.start_run()
        errors: list[str] = []
        try:
            repos = select_team_repos(self.gh.list_org_repos(self.cfg.github_org), self.cfg)
            log.info("Znaleziono %d repozytoriów zespołów", len(repos))
            for repo in repos:
                self.gh.throttle_if_needed()
                try:
                    self.sync_repo(repo)
                except GitHubError as e:
                    log.error("Repo %s: %s", repo["name"], e)
                    self.db.set_sync_error(repo["name"], str(e))
                    errors.append(f"{repo['name']}: {e}")
            msg = f"{len(repos)} repo, błędy: {len(errors)}"
            self.db.finish_run(run_id, not errors, msg + ("; " + "; ".join(errors) if errors else ""),
                               self.gh.rate_remaining)
            log.info("Synchronizacja zakończona: %s, limit API: %s", msg, self.gh.rate_remaining)
            return not errors
        except Exception as e:  # noqa: BLE001 - zapisujemy każdy błąd przebiegu
            log.error("Synchronizacja nieudana: %s", e)
            self.db.finish_run(run_id, False, str(e), self.gh.rate_remaining)
            return False

    def sync_repo(self, repo: dict) -> None:
        org, name = self.cfg.github_org, repo["name"]
        now = datetime.now(timezone.utc)
        self.db.upsert_team(name, repo["default_branch"], repo["archived"])

        state = self.db.sync_state(name)
        start = self.cfg.hackathon_start
        if state and state.get("last_success_at"):
            since = max(start, parse_iso(state["last_success_at"]) - OVERLAP)
            pr_since = to_iso(parse_iso(state["last_success_at"]) - OVERLAP)
        else:
            since, pr_since = start, None

        known = self.db.known_commits(name)
        heads = self.db.branch_heads(name)
        new_commits = 0

        for branch, head in self.gh.list_branches(org, name):
            if heads.get(branch) == head:
                continue  # gałąź bez zmian od ostatniego przebiegu
            on_default = int(branch == repo["default_branch"])
            to_mark = []
            for node in self.gh.branch_history(org, name, branch, to_iso(since)):
                sha = node["oid"]
                if sha in known:
                    if on_default and not known[sha]:
                        to_mark.append(sha)
                        known[sha] = 1
                    continue
                self.db.upsert_commit(self._build_commit(name, node, on_default, now))
                known[sha] = on_default
                new_commits += 1
            self.db.mark_on_default(name, to_mark)
            self.db.set_branch_head(name, branch, head)

        prs = self.gh.pull_requests(org, name, pr_since)
        for pr in prs:
            self.db.upsert_pr({
                "repo": name, "number": pr["number"], "title": pr["title"], "state": pr["state"],
                "created_at": pr["createdAt"], "merged_at": pr["mergedAt"], "closed_at": pr["closedAt"],
                "updated_at": pr["updatedAt"], "additions": pr["additions"], "deletions": pr["deletions"],
            })

        self.db.set_sync_success(name, to_iso(now))
        log.info("%s: %d nowych commitów, %d zmienionych PR", name, new_commits, len(prs))

    def _build_commit(self, repo: str, node: dict, on_default: int, now: datetime) -> dict:
        raw_add, raw_del = node.get("additions") or 0, node.get("deletions") or 0
        is_merge = int((node.get("parents") or {}).get("totalCount", 1) > 1)
        additions, deletions, source = raw_add, raw_del, "commit"
        changed = node.get("changedFilesIfAvailable") or 0

        if self.cfg.file_level_stats and not is_merge and (raw_add + raw_del) > 0:
            try:
                files = self.gh.commit_files(self.cfg.github_org, repo, node["oid"])
                kept = [f for f in files if not is_excluded(f.get("filename", ""), self.cfg.exclude_patterns)]
                additions = sum(f.get("additions", 0) for f in kept)
                deletions = sum(f.get("deletions", 0) for f in kept)
                changed, source = len(files), "files"
            except GitHubError as e:
                log.warning("Brak statystyk plików dla %s@%s: %s", repo, node["oid"][:7], e)

        # Commit z datą z przyszłości liczymy od momentu, w którym go zobaczyliśmy.
        committed = min(parse_iso(node["committedDate"]), now)
        return {
            "repo": repo, "sha": node["oid"], "committed_at": to_iso(committed),
            "first_seen_at": to_iso(now), "author_hash": author_hash(node.get("author")),
            "additions": additions, "deletions": deletions,
            "raw_additions": raw_add, "raw_deletions": raw_del, "changed_files": changed,
            "is_merge": is_merge, "on_default": on_default,
            "is_outlier": int(additions + deletions > self.cfg.outlier_lines),
            "stats_source": source,
        }


def run_forever(cfg: Config, db: Database, stop: threading.Event) -> None:
    gh = GitHubClient(cfg.github_token)
    collector = Collector(cfg, db, gh)
    while not stop.is_set():
        collector.sync_once()
        stop.wait(cfg.poll_interval)

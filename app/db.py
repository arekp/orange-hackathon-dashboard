"""Warstwa zapisu: SQLite lokalnie. Schemat odpowiada tabelom planowanym w BigQuery,
więc przejście na GCP to podmiana tego modułu na adapter BigQuery."""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS teams (
    repo            TEXT PRIMARY KEY,
    default_branch  TEXT,
    discovered_at   TEXT NOT NULL,
    archived        INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS commits (
    repo            TEXT NOT NULL,
    sha             TEXT NOT NULL,
    committed_at    TEXT NOT NULL,
    first_seen_at   TEXT NOT NULL,
    author_hash     TEXT,
    additions       INTEGER NOT NULL DEFAULT 0,
    deletions       INTEGER NOT NULL DEFAULT 0,
    raw_additions   INTEGER NOT NULL DEFAULT 0,
    raw_deletions   INTEGER NOT NULL DEFAULT 0,
    changed_files   INTEGER NOT NULL DEFAULT 0,
    is_merge        INTEGER NOT NULL DEFAULT 0,
    on_default      INTEGER NOT NULL DEFAULT 0,
    is_outlier      INTEGER NOT NULL DEFAULT 0,
    stats_source    TEXT NOT NULL DEFAULT 'commit',
    PRIMARY KEY (repo, sha)
);
CREATE INDEX IF NOT EXISTS idx_commits_time ON commits (committed_at);

CREATE TABLE IF NOT EXISTS pull_requests (
    repo        TEXT NOT NULL,
    number      INTEGER NOT NULL,
    title       TEXT,
    state       TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    merged_at   TEXT,
    closed_at   TEXT,
    updated_at  TEXT,
    additions   INTEGER NOT NULL DEFAULT 0,
    deletions   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (repo, number)
);

CREATE TABLE IF NOT EXISTS branch_heads (
    repo        TEXT NOT NULL,
    branch      TEXT NOT NULL,
    head_sha    TEXT NOT NULL,
    PRIMARY KEY (repo, branch)
);

CREATE TABLE IF NOT EXISTS sync_state (
    repo            TEXT PRIMARY KEY,
    last_success_at TEXT,
    last_error      TEXT,
    last_error_at   TEXT
);

CREATE TABLE IF NOT EXISTS sync_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    ok          INTEGER,
    message     TEXT,
    rate_limit_remaining INTEGER
);
"""


def to_iso(dt: datetime) -> str:
    """Wszystkie czasy zapisujemy w UTC w formacie GitHuba (2026-10-10T09:00:00Z),
    dzięki czemu porównania tekstowe w SQL działają poprawnie."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def utcnow_iso() -> str:
    return to_iso(datetime.now(timezone.utc))


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # --- zapis (idempotentny, odpowiednik MERGE w BigQuery) ---

    def upsert_team(self, repo: str, default_branch: str | None, archived: bool) -> None:
        with self._lock, self.connect() as conn:
            conn.execute(
                """INSERT INTO teams (repo, default_branch, discovered_at, archived)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(repo) DO UPDATE SET default_branch=excluded.default_branch,
                                                   archived=excluded.archived""",
                (repo, default_branch, utcnow_iso(), int(archived)),
            )

    def known_commits(self, repo: str) -> dict[str, int]:
        """sha -> on_default dla commitów repozytorium."""
        with self.connect() as conn:
            rows = conn.execute("SELECT sha, on_default FROM commits WHERE repo=?", (repo,))
            return {r["sha"]: r["on_default"] for r in rows}

    def upsert_commit(self, c: dict) -> None:
        with self._lock, self.connect() as conn:
            conn.execute(
                """INSERT INTO commits (repo, sha, committed_at, first_seen_at, author_hash,
                       additions, deletions, raw_additions, raw_deletions, changed_files,
                       is_merge, on_default, is_outlier, stats_source)
                   VALUES (:repo, :sha, :committed_at, :first_seen_at, :author_hash,
                       :additions, :deletions, :raw_additions, :raw_deletions, :changed_files,
                       :is_merge, :on_default, :is_outlier, :stats_source)
                   ON CONFLICT(repo, sha) DO UPDATE SET
                       on_default = MAX(commits.on_default, excluded.on_default)""",
                c,
            )

    def mark_on_default(self, repo: str, shas: list[str]) -> None:
        if not shas:
            return
        with self._lock, self.connect() as conn:
            conn.executemany(
                "UPDATE commits SET on_default=1 WHERE repo=? AND sha=?",
                [(repo, s) for s in shas],
            )

    def upsert_pr(self, pr: dict) -> None:
        with self._lock, self.connect() as conn:
            conn.execute(
                """INSERT INTO pull_requests (repo, number, title, state, created_at, merged_at,
                       closed_at, updated_at, additions, deletions)
                   VALUES (:repo, :number, :title, :state, :created_at, :merged_at,
                       :closed_at, :updated_at, :additions, :deletions)
                   ON CONFLICT(repo, number) DO UPDATE SET
                       title=excluded.title, state=excluded.state, merged_at=excluded.merged_at,
                       closed_at=excluded.closed_at, updated_at=excluded.updated_at,
                       additions=excluded.additions, deletions=excluded.deletions""",
                pr,
            )

    def branch_heads(self, repo: str) -> dict[str, str]:
        with self.connect() as conn:
            rows = conn.execute("SELECT branch, head_sha FROM branch_heads WHERE repo=?", (repo,))
            return {r["branch"]: r["head_sha"] for r in rows}

    def set_branch_head(self, repo: str, branch: str, sha: str) -> None:
        with self._lock, self.connect() as conn:
            conn.execute(
                """INSERT INTO branch_heads (repo, branch, head_sha) VALUES (?, ?, ?)
                   ON CONFLICT(repo, branch) DO UPDATE SET head_sha=excluded.head_sha""",
                (repo, branch, sha),
            )

    def sync_state(self, repo: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM sync_state WHERE repo=?", (repo,)).fetchone()
            return dict(row) if row else None

    def set_sync_success(self, repo: str, at: str) -> None:
        with self._lock, self.connect() as conn:
            conn.execute(
                """INSERT INTO sync_state (repo, last_success_at) VALUES (?, ?)
                   ON CONFLICT(repo) DO UPDATE SET last_success_at=excluded.last_success_at,
                                                   last_error=NULL""",
                (repo, at),
            )

    def set_sync_error(self, repo: str, error: str) -> None:
        with self._lock, self.connect() as conn:
            conn.execute(
                """INSERT INTO sync_state (repo, last_error, last_error_at) VALUES (?, ?, ?)
                   ON CONFLICT(repo) DO UPDATE SET last_error=excluded.last_error,
                                                   last_error_at=excluded.last_error_at""",
                (repo, error[:500], utcnow_iso()),
            )

    def start_run(self) -> int:
        with self._lock, self.connect() as conn:
            cur = conn.execute("INSERT INTO sync_runs (started_at) VALUES (?)", (utcnow_iso(),))
            return int(cur.lastrowid)

    def finish_run(self, run_id: int, ok: bool, message: str, rate_remaining: int | None) -> None:
        with self._lock, self.connect() as conn:
            conn.execute(
                "UPDATE sync_runs SET finished_at=?, ok=?, message=?, rate_limit_remaining=? WHERE id=?",
                (utcnow_iso(), int(ok), message[:1000], rate_remaining, run_id),
            )

    # --- odczyt dla metryk ---

    def fetch_all(self, sql: str, params: tuple = ()) -> list[dict]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(sql, params)]

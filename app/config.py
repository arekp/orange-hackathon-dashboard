"""Konfiguracja aplikacji z zmiennych środowiskowych (lub pliku .env)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Minimalny parser .env, żeby nie wymagać zewnętrznych bibliotek."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _parse_time(value: str | None, default: datetime) -> datetime:
    if not value:
        return default
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "tak", "on")


@dataclass
class Config:
    github_token: str
    github_org: str
    repo_prefix: str
    repo_topic: str
    poll_interval: int
    hackathon_start: datetime
    hackathon_end: datetime
    db_path: Path
    teams_file: Path
    port: int
    timezone: str
    file_level_stats: bool
    outlier_lines: int
    exclude_patterns: list[str] = field(default_factory=list)
    demo: bool = False
    event_name: str = "Orange Hackathon"


DEFAULT_EXCLUDES = [
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "Pipfile.lock",
    "go.sum", "Cargo.lock", "composer.lock", "Gemfile.lock",
    "node_modules/*", "*/node_modules/*", "dist/*", "*/dist/*", "build/*", "*/build/*",
    "vendor/*", "*/vendor/*", ".next/*", "*/.next/*", "target/*", "*/target/*",
    "*.min.js", "*.min.css", "*.map",
    "*.png", "*.jpg", "*.jpeg", "*.gif", "*.svg", "*.ico", "*.webp", "*.pdf",
    "*.woff", "*.woff2", "*.ttf", "*.eot", "*.zip", "*.jar", "*.bin",
]


def load_config(demo: bool | None = None) -> Config:
    _load_dotenv(ROOT / ".env")
    now = datetime.now(timezone.utc)
    is_demo = _bool(os.environ.get("DEMO"), False) if demo is None else demo

    if is_demo:
        # W trybie demo hackaton "trwa" od 30 h i potrwa jeszcze 18 h.
        default_start = now - timedelta(hours=30)
        default_end = now + timedelta(hours=18)
    else:
        default_start = now - timedelta(hours=24)
        default_end = now + timedelta(hours=24)

    extra = [p.strip() for p in os.environ.get("EXCLUDE_PATTERNS", "").split(",") if p.strip()]

    return Config(
        github_token=os.environ.get("GITHUB_TOKEN", ""),
        github_org=os.environ.get("GITHUB_ORG", ""),
        repo_prefix=os.environ.get("REPO_PREFIX", ""),
        repo_topic=os.environ.get("REPO_TOPIC", ""),
        poll_interval=int(os.environ.get("POLL_INTERVAL_SECONDS", "600")),
        hackathon_start=default_start if is_demo else _parse_time(os.environ.get("HACKATHON_START"), default_start),
        hackathon_end=default_end if is_demo else _parse_time(os.environ.get("HACKATHON_END"), default_end),
        db_path=Path(os.environ.get("DB_PATH", str(ROOT / "data" / ("demo.db" if is_demo else "hackathon.db")))),
        teams_file=Path(os.environ.get("TEAMS_FILE", str(ROOT / "teams.json"))),
        port=int(os.environ.get("PORT", "8080")),
        timezone=os.environ.get("TIMEZONE", "Europe/Warsaw"),
        file_level_stats=_bool(os.environ.get("FILE_LEVEL_STATS"), True),
        outlier_lines=int(os.environ.get("OUTLIER_LINES", "5000")),
        exclude_patterns=DEFAULT_EXCLUDES + extra,
        demo=is_demo,
        event_name=os.environ.get("EVENT_NAME", "Orange Hackathon"),
    )

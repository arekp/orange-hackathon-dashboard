"""Uruchomienie lokalne.

    python main.py            # kolektor GitHub + dashboard (wymaga .env z tokenem)
    python main.py --demo     # dane testowe, bez tokenu
    python main.py --once     # jedna synchronizacja i koniec (jak Cloud Run job)
    python main.py --serve-only
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading

from app.config import load_config
from app.db import Database


def main() -> int:
    parser = argparse.ArgumentParser(description="Dashboard dynamiki zespołów na hackatonie")
    parser.add_argument("--demo", action="store_true", help="dane testowe zamiast GitHuba")
    parser.add_argument("--once", action="store_true", help="jedna synchronizacja z GitHubem i wyjście")
    parser.add_argument("--serve-only", action="store_true", help="tylko dashboard, bez kolektora")
    parser.add_argument("--collect-only", action="store_true", help="tylko kolektor, bez dashboardu")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config(demo=True if args.demo else None)
    db = Database(cfg.db_path)
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())

    extra_names = None
    if cfg.demo:
        from app.demo import demo_repo_names, run_demo
        extra_names = demo_repo_names()
        if not args.serve_only:
            threading.Thread(target=run_demo, args=(cfg, db, stop), daemon=True).start()
    else:
        from app.collector import Collector, run_forever
        from app.github import GitHubClient, GitHubError
        if not cfg.github_org and not args.serve_only:
            logging.error("Brak GITHUB_ORG w .env")
            return 2
        try:
            if args.once:
                ok = Collector(cfg, db, GitHubClient(cfg.github_token)).sync_once()
                return 0 if ok else 1
            if not args.serve_only:
                GitHubClient(cfg.github_token)  # wczesna walidacja tokenu
                threading.Thread(target=run_forever, args=(cfg, db, stop), daemon=True).start()
        except GitHubError as e:
            logging.error("%s", e)
            return 2

    if args.collect_only:
        stop.wait()
        return 0

    from app.server import serve
    serve(cfg, db, extra_names, stop)
    return 0


if __name__ == "__main__":
    sys.exit(main())

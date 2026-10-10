"""Serwer HTTP: udostępnia frontend i API z agregatami (biblioteka standardowa)."""
from __future__ import annotations

import json
import logging
import mimetypes
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .config import ROOT, Config
from .db import Database, to_iso, utcnow_iso
from .metrics import compute_dashboard

log = logging.getLogger("server")
STATIC = ROOT / "static"


class DashboardCache:
    """Wiele ekranów odpytuje API naraz, więc wynik trzymamy kilka sekund."""

    def __init__(self, cfg: Config, db: Database, extra_names: dict[str, str] | None, ttl: float = 5.0):
        self.cfg, self.db, self.extra_names, self.ttl = cfg, db, extra_names, ttl
        self._lock = threading.Lock()
        self._at = 0.0
        self._body = b""

    def get(self) -> bytes:
        with self._lock:
            if time.monotonic() - self._at > self.ttl:
                data = compute_dashboard(self.cfg, self.db, self.extra_names)
                self._body = json.dumps(data, ensure_ascii=False).encode()
                self._at = time.monotonic()
            return self._body


def make_handler(cache: DashboardCache):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # ciszej w konsoli
            log.debug(fmt, *args)

        def _send(self, code: int, body: bytes, ctype: str, cache_control: str = "no-store") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache_control)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/api/dashboard":
                try:
                    return self._send(200, cache.get(), "application/json; charset=utf-8")
                except Exception as e:  # noqa: BLE001
                    log.exception("Błąd liczenia metryk")
                    return self._send(500, json.dumps({"error": str(e)}).encode(), "application/json")
            if path == "/api/meta":
                cfg = cache.cfg
                meta = {"event_name": cfg.event_name, "start": to_iso(cfg.hackathon_start),
                        "end": to_iso(cfg.hackathon_end), "now": utcnow_iso(), "demo": cfg.demo,
                        "timezone": cfg.timezone}
                return self._send(200, json.dumps(meta, ensure_ascii=False).encode(), "application/json; charset=utf-8")
            if path == "/api/health":
                return self._send(200, b'{"ok":true}', "application/json")
            if path == "/":
                path = "/index.html"
            file = (STATIC / path.lstrip("/")).resolve()
            if not str(file).startswith(str(STATIC.resolve())) or not file.is_file():
                return self._send(404, b"Not found", "text/plain")
            ctype = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype.endswith("javascript"):
                ctype += "; charset=utf-8"
            return self._send(200, file.read_bytes(), ctype, "no-cache")

    return Handler


def serve(cfg: Config, db: Database, extra_names: dict[str, str] | None, stop: threading.Event) -> None:
    cache = DashboardCache(cfg, db, extra_names)
    httpd = ThreadingHTTPServer(("0.0.0.0", cfg.port), make_handler(cache))
    log.info("Dashboard: http://localhost:%d", cfg.port)
    threading.Thread(target=lambda: (stop.wait(), httpd.shutdown()), daemon=True).start()
    httpd.serve_forever()

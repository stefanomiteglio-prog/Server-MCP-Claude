"""Dashboard web locale: mostra le chiamate ai modelli lette dal registro (solo metadati).

È un processo separato dal server MCP: `local-llm-dashboard` legge il file SQLite scritto dalle
sessioni di Claude Code e lo serve su 127.0.0.1. Non contatta l'API e non richiede la API key.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .calllog import OUTCOME_CLASSES, WINDOWS, dashboard_data
from .config import ConfigError, call_log_path, load_env_file

logger = logging.getLogger("local_llm_mcp")

HOST = "127.0.0.1"
DEFAULT_PORT = 8765
MAX_FILTER_CHARS = 100
# Solo risorse della pagina stessa: niente CDN, niente host esterni.
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
        "connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port: int, call_log: Path | None) -> None:
        super().__init__((HOST, port), DashboardHandler)
        self.call_log = call_log
        self.page = resources.files(__package__).joinpath("dashboard.html").read_bytes()

    @property
    def url(self) -> str:
        return f"http://{HOST}:{self.server_address[1]}/"


class DashboardHandler(BaseHTTPRequestHandler):
    server: DashboardServer

    def do_GET(self) -> None:  # noqa: N802 (nome imposto da http.server)
        # Contro il DNS rebinding: una pagina esterna non può farsi servire i dati via un suo nome.
        port = self.server.server_address[1]
        if self.headers.get("Host") not in (f"{HOST}:{port}", f"localhost:{port}"):
            self._send(403, b"Host non consentito", "text/plain; charset=utf-8")
            return
        url = urlsplit(self.path)
        if url.path == "/":
            self._send(200, self.server.page, "text/html; charset=utf-8")
        elif url.path == "/api/calls":
            self._api(parse_qs(url.query))
        else:
            self._send(404, b"Non trovato", "text/plain; charset=utf-8")

    def _api(self, query: dict[str, list[str]]) -> None:
        def param(name: str) -> str | None:
            value = (query.get(name) or [""])[0].strip()
            return value[:MAX_FILTER_CHARS] or None

        window = param("window") or "24h"
        outcome = param("outcome")
        if window not in WINDOWS or (outcome and outcome not in OUTCOME_CLASSES):
            self._json(400, {"error": "parametro window o outcome non valido"})
            return
        try:
            data = dashboard_data(
                self.server.call_log,
                window,
                tool=param("tool"),
                model=param("model"),
                outcome=outcome,
            )
        except sqlite3.Error as exc:
            logger.warning("dashboard: lettura del registro non riuscita (%s)", exc)
            self._json(503, {"error": f"registro non leggibile: {type(exc).__name__}"})
            return
        self._json(200, data)

    def _json(self, status: int, body: dict[str, Any]) -> None:
        payload = json.dumps(body, ensure_ascii=False).encode()
        self._send(status, payload, "application/json; charset=utf-8")

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        logger.debug("dashboard " + format, *args)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="local-llm-dashboard",
        description="Dashboard web locale delle chiamate ai modelli self-hosted (solo metadati).",
    )
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help=f"porta su {HOST} (default {DEFAULT_PORT})"
    )
    parser.add_argument("--no-browser", action="store_true", help="non aprire il browser")
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    logging.basicConfig(
        stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    load_env_file()
    try:
        path = call_log_path()
    except ConfigError as exc:
        print(f"[local-llm-dashboard] Configurazione non valida: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    if path is None:
        print(
            "[local-llm-dashboard] Il registro delle chiamate è disattivato "
            "(LOCAL_LLM_CALL_LOG=off): la dashboard non avrebbe dati.",
            file=sys.stderr,
        )
        raise SystemExit(2)

    try:
        server = DashboardServer(args.port, path)
    except OSError as exc:
        print(f"[local-llm-dashboard] Porta {args.port} non disponibile: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    print(f"Dashboard: {server.url}  (registro: {path})  Ctrl+C per uscire")
    if not args.no_browser:
        webbrowser.open(server.url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

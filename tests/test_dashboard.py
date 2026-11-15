import http.client
import json
import threading

import pytest

from local_llm_mcp.calllog import CallLog
from local_llm_mcp.dashboard import DashboardServer


@pytest.fixture
def dashboard(tmp_path):
    """Dashboard su una porta libera di 127.0.0.1 con un registro di prova."""
    path = tmp_path / "calls.sqlite3"
    log = CallLog(path)
    log.record({"tool": "summarize_text", "model": "gemma-4-26b", "outcome": "ok"})
    log.record({"tool": "classify_text", "model": "gemma-4-26b", "outcome": "error:503"})
    server = DashboardServer(0, path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def get(server, path, host=None):
    port = server.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path, headers={"Host": host or f"127.0.0.1:{port}"})
    response = conn.getresponse()
    body = response.read()
    conn.close()
    return response, body


def test_serves_page_with_strict_headers(dashboard):
    response, body = get(dashboard, "/")

    assert response.status == 200
    assert response.headers["Content-Type"].startswith("text/html")
    assert "default-src 'none'" in response.headers["Content-Security-Policy"]
    assert b"/api/calls" in body
    assert b"https://" not in body  # nessuna risorsa esterna


def test_api_returns_aggregated_data(dashboard):
    response, body = get(dashboard, "/api/calls?window=1h&outcome=error")

    data = json.loads(body)
    assert response.status == 200
    assert data["available"] is True
    assert data["summary"]["calls"] == 1
    assert data["recent"][0]["tool"] == "classify_text"


def test_localhost_host_is_accepted(dashboard):
    response, _ = get(dashboard, "/api/calls", host=f"localhost:{dashboard.server_address[1]}")

    assert response.status == 200


@pytest.mark.parametrize("query", ["window=2y", "outcome=boh"])
def test_invalid_parameters(dashboard, query):
    response, body = get(dashboard, f"/api/calls?{query}")

    assert response.status == 400
    assert "non valido" in json.loads(body)["error"]


def test_foreign_host_is_rejected(dashboard):
    response, _ = get(dashboard, "/api/calls", host="evil.example:80")

    assert response.status == 403


def test_unknown_path(dashboard):
    response, _ = get(dashboard, "/../.env")

    assert response.status == 404


def test_missing_log_reports_unavailable(tmp_path):
    server = DashboardServer(0, tmp_path / "missing.sqlite3")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        response, body = get(server, "/api/calls")
    finally:
        server.shutdown()
        server.server_close()

    assert response.status == 200
    assert json.loads(body)["available"] is False

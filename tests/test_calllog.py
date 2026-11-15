import sqlite3
from contextlib import closing

import pytest
from fastmcp.exceptions import ToolError
from helpers import chat_response, error_response

from local_llm_mcp.calllog import CallLog, dashboard_data

NOW = 1_800_000_000.0


class Clock:
    def __init__(self, now: float = NOW) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def rec(**values):
    base = {"tool": "summarize_text", "model": "gemma-4-26b", "outcome": "ok", "duration_ms": 100}
    return {**base, **values}


def rows(path):
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        return [dict(r) for r in db.execute("SELECT * FROM calls ORDER BY id")]


def test_records_only_whitelisted_metadata(tmp_path):
    path = tmp_path / "sub" / "calls.sqlite3"
    log = CallLog(path, clock=Clock())

    log.record(rec(input_chars=5120, prompt_tokens=10, request_id="req_1", text="contenuto"))

    (row,) = rows(path)
    assert row["ts"] == NOW
    assert row["tool"] == "summarize_text"
    assert row["input_chars"] == 5120
    assert row["request_id"] == "req_1"
    assert row["pid"] > 0
    assert "text" not in row
    assert "contenuto" not in path.read_bytes().decode("latin-1")


def test_retention_deletes_old_rows_at_startup(tmp_path):
    path = tmp_path / "calls.sqlite3"
    clock = Clock()
    log = CallLog(path, retention_days=30, clock=clock)
    clock.now = NOW - 31 * 86400
    log.record(rec(request_id="old"))
    clock.now = NOW - 29 * 86400
    log.record(rec(request_id="recent"))

    CallLog(path, retention_days=30, clock=Clock())

    assert [r["request_id"] for r in rows(path)] == ["recent"]


def test_unwritable_path_disables_log_without_failing(tmp_path, caplog):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    log = CallLog(blocker / "calls.sqlite3")

    log.record(rec())

    assert "call_log disattivato" in caplog.text


def test_dashboard_data_missing_file(tmp_path):
    data = dashboard_data(tmp_path / "missing.sqlite3", "24h", now=NOW)

    assert data["available"] is False
    assert "summary" not in data
    assert dashboard_data(None, now=NOW)["path"] is None


def test_dashboard_aggregates(tmp_path):
    path = tmp_path / "calls.sqlite3"
    clock = Clock(NOW - 600)
    log = CallLog(path, clock=clock)
    log.record(rec(duration_ms=100, prompt_tokens=10, completion_tokens=5, input_chars=1000))
    log.record(rec(duration_ms=300, prompt_tokens=20, completion_tokens=5, chunks=3))
    log.record(rec(tool="classify_text", outcome="invalid_output:bad_label", duration_ms=200))
    log.record(rec(tool="list_models", model="-", outcome="no_listing", duration_ms=50))
    clock.now = NOW - 60
    log.record(rec(model="qwen3-coder-30b", outcome="error:401", duration_ms=1000))
    clock.now = NOW - 2 * 86400  # fuori dalla finestra di 24 ore
    log.record(rec(duration_ms=9999))

    data = dashboard_data(path, "24h", now=NOW)

    s = data["summary"]
    assert data["available"] is True
    assert (s["calls"], s["ok"], s["invalid"], s["error"]) == (5, 3, 1, 1)
    assert (s["prompt_tokens"], s["completion_tokens"]) == (30, 10)
    assert s["chunked_calls"] == 1
    assert (s["p50_ms"], s["p95_ms"], s["max_ms"]) == (200, 1000, 1000)
    assert s["avg_ms"] == 330

    buckets = data["timeline"]["buckets"]
    assert data["timeline"]["bucket_seconds"] == 1800
    assert len(buckets) == 48
    assert buckets[-1] == {"start": NOW - 1800, "ok": 3, "partial": 0, "invalid": 1, "error": 1}
    assert sum(b["ok"] + b["invalid"] + b["error"] for b in buckets) == 5

    by_tool = {g["name"]: g for g in data["by_tool"]}
    assert by_tool["summarize_text"]["calls"] == 3
    assert by_tool["summarize_text"]["failures"] == 1
    assert by_tool["summarize_text"]["tokens"] == 40
    assert data["by_tool"][0]["name"] == "summarize_text"
    assert {o["outcome"] for o in data["outcomes"]} == {"error:401", "invalid_output:bad_label"}
    assert data["recent"][0]["outcome"] == "error:401"
    assert data["options"]["model"] == ["-", "gemma-4-26b", "qwen3-coder-30b"]


def test_dashboard_filters(tmp_path):
    path = tmp_path / "calls.sqlite3"
    log = CallLog(path, clock=Clock(NOW - 10))
    log.record(rec())
    log.record(rec(tool="classify_text", outcome="invalid_output:bad_label"))
    log.record(rec(model="qwen3-coder-30b", outcome="error:503"))

    assert dashboard_data(path, "1h", tool="classify_text", now=NOW)["summary"]["calls"] == 1
    assert dashboard_data(path, "1h", model="qwen3-coder-30b", now=NOW)["summary"]["error"] == 1
    only_errors = dashboard_data(path, "1h", outcome="error", now=NOW)
    assert [r["outcome"] for r in only_errors["recent"]] == ["error:503"]
    # Le opzioni dei filtri restano complete anche con un filtro attivo.
    assert only_errors["options"]["tool"] == ["classify_text", "summarize_text"]
    assert dashboard_data(path, "1h", tool="nessuno", now=NOW)["summary"]["calls"] == 0


def test_partial_reviews_are_a_separate_class(tmp_path):
    path = tmp_path / "calls.sqlite3"
    log = CallLog(path, clock=Clock(NOW - 10))
    log.record(rec(tool="review_code_diff"))
    log.record(rec(tool="review_code_diff", outcome="partial:truncated", chunks=3, failed_chunks=1))
    log.record(rec(tool="review_code_diff", outcome="error:400"))

    data = dashboard_data(path, "1h", now=NOW)

    s = data["summary"]
    assert (s["calls"], s["ok"], s["partial"], s["invalid"], s["error"]) == (3, 1, 1, 0, 1)
    assert data["timeline"]["buckets"][-1]["partial"] == 1
    (group,) = data["by_tool"]
    assert (group["partial"], group["failures"]) == (1, 1)
    only_partial = dashboard_data(path, "1h", outcome="partial", now=NOW)["recent"]
    assert [(r["outcome"], r["chunks"], r["failed_chunks"]) for r in only_partial] == [
        ("partial:truncated", 3, 1)
    ]


def test_existing_log_gets_new_columns(tmp_path):
    path = tmp_path / "calls.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE calls (id INTEGER PRIMARY KEY, ts REAL NOT NULL, pid INTEGER, "
            "tool TEXT NOT NULL, model TEXT, outcome TEXT NOT NULL, duration_ms INTEGER, "
            "input_chars INTEGER, output_chars INTEGER, prompt_tokens INTEGER, "
            "completion_tokens INTEGER, chunks INTEGER, redacted_secrets INTEGER, "
            "finish_reason TEXT, request_id TEXT)"
        )
        db.execute(
            "INSERT INTO calls (ts, tool, outcome) VALUES (?, 'ask_local_model', 'ok')", (NOW,)
        )
    db.close()

    CallLog(path, clock=Clock()).record(rec(outcome="partial:truncated", failed_chunks=2))

    assert [r["failed_chunks"] for r in rows(path)] == [None, 2]


async def test_tool_calls_are_recorded(api, call, tmp_path):
    path = tmp_path / "calls.sqlite3"
    schema = {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]}
    api.queue(chat_response({"x": "1"}), error_response(401, "bad key"))

    args = {"json_schema": schema, "text": "Un testo da estrarre."}
    await call("extract_structured_data", args, call_log=path)
    with pytest.raises(ToolError):
        await call("critique_plan", {"plan": "Piano: fare X."}, call_log=path)

    ok, failed = rows(path)
    assert ok["tool"] == "extract_structured_data"
    assert ok["model"] == "glm-5.3-flash"
    assert (ok["outcome"], ok["effort"]) == ("ok", "low")
    assert ok["request_id"] == "req_test123"
    assert (ok["prompt_tokens"], ok["completion_tokens"]) == (10, 5)
    assert failed["tool"] == "critique_plan"
    assert failed["outcome"] == "error:401"
    assert "Un testo da estrarre" not in path.read_bytes().decode("latin-1")


async def test_no_call_log_by_default(api, call, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    schema = {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]}
    api.queue(chat_response({"x": "1"}))

    await call("extract_structured_data", {"json_schema": schema, "text": "x"})

    assert list(tmp_path.iterdir()) == []


OLD_SCHEMA = """
CREATE TABLE calls (
    id INTEGER PRIMARY KEY, ts REAL NOT NULL, pid INTEGER, tool TEXT NOT NULL, model TEXT,
    outcome TEXT NOT NULL, duration_ms INTEGER, input_chars INTEGER, output_chars INTEGER,
    prompt_tokens INTEGER, completion_tokens INTEGER, chunks INTEGER, redacted_secrets INTEGER,
    finish_reason TEXT, request_id TEXT
);
INSERT INTO calls (ts, tool, outcome) VALUES (1799999990, 'summarize_text', 'ok');
"""


def test_old_log_gets_new_columns_at_startup(tmp_path):
    path = tmp_path / "calls.sqlite3"
    with closing(sqlite3.connect(path)) as db:
        db.executescript(OLD_SCHEMA)

    CallLog(path, clock=Clock()).record(rec(retried_without_reasoning=2))

    old, new = rows(path)
    assert old["retried_without_reasoning"] is None
    assert new["retried_without_reasoning"] == 2


def test_dashboard_reads_old_log_without_new_columns(tmp_path):
    path = tmp_path / "calls.sqlite3"
    with closing(sqlite3.connect(path)) as db:
        db.executescript(OLD_SCHEMA)

    data = dashboard_data(path, "1h", now=NOW)

    assert data["summary"]["calls"] == 1
    assert data["summary"]["retried_calls"] == 0
    assert data["recent"][0]["retried_without_reasoning"] is None


def test_dashboard_counts_calls_retried_without_reasoning(tmp_path):
    path = tmp_path / "calls.sqlite3"
    log = CallLog(path, clock=Clock(NOW - 10))
    log.record(rec(retried_without_reasoning=3))
    log.record(rec())

    data = dashboard_data(path, "1h", now=NOW)

    assert data["summary"]["retried_calls"] == 1
    assert [r["retried_without_reasoning"] for r in data["recent"]] == [None, 3]


def test_dashboard_summarizes_agent_metrics(tmp_path):
    path = tmp_path / "calls.sqlite3"
    log = CallLog(path, clock=Clock(NOW - 10))
    glm = {"tool": "ask_codebase", "model": "glm-5.3-flash", "effort": "medium", "steps": 6}
    log.record(rec(**glm, citations_total=10, citations_verified=9, stopped="steps"))
    log.record(rec(**{**glm, "tool": "propose_change"}, applies=1))
    log.record(
        rec(**{**glm, "tool": "propose_change"}, applies=0, citations_total=2, citations_verified=2)
    )

    data = dashboard_data(path, "1h", now=NOW)

    s = data["summary"]
    assert (s["citations_verified"], s["citations_total"]) == (11, 12)
    assert (s["patches_applied"], s["patches"], s["stopped_calls"]) == (1, 2, 1)
    by_tool = {g["name"]: g for g in data["by_tool"]}
    assert (
        by_tool["ask_codebase"]["citations_verified"],
        by_tool["ask_codebase"]["citations_total"],
    ) == (9, 10)
    assert data["recent"][-1]["stopped"] == "steps"


def test_dashboard_reads_log_without_agent_columns(tmp_path):
    path = tmp_path / "calls.sqlite3"
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript(OLD_SCHEMA)

    s = dashboard_data(path, "1h", now=NOW)["summary"]

    assert (s["citations_total"], s["patches"], s["stopped_calls"]) == (0, 0, 0)

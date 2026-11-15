"""Tool per glm-5.3-flash con API simulata: agente, verifiche del server, errori, registro."""

import json
import sqlite3

import pytest
from fastmcp.exceptions import ToolError
from helpers import FakeAPI, chat_response, tool_call_response, tool_json

RATE = (
    "def rate(kml):\n"
    "    if kml >= 0.85:\n"
    "        return 'good'\n"
    "    if kml > 0.96:\n"
    "        return 'excellent'\n"
    "    return 'bad'\n"
)


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    (root / "app").mkdir(parents=True)
    (root / "app" / "rate.py").write_text(RATE, encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_rate.py").write_text("from app.rate import rate\n", encoding="utf-8")
    (root / ".env").write_text("TOKEN=sk-abcdefghijklmnopqrstuv\n")
    return root.resolve()


@pytest.fixture
def glm(call, project):
    """glm(tool, args, **settings): chiama un tool con la cartella del progetto consentita."""

    async def _glm(tool, args, **overrides):
        overrides.setdefault("allowed_read_roots", (project.parent,))
        return await call(tool, args, **overrides)

    return _glm


def bodies(api: FakeAPI) -> list[dict]:
    return [json.loads(r.content) for r in api.requests]


def answer(**data) -> object:
    return chat_response(data, model="glm-5.3-flash")


# --- ask_codebase ------------------------------------------------------------------------


async def test_ask_codebase_explores_and_verifies_citations(api, glm, project, tmp_path):
    api.queue(
        tool_call_response(("search_text", {"pattern": "def rate"})),
        tool_call_response(("read_file", {"path": "app/rate.py"})),
        answer(
            answer="rate() non restituisce mai 'excellent'.",
            citations=[
                {"path": "app/rate.py", "line": 2, "quote": "if kml >= 0.85:"},
                {"path": "app/rate.py", "line": 1, "quote": "return 'excellent'"},
                {"path": "app/rate.py", "line": 7, "quote": "raise ValueError(kml)"},
            ],
            open_points=[],
        ),
    )
    log = tmp_path / "calls.sqlite3"

    result = tool_json(
        await glm(
            "ask_codebase",
            {
                "question": "Perché non esce mai excellent?",
                "root": str(project),
                "language": "Italian",
            },
            call_log=log,
        )
    )

    assert result["ok"] and result["answer"].startswith("rate()")
    verified = [(c["verified"], c["line"], c.get("line_corrected")) for c in result["citations"]]
    assert verified == [(True, 2, None), (True, 5, True), (False, 7, None)]
    assert result["stats"]["files_read"] == ["app/rate.py"]
    assert result["stats"]["citations_verified"] == "2/3"
    assert result["stats"]["effort"] == "medium"
    assert result["stats"]["requests"] == 3

    first = bodies(api)[0]
    assert first["model"] == "glm-5.3-flash"
    assert first["reasoning_effort"] == "medium"
    assert first["response_format"]["json_schema"]["name"] == "ask_codebase"
    assert "Write every free-text value in Italian" in first["messages"][1]["content"]

    with sqlite3.connect(log) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM calls").fetchone()
    assert row["tool"] == "ask_codebase" and row["outcome"] == "ok"
    assert (row["effort"], row["steps"], row["tool_calls"], row["files_read"]) == (
        "medium",
        3,
        2,
        1,
    )
    assert (row["citations_total"], row["citations_verified"]) == (3, 2)


async def test_effort_can_be_raised(api, glm, project):
    api.queue(answer(answer="ok", citations=[], open_points=[]))

    result = tool_json(
        await glm("ask_codebase", {"question": "x", "root": str(project), "effort": "high"})
    )

    assert result["stats"]["effort"] == "high"
    assert bodies(api)[0]["reasoning_effort"] == "high"


async def test_root_must_be_an_absolute_existing_folder(glm, tmp_path):
    with pytest.raises(ToolError, match="ASSOLUTO"):
        await glm("ask_codebase", {"question": "x", "root": "proj"})
    with pytest.raises(ToolError, match="non trovata"):
        await glm("ask_codebase", {"question": "x", "root": str(tmp_path / "manca")})


async def test_root_outside_allowed_folders(glm, project, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(ToolError, match="fuori dalle cartelle consentite"):
        await glm(
            "ask_codebase", {"question": "x", "root": str(project)}, allowed_read_roots=(other,)
        )
    with pytest.raises(ToolError, match="disattivata"):
        await glm("ask_codebase", {"question": "x", "root": str(project)}, allowed_read_roots=())


async def test_invalid_answer_after_retry_is_not_invented(api, glm, project, tmp_path):
    api.queue(chat_response("Non so rispondere in JSON"), chat_response('{"answer": 1}'))
    log = tmp_path / "calls.sqlite3"

    result = tool_json(
        await glm("ask_codebase", {"question": "x", "root": str(project)}, call_log=log)
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_output"
    assert "schema" in result["error"]["message"]
    with sqlite3.connect(log) as db:
        assert db.execute("SELECT outcome FROM calls").fetchone()[0] == "invalid_output:schema"


async def test_secrets_in_the_question_are_redacted(api, glm, project):
    api.queue(answer(answer="ok", citations=[], open_points=[]))

    result = tool_json(
        await glm(
            "ask_codebase",
            {"question": "La chiave sk-abcdefghijklmnopqrstuv funziona?", "root": str(project)},
        )
    )

    assert "sk-abcdefghijklmnopqrstuv" not in json.dumps(bodies(api))
    assert "redaction_note" in result


# --- review_changes ----------------------------------------------------------------------

DIFF = """\
diff --git a/app/rate.py b/app/rate.py
--- a/app/rate.py
+++ b/app/rate.py
@@ -1,4 +1,4 @@
 def rate(kml):
-    if kml > 0.96:
-        return 'excellent'
+    if kml >= 0.85:
+        return 'good'
diff --git a/package-lock.json b/package-lock.json
--- a/package-lock.json
+++ b/package-lock.json
@@ -1 +1 @@
-{}
+{"a": 1}
"""


def issue(severity, evidence, line=1, file="app/rate.py"):
    return {
        "file": file,
        "line": line,
        "severity": severity,
        "title": "t",
        "explanation": "e",
        "evidence": evidence,
        "suggestion": "s",
    }


async def test_review_changes_checks_evidence_in_diff_and_files(api, glm, project):
    api.queue(
        tool_call_response(("read_file", {"path": "app/rate.py"})),
        answer(
            summary="Un'inversione dei controlli.",
            issues=[
                issue("low", "raise ValueError(kml)"),
                issue("medium", "return 'bad'", line=9),
                issue("high", "+    if kml >= 0.85:", line=40),
            ],
        ),
    )

    result = tool_json(await glm("review_changes", {"root": str(project), "diff": DIFF}))

    assert [i["severity"] for i in result["issues"]] == ["high", "medium", "low"]
    assert [(i["evidence_found"], i["line"]) for i in result["issues"]] == [
        ("diff", 2),
        ("file", 6),
        (None, 1),
    ]
    assert result["skipped_files"] == ["package-lock.json"]
    assert result["stats"]["citations_verified"] == "2/3"
    first = bodies(api)[0]
    assert first["reasoning_effort"] == "medium"
    assert "package-lock" not in first["messages"][1]["content"]


async def test_review_changes_only_generated_files(glm, project):
    lock_only = DIFF[DIFF.index("diff --git a/package-lock.json") :]
    with pytest.raises(ToolError, match="vuoto"):
        await glm("review_changes", {"root": str(project), "diff": lock_only})


# --- diagnose_error ----------------------------------------------------------------------


async def test_diagnose_error_verifies_nested_evidence(api, glm, project):
    api.queue(
        answer(
            summary="Soglie invertite.",
            hypotheses=[
                {
                    "cause": "controllo >= 0.85 prima di > 0.96",
                    "confidence": "high",
                    "evidence": [
                        {"path": "app/rate.py", "line": 2, "quote": "if kml >= 0.85:"},
                        {"path": "app/nope.py", "line": 1, "quote": "nope nope"},
                    ],
                    "how_to_confirm": "rate(0.97)",
                    "fix": "invertire i controlli",
                }
            ],
            open_points=["nessun test"],
        )
    )

    result = tool_json(
        await glm(
            "diagnose_error",
            {"root": str(project), "error": "AssertionError: 'good' != 'excellent'"},
        )
    )

    evidence = result["hypotheses"][0]["evidence"]
    assert [e["verified"] for e in evidence] == [True, False]
    assert result["stats"]["citations_verified"] == "1/2"


# --- propose_change ----------------------------------------------------------------------

PATCH = """\
--- a/app/rate.py
+++ b/app/rate.py
@@ -1,5 +1,5 @@
 def rate(kml):
-    if kml >= 0.85:
-        return 'good'
     if kml > 0.96:
         return 'excellent'
+    if kml >= 0.85:
+        return 'good'
"""


def proposal(patch):
    return answer(patch=patch, rationale="r", risks=[], tests_to_run=["pytest"], citations=[])


async def test_propose_change_returns_a_checked_patch(api, glm, project):
    api.queue(proposal(PATCH))

    result = tool_json(await glm("propose_change", {"task": "fix", "root": str(project)}))

    assert result["applies"] is True
    assert "@@ -1,6 +1,6 @@" in result["patch"]  # contesto finale aggiunto dal server
    assert result["files"][0]["status"] == "modified"
    assert result["tests_to_run"] == ["pytest"]
    assert (project / "app" / "rate.py").read_text() == RATE  # nessuna scrittura


async def test_patch_that_does_not_apply_goes_back_to_the_model(api, glm, project):
    wrong = PATCH.replace("-    if kml >= 0.85:", "-    if kml >= 0.80:")
    api.queue(proposal(wrong), proposal(PATCH))

    result = tool_json(await glm("propose_change", {"task": "fix", "root": str(project)}))

    assert result["applies"] is True
    retry = bodies(api)[1]["messages"][-1]["content"]
    assert "the patch does not apply" in retry and "app/rate.py" in retry


async def test_patch_still_wrong_is_returned_with_problems(api, glm, project, tmp_path):
    wrong = PATCH.replace("-    if kml >= 0.85:", "-    if kml >= 0.80:")
    api.queue(proposal(wrong), proposal(wrong))
    log = tmp_path / "calls.sqlite3"

    result = tool_json(
        await glm("propose_change", {"task": "fix", "root": str(project)}, call_log=log)
    )

    assert result["applies"] is False
    assert result["patch"] == wrong
    assert "not in the file" in result["files"][0]["problems"][0]
    with sqlite3.connect(log) as db:
        assert db.execute("SELECT applies FROM calls").fetchone()[0] == 0


# --- write_tests -------------------------------------------------------------------------


def written_tests(test_path, mode):
    return answer(
        test_path=test_path,
        mode=mode,
        content="def test_rate():\n    assert rate(0.97) == 'excellent'\n",
        framework="pytest",
        run_command="pytest tests/test_rate.py",
        cases=[{"name": "test_rate", "checks": "excellent"}],
        assumptions=[],
        citations=[],
    )


async def test_write_tests_flags_an_existing_file(api, glm, project):
    api.queue(written_tests("tests/test_rate.py", "new_file"))

    result = tool_json(
        await glm("write_tests", {"target": "app/rate.py", "root": str(project), "symbol": "rate"})
    )

    assert result["test_path_exists"] is True
    assert "non sovrascriverlo" in result["warning"]
    assert "'rate' in app/rate.py" in bodies(api)[0]["messages"][1]["content"]


async def test_write_tests_new_file(api, glm, project):
    api.queue(written_tests("tests/test_rate_new.py", "new_file"))

    result = tool_json(await glm("write_tests", {"target": "app/rate.py", "root": str(project)}))

    assert result["test_path_exists"] is False and "warning" not in result


async def test_write_tests_target_must_exist(glm, project):
    with pytest.raises(ToolError, match="target"):
        await glm("write_tests", {"target": "app/missing.py", "root": str(project)})


# --- critique_plan -----------------------------------------------------------------------

CRITIQUE = {
    "verdict": "needs_changes",
    "summary": "s",
    "issues": [{"issue": "i", "severity": "high", "suggestion": "s"}],
    "missing_cases": [],
    "alternatives": [],
    "questions": [],
    "citations": [],
}


async def test_critique_plan_without_root_uses_no_functions(api, glm):
    api.queue(answer(**CRITIQUE))

    result = tool_json(await glm("critique_plan", {"plan": "Riscrivere tutto in Rust."}))

    assert result["verdict"] == "needs_changes"
    body = bodies(api)[0]
    assert "tools" not in body
    assert "no access to the code" in body["messages"][1]["content"]
    assert "function_calls" not in result["stats"]


async def test_critique_plan_with_root_explores(api, glm, project):
    api.queue(tool_call_response(("list_dir", {})), answer(**CRITIQUE))

    result = tool_json(await glm("critique_plan", {"plan": "p", "root": str(project)}))

    assert result["stats"]["function_calls"] == 1
    assert "tools" in bodies(api)[0]


# --- analyze_documents -------------------------------------------------------------------


async def test_analyze_documents_verifies_quotes(api, glm, tmp_path):
    a = tmp_path / "contratto.txt"
    a.write_text(
        "Il canone è di 100 euro al mese.\nIl contratto dura due anni.\n", encoding="utf-8"
    )
    b = tmp_path / "offerta.txt"
    b.write_text("Canone: 120 euro.\n", encoding="utf-8")
    api.queue(
        answer(
            answer="Il contratto costa meno.",
            citations=[
                {"document": 1, "quote": "Il contratto dura due anni."},
                {"document": 2, "quote": "Canone: 150 euro."},
                {"document": 3, "quote": "x"},
            ],
            not_found=[],
        )
    )

    result = tool_json(
        await glm("analyze_documents", {"question": "Confronta", "paths": [str(a), str(b)]})
    )

    c = result["citations"]
    assert (c[0]["verified"], c[0]["name"], c[0]["line"]) == (True, "contratto.txt", 2)
    assert c[1]["verified"] is False and c[1]["problem"].startswith("quote not found")
    assert c[2]["problem"] == "unknown document number"
    assert result["documents"] == ["contratto.txt", "offerta.txt"]
    body = bodies(api)[0]
    assert "tools" not in body
    assert "DOCUMENT 2: offerta.txt" in body["messages"][1]["content"]


async def test_analyze_documents_needs_one_source(glm, tmp_path):
    with pytest.raises(ToolError, match="'text' oppure 'paths'"):
        await glm("analyze_documents", {"question": "x"})


# --- extract_structured_data -------------------------------------------------------------

SCHEMA = {
    "type": "object",
    "properties": {
        "numero": {"type": "string"},
        "totale": {"type": ["number", "null"]},
        "righe": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["numero", "totale"],
    "additionalProperties": False,
}


async def test_extract_validates_against_the_schema(api, glm):
    api.queue(answer(numero="2026-17", totale=None, righe=["a", "b"]))

    result = tool_json(
        await glm("extract_structured_data", {"json_schema": SCHEMA, "text": "Fattura 2026-17"})
    )

    assert result["data"] == {"numero": "2026-17", "totale": None, "righe": ["a", "b"]}
    body = bodies(api)[0]
    assert body["response_format"]["json_schema"]["schema"] == SCHEMA
    assert body["reasoning_effort"] == "low"


async def test_extract_retries_once_then_gives_up(api, glm):
    api.queue(answer(numero=17), answer(numero="17", totale=3.5))
    result = tool_json(
        await glm("extract_structured_data", {"json_schema": SCHEMA, "text": "Fattura 17"})
    )
    assert result["data"] == {"numero": "17", "totale": 3.5}
    assert "not valid" in bodies(api)[1]["messages"][-1]["content"]

    api.queue(answer(numero=17), answer(numero=18))
    result = tool_json(
        await glm("extract_structured_data", {"json_schema": SCHEMA, "text": "Fattura 17"})
    )
    assert result["ok"] is False and "numero" in result["error"]["message"]


async def test_extract_rejects_an_invalid_schema(glm):
    with pytest.raises(ToolError, match="json_schema"):
        await glm("extract_structured_data", {"json_schema": {"type": "array"}, "text": "x"})


async def test_truncated_answer_is_an_error(api, glm):
    api.queue(chat_response("", finish_reason="length"))
    with pytest.raises(ToolError, match="troncata"):
        await glm("extract_structured_data", {"json_schema": SCHEMA, "text": "x"})

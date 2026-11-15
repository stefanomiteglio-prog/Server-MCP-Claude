"""Verifiche lato server: citazioni, patch in memoria, schema JSON."""

import shutil
import subprocess

import pytest

from local_llm_mcp.citations import citation_stats, verify_citations
from local_llm_mcp.patches import check_patch
from local_llm_mcp.safety import InputError
from local_llm_mcp.schemas import check_schema, json_validator, parse_json_answer

RATE = "def rate(kml):\n    if kml >= 0.85:\n        return 'good'\n    if kml > 0.96:\n        return 'excellent'\n    return 'bad'\n"
FILES = {"src/rate.py": RATE}


def lookup(path):
    path = path.removeprefix("./")
    return (path, FILES[path]) if path in FILES else None


# --- citazioni ---------------------------------------------------------------------------


def test_exact_citation_is_verified():
    [c] = verify_citations([{"path": "src/rate.py", "line": 2, "quote": "if kml >= 0.85:"}], lookup)
    assert c == {"path": "src/rate.py", "line": 2, "quote": "if kml >= 0.85:", "verified": True}


def test_wrong_line_is_corrected():
    [c] = verify_citations([{"path": "./src/rate.py", "line": 9, "quote": "return 'bad'"}], lookup)
    assert (c["verified"], c["line"], c["line_corrected"], c["path"]) == (
        True,
        6,
        True,
        "src/rate.py",
    )


def test_multiline_quote_with_different_spacing_and_read_file_prefixes():
    quote = "4:     if kml > 0.96:\n5:         return   'excellent'"
    [c] = verify_citations([{"path": "src/rate.py", "line": 4, "quote": quote}], lookup)
    assert c["verified"] and (c["line"], c["end_line"]) == (4, 5)
    assert "line_corrected" not in c


def test_comment_markers_are_ignored():
    dart = "  /// Finché questo flag non è impostato,\n  /// i record vengono ricaricati.\nint x = 1;\n"

    def files(path):
        return (path, dart)

    quote = "// Finché questo flag non è impostato,\n// i record vengono ricaricati."
    [ok, wrong] = verify_citations(
        [
            {"path": "lib/a.dart", "line": 1, "quote": quote},
            {"path": "lib/a.dart", "line": 1, "quote": "// i record vengono eliminati."},
        ],
        files,
    )
    assert ok["verified"] and (ok["line"], ok["end_line"]) == (1, 2)
    assert not wrong["verified"]


def test_nearest_occurrence_wins():
    [c] = verify_citations([{"path": "src/rate.py", "line": 5, "quote": "if kml"}], lookup)
    assert c["line"] == 4 and c["line_corrected"]


@pytest.mark.parametrize(
    ("item", "problem"),
    [
        ({"path": "src/rate.py", "line": 2, "quote": "if kml > 0.5:"}, "not found in the file"),
        ({"path": "src/other.py", "line": 1, "quote": "def rate"}, "file not found"),
        ({"path": "src/rate.py", "line": 1, "quote": "()"}, "too short"),
        ({"path": "src/rate.py", "line": "x"}, "too short"),
    ],
)
def test_unverified_citations(item, problem):
    [c] = verify_citations([item], lookup)
    assert not c["verified"] and problem in c["problem"]


def test_citation_list_robustness_and_stats():
    out = verify_citations(
        ["not a dict", {"path": "src/rate.py", "line": "2", "quote": "kml >= 0.85"}, {}], lookup
    )
    assert [c["verified"] for c in out] == [True, False]
    assert citation_stats(out) == {"citations_total": 2, "citations_verified": 1}
    assert verify_citations(None, lookup) == []


# --- patch -------------------------------------------------------------------------------

GOOD = """\
--- a/src/rate.py
+++ b/src/rate.py
@@ -1,5 +1,5 @@
 def rate(kml):
-    if kml >= 0.85:
-        return 'good'
     if kml > 0.96:
         return 'excellent'
+    if kml >= 0.85:
+        return 'good'
"""


def git_apply_check(patch: str, tmp_path) -> bool:
    if shutil.which("git") is None:
        pytest.skip("git non disponibile")
    for name, text in FILES.items():
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode())
    (tmp_path / "p.diff").write_bytes(patch.encode())
    done = subprocess.run(
        ["git", "apply", "--check", "p.diff"],  # noqa: S607
        cwd=tmp_path,
        capture_output=True,
        check=False,
    )
    return done.returncode == 0


def test_good_patch_applies_and_gets_trailing_context(tmp_path):
    # Senza contesto finale git apply vuole l'hunk alla fine del file e rifiuta la diff.
    assert not git_apply_check(GOOD, tmp_path)

    result = check_patch(GOOD, lookup)

    assert result["applies"]
    assert result["files"] == [
        {
            "path": "src/rate.py",
            "status": "modified",
            "hunks": [{"hunk": 1, "applies": True, "context_added": 1}],
            "problems": [],
        }
    ]
    assert "@@ -1,6 +1,6 @@" in result["patch"]
    assert result["patch"].endswith("+        return 'good'\n     return 'bad'\n")
    assert git_apply_check(result["patch"], tmp_path)


def test_wrong_header_counts_and_lines_are_fixed(tmp_path):
    bad = GOOD.replace("@@ -1,5 +1,5 @@", "@@ -3,9 +3,2 @@")

    result = check_patch(bad, lookup)

    assert result["applies"]
    assert result["files"][0]["hunks"] == [
        {"hunk": 1, "applies": True, "offset": -2, "context_added": 1}
    ]
    assert "@@ -1,6 +1,6 @@" in result["patch"]
    assert git_apply_check(result["patch"], tmp_path)


def test_trailing_spaces_in_the_file_are_kept(tmp_path):
    FILES["src/sp.py"] = "a = 1   \nb = 2\n"
    try:
        diff = "--- a/src/sp.py\n+++ b/src/sp.py\n@@ -1,2 +1,2 @@\n a = 1\n-b = 2\n+b = 3\n"
        result = check_patch(diff, lookup)
        assert result["applies"]
        assert " a = 1   \n" in result["patch"]
        assert git_apply_check(result["patch"], tmp_path)
    finally:
        del FILES["src/sp.py"]


def test_blank_context_line_without_space_and_crlf(tmp_path):
    FILES["src/blank.py"] = "a = 1\n\nb = 2\n"
    try:
        diff = "--- a/src/blank.py\r\n+++ b/src/blank.py\r\n@@ -1,3 +1,3 @@\r\n a = 1\r\n\r\n-b = 2\r\n+b = 3\r\n"
        result = check_patch(diff, lookup)
        assert result["applies"], result
        assert git_apply_check(result["patch"], tmp_path)
    finally:
        del FILES["src/blank.py"]


def test_patch_with_lines_not_in_the_file():
    bad = GOOD.replace("-    if kml >= 0.85:", "-    if kml >= 0.80:")
    result = check_patch(bad, lookup)

    assert not result["applies"]
    assert result["files"][0]["hunks"] == [{"hunk": 1, "applies": False}]
    assert "not in the file" in result["files"][0]["problems"][0]


def test_two_hunks_apply_in_order(tmp_path):
    diff = """\
--- a/src/rate.py
+++ b/src/rate.py
@@ -1,2 +1,3 @@
+# rating
 def rate(kml):
     if kml >= 0.85:
@@ -6,1 +7,1 @@
-    return 'bad'
+    return 'poor'
"""
    result = check_patch(diff, lookup)
    assert result["applies"], result
    assert git_apply_check(result["patch"], tmp_path)


def test_new_file_and_missing_file(tmp_path):
    created = "--- /dev/null\n+++ b/src/new.py\n@@ -0,0 +1,2 @@\n+x = 1\n+y = 2\n"
    result = check_patch(created, lookup)
    assert result["applies"] and result["files"][0]["status"] == "created"
    assert git_apply_check(result["patch"], tmp_path)

    exists = created.replace("src/new.py", "src/rate.py")
    assert check_patch(exists, lookup)["files"][0]["problems"] == ["the file already exists"]

    missing = GOOD.replace("src/rate.py", "src/missing.py")
    assert check_patch(missing, lookup)["files"][0]["problems"] == [
        "file not found or not readable"
    ]


def test_not_a_diff():
    assert check_patch("ecco la patch: cambia la riga 2", lookup)["error"] == "no file in the diff"
    assert "no hunk" in check_patch("--- a/x\n+++ b/x\n", lookup)["error"]


# --- schema ------------------------------------------------------------------------------

SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}, "n": {"type": "integer"}},
    "required": ["answer"],
    "additionalProperties": False,
}


def test_parse_json_answer():
    assert parse_json_answer('```json\n{"answer": "ok"}\n```', SCHEMA) == ({"answer": "ok"}, None)
    assert parse_json_answer("non JSON", SCHEMA) == (None, "not valid JSON")
    value, error = parse_json_answer('{"n": "tre", "extra": 1}', SCHEMA)
    assert value is None
    assert "'answer' is a required property" in error
    assert "n: 'tre' is not of type 'integer'" in error
    assert json_validator(SCHEMA)('{"answer": "x"}') is None


@pytest.mark.parametrize(
    "schema", [None, {"type": "array"}, {"type": "object", "properties": {"a": {"type": "nope"}}}]
)
def test_invalid_user_schema(schema):
    with pytest.raises(InputError, match="json_schema"):
        check_schema(schema)

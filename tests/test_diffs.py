from local_llm_mcp.diffs import (
    code_text,
    diff_files,
    evidence_line,
    new_file_lines,
    quoted_from,
    skip_generated_files,
)

APP = """diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -10,4 +10,5 @@ def main():
 def total(items):
-    return sum(items)
+    values = [i.price for i in items]
+    return sum(values)

 print(total([]))
"""

LOCK = """diff --git a/uv.lock b/uv.lock
--- a/uv.lock
+++ b/uv.lock
@@ -1 +1 @@
-version = 1
+version = 2
"""

DELETED = """diff --git a/old.py b/old.py
--- a/old.py
+++ /dev/null
@@ -1 +0,0 @@
-x = 1
"""


def test_new_file_lines_follow_hunk_headers():
    lines = new_file_lines(APP)

    assert lines == {
        "src/app.py": [
            (10, "def total(items):"),
            (11, "    values = [i.price for i in items]"),
            (12, "    return sum(values)"),
            (13, ""),
            (14, "print(total([]))"),
        ]
    }


def test_deleted_file_has_no_new_lines():
    assert new_file_lines(DELETED) == {}


def test_evidence_line_finds_unique_quoted_line():
    lines = new_file_lines(APP)["src/app.py"]

    assert evidence_line("+ return sum(values)", lines) == 12
    # Le righe rimosse non sono nel nuovo file: si passa alla riga successiva.
    assert evidence_line("-    return sum(items)\n+    return sum(values)", lines) == 12
    assert evidence_line("`non presente`", lines) is None


def test_quoted_from_ignores_markers_spaces_and_backticks():
    code = code_text(APP)

    assert quoted_from("`values = [i.price   for i in items]`", code)
    assert quoted_from("-    return sum(items)\n+    values = [i.price for i in items]", code)
    assert not quoted_from("return sum(prices)", code)
    assert not quoted_from("...", code)


def test_skip_generated_files_drops_lockfiles():
    kept, skipped = skip_generated_files(APP + LOCK)

    assert skipped == ["uv.lock"]
    assert kept == APP


def test_diff_files_in_order_without_duplicates():
    assert diff_files(APP + LOCK + DELETED + APP) == ["src/app.py", "uv.lock", "old.py"]
    rename = "diff --git a/a.png b/b.png\nBinary files differ\n"
    assert diff_files(rename) == ["b.png"]

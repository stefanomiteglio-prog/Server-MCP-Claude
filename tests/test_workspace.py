"""Funzioni in sola lettura offerte al modello: confini, esclusioni, budget, oscuramento."""

import pytest

from local_llm_mcp.workspace import READ_MAX_LINES, Workspace


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    (root / "src" / "app").mkdir(parents=True)
    (root / "src" / "app" / "rate.py").write_text(
        "def rate(kml):\n    if kml >= 0.85:\n        return 'good'\n    return 'bad'\n",
        encoding="utf-8",
    )
    (root / "src" / "app" / "trip.py").write_text(
        "class Trip:\n    async def cost(self, km):\n        return km * 2\n", encoding="utf-8"
    )
    (root / "node_modules" / "lib").mkdir(parents=True)
    (root / "node_modules" / "lib" / "index.js").write_text("def rate(): pass\n")
    (root / ".env").write_text("LOCAL_LLM_API_KEY=sk-abcdefghijklmnopqrstuv\n")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("[core]\n")
    (root / "settings.py").write_text('API_KEY = "sk-abcdefghijklmnopqrstuv"\n')
    (tmp_path / "outside.txt").write_text("segreto fuori dal progetto\n")
    return root.resolve()


def ws(root, budget=100_000) -> Workspace:
    return Workspace(root=root, max_read_chars=budget, max_file_chars=50_000)


def test_list_dir_hides_dependencies_and_sensitive_files(project):
    out = ws(project).call("list_dir", {})

    assert "src/" in out and "rate.py" in out and "settings.py" in out
    assert "node_modules" not in out
    assert ".env" not in out
    assert ".git" not in out


def test_read_file_numbers_lines_and_tracks_files(project):
    w = ws(project)
    out = w.call("read_file", {"path": "src/app/rate.py", "start_line": 2, "end_line": 3})

    assert out.splitlines()[0] == "src/app/rate.py (lines 2-3 of 4 — 1 more lines after 3)"
    assert "2:     if kml >= 0.85:" in out
    assert "def rate" not in out
    assert w.files_read == {"src/app/rate.py"}
    assert "src/app/rate.py" in w.texts


def test_read_file_caps_lines_per_call(project):
    big = project / "big.txt"
    big.write_text("\n".join(f"riga {i}" for i in range(1, 1001)))

    out = ws(project).call("read_file", {"path": "big.txt"})

    assert f"lines 1-{READ_MAX_LINES} of 1000" in out
    assert f"riga {READ_MAX_LINES + 1}" not in out


@pytest.mark.parametrize("path", ["../outside.txt", "src/../../outside.txt"])
def test_paths_outside_the_root_are_refused(project, path):
    out = ws(project).call("read_file", {"path": path})
    assert out.startswith("ERROR:")
    assert "segreto" not in out


def test_absolute_path_outside_is_refused(project):
    out = ws(project).call("read_file", {"path": str(project.parent / "outside.txt")})
    assert out.startswith("ERROR: outside the project")


@pytest.mark.parametrize("path", [".env", ".git/config"])
def test_sensitive_files_are_refused(project, path):
    out = ws(project).call("read_file", {"path": path})
    assert out.startswith("ERROR: excluded for safety")


def test_secrets_are_redacted(project):
    w = ws(project)
    out = w.call("read_file", {"path": "settings.py"})

    assert "sk-abcdefghijklmnopqrstuv" not in out
    assert "[REDACTED:" in out
    assert w.redactions


def test_search_text_skips_hidden_folders(project):
    out = ws(project).call("search_text", {"pattern": r"def rate"})

    assert out == "src/app/rate.py:1: def rate(kml):"


def test_search_text_glob_and_case(project):
    w = ws(project)
    assert w.call("search_text", {"pattern": "TRIP", "ignore_case": True, "glob": "*.py"}) == (
        "src/app/trip.py:1: class Trip:"
    )
    assert w.call("search_text", {"pattern": "TRIP"}) == "no matches"


def test_search_text_bad_regex_is_an_error_for_the_model(project):
    assert ws(project).call("search_text", {"pattern": "("}).startswith("ERROR:")


def test_file_outline(project):
    out = ws(project).call("file_outline", {"path": "src/app/trip.py"})
    assert "1: class Trip:" in out
    assert "2: async def cost(self, km):" in out


def test_unknown_function_and_bad_arguments(project):
    w = ws(project)
    assert w.call("delete_file", {"path": "x"}) == "ERROR: unknown function 'delete_file'"
    assert w.call("read_file", {"file": "x"}).startswith("ERROR: invalid arguments")
    assert w.call("read_file", {"path": "src/app/rate.py", "start_line": "abc"}).startswith(
        "ERROR:"
    )


def test_null_strings_count_as_missing(project):
    out = ws(project).call("read_file", {"path": "src/app/rate.py", "end_line": "null"})
    assert "lines 1-4 of 4" in out


def test_reading_budget(project):
    w = ws(project, budget=60)
    first = w.call("read_file", {"path": "src/app/rate.py"})

    assert first.endswith("reading budget exhausted. Answer now.")
    assert w.read_chars == 60
    assert w.call("list_dir", {}).startswith("ERROR: reading budget exhausted")


def test_text_of_does_not_use_the_budget(project):
    w = ws(project, budget=1000)

    rel, text = w.text_of("./src/app/rate.py")

    assert rel == "src/app/rate.py" and text.startswith("def rate")
    assert w.read_chars == 0 and w.files_read == set()
    assert w.text_of(".env") is None
    assert w.text_of("missing.py") is None

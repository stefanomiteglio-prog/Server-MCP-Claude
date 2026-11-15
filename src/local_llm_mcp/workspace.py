"""Funzioni in sola lettura che l'agente offre al modello: elenco, lettura, ricerca, struttura.

Tutto resta dentro `root` (una cartella dentro LOCAL_LLM_ALLOWED_READ_ROOTS). I file sensibili di
`files.py` sono invisibili, i segreti riconosciuti vengono oscurati e i caratteri restituiti al
modello si contano in un budget unico per l'intero compito. Gli errori tornano al modello come
testo ("ERROR: ..."), così può correggersi: non interrompono il compito.
"""

from __future__ import annotations

import fnmatch
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .files import read_text_file, sensitive_reason
from .safety import InputError, redact_secrets

# Cartelle di dipendenze, build e cache: rumore per l'agente.
SKIP_DIRS = {
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    "build",
    "dist",
    "target",
    ".dart_tool",
    ".gradle",
    ".idea",
    ".vs",
    ".vscode",
    ".next",
    ".nuxt",
    "coverage",
    "vendor",
    "Pods",
    ".local",
}

LIST_MAX_ENTRIES = 300
READ_MAX_LINES = 400
SEARCH_MAX_RESULTS = 60
SEARCH_MAX_FILE_BYTES = 1_000_000
LINE_PREVIEW_CHARS = 200

# Definizioni riconosciute da file_outline: classi, funzioni, metodi, tipi nei linguaggi comuni.
_OUTLINE = re.compile(
    r"""^\s*(?:
        (?:export\s+)?(?:default\s+)?(?:abstract\s+|final\s+|sealed\s+|data\s+|static\s+)*
        (?:async\s+)?(?:def|class|function|func|fn|interface|enum|struct|trait|impl|mixin|extension|
           type|module|namespace|object|record)\b
      | (?:(?:public|private|protected|internal|static|final|abstract|override|virtual|async|
           synchronized|readonly)\s+)+[\w<>\[\],.?\s]*?\w+\s*\(
      | (?:const|let|var)\s+\w+\s*=\s*(?:async\s*)?(?:\([^)]*\)|\w+)\s*=>
      | [A-Z]\w*(?:<[^>]*>)?\??\s+\w+\s*\([^;]*\)\s*(?:async\s*)?\{?\s*$
    )""",
    re.X,
)


@dataclass
class Workspace:
    root: Path
    max_read_chars: int
    max_file_chars: int
    read_chars: int = 0
    files_read: set[str] = field(default_factory=set)
    redactions: Counter[str] = field(default_factory=Counter)
    calls: Counter[str] = field(default_factory=Counter)
    # Testo (già oscurato) dei file letti: serve a verificare le citazioni del modello.
    texts: dict[str, str] = field(default_factory=dict)

    @property
    def budget_left(self) -> int:
        return max(0, self.max_read_chars - self.read_chars)

    # --- interfaccia per l'agente -------------------------------------------------------

    def tool_specs(self) -> list[dict[str, Any]]:
        return [
            _spec(
                "list_dir",
                "List files and folders under a directory of the project, with sizes in bytes. "
                "Dependency, build and cache folders are skipped. Start here to find your way.",
                {
                    "path": {"type": "string", "description": "Relative path; '.' is the root."},
                    "depth": {
                        "type": "integer",
                        "description": "Levels to descend, 1-6 (default 3).",
                    },
                },
                [],
            ),
            _spec(
                "read_file",
                f"Read a text file with numbered lines (at most {READ_MAX_LINES} lines per call). "
                "Read only the ranges you need: use search_text or file_outline first on big files.",
                {
                    "path": {"type": "string", "description": "Relative path of the file."},
                    "start_line": {"type": "integer", "description": "First line (default 1)."},
                    "end_line": {"type": "integer", "description": "Last line, inclusive."},
                },
                ["path"],
            ),
            _spec(
                "search_text",
                "Search a regular expression (Python syntax) in the text files of the project. "
                f"Returns up to {SEARCH_MAX_RESULTS} matches as 'path:line: text'.",
                {
                    "pattern": {"type": "string", "description": "Regular expression."},
                    "path": {
                        "type": "string",
                        "description": "Folder or file to search (default '.').",
                    },
                    "glob": {"type": "string", "description": "File name filter, e.g. '*.py'."},
                    "ignore_case": {"type": "boolean", "description": "Default false."},
                },
                ["pattern"],
            ),
            _spec(
                "file_outline",
                "List the definitions (classes, functions, methods, types) of a file with their "
                "line numbers, without reading the whole file.",
                {"path": {"type": "string", "description": "Relative path of the file."}},
                ["path"],
            ),
        ]

    def call(self, name: str, args: dict[str, Any]) -> str:
        """Esegue una funzione e restituisce il testo per il modello (mai un'eccezione)."""
        self.calls[name] += 1
        handler = {
            "list_dir": self.list_dir,
            "read_file": self.read_file,
            "search_text": self.search_text,
            "file_outline": self.file_outline,
        }.get(name)
        if handler is None:
            return f"ERROR: unknown function '{name}'"
        if self.budget_left == 0:
            return "ERROR: reading budget exhausted. Do not call more functions: answer now."
        try:
            text = handler(**_clean_args(args))
        except TypeError as exc:
            return f"ERROR: invalid arguments for {name}: {exc}"
        except (InputError, OSError, re.error, ValueError) as exc:
            return f"ERROR: {exc}"
        return self._charge(text)

    # --- funzioni ------------------------------------------------------------------------

    def list_dir(self, path: str = ".", depth: int = 3) -> str:
        base = self._resolve(path)
        if not base.is_dir():
            raise InputError(f"not a directory: {path}")
        depth = min(max(int(depth), 1), 6)
        lines: list[str] = []
        truncated = False

        def walk(folder: Path, level: int) -> None:
            nonlocal truncated
            try:
                entries = sorted(folder.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
            except OSError:
                return
            for entry in entries:
                if len(lines) >= LIST_MAX_ENTRIES:
                    truncated = True
                    return
                if self._hidden(entry):
                    continue
                indent = "  " * level
                if entry.is_dir():
                    lines.append(f"{indent}{entry.name}/")
                    if level + 1 < depth:
                        walk(entry, level + 1)
                elif entry.is_file():
                    lines.append(f"{indent}{entry.name} ({entry.stat().st_size} B)")

        walk(base, 0)
        if truncated:
            lines.append(f"... truncated at {LIST_MAX_ENTRIES} entries: list a subfolder")
        return f"{self._rel(base)}/\n" + ("\n".join(lines) or "(empty)")

    def read_file(self, path: str, start_line: int = 1, end_line: int | None = None) -> str:
        rel, text = self._text(path)
        lines = text.splitlines()
        total = len(lines)
        start = max(int(start_line), 1)
        end = total if end_line is None else min(int(end_line), total)
        end = min(end, start + READ_MAX_LINES - 1)
        if start > total:
            return f"{rel}: the file has {total} lines"
        self.files_read.add(rel)
        width = len(str(end))
        body = "\n".join(f"{i:>{width}}: {lines[i - 1]}" for i in range(start, end + 1))
        more = f" — {total - end} more lines after {end}" if end < total else ""
        return f"{rel} (lines {start}-{end} of {total}{more})\n{body}"

    def search_text(
        self,
        pattern: str,
        path: str = ".",
        glob: str | None = None,
        ignore_case: bool = False,
    ) -> str:
        regex = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
        base = self._resolve(path)
        files = [base] if base.is_file() else self._files(base)
        hits: list[str] = []
        for file in files:
            if glob and not fnmatch.fnmatch(file.name, glob):
                continue
            if file.stat().st_size > SEARCH_MAX_FILE_BYTES:
                continue
            try:
                rel, text = self._text(str(file))
            except InputError:
                continue  # binari, sensibili, illeggibili: si saltano in silenzio
            for number, line in enumerate(text.splitlines(), 1):
                if regex.search(line):
                    hits.append(f"{rel}:{number}: {line.strip()[:LINE_PREVIEW_CHARS]}")
                    if len(hits) >= SEARCH_MAX_RESULTS:
                        hits.append(
                            f"... stopped at {SEARCH_MAX_RESULTS} matches: narrow the search"
                        )
                        return "\n".join(hits)
        return "\n".join(hits) or "no matches"

    def file_outline(self, path: str) -> str:
        rel, text = self._text(path)
        found = [
            f"{number}: {line.strip()[:LINE_PREVIEW_CHARS]}"
            for number, line in enumerate(text.splitlines(), 1)
            if _OUTLINE.match(line)
        ]
        total = len(text.splitlines())
        if not found:
            return f"{rel} ({total} lines): no definitions recognised, use read_file"
        return f"{rel} ({total} lines)\n" + "\n".join(found[:LIST_MAX_ENTRIES])

    def text_of(self, path: str) -> tuple[str, str] | None:
        """(percorso relativo, testo oscurato) di un file del progetto, senza consumare budget:
        serve al server per verificare citazioni e patch. None se il file non è leggibile."""
        try:
            return self._text(path)
        except (InputError, OSError):
            return None

    # --- supporto -----------------------------------------------------------------------

    def _resolve(self, raw: str) -> Path:
        raw = (raw or ".").strip()
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = self.root / raw.lstrip("/\\")
        try:
            resolved = candidate.resolve(strict=True)
        except FileNotFoundError:
            raise InputError(f"not found: {raw}") from None
        if not resolved.is_relative_to(self.root):
            raise InputError(f"outside the project: {raw}")
        # Le cartelle di SKIP_DIRS sono solo nascoste negli elenchi: un file lì dentro si può
        # leggere se il modello lo chiede esplicitamente.
        reason = sensitive_reason(candidate) or sensitive_reason(resolved)
        if reason:
            raise InputError(f"excluded for safety ({reason}): {raw}")
        return resolved

    def _text(self, raw: str) -> tuple[str, str]:
        resolved = self._resolve(raw)
        if not resolved.is_file():
            raise InputError(f"not a file: {raw}")
        rel = self._rel(resolved)
        if rel not in self.texts:
            text = read_text_file(str(resolved), (self.root,), self.max_file_chars)
            text, found = redact_secrets(text)
            self.redactions.update(found)
            self.texts[rel] = text
        return rel, self.texts[rel]

    def _files(self, base: Path) -> list[Path]:
        out: list[Path] = []
        stack = [base]
        while stack:
            folder = stack.pop()
            try:
                entries = sorted(folder.iterdir(), key=lambda p: p.name.lower(), reverse=True)
            except OSError:
                continue
            for entry in entries:
                if self._hidden(entry):
                    continue
                if entry.is_dir():
                    stack.append(entry)
                elif entry.is_file():
                    out.append(entry)
        return sorted(out, key=lambda p: self._rel(p))

    def _hidden(self, entry: Path) -> bool:
        if entry.is_symlink():
            try:
                if not entry.resolve().is_relative_to(self.root):
                    return True
            except OSError:
                return True
        if entry.is_dir():
            return entry.name in SKIP_DIRS or sensitive_reason(entry / "x") is not None
        return sensitive_reason(entry) is not None

    def _rel(self, path: Path) -> str:
        rel = path.relative_to(self.root).as_posix()
        return rel if rel != "." else "."

    def _charge(self, text: str) -> str:
        left = self.budget_left
        if len(text) > left:
            text = text[:left] + "\n... cut: reading budget exhausted. Answer now."
        self.read_chars += min(len(text), left)
        return text


def _spec(name: str, description: str, props: dict[str, Any], required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": props, "required": required},
        },
    }


def _clean_args(args: dict[str, Any]) -> dict[str, Any]:
    """Il modello a volte scrive "null" o "" nei campi facoltativi (documentato dall'API)."""
    return {k: v for k, v in args.items() if v is not None and v not in ("null", "")}

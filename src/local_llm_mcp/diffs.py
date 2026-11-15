"""Analisi di diff unified: file toccati, righe del nuovo file, citazioni, file generati.

Usato dalla review delle modifiche; non conosce né MCP né l'API.
"""

from __future__ import annotations

import re
from itertools import pairwise

GIT_FILE = re.compile(r"^diff --git ", re.M)
PLAIN_FILE = re.compile(r"^--- .*\n\+\+\+ ", re.M)
HUNK = re.compile(r"^@@ ", re.M)
HUNK_HEADER = re.compile(r"^@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
# Lockfile e file generati: non si rivedono e in una diff possono occupare quasi tutto lo spazio.
GENERATED_NAMES = {
    "package-lock.json",
    "npm-shrinkwrap.json",
    "pnpm-lock.yaml",
    "packages.lock.json",
    "go.sum",
}
GENERATED_SUFFIXES = (".lock", ".min.js", ".min.css", ".g.dart", ".freezed.dart")
FILE_PATHS = re.compile(r"^--- (.+?)(?:\t.*)?\n\+\+\+ (.+?)(?:\t.*)?$", re.M)
GIT_PATH = re.compile(r"^diff --git a/.+? b/(.+)$", re.M)


def strip_ab(path: str) -> str:
    return re.sub(r"^[ab]/", "", path.strip())


def new_file_lines(diff: str) -> dict[str, list[tuple[int, str]]]:
    """Per ogni file della diff, le righe del nuovo file presenti negli hunk (contesto e righe
    aggiunte) con il loro numero, ricavato dagli header @@."""
    files: dict[str, list[tuple[int, str]]] = {}
    current: list[tuple[int, str]] | None = None
    number = old_left = new_left = 0
    for line in diff.splitlines():
        if old_left > 0 or new_left > 0:
            tag = line[:1]
            if tag == "\\":  # "\ No newline at end of file"
                continue
            if tag in ("", " ", "+"):  # una riga di contesto vuota può aver perso lo spazio
                if current is not None:
                    current.append((number, line[1:]))
                number += 1
                new_left -= 1
                old_left -= tag != "+"
                continue
            if tag == "-":
                old_left -= 1
                continue
            old_left = new_left = 0  # hunk più corto dell'header: si torna alle intestazioni
        hunk = HUNK_HEADER.match(line)
        if hunk:
            old_count, start, new_count = hunk.groups()
            old_left = int(old_count or 1)
            new_left = int(new_count or 1)
            number = int(start)
        elif line.startswith("+++ "):
            path = line[4:].split("\t")[0].strip()
            current = None if path == "/dev/null" else files.setdefault(strip_ab(path), [])
    return files


def evidence_line(evidence: str, lines: list[tuple[int, str]]) -> int | None:
    """Numero della prima riga citata che compare una sola volta tra le righe del nuovo file;
    None se nessuna è univoca. Le righe rimosse (-) non sono nel nuovo file e si saltano."""
    for raw in evidence.splitlines():
        quoted = raw.strip().strip("`").strip()
        if quoted.startswith("-") or quoted in ("", "...", "…"):
            continue
        quoted = " ".join(quoted.removeprefix("+").split())
        if not quoted:
            continue
        hits = [number for number, text in lines if " ".join(text.split()) == quoted]
        if len(hits) == 1:
            return hits[0]
    return None


def code_text(diff: str) -> str:
    """Il testo delle righe della diff senza il marcatore iniziale (+, - o spazio) e con gli spazi
    compattati: la forma in cui si cerca la citazione di un problema."""
    lines = (line[1:] if line[:1] in "+- " else line for line in diff.splitlines())
    return " ".join(" ".join(lines).split())


def quoted_from(evidence: str, diff_code: str) -> bool:
    """True se la citazione compare nella diff, con o senza i marcatori + e - all'inizio delle
    righe e senza badare a spazi e backtick."""
    lines = [line.strip().strip("`").strip() for line in evidence.splitlines()]
    lines = [line for line in lines if line and line not in ("...", "…")]
    if not lines:
        return False
    for candidate in (lines, [line[1:] if line[:1] in "+-" else line for line in lines]):
        if " ".join(" ".join(candidate).split()) in diff_code:
            return True
    return False


def skip_generated_files(diff: str) -> tuple[str, list[str]]:
    """La diff senza le sezioni di lockfile e file generati (GENERATED_NAMES e
    GENERATED_SUFFIXES), e l'elenco dei file tolti."""
    pattern = GIT_FILE if GIT_FILE.search(diff) else PLAIN_FILE
    bounds = [0, *(m.start() for m in pattern.finditer(diff) if m.start() > 0), len(diff)]
    kept: list[str] = []
    skipped: list[str] = []
    for a, b in pairwise(bounds):
        section = diff[a:b]
        files = diff_files(section)
        if files and all(is_generated(name) for name in files):
            skipped.extend(name for name in files if name not in skipped)
        else:
            kept.append(section)
    return "".join(kept), skipped


def is_generated(path: str) -> bool:
    name = path.rsplit("/", 1)[-1].lower()
    return name in GENERATED_NAMES or name.endswith(GENERATED_SUFFIXES)


def diff_files(diff: str) -> list[str]:
    """I percorsi dei file toccati dalla diff, nell'ordine e senza ripetizioni."""
    paths = [old if new == "/dev/null" else new for old, new in FILE_PATHS.findall(diff)]
    if not paths:  # diff di git senza righe ---/+++ (solo rinomina, file binario)
        paths = GIT_PATH.findall(diff)
    files: list[str] = []
    for path in paths:
        path = strip_ab(path)
        if path not in files:
            files.append(path)
    return files

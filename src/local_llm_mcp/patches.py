"""Controllo in memoria di una unified diff proposta dal modello: nessun file viene scritto.

Per ogni hunk le righe di contesto e quelle tolte devono comparire nel file (spazi finali e a capo
ignorati). Come `patch`, un hunk si cerca anche più avanti o più indietro se le righe dichiarate
sono sbagliate: lo spostamento si segnala in `offset`. I modelli sbagliano spesso i conteggi
nelle intestazioni `@@`: si ricalcolano e la diff restituita in `patch` è quella corretta,
applicabile con `git apply`.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")
DEV_NULL = "/dev/null"
# Righe di contesto aggiunte dopo un hunk che non ne ha (come git diff).
CONTEXT_LINES = 3

Lookup = Callable[[str], "tuple[str, str] | None"]


@dataclass
class _Hunk:
    old_start: int
    header_tail: str
    lines: list[str] = field(default_factory=list)  # con il prefisso ' ', '-', '+'

    @property
    def old(self) -> list[str]:
        return [line[1:] for line in self.lines if line[0] in " -"]

    @property
    def new(self) -> list[str]:
        return [line[1:] for line in self.lines if line[0] in " +"]


@dataclass
class _FilePatch:
    old_path: str
    new_path: str
    hunks: list[_Hunk] = field(default_factory=list)

    @property
    def path(self) -> str:
        return self.old_path if self.new_path == DEV_NULL else self.new_path

    @property
    def status(self) -> str:
        if self.old_path == DEV_NULL:
            return "created"
        if self.new_path == DEV_NULL:
            return "deleted"
        return "modified"


class PatchFormatError(ValueError):
    pass


def check_patch(diff: str, lookup: Lookup) -> dict[str, Any]:
    """{"applies", "files": [{path, status, hunks, problems}], "patch"}.

    `lookup(path)` restituisce (percorso normalizzato, testo) del file attuale, o None se non
    esiste o non è leggibile.
    """
    try:
        files = parse_diff(diff)
    except PatchFormatError as exc:
        return {"applies": False, "files": [], "error": str(exc), "patch": None}
    if not files:
        return {"applies": False, "files": [], "error": "no file in the diff", "patch": None}

    report, fixed_parts, applies = [], [], True
    for fp in files:
        entry, fixed = _check_file(fp, lookup)
        applies &= not entry["problems"]
        report.append(entry)
        fixed_parts.append(fixed)
    return {"applies": applies, "files": report, "patch": "".join(fixed_parts)}


def parse_diff(diff: str) -> list[_FilePatch]:
    files: list[_FilePatch] = []
    current: _FilePatch | None = None
    hunk: _Hunk | None = None
    lines = diff.replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            current = _FilePatch(_path(line[4:]), _path(lines[i + 1][4:]))
            files.append(current)
            hunk = None
            i += 2
            continue
        match = _HUNK.match(line)
        if match:
            if current is None:
                raise PatchFormatError("hunk before the '---'/'+++' file header")
            hunk = _Hunk(int(match.group(1)), match.group(5))
            current.hunks.append(hunk)
        elif hunk is not None and line[:1] in (" ", "-", "+"):
            hunk.lines.append(line)
        elif hunk is not None and line == "" and _more_hunk_lines(lines, i):
            hunk.lines.append(" ")  # riga vuota di contesto senza lo spazio iniziale
        elif line.startswith("\\"):
            pass  # "\ No newline at end of file"
        else:
            hunk = None  # "diff --git", "index ...", testo libero: fine dell'hunk
        i += 1
    for fp in files:
        if not fp.hunks:
            raise PatchFormatError(f"no hunk for {fp.path}")
    return files


def _check_file(fp: _FilePatch, lookup: Lookup) -> tuple[dict[str, Any], str]:
    problems: list[str] = []
    hunks_report: list[dict[str, Any]] = []
    found = lookup(fp.path) if fp.path != DEV_NULL else None
    path = found[0] if found else fp.path

    if fp.status == "created":
        if found is not None:
            problems.append("the file already exists")
        content: list[str] = []
    elif found is None:
        problems.append("file not found or not readable")
        content = []
    else:
        content = found[1].replace("\r\n", "\n").split("\n")
        if content and content[-1] == "":
            content.pop()

    fixed_hunks: list[str] = []
    shift = 0  # righe aggiunte meno tolte dagli hunk precedenti
    floor = 0  # un hunk non può partire prima della fine del precedente
    working = list(content)
    for number, hunk in enumerate(fp.hunks, 1):
        old, new = hunk.old, hunk.new
        expected = (hunk.old_start - 1 if old else hunk.old_start) + shift
        position = None if problems else _locate(working, old, expected, floor)
        info: dict[str, Any] = {"hunk": number, "applies": position is not None}
        if position is None:
            if not problems:
                problems.append(f"hunk {number}: the removed/context lines are not in the file")
            hunks_report.append(info)
            continue
        if position != expected:
            info["offset"] = position - expected
        lines = _exact_lines(hunk.lines, working, position)
        end = position + len(old)
        if _trailing_context(lines) == 0 and end < len(working):
            # git apply ancora alla fine del file un hunk senza contesto finale: si aggiunge
            # il contesto vero che segue, così la diff si applica dove il modello intendeva.
            extra = [" " + line for line in working[end : end + CONTEXT_LINES]]
            lines += extra
            info["context_added"] = len(extra)
        old_len = sum(1 for line in lines if line[0] in " -")
        new_len = sum(1 for line in lines if line[0] in " +")
        working[position : position + len(old)] = new
        old_start = position - shift + 1 if old_len else position - shift
        new_start = position + 1 if new_len else position
        fixed_hunks.append(
            f"@@ -{old_start},{old_len} +{new_start},{new_len} @@{hunk.header_tail}\n"
            + "".join(line + "\n" for line in lines)
        )
        shift += len(new) - len(old)
        floor = position + len(new)
        hunks_report.append(info)

    if fp.status == "deleted" and not problems and working:
        problems.append("the diff deletes the file but does not remove all its lines")

    old_name = DEV_NULL if fp.status == "created" else f"a/{path}"
    new_name = DEV_NULL if fp.status == "deleted" else f"b/{path}"
    fixed = f"--- {old_name}\n+++ {new_name}\n" + "".join(fixed_hunks)
    entry = {"path": path, "status": fp.status, "hunks": hunks_report, "problems": problems}
    return entry, fixed


def _exact_lines(lines: list[str], content: list[str], position: int) -> list[str]:
    """Righe dell'hunk con il contesto e le righe tolte copiate dal file (spazi finali compresi):
    il confronto le ha accettate ignorando gli spazi finali, git apply no."""
    out, index = [], position
    for line in lines:
        if line[0] in " -":
            out.append(line[0] + content[index])
            index += 1
        else:
            out.append(line)
    return out


def _trailing_context(lines: list[str]) -> int:
    count = 0
    for line in reversed(lines):
        if line[0] != " ":
            break
        count += 1
    return count


def _locate(content: list[str], old: list[str], expected: int, floor: int) -> int | None:
    """Posizione (da 0) in cui `old` compare in `content`, la più vicina a `expected`."""
    if not old:
        return expected if floor <= expected <= len(content) else None
    target = [line.rstrip() for line in old]
    stripped = [line.rstrip() for line in content]
    candidates = [
        i for i in range(floor, len(content) - len(old) + 1) if stripped[i : i + len(old)] == target
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda i: abs(i - expected))


def _more_hunk_lines(lines: list[str], i: int) -> bool:
    """Una riga vuota fa parte dell'hunk solo se dopo continua l'hunk."""
    for line in lines[i + 1 :]:
        if line == "":
            continue
        return line[:1] in (" ", "-", "+") and not line.startswith(("--- ", "+++ "))
    return False


def _path(raw: str) -> str:
    name = raw.split("\t", 1)[0].strip()
    if name == DEV_NULL:
        return DEV_NULL
    if name.startswith(("a/", "b/")):
        name = name[2:]
    return name

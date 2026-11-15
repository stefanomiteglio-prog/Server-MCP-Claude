"""Verifica delle citazioni del modello sul testo reale dei file.

Una citazione è {path, line, end_line?, quote}. È `verified` se `quote` compare nel file (spazi
normalizzati, anche su più righe e come parte di riga). Se compare ma a un'altra riga, la riga si
corregge e si segnala con `line_corrected`. Il controllo è sul testo già oscurato che ha visto il
modello: un segreto citato risulta [REDACTED:...] in entrambi.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

# Una citazione così corta compare quasi ovunque: non dimostra nulla.
MIN_QUOTE_CHARS = 4

# Prefisso "  12: " copiato dall'output di read_file.
_LINE_PREFIX = re.compile(r"^\s*\d+:\s?")
_SPACES = re.compile(r"\s+")
# Marcatore di commento all'inizio della riga: i modelli scrivono spesso "//" al posto di "///" o
# "#" al posto di "##". Si toglie sia dalla citazione sia dal file: il testo deve comunque esistere.
_COMMENT = re.compile(r"^(?:/{2,}|#+|\*+|--)\s?")

Lookup = Callable[[str], "tuple[str, str] | None"]


def verify_citations(items: Any, lookup: Lookup) -> list[dict[str, Any]]:
    """Verifica ogni citazione; `lookup(path)` restituisce (percorso normalizzato, testo) o None.

    Le voci che non sono oggetti vengono scartate; i campi mancanti valgono come non verificati.
    """
    out = []
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict):
            out.append(verify_citation(item, lookup))
    return out


def verify_citation(item: dict[str, Any], lookup: Lookup) -> dict[str, Any]:
    path = str(item.get("path") or "").strip()
    quote = item.get("quote") if isinstance(item.get("quote"), str) else ""
    line = _as_int(item.get("line"))
    end_line = _as_int(item.get("end_line"))
    result: dict[str, Any] = {"path": path, "line": line, "quote": quote, "verified": False}
    if end_line is not None:
        result["end_line"] = end_line

    found = lookup(path) if path else None
    if found is None:
        result["problem"] = "file not found or not readable"
        return result
    result["path"], text = found

    needle = normalize_quote(quote)
    if len(needle.replace("\n", "")) < MIN_QUOTE_CHARS:
        result["problem"] = "quote missing or too short to verify"
        return result
    starts = find_quote(needle, text)
    if not starts:
        result["problem"] = "quote not found in the file"
        return result

    start, end = min(starts, key=lambda s: abs(s[0] - (line or 0)))
    result["verified"] = True
    if line != start or (end_line is not None and end_line != end):
        result["line_corrected"] = True
    result["line"] = start
    if end != start:
        result["end_line"] = end
    else:
        result.pop("end_line", None)
    return result


def find_quote(needle: str, text: str) -> list[tuple[int, int]]:
    """Righe (inizio, fine), da 1, di ogni occorrenza della citazione già normalizzata."""
    lines = [_normalize(line) for line in text.splitlines()]
    haystack = "\n".join(lines)
    # Offset di inizio di ogni riga, per tradurre la posizione del testo in numero di riga.
    offsets, pos = [], 0
    for line in lines:
        offsets.append(pos)
        pos += len(line) + 1

    found, index = [], haystack.find(needle)
    while index != -1:
        start = _line_at(offsets, index)
        end = _line_at(offsets, index + len(needle) - 1)
        found.append((start, end))
        index = haystack.find(needle, index + 1)
    return found


def normalize_quote(quote: str) -> str:
    lines = [line for line in quote.strip("\n").splitlines()]
    # Se ogni riga ha il prefisso di read_file, il modello l'ha copiato: si toglie.
    if lines and all(_LINE_PREFIX.match(line) for line in lines):
        lines = [_LINE_PREFIX.sub("", line, count=1) for line in lines]
    return "\n".join(_normalize(line) for line in lines).strip()


def _normalize(line: str) -> str:
    return _COMMENT.sub("", _SPACES.sub(" ", line).strip(), count=1).strip()


def _line_at(offsets: list[int], index: int) -> int:
    lo, hi = 0, len(offsets) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if offsets[mid] <= index:
            lo = mid
        else:
            hi = mid - 1
    return lo + 1


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def citation_stats(citations: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "citations_total": len(citations),
        "citations_verified": sum(1 for c in citations if c.get("verified")),
    }

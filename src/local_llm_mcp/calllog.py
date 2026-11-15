"""Registro delle chiamate ai modelli in SQLite: solo metadati, mai il contenuto.

Il server MCP scrive una riga per chiamata di tool (gli stessi metadati della riga di log
`tool_call`); la dashboard legge il file in sola lettura. Più sessioni di Claude Code possono
scrivere nello stesso file: SQLite in modalità WAL gestisce la concorrenza fra processi.
"""

from __future__ import annotations

import logging
import math
import os
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any

logger = logging.getLogger("local_llm_mcp")

# Le sole chiavi del record di _track salvate nel registro. È un elenco chiuso: se un giorno il
# record contenesse altro, non finirebbe comunque nel file.
FIELDS = (
    "tool",
    "model",
    "outcome",
    "duration_ms",
    "input_chars",
    "output_chars",
    "prompt_tokens",
    "completion_tokens",
    "chunks",
    "failed_chunks",
    "redacted_secrets",
    "finish_reason",
    "request_id",
    "retried_without_reasoning",
    # Tool con agente (glm-5.3-flash).
    "effort",
    "steps",
    "tool_calls",
    "files_read",
    "read_chars",
    "stopped",
    "citations_total",
    "citations_verified",
    "applies",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    pid INTEGER,
    tool TEXT NOT NULL,
    model TEXT,
    outcome TEXT NOT NULL,
    duration_ms INTEGER,
    input_chars INTEGER,
    output_chars INTEGER,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    chunks INTEGER,
    failed_chunks INTEGER,
    redacted_secrets INTEGER,
    finish_reason TEXT,
    request_id TEXT,
    retried_without_reasoning INTEGER,
    effort TEXT,
    steps INTEGER,
    tool_calls INTEGER,
    files_read INTEGER,
    read_chars INTEGER,
    stopped TEXT,
    citations_total INTEGER,
    citations_verified INTEGER,
    applies INTEGER
);
CREATE INDEX IF NOT EXISTS calls_ts ON calls (ts);
"""
# Colonne aggiunte dopo la prima versione del registro: i file già esistenti le ricevono all'avvio.
ADDED_COLUMNS = {
    "failed_chunks": "INTEGER",
    "retried_without_reasoning": "INTEGER",
    "effort": "TEXT",
    "steps": "INTEGER",
    "tool_calls": "INTEGER",
    "files_read": "INTEGER",
    "read_chars": "INTEGER",
    "stopped": "TEXT",
    "citations_total": "INTEGER",
    "citations_verified": "INTEGER",
    "applies": "INTEGER",
}

# Finestre della dashboard: durata e ampiezza di una barra del grafico, in secondi.
WINDOWS = {
    "1h": (3600, 60),
    "6h": (6 * 3600, 300),
    "24h": (86400, 1800),
    "7d": (7 * 86400, 4 * 3600),
    "30d": (30 * 86400, 86400),
}
OUTCOME_CLASSES = ("ok", "partial", "invalid", "error")
# no_listing (l'API non espone /models) è una risposta valida del tool, non un errore.
# partial:<codice> è una review riuscita solo in parte: alcuni blocchi non sono stati rivisti.
_OUTCOME_CLASS = (
    "CASE WHEN outcome IN ('ok', 'no_listing') THEN 'ok' "
    "WHEN outcome LIKE 'partial:%' THEN 'partial' "
    "WHEN outcome LIKE 'invalid_output:%' THEN 'invalid' ELSE 'error' END"
)
RECENT_LIMIT = 100
GROUP_LIMIT = 20


class CallLog:
    """Scrittura del registro. Un errore del registro non fa mai fallire un tool."""

    def __init__(
        self, path: Path, retention_days: int = 30, *, clock: Callable[[], float] = time.time
    ) -> None:
        self.path = path
        self._clock = clock
        self._enabled = True
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with _connect(path) as db:
                db.execute("PRAGMA journal_mode=WAL")
                db.executescript(SCHEMA)
                _add_missing_columns(db)
                db.execute("DELETE FROM calls WHERE ts < ?", (clock() - retention_days * 86400,))
        except (OSError, sqlite3.Error) as exc:
            self._enabled = False
            logger.warning("call_log disattivato: %s (%s)", type(exc).__name__, path)

    def record(self, rec: dict[str, Any]) -> None:
        if not self._enabled:
            return
        row = {"ts": self._clock(), "pid": os.getpid()}
        row.update((key, rec.get(key)) for key in FIELDS)
        columns = ", ".join(row)
        placeholders = ", ".join(f":{key}" for key in row)
        try:
            with _connect(self.path) as db:
                db.execute(f"INSERT INTO calls ({columns}) VALUES ({placeholders})", row)
        except (OSError, sqlite3.Error) as exc:
            logger.warning("call_log: scrittura non riuscita (%s)", type(exc).__name__)


def dashboard_data(
    path: Path | None,
    window: str = "24h",
    *,
    tool: str | None = None,
    model: str | None = None,
    outcome: str | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Tutto ciò che mostra la dashboard per una finestra temporale e dei filtri opzionali.

    I nomi di colonna nelle query SQL vengono solo da costanti di questo modulo; i valori dei
    filtri passano sempre come parametri.
    """
    span, bucket = WINDOWS[window]
    now = time.time() if now is None else now
    since = now - span
    data: dict[str, Any] = {
        "window": window,
        "now": now,
        "since": since,
        "path": str(path) if path else None,
        "available": False,
    }
    if path is None or not path.is_file():
        return data

    where, params = ["ts >= ?"], [since]
    for column, value in (("tool", tool), ("model", model), (_OUTCOME_CLASS, outcome)):
        if value:
            where.append(f"{column} = ?")
            params.append(value)
    f = _Filter(" AND ".join(where), params)

    with _connect(path, readonly=True) as db:
        db.row_factory = sqlite3.Row
        data["available"] = True
        # Un registro scritto da un server MCP non aggiornato può non avere le colonne nuove: la
        # dashboard lo legge in sola lettura, quindi le colonne mancanti valgono NULL.
        present = _columns(db)
        columns = [c if c in present else f"NULL AS {c}" for c in FIELDS]
        data["summary"] = _summary(db, f, present)
        data["timeline"] = {
            "bucket_seconds": bucket,
            "buckets": _timeline(db, f, since, bucket, span // bucket),
        }
        data["by_tool"] = _groups(db, f, "tool", present)
        data["by_model"] = _groups(db, f, "model", present)
        data["outcomes"] = f.rows(
            db,
            "SELECT outcome, COUNT(*) AS calls FROM calls WHERE {cond} "
            f"AND {_OUTCOME_CLASS} != 'ok' GROUP BY outcome ORDER BY calls DESC LIMIT 10",
        )
        data["recent"] = f.rows(
            db,
            f"SELECT ts, pid, {', '.join(columns)} FROM calls WHERE {{cond}} "
            f"ORDER BY ts DESC LIMIT {RECENT_LIMIT}",
        )
        # Valori dei filtri: sull'intera finestra, così un filtro attivo non nasconde gli altri.
        everything = _Filter("ts >= ?", [since])
        data["options"] = {
            column: [
                r["v"]
                for r in everything.rows(
                    db, f"SELECT DISTINCT {column} AS v FROM calls WHERE {{cond}} ORDER BY 1"
                )
                if r["v"]
            ]
            for column in ("tool", "model")
        }
    return data


class _Filter:
    """Condizione WHERE con i suoi parametri; {cond} nella query viene sostituito."""

    def __init__(self, cond: str, params: list[Any]) -> None:
        self.cond = cond
        self.params = params

    def rows(
        self, db: sqlite3.Connection, sql: str, before: tuple[Any, ...] = ()
    ) -> list[dict[str, Any]]:
        query = sql.replace("{cond}", self.cond)
        return [dict(r) for r in db.execute(query, [*before, *self.params]).fetchall()]


def _col(name: str, present: set[str]) -> str:
    """La colonna, o NULL se il registro è stato scritto prima che esistesse."""
    return name if name in present else "NULL"


def _summary(db: sqlite3.Connection, f: _Filter, present: set[str]) -> dict[str, Any]:
    retried = _col("retried_without_reasoning", present)
    applies = _col("applies", present)
    (row,) = f.rows(
        db,
        f"""SELECT COUNT(*) AS calls,
            SUM({_OUTCOME_CLASS} = 'ok') AS ok,
            SUM({_OUTCOME_CLASS} = 'partial') AS partial,
            SUM({_OUTCOME_CLASS} = 'invalid') AS invalid,
            SUM({_OUTCOME_CLASS} = 'error') AS error,
            SUM(prompt_tokens) AS prompt_tokens,
            SUM(completion_tokens) AS completion_tokens,
            SUM(input_chars) AS input_chars,
            SUM(redacted_secrets) AS redacted_secrets,
            SUM(chunks IS NOT NULL) AS chunked_calls,
            SUM({retried} IS NOT NULL) AS retried_calls,
            SUM({_col("stopped", present)} IS NOT NULL) AS stopped_calls,
            SUM({_col("citations_total", present)}) AS citations_total,
            SUM({_col("citations_verified", present)}) AS citations_verified,
            SUM({applies} IS NOT NULL) AS patches,
            SUM({applies} = 1) AS patches_applied,
            AVG(duration_ms) AS avg_ms,
            MAX(duration_ms) AS max_ms
        FROM calls WHERE {{cond}}""",
    )
    summary = {key: value or 0 for key, value in row.items()}
    summary["avg_ms"] = round(summary["avg_ms"])
    for name, fraction in (("p50_ms", 0.5), ("p95_ms", 0.95)):
        summary[name] = 0
        if summary["calls"]:
            # Percentile "nearest rank": la durata in posizione ceil(f * n), contando da 1.
            offset = math.ceil(fraction * summary["calls"]) - 1
            (value,) = f.rows(
                db,
                "SELECT duration_ms AS v FROM calls WHERE {cond} "
                f"ORDER BY duration_ms LIMIT 1 OFFSET {offset}",
            )
            summary[name] = value["v"] or 0
    return summary


def _timeline(
    db: sqlite3.Connection, f: _Filter, since: float, bucket: int, count: int
) -> list[dict[str, Any]]:
    buckets = [
        {"start": since + i * bucket, **dict.fromkeys(OUTCOME_CLASSES, 0)} for i in range(count)
    ]
    rows = f.rows(
        db,
        f"SELECT CAST((ts - ?) / ? AS INTEGER) AS b, {_OUTCOME_CLASS} AS kind, COUNT(*) AS n "
        "FROM calls WHERE {cond} GROUP BY b, kind",
        before=(since, bucket),
    )
    for r in rows:
        # La chiamata al secondo esatto di "adesso" cade nell'ultima barra, non oltre.
        buckets[min(max(r["b"], 0), count - 1)][r["kind"]] += r["n"]
    return buckets


def _groups(
    db: sqlite3.Connection, f: _Filter, column: str, present: set[str]
) -> list[dict[str, Any]]:
    return f.rows(
        db,
        f"""SELECT {column} AS name, COUNT(*) AS calls,
            SUM({_OUTCOME_CLASS} IN ('invalid', 'error')) AS failures,
            SUM({_OUTCOME_CLASS} = 'partial') AS partial,
            CAST(ROUND(AVG(duration_ms)) AS INTEGER) AS avg_ms,
            MAX(duration_ms) AS max_ms,
            COALESCE(SUM(prompt_tokens), 0) + COALESCE(SUM(completion_tokens), 0) AS tokens,
            SUM({_col("citations_total", present)}) AS citations_total,
            SUM({_col("citations_verified", present)}) AS citations_verified
        FROM calls WHERE {{cond}} GROUP BY {column} ORDER BY calls DESC LIMIT {GROUP_LIMIT}""",
    )


def _columns(db: sqlite3.Connection) -> set[str]:
    return {row[1] for row in db.execute("PRAGMA table_info(calls)")}


def _add_missing_columns(db: sqlite3.Connection) -> None:
    present = _columns(db)
    for column, kind in ADDED_COLUMNS.items():
        if column not in present:
            db.execute(f"ALTER TABLE calls ADD COLUMN {column} {kind}")


@contextmanager
def _connect(path: Path, *, readonly: bool = False) -> Iterator[sqlite3.Connection]:
    """Connessione breve: transazione confermata all'uscita e file chiuso subito."""
    if readonly:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=2.0)
    else:
        conn = sqlite3.connect(path, timeout=2.0)
    with closing(conn), conn:
        yield conn

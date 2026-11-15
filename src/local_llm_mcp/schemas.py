"""Lettura e validazione delle risposte JSON del modello contro uno JSON Schema (draft 2020-12)."""

from __future__ import annotations

import json
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from .safety import InputError

MAX_REPORTED_ERRORS = 5


def response_format(name: str, schema: dict[str, Any]) -> dict[str, Any]:
    """Il parametro response_format dell'API per uno schema."""
    return {"type": "json_schema", "json_schema": {"name": name, "schema": schema, "strict": True}}


def check_schema(schema: Any, label: str = "json_schema") -> None:
    """Rifiuta uno schema non valido passato da chi chiama un tool."""
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise InputError(f'\'{label}\' deve essere un JSON Schema con "type": "object"')
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise InputError(f"'{label}' non è un JSON Schema valido: {exc.message}") from exc


def schema_errors(value: Any, schema: dict[str, Any]) -> list[str]:
    """Errori di validazione leggibili ('percorso: messaggio'), al massimo MAX_REPORTED_ERRORS."""
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(value), key=lambda e: list(e.absolute_path))
    out = []
    for error in errors[:MAX_REPORTED_ERRORS]:
        where = "/".join(str(p) for p in error.absolute_path) or "(root)"
        out.append(f"{where}: {error.message[:200]}")
    if len(errors) > MAX_REPORTED_ERRORS:
        out.append(f"... and {len(errors) - MAX_REPORTED_ERRORS} more")
    return out


def parse_json_answer(content: str, schema: dict[str, Any]) -> tuple[Any, str | None]:
    """(oggetto, None) se il contenuto è un JSON valido per lo schema, altrimenti (None, motivo)."""
    try:
        value = json.loads(strip_fence(content))
    except ValueError:
        return None, "not valid JSON"
    errors = schema_errors(value, schema)
    if errors:
        return None, "does not match the schema: " + "; ".join(errors)
    return value, None


def json_validator(schema: dict[str, Any]):
    """Validator per Agent.run: None se la risposta va bene, altrimenti il motivo."""

    def validate(content: str) -> str | None:
        return parse_json_answer(content, schema)[1]

    return validate


def strip_fence(content: str) -> str:
    """Toglie un eventuale blocco ```json ... ``` attorno al JSON."""
    text = content.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    return text.strip()

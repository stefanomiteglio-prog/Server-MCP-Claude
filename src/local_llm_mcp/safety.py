"""Limiti di dimensione e oscuramento dei segreti prima dell'invio al modello.

Il rilevamento dei segreti è best-effort: copre i formati più comuni, non è una garanzia.
"""

from __future__ import annotations

import re
from collections import Counter


class InputError(ValueError):
    """Input non valido: va corretto da chi chiama il tool, non ritentato."""


_SPECIFIC_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "PRIVATE_KEY",
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----.*?-----END [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----",
            re.S,
        ),
    ),
    ("API_KEY", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}")),
    ("AWS_ACCESS_KEY", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("GITHUB_TOKEN", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})")),
    ("GITLAB_TOKEN", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}")),
    ("SLACK_TOKEN", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}")),
    ("GOOGLE_API_KEY", re.compile(r"\bAIza[0-9A-Za-z_-]{35}")),
    ("STRIPE_KEY", re.compile(r"\b[rs]k_(?:live|test)_[A-Za-z0-9]{16,}")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
]

# "Authorization: Bearer <token>": si oscura solo il token.
_BEARER = re.compile(r"(?i)(\bbearer\s+)([A-Za-z0-9._~+/=-]{16,})")

# scheme://utente:password@host: si oscura solo la password.
_URL_CREDENTIALS = re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://[^\s/:@]+:)([^\s/@]+)(@)")

# Assegnazioni del tipo password = "...", API_KEY=..., "client_secret": "...".
# Il nome deve TERMINARE con la parola chiave (token_type, tokenizer, max_tokens non contano).
_KEY_NAME_RE = (
    r"[A-Za-z0-9_.-]*?(?:password|passwd|secret|secret[_-]?key|token|api[_-]?key|apikey"
    r"|access[_-]?key|private[_-]?key|client[_-]?secret|credentials?)\b"
)
_SEPARATOR = r"[\"']?\s*(?:=>|=|:)\s*"
# Valore tra virgolette: almeno 6 caratteri, senza spazi.
_QUOTED_ASSIGNMENT = re.compile(
    rf"(?i)(?P<head>{_KEY_NAME_RE}{_SEPARATOR})(?P<q>[\"'])(?P<value>[^\"'\s]{{6,}})(?P=q)"
)
# Valore senza virgolette (stile .env/YAML): almeno 8 caratteri, contiene una cifra e
# niente che faccia pensare a codice (chiamate, attributi, variabili ${...}).
_UNQUOTED_ASSIGNMENT = re.compile(
    rf"(?i)(?P<head>{_KEY_NAME_RE}{_SEPARATOR})"
    r"(?P<value>(?=[^\s\"'`,;(){}\[\]$<>.]*\d)[^\s\"'`,;(){}\[\]$<>.]{8,})"
)


def placeholder(kind: str) -> str:
    return f"[REDACTED:{kind}]"


def redact_secrets(text: str) -> tuple[str, Counter[str]]:
    """Sostituisce i segreti riconosciuti con [REDACTED:TIPO]; restituisce testo e conteggi."""
    found: Counter[str] = Counter()

    for kind, pattern in _SPECIFIC_PATTERNS:
        text, n = pattern.subn(placeholder(kind), text)
        found[kind] += n

    text, n = _BEARER.subn(lambda m: m.group(1) + placeholder("BEARER_TOKEN"), text)
    found["BEARER_TOKEN"] += n

    text, n = _URL_CREDENTIALS.subn(
        lambda m: m.group(1) + placeholder("URL_PASSWORD") + m.group(3), text
    )
    found["URL_PASSWORD"] += n

    text, n = _QUOTED_ASSIGNMENT.subn(
        lambda m: m.group("head") + m.group("q") + placeholder("SECRET") + m.group("q"), text
    )
    found["SECRET"] += n

    text, n = _UNQUOTED_ASSIGNMENT.subn(lambda m: m.group("head") + placeholder("SECRET"), text)
    found["SECRET"] += n

    return text, +found  # "+" rimuove i conteggi a zero


def ensure_not_empty(text: str, label: str) -> None:
    if not text or not text.strip():
        raise InputError(f"'{label}' è vuoto")


def ensure_max_chars(size: int, limit: int, label: str, env_var: str) -> None:
    if size > limit:
        raise InputError(
            f"'{label}' troppo lungo: {size:,} caratteri, limite {limit:,}. "
            f"Dividi il contenuto in parti più piccole oppure aumenta {env_var}"
        )


def redaction_note(found: Counter[str]) -> str | None:
    if not found:
        return None
    kinds = ", ".join(f"{kind}×{count}" for kind, count in sorted(found.items()))
    return (
        f"{sum(found.values())} possibili segreti oscurati prima dell'invio al modello ({kinds}): "
        "il modello non ha visto quei valori"
    )

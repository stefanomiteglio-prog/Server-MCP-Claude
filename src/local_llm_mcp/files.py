"""Lettura di file di testo in sola lettura, limitata alle cartelle consentite.

Serve a non far passare a Claude il contenuto di file lunghi: il server lo legge e lo
invia direttamente al modello. Nessuna scrittura, nessun elenco di cartelle.
"""

from __future__ import annotations

import codecs
from pathlib import Path

from .safety import InputError

SENSITIVE_DIRS = {".git", ".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker"}
SENSITIVE_FILENAMES = {
    ".netrc",
    "_netrc",
    ".pgpass",
    ".npmrc",
    ".pypirc",
    ".git-credentials",
    ".htpasswd",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "credentials",
    "credentials.json",
    "secrets.json",
    "secrets.yaml",
    "secrets.yml",
}
SENSITIVE_SUFFIXES = {
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".jks",
    ".keystore",
    ".kdbx",
    ".ppk",
    ".gpg",
    ".asc",
    ".tfstate",
}
# .env.example & co. non contengono segreti reali e restano leggibili.
ENV_TEMPLATE_SUFFIXES = (".example", ".sample", ".template", ".dist")


def sensitive_reason(path: Path) -> str | None:
    name = path.name.lower()
    if (
        name == ".env"
        or name.endswith(".env")
        or (name.startswith(".env.") and not name.endswith(ENV_TEMPLATE_SUFFIXES))
    ):
        return "file di variabili d'ambiente"
    if name in SENSITIVE_FILENAMES:
        return "file di credenziali"
    if path.suffix.lower() in SENSITIVE_SUFFIXES:
        return "chiave o certificato"
    if any(part.lower() in SENSITIVE_DIRS for part in path.parts[:-1]):
        return "cartella riservata"
    return None


def read_text_file(
    raw_path: str,
    roots: tuple[Path, ...],
    max_chars: int,
    env_var: str = "LOCAL_LLM_DOCUMENT_MAX_CHARS",
) -> str:
    if not roots:
        raise InputError(
            "Lettura da file disattivata: imposta LOCAL_LLM_ALLOWED_READ_ROOTS oppure passa il "
            "contenuto nel parametro di testo"
        )
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        raise InputError("'path' deve essere un percorso assoluto")
    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError as exc:
        raise InputError(f"File non trovato: {raw_path}") from exc
    except OSError as exc:
        raise InputError(f"Impossibile accedere al file ({type(exc).__name__})") from exc

    if not any(resolved.is_relative_to(root) for root in roots):
        # Le cartelle nel messaggio permettono a Claude di scegliere subito un percorso valido.
        raise InputError(
            "Percorso fuori dalle cartelle consentite (LOCAL_LLM_ALLOWED_READ_ROOTS): "
            f"{'; '.join(str(root) for root in roots)}. Salva il file in una di queste cartelle, "
            "ma non nel repository su cui stai lavorando, oppure passa il contenuto come testo"
        )
    reason = sensitive_reason(path) or sensitive_reason(resolved)
    if reason:
        raise InputError(f"File escluso per sicurezza: {reason}")
    if not resolved.is_file():
        raise InputError("Il percorso non indica un file")

    # UTF-8 usa al massimo 4 byte per carattere: oltre questa soglia il limite è superato.
    if resolved.stat().st_size > max_chars * 4:
        raise InputError(
            f"File troppo grande per il limite di {max_chars:,} caratteri. Dividilo oppure "
            f"aumenta {env_var}"
        )
    data = resolved.read_bytes()
    # Windows PowerShell 5.1 scrive in UTF-16 con BOM (per esempio "git diff > file"): i suoi
    # byte nulli non vogliono dire che il file sia binario.
    utf16 = data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE))
    if not utf16 and b"\x00" in data:
        raise InputError("Il file sembra binario: sono supportati solo file di testo")
    try:
        text = data.decode("utf-16" if utf16 else "utf-8-sig")
    except UnicodeDecodeError as exc:
        raise InputError("Il file non è testo UTF-8 né UTF-16 con BOM") from exc
    if "\x00" in text:
        raise InputError("Il file sembra binario: sono supportati solo file di testo")
    # Gli a capo di Windows confonderebbero l'analisi delle diff; per i modelli non contano.
    return text.replace("\r\n", "\n")

"""Configurazione letta esclusivamente da variabili d'ambiente (ed eventuale file .env)."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# src/local_llm_mcp/config.py -> radice del progetto
PROJECT_ROOT = Path(__file__).resolve().parents[2]

VALID_TIERS = ("slow", "medium", "fast", "ludicrous")
VALID_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")

# Limite documentato dall'API per max_tokens / max_completion_tokens (oltre: 422).
API_MAX_OUTPUT_TOKENS = 32768

# Livelli di reasoning_effort di glm-5.3-flash. Va sempre inviato: senza, il modello ragiona più
# che con "high" (test reali del 13/11/2026, docs/piano-glm.md).
VALID_EFFORTS = ("low", "medium", "high")

# Registro delle chiamate (solo metadati) letto dalla dashboard. .local/ è escluso da git.
DEFAULT_CALL_LOG = PROJECT_ROOT / ".local" / "calls.sqlite3"
CALL_LOG_OFF = ("off", "no", "false", "0")

# Variabili dei tool per i modelli piccoli, tolti con il passaggio a GLM: se ancora presenti
# vengono ignorate e `--check` le segnala. LOCAL_LLM_MAX_INPUT_CHARS e LOCAL_LLM_MAX_OUTPUT_TOKENS
# hanno lasciato il posto a LOCAL_LLM_DOCUMENT_MAX_CHARS e LOCAL_LLM_GLM_MAX_OUTPUT_TOKENS: i
# vecchi valori (60000 caratteri, 4096-8192 token) troncherebbero i compiti di GLM.
OBSOLETE_VARIABLES = (
    "LOCAL_LLM_DEFAULT_MODEL",
    "LOCAL_LLM_CODE_MODEL",
    "LOCAL_LLM_EXCLUDED_MODELS",
    "LOCAL_LLM_MAX_CHUNKS",
    "LOCAL_LLM_REVIEW_CHUNK_CHARS",
    "LOCAL_LLM_MAX_PROMPT_CHARS",
    "LOCAL_LLM_MAX_INPUT_CHARS",
    "LOCAL_LLM_MAX_OUTPUT_TOKENS",
)


class ConfigError(ValueError):
    """Configurazione mancante o non valida."""


@dataclass(frozen=True)
class Settings:
    base_url: str
    api_key: str = field(repr=False)
    # Modello di tutti i tool (docs/piano-glm.md).
    model: str = "glm-5.3-flash"
    default_effort: str = "medium"
    # response_format inviato a ogni passo insieme a tools. GLM esplora lo stesso e risponde in
    # JSON senza una richiesta in più; qwen3-coder-next e qwen3.8-27b invece rispondono subito
    # senza leggere nulla (prove del 14/11/2026): per loro il JSON si chiede solo alla fine.
    json_while_exploring: bool = True
    # Limiti dell'agente in sola lettura: passi (richieste al modello), caratteri letti dai file
    # in tutto il compito, durata dell'intero tool.
    agent_max_steps: int = 25
    agent_max_read_chars: int = 200_000
    task_timeout_seconds: float = 300.0
    # Token di output per richiesta dei tool GLM: il ragionamento con effort high li consuma in
    # fretta (con 4096 propose_change e critique_plan venivano troncati, prove del 13/11/2026).
    glm_max_output_tokens: int = API_MAX_OUTPUT_TOKENS
    # Caratteri massimi di documenti, diff e file letti dai tool per GLM (contesto 262k token).
    document_max_chars: int = 600_000
    tier: str | None = None
    timeout_seconds: float = 120.0
    max_retries: int = 2
    allowed_read_roots: tuple[Path, ...] = ()
    log_level: str = "INFO"
    # None = registro disattivato (anche il default dei test: nessun file scritto).
    call_log: Path | None = None
    call_log_days: int = 30

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if environ is None else environ

        def get(name: str) -> str | None:
            value = env.get(name, "").strip()
            return value or None

        base_url = get("LOCAL_LLM_BASE_URL")
        if not base_url:
            raise ConfigError("LOCAL_LLM_BASE_URL non impostata (es. https://api.example.org/v1)")
        if not re.match(r"^https?://", base_url):
            raise ConfigError("LOCAL_LLM_BASE_URL deve iniziare con http:// o https://")

        api_key = get("LOCAL_LLM_API_KEY")
        if not api_key:
            raise ConfigError("LOCAL_LLM_API_KEY non impostata")

        tier = get("LOCAL_LLM_TIER")
        if tier is not None and tier not in VALID_TIERS:
            raise ConfigError(f"LOCAL_LLM_TIER deve essere uno tra {', '.join(VALID_TIERS)}")

        log_level = (get("LOCAL_LLM_LOG_LEVEL") or "INFO").upper()
        if log_level not in VALID_LOG_LEVELS:
            raise ConfigError(
                f"LOCAL_LLM_LOG_LEVEL deve essere uno tra {', '.join(VALID_LOG_LEVELS)}"
            )

        effort = (get("LOCAL_LLM_DEFAULT_EFFORT") or cls.default_effort).lower()
        if effort not in VALID_EFFORTS:
            raise ConfigError(
                f"LOCAL_LLM_DEFAULT_EFFORT deve essere uno tra {', '.join(VALID_EFFORTS)}"
            )

        model = get("LOCAL_LLM_MODEL") or cls.model
        return cls(
            base_url=base_url.rstrip("/"),
            api_key=api_key,
            model=model,
            json_while_exploring=_json_while_exploring(get, model),
            default_effort=effort,
            agent_max_steps=_int(get, "LOCAL_LLM_AGENT_MAX_STEPS", 25, 1),
            agent_max_read_chars=_int(get, "LOCAL_LLM_AGENT_MAX_READ_CHARS", 200_000, 1000),
            task_timeout_seconds=_float(get, "LOCAL_LLM_TASK_TIMEOUT_SECONDS", 300.0, 10.0),
            glm_max_output_tokens=_glm_output_tokens(get),
            document_max_chars=_int(get, "LOCAL_LLM_DOCUMENT_MAX_CHARS", 600_000, 1000),
            tier=tier,
            timeout_seconds=_float(get, "LOCAL_LLM_TIMEOUT_SECONDS", 120.0, 1.0),
            max_retries=_int(get, "LOCAL_LLM_MAX_RETRIES", 2, 0),
            allowed_read_roots=_roots(get("LOCAL_LLM_ALLOWED_READ_ROOTS")),
            log_level=log_level,
            call_log=call_log_path(env),
            call_log_days=_int(get, "LOCAL_LLM_CALL_LOG_DAYS", 30, 1),
        )


def _json_while_exploring(get, model: str) -> bool:
    raw = get("LOCAL_LLM_JSON_WHILE_EXPLORING")
    if raw is None:
        return model.casefold().startswith("glm-")
    value = raw.casefold()
    if value in ("true", "1", "yes", "on"):
        return True
    if value in ("false", "0", "no", "off"):
        return False
    raise ConfigError("LOCAL_LLM_JSON_WHILE_EXPLORING deve essere true o false")


def _glm_output_tokens(get) -> int:
    value = _int(get, "LOCAL_LLM_GLM_MAX_OUTPUT_TOKENS", API_MAX_OUTPUT_TOKENS, 1024)
    if value > API_MAX_OUTPUT_TOKENS:
        raise ConfigError(
            f"LOCAL_LLM_GLM_MAX_OUTPUT_TOKENS non può superare {API_MAX_OUTPUT_TOKENS}"
        )
    return value


def obsolete_variables(environ: Mapping[str, str] | None = None) -> list[str]:
    """Le variabili di OBSOLETE_VARIABLES impostate (non vuote) nell'ambiente."""
    env = os.environ if environ is None else environ
    return [name for name in OBSOLETE_VARIABLES if env.get(name, "").strip()]


def call_log_path(environ: Mapping[str, str] | None = None) -> Path | None:
    """Percorso del registro delle chiamate da LOCAL_LLM_CALL_LOG; None se disattivato.

    Separata da Settings.from_env perché la dashboard la usa senza URL né API key.
    """
    env = os.environ if environ is None else environ
    raw = env.get("LOCAL_LLM_CALL_LOG", "").strip()
    if not raw:
        return DEFAULT_CALL_LOG
    if raw.lower() in CALL_LOG_OFF:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ConfigError(f"LOCAL_LLM_CALL_LOG: il percorso '{raw}' non è assoluto")
    return path


def load_env_file() -> None:
    """Carica il file .env del progetto senza sovrascrivere le variabili già presenti."""
    env_file = os.environ.get("LOCAL_LLM_ENV_FILE") or str(PROJECT_ROOT / ".env")
    load_dotenv(env_file, override=False)


def _int(get, name: str, default: int, minimum: int) -> int:
    raw = get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} deve essere un numero intero") from exc
    if value < minimum:
        raise ConfigError(f"{name} deve essere >= {minimum}")
    return value


def _float(get, name: str, default: float, minimum: float) -> float:
    raw = get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} deve essere un numero") from exc
    if value < minimum:
        raise ConfigError(f"{name} deve essere >= {minimum}")
    return value


def _roots(raw: str | None) -> tuple[Path, ...]:
    """Cartelle leggibili, separate da os.pathsep (';' su Windows, ':' altrove)."""
    if not raw:
        return ()
    roots = []
    for part in raw.split(os.pathsep):
        part = part.strip()
        if not part:
            continue
        path = Path(part).expanduser()
        if not path.is_absolute():
            raise ConfigError(f"LOCAL_LLM_ALLOWED_READ_ROOTS: il percorso '{part}' non è assoluto")
        if not path.is_dir():
            raise ConfigError(f"LOCAL_LLM_ALLOWED_READ_ROOTS: la cartella '{part}' non esiste")
        roots.append(path.resolve())
    return tuple(roots)

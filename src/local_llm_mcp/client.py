"""Client HTTP riutilizzabile per l'API OpenAI-compatible. Non conosce MCP."""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from . import __version__
from .config import Settings

# Oltre questa attesa non ritentiamo un 429: meglio restituire subito l'errore a Claude.
MAX_RETRY_AFTER_SECONDS = 30.0
BACKOFF_BASE_SECONDS = 1.0

# Limite dell'API sulle richieste di chat (60 al minuto). Lo rispettiamo lato client, così un
# compito dell'agente con molti passi non provoca una raffica di 429.
CHAT_REQUESTS_PER_MINUTE = 60

# 503 che vale la pena ritentare; model_activation_required no (il modello non è caricato).
RETRYABLE_503_TYPES = {"model_unavailable", "zone_unavailable"}


class LLMError(Exception):
    """Errore dell'API o del trasporto, con un messaggio comprensibile."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        error_type: str | None = None,
        request_id: str | None = None,
        retryable: bool = False,
        retry_after: float | None = None,
    ) -> None:
        if request_id:
            message = f"{message} (request_id: {request_id})"
        super().__init__(message)
        self.status = status
        self.error_type = error_type
        self.request_id = request_id
        self.retryable = retryable
        self.retry_after = retry_after


@dataclass
class ToolCall:
    """Chiamata a una funzione chiesta dal modello. `arguments` è la stringa JSON grezza."""

    id: str
    name: str
    arguments: str


@dataclass
class ChatResult:
    content: str
    finish_reason: str | None
    model: str
    request_id: str | None
    usage: dict[str, Any] = field(default_factory=dict)
    tool_calls: list[ToolCall] = field(default_factory=list)

    def assistant_message(self) -> dict[str, Any]:
        """Il messaggio da rimettere nella storia prima dei risultati delle funzioni."""
        message: dict[str, Any] = {"role": "assistant", "content": self.content or None}
        if self.tool_calls:
            message["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": call.arguments},
                }
                for call in self.tool_calls
            ]
        return message


class RateLimiter:
    """Finestra mobile: al massimo `limit` richieste negli ultimi `period` secondi."""

    def __init__(
        self,
        limit: int,
        period: float,
        *,
        clock: Callable[[], float],
        sleep: Callable[[float], Awaitable[Any]],
    ) -> None:
        self._limit = limit
        self._period = period
        self._clock = clock
        self._sleep = sleep
        self._sent: deque[float] = deque()
        # Chi aspetta tiene il lock: le richieste partono nell'ordine in cui sono arrivate.
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = self._clock()
                while self._sent and now - self._sent[0] >= self._period:
                    self._sent.popleft()
                if len(self._sent) < self._limit:
                    self._sent.append(now)
                    return
                await self._sleep(self._period - (now - self._sent[0]))


class LLMClient:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        requests_per_minute: int = CHAT_REQUESTS_PER_MINUTE,
    ) -> None:
        headers = {
            "Authorization": f"Bearer {settings.api_key}",
            "Accept": "application/json",
            "User-Agent": f"local-llm-mcp/{__version__}",
        }
        if settings.tier:
            headers["X-Siati-Tier"] = settings.tier
        self._timeout = settings.timeout_seconds
        self._max_retries = settings.max_retries
        self._sleep = sleep
        self._chat_limiter = RateLimiter(requests_per_minute, 60.0, clock=clock, sleep=sleep)
        self._http = httpx.AsyncClient(
            base_url=settings.base_url + "/",
            headers=headers,
            timeout=httpx.Timeout(
                settings.timeout_seconds, connect=min(10.0, settings.timeout_seconds)
            ),
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def list_models(self) -> list[dict[str, Any]]:
        response = await self._request("GET", "models")
        data = _json(response)
        models = data.get("data") if isinstance(data, dict) else None
        if not isinstance(models, list):
            raise LLMError(
                "Risposta inattesa da /models: manca la lista 'data'", status=response.status_code
            )
        return [m for m in models if isinstance(m, dict) and m.get("id")]

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        max_tokens: int,
        temperature: float | None = None,
        response_format: dict[str, Any] | None = None,
        reasoning_effort: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
        parallel_tool_calls: bool | None = None,
        timeout: float | None = None,
    ) -> ChatResult:
        """timeout: secondi per questa richiesta al posto di LOCAL_LLM_TIMEOUT_SECONDS (l'agente lo
        riduce per restare nella durata massima del compito)."""
        # Solo parametri documentati: l'API rifiuta con 400 qualsiasi campo sconosciuto.
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_completion_tokens": max_tokens,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if response_format is not None:
            payload["response_format"] = response_format
        # Accettato anche dai modelli che non lo dichiarano in /models (prove del 14/11/2026).
        if reasoning_effort is not None:
            payload["reasoning_effort"] = reasoning_effort
        if tools:
            payload["tools"] = tools
            if tool_choice is not None:
                payload["tool_choice"] = tool_choice
            if parallel_tool_calls is not None:
                payload["parallel_tool_calls"] = parallel_tool_calls

        response = await self._request(
            "POST", "chat/completions", json=payload, limiter=self._chat_limiter, timeout=timeout
        )
        data = _json(response)
        request_id = response.headers.get("x-request-id") or (
            data.get("id") if isinstance(data, dict) else None
        )
        try:
            choice = data["choices"][0]
            message = choice.get("message") or {}
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(
                "Risposta inattesa dall'API: manca 'choices'", request_id=request_id
            ) from exc
        return ChatResult(
            content=message.get("content") or "",
            finish_reason=choice.get("finish_reason"),
            model=data.get("model") or model,
            request_id=request_id,
            usage=data.get("usage") or {},
            tool_calls=_tool_calls(message.get("tool_calls")),
        )

    async def _request(
        self,
        method: str,
        path: str,
        json: Any = None,
        limiter: RateLimiter | None = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        attempt = 0
        seconds = self._timeout if timeout is None else timeout
        extra = (
            {}
            if timeout is None
            else {"timeout": httpx.Timeout(timeout, connect=min(10.0, timeout))}
        )
        while True:
            # Anche ogni nuovo tentativo conta come richiesta per il limite dell'API.
            if limiter is not None:
                await limiter.acquire()
            try:
                response = await self._http.request(method, path, json=json, **extra)
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                # La richiesta non è arrivata al server: ritentare è sicuro.
                error = LLMError(
                    f"Impossibile contattare l'API ({type(exc).__name__}): controlla LOCAL_LLM_BASE_URL "
                    "e la connessione di rete",
                    retryable=True,
                )
            except httpx.TimeoutException as exc:
                raise LLMError(
                    f"Timeout: nessuna risposta entro {seconds:g} secondi. Riduci il compito, "
                    "usa un effort più basso o aumenta LOCAL_LLM_TIMEOUT_SECONDS",
                    error_type="timeout",
                ) from exc
            except httpx.HTTPError as exc:
                raise LLMError(f"Errore di trasporto HTTP ({type(exc).__name__})") from exc
            else:
                if response.status_code < 400:
                    return response
                error = _error_from_response(response)

            if not error.retryable or attempt >= self._max_retries:
                raise error
            delay = (
                error.retry_after
                if error.retry_after is not None
                else BACKOFF_BASE_SECONDS * 2**attempt
            )
            if delay > MAX_RETRY_AFTER_SECONDS:
                raise error
            attempt += 1
            await self._sleep(delay)


def _tool_calls(raw: Any) -> list[ToolCall]:
    """Chiamate di funzione valide della risposta; le voci malformate si scartano."""
    calls = []
    for index, item in enumerate(raw if isinstance(raw, list) else []):
        function = item.get("function") if isinstance(item, dict) else None
        if not isinstance(function, dict) or not isinstance(function.get("name"), str):
            continue
        arguments = function.get("arguments")
        if isinstance(arguments, dict):
            arguments = json.dumps(arguments)
        calls.append(
            ToolCall(
                id=str(item.get("id") or f"call_{index}"),
                name=function["name"],
                arguments=arguments if isinstance(arguments, str) else "",
            )
        )
    return calls


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError as exc:
        raise LLMError(
            f"L'API ha risposto {response.status_code} con un corpo non JSON",
            status=response.status_code,
            request_id=response.headers.get("x-request-id"),
        ) from exc


def _error_from_response(response: httpx.Response) -> LLMError:
    status = response.status_code
    request_id = response.headers.get("x-request-id")
    message, error_type, code, param = "", None, None, None
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            message = str(err.get("message") or "")
            error_type = err.get("type")
            code = err.get("code")
            param = err.get("param")
            request_id = request_id or err.get("request_id")
        message = message or str(body.get("message") or "")
    detail = f": {message}" if message else ""
    if param:
        detail += f" (parametro: {param})"

    kind = error_type or code
    retryable = False
    retry_after = None

    if status == 401:
        text = f"API key rifiutata (401){detail}. Controlla LOCAL_LLM_API_KEY"
    elif status == 402:
        text = f"Credito esaurito o limite di spesa raggiunto (402){detail}"
    elif status == 404 and kind == "model_not_found":
        text = f"Modello non trovato (404){detail}. Controlla LOCAL_LLM_MODEL (uv run local-llm-mcp --check)"
    elif status == 404:
        text = f"Endpoint non trovato (404){detail}. Controlla LOCAL_LLM_BASE_URL"
    elif status == 429:
        retry_after = _retry_after(response)
        wait = f", riprova tra {retry_after:g} s" if retry_after is not None else ""
        text = f"Limite di richieste superato (429){wait}{detail}"
        retryable = True
    elif status == 502:
        text = f"Nessuna macchina ha risposto (502){detail}"
        retryable = True
    elif status == 503 and kind == "model_activation_required":
        text = (
            f"Il modello non è caricato e va attivato su richiesta (503){detail}. Attivalo dal "
            "provider o imposta un altro LOCAL_LLM_MODEL"
        )
    elif status == 503:
        text = f"Modello temporaneamente non disponibile (503){detail}"
        retryable = kind in RETRYABLE_503_TYPES or kind is None
    elif status in (400, 422):
        text = f"Richiesta rifiutata dall'API ({status}){detail}"
    else:
        text = f"Errore HTTP {status}{detail}"

    return LLMError(
        text,
        status=status,
        error_type=kind,
        request_id=request_id,
        retryable=retryable,
        retry_after=retry_after,
    )


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None

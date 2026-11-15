"""Helper per simulare l'API OpenAI-compatible nei test."""

import json
from collections.abc import Callable
from typing import Any

import httpx

API_KEY = "sk-test-0123456789abcdef"

Responder = Callable[[httpx.Request], httpx.Response]


class FakeAPI:
    """Restituisce le risposte in coda, nell'ordine, e registra le richieste ricevute.

    Un elemento della coda può essere anche una funzione richiesta -> risposta, utile quando la
    risposta dipende dal contenuto (per esempio blocchi riassunti in parallelo).

    GET /models ha una coda a parte (queue_models) e le sue richieste vanno in model_requests:
    i tool a blocchi leggono l'elenco dei modelli prima di ogni altra richiesta. Con la coda
    vuota risponde 404, cioè "elenco non disponibile".
    """

    def __init__(self) -> None:
        self.responses: list[httpx.Response | Exception | Responder] = []
        self.requests: list[httpx.Request] = []
        self.model_responses: list[httpx.Response | Exception] = []
        self.model_requests: list[httpx.Request] = []

    def queue(self, *items: httpx.Response | Exception | Responder) -> None:
        self.responses.extend(items)

    def queue_models(self, *items: httpx.Response | Exception) -> None:
        self.model_responses.extend(items)

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/models"):
            return self._models(request)
        self.requests.append(request)
        if not self.responses:
            raise AssertionError(f"richiesta HTTP inattesa: {request.method} {request.url}")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        if callable(item):
            return item(request)
        return item

    def _models(self, request: httpx.Request) -> httpx.Response:
        self.model_requests.append(request)
        if not self.model_responses:
            return error_response(404, "not found")
        item = self.model_responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    @property
    def last_json(self) -> dict[str, Any]:
        return json.loads(self.requests[-1].content)


def chat_response(
    content: str | dict | None, finish_reason: str = "stop", model: str = "gemma-4-26b"
) -> httpx.Response:
    if isinstance(content, dict):
        content = json.dumps(content)
    return httpx.Response(
        200,
        headers={"X-Request-Id": "req_test123"},
        json={
            "id": "req_test123",
            "object": "chat.completion",
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": finish_reason,
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        },
    )


def tool_call_response(
    *calls: tuple[str, dict | str], model: str = "glm-5.3-flash"
) -> httpx.Response:
    """Risposta in cui il modello chiede di eseguire funzioni: (nome, argomenti) per chiamata."""
    tool_calls = [
        {
            "id": f"call_{i}",
            "type": "function",
            "function": {
                "name": name,
                "arguments": args if isinstance(args, str) else json.dumps(args),
            },
        }
        for i, (name, args) in enumerate(calls)
    ]
    return httpx.Response(
        200,
        headers={"X-Request-Id": "req_tools"},
        json={
            "id": "req_tools",
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": None, "tool_calls": tool_calls},
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
        },
    )


def models_response(model: str, parameters: list[str]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "object": "list",
            "data": [
                {
                    "id": model,
                    "context_length": 32768,
                    "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
                    "supported_parameters": parameters,
                }
            ],
        },
    )


def error_response(
    status: int,
    message: str,
    error_type: str | None = None,
    code: str | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    error: dict[str, Any] = {"message": message, "type": error_type or "invalid_request_error"}
    if code:
        error["code"] = code
    return httpx.Response(
        status, json={"error": error}, headers={"X-Request-Id": "req_err456", **(headers or {})}
    )


def models_list_response(*models: dict[str, Any]) -> httpx.Response:
    """Risposta di GET /models con i modelli dati (id, context_length, supported_parameters...)."""
    data = [
        {"object": "model", "architecture": {"output_modalities": ["text"]}, **m} for m in models
    ]
    return httpx.Response(200, json={"object": "list", "data": data})


def tool_text(result: Any) -> str:
    return result.content[0].text


def tool_json(result: Any) -> dict[str, Any]:
    return json.loads(tool_text(result))

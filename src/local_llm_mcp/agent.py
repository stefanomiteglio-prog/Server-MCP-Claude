"""Loop di function calling: il modello esplora il progetto con le funzioni di Workspace (sola
lettura) finché non risponde, entro i limiti di passi, caratteri letti e durata.

Non conosce MCP né i singoli tool: riceve system prompt, richiesta e, se serve, il formato JSON
della risposta finale.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from .client import ChatResult, LLMClient, LLMError, ToolCall
from .config import Settings
from .schemas import strip_fence
from .workspace import Workspace

logger = logging.getLogger(__name__)

Progress = Callable[[int, int, str], Awaitable[None]]
LOW_EFFORT = "low"
# Restituisce None se la risposta va bene, altrimenti il motivo (rimandato al modello).
Validator = Callable[[str], str | None]
# Sotto questo margine non si avvia una nuova richiesta: non farebbe in tempo a rispondere.
MIN_REQUEST_SECONDS = 5.0

# Messaggi (in inglese, come gli altri prompt) quando un limite ferma l'esplorazione.
STOP_NOTES = {
    "max_steps": "You have used all the exploration steps.",
    "read_budget": "You have used all the reading budget.",
    "timeout": "Time is up.",
}
FINAL_NOTE = (
    " Do not call any more functions: give your final answer now with what you have found, and "
    "say explicitly what you could not check."
)
# Ai modelli che non esplorano con response_format (Settings.json_while_exploring) il JSON si
# chiede dopo l'esplorazione, con una richiesta in più.
JSON_NOTE = "Now give your final answer as a single JSON object that follows the required schema."
REFORMAT_NOTE = (
    "Your answer is not in the required format ({reason}). Write it again as a single JSON "
    "object that follows the required schema, with the same content."
)


@dataclass
class AgentResult:
    content: str
    finish_reason: str | None
    steps: int
    stopped: str | None = None
    request_id: str | None = None
    tool_calls: int = 0
    reformatted: bool = False
    # Richieste ripetute con effort low dopo una risposta troncata.
    effort_lowered: int = 0
    usage: dict[str, int] = field(
        default_factory=lambda: {"prompt_tokens": 0, "completion_tokens": 0}
    )


class Agent:
    def __init__(
        self,
        client: LLMClient,
        settings: Settings,
        workspace: Workspace,
        *,
        effort: str,
        progress: Progress | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.client = client
        self.settings = settings
        self.workspace = workspace
        self.effort = effort
        self.progress = progress
        self.clock = clock

    async def run(
        self,
        system: str,
        user: str,
        *,
        response_format: dict[str, Any] | None = None,
        validate: Validator | None = None,
    ) -> AgentResult:
        if response_format is not None and validate is None:
            validate = _json_object_error
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        out = AgentResult(content="", finish_reason=None, steps=0)
        specs = self.workspace.tool_specs()
        # L'esplorazione finisce entro LOCAL_LLM_TASK_TIMEOUT_SECONDS; la risposta finale (con
        # l'eventuale riformattazione o il nuovo tentativo) ha al massimo un altro
        # LOCAL_LLM_TIMEOUT_SECONDS. Ogni richiesta ha come timeout il tempo che resta.
        deadline = self.clock() + self.settings.task_timeout_seconds
        final_deadline = deadline + self.settings.timeout_seconds
        max_steps = self.settings.agent_max_steps
        explore_format = response_format if self.settings.json_while_exploring else None

        while True:
            if out.steps >= max_steps:
                out.stopped = "max_steps"
            elif self.workspace.budget_left == 0:
                out.stopped = "read_budget"
            elif deadline - self.clock() < MIN_REQUEST_SECONDS:
                out.stopped = "timeout"
            if out.stopped:
                break
            try:
                result = await self._chat(
                    out, messages, specs, response_format=explore_format, until=deadline
                )
            except LLMError as exc:
                if exc.error_type != "timeout":
                    raise
                # Un passo troppo lento non butta via l'esplorazione fatta: si chiede la risposta.
                logger.info("passo %d oltre il tempo: risposta finale", out.steps)
                out.stopped = "timeout"
                break
            if not result.tool_calls:
                if response_format is not None and explore_format is None:
                    _require_answer(result)
                    messages.append(result.assistant_message())
                    messages.append({"role": "user", "content": JSON_NOTE})
                    result = await self._chat(
                        out,
                        messages,
                        specs,
                        tool_choice="none",
                        response_format=response_format,
                        until=final_deadline,
                    )
                return await self._finish(
                    out, messages, specs, result, response_format, validate, final_deadline
                )
            messages.append(result.assistant_message())
            for call in result.tool_calls:
                messages.append(
                    {"role": "tool", "tool_call_id": call.id, "content": self._execute(call)}
                )
            out.tool_calls += len(result.tool_calls)
            if self.progress is not None:
                await self.progress(out.steps, max_steps, _describe(result.tool_calls))

        logger.info("agente fermato: %s dopo %d passi", out.stopped, out.steps)
        messages.append({"role": "user", "content": STOP_NOTES[out.stopped] + FINAL_NOTE})
        result = await self._chat(
            out,
            messages,
            specs,
            tool_choice="none",
            response_format=response_format,
            until=final_deadline,
        )
        return await self._finish(
            out, messages, specs, result, response_format, validate, final_deadline
        )

    async def _finish(
        self,
        out: AgentResult,
        messages: list[dict[str, Any]],
        specs: list[dict[str, Any]],
        result: ChatResult,
        response_format: dict[str, Any] | None,
        validate: Validator | None,
        until: float,
    ) -> AgentResult:
        """Risposta finale. Se non rispetta il formato, una sola richiesta per riscriverla."""
        _require_answer(result)
        reason = validate(result.content) if validate else None
        if reason is not None:
            logger.info("risposta finale non valida (%s): nuova richiesta di formato", reason)
            messages.append(result.assistant_message())
            messages.append({"role": "user", "content": REFORMAT_NOTE.format(reason=reason)})
            result = await self._chat(
                out,
                messages,
                specs,
                tool_choice="none",
                response_format=response_format,
                until=until,
            )
            _require_answer(result)
            out.reformatted = True
        out.content = result.content
        out.finish_reason = result.finish_reason
        return out

    async def _chat(
        self,
        out: AgentResult,
        messages: list[dict[str, Any]],
        specs: list[dict[str, Any]],
        *,
        tool_choice: str | None = None,
        response_format: dict[str, Any] | None = None,
        until: float,
    ) -> ChatResult:
        async def request(effort: str) -> ChatResult:
            left = until - self.clock()
            if left < MIN_REQUEST_SECONDS:
                raise LLMError(
                    "Timeout: durata massima del compito superata (LOCAL_LLM_TASK_TIMEOUT_SECONDS "
                    "più LOCAL_LLM_TIMEOUT_SECONDS per la risposta finale). Riduci il compito o "
                    "usa un effort più basso",
                    error_type="timeout",
                )
            result = await self.client.chat(
                messages,
                model=self.settings.model,
                max_tokens=self.settings.glm_max_output_tokens,
                reasoning_effort=effort,
                tools=specs,
                tool_choice=tool_choice,
                parallel_tool_calls=True,
                response_format=response_format,
                timeout=min(self.settings.timeout_seconds, left),
            )
            add_usage(out, result)
            return result

        result = await request(self.effort)
        if result.finish_reason == "length" and self.effort != LOW_EFFORT:
            # Di solito l'output l'ha consumato il ragionamento: meglio una risposta con meno
            # ragionamento che perdere tutta l'esplorazione fatta.
            logger.info("risposta troncata al passo %d: nuovo tentativo con effort low", out.steps)
            out.effort_lowered += 1
            result = await request(LOW_EFFORT)
        logger.debug(
            "passo %d: finish=%s tool_calls=%d",
            out.steps,
            result.finish_reason,
            len(result.tool_calls),
        )
        return result

    def _execute(self, call: ToolCall) -> str:
        try:
            args = json.loads(call.arguments) if call.arguments.strip() else {}
        except ValueError:
            return f"ERROR: the arguments of {call.name} are not valid JSON"
        if not isinstance(args, dict):
            return f"ERROR: the arguments of {call.name} must be a JSON object"
        return self.workspace.call(call.name, args)


def add_usage(out: AgentResult, result: ChatResult) -> None:
    out.steps += 1
    out.request_id = result.request_id
    for key in ("prompt_tokens", "completion_tokens"):
        value = result.usage.get(key)
        if isinstance(value, int):
            out.usage[key] += value


def _require_answer(result: ChatResult) -> None:
    if result.finish_reason == "length":
        raise LLMError(
            "Risposta troncata: il modello ha esaurito il limite di output (anche il ragionamento "
            "consuma token). Riduci il compito, usa effort più basso o aumenta "
            "LOCAL_LLM_GLM_MAX_OUTPUT_TOKENS",
            request_id=result.request_id,
        )
    if not result.content.strip():
        raise LLMError("Il modello non ha dato una risposta finale", request_id=result.request_id)


def _json_object_error(content: str) -> str | None:
    try:
        value = json.loads(strip_fence(content))
    except ValueError:
        return "not valid JSON"
    return None if isinstance(value, dict) else "not a JSON object"


def _describe(calls: list[ToolCall]) -> str:
    return ", ".join(call.name for call in calls)

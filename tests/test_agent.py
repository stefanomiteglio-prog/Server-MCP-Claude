"""Loop di function calling con risposte simulate: sequenze di tool_calls, limiti, formato."""

import json

import httpx
import pytest
from helpers import FakeAPI, chat_response, tool_call_response

from local_llm_mcp.agent import Agent
from local_llm_mcp.client import LLMClient, LLMError
from local_llm_mcp.workspace import Workspace


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "rate.py").write_text("def rate(kml):\n    return 'good'\n", encoding="utf-8")
    return root.resolve()


@pytest.fixture
def run(api: FakeAPI, make_settings, project):
    async def _run(*, response_format=None, clock=None, progress=None, budget=100_000, **overrides):
        settings = make_settings(**overrides)
        client = LLMClient(settings, transport=httpx.MockTransport(api.handler))
        workspace = Workspace(root=project, max_read_chars=budget, max_file_chars=50_000)
        kwargs = {"clock": clock} if clock else {}
        agent = Agent(client, settings, workspace, effort="medium", progress=progress, **kwargs)
        try:
            result = await agent.run("system", "question", response_format=response_format)
        finally:
            await client.aclose()
        return result, workspace

    return _run


def bodies(api: FakeAPI) -> list[dict]:
    return [json.loads(r.content) for r in api.requests]


async def test_tool_calls_then_answer(api, run):
    api.queue(
        tool_call_response(("list_dir", {}), ("read_file", {"path": "rate.py"})),
        chat_response("rate.py:2 restituisce sempre 'good'"),
    )
    steps = []

    async def progress(step, total, text):
        steps.append((step, total, text))

    result, workspace = await run(progress=progress)

    assert result.content == "rate.py:2 restituisce sempre 'good'"
    assert (result.steps, result.tool_calls, result.stopped) == (2, 2, None)
    assert result.usage == {"prompt_tokens": 110, "completion_tokens": 15}
    assert workspace.files_read == {"rate.py"}
    assert steps == [(1, 25, "list_dir, read_file")]

    first, second = bodies(api)
    assert first["model"] == "glm-5.3-flash"
    assert first["reasoning_effort"] == "medium"
    assert first["parallel_tool_calls"] is True
    assert [t["function"]["name"] for t in first["tools"]] == [
        "list_dir",
        "read_file",
        "search_text",
        "file_outline",
    ]
    assert "tool_choice" not in first
    history = second["messages"]
    assert [m["role"] for m in history] == ["system", "user", "assistant", "tool", "tool"]
    assert history[2]["content"] is None
    assert [c["id"] for c in history[2]["tool_calls"]] == ["call_0", "call_1"]
    assert history[3]["tool_call_id"] == "call_0"
    assert history[4]["tool_call_id"] == "call_1"
    assert "1: def rate(kml):" in history[4]["content"]


async def test_malformed_arguments_go_back_to_the_model(api, run):
    api.queue(tool_call_response(("read_file", "{not json")), chat_response("fatto"))

    result, _ = await run()

    assert result.content == "fatto"
    tool_message = bodies(api)[1]["messages"][-1]
    assert tool_message["content"] == "ERROR: the arguments of read_file are not valid JSON"


async def test_max_steps_forces_a_final_answer(api, run):
    api.queue(
        tool_call_response(("list_dir", {})),
        tool_call_response(("list_dir", {})),
        chat_response("risposta parziale"),
    )

    result, _ = await run(agent_max_steps=2)

    assert result.content == "risposta parziale"
    assert result.stopped == "max_steps"
    assert result.steps == 3
    last = bodies(api)[-1]
    assert last["tool_choice"] == "none"
    assert last["messages"][-1]["role"] == "user"
    assert "exploration steps" in last["messages"][-1]["content"]


async def test_reading_budget_stops_exploration(api, run):
    api.queue(tool_call_response(("read_file", {"path": "rate.py"})), chat_response("ok"))

    result, workspace = await run(budget=1000)
    assert result.stopped is None

    api.queue(tool_call_response(("read_file", {"path": "rate.py"})), chat_response("ok"))
    result, workspace = await run(budget=10)
    assert result.stopped == "read_budget"
    assert workspace.budget_left == 0


class Clock:
    """Orologio finto: avanza solo quando una risposta simulata lo sposta."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def after(self, seconds: float, response):
        def respond(request):
            self.now += seconds
            if isinstance(response, Exception):
                raise response
            return response

        return respond


def read_timeout(request: httpx.Request) -> float:
    return request.extensions["timeout"]["read"]


async def test_timeout_stops_exploration(api, run):
    clock = Clock()
    api.queue(clock.after(298, tool_call_response(("list_dir", {}))), chat_response("ok"))

    result, _ = await run(clock=clock, task_timeout_seconds=300, timeout_seconds=120)

    assert result.stopped == "timeout"
    assert "Time is up" in bodies(api)[-1]["messages"][-1]["content"]
    # Ogni richiesta ha come timeout il tempo che resta: la risposta finale ha al massimo
    # LOCAL_LLM_TIMEOUT_SECONDS oltre la durata dell'esplorazione.
    assert [read_timeout(r) for r in api.requests] == [120, 120]


async def test_request_timeout_shrinks_to_the_time_left(api, run):
    clock = Clock()
    api.queue(clock.after(250, tool_call_response(("list_dir", {}))), chat_response("ok"))

    await run(clock=clock, task_timeout_seconds=300, timeout_seconds=120)

    assert [read_timeout(r) for r in api.requests] == [120, 50]


async def test_slow_step_keeps_the_exploration(api, run):
    clock = Clock()
    api.queue(
        tool_call_response(("read_file", {"path": "rate.py"})),
        clock.after(120, httpx.ReadTimeout("timed out")),
        chat_response("rate.py restituisce sempre 'good'"),
    )

    result, workspace = await run(clock=clock, task_timeout_seconds=300, timeout_seconds=120)

    assert result.stopped == "timeout"
    assert result.content == "rate.py restituisce sempre 'good'"
    assert workspace.files_read == {"rate.py"}
    final = bodies(api)[-1]
    assert final["tool_choice"] == "none"
    assert "1: def rate(kml):" in final["messages"][3]["content"]


async def test_final_answer_cannot_exceed_the_total_duration(api, run):
    clock = Clock()
    fmt = {"type": "json_object"}
    api.queue(clock.after(126, chat_response("niente JSON")))

    with pytest.raises(LLMError, match="durata massima"):
        await run(clock=clock, response_format=fmt, task_timeout_seconds=10, timeout_seconds=120)
    # Durata massima 10 + 120 s: dopo la prima risposta ne restano 4, troppo pochi per la
    # richiesta di riformattazione.
    assert len(api.requests) == 1


async def test_invalid_json_answer_is_reformatted_once(api, run):
    fmt = {"type": "json_schema", "json_schema": {"name": "x", "schema": {"type": "object"}}}
    api.queue(chat_response("Ecco la risposta: niente JSON"), chat_response({"answer": "ok"}))

    result, _ = await run(response_format=fmt)

    assert json.loads(result.content) == {"answer": "ok"}
    assert result.reformatted
    first, second = bodies(api)
    # Il formato si invia anche durante l'esplorazione: con glm-5.3-flash il modello chiama
    # comunque le funzioni e poi risponde in JSON, senza la richiesta di riscrittura (prova reale).
    assert first["response_format"] == fmt
    assert second["response_format"] == fmt
    assert second["tool_choice"] == "none"
    assert "not valid JSON" in second["messages"][-1]["content"]


async def test_json_only_at_the_end_for_models_that_skip_exploration(api, run):
    # qwen3-coder-next e qwen3.8-27b con tools + response_format rispondono subito senza leggere
    # nulla: per loro si esplora senza formato e il JSON si chiede con una richiesta finale.
    fmt = {"type": "json_schema", "json_schema": {"name": "x", "schema": {"type": "object"}}}
    api.queue(
        tool_call_response(("read_file", {"path": "rate.py"})),
        chat_response("rate.py:2 restituisce sempre 'good'"),
        chat_response({"answer": "ok"}),
    )

    result, workspace = await run(
        response_format=fmt, model="qwen3-coder-next", json_while_exploring=False
    )

    assert json.loads(result.content) == {"answer": "ok"}
    assert not result.reformatted
    assert workspace.files_read == {"rate.py"}
    first, second, third = bodies(api)
    assert "response_format" not in first and "response_format" not in second
    assert third["response_format"] == fmt
    assert third["tool_choice"] == "none"
    assert third["messages"][-2]["content"] == "rate.py:2 restituisce sempre 'good'"
    assert "JSON" in third["messages"][-1]["content"]


async def test_fenced_json_is_accepted(api, run):
    api.queue(chat_response('```json\n{"answer": "ok"}\n```'))

    result, _ = await run(response_format={"type": "json_object"})

    assert not result.reformatted


async def test_truncated_answer_is_retried_with_low_effort(api, run):
    api.queue(chat_response("", finish_reason="length"), chat_response("ok"))

    result, _ = await run()

    assert (result.content, result.effort_lowered, result.steps) == ("ok", 1, 2)
    first, second = bodies(api)
    assert (first["reasoning_effort"], second["reasoning_effort"]) == ("medium", "low")
    assert first["max_completion_tokens"] == 32768


async def test_truncated_answer_is_an_error(api, run):
    api.queue(chat_response("", finish_reason="length"), chat_response("", finish_reason="length"))

    with pytest.raises(LLMError, match="troncata"):
        await run()


async def test_empty_final_answer_is_an_error(api, run):
    api.queue(chat_response(None))

    with pytest.raises(LLMError, match="risposta finale"):
        await run()

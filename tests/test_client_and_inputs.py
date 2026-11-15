"""Client HTTP (errori, retry, timeout, rate limit) e lettura degli input da file, provati
attraverso extract_structured_data: una sola richiesta, effort low e quindi nessun nuovo tentativo
su una risposta troncata."""

import httpx
import pytest
from fastmcp.exceptions import ToolError
from helpers import chat_response, error_response, tool_json

from local_llm_mcp.client import LLMClient

SCHEMA = {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]}
ARGS = {"json_schema": SCHEMA, "text": "x vale 1"}


def ok() -> httpx.Response:
    return chat_response({"x": "1"})


async def extract(call, args=None, **settings):
    return tool_json(await call("extract_structured_data", args or ARGS, **settings))


# --- client ------------------------------------------------------------------------------


async def test_unauthorized_gives_clear_error(api, call):
    api.queue(error_response(401, "invalid api key", code="invalid_api_key"))

    with pytest.raises(
        ToolError, match=r"API key rifiutata \(401\).*LOCAL_LLM_API_KEY.*req_err456"
    ):
        await extract(call)


async def test_unknown_model_gives_clear_error(api, call):
    api.queue(error_response(404, "model not found", error_type="model_not_found"))

    with pytest.raises(ToolError, match="LOCAL_LLM_MODEL"):
        await extract(call)


async def test_rate_limit_is_retried_after_retry_after(api, call, sleeps):
    api.queue(
        error_response(
            429,
            "rate limit exceeded",
            error_type="rate_limit_exceeded",
            headers={"Retry-After": "3"},
        ),
        ok(),
    )

    assert (await extract(call))["data"] == {"x": "1"}
    assert sleeps == [3.0]
    assert len(api.requests) == 2


async def test_bad_gateway_retries_with_backoff_then_fails(api, call, sleeps):
    api.queue(*(error_response(502, "bad gateway", error_type="bad_gateway") for _ in range(3)))

    with pytest.raises(ToolError, match="502"):
        await extract(call)
    assert sleeps == [1.0, 2.0]
    assert len(api.requests) == 3


async def test_model_activation_required_is_not_retried(api, call, sleeps):
    api.queue(error_response(503, "not loaded", error_type="model_activation_required"))

    with pytest.raises(ToolError, match="non è caricato"):
        await extract(call)
    assert sleeps == []


async def test_timeout_gives_clear_error_and_no_retry(api, call):
    api.queue(httpx.ReadTimeout("timed out"))

    with pytest.raises(ToolError, match="Timeout.*LOCAL_LLM_TIMEOUT_SECONDS"):
        await extract(call, timeout_seconds=5)
    assert len(api.requests) == 1


async def test_connection_error_is_retried(api, call, sleeps):
    api.queue(httpx.ConnectError("refused"), ok())

    assert (await extract(call))["ok"]
    assert sleeps == [1.0]


async def test_tier_header_and_documented_parameters_only(api, call):
    api.queue(ok())

    await extract(call, tier="fast")

    request = api.requests[0]
    assert request.headers["X-Siati-Tier"] == "fast"
    assert set(api.last_json) == {
        "model",
        "messages",
        "max_completion_tokens",
        "response_format",
        "reasoning_effort",
    }


@pytest.fixture
def limited_client(api, make_settings):
    """Client con limite di 2 richieste al minuto e un orologio finto che avanza solo dormendo."""
    clock = {"now": 0.0, "waits": []}

    async def fake_sleep(seconds: float) -> None:
        clock["waits"].append(seconds)
        clock["now"] += seconds

    client = LLMClient(
        make_settings(),
        transport=httpx.MockTransport(api.handler),
        sleep=fake_sleep,
        clock=lambda: clock["now"],
        requests_per_minute=2,
    )
    return client, clock


async def test_chat_requests_respect_the_rate_limit(api, limited_client):
    client, clock = limited_client
    api.queue(*(chat_response(str(i)) for i in range(4)))
    messages = [{"role": "user", "content": "x"}]

    await client.chat(messages, model="m", max_tokens=10)  # t=0
    clock["now"] = 10.0
    await client.chat(messages, model="m", max_tokens=10)  # t=10
    await client.chat(messages, model="m", max_tokens=10)  # aspetta fino a t=60
    await client.chat(messages, model="m", max_tokens=10)  # aspetta fino a t=70
    await client.aclose()

    assert clock["waits"] == [50.0, 10.0]


async def test_retries_count_against_the_rate_limit(api, limited_client):
    client, clock = limited_client
    api.queue(
        chat_response("1"), error_response(502, "bad gateway", "bad_gateway"), chat_response("2")
    )
    messages = [{"role": "user", "content": "x"}]

    await client.chat(messages, model="m", max_tokens=10)
    await client.chat(messages, model="m", max_tokens=10)
    await client.aclose()

    # Backoff di 1 s dopo il 502, poi il nuovo tentativo aspetta che la finestra si liberi.
    assert clock["waits"] == [1.0, 59.0]


async def test_malformed_tool_calls_are_dropped(api, make_settings):
    raw = [
        {"id": "a", "function": {"name": "read_file", "arguments": {"path": "x"}}},
        {"id": "b", "function": {"arguments": "{}"}},
        "nonsense",
    ]
    api.queue(
        httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": None, "tool_calls": raw},
                        "finish_reason": "tool_calls",
                    }
                ]
            },
        )
    )
    client = LLMClient(make_settings(), transport=httpx.MockTransport(api.handler))

    result = await client.chat([{"role": "user", "content": "x"}], model="m", max_tokens=10)
    await client.aclose()

    assert [(c.id, c.name, c.arguments) for c in result.tool_calls] == [
        ("a", "read_file", '{"path": "x"}')
    ]


# --- input da file -----------------------------------------------------------------------


async def test_text_from_file_in_allowed_root(api, call, tmp_path):
    source = tmp_path / "fattura.txt"
    source.write_text("Fattura numero 17\n", encoding="utf-8")
    api.queue(ok())

    await extract(
        call,
        {"json_schema": SCHEMA, "path": str(source)},
        allowed_read_roots=(tmp_path.resolve(),),
    )

    assert "Fattura numero 17" in api.last_json["messages"][1]["content"]


async def test_file_reading_disabled_without_roots(api, call, tmp_path):
    source = tmp_path / "a.txt"
    source.write_text("ciao", encoding="utf-8")

    with pytest.raises(ToolError, match="LOCAL_LLM_ALLOWED_READ_ROOTS"):
        await extract(call, {"json_schema": SCHEMA, "path": str(source)})
    assert api.requests == []


async def test_file_outside_allowed_roots_is_rejected(api, call, tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("segreto aziendale", encoding="utf-8")

    with pytest.raises(ToolError, match="fuori dalle cartelle consentite") as exc:
        await extract(
            call,
            {"json_schema": SCHEMA, "path": str(outside)},
            allowed_read_roots=(allowed.resolve(),),
        )
    # Le cartelle consentite sono nel messaggio: Claude può scegliere subito un percorso valido.
    assert str(allowed.resolve()) in str(exc.value)
    assert api.requests == []


async def test_env_file_is_never_read(api, call, tmp_path):
    env = tmp_path / ".env"
    env.write_text("API_KEY=sk-real", encoding="utf-8")

    with pytest.raises(ToolError, match="escluso per sicurezza"):
        await extract(
            call, {"json_schema": SCHEMA, "path": str(env)}, allowed_read_roots=(tmp_path,)
        )
    assert api.requests == []


async def test_text_and_path_together_are_rejected(call, tmp_path):
    with pytest.raises(ToolError, match="non entrambi"):
        await extract(call, {**ARGS, "path": str(tmp_path / "a.txt")})


async def test_missing_input_is_rejected(call):
    with pytest.raises(ToolError, match="oppure 'path'"):
        await extract(call, {"json_schema": SCHEMA})


async def test_file_over_limit_names_the_path_parameter(api, call, tmp_path):
    source = tmp_path / "big.txt"
    source.write_text("x" * 1500, encoding="utf-8")

    with pytest.raises(ToolError, match="'path' troppo lungo"):
        await extract(
            call,
            {"json_schema": SCHEMA, "path": str(source)},
            allowed_read_roots=(tmp_path.resolve(),),
            document_max_chars=1000,
        )
    assert api.requests == []

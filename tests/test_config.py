import os

import pytest

from local_llm_mcp.config import DEFAULT_CALL_LOG, ConfigError, Settings, obsolete_variables

BASE = {"LOCAL_LLM_BASE_URL": "https://api.test/v1/", "LOCAL_LLM_API_KEY": "sk-test"}


def test_defaults():
    s = Settings.from_env(BASE)

    assert s.base_url == "https://api.test/v1"
    assert s.tier is None
    assert s.document_max_chars == 600_000
    assert s.allowed_read_roots == ()
    assert s.call_log == DEFAULT_CALL_LOG
    assert s.call_log_days == 30
    assert "sk-test" not in repr(s)


def test_overrides(tmp_path):
    other = tmp_path / "b"
    other.mkdir()
    s = Settings.from_env(
        {
            **BASE,
            "LOCAL_LLM_TIER": "fast",
            "LOCAL_LLM_TIMEOUT_SECONDS": "30",
            "LOCAL_LLM_DOCUMENT_MAX_CHARS": "1000",
            "LOCAL_LLM_ALLOWED_READ_ROOTS": f"{tmp_path}{os.pathsep}{other}",
            "LOCAL_LLM_LOG_LEVEL": "debug",
            "LOCAL_LLM_CALL_LOG": str(tmp_path / "calls.sqlite3"),
            "LOCAL_LLM_CALL_LOG_DAYS": "7",
        }
    )

    assert s.tier == "fast"
    assert s.timeout_seconds == 30.0
    assert s.document_max_chars == 1000
    assert s.allowed_read_roots == (tmp_path.resolve(), other.resolve())
    assert s.log_level == "DEBUG"
    assert s.call_log == tmp_path / "calls.sqlite3"
    assert s.call_log_days == 7


def test_glm_agent_settings():
    s = Settings.from_env(BASE)
    assert (s.model, s.default_effort, s.glm_max_output_tokens) == (
        "glm-5.3-flash",
        "medium",
        32768,
    )
    assert (s.agent_max_steps, s.agent_max_read_chars, s.task_timeout_seconds) == (25, 200_000, 300)

    s = Settings.from_env(
        {
            **BASE,
            "LOCAL_LLM_MODEL": "glm-5.4",
            "LOCAL_LLM_DEFAULT_EFFORT": "HIGH",
            "LOCAL_LLM_AGENT_MAX_STEPS": "5",
            "LOCAL_LLM_AGENT_MAX_READ_CHARS": "50000",
            "LOCAL_LLM_TASK_TIMEOUT_SECONDS": "60",
        }
    )
    assert (s.model, s.default_effort) == ("glm-5.4", "high")
    assert (s.agent_max_steps, s.agent_max_read_chars, s.task_timeout_seconds) == (5, 50_000, 60)


@pytest.mark.parametrize("value", ["off", "OFF", "no", "false", "0"])
def test_call_log_can_be_disabled(value):
    assert Settings.from_env({**BASE, "LOCAL_LLM_CALL_LOG": value}).call_log is None


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"LOCAL_LLM_API_KEY": "sk-test"}, "LOCAL_LLM_BASE_URL"),
        ({"LOCAL_LLM_BASE_URL": "https://api.test/v1"}, "LOCAL_LLM_API_KEY"),
        ({**BASE, "LOCAL_LLM_BASE_URL": "api.test/v1"}, "http"),
        ({**BASE, "LOCAL_LLM_TIER": "turbo"}, "LOCAL_LLM_TIER"),
        ({**BASE, "LOCAL_LLM_TIMEOUT_SECONDS": "abc"}, "LOCAL_LLM_TIMEOUT_SECONDS"),
        ({**BASE, "LOCAL_LLM_DEFAULT_EFFORT": "xhigh"}, "LOCAL_LLM_DEFAULT_EFFORT"),
        ({**BASE, "LOCAL_LLM_AGENT_MAX_STEPS": "0"}, "LOCAL_LLM_AGENT_MAX_STEPS"),
        ({**BASE, "LOCAL_LLM_GLM_MAX_OUTPUT_TOKENS": "40000"}, "32768"),
        ({**BASE, "LOCAL_LLM_DOCUMENT_MAX_CHARS": "abc"}, "LOCAL_LLM_DOCUMENT_MAX_CHARS"),
        ({**BASE, "LOCAL_LLM_AGENT_MAX_READ_CHARS": "10"}, "LOCAL_LLM_AGENT_MAX_READ_CHARS"),
        ({**BASE, "LOCAL_LLM_TASK_TIMEOUT_SECONDS": "1"}, "LOCAL_LLM_TASK_TIMEOUT_SECONDS"),
        ({**BASE, "LOCAL_LLM_ALLOWED_READ_ROOTS": "relativo/cartella"}, "assoluto"),
        ({**BASE, "LOCAL_LLM_LOG_LEVEL": "LOUD"}, "LOCAL_LLM_LOG_LEVEL"),
        ({**BASE, "LOCAL_LLM_CALL_LOG": "relativo/calls.sqlite3"}, "assoluto"),
        ({**BASE, "LOCAL_LLM_CALL_LOG_DAYS": "0"}, "LOCAL_LLM_CALL_LOG_DAYS"),
    ],
)
def test_invalid_configuration(env, message):
    with pytest.raises(ConfigError, match=message):
        Settings.from_env(env)


def test_missing_root_directory(tmp_path):
    with pytest.raises(ConfigError, match="non esiste"):
        Settings.from_env({**BASE, "LOCAL_LLM_ALLOWED_READ_ROOTS": str(tmp_path / "manca")})


def test_obsolete_variables_are_ignored_and_listed():
    env = {
        **BASE,
        "LOCAL_LLM_DEFAULT_MODEL": "gemma-4-26b",
        "LOCAL_LLM_MAX_OUTPUT_TOKENS": "8192",
        "LOCAL_LLM_MAX_CHUNKS": "0",
    }

    s = Settings.from_env(env)

    assert s.glm_max_output_tokens == 32768
    assert obsolete_variables(env) == [
        "LOCAL_LLM_DEFAULT_MODEL",
        "LOCAL_LLM_MAX_CHUNKS",
        "LOCAL_LLM_MAX_OUTPUT_TOKENS",
    ]
    assert obsolete_variables(BASE) == []


GLM_PARAMS = ["tools", "tool_choice", "response_format", "reasoning_effort"]


def test_check_model_accepts_glm_without_warnings():
    from local_llm_mcp.server import check_model

    model = {"id": "glm-5.3-flash", "context_length": 262144, "supported_parameters": GLM_PARAMS}

    assert check_model(model, Settings.from_env(BASE)) == ([], [])


def test_check_model_only_warns_without_reasoning_effort():
    from local_llm_mcp.server import check_model

    model = {"context_length": 262144, "supported_parameters": ["tools", "response_format"]}

    missing, warnings = check_model(model, Settings.from_env(BASE))

    assert missing == []
    assert len(warnings) == 1 and "reasoning_effort" in warnings[0]


def test_check_model_requires_tools_and_response_format():
    from local_llm_mcp.server import check_model

    model = {"context_length": 262144, "supported_parameters": ["reasoning_effort"]}

    missing, _ = check_model(model, Settings.from_env(BASE))

    assert missing == ["tools", "response_format"]


def test_check_model_warns_when_limits_exceed_the_context():
    from local_llm_mcp.server import check_model

    model = {"context_length": 65536, "supported_parameters": GLM_PARAMS}

    _, warnings = check_model(model, Settings.from_env(BASE))

    assert len(warnings) == 1
    assert "LOCAL_LLM_DOCUMENT_MAX_CHARS e LOCAL_LLM_AGENT_MAX_READ_CHARS" in warnings[0]
    assert "98000 caratteri" in warnings[0]
    small = Settings.from_env(
        {**BASE, "LOCAL_LLM_DOCUMENT_MAX_CHARS": "90000", "LOCAL_LLM_AGENT_MAX_READ_CHARS": "90000"}
    )
    assert check_model(model, small) == ([], [])


def test_json_while_exploring_defaults_by_model():
    assert Settings.from_env(BASE).json_while_exploring is True
    other = {**BASE, "LOCAL_LLM_MODEL": "qwen3-coder-next"}
    assert Settings.from_env(other).json_while_exploring is False
    forced = {**other, "LOCAL_LLM_JSON_WHILE_EXPLORING": "true"}
    assert Settings.from_env(forced).json_while_exploring is True
    with pytest.raises(ConfigError, match="LOCAL_LLM_JSON_WHILE_EXPLORING"):
        Settings.from_env({**BASE, "LOCAL_LLM_JSON_WHILE_EXPLORING": "forse"})


def test_check_model_warns_when_output_tokens_exceed_the_context():
    from local_llm_mcp.server import check_model

    model = {"context_length": 16384, "supported_parameters": GLM_PARAMS}

    _, warnings = check_model(model, Settings.from_env(BASE))

    assert len(warnings) == 1
    assert "LOCAL_LLM_GLM_MAX_OUTPUT_TOKENS (32768)" in warnings[0]

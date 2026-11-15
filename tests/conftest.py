"""Fixture comuni. Nessun test chiama l'API reale: tutte le richieste passano da httpx.MockTransport."""

from typing import Any

import httpx
import pytest
from fastmcp import Client
from helpers import API_KEY, FakeAPI

from local_llm_mcp.client import LLMClient
from local_llm_mcp.config import Settings
from local_llm_mcp.server import create_server


@pytest.fixture
def api() -> FakeAPI:
    return FakeAPI()


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def make_settings():
    def _make(**overrides: Any) -> Settings:
        values: dict[str, Any] = {"base_url": "https://api.test/v1", "api_key": API_KEY}
        values.update(overrides)
        return Settings(**values)

    return _make


@pytest.fixture
def call(api: FakeAPI, make_settings, sleeps: list[float]):
    """call(tool, args, **settings) esegue il tool tramite un client MCP in memoria."""

    async def _call(tool: str, args: dict[str, Any] | None = None, **overrides: Any) -> Any:
        settings = make_settings(**overrides)

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        client = LLMClient(settings, transport=httpx.MockTransport(api.handler), sleep=fake_sleep)
        async with Client(create_server(settings, client)) as mcp_client:
            return await mcp_client.call_tool(tool, args or {})

    return _call

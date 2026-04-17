"""Tests for Nova LLM client — config-based client creation, tool formatting."""

import os
import pytest

from nova.llmcore import (
    LLMClient, AnthropicSession, OpenAISession,
    ToolCall, LLMResponse, create_client_from_config,
)


class TestCreateClientFromConfig:

    def test_anthropic_key_creates_anthropic_session(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        client = create_client_from_config()
        assert isinstance(client.backend, AnthropicSession)

    def test_openai_key_creates_openai_session(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        client = create_client_from_config()
        assert isinstance(client.backend, OpenAISession)

    def test_openrouter_key_creates_openai_session(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
        client = create_client_from_config()
        assert isinstance(client.backend, OpenAISession)
        assert client.backend.base_url == "https://openrouter.ai/api/v1"

    def test_no_key_raises(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        with pytest.raises(ValueError, match="No API key"):
            create_client_from_config()


class TestToolFormatting:

    def test_format_tools_converts_schema(self):
        client = LLMClient(AnthropicSession({'api_key': 'test'}))
        schema = [
            {'name': 'code_run', 'description': 'Run code', 'parameters': {'type': 'object'}}
        ]
        formatted = client._format_tools(schema)
        assert len(formatted) == 1
        assert formatted[0]['function']['name'] == 'code_run'
        assert formatted[0]['type'] == 'function'

    def test_format_tools_passes_through_existing_format(self):
        client = LLMClient(AnthropicSession({'api_key': 'test'}))
        schema = [
            {'type': 'function', 'function': {'name': 'test', 'description': 'desc', 'parameters': {}}}
        ]
        formatted = client._format_tools(schema)
        assert formatted[0]['function']['name'] == 'test'

    def test_format_tools_empty_returns_none(self):
        client = LLMClient(AnthropicSession({'api_key': 'test'}))
        assert client._format_tools(None) is None
        assert client._format_tools([]) is None


class TestToolCall:

    def test_tool_call_fields(self):
        tc = ToolCall(id="1", name="code_run", arguments='{"code": "print(1)"}')
        assert tc.id == "1"
        assert tc.function.name == "code_run"
        assert tc.function.arguments == '{"code": "print(1)"}'


class TestLLMResponse:

    def test_response_fields(self):
        resp = LLMResponse(content="text", tool_calls=[ToolCall("1", "test", '{}')])
        assert resp.content == "text"
        assert len(resp.tool_calls) == 1

    def test_response_defaults(self):
        resp = LLMResponse()
        assert resp.content == ""
        assert resp.tool_calls == []
"""Tests for Nova system prompt builder."""

import os
import tempfile

import pytest

from nova.context.system_prompt import build_system_prompt
from nova.memory.engine import NovaMemory


@pytest.fixture
def prompt_memory(tmp_path):
    """NovaMemory for prompt building tests."""
    db_path = os.path.join(str(tmp_path), '.nova', 'nova.db')
    os.makedirs(os.path.join(str(tmp_path), '.nova'), exist_ok=True)
    mem = NovaMemory(db_path)
    yield mem
    mem.close()


class TestBuildSystemPrompt:

    def test_includes_role(self, prompt_memory):
        prompt = build_system_prompt(prompt_memory)
        assert "Nova Agent" in prompt or "Self-Evolving" in prompt

    def test_includes_memory_system(self, prompt_memory):
        prompt = build_system_prompt(prompt_memory)
        assert "Memory System" in prompt or "unified" in prompt.lower() or "project" in prompt.lower()

    def test_includes_tool_reference(self, prompt_memory):
        prompt = build_system_prompt(prompt_memory)
        assert "db_query" in prompt
        assert "wiki_query" in prompt
        assert "fact_search" in prompt

    def test_includes_memory_stats(self, prompt_memory):
        prompt = build_system_prompt(prompt_memory)
        assert "Memory Stats" in prompt or "wiki" in prompt.lower()

    def test_includes_timestamp(self, prompt_memory):
        prompt = build_system_prompt(prompt_memory)
        # Timestamp format: "Today: 2026-..."
        assert "Today:" in prompt or "2026" in prompt

    def test_includes_autonomous_section(self, prompt_memory):
        prompt = build_system_prompt(prompt_memory)
        assert "AUTONOMOUS" in prompt

    def test_includes_cron_section(self, prompt_memory):
        prompt = build_system_prompt(prompt_memory)
        assert "cron" in prompt.lower() or "Cron" in prompt
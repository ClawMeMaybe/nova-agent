"""Tests for Nova autonomous monitor — idle detection, prompt building."""

import time
import tempfile
import os

import pytest

from nova.autonomous import AutonomousMonitor, IDLE_THRESHOLD, CHECK_INTERVAL
from nova.memory.engine import NovaMemory


class MockAgent:
    """Minimal agent mock for testing autonomous monitor."""
    def __init__(self, tmpdir):
        db_path = os.path.join(tmpdir, '.nova', 'nova.db')
        os.makedirs(os.path.join(tmpdir, '.nova'), exist_ok=True)
        self.memory = NovaMemory(db_path)
        self.is_running = False
        self._put_task_args = None

    def put_task(self, query, source="user"):
        self._put_task_args = (query, source)
        return None


@pytest.fixture
def mock_agent(tmp_path):
    os.makedirs(os.path.join(tmp_path, '.nova'), exist_ok=True)
    return MockAgent(str(tmp_path))


class TestIdleThreshold:

    def test_idle_threshold_is_30min(self):
        assert IDLE_THRESHOLD == 1800

    def test_check_interval_is_10min(self):
        assert CHECK_INTERVAL == 600

    def test_mark_activity_resets_timer(self, mock_agent):
        monitor = AutonomousMonitor(mock_agent)
        monitor._last_activity = time.time() - 1000
        monitor.mark_activity()
        idle = time.time() - monitor._last_activity
        assert idle < 5


class TestPromptBuilding:

    def test_prompt_with_todo(self, mock_agent):
        # Create autonomous-todo wiki page in global memory
        mock_agent.memory.wiki_ingest(
            "autonomous-todo",
            "1. Review memory for stale facts\n2. Check environment setup",
            "autonomous,todo",
            category="decision"
        )
        monitor = AutonomousMonitor(mock_agent)
        prompt = monitor._build_autonomous_prompt()
        assert "[AUTONOMOUS MODE]" in prompt
        assert "Existing TODO" in prompt
        assert "Review memory" in prompt

    def test_prompt_without_todo(self, tmp_path):
        # Fresh agent with no autonomous-todo
        os.makedirs(os.path.join(tmp_path, '.nova'), exist_ok=True)
        agent = MockAgent(str(tmp_path))
        monitor = AutonomousMonitor(agent)
        prompt = monitor._build_autonomous_prompt()
        assert "[AUTONOMOUS MODE]" in prompt
        assert "No existing TODO" in prompt
        assert "value formula" in prompt.lower() or "priority" in prompt.lower()

    def test_prompt_includes_memory_stats(self, mock_agent):
        monitor = AutonomousMonitor(mock_agent)
        prompt = monitor._build_autonomous_prompt()
        assert "Memory Stats" in prompt
        assert "wiki" in prompt.lower()


class TestTaskInjection:

    def test_trigger_injects_autonomous_task(self, mock_agent):
        monitor = AutonomousMonitor(mock_agent)
        monitor._trigger_autonomous()
        assert mock_agent._put_task_args is not None
        query, source = mock_agent._put_task_args
        assert source == "autonomous"
        assert "[AUTONOMOUS MODE]" in query

    def test_trigger_resets_activity(self, mock_agent):
        monitor = AutonomousMonitor(mock_agent)
        monitor._last_activity = time.time() - 5000
        monitor._trigger_autonomous()
        idle = time.time() - monitor._last_activity
        assert idle < 5
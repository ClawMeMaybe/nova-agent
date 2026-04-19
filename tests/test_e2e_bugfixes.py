"""Tests for the three E2E evolution bug fixes:
1. Skill success rate auto-update on session completion
2. Link name auto-fill from DB when LLM provides only IDs
3. Clean display output (no handler history pollution)
"""

import os
import tempfile

from nova.memory.engine import NovaMemory
from nova.tools.handler import NovaHandler


# ── US-002: Link Name Auto-Fill ──

class TestLinkNameAutoFill:

    def test_resolve_fact_name(self, memory):
        fid = memory.fact_add("Flask runs on port 5000 by default", category="environment", tags="flask")
        parent = _MockParent(memory)
        handler = NovaHandler(parent, cwd=tempfile.mkdtemp())
        name = handler._resolve_link_name('fact', fid, '')
        assert name == "Flask runs on port 5000 by default"

    def test_resolve_skill_name(self, memory):
        sid = memory.skill_add("deploy-flask", "Deploy Flask app", ["1. Set port"], triggers="flask,deploy", tags="flask")
        parent = _MockParent(memory)
        handler = NovaHandler(parent, cwd=tempfile.mkdtemp())
        name = handler._resolve_link_name('skill', sid, '')
        assert name == "deploy-flask"

    def test_resolve_wiki_name(self, memory):
        wid = memory.wiki_add('test-arch', 'Test Architecture', 'Content here', category='pattern', tags='test')
        parent = _MockParent(memory)
        handler = NovaHandler(parent, cwd=tempfile.mkdtemp())
        name = handler._resolve_link_name('wiki', wid, '')
        assert name == "Test Architecture"

    def test_resolve_preserves_existing_name(self, memory):
        parent = _MockParent(memory)
        handler = NovaHandler(parent, cwd=tempfile.mkdtemp())
        name = handler._resolve_link_name('fact', 1, 'already-set')
        assert name == 'already-set'

    def test_resolve_returns_empty_for_missing_id(self, memory):
        parent = _MockParent(memory)
        handler = NovaHandler(parent, cwd=tempfile.mkdtemp())
        name = handler._resolve_link_name('fact', 99999, '')
        assert name == ''

    def test_resolve_returns_empty_for_none_id(self, memory):
        parent = _MockParent(memory)
        handler = NovaHandler(parent, cwd=tempfile.mkdtemp())
        name = handler._resolve_link_name('fact', None, '')
        assert name == ''

    def test_resolve_memory(self, memory):
        fid = memory.fact_add("Global pattern fact", category="pattern", tags="test")
        parent = _MockParent(memory)
        handler = NovaHandler(parent, cwd=tempfile.mkdtemp())
        name = handler._resolve_link_name('fact', fid, '')
        assert "Global pattern fact" in name

    def test_resolve_fact_name_truncated_to_60(self, memory):
        long_content = "A" * 120
        fid = memory.fact_add(long_content, category="general")
        parent = _MockParent(memory)
        handler = NovaHandler(parent, cwd=tempfile.mkdtemp())
        name = handler._resolve_link_name('fact', fid, '')
        assert len(name) == 60


# ── US-001: Skill Success Rate Auto-Update ──

class TestSkillSuccessAutoUpdate:

    def test_skill_update_success_increases_rate(self, memory):
        memory.skill_add("test-skill", "Test", ["1. Step"], triggers="test", tags="test")
        initial_rate = memory._conn.execute("SELECT success_rate FROM skills WHERE name='test-skill'").fetchone()['success_rate']
        memory.skill_update_success("test-skill", success=True)
        new_rate = memory._conn.execute("SELECT success_rate FROM skills WHERE name='test-skill'").fetchone()['success_rate']
        assert new_rate > initial_rate

    def test_skill_update_failure_decreases_rate(self, memory):
        memory.skill_add("fail-skill", "Test", ["1. Step"], triggers="test", tags="test")
        initial_rate = memory._conn.execute("SELECT success_rate FROM skills WHERE name='fail-skill'").fetchone()['success_rate']
        memory.skill_update_success("fail-skill", success=False)
        new_rate = memory._conn.execute("SELECT success_rate FROM skills WHERE name='fail-skill'").fetchone()['success_rate']
        assert new_rate < initial_rate

    def test_skill_update_single_db(self, memory):
        memory.skill_add("tier-skill", "Test", ["1. Step"], triggers="tier")
        memory.skill_update_success("tier-skill", success=True)
        rate = memory._conn.execute("SELECT success_rate FROM skills WHERE name='tier-skill'").fetchone()['success_rate']
        assert rate > 0.5

    def test_skill_update_nonexistent_skill_no_error(self, memory):
        memory.skill_update_success("nonexistent-skill", success=True)


# ── US-003: Clean Display Output ──

class TestCleanDisplayOutput:
    """Verify that main.py no longer prepends handler history to display output.
    Tested via reading the source — the integration test requires real LLM calls."""

    def test_main_py_does_not_prepend_history(self):
        from nova import main
        import inspect
        source = inspect.getsource(main.NovaAgent.run)
        # Should NOT contain the old pattern of prepending handler history
        assert "handler_summary" not in source or "full_resp" not in source.split("handler_summary")[0].split("\n")[-1]
        # Should contain the new pattern: saving history separately
        assert "self.history = handler.history_info" in source
        # Should NOT contain the old line that prepended history to display output
        assert "'\n'.join(handler_summary) + '\n\n' + full_resp" not in source

    def test_skill_success_update_after_session(self):
        from nova import main
        import inspect
        source = inspect.getsource(main.NovaAgent.run)
        # Should contain skill success rate update logic
        assert "skill_update_success" in source


# ── Helper ──

class _MockParent:
    """Minimal mock to create NovaHandler with real memory."""
    def __init__(self, memory):
        self.memory = memory
        self.events = None
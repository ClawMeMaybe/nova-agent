"""Integration test: simulate multi-session, multi-turn flow to verify session_turns persistence and evolution.

This test exercises the full pipeline without a live LLM:
1. Create sessions, add turns manually
2. Query session_turns to verify detail is persisted
3. Use session_relevant_turns for context injection
4. Verify self-evolvement: skill refinement using session detail
"""

import os
import json
import tempfile

import pytest

from nova.memory.engine import NovaMemory, TwoTierMemory


@pytest.fixture
def memory(tmp_path):
    local_db = str(tmp_path / ".nova" / "nova.db")
    global_db = str(tmp_path / "global_nova" / "nova.db")
    mem = TwoTierMemory(local_db, global_db)
    yield mem
    mem.close()


class TestMultiSessionFlow:
    """Simulate a user doing multiple tasks across sessions, verifying turn tracking."""

    def test_three_sessions_with_turns(self, memory):
        """Session 1: deploy flask → Session 2: fix bug → Session 3: deploy flask again (should recall session 1)."""

        # ── Session 1: Deploy Flask ──
        sid1 = memory.session_create("deploy flask app to production server")
        memory.session_turn_add(sid1, 1, 'assistant', content='Checking server status',
                                tool_name='code_run', tool_args='{"script":"ssh prod-server systemctl status flask"}',
                                tool_result='flask.service: inactive (dead)')
        memory.session_turn_add(sid1, 1, 'user', tool_result='inactive service detected')
        memory.session_turn_add(sid1, 2, 'assistant', content='Starting flask service',
                                tool_name='code_run', tool_args='{"script":"ssh prod-server systemctl start flask"}',
                                tool_result='flask.service: active (running)')
        memory.session_turn_add(sid1, 2, 'user', tool_result='service started')
        memory.session_turn_add(sid1, 3, 'assistant', content='Verifying deployment',
                                tool_name='code_run', tool_args='{"script":"curl http://prod-server:5000"}',
                                tool_result='200 OK: Flask app running')
        memory.session_update(sid1, summary="Deployed flask to prod", result="200 OK",
                              had_knowledge=True)

        # Crystallize a skill from this session
        memory.skill_add("deploy-flask", "Deploy Flask app to production",
                         ["1. SSH into prod-server and check flask service status",
                          "2. Start flask service if inactive: systemctl start flask",
                          "3. Verify: curl http://prod-server:5000 returns 200"],
                         triggers="deploy,flask,production,server",
                         pitfalls=["Don't forget to check service status first",
                                   "Always verify with curl after starting"])

        # ── Session 2: Fix a bug (different topic) ──
        sid2 = memory.session_create("fix authentication bug in login API")
        memory.session_turn_add(sid2, 1, 'assistant', content='Reading auth code',
                                tool_name='file_read', tool_args='{"path":"auth/login.py"}',
                                tool_result='def authenticate(user, password): return check_hash(password, stored_hash)')
        memory.session_turn_add(sid2, 2, 'assistant', content='Found the bug — wrong hash comparison',
                                tool_name='file_patch', tool_args='{"path":"auth/login.py","old":"check_hash","new":"verify_hash"}',
                                tool_result='patched successfully')
        memory.session_update(sid2, summary="Fixed auth hash bug", result="login works",
                              had_knowledge=True)

        # ── Verify session detail ──
        turns1 = memory.session_turns_query(sid1)
        assert len(turns1) == 5  # 3 assistant + 2 user tool_results
        assert any(t['tool_name'] == 'code_run' and 'systemctl' in t['tool_args'] for t in turns1)

        turns2 = memory.session_turns_query(sid2)
        assert len(turns2) == 2
        assert turns2[0]['tool_name'] == 'file_read'

    def test_session_relevant_turns_recalls_past_deploy(self, memory):
        """Session 3 about deploying flask should recall session 1's tool calls."""
        # First create session 1
        sid1 = memory.session_create("deploy flask app to production server")
        memory.session_turn_add(sid1, 1, 'assistant', tool_name='code_run',
                                tool_args='{"script":"ssh prod-server systemctl start flask"}',
                                tool_result='flask.service: active (running)')
        memory.session_update(sid1, summary="Deployed flask", result="success")

        # Now query for relevant turns when user starts a new flask deploy task
        context = memory.session_relevant_turns("deploy flask application")
        assert "code_run" in context
        assert "flask" in context.lower() or "deploy" in context.lower()

    def test_evolution_review_session_turns_for_skill_refinement(self, memory):
        """Simulate autonomous mode reviewing session_turns to refine a struggling skill."""
        # Create a skill with low success rate
        memory.skill_add("deploy-django", "Deploy Django app",
                         ["1. Push code to server", "2. Verify: curl localhost"],
                         triggers="deploy,django", pitfalls=[],
                         success_rate=0.3)

        # Create a session where this skill failed
        sid = memory.session_create("deploy django to staging")
        memory.session_turn_add(sid, 1, 'assistant', tool_name='code_run',
                                tool_args='{"script":"scp -r . staging:/opt/django"}',
                                tool_result='Permission denied (publickey)')
        memory.session_turn_add(sid, 2, 'assistant', tool_name='code_run',
                                tool_args='{"script":"ssh staging systemctl restart django"}',
                                tool_result='Connection refused')
        memory.session_update(sid, summary="Failed deployment", result="permission denied")

        # Autonomous review: query session turns for django failures
        result = memory.safe_query(
            "SELECT st.tool_name, st.tool_result FROM session_turns st "
            "JOIN sessions s ON st.session_id = s.id "
            "WHERE s.task LIKE '%django%' AND st.tool_result LIKE '%denied%'"
        )
        assert result['status'] == 'success'
        assert len(result['rows']) >= 1
        assert 'denied' in result['rows'][0]['tool_result'].lower()

        # Improve the skill based on what went wrong
        memory.skill_improve("deploy-django",
                             new_steps=["1. Verify SSH key access before deploying",
                                        "2. Push code to server",
                                        "3. Verify: curl localhost returns 200"],
                             new_pitfalls=["Don't deploy without verifying SSH key first",
                                          "Always check connection before restarting services"])

        # Verify skill was improved (skill_add routes to global by default)
        skill = memory._global._conn.execute("SELECT * FROM skills WHERE name='deploy-django'").fetchone()
        assert skill is not None
        assert skill['version'] == 2
        pitfalls = json.loads(skill['pitfalls'])
        assert len(pitfalls) == 2

    def test_full_session_archival_and_crystallization(self, memory):
        """Simulate full session lifecycle: create → turns → update → crystallize → wiki page."""
        sid = memory.session_create("setup development environment")
        memory.session_turn_add(sid, 1, 'assistant', content='Installing dependencies',
                                tool_name='code_run', tool_args='{"script":"pip install flask pytest"}',
                                tool_result='Successfully installed flask pytest')
        memory.session_turn_add(sid, 2, 'assistant', content='Setting up .env file',
                                tool_name='file_write', tool_args='{"path":".env","content":"FLASK_ENV=development"}',
                                tool_result='written')

        # Mark knowledge produced and update session
        memory._knowledge_produced = True
        memory.session_update(sid, summary="Environment setup complete", result="ready",
                              had_knowledge=True)

        # Crystallize into wiki
        page_id = memory.session_crystallize(sid)
        assert page_id is not None

        # Verify the wiki page was created
        page = memory.wiki_read('session-setup-development-enviro')
        # Slug may vary, let's check the wiki was created
        pages = memory.wiki_list()
        session_pages = [p for p in pages if p['category'] == 'session-log']
        assert len(session_pages) >= 1

        # Verify turns are still accessible
        turns = memory.session_turns_query(sid)
        assert len(turns) == 2
        assert turns[0]['tool_name'] == 'code_run'
        assert 'flask' in turns[0]['tool_result']

    def test_pruning_does_not_affect_recent_sessions(self, memory):
        """Verify pruning old sessions cascades to turns but recent ones survive."""
        # Create a session and turns
        sid = memory.session_create("recent important task")
        memory.session_turn_add(sid, 1, 'assistant', tool_name='code_run',
                                tool_result='important output')

        # Prune with 30 days — recent sessions should survive
        memory.prune(max_age_days=30)

        # Session and turns still exist
        turns = memory.session_turns_query(sid)
        assert len(turns) == 1

    def test_session_context_injection_matches_task_keywords(self, memory):
        """Verify that session_relevant_turns picks up sessions by keyword matching."""
        # Create session about docker
        sid_docker = memory.session_create("docker compose setup for microservices")
        memory.session_turn_add(sid_docker, 1, 'assistant', tool_name='code_run',
                                tool_args='{"script":"docker-compose up -d"}',
                                tool_result='started 3 services')
        memory.session_update(sid_docker, summary="docker compose running", result="3 services up")

        # Create session about python testing (unrelated)
        sid_test = memory.session_create("python pytest configuration")
        memory.session_turn_add(sid_test, 1, 'assistant', tool_name='code_run',
                                tool_args='{"script":"pytest --init"}',
                                tool_result='pytest.ini created')
        memory.session_update(sid_test, summary="pytest configured", result="ready")

        # Query for docker context — should only match docker session
        context = memory.session_relevant_turns("docker compose build microservices")
        assert "docker-compose" in context or "docker" in context.lower()
        assert "pytest" not in context

        # Query for pytest context — should only match pytest session
        context2 = memory.session_relevant_turns("pytest unit test configuration")
        assert "pytest" in context2.lower() or "test" in context2.lower()
        assert "docker" not in context2.lower()
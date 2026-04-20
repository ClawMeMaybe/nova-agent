"""Integration tests for brainstorm → implement skill pipeline.

Tests the infrastructure the LLM-driven flow relies on:
prompt generation, wiki spec persistence, task tracking lifecycle.
"""

import pytest

from nova.brainstorm import build_brainstorm_prompt
from nova.implement import build_implement_prompt


class TestBrainstormPromptGeneration:

    def test_prompt_with_topic(self):
        prompt = build_brainstorm_prompt("add a login page")
        assert "add a login page" in prompt
        assert "Round" in prompt
        assert "ask_user" in prompt
        assert "★ Recommended" in prompt

    def test_prompt_without_topic(self):
        prompt = build_brainstorm_prompt()
        assert "What idea or problem" in prompt
        assert "★ Recommended" in prompt
        assert "ambiguity" in prompt

    def test_prompt_contains_exit_conditions(self):
        prompt = build_brainstorm_prompt("test")
        assert "0.30" in prompt  # threshold
        assert "Round 6" in prompt  # hard cap

    def test_prompt_contains_spec_crystallization(self):
        prompt = build_brainstorm_prompt("test")
        assert "Acceptance Criteria" in prompt
        assert "wiki_ingest" in prompt

    def test_prompt_contains_implement_bridge(self):
        prompt = build_brainstorm_prompt("test")
        assert "implement" in prompt.lower()


class TestImplementPromptGeneration:

    def test_prompt_with_spec_slug(self):
        prompt = build_implement_prompt("brainstorm-spec-login")
        assert "brainstorm-spec-login" in prompt
        assert "Phase 0" in prompt
        assert "Phase 1" in prompt
        assert "Phase 2" in prompt
        assert "PLAN FIRST" in prompt

    def test_prompt_without_spec_slug(self):
        prompt = build_implement_prompt()
        assert "wiki_query" in prompt
        assert "brainstorm spec" in prompt.lower()

    def test_prompt_contains_phases(self):
        prompt = build_implement_prompt("test")
        assert "Phase 0: User Confirmation" in prompt
        assert "Phase 1: Read Spec" in prompt
        assert "Phase 2: Write Implementation Plan" in prompt
        assert "Phase 3: Execute Step-by-Step" in prompt
        assert "Phase 4: Final Verification" in prompt
        assert "Phase 5: Completion or Timeout" in prompt

    def test_prompt_contains_task_tracking(self):
        prompt = build_implement_prompt("test")
        assert "task_create" in prompt
        assert "task_update" in prompt
        assert "task_progress" in prompt

    def test_prompt_contains_safety_limits(self):
        prompt = build_implement_prompt("test")
        assert "40" in prompt  # 40 turn max
        assert "ANTI-PATTERNS" in prompt

    def test_prompt_contains_no_flat_files_rule(self):
        prompt = build_implement_prompt("test")
        assert "prd.json" in prompt  # explicitly forbidden
        assert "wiki_ingest" in prompt  # correct alternative


class TestBrainstormToImplementIntegration:

    def test_full_pipeline_infrastructure(self, memory):
        """Simulate the full brainstorm→implement data flow through the DB."""
        # Step 1: Brainstorm saves spec to wiki (what LLM would do)
        memory.wiki_add(
            slug="brainstorm-spec-login",
            title="Brainstorm Spec: Add Login Page",
            content="""# Brainstorm Spec: Add Login Page

## Goal
Add a login page with email/password authentication.

## Constraints
- Must use Flask
- No external auth providers
- Session-based auth

## Non-Goals
- Social login (OAuth)
- Password reset flow

## Acceptance Criteria
- [ ] Login form renders at /login with email and password fields
- [ ] Invalid credentials show error message
- [ ] Successful login redirects to dashboard
- [ ] Session persists across page loads""",
            category="decision",
            tags="brainstorm,spec,login,auth"
        )

        # Step 2: Verify spec is in wiki
        pages = memory.wiki_query("brainstorm spec")
        assert len(pages) > 0

        # Step 3: Implement skill creates tasks for each criterion
        criteria = [
            "Login form renders at /login with email and password fields",
            "Invalid credentials show error message",
            "Successful login redirects to dashboard",
            "Session persists across page loads"
        ]
        task_ids = []
        for i, criterion in enumerate(criteria, 1):
            tid = memory.task_create("brainstorm-spec-login", criterion, step_number=i)
            task_ids.append(tid)

        # Step 4: Verify all tasks created
        tasks = memory.task_list_by_spec("brainstorm-spec-login")
        assert len(tasks) == 4
        assert all(t['status'] == 'pending' for t in tasks)

        # Step 5: Mark some criteria as pass (simulate LLM verification)
        memory.task_update_status(task_ids[0], 'pass', notes="Form renders correctly", verification_method="code_run")
        memory.task_update_status(task_ids[1], 'pass', notes="Error shown on bad creds", verification_method="code_run")

        # Step 6: Check progress — 2/4 passed, not complete
        progress = memory.task_progress("brainstorm-spec-login")
        assert progress['total'] == 4
        assert progress['passed'] == 2
        assert progress['pending'] == 2
        assert progress['complete'] is False

        # Step 7: Mark remaining criteria
        memory.task_update_status(task_ids[2], 'pass', notes="Redirect works", verification_method="code_run")
        memory.task_update_status(task_ids[3], 'fail', notes="Session lost after restart", verification_method="code_run")

        # Step 8: Progress — 3/4 passed, 1 failed, not complete
        progress = memory.task_progress("brainstorm-spec-login")
        assert progress['passed'] == 3
        assert progress['failed'] == 1
        assert progress['complete'] is False

        # Step 9: Fix the failing criterion
        memory.task_update_status(task_ids[3], 'pass', notes="Session now persists", verification_method="code_run")

        # Step 10: All criteria pass — complete!
        progress = memory.task_progress("brainstorm-spec-login")
        assert progress['passed'] == 4
        assert progress['failed'] == 0
        assert progress['pending'] == 0
        assert progress['complete'] is True

    def test_implement_plan_saved_to_wiki(self, memory):
        """Simulate implement skill saving its plan to wiki."""
        # Save brainstorm spec
        memory.wiki_add(
            slug="brainstorm-spec-api",
            title="Brainstorm Spec: REST API",
            content="# Brainstorm Spec: REST API\n\n## Goal\nBuild REST API for task management.",
            category="decision",
            tags="brainstorm,spec,api"
        )

        # Save implementation plan (Phase 2 of implement)
        memory.wiki_add(
            slug="implementation-plan-api",
            title="Implementation Plan: REST API",
            content="""## Implementation Plan: REST API

### Step 1: Define models
- Files: models.py
- Criteria: 1, 2

### Step 2: Build endpoints
- Files: routes.py
- Criteria: 3, 4

| Criterion | Step | Status |
|-----------|------|--------|
| Models defined | Step 1 | pending |
| CRUD works | Step 2 | pending |""",
            category="decision",
            tags="implement,plan,api"
        )

        # Verify both exist in wiki
        spec_pages = memory.wiki_query("brainstorm spec")
        plan_pages = memory.wiki_query("implementation plan")
        assert len(spec_pages) > 0
        assert len(plan_pages) > 0

    def test_timeout_progress_summary(self, memory):
        """Simulate implement timeout — save progress summary to wiki."""
        # Create tasks, some pass, some incomplete
        tid1 = memory.task_create("spec-timeout", "criterion 1", step_number=1)
        tid2 = memory.task_create("spec-timeout", "criterion 2", step_number=2)

        memory.task_update_status(tid1, 'pass', notes="verified")
        # tid2 stays pending (simulating timeout)

        progress = memory.task_progress("spec-timeout")
        assert progress['passed'] == 1
        assert progress['pending'] == 1
        assert progress['complete'] is False

        # Save progress summary to wiki (Phase 5 timeout)
        memory.wiki_add(
            slug="implement-progress-timeout",
            title="Implementation Progress: Timeout Test",
            content=f"""## Implementation Progress

### Completed Criteria
- criterion 1 — verified

### Incomplete Criteria
- criterion 2 — not yet attempted

### Progress: {progress['passed']}/{progress['total']} passed""",
            category="session-log",
            tags="implement,progress,partial"
        )

        page = memory.wiki_read("implement-progress-timeout")
        assert page is not None
        assert "criterion 1" in page['content']
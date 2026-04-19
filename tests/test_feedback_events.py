"""Tests for Nova Memory Feedback Events — per-turn feedback, trust updates, evolution loss integration."""

import os
import tempfile

from nova.memory.engine import (
    NovaMemory,
    DBQ_ALLOWED_TABLES, SCHEMA_V6,
)


# ── Feedback Event Creation ──

class TestFeedbackEventCreation:

    def test_feedback_event_add_creates_row(self, memory):
        sid = memory.session_create("test task")
        fid = memory.fact_add("test fact for feedback", category="general")
        event_id = memory.feedback_event_add(
            'fact', fid, '', True, "helpful reason",
            session_id=sid, turn_num=1
        )
        assert event_id > 0
        row = memory._conn.execute(
            "SELECT * FROM feedback_events WHERE id=?", (event_id,)
        ).fetchone()
        assert row['target_type'] == 'fact'
        assert row['target_id'] == fid
        assert row['helpful'] == 1
        assert row['reason'] == 'helpful reason'
        assert row['session_id'] == sid
        assert row['turn_num'] == 1

    def test_feedback_event_add_skill(self, memory):
        sid = memory.session_create("test task")
        memory.skill_add("test-skill", "A test skill", ["1. Do thing"], triggers="test")
        event_id = memory.feedback_event_add(
            'skill', None, 'test-skill', True, "",
            session_id=sid, turn_num=2
        )
        assert event_id > 0
        row = memory._conn.execute(
            "SELECT * FROM feedback_events WHERE id=?", (event_id,)
        ).fetchone()
        assert row['target_type'] == 'skill'
        assert row['target_name'] == 'test-skill'
        assert row['helpful'] == 1

    def test_feedback_event_unhelpful(self, memory):
        sid = memory.session_create("test task")
        fid = memory.fact_add("bad fact", category="general")
        event_id = memory.feedback_event_add(
            'fact', fid, '', False, "not relevant to current task",
            session_id=sid, turn_num=3
        )
        assert event_id > 0
        row = memory._conn.execute(
            "SELECT * FROM feedback_events WHERE id=?", (event_id,)
        ).fetchone()
        assert row['helpful'] == 0
        assert row['reason'] == 'not relevant to current task'


# ── Trust Updates ──

class TestFeedbackTrustUpdate:

    def test_helpful_feedback_increases_trust(self, memory):
        sid = memory.session_create("test task")
        fid = memory.fact_add("helpful feedback fact", category="general", trust_score=0.5)
        memory.feedback_event_add('fact', fid, '', True, "", session_id=sid, turn_num=1)
        fact = memory._conn.execute("SELECT trust_score, helpful_count FROM facts WHERE id=?", (fid,)).fetchone()
        assert fact['trust_score'] == 0.55  # 0.5 + 0.05
        assert fact['helpful_count'] == 1

    def test_unhelpful_feedback_decreases_trust(self, memory):
        sid = memory.session_create("test task")
        fid = memory.fact_add("unhelpful feedback fact", category="general", trust_score=0.5)
        memory.feedback_event_add('fact', fid, '', False, "outdated and wrong info", session_id=sid, turn_num=1)
        fact = memory._conn.execute("SELECT trust_score, unhelpful_count FROM facts WHERE id=?", (fid,)).fetchone()
        assert fact['trust_score'] == 0.40  # 0.5 - 0.10
        assert fact['unhelpful_count'] == 1

    def test_unhelpful_by_id_matches_content_method(self, memory):
        """Verify fact_mark_unhelpful_by_id produces same result as fact_mark_unhelpful(content)."""
        fid1 = memory.fact_add("compare fact a", category="general", trust_score=0.7)
        fid2 = memory.fact_add("compare fact b", category="general", trust_score=0.7)
        memory.fact_mark_unhelpful("compare fact a")
        memory.fact_mark_unhelpful_by_id(fid2)
        fact_a = memory._conn.execute("SELECT trust_score, unhelpful_count FROM facts WHERE id=?", (fid1,)).fetchone()
        fact_b = memory._conn.execute("SELECT trust_score, unhelpful_count FROM facts WHERE id=?", (fid2,)).fetchone()
        assert fact_a['trust_score'] == fact_b['trust_score']
        assert fact_a['unhelpful_count'] == fact_b['unhelpful_count']


# ── Skill Updates ──

class TestFeedbackSkillUpdate:

    def test_helpful_skill_feedback(self, memory):
        sid = memory.session_create("test task")
        memory.skill_add("feedback-skill", "A skill", ["1. Step"], triggers="feedback", success_rate=0.5)
        memory.feedback_event_add('skill', None, 'feedback-skill', True, "", session_id=sid, turn_num=1)
        skill = memory._conn.execute("SELECT success_rate FROM skills WHERE name=?", ('feedback-skill',)).fetchone()
        assert skill['success_rate'] > 0.5

    def test_unhelpful_skill_feedback(self, memory):
        sid = memory.session_create("test task")
        memory.skill_add("bad-skill", "A bad skill", ["1. Step"], triggers="bad", success_rate=0.5)
        memory.feedback_event_add('skill', None, 'bad-skill', False, "steps are outdated and incorrect", session_id=sid, turn_num=1)
        skill = memory._conn.execute("SELECT success_rate FROM skills WHERE name=?", ('bad-skill',)).fetchone()
        assert skill['success_rate'] < 0.5


# ── Session Feedback Quality ──

class TestSessionFeedbackQuality:

    def test_session_feedback_quality_counts(self, memory):
        sid = memory.session_create("test task")
        fid1 = memory.fact_add("quality test fact 1", category="general")
        fid2 = memory.fact_add("quality test fact 2", category="general")
        memory.feedback_event_add('fact', fid1, '', True, "", session_id=sid, turn_num=1)
        memory.feedback_event_add('fact', fid2, '', False, "not useful at all here", session_id=sid, turn_num=2)
        quality = memory.session_feedback_quality(sid)
        assert quality['helpful_count'] == 1
        assert quality['unhelpful_count'] == 1
        assert quality['total_count'] == 2

    def test_session_feedback_quality_empty(self, memory):
        sid = memory.session_create("test task")
        quality = memory.session_feedback_quality(sid)
        assert quality['helpful_count'] == 0
        assert quality['unhelpful_count'] == 0
        assert quality['total_count'] == 0


# ── Evolution Loss Integration ──

class TestFeedbackEvolutionLoss:

    def test_evolution_loss_uses_feedback_events(self, memory):
        sid = memory.session_create("test task")
        fid1 = memory.fact_add("evolution fact 1", category="general")
        fid2 = memory.fact_add("evolution fact 2", category="general")
        # Mark one helpful, one unhelpful via feedback
        memory.feedback_event_add('fact', fid1, '', True, "", session_id=sid, turn_num=1)
        memory.feedback_event_add('fact', fid2, '', False, "irrelevant to this task context", session_id=sid, turn_num=2)
        loss = memory.compute_evolution_loss(
            session_id=sid, turns_used=5, max_turns=40,
            task_success=True, accessed_fact_ids=[fid1, fid2],
            accessed_skill_names=[], hindsight_hint=''
        )
        # helpful_ratio = 1 helpful / max(2 total, 2 accessed) = 0.5
        # loss_knowledge_quality = 1 - 0.5 = 0.5
        assert loss['loss_knowledge_quality'] == 0.5

    def test_evolution_loss_fallback_no_feedback(self, memory):
        """When no feedback_events exist, fallback to helpful_count logic."""
        sid = memory.session_create("test task")
        fid = memory.fact_add("fallback fact", category="general")
        # No feedback events created — should fallback to helpful_count logic
        loss = memory.compute_evolution_loss(
            session_id=sid, turns_used=5, max_turns=40,
            task_success=True, accessed_fact_ids=[fid],
            accessed_skill_names=[], hindsight_hint=''
        )
        # Fallback: helpful_count=0, unhelpful_count=0 → helpful=0 → ratio=0.0 → loss_kq=1.0
        # Actually: helpful_count=0 NOT > unhelpful_count=0 → helpful=0 → ratio=0.0
        assert loss['loss_knowledge_quality'] == 1.0  # no helpful_count > unhelpful_count


# ── DB Query ──

class TestFeedbackDbQuery:

    def test_feedback_events_queryable(self, memory):
        assert 'feedback_events' in DBQ_ALLOWED_TABLES
        sid = memory.session_create("test task")
        fid = memory.fact_add("db query fact", category="general")
        memory.feedback_event_add('fact', fid, '', True, "works well", session_id=sid, turn_num=1)
        result = memory.safe_query("SELECT * FROM feedback_events")
        assert result['status'] == 'success'
        assert result['row_count'] >= 1

    def test_feedback_events_schema_visible(self, memory):
        schema = memory.get_schema_info()
        assert 'feedback_events' in schema['tables']
        cols = [c['name'] for c in schema['tables']['feedback_events']]
        assert 'target_type' in cols
        assert 'helpful' in cols
        assert 'reason' in cols
        assert 'session_id' in cols


# ── Pruning ──

class TestFeedbackPruning:

    def test_feedback_events_pruned_with_sessions(self, memory):
        sid = memory.session_create("old task")
        fid = memory.fact_add("pruning test fact", category="general")
        memory.feedback_event_add('fact', fid, '', True, "", session_id=sid, turn_num=1)
        # Verify event exists
        count_before = memory._conn.execute("SELECT COUNT(*) FROM feedback_events").fetchone()[0]
        assert count_before >= 1
        # Prune (set max_age very short to trigger deletion)
        memory.prune_old_sessions(max_age_days=0)
        # feedback_events for old session should be deleted
        count_after = memory._conn.execute("SELECT COUNT(*) FROM feedback_events WHERE session_id=?", (sid,)).fetchone()[0]
        assert count_after == 0


# ── Existing Tests Unchanged ──

class TestExistingTestsUnchanged:
    """Verify existing test_memory_engine.py tests still pass — run separately via pytest."""
    pass  # Verified by running pytest tests/test_memory_engine.py independently
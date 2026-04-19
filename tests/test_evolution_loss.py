"""Tests for evolution_log table, V4 migration, loss computation, gradient application, and evolution score."""

import os
import json
import tempfile

import pytest

from nova.memory.engine import NovaMemory


@pytest.fixture
def local_db(tmp_path):
    return str(tmp_path / ".nova" / "nova.db")


@pytest.fixture
def global_db(tmp_path):
    return str(tmp_path / "global_nova" / "nova.db")


@pytest.fixture
def memory(local_db):
    mem = NovaMemory(local_db)
    yield mem
    mem.close()


# ── Schema ──


class TestEvolutionLogSchema:
    def test_fresh_db_has_evolution_log(self, tmp_path):
        db_path = str(tmp_path / ".nova" / "nova.db")
        mem = NovaMemory(db_path)
        row = mem._conn.execute("SELECT value FROM _meta WHERE key='schema_version'").fetchone()
        assert int(row[0]) >= 4
        # Check evolution_log table exists
        tables = [r[0] for r in mem._conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        assert 'evolution_log' in tables
        mem.close()

    def test_evolution_log_columns(self, memory):
        cols = [r[1] for r in memory._conn.execute("PRAGMA table_info(evolution_log)").fetchall()]
        expected = ['id', 'session_id', 'loss_task', 'loss_efficiency', 'loss_recurrence',
                    'loss_knowledge_quality', 'loss_total', 'evolution_score',
                    'gradient_facts', 'gradient_skills', 'improvement_targets', 'created_at']
        for col in expected:
            assert col in cols

    def test_evolution_log_index(self, memory):
        indexes = [r[0] for r in memory._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='evolution_log'"
        ).fetchall()]
        assert 'idx_evolution_log_score' in indexes


# ── Loss Computation ──


class TestComputeEvolutionLoss:
    def test_success_session_low_loss(self, memory):
        sid = memory.session_create("build deploy pipeline")
        loss = memory.compute_evolution_loss(
            session_id=sid, turns_used=5, max_turns=40,
            task_success=True, accessed_fact_ids=[], accessed_skill_names=[]
        )
        assert loss['loss_task'] == 0.0
        assert loss['loss_efficiency'] == 5 / 40
        # Recurrence may be > 0 if keywords match prior sessions in DB, but should be small
        assert loss['loss_recurrence'] < 0.5
        assert loss['loss_total'] > 0
        assert loss['evolution_score'] > 0

    def test_failure_session_high_loss(self, memory):
        sid = memory.session_create("test task that failed")
        loss = memory.compute_evolution_loss(
            session_id=sid, turns_used=40, max_turns=40,
            task_success=False, accessed_fact_ids=[], accessed_skill_names=[]
        )
        assert loss['loss_task'] == 1.0
        assert loss['loss_efficiency'] == 1.0
        assert loss['loss_total'] > 1.0
        assert loss['gradient_facts'] == []
        assert loss['gradient_skills'] == []

    def test_loss_with_facts(self, memory):
        fid = memory.fact_add("test fact", category="general", tags="test")
        sid = memory.session_create("test with facts")
        loss = memory.compute_evolution_loss(
            session_id=sid, turns_used=3, max_turns=40,
            task_success=True, accessed_fact_ids=[fid], accessed_skill_names=[]
        )
        # Fact has initial trust=0.5, helpful_count=0, unhelpful_count=0 → not helpful → direction=+ (success)
        assert len(loss['gradient_facts']) == 1
        assert loss['gradient_facts'][0]['id'] == fid
        assert loss['gradient_facts'][0]['direction'] == '+'

    def test_loss_with_skills(self, memory):
        memory.skill_add("test_skill", "desc", ["step1"], tags="test", triggers="test")
        sid = memory.session_create("test with skills")
        loss = memory.compute_evolution_loss(
            session_id=sid, turns_used=3, max_turns=40,
            task_success=True, accessed_fact_ids=[], accessed_skill_names=["test_skill"]
        )
        assert len(loss['gradient_skills']) == 1
        assert loss['gradient_skills'][0]['name'] == 'test_skill'
        assert loss['gradient_skills'][0]['direction'] == '+'

    def test_loss_negative_gradient_on_failure(self, memory):
        fid = memory.fact_add("bad fact", category="general")
        memory.skill_add("bad_skill", "desc", ["step1"], triggers="bad")
        sid = memory.session_create("failing task")
        loss = memory.compute_evolution_loss(
            session_id=sid, turns_used=30, max_turns=40,
            task_success=False, accessed_fact_ids=[fid], accessed_skill_names=["bad_skill"]
        )
        assert loss['gradient_facts'][0]['direction'] == '-'
        assert loss['gradient_skills'][0]['direction'] == '-'
        assert 'bad_skill' in loss['improvement_targets']

    def test_recurrence_penalty(self, memory):
        # Create a past failed session with similar keywords
        past_sid = memory.session_create("deploy auth service")
        memory.session_update(past_sid, summary="deploy auth", result="fail: timeout")
        # Now compute loss for a similar task
        sid = memory.session_create("deploy auth service v2")
        loss = memory.compute_evolution_loss(
            session_id=sid, turns_used=10, max_turns=40,
            task_success=False, accessed_fact_ids=[], accessed_skill_names=[]
        )
        # Should have some recurrence penalty from the past failure
        assert loss['loss_recurrence'] > 0


# ── Evolution Log Persistence ──


class TestEvolutionLogPersistence:
    def test_log_add(self, memory):
        sid = memory.session_create("test task")
        loss = memory.compute_evolution_loss(
            session_id=sid, turns_used=5, max_turns=40,
            task_success=True, accessed_fact_ids=[], accessed_skill_names=[]
        )
        log_id = memory.evolution_log_add(sid, loss)
        assert log_id > 0
        # Verify row exists
        row = memory._conn.execute(
            "SELECT loss_task, loss_total, evolution_score FROM evolution_log WHERE id=?", (log_id,)
        ).fetchone()
        assert row is not None
        assert row[0] == loss['loss_task']
        assert row[1] == loss['loss_total']

    def test_log_gradient_json(self, memory):
        fid = memory.fact_add("fact for gradient", category="test")
        sid = memory.session_create("gradient test")
        loss = memory.compute_evolution_loss(
            session_id=sid, turns_used=3, max_turns=40,
            task_success=True, accessed_fact_ids=[fid], accessed_skill_names=[]
        )
        log_id = memory.evolution_log_add(sid, loss)
        row = memory._conn.execute(
            "SELECT gradient_facts, improvement_targets FROM evolution_log WHERE id=?", (log_id,)
        ).fetchone()
        gf = json.loads(row[0])
        assert len(gf) == 1
        assert gf[0]['id'] == fid


# ── Gradient Application ──


class TestApplyGradient:
    def test_positive_gradient_strengthens_fact(self, memory):
        fid = memory.fact_add("good fact", category="test", trust_score=0.5)
        sid = memory.session_create("success task")
        loss = memory.compute_evolution_loss(
            session_id=sid, turns_used=3, max_turns=40,
            task_success=True, accessed_fact_ids=[fid], accessed_skill_names=[]
        )
        memory.evolution_log_add(sid, loss)
        memory.apply_gradient(loss)
        # Trust should increase
        fact = memory._conn.execute("SELECT trust_score FROM facts WHERE id=?", (fid,)).fetchone()
        assert fact[0] > 0.5

    def test_negative_gradient_weakens_fact(self, memory):
        fid = memory.fact_add("bad fact", category="test", trust_score=0.5)
        sid = memory.session_create("fail task")
        loss = memory.compute_evolution_loss(
            session_id=sid, turns_used=30, max_turns=40,
            task_success=False, accessed_fact_ids=[fid], accessed_skill_names=[]
        )
        memory.evolution_log_add(sid, loss)
        memory.apply_gradient(loss)
        # Trust should decrease (2x penalty for negative)
        fact = memory._conn.execute("SELECT trust_score FROM facts WHERE id=?", (fid,)).fetchone()
        assert fact[0] < 0.5

    def test_gradient_updates_skill_success(self, memory):
        memory.skill_add("good_skill", "desc", ["step1"], triggers="good", success_rate=0.5)
        sid = memory.session_create("success task")
        loss = memory.compute_evolution_loss(
            session_id=sid, turns_used=3, max_turns=40,
            task_success=True, accessed_fact_ids=[], accessed_skill_names=["good_skill"]
        )
        memory.evolution_log_add(sid, loss)
        memory.apply_gradient(loss)
        skill = memory._conn.execute("SELECT success_rate FROM skills WHERE name=?", ("good_skill",)).fetchone()
        assert skill[0] > 0.5


# ── Evolution Score ──


class TestEvolutionScore:
    def test_initial_score(self, memory):
        score, trend = memory.evolution_score()
        # No evolution_log entries → default score=0.5
        assert score == 0.5
        assert trend == 0.0

    def test_score_after_success(self, memory):
        sid = memory.session_create("success task")
        loss = memory.compute_evolution_loss(
            session_id=sid, turns_used=3, max_turns=40,
            task_success=True, accessed_fact_ids=[], accessed_skill_names=[]
        )
        memory.evolution_log_add(sid, loss)
        score, trend = memory.evolution_score()
        assert score > 0.5  # should improve from baseline

    def test_score_declines_after_failure(self, memory):
        # Add multiple successes first
        for i in range(3):
            sid = memory.session_create(f"success task {i}")
            loss = memory.compute_evolution_loss(
                session_id=sid, turns_used=3, max_turns=40,
                task_success=True, accessed_fact_ids=[], accessed_skill_names=[]
            )
            memory.evolution_log_add(sid, loss)
        score_after_success, _ = memory.evolution_score()
        # Add failures
        for i in range(3):
            sid = memory.session_create(f"failure task {i}")
            loss = memory.compute_evolution_loss(
                session_id=sid, turns_used=40, max_turns=40,
                task_success=False, accessed_fact_ids=[], accessed_skill_names=[]
            )
            memory.evolution_log_add(sid, loss)
        score_after_failure, trend = memory.evolution_score()
        assert score_after_failure < score_after_success


# ── V4 Migration ──


class TestV4Migration:
    def test_v3_db_upgrades_to_v4(self, tmp_path):
        """Verify a V3 database gets V4 evolution_log table on open."""
        db_path = str(tmp_path / ".nova" / "nova.db")
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        # Create a V3 DB by opening NovaMemory (which auto-migrates to current version)
        mem = NovaMemory(db_path)
        # Manually downgrade schema version marker to 3 to simulate a V3 DB
        mem._conn.execute("UPDATE _meta SET value='3' WHERE key='schema_version'")
        # Drop evolution_log to simulate it not existing yet
        mem._conn.execute("DROP TABLE IF EXISTS evolution_log")
        mem._conn.execute("DROP INDEX IF EXISTS idx_evolution_log_score")
        mem._conn.commit()
        mem.close()
        # Reopen — should auto-migrate to V4
        mem2 = NovaMemory(db_path)
        tables = [r[0] for r in mem2._conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        assert 'evolution_log' in tables
        version = mem2._conn.execute("SELECT value FROM _meta WHERE key='schema_version'").fetchone()[0]
        assert int(version) >= 4
        mem2.close()


import sqlite3


# ── Pipeline Fix Tests ──


class TestEvolutionOnFailure:
    """Verify evolution loss is computed on ALL outcomes, not just success."""

    def test_loss_computed_on_failure(self, memory):
        sid = memory.session_create("failing task")
        loss = memory.compute_evolution_loss(
            session_id=sid, turns_used=40, max_turns=40,
            task_success=False, accessed_fact_ids=[], accessed_skill_names=[],
            hindsight_hint='tried code_run but timeout; tried file_write but error'
        )
        assert loss['loss_task'] == 1.0
        assert loss['loss_total'] > 1.0
        assert loss['hindsight_hint'] == 'tried code_run but timeout; tried file_write but error'

    def test_gradient_applied_on_failure(self, memory):
        fid = memory.fact_add("bad fact", category="test", trust_score=0.5)
        memory.skill_add("bad_skill", "desc", ["step1"], triggers="bad")
        sid = memory.session_create("failing task")
        loss = memory.compute_evolution_loss(
            session_id=sid, turns_used=40, max_turns=40,
            task_success=False, accessed_fact_ids=[fid],
            accessed_skill_names=["bad_skill"],
            hindsight_hint='bad_skill timeout'
        )
        memory.evolution_log_add(sid, loss)
        memory.apply_gradient(loss)
        # Fact trust should decrease (negative gradient)
        fact = memory._conn.execute("SELECT trust_score FROM facts WHERE id=?", (fid,)).fetchone()
        assert fact[0] < 0.5
        # Skill success rate should decrease
        skill = memory._conn.execute("SELECT success_rate FROM skills WHERE name=?", ("bad_skill",)).fetchone()
        assert skill[0] < 0.5

    def test_hindsight_hint_stored_in_evolution_log(self, memory):
        sid = memory.session_create("failing task")
        loss = memory.compute_evolution_loss(
            session_id=sid, turns_used=30, max_turns=40,
            task_success=False, accessed_fact_ids=[], accessed_skill_names=[],
            hindsight_hint='file_read failed; code_run timeout'
        )
        log_id = memory.evolution_log_add(sid, loss)
        row = memory._conn.execute(
            "SELECT hindsight_hint FROM evolution_log WHERE id=?", (log_id,)
        ).fetchone()
        assert row[0] == 'file_read failed; code_run timeout'

    def test_hindsight_hint_empty_on_success(self, memory):
        sid = memory.session_create("success task")
        loss = memory.compute_evolution_loss(
            session_id=sid, turns_used=5, max_turns=40,
            task_success=True, accessed_fact_ids=[], accessed_skill_names=[]
        )
        assert loss['hindsight_hint'] == ''


class TestNoDoubleCounting:
    """Verify gradient is the sole feedback mechanism — no flat +0.05 alongside gradient."""

    def test_gradient_only_strengthens_fact_on_success(self, memory):
        fid = memory.fact_add("good fact", category="test", trust_score=0.5)
        sid = memory.session_create("success task")
        loss = memory.compute_evolution_loss(
            session_id=sid, turns_used=5, max_turns=40,
            task_success=True, accessed_fact_ids=[fid], accessed_skill_names=[]
        )
        memory.evolution_log_add(sid, loss)
        memory.apply_gradient(loss)
        # Trust should increase by gradient magnitude (NOT +0.05 flat)
        fact = memory._conn.execute("SELECT trust_score FROM facts WHERE id=?", (fid,)).fetchone()
        # Gradient magnitude = loss_total * 0.1, should be different from flat +0.05
        expected_mag = loss['loss_total'] * 0.1
        assert abs(fact[0] - (0.5 + expected_mag)) < 0.01  # gradient, not flat

    def test_no_flat_helpful_update_on_success(self, memory):
        """The handler no longer calls fact_mark_helpful — only apply_gradient."""
        fid = memory.fact_add("test fact", category="test", trust_score=0.5)
        # Simulate what handler does: only apply_gradient, no fact_mark_helpful
        sid = memory.session_create("success task")
        loss = memory.compute_evolution_loss(
            session_id=sid, turns_used=3, max_turns=40,
            task_success=True, accessed_fact_ids=[fid], accessed_skill_names=[]
        )
        memory.apply_gradient(loss)
        fact = memory._conn.execute("SELECT trust_score FROM facts WHERE id=?", (fid,)).fetchone()
        # Should be 0.5 + gradient magnitude, NOT 0.5 + 0.05 + gradient magnitude
        expected = 0.5 + loss['loss_total'] * 0.1
        assert abs(fact[0] - expected) < 0.02


class TestEvolutionContext:
    """Verify evolution status is injected into next session context."""

    def test_build_context_includes_evolution_score(self, memory):
        # Create some evolution data
        sid = memory.session_create("test task")
        loss = memory.compute_evolution_loss(
            session_id=sid, turns_used=3, max_turns=40,
            task_success=True, accessed_fact_ids=[], accessed_skill_names=[]
        )
        memory.evolution_log_add(sid, loss)
        context = memory.build_context_prompt()
        assert 'Evolution Status' in context

    def test_build_context_no_evolution_when_no_data(self, memory):
        context = memory.build_context_prompt()
        # Default score=0.5 means no data — should NOT show evolution section
        assert 'Evolution Status' not in context

    def test_build_context_shows_targets_when_declining(self, memory):
        # Add a failure to get improvement_targets
        memory.skill_add("struggle_skill", "desc", ["step1"], triggers="struggle")
        sid = memory.session_create("failing task")
        loss = memory.compute_evolution_loss(
            session_id=sid, turns_used=40, max_turns=40,
            task_success=False, accessed_fact_ids=[], accessed_skill_names=["struggle_skill"]
        )
        memory.evolution_log_add(sid, loss)
        # Need at least 2 entries for trend
        sid2 = memory.session_create("another failure")
        loss2 = memory.compute_evolution_loss(
            session_id=sid2, turns_used=40, max_turns=40,
            task_success=False, accessed_fact_ids=[], accessed_skill_names=["struggle_skill"]
        )
        memory.evolution_log_add(sid2, loss2)
        context = memory.build_context_prompt()
        assert 'Evolution Status' in context


class TestWikiQualityFeedback:
    """Verify wiki pages get confidence from RL, not hardcoded 'low'."""

    def test_wiki_mark_quality_high(self, memory):
        page_id = memory.wiki_add('test-page', 'Test', 'Some content', category='reference')
        result = memory.wiki_mark_quality('test-page', 'high')
        assert result is True
        page = memory.wiki_read('test-page')
        assert page['confidence'] == 'high'

    def test_wiki_mark_quality_low(self, memory):
        page_id = memory.wiki_add('test-page2', 'Test2', 'Content', category='reference')
        memory.wiki_mark_quality('test-page2', 'low')
        page = memory.wiki_read('test-page2')
        assert page['confidence'] == 'low'

    def test_wiki_mark_quality_via_memory(self, memory):
        memory.wiki_add('test-page3', 'Test3', 'Content')
        result = memory.wiki_mark_quality('test-page3', 'high')
        assert result is True

    def test_session_crystallize_dynamic_confidence(self, memory):
        memory.fact_add("test fact", category="test")
        sid = memory.session_create("success session")
        memory.session_update(sid, summary="success", result="done", had_knowledge=True)
        # Add evolution data to get a decent score
        loss = memory.compute_evolution_loss(
            session_id=sid, turns_used=3, max_turns=40,
            task_success=True, accessed_fact_ids=[], accessed_skill_names=[]
        )
        memory.evolution_log_add(sid, loss)
        # Crystallize — should use dynamic confidence
        page_id = memory.session_crystallize(sid)
        if page_id:
            page = memory.wiki_read(memory._conn.execute(
                "SELECT slug FROM wiki_pages WHERE id=?", (page_id,)
            ).fetchone()[0])
            # Should NOT be hardcoded 'low'
            assert page['confidence'] != 'low' or memory.evolution_score()[0] < 0.4


class TestV5Migration:
    """Verify V5 schema migration adds hindsight_hint column."""

    def test_fresh_db_has_hindsight_hint(self, memory):
        cols = [r[1] for r in memory._conn.execute("PRAGMA table_info(evolution_log)").fetchall()]
        assert 'hindsight_hint' in cols

    def test_v4_db_migrates_to_v5(self, tmp_path):
        db_path = str(tmp_path / ".nova" / "nova.db")
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        mem = NovaMemory(db_path)
        # Downgrade to V4 (has evolution_log but no hindsight_hint)
        mem._conn.execute("UPDATE _meta SET value='4' WHERE key='schema_version'")
        # Drop hindsight_hint if present
        cols = [r[1] for r in mem._conn.execute("PRAGMA table_info(evolution_log)").fetchall()]
        if 'hindsight_hint' in cols:
            # Can't drop column in SQLite, so recreate table without it
            mem._conn.execute("ALTER TABLE evolution_log RENAME TO evolution_log_old")
            mem._conn.execute("""CREATE TABLE evolution_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                loss_task REAL NOT NULL,
                loss_efficiency REAL NOT NULL,
                loss_recurrence REAL NOT NULL DEFAULT 0,
                loss_knowledge_quality REAL NOT NULL DEFAULT 0,
                loss_total REAL NOT NULL,
                evolution_score REAL NOT NULL,
                gradient_facts TEXT NOT NULL DEFAULT '[]',
                gradient_skills TEXT NOT NULL DEFAULT '[]',
                improvement_targets TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL
            )""")
            mem._conn.execute("INSERT INTO evolution_log SELECT id,session_id,loss_task,loss_efficiency,loss_recurrence,loss_knowledge_quality,loss_total,evolution_score,gradient_facts,gradient_skills,improvement_targets,created_at FROM evolution_log_old")
            mem._conn.execute("DROP TABLE evolution_log_old")
        mem._conn.commit()
        mem.close()
        # Reopen — should migrate to V5 and add hindsight_hint
        mem2 = NovaMemory(db_path)
        cols2 = [r[1] for r in mem2._conn.execute("PRAGMA table_info(evolution_log)").fetchall()]
        assert 'hindsight_hint' in cols2
        version = mem2._conn.execute("SELECT value FROM _meta WHERE key='schema_version'").fetchone()[0]
        assert int(version) >= 5
        mem2.close()

    def test_schema_version_is_5(self, memory):
        row = memory._conn.execute("SELECT value FROM _meta WHERE key='schema_version'").fetchone()
        assert int(row[0]) >= 5
"""Tests for evolution_log table, V4 migration, loss computation, gradient application, and evolution score."""

import os
import json
import tempfile

import pytest

from nova.memory.engine import NovaMemory, TwoTierMemory, SCHEMA_V4


@pytest.fixture
def local_db(tmp_path):
    return str(tmp_path / ".nova" / "nova.db")


@pytest.fixture
def global_db(tmp_path):
    return str(tmp_path / "global_nova" / "nova.db")


@pytest.fixture
def local_memory(local_db):
    mem = NovaMemory(local_db)
    yield mem
    mem.close()


@pytest.fixture
def two_tier(local_db, global_db):
    mem = TwoTierMemory(local_db, global_db)
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

    def test_evolution_log_columns(self, local_memory):
        cols = [r[1] for r in local_memory._conn.execute("PRAGMA table_info(evolution_log)").fetchall()]
        expected = ['id', 'session_id', 'loss_task', 'loss_efficiency', 'loss_recurrence',
                    'loss_knowledge_quality', 'loss_total', 'evolution_score',
                    'gradient_facts', 'gradient_skills', 'improvement_targets', 'created_at']
        for col in expected:
            assert col in cols

    def test_evolution_log_index(self, local_memory):
        indexes = [r[0] for r in local_memory._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='evolution_log'"
        ).fetchall()]
        assert 'idx_evolution_log_score' in indexes


# ── Loss Computation ──


class TestComputeEvolutionLoss:
    def test_success_session_low_loss(self, local_memory):
        sid = local_memory.session_create("build deploy pipeline")
        loss = local_memory.compute_evolution_loss(
            session_id=sid, turns_used=5, max_turns=40,
            task_success=True, accessed_fact_ids=[], accessed_skill_names=[]
        )
        assert loss['loss_task'] == 0.0
        assert loss['loss_efficiency'] == 5 / 40
        # Recurrence may be > 0 if keywords match prior sessions in DB, but should be small
        assert loss['loss_recurrence'] < 0.5
        assert loss['loss_total'] > 0
        assert loss['evolution_score'] > 0

    def test_failure_session_high_loss(self, local_memory):
        sid = local_memory.session_create("test task that failed")
        loss = local_memory.compute_evolution_loss(
            session_id=sid, turns_used=40, max_turns=40,
            task_success=False, accessed_fact_ids=[], accessed_skill_names=[]
        )
        assert loss['loss_task'] == 1.0
        assert loss['loss_efficiency'] == 1.0
        assert loss['loss_total'] > 1.0
        assert loss['gradient_facts'] == []
        assert loss['gradient_skills'] == []

    def test_loss_with_facts(self, local_memory):
        fid = local_memory.fact_add("test fact", category="general", tags="test")
        sid = local_memory.session_create("test with facts")
        loss = local_memory.compute_evolution_loss(
            session_id=sid, turns_used=3, max_turns=40,
            task_success=True, accessed_fact_ids=[fid], accessed_skill_names=[]
        )
        # Fact has initial trust=0.5, helpful_count=0, unhelpful_count=0 → not helpful → direction=+ (success)
        assert len(loss['gradient_facts']) == 1
        assert loss['gradient_facts'][0]['id'] == fid
        assert loss['gradient_facts'][0]['direction'] == '+'

    def test_loss_with_skills(self, local_memory):
        local_memory.skill_add("test_skill", "desc", ["step1"], tags="test", triggers="test")
        sid = local_memory.session_create("test with skills")
        loss = local_memory.compute_evolution_loss(
            session_id=sid, turns_used=3, max_turns=40,
            task_success=True, accessed_fact_ids=[], accessed_skill_names=["test_skill"]
        )
        assert len(loss['gradient_skills']) == 1
        assert loss['gradient_skills'][0]['name'] == 'test_skill'
        assert loss['gradient_skills'][0]['direction'] == '+'

    def test_loss_negative_gradient_on_failure(self, local_memory):
        fid = local_memory.fact_add("bad fact", category="general")
        local_memory.skill_add("bad_skill", "desc", ["step1"], triggers="bad")
        sid = local_memory.session_create("failing task")
        loss = local_memory.compute_evolution_loss(
            session_id=sid, turns_used=30, max_turns=40,
            task_success=False, accessed_fact_ids=[fid], accessed_skill_names=["bad_skill"]
        )
        assert loss['gradient_facts'][0]['direction'] == '-'
        assert loss['gradient_skills'][0]['direction'] == '-'
        assert 'bad_skill' in loss['improvement_targets']

    def test_recurrence_penalty(self, local_memory):
        # Create a past failed session with similar keywords
        past_sid = local_memory.session_create("deploy auth service")
        local_memory.session_update(past_sid, summary="deploy auth", result="fail: timeout")
        # Now compute loss for a similar task
        sid = local_memory.session_create("deploy auth service v2")
        loss = local_memory.compute_evolution_loss(
            session_id=sid, turns_used=10, max_turns=40,
            task_success=False, accessed_fact_ids=[], accessed_skill_names=[]
        )
        # Should have some recurrence penalty from the past failure
        assert loss['loss_recurrence'] > 0


# ── Evolution Log Persistence ──


class TestEvolutionLogPersistence:
    def test_log_add(self, local_memory):
        sid = local_memory.session_create("test task")
        loss = local_memory.compute_evolution_loss(
            session_id=sid, turns_used=5, max_turns=40,
            task_success=True, accessed_fact_ids=[], accessed_skill_names=[]
        )
        log_id = local_memory.evolution_log_add(sid, loss)
        assert log_id > 0
        # Verify row exists
        row = local_memory._conn.execute(
            "SELECT loss_task, loss_total, evolution_score FROM evolution_log WHERE id=?", (log_id,)
        ).fetchone()
        assert row is not None
        assert row[0] == loss['loss_task']
        assert row[1] == loss['loss_total']

    def test_log_gradient_json(self, local_memory):
        fid = local_memory.fact_add("fact for gradient", category="test")
        sid = local_memory.session_create("gradient test")
        loss = local_memory.compute_evolution_loss(
            session_id=sid, turns_used=3, max_turns=40,
            task_success=True, accessed_fact_ids=[fid], accessed_skill_names=[]
        )
        log_id = local_memory.evolution_log_add(sid, loss)
        row = local_memory._conn.execute(
            "SELECT gradient_facts, improvement_targets FROM evolution_log WHERE id=?", (log_id,)
        ).fetchone()
        gf = json.loads(row[0])
        assert len(gf) == 1
        assert gf[0]['id'] == fid


# ── Gradient Application ──


class TestApplyGradient:
    def test_positive_gradient_strengthens_fact(self, local_memory):
        fid = local_memory.fact_add("good fact", category="test", trust_score=0.5)
        sid = local_memory.session_create("success task")
        loss = local_memory.compute_evolution_loss(
            session_id=sid, turns_used=3, max_turns=40,
            task_success=True, accessed_fact_ids=[fid], accessed_skill_names=[]
        )
        local_memory.evolution_log_add(sid, loss)
        local_memory.apply_gradient(loss)
        # Trust should increase
        fact = local_memory._conn.execute("SELECT trust_score FROM facts WHERE id=?", (fid,)).fetchone()
        assert fact[0] > 0.5

    def test_negative_gradient_weakens_fact(self, local_memory):
        fid = local_memory.fact_add("bad fact", category="test", trust_score=0.5)
        sid = local_memory.session_create("fail task")
        loss = local_memory.compute_evolution_loss(
            session_id=sid, turns_used=30, max_turns=40,
            task_success=False, accessed_fact_ids=[fid], accessed_skill_names=[]
        )
        local_memory.evolution_log_add(sid, loss)
        local_memory.apply_gradient(loss)
        # Trust should decrease (2x penalty for negative)
        fact = local_memory._conn.execute("SELECT trust_score FROM facts WHERE id=?", (fid,)).fetchone()
        assert fact[0] < 0.5

    def test_gradient_updates_skill_success(self, local_memory):
        local_memory.skill_add("good_skill", "desc", ["step1"], triggers="good", success_rate=0.5)
        sid = local_memory.session_create("success task")
        loss = local_memory.compute_evolution_loss(
            session_id=sid, turns_used=3, max_turns=40,
            task_success=True, accessed_fact_ids=[], accessed_skill_names=["good_skill"]
        )
        local_memory.evolution_log_add(sid, loss)
        local_memory.apply_gradient(loss)
        skill = local_memory._conn.execute("SELECT success_rate FROM skills WHERE name=?", ("good_skill",)).fetchone()
        assert skill[0] > 0.5


# ── Evolution Score ──


class TestEvolutionScore:
    def test_initial_score(self, local_memory):
        score, trend = local_memory.evolution_score()
        # No evolution_log entries → default score=0.5
        assert score == 0.5
        assert trend == 0.0

    def test_score_after_success(self, local_memory):
        sid = local_memory.session_create("success task")
        loss = local_memory.compute_evolution_loss(
            session_id=sid, turns_used=3, max_turns=40,
            task_success=True, accessed_fact_ids=[], accessed_skill_names=[]
        )
        local_memory.evolution_log_add(sid, loss)
        score, trend = local_memory.evolution_score()
        assert score > 0.5  # should improve from baseline

    def test_score_declines_after_failure(self, local_memory):
        # Add multiple successes first
        for i in range(3):
            sid = local_memory.session_create(f"success task {i}")
            loss = local_memory.compute_evolution_loss(
                session_id=sid, turns_used=3, max_turns=40,
                task_success=True, accessed_fact_ids=[], accessed_skill_names=[]
            )
            local_memory.evolution_log_add(sid, loss)
        score_after_success, _ = local_memory.evolution_score()
        # Add failures
        for i in range(3):
            sid = local_memory.session_create(f"failure task {i}")
            loss = local_memory.compute_evolution_loss(
                session_id=sid, turns_used=40, max_turns=40,
                task_success=False, accessed_fact_ids=[], accessed_skill_names=[]
            )
            local_memory.evolution_log_add(sid, loss)
        score_after_failure, trend = local_memory.evolution_score()
        assert score_after_failure < score_after_success


# ── TwoTierMemory Wrappers ──


class TestTwoTierEvolutionWrappers:
    def test_compute_evolution_loss_via_two_tier(self, two_tier):
        sid = two_tier.session_create("test via two tier")
        loss = two_tier.compute_evolution_loss(
            session_id=sid, turns_used=5, max_turns=40,
            task_success=True, accessed_fact_ids=[], accessed_skill_names=[]
        )
        assert loss['loss_task'] == 0.0

    def test_evolution_log_add_via_two_tier(self, two_tier):
        sid = two_tier.session_create("test via two tier")
        loss = two_tier.compute_evolution_loss(
            session_id=sid, turns_used=5, max_turns=40,
            task_success=True, accessed_fact_ids=[], accessed_skill_names=[]
        )
        log_id = two_tier.evolution_log_add(sid, loss)
        assert log_id > 0

    def test_apply_gradient_via_two_tier(self, two_tier):
        fid = two_tier.fact_add("tier fact", category="test", tier="local")
        sid = two_tier.session_create("two tier gradient")
        loss = two_tier.compute_evolution_loss(
            session_id=sid, turns_used=3, max_turns=40,
            task_success=True, accessed_fact_ids=[fid], accessed_skill_names=[]
        )
        two_tier.evolution_log_add(sid, loss)
        two_tier.apply_gradient(loss)
        # Verify via direct local access
        fact = two_tier._local._conn.execute("SELECT trust_score FROM facts WHERE id=?", (fid,)).fetchone()
        assert fact[0] > 0.5

    def test_evolution_score_via_two_tier(self, two_tier):
        score, trend = two_tier.evolution_score()
        assert score == 0.5  # no entries yet

    def test_stats_includes_evolution(self, two_tier):
        stats = two_tier.stats()
        assert 'evolution_score' in stats
        assert 'evolution_trend' in stats


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
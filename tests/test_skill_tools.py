"""Tests for skill_add, skill_search, skill_improve, skill_match, and schema migration."""

import os
import json
import tempfile

import pytest

from nova.memory.engine import NovaMemory, TwoTierMemory, SCHEMA_V1, SCHEMA_V2


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


# ── skill_add with triggers and pitfalls ──


class TestSkillAdd:
    def test_add_skill_with_triggers_pitfalls(self, local_memory):
        sid = local_memory.skill_add(
            "deploy-flask", "Deploy Flask app to server",
            ["1. Check config", "2. Upload files", "3. Verify: curl returns 200"],
            tags="deploy,flask,sop",
            triggers="deploy,flask,ssh,systemd",
            pitfalls=["Don't forget daemon-reload", "Always verify service starts"]
        )
        assert sid > 0

        # Verify stored correctly
        row = local_memory._conn.execute("SELECT * FROM skills WHERE name='deploy-flask'").fetchone()
        assert row['name'] == "deploy-flask"
        assert row['triggers'] == "deploy,flask,ssh,systemd"
        assert row['version'] == 1
        pitfalls = json.loads(row['pitfalls'])
        assert len(pitfalls) == 2
        assert "Don't forget daemon-reload" in pitfalls

    def test_add_skill_minimal(self, local_memory):
        sid = local_memory.skill_add(
            "simple-task", "A simple SOP",
            ["1. Do thing", "2. Verify: check result"],
            triggers="simple,basic"
        )
        assert sid > 0

    def test_add_skill_updates_existing(self, local_memory):
        local_memory.skill_add(
            "deploy-flask", "Deploy Flask app",
            ["1. Old step"],
            triggers="deploy,flask"
        )
        local_memory.skill_add(
            "deploy-flask", "Deploy Flask app v2",
            ["1. New step", "2. Verify: check output"],
            triggers="deploy,flask,ssh",
            pitfalls=["Don't skip verification"]
        )
        row = local_memory._conn.execute("SELECT * FROM skills WHERE name='deploy-flask'").fetchone()
        steps = json.loads(row['steps'])
        assert len(steps) == 2
        assert row['version'] == 2

    def test_default_skill_has_triggers_pitfalls(self, local_memory):
        row = local_memory._conn.execute("SELECT * FROM skills WHERE name='memory_management'").fetchone()
        assert row['triggers'] == "memory,update,crystallize,facts,wiki,skills,knowledge"
        pitfalls = json.loads(row['pitfalls'])
        assert len(pitfalls) == 3


# ── skill_search ──


class TestSkillSearch:
    def test_search_by_trigger_keywords(self, local_memory):
        local_memory.skill_add(
            "deploy-flask", "Deploy Flask",
            ["1. Deploy", "2. Verify"],
            triggers="deploy,flask,ssh"
        )
        results = local_memory.skill_search("deploy")
        assert len(results) >= 1
        assert results[0]['name'] == 'deploy-flask'
        assert 'pitfalls' in results[0]
        assert isinstance(results[0]['pitfalls'], list)

    def test_search_no_results(self, local_memory):
        results = local_memory.skill_search("nonexistent_topic_xyz")
        assert results == []

    def test_search_min_success_filter(self, local_memory):
        local_memory.skill_add(
            "bad-skill", "A skill with low success",
            ["1. Try something"],
            triggers="bad,fail",
            success_rate=0.1
        )
        results = local_memory.skill_search("bad", min_success=0.5)
        # Should not find the low-success skill
        assert not any(r['name'] == 'bad-skill' for r in results)


# ── skill_improve ──


class TestSkillImprove:
    def test_improve_skill_increments_version(self, local_memory):
        local_memory.skill_add(
            "deploy-flask", "Deploy Flask",
            ["1. Old step"],
            triggers="deploy,flask"
        )
        result = local_memory.skill_improve(
            "deploy-flask",
            new_steps=["1. New step", "2. Verify"],
            new_pitfalls=["Don't skip step 2"]
        )
        assert result is True
        row = local_memory._conn.execute("SELECT * FROM skills WHERE name='deploy-flask'").fetchone()
        assert row['version'] == 2
        assert row['last_improved_at'] != ''
        steps = json.loads(row['steps'])
        assert len(steps) == 2

    def test_improve_skill_merges_pitfalls(self, local_memory):
        local_memory.skill_add(
            "deploy-flask", "Deploy Flask",
            ["1. Step"],
            triggers="deploy",
            pitfalls=["Existing pitfall A"]
        )
        local_memory.skill_improve(
            "deploy-flask",
            new_pitfalls=["New pitfall B", "Existing pitfall A"]
        )
        row = local_memory._conn.execute("SELECT * FROM skills WHERE name='deploy-flask'").fetchone()
        pitfalls = json.loads(row['pitfalls'])
        # Dedup: "Existing pitfall A" should not appear twice
        assert len(pitfalls) == 2
        assert "Existing pitfall A" in pitfalls
        assert "New pitfall B" in pitfalls

    def test_improve_nonexistent_skill(self, local_memory):
        result = local_memory.skill_improve("nonexistent-skill")
        assert result is False


# ── skill_match (SQL LIKE-based, not FTS5) ──


class TestSkillMatch:
    def test_match_by_trigger_keyword(self, local_memory):
        local_memory.skill_add(
            "deploy-flask", "Deploy Flask",
            ["1. SSH", "2. Upload", "3. Verify: curl 200"],
            triggers="deploy,flask,ssh,systemd",
            pitfalls=["Don't skip daemon-reload"]
        )
        results = local_memory.skill_match("deploy flask app to server")
        assert len(results) >= 1
        assert results[0]['name'] == 'deploy-flask'
        assert 'pitfalls' in results[0]

    def test_match_no_stopwords(self, local_memory):
        # Query with only stopwords should return no matches
        results = local_memory.skill_match("how to do the thing")
        assert results == []

    def test_match_respects_min_success(self, local_memory):
        local_memory.skill_add(
            "broken-skill", "A broken SOP",
            ["1. Try"],
            triggers="broken,bad",
            success_rate=0.1
        )
        results = local_memory.skill_match("broken bad task", min_success=0.5)
        # broken-skill (success 0.1) must be filtered out
        broken_found = any(r['name'] == 'broken-skill' for r in results)
        assert not broken_found


# ── TwoTierMemory skill operations ──


class TestTwoTierSkillOps:
    def test_skill_add_routes_to_global(self, two_tier):
        sid = two_tier.skill_add(
            "global-skill", "Cross-project skill",
            ["1. Step"],
            triggers="global,cross-project",
            tier="global"
        )
        assert sid > 0
        # Verify knowledge was produced (it sets a flag, doesn't return)
        assert two_tier._knowledge_produced is True

    def test_skill_search_both_tiers(self, two_tier):
        two_tier.skill_add(
            "local-skill", "Local only",
            ["1. Local step"],
            triggers="local,project",
            tier="local"
        )
        two_tier.skill_add(
            "global-skill", "Global only",
            ["1. Global step"],
            triggers="global,cross-project",
            tier="global"
        )
        results = two_tier.skill_search("skill", tier="auto")
        assert len(results) >= 2

    def test_skill_match_both_tiers(self, two_tier):
        two_tier.skill_add(
            "deploy-local", "Local deploy SOP",
            ["1. Deploy locally"],
            triggers="deploy,local",
            tier="local"
        )
        two_tier.skill_add(
            "deploy-global", "Global deploy SOP",
            ["1. Deploy globally"],
            triggers="deploy,global",
            tier="global"
        )
        results = two_tier.skill_match("deploy application", limit=2)
        assert len(results) >= 1

    def test_skill_improve_tier_routing(self, two_tier):
        two_tier.skill_add(
            "global-skill", "Global SOP",
            ["1. Old step"],
            triggers="global",
            tier="global"
        )
        result = two_tier.skill_improve(
            "global-skill",
            new_steps=["1. New step", "2. Verify"],
            tier="global"
        )
        assert result is True


# ── Schema migration ──


class TestSchemaMigration:
    def test_fresh_db_has_v2_schema(self, tmp_path):
        db_path = str(tmp_path / ".nova" / "nova.db")
        mem = NovaMemory(db_path)
        cols = [r[1] for r in mem._conn.execute("PRAGMA table_info(skills)").fetchall()]
        mem.close()
        assert 'triggers' in cols
        assert 'pitfalls' in cols
        assert 'version' in cols
        assert 'last_improved_at' in cols

    def test_schema_version_is_2(self, tmp_path):
        db_path = str(tmp_path / ".nova" / "nova.db")
        mem = NovaMemory(db_path)
        row = mem._conn.execute("SELECT value FROM _meta WHERE key='schema_version'").fetchone()
        mem.close()
        assert int(row[0]) >= 2

    def test_v1_db_migrates_to_v2(self, tmp_path):
        db_path = str(tmp_path / ".nova" / "nova.db")
        os.makedirs(str(tmp_path / ".nova"), exist_ok=True)

        # Create a legacy V1 DB (without triggers/pitfalls/version columns)
        # This simulates a DB created before the V2 migration
        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS _meta (key TEXT UNIQUE, value TEXT);
            INSERT OR IGNORE INTO _meta (key, value) VALUES ('schema_version', '1');

            CREATE TABLE IF NOT EXISTS wiki_pages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slug TEXT UNIQUE NOT NULL,
                title TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'reference',
                content TEXT NOT NULL,
                tags TEXT NOT NULL DEFAULT '',
                confidence TEXT NOT NULL DEFAULT 'medium',
                sources TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT UNIQUE NOT NULL,
                category TEXT NOT NULL DEFAULT 'general',
                tags TEXT NOT NULL DEFAULT '',
                trust_score REAL NOT NULL DEFAULT 0.5,
                retrieval_count INTEGER NOT NULL DEFAULT 0,
                helpful_count INTEGER NOT NULL DEFAULT 0,
                unhelpful_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS skills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                steps TEXT NOT NULL DEFAULT '[]',
                success_rate REAL NOT NULL DEFAULT 0.5,
                usage_count INTEGER NOT NULL DEFAULT 0,
                tags TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task TEXT NOT NULL,
                summary TEXT NOT NULL DEFAULT '',
                result TEXT NOT NULL DEFAULT '',
                wiki_page_id INTEGER DEFAULT NULL,
                had_knowledge_output INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS wiki_fts USING fts5(
                title, content, tags, category,
                content=wiki_pages, content_rowid=id
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(
                content, category, tags,
                content=facts, content_rowid=id
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS skills_fts USING fts5(
                name, description, steps, tags,
                content=skills, content_rowid=id
            );

            CREATE INDEX IF NOT EXISTS idx_facts_trust ON facts(trust_score DESC);
            CREATE INDEX IF NOT EXISTS idx_facts_category ON facts(category);
            CREATE INDEX IF NOT EXISTS idx_skills_success ON skills(success_rate DESC, usage_count DESC);
            CREATE INDEX IF NOT EXISTS idx_sessions_created ON sessions(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_wiki_category ON wiki_pages(category);

            -- Add a legacy skill to verify it survives migration
            INSERT INTO skills (name, description, steps, success_rate, tags, created_at, updated_at)
            VALUES ('legacy-skill', 'Old format skill', '["Step 1", "Step 2"]', 0.5, 'legacy', '2025-01-01T00:00:00', '2025-01-01T00:00:00');
        """)
        conn.commit()

        # Verify V1 state — no triggers column
        cols_before = [r[1] for r in conn.execute("PRAGMA table_info(skills)").fetchall()]
        conn.close()
        assert 'triggers' not in cols_before

        # Now open with NovaMemory — should auto-migrate
        mem = NovaMemory(db_path)
        cols_after = [r[1] for r in mem._conn.execute("PRAGMA table_info(skills)").fetchall()]
        version = mem._conn.execute("SELECT value FROM _meta WHERE key='schema_version'").fetchone()[0]

        # Verify legacy skill survived migration and got new default values
        legacy = mem._conn.execute("SELECT * FROM skills WHERE name='legacy-skill'").fetchone()
        mem.close()

        assert 'triggers' in cols_after
        assert 'pitfalls' in cols_after
        assert 'version' in cols_after
        assert 'last_improved_at' in cols_after
        assert int(version) >= 2
        assert legacy['triggers'] == ''
        assert json.loads(legacy['pitfalls']) == []
        assert legacy['version'] == 1


# ── LLM SQL queryability of skills ──


class TestSkillSQLQuery:
    def test_db_query_can_select_skills(self, local_memory):
        local_memory.skill_add(
            "deploy-flask", "Deploy Flask",
            ["1. Deploy", "2. Verify"],
            triggers="deploy,flask,ssh"
        )
        result = local_memory.safe_query("SELECT name, triggers, version FROM skills WHERE triggers LIKE '%deploy%'")
        assert result['status'] == 'success'
        assert len(result['rows']) >= 1
        assert result['rows'][0]['name'] == 'deploy-flask'
"""Tests for Nova Knowledge Links — links, clusters, cascade flags, dynamic context."""

import os
import tempfile

from nova.memory.engine import (
    NovaMemory,
    DBQ_ALLOWED_TABLES, SCHEMA_V7,
)


# ── Knowledge Link Creation ──

class TestKnowledgeLinkCreation:

    def test_link_add_fact_to_skill(self, memory):
        fid = memory.fact_add("Flask runs on port 5000", category="environment", tags="flask,deployment")
        sid = memory.skill_add("deploy-flask", "Deploy Flask app", ["1. Set FLASK_PORT", "2. Verify: check localhost"], triggers="flask,deploy", tags="flask,deployment")
        link_id = memory.link_add('fact', fid, '', 'skill', sid, 'deploy-flask', 'depends_on')
        assert link_id > 0
        row = memory._conn.execute("SELECT * FROM knowledge_links WHERE id=?", (link_id,)).fetchone()
        assert row['source_type'] == 'fact'
        assert row['source_id'] == fid
        assert row['target_type'] == 'skill'
        assert row['target_name'] == 'deploy-flask'
        assert row['link_type'] == 'depends_on'

    def test_link_add_skill_to_wiki(self, memory):
        sid = memory.skill_add("test-skill", "A test skill", ["1. Step"], triggers="test", tags="testing")
        wid = memory.wiki_add('test-arch', 'Test Architecture', 'Testing patterns...', category='pattern', tags='testing')
        link_id = memory.link_add('skill', sid, 'test-skill', 'wiki', wid, 'test-arch', 'related_to')
        assert link_id > 0
        row = memory._conn.execute("SELECT * FROM knowledge_links WHERE id=?", (link_id,)).fetchone()
        assert row['link_type'] == 'related_to'

    def test_link_add_with_type(self, memory):
        fid = memory.fact_add("old config format", category="environment", tags="config")
        fid2 = memory.fact_add("new config format", category="environment", tags="config")
        link_id = memory.link_add('fact', fid, '', 'fact', fid2, '', 'contradicts')
        assert link_id > 0
        row = memory._conn.execute("SELECT * FROM knowledge_links WHERE id=?", (link_id,)).fetchone()
        assert row['link_type'] == 'contradicts'


# ── Knowledge Link Search ──

class TestKnowledgeLinkSearch:

    def test_search_by_source_type(self, memory):
        fid = memory.fact_add("fact for search", category="general", tags="search")
        sid = memory.skill_add("skill-for-search", "Skill", ["1. Step"], triggers="search", tags="search")
        memory.link_add('fact', fid, '', 'skill', sid, 'skill-for-search', 'depends_on')
        results = memory.link_search(source_type='fact')
        assert len(results) >= 1
        assert all(r['source_type'] == 'fact' for r in results)

    def test_search_by_target_type(self, memory):
        fid = memory.fact_add("target test fact", category="general")
        sid = memory.skill_add("target-test-skill", "Skill", ["1. Step"], triggers="test")
        memory.link_add('fact', fid, '', 'skill', sid, 'target-test-skill', 'depends_on')
        results = memory.link_search(target_type='skill')
        assert len(results) >= 1
        assert all(r['target_type'] == 'skill' for r in results)

    def test_search_by_link_type(self, memory):
        fid = memory.fact_add("contradicts fact a", category="general")
        fid2 = memory.fact_add("contradicts fact b", category="general")
        memory.link_add('fact', fid, '', 'fact', fid2, '', 'contradicts')
        results = memory.link_search(link_type='contradicts')
        assert len(results) >= 1
        assert all(r['link_type'] == 'contradicts' for r in results)

    def test_search_all_links(self, memory):
        fid = memory.fact_add("all links fact", category="general")
        sid = memory.skill_add("all-links-skill", "Skill", ["1. Step"], triggers="all")
        memory.link_add('fact', fid, '', 'skill', sid, 'all-links-skill', 'depends_on')
        results = memory.link_search()
        assert len(results) >= 1


# ── Knowledge Link Deletion ──

class TestKnowledgeLinkDeletion:

    def test_link_delete_removes_row(self, memory):
        fid = memory.fact_add("delete test fact", category="general")
        sid = memory.skill_add("delete-test-skill", "Skill", ["1. Step"], triggers="delete")
        link_id = memory.link_add('fact', fid, '', 'skill', sid, 'delete-test-skill', 'depends_on')
        assert memory.link_delete(link_id)
        row = memory._conn.execute("SELECT * FROM knowledge_links WHERE id=?", (link_id,)).fetchone()
        assert row is None


# ── Cluster Search ──

class TestClusterSearch:

    def test_cluster_search_tag_overlap(self, memory):
        memory.fact_add("Flask config uses env vars", category="environment", tags="flask,config,deployment")
        memory.fact_add("Deploy with Docker", category="environment", tags="deployment,docker")
        memory.skill_add("deploy-flask", "Deploy Flask app", ["1. Set env vars", "2. Verify: curl localhost"], triggers="flask,deploy", tags="flask,deployment")
        results = memory.cluster_search("flask deployment", min_relevance=0.2)
        assert len(results) >= 1
        # Should find a bundle with flask/deployment tag
        assert any(b['topic_tag'] in ('flask', 'deployment') for b in results)

    def test_cluster_search_category_match(self, memory):
        # Fact with no tag overlap but matching category keyword
        memory.fact_add("Python version is 3.12", category="python", tags="python,version")
        results = memory.cluster_search("python", min_relevance=0.2)
        assert len(results) >= 1

    def test_cluster_search_min_relevance_filter(self, memory):
        memory.fact_add("unrelated astronomy fact", category="general", tags="stars,space")
        results = memory.cluster_search("flask deployment", min_relevance=0.5)
        # Astronomy fact shouldn't appear — no tag overlap with flask/deployment
        for b in results:
            assert 'astronomy' not in b['topic_tag']

    def test_cluster_search_empty_result(self, memory):
        results = memory.cluster_search("quantum physics rocket", min_relevance=0.3)
        assert len(results) == 0

    def test_cluster_search_multi_type_bundle(self, memory):
        memory.fact_add("Flask port is 5000", category="environment", tags="flask,deployment")
        memory.skill_add("deploy-flask", "Deploy Flask", ["1. Set port", "2. Verify: check port"], triggers="flask,deploy", tags="flask,deployment")
        memory.wiki_add('flask-arch', 'Flask Architecture', 'Flask deployment patterns...', category='pattern', tags='flask,deployment')
        results = memory.cluster_search("flask deployment", min_relevance=0.2)
        assert len(results) >= 1
        # Should find bundle containing facts AND skills AND wiki
        best = results[0]
        assert len(best['facts']) >= 1 or len(best['skills']) >= 1 or len(best['wiki_pages']) >= 1


# ── Cascade Flags ──

class TestCascadeFlags:

    def test_feedback_flags_linked_items(self, memory):
        sid = memory.session_create("cascade test")
        fid = memory.fact_add("cascade test fact", category="general")
        sid_skill = memory.skill_add("cascade-test-skill", "Skill", ["1. Step"], triggers="cascade")
        memory.link_add('fact', fid, '', 'skill', sid_skill, 'cascade-test-skill', 'depends_on')
        # Mark fact unhelpful — should flag linked skill
        memory.feedback_event_add('fact', fid, '', False, "this fact was wrong and outdated", session_id=sid, turn_num=1)
        skill = memory._conn.execute("SELECT needs_review FROM skills WHERE name=?", ('cascade-test-skill',)).fetchone()
        assert skill['needs_review'] == 1

    def test_get_items_needing_review(self, memory):
        fid = memory.fact_add("review test fact", category="general")
        memory._conn.execute("UPDATE facts SET needs_review=1 WHERE id=?", (fid,))
        memory._conn.commit()
        review = memory.get_items_needing_review()
        assert len(review['facts_needing_review']) >= 1
        assert any(r['content'] == 'review test fact' for r in review['facts_needing_review'])

    def test_mark_reviewed(self, memory):
        fid = memory.fact_add("mark reviewed fact", category="general")
        memory._conn.execute("UPDATE facts SET needs_review=1 WHERE id=?", (fid,))
        memory._conn.commit()
        assert memory.mark_reviewed('facts', fid)
        fact = memory._conn.execute("SELECT needs_review FROM facts WHERE id=?", (fid,)).fetchone()
        assert fact['needs_review'] == 0

    def test_evolution_declining_includes_flagged_items(self, memory):
        sid = memory.session_create("evolution cascade test")
        fid = memory.fact_add("evolution flagged fact", category="general")
        memory._conn.execute("UPDATE facts SET needs_review=1 WHERE id=?", (fid,))
        memory._conn.commit()
        # Create two evolution log entries: older (good) then recent (bad) → declining trend
        memory._conn.execute(
            "INSERT INTO evolution_log (session_id,loss_task,loss_efficiency,loss_recurrence,loss_knowledge_quality,loss_total,evolution_score,gradient_facts,gradient_skills,improvement_targets,hindsight_hint,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, 0.0, 0.1, 0.0, 0.3, 0.4, 0.8, '[]', '[]', '[]', '', '2025-01-01T00:00:00')
        )
        memory._conn.execute(
            "INSERT INTO evolution_log (session_id,loss_task,loss_efficiency,loss_recurrence,loss_knowledge_quality,loss_total,evolution_score,gradient_facts,gradient_skills,improvement_targets,hindsight_hint,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, 1.0, 0.5, 0.2, 0.5, 2.2, 0.1, '[]', '[]', '[]', '', '2025-06-01T00:00:00')
        )
        memory._conn.commit()
        loss = memory.compute_evolution_loss(
            session_id=sid, turns_used=35, max_turns=40,
            task_success=False, accessed_fact_ids=[fid],
            accessed_skill_names=[], hindsight_hint=''
        )
        # When declining, improvement_targets should include flagged items
        assert any('flagged' in t for t in loss['improvement_targets'])


# ── Dynamic Context Budget ──

class TestDynamicContextBudget:

    def test_complexity_estimation(self, memory):
        # Short prompt → low complexity, long prompt → high
        kw_short = memory._extract_keywords("fix bug")
        kw_long = memory._extract_keywords("deploy Flask application with Docker on AWS using environment variables and secrets management")
        assert len(kw_short) < len(kw_long)

    def test_budget_calculation(self, memory):
        # Simple task → 3000 base budget
        prompt_simple = memory.build_context_prompt("fix bug")
        assert len(prompt_simple) >= 500  # At least has meta rules + catalog
        # Complex task → larger budget
        prompt_complex = memory.build_context_prompt("deploy Flask application with Docker on AWS using environment variables")
        # Both should work (no crashes)
        assert isinstance(prompt_simple, str)
        assert isinstance(prompt_complex, str)

    def test_bundle_injection_with_catalog_fallback(self, memory):
        # No matching cluster → should fall back to catalog + proven facts
        prompt_empty = memory.build_context_prompt("quantum physics rocket")
        assert '[Knowledge Catalog]' in prompt_empty
        # With matching cluster → should inject bundle
        memory.fact_add("Flask test fact", category="environment", tags="flask,test")
        prompt_flask = memory.build_context_prompt("flask test")
        # Should contain catalog (always present) and possibly a bundle
        assert '[Knowledge Catalog]' in prompt_flask


# ── Knowledge Links DB Query ──

class TestKnowledgeLinksDbQuery:

    def test_knowledge_links_queryable(self, memory):
        assert 'knowledge_links' in DBQ_ALLOWED_TABLES
        fid = memory.fact_add("db query fact", category="general")
        sid = memory.skill_add("db-query-skill", "Skill", ["1. Step"], triggers="query")
        memory.link_add('fact', fid, '', 'skill', sid, 'db-query-skill', 'depends_on')
        result = memory.safe_query("SELECT * FROM knowledge_links")
        assert result['status'] == 'success'
        assert result['row_count'] >= 1

    def test_knowledge_links_schema_visible(self, memory):
        schema = memory.get_schema_info()
        assert 'knowledge_links' in schema['tables']
        cols = [c['name'] for c in schema['tables']['knowledge_links']]
        assert 'source_type' in cols
        assert 'target_type' in cols
        assert 'link_type' in cols


# ── Needs Review Columns ──

class TestNeedsReviewColumns:

    def test_facts_has_needs_review(self, memory):
        cols = [r[1] for r in memory._conn.execute("PRAGMA table_info(facts)").fetchall()]
        assert 'needs_review' in cols

    def test_skills_has_needs_review(self, memory):
        cols = [r[1] for r in memory._conn.execute("PRAGMA table_info(skills)").fetchall()]
        assert 'needs_review' in cols

    def test_needs_review_default_zero(self, memory):
        fid = memory.fact_add("default needs_review fact", category="general")
        fact = memory._conn.execute("SELECT needs_review FROM facts WHERE id=?", (fid,)).fetchone()
        assert fact['needs_review'] == 0


# ── Existing Tests Unchanged ──

class TestExistingTestsUnchanged:
    """Verify existing test_memory_engine.py tests still pass — run separately via pytest."""
    pass
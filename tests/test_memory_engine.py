"""Tests for Nova Memory Engine — trust, routing, wiki, facts, SQL sandbox, concurrent writes."""

import os
import threading
import tempfile
import time

from nova.memory.engine import (
    NovaMemory, TwoTierMemory, _route_tier, _fts_escape,
    DBQ_BLOCKED_KEYWORDS, DBQ_ALLOWED_OPS, DBQ_ALLOWED_TABLES, DBQ_MAX_ROWS,
)


# ── Trust Evolution ──

class TestTrustEvolution:

    def test_fact_initial_trust(self, local_memory):
        fid = local_memory.fact_add("test fact", category="general", trust_score=0.5)
        fact = local_memory._conn.execute("SELECT trust_score FROM facts WHERE id=?", (fid,)).fetchone()
        assert fact[0] == 0.5

    def test_mark_helpful_increases_trust(self, local_memory):
        fid = local_memory.fact_add("helpful fact", category="general")
        local_memory.fact_mark_helpful_by_id(fid)
        fact = local_memory._conn.execute("SELECT trust_score FROM facts WHERE id=?", (fid,)).fetchone()
        assert fact[0] == 0.55  # 0.5 + 0.05

    def test_mark_unhelpful_decreases_trust(self, local_memory):
        fid = local_memory.fact_add("bad fact", category="general")
        local_memory.fact_mark_unhelpful("bad fact")
        fact = local_memory._conn.execute("SELECT trust_score FROM facts WHERE id=?", (fid,)).fetchone()
        assert fact[0] == 0.40  # 0.5 - 0.10

    def test_trust_caps_at_1(self, local_memory):
        fid = local_memory.fact_add("super fact", category="general", trust_score=0.97)
        local_memory.fact_mark_helpful_by_id(fid)
        local_memory.fact_mark_helpful_by_id(fid)
        fact = local_memory._conn.execute("SELECT trust_score FROM facts WHERE id=?", (fid,)).fetchone()
        assert fact[0] == 1.0

    def test_trust_floor_at_0(self, local_memory):
        fid = local_memory.fact_add("terrible fact", category="general", trust_score=0.05)
        local_memory.fact_mark_unhelpful("terrible fact")
        fact = local_memory._conn.execute("SELECT trust_score FROM facts WHERE id=?", (fid,)).fetchone()
        assert fact[0] == 0.0

    def test_retrieval_bumps_trust(self, local_memory):
        fid = local_memory.fact_add("retrieved fact", category="general")
        local_memory.fact_search("retrieved fact")
        fact = local_memory._conn.execute("SELECT trust_score,retrieval_count FROM facts WHERE id=?", (fid,)).fetchone()
        assert fact[0] == 0.51  # 0.5 + 0.01
        assert fact[1] == 1

    def test_decay_auto_delete_below_015(self, local_memory):
        fid = local_memory.fact_add("decaying fact", category="general", trust_score=0.10)
        deleted = local_memory.decay_low_trust_facts(min_trust=0.15)
        assert deleted >= 1
        remaining = local_memory._conn.execute("SELECT COUNT(*) FROM facts WHERE id=?", (fid,)).fetchone()[0]
        assert remaining == 0


# ── Category Routing ──

class TestCategoryRouting:

    def test_environment_routes_local(self):
        assert _route_tier('environment', 'auto') == 'local'

    def test_debugging_routes_local(self):
        assert _route_tier('debugging', 'auto') == 'local'

    def test_session_log_routes_local(self):
        assert _route_tier('session-log', 'auto') == 'local'

    def test_pattern_routes_global(self):
        assert _route_tier('pattern', 'auto') == 'global'

    def test_convention_routes_global(self):
        assert _route_tier('convention', 'auto') == 'global'

    def test_decision_routes_global(self):
        assert _route_tier('decision', 'auto') == 'global'

    def test_explicit_tier_overrides_routing(self):
        assert _route_tier('pattern', 'local') == 'local'
        assert _route_tier('environment', 'global') == 'global'


# ── Two-Tier Routing in TwoTierMemory ──

class TestTwoTierRouting:

    def test_environment_fact_stored_in_local(self, two_tier_memory):
        fid = two_tier_memory.fact_add("local path fact", category="environment", tier="auto")
        local_fact = two_tier_memory._local._conn.execute("SELECT COUNT(*) FROM facts WHERE content=?", ("local path fact",)).fetchone()[0]
        assert local_fact >= 1

    def test_pattern_fact_stored_in_global(self, two_tier_memory):
        fid = two_tier_memory.fact_add("global pattern fact", category="pattern", tier="auto")
        global_fact = two_tier_memory._global._conn.execute("SELECT COUNT(*) FROM facts WHERE content=?", ("global pattern fact",)).fetchone()[0]
        assert global_fact >= 1


# ── Wiki Operations ──

class TestWikiOperations:

    def test_wiki_add(self, local_memory):
        pid = local_memory.wiki_add('test-page', 'Test Page', 'Some content', category='reference')
        assert pid > 0
        page = local_memory.wiki_read('test-page')
        assert page['title'] == 'Test Page'
        assert 'Some content' in page['content']

    def test_wiki_ingest_append(self, local_memory):
        local_memory.wiki_add('guide', 'Guide', 'Part 1', category='reference')
        pid = local_memory.wiki_ingest('Guide', 'Part 2', 'guide', category='reference')
        page = local_memory.wiki_read('guide')
        assert 'Part 1' in page['content']
        assert 'Part 2' in page['content']

    def test_wiki_ingest_dedup(self, local_memory):
        local_memory.wiki_add('dup', 'Dup', 'Original content here', category='reference')
        pid = local_memory.wiki_ingest('Dup', 'Original content here', 'dup', category='reference')
        page = local_memory.wiki_read('dup')
        # Dedup should prevent append — content stays the same
        assert page['content'] == 'Original content here'

    def test_wiki_query_fts(self, local_memory):
        local_memory.wiki_add('python-tips', 'Python Tips', 'Use list comprehensions in Python', category='reference')
        results = local_memory.wiki_query('python')
        assert len(results) >= 1
        assert any('python' in r['title'].lower() or 'python' in r['content'].lower() for r in results)

    def test_wiki_list(self, local_memory):
        local_memory.wiki_add('a', 'A', 'content', category='reference')
        local_memory.wiki_add('b', 'B', 'content', category='reference')
        pages = local_memory.wiki_list()
        assert len(pages) >= 2

    def test_wiki_delete(self, local_memory):
        local_memory.wiki_add('del-me', 'Delete Me', 'gone', category='reference')
        assert local_memory.wiki_delete('del-me')
        assert local_memory.wiki_read('del-me') is None


# ── FTS5 Search ──

class TestFactSearch:

    def test_fact_search_finds_matching_facts(self, local_memory):
        local_memory.fact_add("docker containers are lightweight", category="architecture")
        local_memory.fact_add("python uses duck typing", category="pattern")
        results = local_memory.fact_search("docker")
        assert len(results) >= 1
        assert any('docker' in r['content'].lower() for r in results)

    def test_fact_search_min_trust_filter(self, local_memory):
        fid = local_memory.fact_add("low trust fact", category="general", trust_score=0.2)
        results = local_memory.fact_search("low trust", min_trust=0.5)
        # Should not return facts below min_trust
        assert not any(r['id'] == fid for r in results)


# ── SQL Sandbox ──

class TestSQLSandbox:

    def test_select_allowed(self, local_memory):
        result = local_memory.safe_query("SELECT COUNT(*) as cnt FROM facts")
        assert result['status'] == 'success'

    def test_insert_allowed(self, local_memory):
        result = local_memory.safe_query("INSERT INTO facts (content, category, tags, trust_score, created_at, updated_at) VALUES ('sandbox test', 'general', '', 0.5, '2026-01-01', '2026-01-01')")
        assert result['status'] == 'success'

    def test_update_allowed(self, local_memory):
        fid = local_memory.fact_add("update target", category="general")
        result = local_memory.safe_query(f"UPDATE facts SET trust_score=0.8 WHERE id={fid}")
        assert result['status'] == 'success'

    def test_delete_blocked(self, local_memory):
        result = local_memory.safe_query("DELETE FROM facts WHERE id=1")
        assert result['status'] == 'error'
        assert 'DELETE' in result['msg'] or 'Blocked' in result['msg']

    def test_drop_blocked(self, local_memory):
        result = local_memory.safe_query("DROP TABLE facts")
        assert result['status'] == 'error'
        assert 'DROP' in result['msg'] or 'Blocked' in result['msg']

    def test_alter_blocked(self, local_memory):
        result = local_memory.safe_query("ALTER TABLE facts ADD COLUMN x TEXT")
        assert result['status'] == 'error'
        assert 'ALTER' in result['msg'] or 'Blocked' in result['msg']

    def test_create_blocked(self, local_memory):
        result = local_memory.safe_query("CREATE TABLE evil (id INT)")
        assert result['status'] == 'error'
        assert 'CREATE' in result['msg'] or 'Blocked' in result['msg']

    def test_pragma_blocked(self, local_memory):
        result = local_memory.safe_query("PRAGMA table_info(facts)")
        assert result['status'] == 'error'
        assert 'PRAGMA' in result['msg'] or 'Blocked' in result['msg']

    def test_disallowed_table_blocked(self, local_memory):
        result = local_memory.safe_query("SELECT * FROM sqlite_master")
        assert result['status'] == 'error'
        assert 'sqlite_master' in result['msg'] or 'not in allowed' in result['msg']

    def test_max_rows_cap(self, local_memory):
        for i in range(60):
            local_memory.fact_add(f"row fact {i}", category="general")
        result = local_memory.safe_query("SELECT * FROM facts")
        assert result['status'] == 'success'
        assert result['row_count'] <= DBQ_MAX_ROWS


# ── Stats ──

class TestStats:

    def test_two_tier_stats(self, two_tier_memory):
        stats = two_tier_memory.stats()
        assert 'local_facts' in stats
        assert 'global_facts' in stats
        assert 'local_wiki_pages' in stats
        assert 'global_wiki_pages' in stats
        assert isinstance(stats['local_avg_trust'], float)


# ── Concurrent Writes ──

class TestConcurrentWrites:

    def test_multithread_writes(self, two_tier_memory):
        errors = []
        facts_written = [0]

        def writer(tier, idx):
            try:
                two_tier_memory.fact_add(
                    f"thread-{tier}-{idx} fact",
                    category="general", tier=tier
                )
                facts_written[0] += 1
            except Exception as e:
                errors.append(str(e))

        threads = []
        for i in range(4):
            threads.append(threading.Thread(target=writer, args=("local", i)))
            threads.append(threading.Thread(target=writer, args=("global", i)))

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert facts_written[0] == 8


# ── Proactive Recall ──

class TestProactiveRecall:

    def test_proactive_recall_returns_relevant(self, two_tier_memory):
        two_tier_memory.fact_add("pytest is a testing framework", category="general", tier="local")
        result = two_tier_memory.proactive_recall("test pytest framework")
        assert "pytest" in result.lower() or result != ""


# ── Time Decay ──

class TestTimeDecay:

    def test_environment_decay_fast(self, local_memory):
        fid = local_memory.fact_add("env config path", category="environment", trust_score=0.7)
        # Force updated_at to 6 months ago
        old_time = "2025-10-17T00:00:00"
        local_memory._conn.execute("UPDATE facts SET updated_at=? WHERE id=?", (old_time, fid))
        local_memory._conn.commit()
        local_memory.apply_time_decay()
        fact = local_memory._conn.execute("SELECT trust_score FROM facts WHERE id=?", (fid,)).fetchone()
        # 0.06/month * 6 months = 0.36 decay → 0.7 - 0.36 = 0.34
        assert fact[0] < 0.7

    def test_pattern_decay_slow(self, local_memory):
        fid = local_memory.fact_add("reusable pattern", category="pattern", trust_score=0.7)
        old_time = "2025-10-17T00:00:00"
        local_memory._conn.execute("UPDATE facts SET updated_at=? WHERE id=?", (old_time, fid))
        local_memory._conn.commit()
        local_memory.apply_time_decay()
        fact = local_memory._conn.execute("SELECT trust_score FROM facts WHERE id=?", (fid,)).fetchone()
        # 0.01/month * 6 months = 0.06 decay → 0.7 - 0.06 = 0.64
        assert fact[0] > 0.60

    def test_high_retrieval_resists_decay(self, local_memory):
        fid = local_memory.fact_add("frequently used fact", category="environment", trust_score=0.7)
        # Set retrieval_count >= 5 to resist decay
        local_memory._conn.execute("UPDATE facts SET retrieval_count=5, updated_at='2025-10-17T00:00:00' WHERE id=?", (fid,))
        local_memory._conn.commit()
        local_memory.apply_time_decay()
        fact = local_memory._conn.execute("SELECT trust_score FROM facts WHERE id=?", (fid,)).fetchone()
        assert fact[0] == 0.7  # No decay applied
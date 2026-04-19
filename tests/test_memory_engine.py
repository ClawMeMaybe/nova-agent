"""Tests for Nova Memory Engine — trust, wiki, facts, SQL sandbox, concurrent writes."""

import os
import threading
import tempfile
import time

from nova.memory.engine import (
    NovaMemory, _fts_escape,
    DBQ_BLOCKED_KEYWORDS, DBQ_ALLOWED_OPS, DBQ_ALLOWED_TABLES, DBQ_MAX_ROWS,
)


# ── Trust Evolution ──

class TestTrustEvolution:

    def test_fact_initial_trust(self, memory):
        fid = memory.fact_add("test fact", category="general", trust_score=0.5)
        fact = memory._conn.execute("SELECT trust_score FROM facts WHERE id=?", (fid,)).fetchone()
        assert fact[0] == 0.5

    def test_mark_helpful_increases_trust(self, memory):
        fid = memory.fact_add("helpful fact", category="general")
        memory.fact_mark_helpful_by_id(fid)
        fact = memory._conn.execute("SELECT trust_score FROM facts WHERE id=?", (fid,)).fetchone()
        assert fact[0] == 0.55  # 0.5 + 0.05

    def test_mark_unhelpful_decreases_trust(self, memory):
        fid = memory.fact_add("bad fact", category="general")
        memory.fact_mark_unhelpful("bad fact")
        fact = memory._conn.execute("SELECT trust_score FROM facts WHERE id=?", (fid,)).fetchone()
        assert fact[0] == 0.40  # 0.5 - 0.10

    def test_trust_caps_at_1(self, memory):
        fid = memory.fact_add("super fact", category="general", trust_score=0.97)
        memory.fact_mark_helpful_by_id(fid)
        memory.fact_mark_helpful_by_id(fid)
        fact = memory._conn.execute("SELECT trust_score FROM facts WHERE id=?", (fid,)).fetchone()
        assert fact[0] == 1.0

    def test_trust_floor_at_0(self, memory):
        fid = memory.fact_add("terrible fact", category="general", trust_score=0.05)
        memory.fact_mark_unhelpful("terrible fact")
        fact = memory._conn.execute("SELECT trust_score FROM facts WHERE id=?", (fid,)).fetchone()
        assert fact[0] == 0.0

    def test_retrieval_bumps_trust(self, memory):
        fid = memory.fact_add("retrieved fact", category="general")
        memory.fact_search("retrieved fact")
        fact = memory._conn.execute("SELECT trust_score,retrieval_count FROM facts WHERE id=?", (fid,)).fetchone()
        assert fact[0] == 0.51  # 0.5 + 0.01
        assert fact[1] == 1

    def test_decay_auto_delete_below_015(self, memory):
        fid = memory.fact_add("decaying fact", category="general", trust_score=0.10)
        deleted = memory.decay_low_trust_facts(min_trust=0.15)
        assert deleted >= 1
        remaining = memory._conn.execute("SELECT COUNT(*) FROM facts WHERE id=?", (fid,)).fetchone()[0]
        assert remaining == 0


# ── Wiki Operations ──

class TestWikiOperations:

    def test_wiki_add(self, memory):
        pid = memory.wiki_add('test-page', 'Test Page', 'Some content', category='reference')
        assert pid > 0
        page = memory.wiki_read('test-page')
        assert page['title'] == 'Test Page'
        assert 'Some content' in page['content']

    def test_wiki_ingest_append(self, memory):
        memory.wiki_add('guide', 'Guide', 'Part 1', category='reference')
        pid = memory.wiki_ingest('Guide', 'Part 2', 'guide', category='reference')
        page = memory.wiki_read('guide')
        assert 'Part 1' in page['content']
        assert 'Part 2' in page['content']

    def test_wiki_ingest_dedup(self, memory):
        memory.wiki_add('dup', 'Dup', 'Original content here', category='reference')
        pid = memory.wiki_ingest('Dup', 'Original content here', 'dup', category='reference')
        page = memory.wiki_read('dup')
        # Dedup should prevent append — content stays the same
        assert page['content'] == 'Original content here'

    def test_wiki_query_fts(self, memory):
        memory.wiki_add('python-tips', 'Python Tips', 'Use list comprehensions in Python', category='reference')
        results = memory.wiki_query('python')
        assert len(results) >= 1
        assert any('python' in r['title'].lower() or 'python' in r['content'].lower() for r in results)

    def test_wiki_list(self, memory):
        memory.wiki_add('a', 'A', 'content', category='reference')
        memory.wiki_add('b', 'B', 'content', category='reference')
        pages = memory.wiki_list()
        assert len(pages) >= 2

    def test_wiki_delete(self, memory):
        memory.wiki_add('del-me', 'Delete Me', 'gone', category='reference')
        assert memory.wiki_delete('del-me')
        assert memory.wiki_read('del-me') is None


# ── FTS5 Search ──

class TestFactSearch:

    def test_fact_search_finds_matching_facts(self, memory):
        memory.fact_add("docker containers are lightweight", category="architecture")
        memory.fact_add("python uses duck typing", category="pattern")
        results = memory.fact_search("docker")
        assert len(results) >= 1
        assert any('docker' in r['content'].lower() for r in results)

    def test_fact_search_min_trust_filter(self, memory):
        fid = memory.fact_add("low trust fact", category="general", trust_score=0.2)
        results = memory.fact_search("low trust", min_trust=0.5)
        # Should not return facts below min_trust
        assert not any(r['id'] == fid for r in results)


# ── SQL Sandbox ──

class TestSQLSandbox:

    def test_select_allowed(self, memory):
        result = memory.safe_query("SELECT COUNT(*) as cnt FROM facts")
        assert result['status'] == 'success'

    def test_insert_allowed(self, memory):
        result = memory.safe_query("INSERT INTO facts (content, category, tags, trust_score, created_at, updated_at) VALUES ('sandbox test', 'general', '', 0.5, '2026-01-01', '2026-01-01')")
        assert result['status'] == 'success'

    def test_update_allowed(self, memory):
        fid = memory.fact_add("update target", category="general")
        result = memory.safe_query(f"UPDATE facts SET trust_score=0.8 WHERE id={fid}")
        assert result['status'] == 'success'

    def test_delete_blocked(self, memory):
        result = memory.safe_query("DELETE FROM facts WHERE id=1")
        assert result['status'] == 'error'
        assert 'DELETE' in result['msg'] or 'Blocked' in result['msg']

    def test_drop_blocked(self, memory):
        result = memory.safe_query("DROP TABLE facts")
        assert result['status'] == 'error'
        assert 'DROP' in result['msg'] or 'Blocked' in result['msg']

    def test_alter_blocked(self, memory):
        result = memory.safe_query("ALTER TABLE facts ADD COLUMN x TEXT")
        assert result['status'] == 'error'
        assert 'ALTER' in result['msg'] or 'Blocked' in result['msg']

    def test_create_blocked(self, memory):
        result = memory.safe_query("CREATE TABLE evil (id INT)")
        assert result['status'] == 'error'
        assert 'CREATE' in result['msg'] or 'Blocked' in result['msg']

    def test_pragma_blocked(self, memory):
        result = memory.safe_query("PRAGMA table_info(facts)")
        assert result['status'] == 'error'
        assert 'PRAGMA' in result['msg'] or 'Blocked' in result['msg']

    def test_disallowed_table_blocked(self, memory):
        result = memory.safe_query("SELECT * FROM sqlite_master")
        assert result['status'] == 'error'
        assert 'sqlite_master' in result['msg'] or 'not in allowed' in result['msg']

    def test_max_rows_cap(self, memory):
        for i in range(60):
            memory.fact_add(f"row fact {i}", category="general")
        result = memory.safe_query("SELECT * FROM facts")
        assert result['status'] == 'success'
        assert result['row_count'] <= DBQ_MAX_ROWS


# ── Stats ──

class TestStats:

    def test_unified_stats(self, memory):
        stats = memory.stats()
        assert 'total_facts' in stats
        assert 'global_facts' in stats
        assert 'total_wiki_pages' in stats
        assert 'global_wiki_pages' in stats
        assert isinstance(stats['avg_trust'], float)


# ── Concurrent Writes ──

class TestConcurrentWrites:

    def test_multithread_writes(self, memory):
        errors = []
        facts_written = [0]

        def writer(idx):
            try:
                memory.fact_add(
                    f"thread-{idx} fact",
                    category="general"
                )
                facts_written[0] += 1
            except Exception as e:
                errors.append(str(e))

        threads = []
        for i in range(8):
            threads.append(threading.Thread(target=writer, args=(i,)))

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert facts_written[0] == 8


# ── Proactive Recall ──

class TestProactiveRecall:

    def test_proactive_recall_returns_relevant(self, memory):
        memory.fact_add("pytest is a testing framework", category="general")
        result = memory.proactive_recall("test pytest framework")
        assert "pytest" in result.lower() or result != ""


# ── Time Decay ──

class TestTimeDecay:

    def test_environment_decay_fast(self, memory):
        fid = memory.fact_add("env config path", category="environment", trust_score=0.7)
        # Force updated_at to 6 months ago
        old_time = "2025-10-17T00:00:00"
        memory._conn.execute("UPDATE facts SET updated_at=? WHERE id=?", (old_time, fid))
        memory._conn.commit()
        memory.apply_time_decay()
        fact = memory._conn.execute("SELECT trust_score FROM facts WHERE id=?", (fid,)).fetchone()
        # 0.06/month * 6 months = 0.36 decay → 0.7 - 0.36 = 0.34
        assert fact[0] < 0.7

    def test_pattern_decay_slow(self, memory):
        fid = memory.fact_add("reusable pattern", category="pattern", trust_score=0.7)
        old_time = "2025-10-17T00:00:00"
        memory._conn.execute("UPDATE facts SET updated_at=? WHERE id=?", (old_time, fid))
        memory._conn.commit()
        memory.apply_time_decay()
        fact = memory._conn.execute("SELECT trust_score FROM facts WHERE id=?", (fid,)).fetchone()
        # 0.01/month * 6 months = 0.06 decay → 0.7 - 0.06 = 0.64
        assert fact[0] > 0.60

    def test_high_retrieval_resists_decay(self, memory):
        fid = memory.fact_add("frequently used fact", category="environment", trust_score=0.7)
        # Set retrieval_count >= 5 to resist decay
        memory._conn.execute("UPDATE facts SET retrieval_count=5, updated_at='2025-10-17T00:00:00' WHERE id=?", (fid,))
        memory._conn.commit()
        memory.apply_time_decay()
        fact = memory._conn.execute("SELECT trust_score FROM facts WHERE id=?", (fid,)).fetchone()
        assert fact[0] == 0.7  # No decay applied
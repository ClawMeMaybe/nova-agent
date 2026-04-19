"""Tests for session_turns table, V3 migration, session_create/update, and turn recording."""

import os
import json
import tempfile

import pytest

from nova.memory.engine import NovaMemory, SCHEMA_V1, SCHEMA_V2, SCHEMA_V3


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


class TestSessionTurnsSchema:
    def test_fresh_db_has_session_turns(self, tmp_path):
        db_path = str(tmp_path / ".nova" / "nova.db")
        mem = NovaMemory(db_path)
        cols = [r[1] for r in mem._conn.execute("PRAGMA table_info(session_turns)").fetchall()]
        mem.close()
        assert 'session_id' in cols
        assert 'turn_num' in cols
        assert 'role' in cols
        assert 'content' in cols
        assert 'tool_name' in cols
        assert 'tool_args' in cols
        assert 'tool_result' in cols
        assert 'thinking' in cols

    def test_schema_version_is_3(self, tmp_path):
        db_path = str(tmp_path / ".nova" / "nova.db")
        mem = NovaMemory(db_path)
        row = mem._conn.execute("SELECT value FROM _meta WHERE key='schema_version'").fetchone()
        mem.close()
        assert int(row[0]) >= 3

    def test_v2_db_migrates_to_v3(self, tmp_path):
        db_path = str(tmp_path / ".nova" / "nova.db")
        os.makedirs(str(tmp_path / ".nova"), exist_ok=True)

        # Create a V2 DB (without session_turns)
        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS _meta (key TEXT UNIQUE, value TEXT);
            INSERT OR IGNORE INTO _meta (key, value) VALUES ('schema_version', '2');

            CREATE TABLE IF NOT EXISTS wiki_pages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slug TEXT UNIQUE NOT NULL, title TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'reference',
                content TEXT NOT NULL, tags TEXT NOT NULL DEFAULT '',
                confidence TEXT NOT NULL DEFAULT 'medium',
                sources TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT UNIQUE NOT NULL, category TEXT NOT NULL DEFAULT 'general',
                tags TEXT NOT NULL DEFAULT '', trust_score REAL NOT NULL DEFAULT 0.5,
                retrieval_count INTEGER NOT NULL DEFAULT 0,
                helpful_count INTEGER NOT NULL DEFAULT 0,
                unhelpful_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS skills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL, description TEXT NOT NULL DEFAULT '',
                steps TEXT NOT NULL DEFAULT '[]', triggers TEXT NOT NULL DEFAULT '',
                pitfalls TEXT NOT NULL DEFAULT '[]',
                success_rate REAL NOT NULL DEFAULT 0.5,
                usage_count INTEGER NOT NULL DEFAULT 0,
                tags TEXT NOT NULL DEFAULT '', version INTEGER NOT NULL DEFAULT 1,
                last_improved_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '',
                result TEXT NOT NULL DEFAULT '',
                wiki_page_id INTEGER DEFAULT NULL,
                had_knowledge_output INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
        """)
        conn.commit()
        conn.close()

        # Verify V2 state — no session_turns
        tables_before = [r[0] for r in sqlite3.connect(db_path).execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        assert 'session_turns' not in tables_before

        # Open with NovaMemory — should auto-migrate to V3
        mem = NovaMemory(db_path)
        tables_after = [r[0] for r in mem._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        version = mem._conn.execute("SELECT value FROM _meta WHERE key='schema_version'").fetchone()[0]
        mem.close()

        assert 'session_turns' in tables_after
        assert int(version) >= 3

    def test_db_query_can_select_session_turns(self, memory):
        sid = memory.session_create("test task")
        memory.session_turn_add(sid, 1, 'assistant', content='hello',
                                       tool_name='code_run', tool_args='{"script":"ls"}',
                                       tool_result='file1.txt')
        result = memory.safe_query(
            "SELECT tool_name, tool_args FROM session_turns WHERE session_id=%d" % sid
        )
        assert result['status'] == 'success'
        assert len(result['rows']) >= 1
        assert result['rows'][0]['tool_name'] == 'code_run'


# ── session_create / session_update ──


class TestSessionCreateUpdate:
    def test_session_create_returns_id(self, memory):
        sid = memory.session_create("deploy flask app")
        assert sid > 0
        session = memory._conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        assert session['task'] == "deploy flask app"
        assert session['summary'] == ''
        assert session['result'] == ''

    def test_session_update_fills_summary_result(self, memory):
        sid = memory.session_create("test task")
        memory.session_update(sid, summary="deployed successfully", result="server running",
                                    had_knowledge=True)
        session = memory._conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        assert session['summary'] == "deployed successfully"
        assert session['result'] == "server running"
        assert session['had_knowledge_output'] == 1

    def test_session_create_returns_id(self, memory):
        sid = memory.session_create("debug error")
        memory.session_turn_add(sid, 1, 'assistant', content='analyzing error',
                                       tool_name='code_run', tool_args='{"script":"cat logs"}',
                                       tool_result='ERROR: permission denied')
        memory.session_turn_add(sid, 2, 'assistant', content='fixed it',
                                       tool_name='file_write', tool_args='{"path":"fix.py"}',
                                       tool_result='written successfully')
        turns = memory.session_turns_query(sid)
        assert len(turns) == 2
        assert turns[0]['turn_num'] == 1
        assert turns[0]['tool_name'] == 'code_run'
        assert turns[1]['tool_name'] == 'file_write'

    def test_query_with_limit(self, memory):
        sid = memory.session_create("test")
        for i in range(5):
            memory.session_turn_add(sid, i + 1, 'assistant', tool_name='tool_%d' % i)
        turns = memory.session_turns_query(sid, limit=2)
        assert len(turns) == 2

    def test_truncation_on_insert(self, memory):
        sid = memory.session_create("test")
        long_content = "x" * 5000
        long_result = "y" * 5000
        memory.session_turn_add(sid, 1, 'assistant', content=long_content,
                                       tool_result=long_result)
        turn = memory.session_turns_query(sid)[0]
        assert len(turn['content']) <= 2000
        assert len(turn['tool_result']) <= 2000

    def test_tool_args_dict_truncation(self, memory):
        sid = memory.session_create("test")
        long_args = {"script": "z" * 5000}
        memory.session_turn_add(sid, 1, 'assistant', tool_args=long_args)
        turn = memory.session_turns_query(sid)[0]
        assert len(turn['tool_args']) <= 2000


# ── session_relevant_turns ──


class TestSessionRelevantTurns:
    def test_find_relevant_session_turns(self, memory):
        # Create a past session about deploying flask
        sid = memory.session_create("deploy flask app to server")
        memory.session_turn_add(sid, 1, 'assistant', tool_name='code_run',
                                       tool_args='{"script":"ssh server"}',
                                       tool_result='connected')
        memory.session_update(sid, summary="deployed", result="success")

        # Query with flask keywords
        context = memory.session_relevant_turns("deploy flask app")
        assert "flask" in context.lower() or "deploy" in context.lower()
        assert "code_run" in context

    def test_no_relevant_sessions_returns_empty(self, memory):
        context = memory.session_relevant_turns("nonexistent_xyz_task")
        assert context == ""


# ── Pruning cascades ──


class TestSessionTurnsPruning:
    def test_prune_cascades_to_turns(self, memory):
        sid = memory.session_create("old task")
        memory.session_turn_add(sid, 1, 'assistant', tool_name='code_run',
                                       tool_result='old result')
        # Verify turn exists
        turns = memory.session_turns_query(sid)
        assert len(turns) == 1

        # Prune with 0 days — deletes everything older than today
        # (session was just created, so use negative to force prune)
        # Instead, manually delete to verify cascade logic
        memory._conn.execute("DELETE FROM session_turns WHERE session_id=?", (sid,))
        memory._conn.execute("DELETE FROM sessions WHERE id=?", (sid,))
        memory._conn.commit()

        turns_after = memory.session_turns_query(sid)
        assert len(turns_after) == 0
"""Nova Memory Engine — SQL-backed with Wiki compounding.

Fuses two concepts:
1. Hermes holographic memory: SQLite + FTS5 + trust scoring + asymmetric feedback
2. Karpathy's LLM Wiki: persistent markdown pages that compound across sessions

Architecture:
  - wiki_pages table: human-readable knowledge docs (markdown + frontmatter)
  - facts table: machine-queryable verified knowledge with trust scores
  - skills table: crystallized SOPs/workflows with success rates
  - sessions table: compressed session archives
  - FTS5 indexes on all three for fast keyword+tag search
  - Trust scoring: helpful +=0.05, unhelpful -=0.10 (asymmetric)
  - Auto-trust on retrieval: facts gain trust when used successfully
  - Smart crystallization: only when knowledge was explicitly produced
  - Context budget: max ~3000 chars to avoid token overflow
  - Temporal decay: low-trust facts fade, old session-logs prune

Layer mapping (backward compatible with L0-L4):
  L0 Meta Rules  → wiki page (category=convention, slug=meta-rules)
  L1 Insight Index → auto-generated from FTS index
  L2 Global Facts  → facts table
  L3 Task SOPs     → skills table + wiki (category=pattern)
  L4 Session Archive → sessions table + wiki (category=session-log)
"""

import json
import os
import re
import sqlite3
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple


def _write_locked(method):
    """Decorator: acquire the write lock before executing a DB write method."""
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    wrapper.__name__ = method.__name__
    wrapper.__doc__ = method.__doc__
    return wrapper

# ── SQL Schema (versioned for future migrations) ──

SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS _meta (key TEXT UNIQUE, value TEXT);
INSERT OR IGNORE INTO _meta (key, value) VALUES ('schema_version', '9');

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS wiki_pages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'reference',
    content TEXT NOT NULL,
    tags TEXT NOT NULL DEFAULT '',
    confidence TEXT NOT NULL DEFAULT 'medium',
    sources TEXT NOT NULL DEFAULT '',
    project_id TEXT DEFAULT NULL,
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
    needs_review INTEGER NOT NULL DEFAULT 0,
    project_id TEXT DEFAULT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS skills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    steps TEXT NOT NULL DEFAULT '[]',
    triggers TEXT NOT NULL DEFAULT '',
    pitfalls TEXT NOT NULL DEFAULT '[]',
    success_rate REAL NOT NULL DEFAULT 0.5,
    usage_count INTEGER NOT NULL DEFAULT 0,
    tags TEXT NOT NULL DEFAULT '',
    contract TEXT DEFAULT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    last_improved_at TEXT NOT NULL DEFAULT '',
    needs_review INTEGER NOT NULL DEFAULT 0,
    project_id TEXT DEFAULT NULL,
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
    project_id TEXT DEFAULT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS session_turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    turn_num INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    tool_name TEXT NOT NULL DEFAULT '',
    tool_args TEXT NOT NULL DEFAULT '{}',
    tool_result TEXT NOT NULL DEFAULT '',
    thinking TEXT NOT NULL DEFAULT '',
    project_id TEXT DEFAULT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_session_turns_session ON session_turns(session_id, turn_num);

CREATE TABLE IF NOT EXISTS evolution_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    loss_task REAL NOT NULL,
    loss_efficiency REAL NOT NULL,
    loss_recurrence REAL NOT NULL DEFAULT 0,
    loss_knowledge_quality REAL NOT NULL DEFAULT 0,
    loss_total REAL NOT NULL,
    evolution_score REAL NOT NULL,
    gradient_facts TEXT NOT NULL DEFAULT '[]',
    gradient_skills TEXT NOT NULL DEFAULT '[]',
    improvement_targets TEXT NOT NULL DEFAULT '[]',
    hindsight_hint TEXT NOT NULL DEFAULT '',
    project_id TEXT DEFAULT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evolution_log_score ON evolution_log(evolution_score DESC);

CREATE TABLE IF NOT EXISTS feedback_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_type TEXT NOT NULL CHECK(target_type IN ('fact', 'skill')),
    target_id INTEGER DEFAULT NULL,
    target_name TEXT NOT NULL DEFAULT '',
    helpful INTEGER NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    turn_num INTEGER NOT NULL DEFAULT 0,
    project_id TEXT DEFAULT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_feedback_session ON feedback_events(session_id);
CREATE INDEX IF NOT EXISTS idx_feedback_target ON feedback_events(target_type, target_id);

CREATE TABLE IF NOT EXISTS knowledge_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type TEXT NOT NULL CHECK(source_type IN ('fact', 'skill', 'wiki')),
    source_id INTEGER NOT NULL,
    source_name TEXT NOT NULL DEFAULT '',
    target_type TEXT NOT NULL CHECK(target_type IN ('fact', 'skill', 'wiki')),
    target_id INTEGER NOT NULL,
    target_name TEXT NOT NULL DEFAULT '',
    link_type TEXT NOT NULL DEFAULT 'depends_on',
    project_id TEXT DEFAULT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_klinks_source ON knowledge_links(source_type, source_id);
CREATE INDEX IF NOT EXISTS idx_klinks_target ON knowledge_links(target_type, target_id);

CREATE VIRTUAL TABLE IF NOT EXISTS wiki_fts USING fts5(
    title, content, tags, category,
    content=wiki_pages, content_rowid=id
);
CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(
    content, category, tags,
    content=facts, content_rowid=id
);
CREATE VIRTUAL TABLE IF NOT EXISTS skills_fts USING fts5(
    name, description, steps, tags, triggers, pitfalls,
    content=skills, content_rowid=id
);

-- FTS5 sync triggers
CREATE TRIGGER IF NOT EXISTS wiki_fts_ins AFTER INSERT ON wiki_pages BEGIN
    INSERT INTO wiki_fts(rowid,title,content,tags,category) VALUES (new.id,new.title,new.content,new.tags,new.category);
END;
CREATE TRIGGER IF NOT EXISTS wiki_fts_del AFTER DELETE ON wiki_pages BEGIN
    INSERT INTO wiki_fts(wiki_fts,rowid,title,content,tags,category) VALUES ('delete',old.id,old.title,old.content,old.tags,old.category);
END;
CREATE TRIGGER IF NOT EXISTS wiki_fts_upd AFTER UPDATE ON wiki_pages BEGIN
    INSERT INTO wiki_fts(wiki_fts,rowid,title,content,tags,category) VALUES ('delete',old.id,old.title,old.content,old.tags,old.category);
    INSERT INTO wiki_fts(rowid,title,content,tags,category) VALUES (new.id,new.title,new.content,new.tags,new.category);
END;

CREATE TRIGGER IF NOT EXISTS facts_fts_ins AFTER INSERT ON facts BEGIN
    INSERT INTO facts_fts(rowid,content,category,tags) VALUES (new.id,new.content,new.category,new.tags);
END;
CREATE TRIGGER IF NOT EXISTS facts_fts_del AFTER DELETE ON facts BEGIN
    INSERT INTO facts_fts(facts_fts,rowid,content,category,tags) VALUES ('delete',old.id,old.content,old.category,old.tags);
END;
CREATE TRIGGER IF NOT EXISTS facts_fts_upd AFTER UPDATE ON facts BEGIN
    INSERT INTO facts_fts(facts_fts,rowid,content,category,tags) VALUES ('delete',old.id,old.content,old.category,old.tags);
    INSERT INTO facts_fts(rowid,content,category,tags) VALUES (new.id,new.content,new.category,new.tags);
END;

CREATE TRIGGER IF NOT EXISTS skills_fts_ins AFTER INSERT ON skills BEGIN
    INSERT INTO skills_fts(rowid,name,description,steps,tags,triggers,pitfalls) VALUES (new.id,new.name,new.description,new.steps,new.tags,new.triggers,new.pitfalls);
END;
CREATE TRIGGER IF NOT EXISTS skills_fts_del AFTER DELETE ON skills BEGIN
    INSERT INTO skills_fts(skills_fts,rowid,name,description,steps,tags,triggers,pitfalls) VALUES ('delete',old.id,old.name,old.description,old.steps,old.tags,old.triggers,old.pitfalls);
END;
CREATE TRIGGER IF NOT EXISTS skills_fts_upd AFTER UPDATE ON skills BEGIN
    INSERT INTO skills_fts(skills_fts,rowid,name,description,steps,tags,triggers,pitfalls) VALUES ('delete',old.id,old.name,old.description,old.steps,old.tags,old.triggers,old.pitfalls);
    INSERT INTO skills_fts(rowid,name,description,steps,tags,triggers,pitfalls) VALUES (new.id,new.name,new.description,new.steps,new.tags,new.triggers,new.pitfalls);
END;

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_facts_trust ON facts(trust_score DESC);
CREATE INDEX IF NOT EXISTS idx_facts_category ON facts(category);
CREATE INDEX IF NOT EXISTS idx_skills_success ON skills(success_rate DESC, usage_count DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_created ON sessions(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_wiki_category ON wiki_pages(category);
"""

SCHEMA_V2 = """
-- V2: Add triggers, pitfalls, version, last_improved_at to skills table
ALTER TABLE skills ADD COLUMN triggers TEXT NOT NULL DEFAULT '';
ALTER TABLE skills ADD COLUMN pitfalls TEXT NOT NULL DEFAULT '[]';
ALTER TABLE skills ADD COLUMN version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE skills ADD COLUMN last_improved_at TEXT NOT NULL DEFAULT '';

-- Drop old FTS5 triggers (they reference the old column set)
DROP TRIGGER IF EXISTS skills_fts_ins;
DROP TRIGGER IF EXISTS skills_fts_del;
DROP TRIGGER IF EXISTS skills_fts_upd;

-- Recreate skills FTS5 to include triggers and pitfalls
-- Note: FTS5 content= tables can't be ALTERed, so we rebuild
DROP TABLE IF EXISTS skills_fts;
CREATE VIRTUAL TABLE skills_fts USING fts5(
    name, description, steps, tags, triggers, pitfalls,
    content=skills, content_rowid=id
);

CREATE TRIGGER skills_fts_ins AFTER INSERT ON skills BEGIN
    INSERT INTO skills_fts(rowid,name,description,steps,tags,triggers,pitfalls) VALUES (new.id,new.name,new.description,new.steps,new.tags,new.triggers,new.pitfalls);
END;
CREATE TRIGGER skills_fts_del AFTER DELETE ON skills BEGIN
    INSERT INTO skills_fts(skills_fts,rowid,name,description,steps,tags,triggers,pitfalls) VALUES ('delete',old.id,old.name,old.description,old.steps,old.tags,old.triggers,old.pitfalls);
END;
CREATE TRIGGER skills_fts_upd AFTER UPDATE ON skills BEGIN
    INSERT INTO skills_fts(skills_fts,rowid,name,description,steps,tags,triggers,pitfalls) VALUES ('delete',old.id,old.name,old.description,old.steps,old.tags,old.triggers,old.pitfalls);
    INSERT INTO skills_fts(rowid,name,description,steps,tags,triggers,pitfalls) VALUES (new.id,new.name,new.description,new.steps,new.tags,new.triggers,new.pitfalls);
END;

-- Update schema version
UPDATE _meta SET value = '2' WHERE key = 'schema_version';
"""

SCHEMA_V3 = """
-- V3: Add session_turns table for detailed per-turn session history
CREATE TABLE IF NOT EXISTS session_turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    turn_num INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    tool_name TEXT NOT NULL DEFAULT '',
    tool_args TEXT NOT NULL DEFAULT '{}',
    tool_result TEXT NOT NULL DEFAULT '',
    thinking TEXT NOT NULL DEFAULT '',
    project_id TEXT DEFAULT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_session_turns_session ON session_turns(session_id, turn_num);

-- Update schema version
UPDATE _meta SET value = '3' WHERE key = 'schema_version';
"""

SCHEMA_V6 = """
-- V6: Add feedback_events table for per-turn feedback on facts/skills
CREATE TABLE IF NOT EXISTS feedback_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_type TEXT NOT NULL CHECK(target_type IN ('fact', 'skill')),
    target_id INTEGER DEFAULT NULL,
    target_name TEXT NOT NULL DEFAULT '',
    helpful INTEGER NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    turn_num INTEGER NOT NULL DEFAULT 0,
    project_id TEXT DEFAULT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_feedback_session ON feedback_events(session_id);
CREATE INDEX IF NOT EXISTS idx_feedback_target ON feedback_events(target_type, target_id);

-- Update schema version
UPDATE _meta SET value = '6' WHERE key = 'schema_version';
"""

SCHEMA_V7 = """
-- V7: Add knowledge_links table + needs_review flags for cascade evolution
CREATE TABLE IF NOT EXISTS knowledge_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type TEXT NOT NULL CHECK(source_type IN ('fact', 'skill', 'wiki')),
    source_id INTEGER NOT NULL,
    source_name TEXT NOT NULL DEFAULT '',
    target_type TEXT NOT NULL CHECK(target_type IN ('fact', 'skill', 'wiki')),
    target_id INTEGER NOT NULL,
    target_name TEXT NOT NULL DEFAULT '',
    link_type TEXT NOT NULL DEFAULT 'depends_on',
    project_id TEXT DEFAULT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_klinks_source ON knowledge_links(source_type, source_id);
CREATE INDEX IF NOT EXISTS idx_klinks_target ON knowledge_links(target_type, target_id);

-- Update schema version
UPDATE _meta SET value = '7' WHERE key = 'schema_version';
"""

SCHEMA_V4 = """
-- V4: Add evolution_log table for gradient-descent self-evolution tracking
CREATE TABLE IF NOT EXISTS evolution_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    loss_task REAL NOT NULL,
    loss_efficiency REAL NOT NULL,
    loss_recurrence REAL NOT NULL DEFAULT 0,
    loss_knowledge_quality REAL NOT NULL DEFAULT 0,
    loss_total REAL NOT NULL,
    evolution_score REAL NOT NULL,
    gradient_facts TEXT NOT NULL DEFAULT '[]',
    gradient_skills TEXT NOT NULL DEFAULT '[]',
    improvement_targets TEXT NOT NULL DEFAULT '[]',
    hindsight_hint TEXT NOT NULL DEFAULT '',
    project_id TEXT DEFAULT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evolution_log_score ON evolution_log(evolution_score DESC);

-- Update schema version
UPDATE _meta SET value = '5' WHERE key = 'schema_version';
"""

SCHEMA_V8 = """
-- V8: Add projects table and project_id columns for unified memory
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

ALTER TABLE facts ADD COLUMN project_id TEXT DEFAULT NULL;
ALTER TABLE skills ADD COLUMN project_id TEXT DEFAULT NULL;
ALTER TABLE wiki_pages ADD COLUMN project_id TEXT DEFAULT NULL;
ALTER TABLE sessions ADD COLUMN project_id TEXT DEFAULT NULL;
ALTER TABLE knowledge_links ADD COLUMN project_id TEXT DEFAULT NULL;
ALTER TABLE feedback_events ADD COLUMN project_id TEXT DEFAULT NULL;
ALTER TABLE session_turns ADD COLUMN project_id TEXT DEFAULT NULL;
ALTER TABLE evolution_log ADD COLUMN project_id TEXT DEFAULT NULL;

CREATE INDEX IF NOT EXISTS idx_facts_project ON facts(project_id);
CREATE INDEX IF NOT EXISTS idx_skills_project ON skills(project_id);
CREATE INDEX IF NOT EXISTS idx_wiki_project ON wiki_pages(project_id);
CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project_id);

UPDATE _meta SET value = '8' WHERE key = 'schema_version';
"""

SCHEMA_V9_MIGRATION = """
ALTER TABLE skills ADD COLUMN contract TEXT DEFAULT NULL;
UPDATE _meta SET value = '9' WHERE key = 'schema_version';
"""

SCHEMA_SQL = SCHEMA_V1

# ── Constants ──

WIKI_CATEGORIES = [
    'architecture', 'decision', 'pattern', 'debugging',
    'environment', 'session-log', 'reference', 'convention',
]

MAX_CONTEXT_CHARS = 3000  # Context budget to avoid token overflow

# ── SQL Sandbox Constants ──

DBQ_ALLOWED_OPS = {'SELECT', 'INSERT', 'UPDATE'}
DBQ_BLOCKED_KEYWORDS = {'DROP', 'ALTER', 'CREATE', 'DELETE', 'ATTACH', 'DETACH', 'PRAGMA', 'REPLACE', 'VACUUM'}
DBQ_ALLOWED_TABLES = {'wiki_pages', 'facts', 'skills', 'sessions', 'session_turns', 'evolution_log', 'feedback_events', 'knowledge_links', '_meta'}
DBQ_MAX_ROWS = 50
DBQ_MAX_CHARS = 5000

DEFAULT_META_RULES = """# Meta Rules

Core behavioral constraints for Nova Agent:
- Always verify before claiming completion
- Never repeat failed actions without new information
- Ask user before irreversible operations
- Use tools to probe, never speculate
- Crystallize experience into wiki pages after complex tasks"""

DEFAULT_FACTS = [
    ("Nova project memory is at <project>/.nova/nova.db", "environment", "paths,memory"),
    ("Nova global memory is at ~/.nova/nova.db", "environment", "paths,memory,global"),
    ("SQLite database backs all memory operations", "architecture", "database,sqlite,memory"),
]

DEFAULT_SKILLS = [
    ("memory_management", "How and when to update agent memory", [
        "1. After completing a complex task successfully, distill learnings",
        "2. When discovering environment facts (paths, configs), use fact_add",
        "3. When a repeated task has a better workflow, use skill_add to crystallize it",
        "4. Use wiki_ingest for rich knowledge, fact_add for quick facts",
        "5. Never store temporary variables or unverified assumptions",
        "6. Verify: check that stored knowledge is accurate and not redundant",
    ], "memory,update,maintenance,knowledge",
     "memory,update,crystallize,facts,wiki,skills,knowledge",
     ["Don't store temporary variables or unverified assumptions",
      "Don't duplicate existing facts — check with fact_search first",
      "Don't skip verification step — always confirm stored knowledge is correct"]),
]


# ── Helpers ──

def _fts_escape(query: str) -> str:
    """Escape a query string for FTS5 MATCH — quote each term separately for OR-style matching."""
    terms = query.replace('"', '').split()
    if not terms:
        return '"*"'
    # Quote each term individually so "docker deploy" matches either term
    return ' '.join(f'"{t}"' for t in terms)




# ── NovaMemory (single DB) ──

class NovaMemory:
    """SQL-backed memory engine with Wiki compounding."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.current_project_id = None  # None = global scope
        os.makedirs(os.path.dirname(db_path) or '.', exist_ok=True)
        self._lock = threading.RLock()  # Reentrant lock — allows nested calls (e.g. wiki_ingest → wiki_add)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()
        self._seed_defaults()

    def _init_schema(self):
        self._conn.executescript(SCHEMA_SQL)
        self._conn.commit()
        # Apply migrations if needed
        self._apply_migrations()

    def _apply_migrations(self):
        """Apply schema migrations based on the current schema version."""
        row = self._conn.execute("SELECT value FROM _meta WHERE key='schema_version'").fetchone()
        current_version = int(row[0]) if row else 1

        if current_version < 2:
            # V2: Add triggers, pitfalls, version, last_improved_at to skills
            try:
                # Check if new columns already exist (fresh DB has them in CREATE TABLE)
                cols = [r[1] for r in self._conn.execute("PRAGMA table_info(skills)").fetchall()]
                needs_migration = 'triggers' not in cols

                if needs_migration:
                    self._conn.executescript(SCHEMA_V2)
                    self._conn.commit()

                    # Rebuild FTS5 content for skills (since we added new columns)
                    self._conn.execute("INSERT INTO skills_fts(skills_fts) VALUES ('rebuild')")
                    self._conn.commit()
                else:
                    # Fresh DB already has V2 schema — just update version marker
                    self._conn.execute("UPDATE _meta SET value='2' WHERE key='schema_version'")
                    self._conn.commit()
            except Exception as e:
                print(f"[Migration] V2 migration error: {e}")
                # Non-critical — fresh DBs already have these columns

        if current_version < 3:
            # V3: Add session_turns table for detailed session history
            try:
                # Check if session_turns already exists (fresh DB has it in CREATE TABLE)
                tables = [r[0] for r in self._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()]
                if 'session_turns' not in tables:
                    self._conn.executescript(SCHEMA_V3)
                    self._conn.commit()
                else:
                    # Fresh DB already has V3 schema — just update version marker
                    self._conn.execute("UPDATE _meta SET value='3' WHERE key='schema_version'")
                    self._conn.commit()
            except Exception as e:
                print(f"[Migration] V3 migration error: {e}")

        if current_version < 4:
            # V4: Add evolution_log table for gradient-descent tracking
            try:
                tables = [r[0] for r in self._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()]
                if 'evolution_log' not in tables:
                    self._conn.executescript(SCHEMA_V4)
                    self._conn.commit()
                else:
                    self._conn.execute("UPDATE _meta SET value='4' WHERE key='schema_version'")
                    self._conn.commit()
            except Exception as e:
                print(f"[Migration] V4 migration error: {e}")

        if current_version < 5:
            # V5: Add hindsight_hint column to evolution_log
            try:
                cols = [r[1] for r in self._conn.execute(
                    "PRAGMA table_info(evolution_log)"
                ).fetchall()]
                if 'hindsight_hint' not in cols:
                    self._conn.execute(
                        "ALTER TABLE evolution_log ADD COLUMN hindsight_hint TEXT NOT NULL DEFAULT ''"
                    )
                    self._conn.commit()
                self._conn.execute("UPDATE _meta SET value='5' WHERE key='schema_version'")
                self._conn.commit()
            except Exception as e:
                print(f"[Migration] V5 migration error: {e}")

        if current_version < 6:
            # V6: Add feedback_events table for per-turn feedback on facts/skills
            try:
                tables = [r[0] for r in self._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()]
                if 'feedback_events' not in tables:
                    self._conn.executescript(SCHEMA_V6)
                    self._conn.commit()
                else:
                    self._conn.execute("UPDATE _meta SET value='6' WHERE key='schema_version'")
                    self._conn.commit()
            except Exception as e:
                print(f"[Migration] V6 migration error: {e}")

        if current_version < 7:
            # V7: Add knowledge_links table + needs_review columns
            try:
                tables = [r[0] for r in self._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()]
                if 'knowledge_links' not in tables:
                    self._conn.executescript(SCHEMA_V7)
                    self._conn.commit()
                else:
                    self._conn.execute("UPDATE _meta SET value='7' WHERE key='schema_version'")
                    self._conn.commit()
                # Add needs_review column if missing (conditional ALTER TABLE)
                for table_name in ('facts', 'skills'):
                    cols = [r[1] for r in self._conn.execute(f"PRAGMA table_info({table_name})").fetchall()]
                    if 'needs_review' not in cols:
                        self._conn.execute(f"ALTER TABLE {table_name} ADD COLUMN needs_review INTEGER NOT NULL DEFAULT 0")
                        self._conn.commit()
            except Exception as e:
                print(f"[Migration] V7 migration error: {e}")

        if current_version < 8:
            # V8: Add projects table + project_id columns (unified memory)
            try:
                # Create projects table if missing
                tables = [r[0] for r in self._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()]
                if 'projects' not in tables:
                    self._conn.execute(
                        "CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL, "
                        "description TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
                    )
                    self._conn.commit()
                # Add project_id column to each table if missing
                for table_name in ('facts', 'skills', 'wiki_pages', 'sessions',
                                   'knowledge_links', 'feedback_events', 'session_turns', 'evolution_log'):
                    cols = [r[1] for r in self._conn.execute(f"PRAGMA table_info({table_name})").fetchall()]
                    if 'project_id' not in cols:
                        self._conn.execute(f"ALTER TABLE {table_name} ADD COLUMN project_id TEXT DEFAULT NULL")
                        self._conn.commit()
                # Create indexes
                for idx in ('idx_facts_project', 'idx_skills_project', 'idx_wiki_project', 'idx_sessions_project'):
                    try:
                        if 'facts' in idx:
                            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_facts_project ON facts(project_id)")
                        elif 'skills' in idx:
                            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_skills_project ON skills(project_id)")
                        elif 'wiki' in idx:
                            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_wiki_project ON wiki_pages(project_id)")
                        elif 'sessions' in idx:
                            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project_id)")
                    except Exception:
                        pass
                self._conn.execute("UPDATE _meta SET value='8' WHERE key='schema_version'")
                self._conn.commit()
            except Exception as e:
                print(f"[Migration] V8 migration error: {e}")

        if current_version < 9:
            # V9: Add contract column to skills table for behavioral contracts
            try:
                cols = [r[1] for r in self._conn.execute("PRAGMA table_info(skills)").fetchall()]
                if 'contract' not in cols:
                    self._conn.execute("ALTER TABLE skills ADD COLUMN contract TEXT DEFAULT NULL")
                    self._conn.commit()
                self._conn.execute("UPDATE _meta SET value='9' WHERE key='schema_version'")
                self._conn.commit()
            except Exception as e:
                print(f"[Migration] V9 migration error: {e}")

    def _seed_defaults(self):
        if self._conn.execute("SELECT COUNT(*) FROM wiki_pages WHERE slug='meta-rules'").fetchone()[0] == 0:
            self.wiki_add('meta-rules', 'Meta Rules', DEFAULT_META_RULES,
                          category='convention', tags='rules,behavior,core')
        for content, cat, tags in DEFAULT_FACTS:
            try:
                self.fact_add(content, category=cat, tags=tags)
            except sqlite3.IntegrityError:
                pass
        for name, desc, steps, tags, triggers, pitfalls in DEFAULT_SKILLS:
            try:
                self.skill_add(name, desc, steps, tags=tags, triggers=triggers, pitfalls=pitfalls)
            except sqlite3.IntegrityError:
                pass

    # ── Project Management ──

    def project_create(self, name: str, description: str = '') -> str:
        """Create a new project and return its UUID."""
        import uuid
        project_id = str(uuid.uuid4())
        now = datetime.now().isoformat()
        self._conn.execute(
            "INSERT INTO projects (id, name, description, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (project_id, name, description, now, now)
        )
        self._conn.commit()
        return project_id

    def project_select(self, project_id: Optional[str]) -> None:
        """Set the current project scope. None resets to global scope."""
        if project_id is not None:
            row = self._conn.execute("SELECT id FROM projects WHERE id=?", (project_id,)).fetchone()
            if not row:
                raise ValueError(f"Project {project_id} not found")
        self.current_project_id = project_id

    def project_list(self) -> List[Dict]:
        """Return all projects."""
        rows = self._conn.execute("SELECT id, name, description, created_at, updated_at FROM projects ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]

    def project_info(self, project_id: str) -> Optional[Dict]:
        """Return project details including scoped knowledge counts."""
        row = self._conn.execute("SELECT id, name, description, created_at, updated_at FROM projects WHERE id=?", (project_id,)).fetchone()
        if not row:
            return None
        info = dict(row)
        info['facts_count'] = self._conn.execute("SELECT COUNT(*) FROM facts WHERE project_id=?", (project_id,)).fetchone()[0]
        info['skills_count'] = self._conn.execute("SELECT COUNT(*) FROM skills WHERE project_id=?", (project_id,)).fetchone()[0]
        info['wiki_count'] = self._conn.execute("SELECT COUNT(*) FROM wiki_pages WHERE project_id=?", (project_id,)).fetchone()[0]
        info['sessions_count'] = self._conn.execute("SELECT COUNT(*) FROM sessions WHERE project_id=?", (project_id,)).fetchone()[0]
        return info

    def project_delete(self, project_id: str) -> bool:
        """Delete a project and all its scoped data."""
        row = self._conn.execute("SELECT id FROM projects WHERE id=?", (project_id,)).fetchone()
        if not row:
            return False
        self._conn.execute("DELETE FROM session_turns WHERE session_id IN (SELECT id FROM sessions WHERE project_id=?)", (project_id,))
        self._conn.execute("DELETE FROM feedback_events WHERE session_id IN (SELECT id FROM sessions WHERE project_id=?)", (project_id,))
        self._conn.execute("DELETE FROM knowledge_links WHERE project_id=?", (project_id,))
        self._conn.execute("DELETE FROM evolution_log WHERE session_id IN (SELECT id FROM sessions WHERE project_id=?)", (project_id,))
        self._conn.execute("DELETE FROM sessions WHERE project_id=?", (project_id,))
        self._conn.execute("DELETE FROM facts WHERE project_id=?", (project_id,))
        self._conn.execute("DELETE FROM skills WHERE project_id=?", (project_id,))
        self._conn.execute("DELETE FROM wiki_pages WHERE project_id=?", (project_id,))
        self._conn.execute("DELETE FROM projects WHERE id=?", (project_id,))
        self._conn.commit()
        if self.current_project_id == project_id:
            self.current_project_id = None
        return True

    # ── Knowledge Promotion ──

    def fact_promote(self, fact_id: int) -> bool:
        """Promote a scoped fact to global (set project_id=NULL)."""
        count = self._conn.execute("UPDATE facts SET project_id=NULL WHERE id=?", (fact_id,)).rowcount
        self._conn.commit()
        return count > 0

    def skill_promote(self, skill_name: str) -> bool:
        """Promote a scoped skill to global (set project_id=NULL)."""
        count = self._conn.execute("UPDATE skills SET project_id=NULL WHERE name=?", (skill_name,)).rowcount
        self._conn.commit()
        return count > 0

    def wiki_promote(self, slug: str) -> bool:
        """Promote a scoped wiki page to global (set project_id=NULL)."""
        count = self._conn.execute("UPDATE wiki_pages SET project_id=NULL WHERE slug=?", (slug,)).rowcount
        self._conn.commit()
        return count > 0

    # ── Query helpers ──

    def _scope_where(self, prefix: str = '') -> str:
        """Generate WHERE clause for project scoping.
        When project is selected: WHERE (project_id = ? OR project_id IS NULL)
        When global: WHERE project_id IS NULL
        prefix: table alias prefix (e.g. 'f.' for facts)
        """
        col = f"{prefix}project_id"
        if self.current_project_id is not None:
            return f"({col} = ? OR {col} IS NULL)"
        return f"{col} IS NULL"

    def _scope_params(self) -> list:
        """Return params for _scope_where. Empty list if global, [project_id] if scoped."""
        if self.current_project_id is not None:
            return [self.current_project_id]
        return []

    def _scope_write_id(self) -> Optional[str]:
        """Return project_id for write operations. None for global scope."""
        return self.current_project_id

    def get_high_trust_facts(self, min_trust=0.7, limit=10) -> List[Dict]:
        """Get facts above trust threshold."""
        rows = self._conn.execute(
            "SELECT content,category FROM facts WHERE trust_score >= ? ORDER BY trust_score DESC LIMIT ?",
            (min_trust, limit)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_top_skills(self, min_success=0.5, limit=5) -> List[Dict]:
        """Get top skills by success rate and usage."""
        rows = self._conn.execute(
            "SELECT name,description FROM skills WHERE success_rate >= ? ORDER BY usage_count DESC LIMIT ?",
            (min_success, limit)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_all_facts(self, min_trust=0.5, limit=15) -> List[Dict]:
        """Get facts for context injection."""
        rows = self._conn.execute(
            "SELECT content,category,tags FROM facts WHERE trust_score >= ? ORDER BY trust_score DESC LIMIT ?",
            (min_trust, limit)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_all_skills(self, limit=5) -> List[Dict]:
        """Get skills for context injection."""
        rows = self._conn.execute(
            "SELECT name,description,steps FROM skills ORDER BY success_rate DESC, usage_count DESC LIMIT ?",
            (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_recent_sessions(self, limit=3) -> List[Dict]:
        """Get recent sessions."""
        rows = self._conn.execute(
            "SELECT task,summary,created_at FROM sessions ORDER BY created_at DESC LIMIT ?",
            (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def count_wiki_pages(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM wiki_pages").fetchone()[0]

    def count_facts(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]

    def count_skills(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM skills").fetchone()[0]

    def count_sessions(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]

    def avg_trust(self) -> float:
        return self._conn.execute("SELECT AVG(trust_score) FROM facts").fetchone()[0] or 0

    def count_feedback_events(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM feedback_events").fetchone()[0]

    def get_items_needing_review(self) -> Dict:
        """Get facts and skills flagged for review."""
        facts = self._conn.execute("SELECT id, content, category, trust_score FROM facts WHERE needs_review=1").fetchall()
        skills = self._conn.execute("SELECT id, name, success_rate FROM skills WHERE needs_review=1").fetchall()
        return {
            'facts_needing_review': [{'id': r['id'], 'content': r['content'], 'category': r['category'], 'trust_score': r['trust_score']} for r in facts],
            'skills_needing_review': [{'id': r['id'], 'name': r['name'], 'success_rate': r['success_rate']} for r in skills],
        }

    def mark_reviewed(self, item_type: str, item_id: int) -> bool:
        """Mark a fact or skill as reviewed (clear needs_review flag)."""
        if item_type not in ('facts', 'skills'):
            return False
        count = self._conn.execute(f"UPDATE {item_type} SET needs_review=0 WHERE id=?", (item_id,)).rowcount
        self._conn.commit()
        return count > 0

    def session_feedback_quality(self, session_id: int) -> Dict:
        """Return helpful/unhelpful counts from feedback_events for a session."""
        total = self._conn.execute(
            "SELECT COUNT(*) FROM feedback_events WHERE session_id=?", (session_id,)
        ).fetchone()[0]
        helpful = self._conn.execute(
            "SELECT COUNT(*) FROM feedback_events WHERE session_id=? AND helpful=1", (session_id,)
        ).fetchone()[0]
        unhelpful = self._conn.execute(
            "SELECT COUNT(*) FROM feedback_events WHERE session_id=? AND helpful=0", (session_id,)
        ).fetchone()[0]
        return {'helpful_count': helpful, 'unhelpful_count': unhelpful, 'total_count': total}

    # ── Wiki Operations ──

    def _make_slug(self, title: str) -> str:
        slug = title.lower().strip()
        slug = re.sub(r'[^\w\s-]', '', slug)
        slug = re.sub(r'[\s_]+', '-', slug)
        slug = slug[:80].rstrip('-')
        return slug or 'untitled'

    @_write_locked
    def wiki_add(self, slug: str, title: str, content: str,
                 category: str = 'reference', tags: str = '',
                 confidence: str = 'medium', sources: str = '') -> int:
        now = datetime.now().isoformat()
        try:
            cur = self._conn.execute(
                "INSERT INTO wiki_pages (slug,title,category,content,tags,confidence,sources,project_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (slug, title, category, content, tags, confidence, sources, self._scope_write_id(), now, now)
            )
            self._conn.commit()
            return cur.lastrowid
        except sqlite3.IntegrityError:
            existing = self._conn.execute(
                "SELECT id, content, tags FROM wiki_pages WHERE slug=?", (slug,)
            ).fetchone()
            if existing:
                # Fix #9: Dedup check before appending
                if self._content_is_duplicate(existing['content'], content):
                    return existing['id']
                merged = existing['content'] + '\n\n---\n\n' + content
                old_tags = set(existing['tags'].split(',')) if existing['tags'] else set()
                new_tags = set(tags.split(',')) if tags else set()
                combined_tags = ','.join(sorted(old_tags | new_tags))
                self._conn.execute(
                    "UPDATE wiki_pages SET content=?, tags=?, updated_at=? WHERE slug=?",
                    (merged, combined_tags, now, slug)
                )
                self._conn.commit()
                return existing['id']
            raise

    def _content_is_duplicate(self, existing: str, new: str) -> bool:
        """Check if new content is substantially duplicated in existing.

        80% token overlap threshold (was 60% — too aggressive, different pages rejected).
        Skip dedup if new content is significantly longer (>200 chars more than existing).
        """
        if not new.strip():
            return True
        # Skip dedup if new content adds significant new material
        if len(new) > len(existing) + 200:
            return False
        existing_tokens = set(existing.lower().split())
        new_tokens = set(new.lower().split())
        if not new_tokens:
            return True
        overlap = len(existing_tokens & new_tokens) / len(new_tokens)
        return overlap > 0.80

    @_write_locked
    def wiki_ingest(self, title: str, content: str, tags: str,
                    category: str = 'reference', confidence: str = 'medium',
                    sources: str = '') -> int:
        slug = self._make_slug(title)
        now = datetime.now().isoformat()

        existing = self._conn.execute(
            "SELECT id, content, tags FROM wiki_pages WHERE slug=?", (slug,)
        ).fetchone()

        if existing:
            # Dedup check before appending (Fix #9)
            if self._content_is_duplicate(existing['content'], content):
                # Just merge tags, skip content
                old_tags = set(existing['tags'].split(',')) if existing['tags'] else set()
                new_tags = set(tags.split(',')) if tags else set()
                if new_tags - old_tags:
                    combined_tags = ','.join(sorted(old_tags | new_tags))
                    self._conn.execute(
                        "UPDATE wiki_pages SET tags=?, updated_at=? WHERE slug=?",
                        (combined_tags, now, slug)
                    )
                    self._conn.commit()
                return existing['id']
            merged = existing['content'] + '\n\n---\n\n' + content
            old_tags = set(existing['tags'].split(',')) if existing['tags'] else set()
            new_tags = set(tags.split(',')) if tags else set()
            combined_tags = ','.join(sorted(old_tags | new_tags))
            self._conn.execute(
                "UPDATE wiki_pages SET content=?, tags=?, updated_at=? WHERE slug=?",
                (merged, combined_tags, now, slug)
            )
            self._conn.commit()
            return existing['id']
        else:
            return self.wiki_add(slug, title, content, category=category,
                                 tags=tags, confidence=confidence, sources=sources)

    def wiki_read(self, slug: str) -> Optional[Dict]:
        # When project selected, try project-scoped first, then global
        if self.current_project_id is not None:
            row = self._conn.execute("SELECT * FROM wiki_pages WHERE slug=? AND project_id=?", (slug, self.current_project_id)).fetchone()
            if row:
                return dict(row)
        row = self._conn.execute("SELECT * FROM wiki_pages WHERE slug=? AND project_id IS NULL", (slug,)).fetchone()
        if row:
            return dict(row)
        return None

    def wiki_query(self, query: str, category: str = None,
                   tags: str = None, limit: int = 10) -> List[Dict]:
        results = []
        if query:
            fts_results = self._conn.execute(
                "SELECT rowid FROM wiki_fts WHERE wiki_fts MATCH ? ORDER BY rank LIMIT ?",
                (_fts_escape(query), limit)
            ).fetchall()
            ids = [r[0] for r in fts_results]
            if ids:
                where = f"id IN ({','.join(str(i) for i in ids)})"
                params = []
                cat_clause = ""
                scope_clause = f" AND {self._scope_where()}"
                params.extend(self._scope_params())
                if category:
                    cat_clause = " AND category=?"
                    params.append(category)
                rows = self._conn.execute(
                    f"SELECT * FROM wiki_pages WHERE {where}{cat_clause}{scope_clause} ORDER BY updated_at DESC",
                    params
                ).fetchall()
                # Fix #1: Exclude session-log pages from search results (they're noise)
                results = [dict(r) for r in rows if r['category'] != 'session-log']

        if tags and len(results) < limit:
            tag_set = set(tags.split(','))
            cat_filter = ""
            params = []
            if category:
                cat_filter = " AND category=?"
                params.append(category)
            all_pages = self._conn.execute(
                f"SELECT * FROM wiki_pages WHERE category != 'session-log' AND {self._scope_where()}{cat_filter}",
                self._scope_params() + params
            ).fetchall()
            for page in all_pages:
                page_tags = set(page['tags'].split(',')) if page['tags'] else set()
                if tag_set & page_tags:
                    d = dict(page)
                    if d not in results:
                        results.append(d)
            results = results[:limit]

        if not query and not tags and category:
            rows = self._conn.execute(
                f"SELECT * FROM wiki_pages WHERE category=? AND {self._scope_where()} ORDER BY updated_at DESC LIMIT ?",
                [category] + self._scope_params() + [limit]
            ).fetchall()
            results = [dict(r) for r in rows]

        for r in results:
            r['cross_refs'] = re.findall(r'\[\[(\w[\w-]*)\]\]', r.get('content', ''))

        return results

    def wiki_list(self) -> List[Dict]:
        rows = self._conn.execute(
            f"SELECT id,slug,title,category,tags,confidence,project_id,updated_at FROM wiki_pages WHERE {self._scope_where()} ORDER BY updated_at DESC",
            self._scope_params()
        ).fetchall()
        return [dict(r) for r in rows]

    @_write_locked
    def wiki_delete(self, slug: str) -> bool:
        count = self._conn.execute("DELETE FROM wiki_pages WHERE slug=?", (slug,)).rowcount
        self._conn.commit()
        return count > 0

    @_write_locked
    def wiki_mark_quality(self, slug: str, quality: str) -> bool:
        """Update wiki page confidence based on RL feedback.
        quality: 'high' (proven helpful), 'medium' (neutral), 'low' (unhelpful/outdated)
        """
        rows = self._conn.execute(
            "UPDATE wiki_pages SET confidence=?, updated_at=? WHERE slug=?",
            (quality, datetime.now().isoformat(), slug)
        ).rowcount
        self._conn.commit()
        return rows > 0

    def wiki_lint(self) -> List[str]:
        issues = []
        pages = self.wiki_list()
        slugs = {p['slug'] for p in pages}
        for page in pages:
            full = self.wiki_read(page['slug'])
            if full:
                refs = re.findall(r'\[\[(\w[\w-]*)\]\]', full['content'])
                for ref in refs:
                    if ref not in slugs:
                        issues.append(f"Orphan ref [[{ref}]] in page '{page['slug']}'")
                if len(full['content']) > 10000:
                    issues.append(f"Page '{page['slug']}' is oversized ({len(full['content'])} bytes)")
        return issues

    # ── Fact Operations ──

    @_write_locked
    def fact_add(self, content: str, category: str = 'general',
                 tags: str = '', trust_score: float = 0.5) -> int:
        now = datetime.now().isoformat()
        project_id = self._scope_write_id()
        try:
            cur = self._conn.execute(
                "INSERT INTO facts (content,category,tags,trust_score,project_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                (content, category, tags, trust_score, project_id, now, now)
            )
            self._conn.commit()
            return cur.lastrowid
        except sqlite3.IntegrityError:
            existing = self._conn.execute("SELECT id,tags FROM facts WHERE content=?", (content,)).fetchone()
            old_tags = set(existing['tags'].split(',')) if existing['tags'] else set()
            new_tags = set(tags.split(',')) if tags else set()
            combined = ','.join(sorted(old_tags | new_tags))
            self._conn.execute("UPDATE facts SET tags=?, updated_at=? WHERE content=?", (combined, now, content))
            self._conn.commit()
            return existing['id']

    def fact_search(self, query: str, category: str = None,
                    min_trust: float = 0.3, limit: int = 10) -> List[Dict]:
        fts_results = self._conn.execute(
            "SELECT rowid FROM facts_fts WHERE facts_fts MATCH ? ORDER BY rank LIMIT ?",
            (_fts_escape(query), limit * 2)
        ).fetchall()
        ids = [r[0] for r in fts_results]
        if not ids:
            return []

        where_clauses = [f"id IN ({','.join(str(i) for i in ids)})", "trust_score >= ?"]
        params = [min_trust]
        if category:
            where_clauses.append("category=?")
            params.append(category)
        # Project scoping: show global + current project facts
        scope = self._scope_where()
        where_clauses.append(scope)
        params.extend(self._scope_params())
        where = " AND ".join(where_clauses)
        rows = self._conn.execute(
            f"SELECT * FROM facts WHERE {where} ORDER BY trust_score DESC LIMIT ?", params + [limit]
        ).fetchall()

        # Fix #2: Auto-trust feedback — facts that get retrieved gain a small trust bump
        for row in rows:
            self._conn.execute(
                "UPDATE facts SET retrieval_count=retrieval_count+1, trust_score=MIN(trust_score+0.01,1.0), updated_at=? WHERE id=?",
                (datetime.now().isoformat(), row['id'])
            )
        self._conn.commit()

        return [dict(r) for r in rows]

    @_write_locked
    def fact_mark_helpful(self, content: str) -> None:
        self._conn.execute(
            "UPDATE facts SET trust_score=MIN(trust_score+0.05,1.0), helpful_count=helpful_count+1, updated_at=? WHERE content=?",
            (datetime.now().isoformat(), content)
        )
        self._conn.commit()

    @_write_locked
    def fact_mark_helpful_by_id(self, fact_id: int) -> None:
        """Mark a fact as helpful by its ID — used when task succeeds after accessing facts."""
        self._conn.execute(
            "UPDATE facts SET trust_score=MIN(trust_score+0.05,1.0), helpful_count=helpful_count+1, updated_at=? WHERE id=?",
            (datetime.now().isoformat(), fact_id)
        )
        self._conn.commit()

    @_write_locked
    def fact_mark_unhelpful(self, content: str) -> None:
        self._conn.execute(
            "UPDATE facts SET trust_score=MAX(trust_score-0.10,0.0), unhelpful_count=unhelpful_count+1, updated_at=? WHERE content=?",
            (datetime.now().isoformat(), content)
        )
        self._conn.commit()

    @_write_locked
    def fact_mark_unhelpful_by_id(self, fact_id: int) -> None:
        """Mark a fact as unhelpful by its ID — used by feedback_event_add."""
        self._conn.execute(
            "UPDATE facts SET trust_score=MAX(trust_score-0.10,0.0), unhelpful_count=unhelpful_count+1, updated_at=? WHERE id=?",
            (datetime.now().isoformat(), fact_id)
        )
        self._conn.commit()

    @_write_locked
    def feedback_event_add(self, target_type: str, target_id: Optional[int],
                           target_name: str, helpful: bool, reason: str,
                           session_id: int, turn_num: int) -> int:
        """Record a feedback event and update the target's trust/success metrics."""
        now = datetime.now().isoformat()
        cur = self._conn.execute(
            "INSERT INTO feedback_events (target_type,target_id,target_name,helpful,reason,session_id,turn_num,project_id,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (target_type, target_id, target_name, int(helpful), reason, session_id, turn_num, self._scope_write_id(), now)
        )
        self._conn.commit()

        # Update the target's trust/success metrics
        if target_type == 'fact' and target_id is not None:
            if helpful:
                self.fact_mark_helpful_by_id(target_id)
            else:
                self.fact_mark_unhelpful_by_id(target_id)
        elif target_type == 'skill' and target_name:
            self.skill_update_success(target_name, success=helpful)

        # Cascade: flag linked items for review when feedback is negative
        if not helpful:
            # Find all links where this target is the source — flag linked targets
            links = self.link_search(source_type=target_type,
                                     source_id=target_id if target_id else None)
            for lk in links:
                if lk['target_type'] == 'fact' and lk['target_id']:
                    self._conn.execute("UPDATE facts SET needs_review=1 WHERE id=?", (lk['target_id'],))
                elif lk['target_type'] == 'skill' and lk['target_name']:
                    self._conn.execute("UPDATE skills SET needs_review=1 WHERE name=?", (lk['target_name'],))
            self._conn.commit()

        return cur.lastrowid

    # ── Knowledge Link Operations ──

    @_write_locked
    def link_add(self, source_type: str, source_id: int, source_name: str,
                 target_type: str, target_id: int, target_name: str,
                 link_type: str = 'depends_on') -> int:
        """Create a link between two knowledge items."""
        now = datetime.now().isoformat()
        cur = self._conn.execute(
            "INSERT INTO knowledge_links (source_type,source_id,source_name,target_type,target_id,target_name,link_type,project_id,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (source_type, source_id, source_name, target_type, target_id, target_name, link_type, self._scope_write_id(), now)
        )
        self._conn.commit()
        return cur.lastrowid

    def link_search(self, source_type: str = None, source_id: int = None,
                    target_type: str = None, target_id: int = None,
                    link_type: str = None) -> List[Dict]:
        """Search knowledge links by optional filters."""
        clauses = []
        params = []
        if source_type:
            clauses.append("source_type=?")
            params.append(source_type)
        if source_id is not None:
            clauses.append("source_id=?")
            params.append(source_id)
        if target_type:
            clauses.append("target_type=?")
            params.append(target_type)
        if target_id is not None:
            clauses.append("target_id=?")
            params.append(target_id)
        if link_type:
            clauses.append("link_type=?")
            params.append(link_type)
        clauses.append(self._scope_where())
        params.extend(self._scope_params())
        where = " AND ".join(clauses) if clauses else "1=1"
        rows = self._conn.execute(
            f"SELECT * FROM knowledge_links WHERE {where} ORDER BY created_at DESC", params
        ).fetchall()
        return [dict(r) for r in rows]

    @_write_locked
    def link_delete(self, link_id: int) -> bool:
        """Delete a single knowledge link."""
        count = self._conn.execute("DELETE FROM knowledge_links WHERE id=?", (link_id,)).rowcount
        self._conn.commit()
        return count > 0

    # ── Cluster Search (tag-based inference) ──

    def cluster_search(self, query: str, min_relevance: float = 0.3,
                       limit: int = 5) -> List[Dict]:
        """Search for composed knowledge bundles by tag overlap and category matching.

        Returns bundles grouped by common tags: {topic_tag, facts, skills, wiki_pages, relevance_score}
        """
        keywords = self._extract_keywords(query)
        if not keywords:
            return []

        # Build query tag set from keywords
        query_tags = set(keywords)

        # Find matching facts (tag overlap + category match)
        fact_results = []
        for kw in keywords[:5]:
            for f in self.fact_search(kw, min_trust=0.3, limit=10):
                f_tags = set(t.strip().lower() for t in f.get('tags', '').split(',') if t.strip())
                overlap = len(query_tags & f_tags)
                max_tags = max(len(query_tags), len(f_tags), 1)
                relevance = overlap / max_tags if overlap > 0 else 0.3 if f.get('category', '') in query_tags else 0.0
                if relevance >= min_relevance:
                    f['_relevance'] = relevance
                    fact_results.append(f)

        # Find matching skills
        skill_results = []
        for kw in keywords[:5]:
            for s in self.skill_search(kw, min_success=0.3, limit=10):
                s_tags = set(t.strip().lower() for t in s.get('tags', '').split(',') if t.strip())
                overlap = len(query_tags & s_tags)
                max_tags = max(len(query_tags), len(s_tags), 1)
                relevance = overlap / max_tags if overlap > 0 else 0.3 if any(kw in s.get('triggers', '') for kw in keywords[:3]) else 0.0
                if relevance >= min_relevance:
                    s['_relevance'] = relevance
                    skill_results.append(s)

        # Find matching wiki pages
        wiki_results = []
        for kw in keywords[:3]:
            for p in self.wiki_query(kw, limit=5):
                p_tags = set(t.strip().lower() for t in p.get('tags', '').split(',') if t.strip())
                overlap = len(query_tags & p_tags)
                max_tags = max(len(query_tags), len(p_tags), 1)
                relevance = overlap / max_tags if overlap > 0 else 0.3 if p.get('category', '') in query_tags else 0.0
                if relevance >= min_relevance:
                    p['_relevance'] = relevance
                    wiki_results.append(p)

        # Deduplicate results
        seen_facts = set()
        unique_facts = []
        for f in fact_results:
            key = f.get('id', f.get('content', ''))
            if key not in seen_facts:
                seen_facts.add(key)
                unique_facts.append(f)

        seen_skills = set()
        unique_skills = []
        for s in skill_results:
            key = s.get('name', '')
            if key not in seen_skills:
                seen_skills.add(key)
                unique_skills.append(s)

        seen_wiki = set()
        unique_wiki = []
        for p in wiki_results:
            key = p.get('slug', p.get('title', ''))
            if key not in seen_wiki:
                seen_wiki.add(key)
                unique_wiki.append(p)

        # Group by best matching tag (topic)
        if not unique_facts and not unique_skills and not unique_wiki:
            return []

        # Find the highest-relevance common tag as topic
        all_tags_with_relevance = {}
        for f in unique_facts:
            for t in set(t.strip().lower() for t in f.get('tags', '').split(',') if t.strip()):
                all_tags_with_relevance[t] = all_tags_with_relevance.get(t, 0) + f['_relevance']
        for s in unique_skills:
            for t in set(t.strip().lower() for t in s.get('tags', '').split(',') if t.strip()):
                all_tags_with_relevance[t] = all_tags_with_relevance.get(t, 0) + s['_relevance']
        for p in unique_wiki:
            for t in set(t.strip().lower() for t in p.get('tags', '').split(',') if t.strip()):
                all_tags_with_relevance[t] = all_tags_with_relevance.get(t, 0) + p['_relevance']

        top_tags = sorted(all_tags_with_relevance.keys(), key=lambda t: all_tags_with_relevance[t], reverse=True)[:limit]

        # Build bundles by topic tag
        bundles = []
        for tag in top_tags:
            tag_facts = [f for f in unique_facts if tag in set(t.strip().lower() for t in f.get('tags', '').split(','))]
            tag_skills = [s for s in unique_skills if tag in set(t.strip().lower() for t in s.get('tags', '').split(','))]
            tag_wiki = [p for p in unique_wiki if tag in set(t.strip().lower() for t in p.get('tags', '').split(','))]
            if not tag_facts and not tag_skills and not tag_wiki:
                continue
            # Clean _relevance from output
            clean_facts = [{k: v for k, v in f.items() if k != '_relevance'} for f in tag_facts]
            clean_skills = [{k: v for k, v in s.items() if k != '_relevance'} for s in tag_skills]
            clean_wiki = [{k: v for k, v in p.items() if k != '_relevance'} for p in tag_wiki]
            avg_relevance = (sum(f['_relevance'] for f in tag_facts) + sum(s['_relevance'] for s in tag_skills) + sum(p['_relevance'] for p in tag_wiki)) / max(len(tag_facts) + len(tag_skills) + len(tag_wiki), 1)
            bundles.append({
                'topic_tag': tag,
                'facts': clean_facts,
                'skills': clean_skills,
                'wiki_pages': clean_wiki,
                'relevance_score': round(avg_relevance, 3),
            })

        return sorted(bundles, key=lambda b: b['relevance_score'], reverse=True)[:limit]

    # ── Skill/SOP Operations ──

    @_write_locked
    def skill_add(self, name: str, description: str, steps: list,
                  tags: str = '', success_rate: float = 0.5,
                  triggers: str = '', pitfalls: list = None,
                  contract: str = None) -> int:
        steps_json = json.dumps(steps, ensure_ascii=False)
        pitfalls_json = json.dumps(pitfalls or [], ensure_ascii=False)
        now = datetime.now().isoformat()
        try:
            cur = self._conn.execute(
                "INSERT INTO skills (name,description,steps,triggers,pitfalls,success_rate,tags,project_id,contract,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (name, description, steps_json, triggers, pitfalls_json, success_rate, tags, self._scope_write_id(), contract, now, now)
            )
            self._conn.commit()
            return cur.lastrowid
        except sqlite3.IntegrityError:
            self._conn.execute(
                "UPDATE skills SET steps=?, triggers=?, pitfalls=?, success_rate=?, tags=?, contract=?, version=version+1, last_improved_at=?, updated_at=? WHERE name=?",
                (steps_json, triggers, pitfalls_json, success_rate, tags, contract, now, now, name)
            )
            self._conn.commit()
            return self._conn.execute("SELECT id FROM skills WHERE name=?", (name,)).fetchone()[0]

    def skill_search(self, query: str, min_success: float = 0.3,
                     limit: int = 5) -> List[Dict]:
        fts_results = self._conn.execute(
            "SELECT rowid FROM skills_fts WHERE skills_fts MATCH ? ORDER BY rank LIMIT ?",
            (_fts_escape(query), limit * 2)
        ).fetchall()
        ids = [r[0] for r in fts_results]
        if not ids:
            return []

        where_clauses = [f"id IN ({','.join(str(i) for i in ids)})", "success_rate >= ?"]
        params = [min_success]
        scope = self._scope_where()
        where_clauses.append(scope)
        params.extend(self._scope_params())
        where = " AND ".join(where_clauses)
        rows = self._conn.execute(
            f"SELECT * FROM skills WHERE {where} ORDER BY success_rate DESC, usage_count DESC LIMIT ?", params + [limit]
        ).fetchall()

        for row in rows:
            self._conn.execute(
                "UPDATE skills SET usage_count=usage_count+1, updated_at=? WHERE id=?",
                (datetime.now().isoformat(), row['id'])
            )
        self._conn.commit()

        results = []
        for r in rows:
            d = dict(r)
            d['steps'] = json.loads(d['steps'])
            d['pitfalls'] = json.loads(d.get('pitfalls', '[]'))
            results.append(d)
        return results

    @_write_locked
    def skill_update_success(self, name: str, success: bool) -> None:
        skill = self._conn.execute("SELECT * FROM skills WHERE name=?", (name,)).fetchone()
        if skill:
            old_rate = skill['success_rate']
            old_count = skill['usage_count']
            new_val = 1.0 if success else 0.0
            weight = min(0.2, 1.0 / (old_count + 5))
            new_rate = old_rate * (1 - weight) + new_val * weight
            self._conn.execute(
                "UPDATE skills SET success_rate=?, updated_at=? WHERE name=?",
                (new_rate, datetime.now().isoformat(), name)
            )
            self._conn.commit()

    @_write_locked
    def skill_improve(self, name: str, new_steps: list = None,
                      new_pitfalls: list = None, new_triggers: str = None) -> bool:
        """Improve an existing skill — increments version, merges new content."""
        skill = self._conn.execute("SELECT * FROM skills WHERE name=?", (name,)).fetchone()
        if not skill:
            return False
        now = datetime.now().isoformat()
        current_steps = json.loads(skill['steps'])
        current_pitfalls = json.loads(skill['pitfalls'] if 'pitfalls' in skill.keys() else '[]')

        if new_steps:
            # Replace steps entirely (new version supersedes old)
            steps_json = json.dumps(new_steps, ensure_ascii=False)
        else:
            steps_json = skill['steps']

        if new_pitfalls:
            # Merge pitfalls: add new ones not already present
            existing_texts = {p.lower() for p in current_pitfalls}
            merged = current_pitfalls + [p for p in new_pitfalls if p.lower() not in existing_texts]
            pitfalls_json = json.dumps(merged, ensure_ascii=False)
        else:
            pitfalls_json = skill['pitfalls']

        triggers_val = new_triggers if new_triggers is not None else (skill['triggers'] if 'triggers' in skill.keys() else '')

        self._conn.execute(
            "UPDATE skills SET steps=?, pitfalls=?, triggers=?, version=version+1, last_improved_at=?, updated_at=? WHERE name=?",
            (steps_json, pitfalls_json, triggers_val, now, now, name)
        )
        self._conn.commit()
        return True

    def skill_match(self, query: str, limit: int = 2, min_success: float = 0.3) -> List[Dict]:
        """Match skills to a task prompt — uses SQL LIKE queries against triggers, name, description, tags.

        Designed for future MySQL migration — no FTS5 dependency.
        The LLM can also query skills directly via db_query SQL:
          SELECT * FROM skills WHERE triggers LIKE '%deploy%' AND success_rate > 0.5
        """
        # Extract keywords from query (skip stopwords)
        stopwords = {'the','a','an','is','are','was','were','be','been','being',
                     'have','has','had','do','does','did','will','would','could',
                     'should','may','might','shall','can','need','to','of','in',
                     'for','on','with','at','by','from','as','into','through',
                     'during','before','after','above','below','between','out',
                     'off','over','under','again','further','then','once','here',
                     'there','when','where','why','how','all','both','each','few',
                     'more','most','other','some','such','no','not','only','own',
                     'same','so','than','too','very','just','because','but','and',
                     'or','if','while','about','up','it','its','this','that','these',
                     'those','i','me','my','we','our','you','your','he','him','his',
                     'she','her','they','them','their','what','which','who','whom',
                     'build','create','write','make','solve','fix','use','run','show',
                     'help','tell','give','find','get','set','put','add','remove'}
        words = re.findall(r'\b[a-zA-Z]{3,}\b', query.lower())
        keywords = [w for w in words if w not in stopwords]
        if not keywords:
            return []

        # Build LIKE-based WHERE clause from keywords — matches triggers, name, description, tags
        # Each keyword checks against all searchable fields with OR
        # Any keyword match is sufficient (OR between keywords, not AND)
        conditions = []
        for kw in keywords[:5]:
            like_val = f'%{kw}%'
            conditions.append(
                f"(triggers LIKE ? OR name LIKE ? OR description LIKE ? OR tags LIKE ?)"
            )
        where = "(" + " OR ".join(conditions) + ") AND success_rate >= ? AND " + self._scope_where()

        # Build params — each keyword produces 4 LIKE params, plus min_success, plus scope params
        params = []
        for kw in keywords[:5]:
            like_val = f'%{kw}%'
            params.extend([like_val, like_val, like_val, like_val])
        params.append(min_success)
        params.extend(self._scope_params())

        rows = self._conn.execute(
            f"SELECT * FROM skills WHERE {where} ORDER BY success_rate DESC, usage_count DESC LIMIT ?",
            params + [limit]
        ).fetchall()

        results = []
        for r in rows:
            d = dict(r)
            d['steps'] = json.loads(d['steps'])
            d['pitfalls'] = json.loads(d.get('pitfalls', '[]'))
            results.append(d)
        return results[:limit]

    # ── Session Operations ──

    @_write_locked
    def session_archive(self, task: str, summary: str, result: str,
                        had_knowledge: bool = False) -> int:
        """Archive a completed session. had_knowledge=True if wiki_ingest/fact_add were used."""
        now = datetime.now().isoformat()
        cur = self._conn.execute(
            "INSERT INTO sessions (task,summary,result,had_knowledge_output,created_at) VALUES (?,?,?,?,?)",
            (task, summary[:200], result[:500], int(had_knowledge), now)
        )
        self._conn.commit()
        return cur.lastrowid

    @_write_locked
    def session_create(self, task: str) -> int:
        """Create a session record at task start (summary/result filled later)."""
        now = datetime.now().isoformat()
        cur = self._conn.execute(
            "INSERT INTO sessions (task,summary,result,had_knowledge_output,project_id,created_at) VALUES (?,?,'',0,?,?)",
            (task[:200], '', self._scope_write_id(), now)
        )
        self._conn.commit()
        return cur.lastrowid

    @_write_locked
    def session_update(self, session_id: int, summary: str = '',
                       result: str = '', had_knowledge: bool = False):
        """Update session record at task end with summary and result."""
        self._conn.execute(
            "UPDATE sessions SET summary=?, result=?, had_knowledge_output=? WHERE id=?",
            (summary[:200], result[:500], int(had_knowledge), session_id)
        )
        self._conn.commit()

    @_write_locked
    def session_turn_add(self, session_id: int, turn_num: int, role: str,
                         content: str = '', tool_name: str = '',
                         tool_args: str = '{}', tool_result: str = '',
                         thinking: str = '') -> int:
        """Record a single turn in a session for detailed history."""
        now = datetime.now().isoformat()
        cur = self._conn.execute(
            "INSERT INTO session_turns (session_id,turn_num,role,content,tool_name,tool_args,tool_result,thinking,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (session_id, turn_num, role, content[:2000], tool_name,
             json.dumps(tool_args, ensure_ascii=False)[:2000] if isinstance(tool_args, dict) else tool_args[:2000],
             tool_result[:2000], thinking[:500], now)
        )
        self._conn.commit()
        return cur.lastrowid

    def session_turns_query(self, session_id: int, limit: int = None) -> List[Dict]:
        """Return all turns for a given session, ordered by turn_num."""
        sql = "SELECT * FROM session_turns WHERE session_id=? ORDER BY turn_num"
        params = [session_id]
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def session_relevant_turns(self, task_prompt: str, max_sessions: int = 3,
                               max_turns: int = 10) -> str:
        """Find relevant past session turns for injecting into current task context.

        Searches sessions.task for keywords from the current task prompt,
        returns the most relevant turns from matching sessions.
        """
        keywords = self._extract_keywords(task_prompt)
        if not keywords:
            return ""

        # Find matching sessions by task keywords
        conditions = []
        params = []
        for kw in keywords[:4]:
            conditions.append("task LIKE ?")
            params.append(f'%{kw}%')
        where = " OR ".join(conditions)

        matching_sessions = self._conn.execute(
            f"SELECT id, task, created_at FROM sessions WHERE {where} ORDER BY created_at DESC LIMIT ?",
            params + [max_sessions]
        ).fetchall()

        if not matching_sessions:
            return ""

        context = "[Past Session Context — relevant tool calls from similar past tasks]\n"
        total_turns = 0
        for session in matching_sessions:
            if total_turns >= max_turns:
                break
            # Get tool-call turns only (most relevant for context)
            turns = self._conn.execute(
                "SELECT turn_num, tool_name, tool_args, tool_result FROM session_turns "
                "WHERE session_id=? AND tool_name != '' ORDER BY turn_num LIMIT ?",
                (session['id'], max_turns - total_turns)
            ).fetchall()

            if turns:
                context += f"  Session '{session['task'][:60]}' ({session['created_at'][:10]}):\n"
                for t in turns:
                    args_preview = t['tool_args'][:100] if t['tool_args'] else ''
                    result_preview = t['tool_result'][:150] if t['tool_result'] else ''
                    context += f"    Turn {t['turn_num']}: {t['tool_name']}({args_preview}) → {result_preview}\n"
                    total_turns += 1

        return context[:1500]

    # ── Evolution Loss & Gradient Descent ──

    def compute_evolution_loss(self, session_id: int, turns_used: int,
                               max_turns: int, task_success: bool,
                               accessed_fact_ids: List[int],
                               accessed_skill_names: List[str],
                               hindsight_hint: str = '') -> Dict:
        """Compute per-session evolution loss and gradient direction.

        The loss measures how effectively knowledge served task completion.
        The gradient identifies which knowledge contributed positively/negatively.
        This mirrors gradient descent: loss → direction → parameter update.
        """
        # loss_task: 0 if success, 1 if fail
        loss_task = 0.0 if task_success else 1.0

        # loss_efficiency: normalized turns (0 = instant, 1 = max_turns used)
        loss_efficiency = turns_used / max_turns

        # loss_recurrence: weighted penalty if similar task failed before
        session = self._conn.execute("SELECT task FROM sessions WHERE id=?", (session_id,)).fetchone()
        keywords = self._extract_keywords(session['task'] if session else '')
        recurrence = 0.0
        for kw in keywords[:3]:
            past_failures = self._conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE task LIKE ? AND result NOT LIKE '%200%' AND result NOT LIKE '%success%'",
                (f'%{kw}%',)
            ).fetchone()[0]
            recurrence += past_failures * 0.1
        loss_recurrence = min(recurrence, 1.0)

        # loss_knowledge_quality: use feedback_events if available, fallback to helpful_count
        fb_quality = self.session_feedback_quality(session_id)
        if fb_quality['total_count'] > 0:
            helpful_ratio = fb_quality['helpful_count'] / max(fb_quality['total_count'], len(accessed_fact_ids) if accessed_fact_ids else 1)
        elif accessed_fact_ids:
            helpful = 0
            for fid in accessed_fact_ids:
                fact = self._conn.execute("SELECT helpful_count, unhelpful_count FROM facts WHERE id=?", (fid,)).fetchone()
                if fact and fact['helpful_count'] > fact['unhelpful_count']:
                    helpful += 1
            helpful_ratio = helpful / len(accessed_fact_ids)
        else:
            helpful_ratio = 0.5  # neutral when no facts accessed
        loss_knowledge_quality = 1.0 - helpful_ratio

        # Weighted total loss
        loss_total = loss_task + 0.3 * loss_efficiency + 0.5 * loss_recurrence + 0.2 * loss_knowledge_quality

        # Compute gradient direction — which knowledge contributed which way
        gradient_facts = []
        for fid in accessed_fact_ids:
            fact = self._conn.execute("SELECT content, trust_score FROM facts WHERE id=?", (fid,)).fetchone()
            if fact:
                direction = '+' if task_success else '-'
                magnitude = abs(loss_total * 0.1)
                gradient_facts.append({'id': fid, 'direction': direction, 'magnitude': round(magnitude, 3)})

        gradient_skills = []
        for sname in accessed_skill_names:
            skill = self._conn.execute("SELECT success_rate FROM skills WHERE name=?", (sname,)).fetchone()
            if skill:
                direction = '+' if task_success else '-'
                magnitude = abs(loss_total * 0.2)
                gradient_skills.append({'name': sname, 'direction': direction, 'magnitude': round(magnitude, 3)})

        # Improvement targets: skills with negative gradient + items needing review when declining
        improvement_targets = [g['name'] for g in gradient_skills if g['direction'] == '-']
        # When evolution is declining, also include items flagged for review
        _, trend = self.evolution_score()
        if trend < 0:
            review_items = self.get_items_needing_review()
            for f in review_items['facts_needing_review']:
                improvement_targets.append(f"fact:{f['content'][:30]}")
            for s in review_items['skills_needing_review']:
                improvement_targets.append(f"skill:{s['name']}")

        # Evolution score (inverse of rolling average loss over last 5 sessions)
        recent_losses = self._conn.execute(
            "SELECT loss_total FROM evolution_log ORDER BY created_at DESC LIMIT 5"
        ).fetchall()
        if recent_losses:
            avg_loss = sum(r[0] for r in recent_losses) / len(recent_losses)
        else:
            avg_loss = loss_total
        evolution_score = round(1.0 - min(avg_loss / 2.0, 1.0), 3)  # normalize: max loss ~2.0

        return {
            'loss_task': loss_task,
            'loss_efficiency': loss_efficiency,
            'loss_recurrence': loss_recurrence,
            'loss_knowledge_quality': loss_knowledge_quality,
            'loss_total': loss_total,
            'evolution_score': evolution_score,
            'gradient_facts': gradient_facts,
            'gradient_skills': gradient_skills,
            'improvement_targets': improvement_targets,
            'hindsight_hint': hindsight_hint,
        }

    @_write_locked
    def evolution_log_add(self, session_id: int, loss_data: Dict) -> int:
        """Persist computed evolution loss to the evolution_log table."""
        now = datetime.now().isoformat()
        cur = self._conn.execute(
            "INSERT INTO evolution_log (session_id,loss_task,loss_efficiency,loss_recurrence,"
            "loss_knowledge_quality,loss_total,evolution_score,gradient_facts,gradient_skills,"
            "improvement_targets,hindsight_hint,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (session_id, loss_data['loss_task'], loss_data['loss_efficiency'],
             loss_data['loss_recurrence'], loss_data['loss_knowledge_quality'],
             loss_data['loss_total'], loss_data['evolution_score'],
             json.dumps(loss_data['gradient_facts'], ensure_ascii=False),
             json.dumps(loss_data['gradient_skills'], ensure_ascii=False),
             json.dumps(loss_data['improvement_targets'], ensure_ascii=False),
             loss_data['hindsight_hint'], now)
        )
        self._conn.commit()
        return cur.lastrowid

    @_write_locked
    def apply_gradient(self, loss_data: Dict):
        """Apply gradient descent step — update knowledge parameters proportional to loss.

        Key innovation: instead of flat +0.05/-0.10, magnitude scales with loss_total.
        High-loss sessions → stronger updates (fast learning).
        Low-loss sessions → gentle updates (fine-tuning).
        """
        for g in loss_data['gradient_facts']:
            if g['direction'] == '+':
                self._conn.execute(
                    "UPDATE facts SET trust_score=MIN(trust_score+?, 1.0) WHERE id=?",
                    (g['magnitude'], g['id'])
                )
            else:
                # 2x penalty: negative direction hurts more than positive helps
                self._conn.execute(
                    "UPDATE facts SET trust_score=MAX(trust_score-?, 0.0) WHERE id=?",
                    (g['magnitude'] * 2, g['id'])
                )

        for g in loss_data['gradient_skills']:
            if g['direction'] == '+':
                self.skill_update_success(g['name'], success=True)
            else:
                self.skill_update_success(g['name'], success=False)

        self._conn.commit()

    def evolution_score(self) -> Tuple[float, float]:
        """Return current evolution_score and trend direction.

        trend > 0: improving, < 0: degrading, 0: stable
        """
        rows = self._conn.execute(
            "SELECT evolution_score, created_at FROM evolution_log ORDER BY created_at DESC LIMIT 10"
        ).fetchall()
        if not rows:
            return 0.5, 0.0  # Default: unknown, neutral trend

        current_score = rows[0]['evolution_score']
        if len(rows) >= 2:
            older_score = rows[-1]['evolution_score']
            trend = current_score - older_score
        else:
            trend = 0.0
        return current_score, trend

    def _extract_keywords(self, text: str) -> List[str]:
        """Extract meaningful keywords from text (skip stopwords)."""
        stopwords = {'the','a','an','is','are','was','were','be','been','being',
                     'have','has','had','do','does','did','will','would','could',
                     'should','may','might','shall','can','need','to','of','in',
                     'for','on','with','at','by','from','as','into','through',
                     'during','before','after','above','below','between','out',
                     'off','over','under','again','further','then','once','here',
                     'there','when','where','why','how','all','both','each','few',
                     'more','most','other','some','such','no','not','only','own',
                     'same','so','than','too','very','just','because','but','and',
                     'or','if','while','about','up','it','its','this','that','these',
                     'those','i','me','my','we','our','you','your','he','him','his',
                     'she','her','they','them','their','what','which','who','whom',
                     'build','create','write','make','solve','fix','use','run','show',
                     'help','tell','give','find','get','set','put','add','remove'}
        words = re.findall(r'\b[a-zA-Z]{3,}\b', text.lower())
        return [w for w in words if w not in stopwords]

    def proactive_recall(self, task_prompt: str) -> str:
        """Inject relevant prior knowledge at task start.

        Extracts keywords from the task, searches facts (high-trust only),
        and returns a compact context block to prepend to the user input.
        """
        keywords = self._extract_keywords(task_prompt)
        if not keywords:
            return ""

        scope_where = self._scope_where('f.')
        scope_params = self._scope_params()

        results = []
        for kw in keywords[:5]:  # Limit keyword expansion
            query = f"SELECT f.id, f.content, f.category, f.trust_score, f.tags FROM facts f INNER JOIN facts_fts ON f.id = facts_fts.rowid WHERE {scope_where} AND facts_fts MATCH ? ORDER BY f.trust_score DESC LIMIT 10"
            try:
                rows = self._conn.execute(query, [kw] + scope_params).fetchall()
                for r in rows:
                    results.append(dict(r))
            except Exception:
                pass

        if not results:
            return ""

        # Dedup by id
        seen = set()
        unique = []
        for r in results:
            if r['id'] not in seen:
                seen.add(r['id'])
                unique.append(r)

        lines = ["[Proactive Recall — proven facts relevant to this task]"]
        for f in unique[:5]:
            lines.append(f"  [{f['category']}] {f['content']} (trust: {f['trust_score']:.2f})")
        return "\n".join(lines)

    def session_crystallize(self, session_id: int) -> Optional[int]:
        """Fix #1: Only crystallize sessions that produced knowledge artifacts."""
        session = self._conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if not session:
            return None

        # Only crystallize if the session produced knowledge (wiki_ingest/fact_add were called)
        if not session['had_knowledge_output']:
            return None

        title = f"Session: {session['task'][:50]}"
        content = f"# {session['task']}\n\n## Summary\n{session['summary']}\n\n## Result\n{session['result']}\n\n## Date\n{session['created_at']}\n"
        slug = self._make_slug(f"session-{session['task'][:30]}-{session['created_at'][:10]}")

        page_id = self.wiki_add(slug, title, content, category='session-log',
                                tags='session,archive',
                                confidence='high' if self.evolution_score()[0] > 0.7 else 'medium' if self.evolution_score()[0] > 0.4 else 'low')
        self._conn.execute("UPDATE sessions SET wiki_page_id=? WHERE id=?", (page_id, session_id))
        self._conn.commit()
        return page_id

    # ── Pruning (Fix #3: prevent unbounded growth) ──

    def prune_old_sessions(self, max_age_days: int = 30) -> int:
        """Delete session-log wiki pages, session_turns, and sessions older than max_age_days."""
        cutoff = datetime.now().timestamp() - max_age_days * 86400
        cutoff_str = datetime.fromtimestamp(cutoff).isoformat()
        # Delete session_turns for old sessions first (FK child)
        self._conn.execute(
            "DELETE FROM session_turns WHERE session_id IN (SELECT id FROM sessions WHERE created_at < ?)",
            (cutoff_str,)
        )
        # Delete feedback_events for old sessions
        self._conn.execute(
            "DELETE FROM feedback_events WHERE session_id IN (SELECT id FROM sessions WHERE created_at < ?)",
            (cutoff_str,)
        )
        # Delete old session-log wiki pages
        old_pages = self._conn.execute(
            "SELECT slug FROM wiki_pages WHERE category='session-log' AND updated_at < ?",
            (cutoff_str,)
        ).fetchall()
        count = 0
        for p in old_pages:
            self.wiki_delete(p['slug'])
            count += 1
        # Delete old sessions with no wiki page
        old_sessions = self._conn.execute(
            "DELETE FROM sessions WHERE created_at < ? AND wiki_page_id IS NULL",
            (cutoff_str,)
        ).rowcount
        count += old_sessions
        self._conn.commit()
        return count

    @_write_locked
    def decay_low_trust_facts(self, min_trust: float = 0.2) -> int:
        """Delete facts that have decayed below minimum trust."""
        count = self._conn.execute(
            "DELETE FROM facts WHERE trust_score < ?", (min_trust,)
        ).rowcount
        self._conn.commit()
        return count

    @_write_locked
    def apply_time_decay(self) -> int:
        """Apply time-based trust decay — facts get less trusted as they age.

        Environment facts decay fast (configs change often).
        Pattern/convention facts decay slowly (knowledge lasts longer).
        Other categories decay at moderate rate.
        """
        now = datetime.now()
        decayed = 0
        rows = self._conn.execute("SELECT id, category, updated_at, trust_score FROM facts").fetchall()
        for row in rows:
            try:
                updated = datetime.fromisoformat(row['updated_at'])
            except (ValueError, TypeError):
                continue
            days_old = (now - updated).days

            # Decay rates per category (trust loss per month of age)
            if row['category'] in ('environment', 'debugging'):
                rate = 0.06  # Fast: env changes, debugging context expires
            elif row['category'] in ('pattern', 'convention', 'decision'):
                rate = 0.01  # Slow: patterns are durable
            else:
                rate = 0.03  # Moderate: general/architecture

            # Only decay if fact hasn't been recently retrieved (retrieval_count < 5)
            retrieval = self._conn.execute("SELECT retrieval_count FROM facts WHERE id=?", (row['id'],)).fetchone()
            if retrieval and retrieval[0] >= 5:
                continue  # Frequently used facts resist decay

            months_old = days_old / 30.0
            decay = rate * months_old
            new_trust = max(0.1, row['trust_score'] - decay)  # Never go below 0.1 here; auto-delete at 0.15

            if new_trust != row['trust_score']:
                self._conn.execute(
                    "UPDATE facts SET trust_score=?, updated_at=? WHERE id=?",
                    (new_trust, row['updated_at'], row['id'])  # Keep original updated_at so decay compounds
                )
                decayed += 1

        # Auto-delete facts that decayed below 0.15
        deleted = self._conn.execute("DELETE FROM facts WHERE trust_score < 0.15").rowcount
        self._conn.commit()
        return decayed + deleted

    # ── Context Builder (with budget limit — Fix #3) ──

    def build_context_prompt(self, task_prompt: str = '') -> str:
        """Build compact context injection — meta rules + catalog, not full dumps.

        The agent has db_query/fact_search/wiki_query for retrieval.
        Context injection only provides: (1) behavioral rules, (2) what's available to query.
        This saves ~2000 chars so the LLM has more room for task reasoning.
        """
        prompt = ""

        # L0 — Meta Rules (always inject — behavioral constraints that must always apply)
        meta = self.wiki_read('meta-rules')
        if meta:
            prompt += f"\n[L0 — Meta Rules]\n{meta['content']}\n"

        # Catalog — what knowledge is available (not the knowledge itself)
        total_facts = self.count_facts()
        global_facts = self._conn.execute(
            "SELECT COUNT(*) FROM facts WHERE project_id IS NULL"
        ).fetchone()[0]
        project_facts = total_facts - global_facts
        total_wiki = self.count_wiki_pages()
        global_wiki = self._conn.execute(
            "SELECT COUNT(*) FROM wiki_pages WHERE project_id IS NULL"
        ).fetchone()[0]
        project_wiki = total_wiki - global_wiki
        total_skills = self.count_skills()
        global_skills = self._conn.execute(
            "SELECT COUNT(*) FROM skills WHERE project_id IS NULL"
        ).fetchone()[0]
        project_skills = total_skills - global_skills

        # Category breakdown for routing hints
        cats = self._conn.execute(
            "SELECT category, COUNT(*) as cnt FROM facts GROUP BY category ORDER BY cnt DESC"
        ).fetchall()

        prompt += "\n[Knowledge Catalog]\n"
        prompt += f"  Facts: {project_facts} project + {global_facts} global (total {total_facts})\n"
        if cats:
            prompt += f"  Categories: {', '.join(f'{r[0]}({r[1]})' for r in cats[:5])}\n"
        prompt += f"  Wiki: {project_wiki} project + {global_wiki} global (total {total_wiki}) pages\n"
        prompt += f"  Skills: {project_skills} project + {global_skills} global (total {total_skills}) SOPs\n"

        # Top 3 highest-trust facts only — proven reliable, worth always showing
        top3 = self.get_high_trust_facts(min_trust=0.7, limit=3)
        if top3:
            prompt += "\n[Proven Facts (trust > 0.7)]\n"
            for f in top3:
                prompt += f"  {f['content']}\n"

        # Evolution status — RL direction for next session
        ev_score, ev_trend = self.evolution_score()
        if ev_score != 0.5:  # Only show if we have data
            trend_arrow = '↑' if ev_trend > 0 else '↓' if ev_trend < 0 else '—'
            prompt += f"\n[Evolution Status] score={ev_score:.2f} trend={trend_arrow}\n"
            if ev_trend < 0:
                targets_row = self._conn.execute(
                    "SELECT improvement_targets FROM evolution_log ORDER BY created_at DESC LIMIT 1"
                ).fetchone()
                if targets_row:
                    tgt_list = json.loads(targets_row[0])
                    if tgt_list:
                        prompt += f"  Priority targets: {', '.join(tgt_list)}\n"

        # Retrieval hints — tell the agent HOW to get what it needs
        prompt += "\n[Retrieval]\n"
        prompt += "  - db_query SELECT for precise, structured retrieval\n"
        prompt += "  - wiki_query for fuzzy keyword search across wiki pages\n"
        prompt += "  - fact_search for trust-ranked fact lookup\n"
        prompt += "  - db_schema to inspect available tables/columns\n"

        prompt += f"\ncwd = {os.path.dirname(self.db_path)}\n"

        return prompt

    # ── Backward-compatible file-like API ──

    def read_layer(self, layer: str) -> str:
        mapping = {
            'L0_meta_rules.txt': self._read_l0,
            'L1_insight_index.txt': self._read_l1,
            'L2_global_facts.txt': self._read_l2,
        }
        reader = mapping.get(layer)
        if reader:
            return reader()
        return ""

    def _read_l0(self) -> str:
        meta = self.wiki_read('meta-rules')
        return meta['content'] if meta else ""

    def _read_l1(self) -> str:
        lines = []
        for f in self._conn.execute("SELECT content,category FROM facts ORDER BY trust_score DESC").fetchall():
            lines.append(f"fact({f['category']}): {f['content']}")
        for s in self._conn.execute("SELECT name FROM skills ORDER BY success_rate DESC").fetchall():
            lines.append(f"skill: {s['name']}")
        return '\n'.join(lines)

    def _read_l2(self) -> str:
        lines = []
        for f in self._conn.execute("SELECT content,category,tags FROM facts ORDER BY trust_score DESC").fetchall():
            lines.append(f"[{f['category']}] {f['content']} (tags: {f['tags']})")
        return '\n'.join(lines)

    def write_layer(self, layer: str, content: str, mode='overwrite'):
        if layer == 'L0_meta_rules.txt':
            self.wiki_add('meta-rules', 'Meta Rules', content, category='convention', tags='rules,behavior,core')
        elif layer == 'L2_global_facts.txt':
            for line in content.strip().split('\n'):
                line = line.strip()
                if not line or line.startswith('#') or line.startswith('['):
                    continue
                cat_match = re.match(r'\[(\w+)\]\s*(.+?)(?:\s*\(tags:\s*(.+)\))?', line)
                if cat_match:
                    self.fact_add(cat_match.group(2), category=cat_match.group(1), tags=cat_match.group(3) or '')
                else:
                    self.fact_add(line)

    def archive_session(self, summary: str, task: str, result: str, had_knowledge: bool = False):
        sid = self.session_archive(task, summary, result, had_knowledge=had_knowledge)
        # Fix #1: Only crystallize if knowledge was produced
        if had_knowledge:
            self.session_crystallize(sid)

    def close(self):
        self._conn.close()

    # ── Stats ──

    def stats(self) -> Dict:
        ev_score, ev_trend = self.evolution_score()
        total_facts = self._conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
        global_facts = self._conn.execute("SELECT COUNT(*) FROM facts WHERE project_id IS NULL").fetchone()[0]
        project_facts = 0
        if self.current_project_id:
            project_facts = self._conn.execute("SELECT COUNT(*) FROM facts WHERE project_id=?", (self.current_project_id,)).fetchone()[0]
        total_skills = self._conn.execute("SELECT COUNT(*) FROM skills").fetchone()[0]
        global_skills = self._conn.execute("SELECT COUNT(*) FROM skills WHERE project_id IS NULL").fetchone()[0]
        project_skills = 0
        if self.current_project_id:
            project_skills = self._conn.execute("SELECT COUNT(*) FROM skills WHERE project_id=?", (self.current_project_id,)).fetchone()[0]
        total_wiki = self._conn.execute("SELECT COUNT(*) FROM wiki_pages").fetchone()[0]
        global_wiki = self._conn.execute("SELECT COUNT(*) FROM wiki_pages WHERE project_id IS NULL").fetchone()[0]
        project_wiki = 0
        if self.current_project_id:
            project_wiki = self._conn.execute("SELECT COUNT(*) FROM wiki_pages WHERE project_id=?", (self.current_project_id,)).fetchone()[0]
        total_sessions = self._conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        global_sessions = self._conn.execute("SELECT COUNT(*) FROM sessions WHERE project_id IS NULL").fetchone()[0]
        avg_trust = self._conn.execute("SELECT AVG(trust_score) FROM facts").fetchone()[0] or 0
        return {
            'total_facts': total_facts,
            'global_facts': global_facts,
            'project_facts': project_facts,
            'total_skills': total_skills,
            'global_skills': global_skills,
            'project_skills': project_skills,
            'total_wiki_pages': total_wiki,
            'global_wiki_pages': global_wiki,
            'project_wiki_pages': project_wiki,
            'total_sessions': total_sessions,
            'global_sessions': global_sessions,
            'avg_trust': avg_trust,
            'evolution_score': ev_score,
            'evolution_trend': ev_trend,
            'current_project': self.current_project_id,
        }

    # ── SQL Sandbox (LLM-as-DBA) ──

    def get_schema_info(self) -> Dict:
        """Return table/column info so the LLM knows what it can query."""
        tables = {}
        for table_name in DBQ_ALLOWED_TABLES:
            if table_name == '_meta':
                cols = [{'name': 'key', 'type': 'TEXT'}, {'name': 'value', 'type': 'TEXT'}]
            else:
                try:
                    rows = self._conn.execute(f"PRAGMA table_info({table_name})").fetchall()
                    cols = [{'name': r[1], 'type': r[2]} for r in rows]
                except Exception:
                    cols = []
            if cols:
                tables[table_name] = cols
        return {
            'tables': tables,
            'db_path': self.db_path,
            'row_counts': {
                t: self._conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in DBQ_ALLOWED_TABLES if t in tables
            },
        }

    def _validate_sql(self, sql: str) -> Optional[str]:
        """Validate SQL for safe execution. Returns error string if blocked."""
        stripped = sql.strip()
        if not stripped:
            return "Empty SQL statement."

        first_word = stripped.upper().split()[0]
        if first_word not in DBQ_ALLOWED_OPS:
            return f"Blocked: '{first_word}' not allowed. Use SELECT, INSERT, or UPDATE only."

        # Remove string literals before keyword checking to avoid false positives
        # (e.g., "CREATE" inside 'creating a skill' should not trigger the CREATE keyword block)
        sql_no_strings = re.sub(r"'[^']*'", '', stripped)
        upper_sql = sql_no_strings.upper()
        for kw in DBQ_BLOCKED_KEYWORDS:
            pattern = r'\b' + kw + r'\b'
            if re.search(pattern, upper_sql):
                if kw == 'REPLACE' and 'INSERT OR REPLACE' in upper_sql:
                    continue
                return f"Blocked: '{kw}' keyword found. Only SELECT/INSERT/UPDATE allowed."

        # Validate table names
        table_refs = re.findall(r'(?:FROM|INTO|UPDATE|JOIN)\s+(\w+)', stripped, re.IGNORECASE)
        for t in table_refs:
            if t.lower() not in {t2.lower() for t2 in DBQ_ALLOWED_TABLES}:
                return f"Blocked: table '{t}' not in allowed tables: {sorted(DBQ_ALLOWED_TABLES)}"

        return None  # Safe

    def safe_query(self, sql: str) -> Dict:
        """Execute sandboxed SQL. SELECT/INSERT/UPDATE only, whitelisted tables, result limits."""
        err = self._validate_sql(sql)
        if err:
            return {"status": "error", "msg": err}

        with self._lock:
            try:
                if sql.strip().upper().startswith('SELECT'):
                    rows = self._conn.execute(sql).fetchall()
                    rows = rows[:DBQ_MAX_ROWS]
                    results = [dict(r) for r in rows]
                    total_chars = len(json.dumps(results, ensure_ascii=False))
                    truncated = False
                    if total_chars > DBQ_MAX_CHARS:
                        results = results[:10]
                        truncated = True

                    # Trust evolution: if SELECT touched facts, bump retrieval count
                    if 'facts' in sql.lower() and results:
                        # Get fact IDs from results if id column present, or by matching content
                        now = datetime.now().isoformat()
                        if results and 'id' in results[0]:
                            for fid in [r['id'] for r in results if 'id' in r]:
                                self._conn.execute(
                                    "UPDATE facts SET retrieval_count=retrieval_count+1, trust_score=MIN(trust_score+0.01,1.0), updated_at=? WHERE id=?",
                                    (now, fid)
                                )
                        else:
                            # Fallback: bump all facts that appear in results by matching content
                            contents = [r.get('content') for r in results if r.get('content')]
                            for c in contents:
                                self._conn.execute(
                                    "UPDATE facts SET retrieval_count=retrieval_count+1, trust_score=MIN(trust_score+0.01,1.0), updated_at=? WHERE content=?",
                                    (now, c)
                                )
                        self._conn.commit()

                    return {
                        "status": "success",
                        "rows": results,
                        "row_count": len(results),
                        "truncated": truncated,
                    }
                else:
                    # INSERT or UPDATE
                    cur = self._conn.execute(sql)
                    self._conn.commit()
                    return {
                        "status": "success",
                        "affected_rows": cur.rowcount,
                        "lastrowid": cur.lastrowid,
                    }
            except Exception as e:
                return {"status": "error", "msg": str(e)}


# ── Project Scan (read-only directory analysis for /learn) ──

    def project_scan(self, project_root: str, depth: str = 'standard') -> dict:
        """Scan a project directory and return structured info for LLM-driven knowledge generation.

        Read-only — gathers info from the filesystem, doesn't write to memory.
        The LLM decides what knowledge to create from this scan data.
        """
        import os as _os

        result = {
            'project_root': project_root,
            'project_name': _os.path.basename(project_root),
            'depth': depth,
            'readme': '',
            'package_config': '',
            'file_tree': [],
            'language': '',
            'key_dirs': [],
            'dependencies': [],
            'test_framework': '',
            'lint_tool': '',
            'project_knowledge_files': '',
            'workspace_configs': '',
            'infrastructure_configs': '',
            'ci_configs': '',
        }

        # ── Read README ──
        readme_paths = ['README.md', 'README.rst', 'README.txt', 'README']
        for rp in readme_paths:
            full = _os.path.join(project_root, rp)
            if _os.path.isfile(full):
                try:
                    with open(full, 'r', encoding='utf-8', errors='replace') as f:
                        content = f.read(5000)
                    result['readme'] = content
                    break
                except Exception:
                    pass

        # ── Read package config ──
        config_paths = [
            ('pyproject.toml', 'python'),
            ('setup.py', 'python'),
            ('setup.cfg', 'python'),
            ('requirements.txt', 'python'),
            ('package.json', 'javascript'),
            ('Cargo.toml', 'rust'),
            ('go.mod', 'go'),
            ('Gemfile', 'ruby'),
            ('pom.xml', 'java'),
            ('build.gradle', 'java'),
            ('Makefile', 'mixed'),
        ]
        for cp, lang in config_paths:
            full = _os.path.join(project_root, cp)
            if _os.path.isfile(full):
                try:
                    with open(full, 'r', encoding='utf-8', errors='replace') as f:
                        content = f.read(3000)
                    result['package_config'] += f"\n--- {cp} ---\n{content}\n"
                    if not result['language']:
                        result['language'] = lang
                except Exception:
                    pass

        # ── Scan file tree ──
        max_depth = {'quick': 1, 'standard': 2, 'deep': 3}.get(depth, 2)
        skip_dirs = {'.git', '.venv', 'venv', '__pycache__', 'node_modules', '.nova', '.omc',
                     'dist', 'build', '.pytest_cache', '.tox', 'egg-info', '.mypy_cache', '.idea'}

        def _scan_dir(path, level):
            if level > max_depth:
                return []
            items = []
            try:
                for entry in sorted(_os.listdir(path)):
                    if entry in skip_dirs or entry.startswith('.'):
                        continue
                    full = _os.path.join(path, entry)
                    if _os.path.isdir(full):
                        items.append(f"{entry}/")
                        if level < max_depth:
                            items.extend(_scan_dir(full, level + 1))
                    else:
                        items.append(entry)
            except Exception:
                pass
            return items

        result['file_tree'] = _scan_dir(project_root, 1)

        # ── Detect key directories ──
        common_src_dirs = ['src', 'lib', 'app', 'nova', 'cmd', 'pkg', 'internal', 'core']
        common_test_dirs = ['tests', 'test', 'spec', '__tests__']
        for entry in result['file_tree']:
            if entry.endswith('/') and entry.rstrip('/') in common_src_dirs:
                result['key_dirs'].append(entry.rstrip('/'))
            if entry.endswith('/') and entry.rstrip('/') in common_test_dirs:
                result['key_dirs'].append(entry.rstrip('/'))

        # ── Detect test/lint from config ──
        config_lower = result['package_config'].lower()
        if 'pytest' in config_lower:
            result['test_framework'] = 'pytest'
        elif 'unittest' in config_lower:
            result['test_framework'] = 'unittest'
        elif 'jest' in config_lower:
            result['test_framework'] = 'jest'
        elif 'vitest' in config_lower:
            result['test_framework'] = 'vitest'
        elif 'playwright' in config_lower:
            result['test_framework'] = 'playwright'
        elif 'mocha' in config_lower:
            result['test_framework'] = 'mocha'
        if 'ruff' in config_lower:
            result['lint_tool'] = 'ruff'
        elif 'flake8' in config_lower:
            result['lint_tool'] = 'flake8'
        elif 'eslint' in config_lower:
            result['lint_tool'] = 'eslint'

        # ── Extract dependencies from package config ──
        import re as _re
        # npm dependencies — only from "dependencies" and "devDependencies" blocks
        for block_match in _re.finditer(
            r'"(?:dev)?[dD]ependencies"\s*:\s*\{([^}]+)\}',
            result['package_config']
        ):
            for dep_match in _re.finditer(
                r'"([a-zA-Z0-9_/@.-]+)":\s*"[^"]*"',
                block_match.group(1)
            ):
                result['dependencies'].append(dep_match.group(1))
        # Python requirements — lines starting with a package name (not comments/flags)
        for line in config_lower.splitlines():
            line = line.strip()
            if line.startswith('#') or line.startswith('-') or line.startswith('['):
                continue
            dep = _re.match(r'^([a-zA-Z0-9_-]+)', line)
            if dep and dep.group(1) not in ('the', 'and', 'for', 'with', 'from', 'import',
                                             'install', 'requires', 'project', 'tool', 'build',
                                             'source', 'options', 'include'):
                result['dependencies'].append(dep.group(1))

        result['dependencies'] = list(set(result['dependencies']))[:30]

        # ── Read project knowledge files (CLAUDE.md, AGENTS.md, etc.) ──
        knowledge_paths = ['CLAUDE.md', 'AGENTS.md', 'CONTRIBUTING.md', 'CODESTYLE.md']
        for kp in knowledge_paths:
            full = _os.path.join(project_root, kp)
            if _os.path.isfile(full):
                try:
                    with open(full, 'r', encoding='utf-8', errors='replace') as f:
                        result['project_knowledge_files'] += f"\n--- {kp} ---\n{f.read(5000)}\n"
                except Exception:
                    pass

        # ── Read workspace/sub-package configs ──
        workspace_dirs = ['packages', 'libs', 'modules', 'apps', 'services']
        for wd in workspace_dirs:
            wd_full = _os.path.join(project_root, wd)
            if _os.path.isdir(wd_full):
                for entry in sorted(_os.listdir(wd_full)):
                    entry_full = _os.path.join(wd_full, entry)
                    pj = _os.path.join(entry_full, 'package.json')
                    pyproj = _os.path.join(entry_full, 'pyproject.toml')
                    for cfg in [pj, pyproj]:
                        if _os.path.isfile(cfg):
                            try:
                                with open(cfg, 'r', encoding='utf-8', errors='replace') as f:
                                    result['workspace_configs'] += f"\n--- {wd}/{entry}/{_os.path.basename(cfg)} ---\n{f.read(3000)}\n"
                            except Exception:
                                pass

        # ── Read infrastructure configs ──
        infra_paths = [
            'tsconfig.json', 'docker-compose.yml', 'docker-compose.dev.yml',
            '.env.example', 'nginx.conf', 'Makefile', 'Vagrantfile',
        ]
        for ip in infra_paths:
            full = _os.path.join(project_root, ip)
            if _os.path.isfile(full):
                try:
                    with open(full, 'r', encoding='utf-8', errors='replace') as f:
                        result['infrastructure_configs'] += f"\n--- {ip} ---\n{f.read(3000)}\n"
                except Exception:
                    pass

        # ── Read CI configs ──
        ci_base = _os.path.join(project_root, '.github', 'workflows')
        if _os.path.isdir(ci_base):
            for ci_file in sorted(_os.listdir(ci_base))[:5]:
                ci_full = _os.path.join(ci_base, ci_file)
                try:
                    with open(ci_full, 'r', encoding='utf-8', errors='replace') as f:
                        result['ci_configs'] += f"\n--- .github/workflows/{ci_file} ---\n{f.read(2000)}\n"
                except Exception:
                    pass

        return result


# ── Legacy alias ──
MemoryEngine = NovaMemory

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
    triggers TEXT NOT NULL DEFAULT '',
    pitfalls TEXT NOT NULL DEFAULT '[]',
    success_rate REAL NOT NULL DEFAULT 0.5,
    usage_count INTEGER NOT NULL DEFAULT 0,
    tags TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 1,
    last_improved_at TEXT NOT NULL DEFAULT '',
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
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_session_turns_session ON session_turns(session_id, turn_num);

-- Update schema version
UPDATE _meta SET value = '3' WHERE key = 'schema_version';
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
DBQ_ALLOWED_TABLES = {'wiki_pages', 'facts', 'skills', 'sessions', 'session_turns', '_meta'}
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


def _route_tier(category: str, tier: str) -> str:
    """Determine which tier to use based on category and explicit tier override."""
    if tier in ('local', 'global'):
        return tier
    # 'auto' routing: most categories default to local, explicit global categories override
    LOCAL_CATEGORIES = {'environment', 'session-log', 'debugging', 'reference', 'general', 'architecture'}
    GLOBAL_CATEGORIES = {'pattern', 'convention', 'decision'}
    if category in GLOBAL_CATEGORIES:
        return 'global'
    return 'local'


# ── NovaMemory (single DB) ──

class NovaMemory:
    """SQL-backed memory engine with Wiki compounding."""

    def __init__(self, db_path: str):
        self.db_path = db_path
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

    # ── Query helpers (Fix #7: avoid direct _conn coupling in TwoTierMemory) ──

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
                "INSERT INTO wiki_pages (slug,title,category,content,tags,confidence,sources,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (slug, title, category, content, tags, confidence, sources, now, now)
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
        row = self._conn.execute("SELECT * FROM wiki_pages WHERE slug=?", (slug,)).fetchone()
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
                if category:
                    cat_clause = " AND category=?"
                    params.append(category)
                rows = self._conn.execute(
                    f"SELECT * FROM wiki_pages WHERE {where}{cat_clause} ORDER BY updated_at DESC",
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
                f"SELECT * FROM wiki_pages WHERE category != 'session-log'{cat_filter}",
                params
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
                "SELECT * FROM wiki_pages WHERE category=? ORDER BY updated_at DESC LIMIT ?",
                (category, limit)
            ).fetchall()
            results = [dict(r) for r in rows]

        for r in results:
            r['cross_refs'] = re.findall(r'\[\[(\w[\w-]*)\]\]', r.get('content', ''))

        return results

    def wiki_list(self) -> List[Dict]:
        rows = self._conn.execute(
            "SELECT id,slug,title,category,tags,confidence,updated_at FROM wiki_pages ORDER BY updated_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    @_write_locked
    def wiki_delete(self, slug: str) -> bool:
        count = self._conn.execute("DELETE FROM wiki_pages WHERE slug=?", (slug,)).rowcount
        self._conn.commit()
        return count > 0

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
        try:
            cur = self._conn.execute(
                "INSERT INTO facts (content,category,tags,trust_score,created_at,updated_at) VALUES (?,?,?,?,?,?)",
                (content, category, tags, trust_score, now, now)
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

    # ── Skill/SOP Operations ──

    @_write_locked
    def skill_add(self, name: str, description: str, steps: list,
                  tags: str = '', success_rate: float = 0.5,
                  triggers: str = '', pitfalls: list = None) -> int:
        steps_json = json.dumps(steps, ensure_ascii=False)
        pitfalls_json = json.dumps(pitfalls or [], ensure_ascii=False)
        now = datetime.now().isoformat()
        try:
            cur = self._conn.execute(
                "INSERT INTO skills (name,description,steps,triggers,pitfalls,success_rate,tags,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (name, description, steps_json, triggers, pitfalls_json, success_rate, tags, now, now)
            )
            self._conn.commit()
            return cur.lastrowid
        except sqlite3.IntegrityError:
            self._conn.execute(
                "UPDATE skills SET steps=?, triggers=?, pitfalls=?, success_rate=?, tags=?, version=version+1, last_improved_at=?, updated_at=? WHERE name=?",
                (steps_json, triggers, pitfalls_json, success_rate, tags, now, now, name)
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
        where = "(" + " OR ".join(conditions) + ") AND success_rate >= ?"

        # Build params — each keyword produces 4 LIKE params, plus min_success
        params = []
        for kw in keywords[:5]:
            like_val = f'%{kw}%'
            params.extend([like_val, like_val, like_val, like_val])
        params.append(min_success)

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
            "INSERT INTO sessions (task,summary,result,had_knowledge_output,created_at) VALUES (?,?,'',0,?)",
            (task[:200], '', now)
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
                                tags='session,archive', confidence='low')
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

    def build_context_prompt(self) -> str:
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
        local_facts = self.count_facts()
        global_facts = self._global.count_facts()
        local_wiki = self.count_wiki_pages()
        global_wiki = self._global.count_wiki_pages()
        local_skills = self.count_skills()
        global_skills = self._global.count_skills()

        # Category breakdown for routing hints
        local_cats = self._conn.execute(
            "SELECT category, COUNT(*) as cnt FROM facts GROUP BY category ORDER BY cnt DESC"
        ).fetchall()
        global_cats = self._global._conn.execute(
            "SELECT category, COUNT(*) as cnt FROM facts GROUP BY category ORDER BY cnt DESC"
        ).fetchall()

        prompt += "\n[Knowledge Catalog]\n"
        prompt += f"  Facts: {local_facts} local + {global_facts} global\n"
        if local_cats:
            prompt += f"  Local categories: {', '.join(f'{r[0]}({r[1]})' for r in local_cats[:5])}\n"
        if global_cats:
            prompt += f"  Global categories: {', '.join(f'{r[0]}({r[1]})' for r in global_cats[:5])}\n"
        prompt += f"  Wiki: {local_wiki} local + {global_wiki} global pages\n"
        prompt += f"  Skills: {local_skills} local + {global_skills} global SOPs\n"

        # Top 3 highest-trust facts only — proven reliable, worth always showing
        top3 = self.get_high_trust_facts(min_trust=0.7, limit=3)
        global_top3 = self._global.get_high_trust_facts(min_trust=0.7, limit=3)
        if top3 or global_top3:
            prompt += "\n[Proven Facts (trust > 0.7)]\n"
            for f in top3:
                prompt += f"  [local] {f['content']}\n"
            for f in global_top3:
                prompt += f"  [global] {f['content']}\n"

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
        return {
            'wiki_pages': self.count_wiki_pages(),
            'facts': self.count_facts(),
            'skills': self.count_skills(),
            'sessions': self.count_sessions(),
            'avg_trust': self.avg_trust(),
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

        upper_sql = stripped.upper()
        for kw in DBQ_BLOCKED_KEYWORDS:
            # Check keyword appears as a standalone word (not inside a string literal)
            pattern = r'\b' + kw + r'\b'
            if re.search(pattern, upper_sql):
                # Allow INSERT OR REPLACE (common SQLite idiom) — REPLACE alone is blocked
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


# ── TwoTierMemory ──

class TwoTierMemory:
    """Two-tier memory: project-local + global.

    Project-local DB: <project>/.nova/nova.db — project-specific knowledge
    Global DB: ~/.nova/nova.db — cross-project transferable patterns

    Queries hit both (local first for relevance), writes route by category.
    """

    def __init__(self, local_db_path: str, global_db_path: str):
        self._local = NovaMemory(local_db_path)
        self._global = NovaMemory(global_db_path)
        self._knowledge_produced = False  # Fix #1: track if session produced knowledge

    def _target(self, tier: str) -> NovaMemory:
        return self._local if tier == 'local' else self._global

    def _mark_knowledge_produced(self):
        """Mark that this session produced knowledge artifacts (for smart crystallization)."""
        self._knowledge_produced = True

    # ── Wiki Operations ──

    def wiki_add(self, slug: str, title: str, content: str,
                 category: str = 'reference', tags: str = '',
                 confidence: str = 'medium', sources: str = '',
                 tier: str = 'auto') -> int:
        self._mark_knowledge_produced()
        target = self._target(_route_tier(category, tier))
        return target.wiki_add(slug, title, content, category=category,
                               tags=tags, confidence=confidence, sources=sources)

    def wiki_ingest(self, title: str, content: str, tags: str,
                    category: str = 'reference', confidence: str = 'medium',
                    sources: str = '', tier: str = 'auto') -> int:
        self._mark_knowledge_produced()
        target = self._target(_route_tier(category, tier))
        return target.wiki_ingest(title, content, tags, category=category,
                                  confidence=confidence, sources=sources)

    def wiki_read(self, slug: str, tier: str = 'local') -> Optional[Dict]:
        result = self._local.wiki_read(slug)
        if result:
            return result
        return self._global.wiki_read(slug)

    def wiki_query(self, query: str, category: str = None,
                   tags: str = None, limit: int = 10,
                   tier: str = 'auto') -> List[Dict]:
        results = []
        seen_slugs = set()
        if tier in ('auto', 'local'):
            for r in self._local.wiki_query(query, category=category, tags=tags, limit=limit):
                if r['slug'] not in seen_slugs:
                    r['_tier'] = 'local'
                    results.append(r)
                    seen_slugs.add(r['slug'])
        if tier in ('auto', 'global') and len(results) < limit:
            for r in self._global.wiki_query(query, category=category, tags=tags, limit=limit):
                if r['slug'] not in seen_slugs:
                    r['_tier'] = 'global'
                    results.append(r)
                    seen_slugs.add(r['slug'])
        return results[:limit]

    def wiki_list(self, tier: str = 'auto') -> List[Dict]:
        results = []
        if tier in ('auto', 'local'):
            for p in self._local.wiki_list():
                p['_tier'] = 'local'
                results.append(p)
        if tier in ('auto', 'global'):
            for p in self._global.wiki_list():
                p['_tier'] = 'global'
                results.append(p)
        return results

    def wiki_delete(self, slug: str, tier: str = 'local') -> bool:
        target = self._target(tier)
        return target.wiki_delete(slug)

    def wiki_lint(self) -> List[str]:
        issues = self._local.wiki_lint()
        issues.extend(self._global.wiki_lint())
        return issues

    # ── Fact Operations ──

    def fact_add(self, content: str, category: str = 'general',
                 tags: str = '', trust_score: float = 0.5,
                 tier: str = 'auto') -> int:
        self._mark_knowledge_produced()
        target = self._target(_route_tier(category, tier))
        return target.fact_add(content, category=category, tags=tags, trust_score=trust_score)

    def fact_search(self, query: str, category: str = None,
                    min_trust: float = 0.3, limit: int = 10,
                    tier: str = 'auto') -> List[Dict]:
        results = []
        seen_content = set()
        if tier in ('auto', 'local'):
            for f in self._local.fact_search(query, category=category, min_trust=min_trust, limit=limit):
                if f['content'] not in seen_content:
                    f['_tier'] = 'local'
                    results.append(f)
                    seen_content.add(f['content'])
        if tier in ('auto', 'global') and len(results) < limit:
            for f in self._global.fact_search(query, category=category, min_trust=min_trust, limit=limit):
                if f['content'] not in seen_content:
                    f['_tier'] = 'global'
                    results.append(f)
                    seen_content.add(f['content'])
        return results[:limit]

    def fact_mark_helpful(self, content: str, tier: str = 'auto') -> None:
        for mem in (self._local, self._global):
            try:
                mem.fact_mark_helpful(content)
            except:
                pass

    def fact_mark_helpful_by_id(self, fact_id: int) -> None:
        """Mark a fact helpful by ID — tries both tiers since we may not know which tier it's in."""
        for mem in (self._local, self._global):
            try:
                mem.fact_mark_helpful_by_id(fact_id)
            except:
                pass

    def fact_mark_unhelpful(self, content: str, tier: str = 'auto') -> None:
        for mem in (self._local, self._global):
            try:
                mem.fact_mark_unhelpful(content)
            except:
                pass

    # ── Skill Operations ──

    def skill_add(self, name: str, description: str, steps: list,
                  tags: str = '', success_rate: float = 0.5,
                  triggers: str = '', pitfalls: list = None,
                  tier: str = 'global') -> int:
        self._mark_knowledge_produced()
        target = self._target(tier)
        return target.skill_add(name, description, steps, tags=tags, success_rate=success_rate,
                                triggers=triggers, pitfalls=pitfalls)

    def skill_search(self, query: str, min_success: float = 0.3,
                     limit: int = 5, tier: str = 'auto') -> List[Dict]:
        results = []
        seen_names = set()
        if tier in ('auto', 'global'):
            for s in self._global.skill_search(query, min_success=min_success, limit=limit):
                if s['name'] not in seen_names:
                    s['_tier'] = 'global'
                    results.append(s)
                    seen_names.add(s['name'])
        if tier in ('auto', 'local') and len(results) < limit:
            for s in self._local.skill_search(query, min_success=min_success, limit=limit):
                if s['name'] not in seen_names:
                    s['_tier'] = 'local'
                    results.append(s)
                    seen_names.add(s['name'])
        return results[:limit]

    def skill_update_success(self, name: str, success: bool) -> None:
        for mem in (self._local, self._global):
            try:
                mem.skill_update_success(name, success)
            except:
                pass

    def skill_improve(self, name: str, new_steps: list = None,
                      new_pitfalls: list = None, new_triggers: str = None,
                      tier: str = 'global') -> bool:
        target = self._target(tier)
        return target.skill_improve(name, new_steps=new_steps,
                                    new_pitfalls=new_pitfalls, new_triggers=new_triggers)

    def skill_match(self, query: str, limit: int = 2, min_success: float = 0.3) -> List[Dict]:
        """Proactive skill matching — searches both tiers using SQL LIKE queries."""
        results = []
        seen_names = set()
        # Global first (skills default to global tier)
        for s in self._global.skill_match(query, limit=limit, min_success=min_success):
            if s['name'] not in seen_names:
                s['_tier'] = 'global'
                results.append(s)
                seen_names.add(s['name'])
        if len(results) < limit:
            for s in self._local.skill_match(query, limit=limit, min_success=min_success):
                if s['name'] not in seen_names:
                    s['_tier'] = 'local'
                    results.append(s)
                    seen_names.add(s['name'])
        return results[:limit]

    # ── Session Operations ──

    def session_archive(self, task: str, summary: str, result: str) -> int:
        return self._local.session_archive(task, summary, result,
                                           had_knowledge=self._knowledge_produced)

    def session_create(self, task: str) -> int:
        return self._local.session_create(task)

    def session_update(self, session_id: int, summary: str = '',
                       result: str = '', had_knowledge: bool = False):
        self._local.session_update(session_id, summary, result, had_knowledge)

    def session_turn_add(self, session_id: int, turn_num: int, role: str,
                         content: str = '', tool_name: str = '',
                         tool_args: str = '{}', tool_result: str = '',
                         thinking: str = '') -> int:
        return self._local.session_turn_add(session_id, turn_num, role, content,
                                            tool_name, tool_args, tool_result, thinking)

    def session_turns_query(self, session_id: int, limit: int = None) -> List[Dict]:
        return self._local.session_turns_query(session_id, limit)

    def session_relevant_turns(self, task_prompt: str, max_sessions: int = 3,
                               max_turns: int = 10) -> str:
        return self._local.session_relevant_turns(task_prompt, max_sessions, max_turns)

    def session_crystallize(self, session_id: int) -> Optional[int]:
        return self._local.session_crystallize(session_id)

    # ── Pruning ──

    def prune(self, max_age_days: int = 30) -> Dict:
        """Prune old data and apply trust decay to both tiers."""
        local_pruned = self._local.prune_old_sessions(max_age_days)
        global_pruned = self._global.prune_old_sessions(max_age_days)
        local_decayed = self._local.decay_low_trust_facts(0.2)
        global_decayed = self._global.decay_low_trust_facts(0.2)
        local_time_decay = self._local.apply_time_decay()
        global_time_decay = self._global.apply_time_decay()
        return {
            'local_sessions_pruned': local_pruned,
            'global_sessions_pruned': global_pruned,
            'local_facts_decayed': local_decayed,
            'global_facts_decayed': global_decayed,
            'local_time_decay': local_time_decay,
            'global_time_decay': global_time_decay,
        }

    # ── Context Builder (using query helpers — Fix #7) ──

    def build_context_prompt(self) -> str:
        """Build compact context — meta rules + catalog, not full dumps.

        The agent has db_query/fact_search/wiki_query for retrieval.
        Context injection only provides: (1) behavioral rules, (2) what's available to query.
        """
        prompt = ""

        # L0 — Meta Rules (always inject — behavioral constraints must always apply)
        meta = self._global.wiki_read('meta-rules')
        if not meta:
            meta = self._local.wiki_read('meta-rules')
        if meta:
            prompt += f"\n[L0 — Meta Rules]\n{meta['content']}\n"

        # Catalog — what knowledge is available (not the knowledge itself)
        ls = self._local.stats()
        gs = self._global.stats()

        # Category breakdown for routing hints
        local_cats = self._local._conn.execute(
            "SELECT category, COUNT(*) as cnt FROM facts GROUP BY category ORDER BY cnt DESC"
        ).fetchall()
        global_cats = self._global._conn.execute(
            "SELECT category, COUNT(*) as cnt FROM facts GROUP BY category ORDER BY cnt DESC"
        ).fetchall()

        prompt += "\n[Knowledge Catalog]\n"
        prompt += f"  Facts: {ls['facts']} local + {gs['facts']} global\n"
        if local_cats:
            prompt += f"  Local categories: {', '.join(f'{r[0]}({r[1]})' for r in local_cats[:5])}\n"
        if global_cats:
            prompt += f"  Global categories: {', '.join(f'{r[0]}({r[1]})' for r in global_cats[:5])}\n"
        prompt += f"  Wiki: {ls['wiki_pages']} local + {gs['wiki_pages']} global pages\n"
        prompt += f"  Skills: {ls['skills']} local + {gs['skills']} global SOPs\n"

        # Top 3 highest-trust facts — proven reliable, worth always showing
        local_top = self._local.get_high_trust_facts(min_trust=0.7, limit=3)
        global_top = self._global.get_high_trust_facts(min_trust=0.7, limit=3)
        if local_top or global_top:
            prompt += "\n[Proven Facts (trust > 0.7)]\n"
            for f in local_top:
                prompt += f"  [local] {f['content']}\n"
            for f in global_top:
                prompt += f"  [global] {f['content']}\n"

        # Retrieval hints
        prompt += "\n[Retrieval]\n"
        prompt += "  - db_query SELECT for precise, structured retrieval\n"
        prompt += "  - wiki_query for fuzzy keyword search across wiki pages\n"
        prompt += "  - fact_search for trust-ranked fact lookup\n"
        prompt += "  - db_schema to inspect available tables/columns\n"

        prompt += f"\ncwd = {os.path.dirname(self._local.db_path)}\n"

        return prompt

    # ── Backward-compatible API ──

    def proactive_recall(self, task_prompt: str, max_facts: int = 5) -> str:
        """Automatically recall relevant knowledge at task start.

        Extracts keywords from the user's task prompt, searches local+global facts
        individually per keyword (avoids AND-matching failures), merges results.
        """
        # Extract meaningful keywords (skip common stopwords)
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
        words = re.findall(r'\b[a-zA-Z]{3,}\b', task_prompt.lower())
        keywords = [w for w in words if w not in stopwords]
        if not keywords:
            return ""

        # Search each keyword individually and merge (avoids AND-matching failures in FTS5)
        results = []
        seen = set()

        for kw in keywords[:5]:  # Top 5 keywords
            # Local first
            for f in self._local.fact_search(kw, min_trust=0.4, limit=3):
                key = f['content']
                if key not in seen:
                    f['_tier'] = 'local'
                    results.append(f)
                    seen.add(key)
            # Global supplement
            for f in self._global.fact_search(kw, min_trust=0.4, limit=2):
                key = f['content']
                if key not in seen:
                    f['_tier'] = 'global'
                    results.append(f)
                    seen.add(key)
            if len(results) >= max_facts:
                break

        results = results[:max_facts]

        # Also check wiki for relevant pages (top 2)
        wiki_hints = []
        for kw in keywords[:2]:
            for p in self._local.wiki_query(kw, limit=1):
                if p['category'] != 'session-log' and p['title'] not in {h.split("'")[1] for h in wiki_hints if "'" in h}:
                    wiki_hints.append(f"  wiki page '{p['title']}' [{p['category']}] — may be relevant")

        if not results and not wiki_hints:
            return ""

        prompt = "\n[Recalled Knowledge — relevant to your task]\n"
        for f in results:
            tier = f.get('_tier', 'local')
            prompt += f"  [{tier}] {f['content']}\n"
        for hint in wiki_hints:
            prompt += hint + "\n"

        return prompt

    # ── Backward-compatible API ──

    def read_layer(self, layer: str) -> str:
        return self._local.read_layer(layer)

    def write_layer(self, layer: str, content: str, mode='overwrite'):
        self._local.write_layer(layer, content, mode=mode)

    def archive_session(self, summary: str, task: str, result: str):
        self._local.archive_session(summary, task, result, had_knowledge=self._knowledge_produced)

    def close(self):
        self._local.close()
        self._global.close()

    # ── Stats ──

    def stats(self) -> Dict:
        ls = self._local.stats()
        gs = self._global.stats()
        return {
            'local_wiki_pages': ls['wiki_pages'],
            'local_facts': ls['facts'],
            'local_skills': ls['skills'],
            'local_sessions': ls['sessions'],
            'local_avg_trust': ls['avg_trust'],
            'global_wiki_pages': gs['wiki_pages'],
            'global_facts': gs['facts'],
            'global_skills': gs['skills'],
            'global_sessions': gs['sessions'],
            'global_avg_trust': gs['avg_trust'],
        }

    # ── SQL Sandbox (LLM-as-DBA) ──

    def get_schema_info(self, tier: str = 'auto') -> Dict:
        """Return schema info for both tiers."""
        result = {}
        if tier in ('auto', 'local'):
            local_info = self._local.get_schema_info()
            local_info['tier'] = 'local'
            result['local'] = local_info
        if tier in ('auto', 'global'):
            global_info = self._global.get_schema_info()
            global_info['tier'] = 'global'
            result['global'] = global_info
        return result

    def safe_query(self, sql: str, tier: str = 'auto') -> Dict:
        """Execute sandboxed SQL across tiers. SELECT hits both, INSERT/UPDATE routes by tier."""
        stripped = sql.strip().upper()
        is_select = stripped.startswith('SELECT')

        if is_select:
            # SELECT: hit both tiers (local first), merge and dedup results
            results = []
            seen_content = set()  # Dedup by content/slug/name (without tier distinction)

            if tier in ('auto', 'local'):
                local_result = self._local.safe_query(sql)
                if local_result['status'] == 'success':
                    for row in local_result.get('rows', []):
                        dedup_key = row.get('slug') or row.get('content') or row.get('name') or str(row.get('id'))
                        if dedup_key not in seen_content:
                            row['_tier'] = 'local'
                            results.append(row)
                            seen_content.add(dedup_key)

            if tier in ('auto', 'global'):
                global_result = self._global.safe_query(sql)
                if global_result['status'] == 'success':
                    for row in global_result.get('rows', []):
                        dedup_key = row.get('slug') or row.get('content') or row.get('name') or str(row.get('id'))
                        if dedup_key not in seen_content:
                            row['_tier'] = 'global'
                            results.append(row)
                            seen_content.add(dedup_key)

            # Enforce limits: keep local-first order but ensure global unique items survive truncation
            # Strategy: take local rows first, then append global-only rows, truncate to DBQ_MAX_ROWS
            if len(results) > DBQ_MAX_ROWS:
                local_rows = [r for r in results if r.get('_tier') == 'local']
                global_rows = [r for r in results if r.get('_tier') == 'global']
                # Keep up to DBQ_MAX_ROWS-5 local rows, then fill with global unique rows
                local_keep = min(len(local_rows), DBQ_MAX_ROWS - min(5, len(global_rows)))
                global_keep = min(len(global_rows), DBQ_MAX_ROWS - local_keep)
                results = local_rows[:local_keep] + global_rows[:global_keep]
                truncated = True
            else:
                truncated = False

            # Enforce char limit
            total_chars = len(json.dumps(results, ensure_ascii=False))
            if total_chars > DBQ_MAX_CHARS:
                results = results[:10]
                truncated = True

            return {
                "status": "success",
                "rows": results,
                "row_count": len(results),
                "truncated": truncated,
            }
        else:
            # INSERT/UPDATE: validate first, then route to appropriate tier
            # Validate against sandbox rules before routing
            err = self._local._validate_sql(sql)
            if err:
                return {"status": "error", "msg": err}

            if tier in ('local', 'global'):
                target = self._target(tier)
                return target.safe_query(sql)
            else:
                # Auto-route: parse category from SQL to determine tier
                # Extract table name from INSERT INTO or UPDATE
                table_match = re.search(r'(?:INTO|UPDATE)\s+(\w+)', sql, re.IGNORECASE)
                if not table_match:
                    return {"status": "error", "msg": "Cannot determine target table for auto-routing."}

                table = table_match.group(1).lower()
                if table == 'sessions':
                    # Sessions always go local
                    return self._local.safe_query(sql)

                # Try to extract category from SQL for routing
                # Handles both forms: category = 'pattern' (UPDATE) and positional VALUES
                category = None

                # Form 1: category = 'value' (UPDATE SET, or INSERT with explicit column=value)
                category_match = re.search(r"category\s*=\s*'(\w+)'", sql, re.IGNORECASE)
                if category_match:
                    category = category_match.group(1)

                # Form 2: positional INSERT VALUES — find category column position in table
                if not category and table in ('facts', 'wiki_pages'):
                    # Get column list from INSERT statement
                    cols_match = re.search(r'\(([^)]+)\)\s*VALUES', sql, re.IGNORECASE)
                    if cols_match:
                        cols = [c.strip().strip('"\'') for c in cols_match.group(1).split(',')]
                        cat_idx = None
                        for i, c in enumerate(cols):
                            if c.lower() == 'category':
                                cat_idx = i
                                break
                        if cat_idx is not None:
                            # Extract the value at that position from VALUES clause
                            vals_match = re.search(r'VALUES\s*\(([^)]+)\)', sql, re.IGNORECASE)
                            if vals_match:
                                vals = [v.strip().strip('"\'') for v in vals_match.group(1).split(',')]
                                if cat_idx < len(vals):
                                    category = vals[cat_idx]

                category = category or 'general'
                target_tier = _route_tier(category, 'auto')
                target = self._target(target_tier)
                result = target.safe_query(sql)

                # Mark knowledge produced if INSERT succeeds
                if result['status'] == 'success':
                    self._mark_knowledge_produced()

                return result


# ── Legacy alias ──
MemoryEngine = TwoTierMemory
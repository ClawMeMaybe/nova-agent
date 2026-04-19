<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-04-17 | Updated: 2026-04-19 -->

# memory

## Purpose
Unified SQLite+FTS5 memory engine with project_id scoping, trust evolution, knowledge links, cluster search, and wiki compounding. The core knowledge persistence layer — every session crystallizes into wiki pages and verified facts that compound over time.

## Key Files

| File | Description |
|------|-------------|
| `__init__.py` | Package init (empty) |
| `engine.py` | NovaMemory — single DB with project_id, all CRUD, FTS5 search, trust evolution, SQL sandbox, context builder, proactive recall, links, clusters, feedback events, evolution loss, project management, promotion |

## For AI Agents

### Working In This Directory
- This is the most complex module (~1800 lines) — changes here affect all agent operations
- NovaMemory handles a single SQLite DB; project_id columns scope reads/writes
- `_write_locked` decorator acquires reentrant lock for all write operations (thread-safe)
- No tier routing — project_id scoping replaces the old local/global split
- Schema is versioned via `_meta` table — `SCHEMA_V1` creates full schema, `SCHEMA_V8` migration adds project_id to existing DBs
- FTS5 triggers auto-sync on INSERT/UPDATE/DELETE for wiki_pages, facts, skills
- `current_project_id` property — None = global scope, UUID = project scope

### Scoping Logic
- `_scope_where(prefix)` — returns `"({prefix}project_id IS NULL OR {prefix}project_id = ?)"` when project selected, `"({prefix}project_id IS NULL)"` when global
- `_scope_params()` — returns `[self.current_project_id]` when project selected, `[]` when global
- `_scope_write_id()` — returns `self.current_project_id` (may be None for global writes)
- Reads: `WHERE project_id IS NULL OR project_id = ?` — both global + project visible
- Writes: use `current_project_id` (NULL = global, UUID = project)
- Evolution: operates on ALL data regardless of project_id (no scoping)

### Project Management
- `project_create(name, description)` → creates project, returns UUID
- `project_select(project_id)` → sets `self.current_project_id`
- `project_list()` → returns all projects with knowledge counts
- `project_info(project_id)` → project details + stats
- `project_delete(project_id)` → deletes project + all scoped data

### Promotion
- `fact_promote(fact_id)` → `UPDATE facts SET project_id = NULL`
- `skill_promote(skill_name)` → `UPDATE skills SET project_id = NULL`
- `wiki_promote(slug)` → `UPDATE wiki_pages SET project_id = NULL`

### Testing Requirements
- Use `conftest.py` fixtures for temp DBs — never write to real `~/.nova/nova.db`
- `memory` fixture creates NovaMemory at temp db
- `memory_with_project` fixture creates + selects a project
- Trust evolution tests must verify: helpful +0.05, unhelpful -0.10, retrieval +0.01, decay rates
- SQL sandbox tests must verify blocked ops: DELETE, DROP, ALTER, PRAGMA, REPLACE
- Feedback event tests verify cascade flags on linked items
- Project scoping tests verify reads see global + project, writes use project_id

### Common Patterns
- `_fts_escape()` — quotes each term separately for OR-style FTS5 matching
- `_content_is_duplicate()` — 80% token overlap threshold for dedup, skip if new content >200 chars longer
- `wiki_ingest()` — append-only strategy (never replaces existing content)
- `build_context_prompt()` — compact injection: meta rules + catalog + proven facts + proactive recall
- `proactive_recall()` — keyword extraction, per-keyword FTS5 search, scoped queries, injects top facts
- `cluster_search()` — tag overlap grouping across facts + skills + wiki pages

### Schema

| Table | Purpose |
|-------|---------|
| `projects` | Named project scopes (id UUID, name, description) |
| `wiki_pages` | Rich knowledge pages (markdown, tags, categories, confidence, cross-refs) |
| `facts` | Atomic verified facts with trust scores (0.0-1.0), asymmetric feedback, needs_review |
| `skills` | Crystallized SOPs with success rates, usage counts, needs_review |
| `sessions` | Task archives with auto-crystallization to wiki |
| `session_turns` | Per-turn records with tool usage and feedback |
| `knowledge_links` | Cross-type connections (source_type→target_type, link_type enum) |
| `feedback_events` | Per-turn helpful/unhelpful feedback with reason, updates trust + success rate |
| `evolution_log` | Evolution score trajectory with loss components |
| `_meta` | Schema version tracking |
| `*_fts` (virtual) | FTS5 search indexes on wiki, facts, skills |

All content tables have `project_id TEXT DEFAULT NULL` column for scoping.

## Dependencies

### Internal
- Used by `nova.tools.handler.NovaHandler` for all wiki/fact/skill/link/cluster/project operations
- Used by `nova.context.system_prompt` for context injection and stats
- Used by `nova.main.NovaAgent` for proactive recall, stats, project management

### External
- `sqlite3` — Core storage with WAL mode, FTS5, and row_factory
- `threading` — RLock for thread-safe write operations
- `json` — Skill steps serialization

<!-- MANUAL: Custom project notes can be added below -->
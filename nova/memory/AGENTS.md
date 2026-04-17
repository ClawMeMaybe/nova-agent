<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-04-17 | Updated: 2026-04-17 -->

# memory

## Purpose
Two-tier SQLite+FTS5 memory engine with trust evolution and wiki compounding. The core knowledge persistence layer — every session crystallizes into wiki pages and verified facts that compound over time.

## Key Files

| File | Description |
|------|-------------|
| `__init__.py` | Package init (empty) |
| `engine.py` | NovaMemory (single DB) + TwoTierMemory (local+global) — all CRUD, FTS5 search, trust evolution, SQL sandbox, context builder, proactive recall |

## For AI Agents

### Working In This Directory
- This is the most complex module (~1500 lines) — changes here affect all agent operations
- NovaMemory handles a single SQLite DB; TwoTierMemory coordinates local+global instances
- `_write_locked` decorator acquires reentrant lock for all write operations (thread-safe)
- `_route_tier()` auto-routes categories: environment/debugging/session-log→local, pattern/convention/decision→global
- Schema is versioned via `_meta` table — `SCHEMA_V1` is current, migrations go through `_init_schema`
- FTS5 triggers auto-sync on INSERT/UPDATE/DELETE for wiki_pages, facts, skills

### Testing Requirements
- Use `conftest.py` fixtures for temp DBs — never write to real `~/.nova/nova.db`
- Test both tiers independently AND via TwoTierMemory coordination
- Trust evolution tests must verify: helpful +0.05, unhelpful -0.10, retrieval +0.01, decay rates
- SQL sandbox tests must verify blocked ops: DELETE, DROP, ALTER, PRAGMA, REPLACE

### Common Patterns
- `_fts_escape()` — quotes each term separately for OR-style FTS5 matching
- `_content_is_duplicate()` — 80% token overlap threshold for dedup, skip if new content >200 chars longer
- `wiki_ingest()` — append-only strategy (never replaces existing content)
- `build_context_prompt()` — compact injection: meta rules + catalog + proven facts, NOT full dumps (~3000 char budget)
- `proactive_recall()` — keyword extraction, per-keyword FTS5 search, local-first merge

### Schema

| Table | Purpose |
|-------|---------|
| `wiki_pages` | Rich knowledge pages (markdown, tags, categories, confidence, cross-refs) |
| `facts` | Atomic verified facts with trust scores (0.0-1.0), asymmetric feedback |
| `skills` | Crystallized SOPs with success rates and usage counts |
| `sessions` | Task archives with auto-crystallization to wiki |
| `_meta` | Schema version tracking |
| `*_fts` (virtual) | FTS5 search indexes on wiki, facts, skills |

## Dependencies

### Internal
- Used by `nova.tools.handler.NovaHandler` for all wiki/fact/SQL operations
- Used by `nova.context.system_prompt` for context injection
- Used by `nova.main.NovaAgent` for proactive recall and stats

### External
- `sqlite3` — Core storage with WAL mode, FTS5, and row_factory
- `threading` — RLock for thread-safe write operations
- `json` — Skill steps serialization

<!-- MANUAL: Custom project notes can be added below -->
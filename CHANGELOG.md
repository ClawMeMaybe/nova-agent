# Changelog

All notable changes to Nova Agent will be documented in this file.

## [0.2.0] - 2026-04-19

### Added
- **Unified memory**: single SQLite DB (`~/.nova/nova.db`) with project_id columns replacing two-tier architecture
- **Project scoping**: named projects with explicit create/select/list/info/delete — reads see global+project, writes go to current project scope
- **Knowledge promotion**: fact_promote, skill_promote, wiki_promote — promote project-scoped knowledge to global
- **Knowledge links**: link_add, link_search — cross-type connections (fact→skill, skill→wiki, fact→fact) with depends_on, related_to, contradicts
- **Cluster search**: tag-based knowledge bundles spanning facts + skills + wiki pages
- **Per-turn feedback**: fact_feedback, skill_feedback — helpful/unhelpful with reason, updates trust and success rates
- **Cascade flags**: unhelpful feedback on linked items sets needs_review=1 on related skills/facts
- **Feedback events**: feedback_events table tracks per-turn feedback, used by evolution loss for knowledge quality
- **31 tools**: added project_create, project_select, project_list, project_info, fact_promote, skill_promote, wiki_promote, fact_feedback, skill_feedback, skill_add, skill_search, skill_feedback, link_add, link_search, cluster_search
- **SCHEMA_V8 migration**: ALTER TABLE adds project_id columns to existing DBs

### Removed
- **TwoTierMemory class**: deleted ~715 lines of routing overhead (local+global coordination, ID collision bugs, inconsistent merge order)
- **Tier routing**: removed `_route_tier()`, LOCAL_CATEGORIES/GLOBAL_CATEGORIES, all `tier='auto'/'local'/'global'` parameters
- **Category routing**: no longer auto-routes environment→local, pattern→global — project_id scoping replaces this

### Changed
- **Stats format**: unified (total_facts, global_facts, project_facts, avg_trust, evolution_score) instead of local/global split
- **Evolution**: operates on ALL data regardless of project_id (was local-only in two-tier)
- **Proactive recall**: added to NovaMemory with FTS5 keyword search + project_id scoping
- **System prompt**: shows unified stats + project context when project is selected

## [0.1.0] - 2026-04-17

### Added
- **Two-tier memory**: project-local + global SQLite databases with FTS5 full-text search
- **17 tools**: code_run, file_read, file_write, file_patch, web_scan, web_execute_js, ask_user, update_working_checkpoint, start_long_term_update, wiki_ingest, wiki_query, fact_add, fact_search, db_query, db_schema, wiki_export, cron
- **Cron system**: LLM-manageable scheduled jobs with three schedule types (one-shot, interval, cron expression), grace window fast-forward, output logging, file-based locking
- **Autonomous self-improvement**: idle detection (>30min), value formula task planning, autonomous-todo wiki page, 30-turn execution limit
- **Trust evolution**: asymmetric feedback (+0.05 helpful, -0.10 unhelpful), time-based decay with category-specific rates, retrieval count auto-bump, auto-delete below 0.15
- **Proactive recall**: keywords auto-extracted from task prompt, relevant knowledge injected before each task
- **Wiki compounding**: append-only knowledge pages that grow across sessions, cross-references, dedup (80% overlap threshold)
- **SQL sandbox**: safe_query blocks DELETE/DROP/ALTER, whitelisted tables, 50-row result cap
- **Thread safety**: threading.Event for is_running, RLock for DB write operations, locked handler assignment
- **CLI commands**: /quit, /stats, /wiki, /cron, /todo
- **7 gateway channels**: CLI, Telegram, Discord, WeChat, Feishu, QQ, DingTalk, Web
- **LLM abstraction**: Anthropic, OpenAI, OpenRouter support
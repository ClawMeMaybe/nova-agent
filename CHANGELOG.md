# Changelog

All notable changes to Nova Agent will be documented in this file.

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
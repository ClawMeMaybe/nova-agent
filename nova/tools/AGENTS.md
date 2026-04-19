<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-04-17 | Updated: 2026-04-19 -->

# tools

## Purpose
NovaHandler — implements all 31 agent tools via the `do_<tool_name>` dispatch pattern. 9 atomic tools (code, files, web, ask_user) + 2 memory tools (checkpoint, long-term update) + 7 wiki/fact/skill tools + 3 link/cluster tools + 4 project/promotion tools + 1 SQL sandbox + 1 cron tool + 1 meta tool + 3 feedback tools.

## Key Files

| File | Description |
|------|-------------|
| `__init__.py` | Package init (empty) |
| `handler.py` | NovaHandler class + standalone helper functions (code_run, file_read, file_write, file_patch, ask_user, smart_format, format_error, get_global_memory) |

## For AI Agents

### Working In This Directory
- Every tool method must return `StepOutcome(data, next_prompt, should_exit)`
- Tool dispatch: `BaseHandler.dispatch(tool_name, args, response)` calls `do_<tool_name>`
- Helper functions (`code_run`, `file_read`, etc.) are standalone — used outside handler too
- `self.code_stop_signal` list enables abort from UI — checked in code_run timeout loop
- `self._accessed_fact_ids` tracks facts used per task — trust feedback on task completion
- `self._accessed_skill_names` tracks skills used per task — for feedback
- `self._get_anchor_prompt()` injects working memory context (history + key_info) into next_prompt
- No tier parameters — all tools use unified NovaMemory with project_id scoping

### Tool Categories

**Atomic (9):** code_run, file_read, file_write, file_patch, web_scan, web_execute_js, ask_user, update_working_checkpoint, start_long_term_update

**Wiki/Fact/Skill (7):** wiki_ingest, wiki_query, wiki_export, fact_add, fact_search, skill_add, skill_search

**Feedback (3):** fact_feedback, skill_feedback, (built into handler via _knowledge_produced)

**Link/Cluster (3):** link_add, link_search, cluster_search

**Project/Promotion (4):** project_create, project_select, project_list, project_info, fact_promote, skill_promote, wiki_promote

**SQL (2):** db_query, db_schema

**Cron (1):** cron

### Testing Requirements
- Test each `do_*` method with mock args/response
- code_run tests: timeout behavior, stop signal, bash vs python
- file_patch tests: uniqueness requirement, not-found errors

### Common Patterns
- `_get_abs_path()` resolves relative paths against `self.cwd`
- `smart_format()` truncates large output with middle ellipsis
- `args.get('_index', 0) > 0` — skip anchor prompt for parallel tool calls (only first gets context)
- `turn_end_callback()` — auto-crystallization nudge, trust feedback, danger warnings every 7 turns
- `_resolve_link_name()` — single-DB lookup (no dual-tier iteration)

## Dependencies

### Internal
- `nova.agent_loop` — BaseHandler, StepOutcome
- `nova.memory.engine` — NovaMemory for wiki/fact/skill/link/cluster/project operations
- `nova.cron.jobs` — create_job, list_jobs, remove_job
- `nova.cron.scheduler` — tick function for cron run action

### External
- `subprocess` — code execution
- `threading` — stdout streaming from subprocess

<!-- MANUAL: Custom project notes can be added below -->
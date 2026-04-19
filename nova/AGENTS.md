<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-04-17 | Updated: 2026-04-19 -->

# nova

## Purpose
Core agent package — contains the ~100-line agent loop, LLM client abstraction, tool handler (31 tools), unified memory engine with project scoping, system prompt builder, autonomous monitor, cron scheduler, and 7 messaging gateway channels.

## Key Files

| File | Description |
|------|-------------|
| `__init__.py` | Package init, exports `__version__` |
| `main.py` | NovaAgent orchestrator — task queue, interactive CLI, unified memory + project management |
| `agent_loop.py` | Core loop — perceive→reason→execute→remember with full conversation history |
| `llmcore.py` | LLM client abstraction — Anthropic, OpenAI, OpenRouter sessions with normalized responses |
| `autonomous.py` | AutonomousMonitor — idle >30min triggers self-improvement tasks |
| `scheduler.py` | Legacy natural-language schedule parser (standalone CLI, separate from cron/) |

## Subdirectories

| Directory | Purpose |
|-----------|---------|
| `tools/` | 31 tools: atomic, memory, wiki/fact/skill, link/cluster, project/promotion, cron (see `tools/AGENTS.md`) |
| `memory/` | Unified SQLite+FTS5 memory engine with project_id scoping (see `memory/AGENTS.md`) |
| `context/` | Dynamic system prompt builder (see `context/AGENTS.md`) |
| `cron/` | Cron scheduler — jobs.py (CRUD), scheduler.py (tick+lock) (see `cron/AGENTS.md`) |
| `gateway/` | 7 messaging channels — Telegram, Discord, WeChat, Feishu, QQ, DingTalk, Web (see `gateway/AGENTS.md`) |
| `assets/` | Static resources — tool schema JSON (31 tool definitions) |
| `temp/` | Runtime temp directory for agent output files |

## For AI Agents

### Working In This Directory
- The core loop in `agent_loop.py` must stay minimal (~100 lines) — this is a design principle
- All tool dispatch uses the `do_<tool_name>` pattern via `BaseHandler.dispatch()`
- NovaAgent is thread-safe: uses `threading.Lock` for handler access, `threading.Event` for busy state
- Task queue is `queue.Queue` — producers call `put_task()`, consumer runs in `agent.run()`
- Unified memory: single DB at `~/.nova/nova.db`, `NovaMemory` class, project_id scoping

### Testing Requirements
- LLMClient tests should mock API calls — no real API keys in test env
- Memory tests use real SQLite with temp directories (see `tests/conftest.py`)
- Agent loop tests mock handler dispatch

### Common Patterns
- `StepOutcome(data, next_prompt, should_exit)` drives the loop — every tool must return one
- `create_client_from_config()` reads env vars to pick LLM provider: Anthropic > OpenAI > OpenRouter
- Unified memory: `NovaMemory(os.path.join(HOME_DIR, '.nova', 'nova.db'))` — no two-tier split

## Dependencies

### Internal
- `nova.tools.handler` — NovaHandler implements all 31 tools
- `nova.memory.engine` — NovaMemory for unified knowledge persistence with project_id scoping
- `nova.context.system_prompt` — Builds dynamic system prompt
- `nova.cron` — NovaCron background scheduler
- `nova.autonomous` — AutonomousMonitor idle self-improvement

### External
- `anthropic` — Claude API (default)
- `openai` — GPT/OpenRouter API (alternative)

<!-- MANUAL: Custom project notes can be added below -->
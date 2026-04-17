<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-04-17 | Updated: 2026-04-17 -->

# tests

## Purpose
pytest test suite for Nova Agent — covers memory engine, cron jobs, agent loop, LLM core, tools handler, and system prompt builder.

## Key Files

| File | Description |
|------|-------------|
| `conftest.py` | Shared fixtures — temp project dirs, local/global DB paths, memory instances |
| `test_memory_engine.py` | Tests for NovaMemory and TwoTierMemory (CRUD, FTS5, trust, decay, dedup) |
| `test_cron_jobs.py` | Tests for cron job CRUD, schedule parsing, grace windows |
| `test_autonomous.py` | Tests for AutonomousMonitor idle detection and prompt building |
| `test_agent_loop.py` | Tests for agent_runner_loop, BaseHandler dispatch, StepOutcome |
| `test_llmcore.py` | Tests for LLMClient, AnthropicSession, OpenAISession (mocked) |
| `test_tools_handler.py` | Tests for NovaHandler tool implementations (code_run, file ops, wiki/fact) |
| `test_system_prompt.py` | Tests for build_system_prompt context injection |

## For AI Agents

### Working In This Directory
- Run with `pytest tests/` or `pytest --cov=nova tests/`
- All DB tests use temp directories via `conftest.py` fixtures — never touch real `~/.nova/`
- Memory fixtures auto-close on teardown to avoid SQLite lock issues
- LLM tests mock API responses — no real API calls

### Testing Requirements
- Every new feature needs a corresponding test file
- Memory tests must verify both local and global tiers
- Trust evolution tests should check decay rates per category

### Common Patterns
- `@pytest.fixture` for setup/teardown — temp DBs, memory instances
- `smart_format` truncation testing with known-length strings
- SQL sandbox tests verify blocked operations (DELETE, DROP, etc.)

<!-- MANUAL: Custom project notes can be added below -->
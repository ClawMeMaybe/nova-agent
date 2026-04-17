<!-- Generated: 2026-04-17 | Updated: 2026-04-17 -->

# nova-agent

## Purpose
Self-evolving AI agent with compounding knowledge — minimal loop, atomic tools, two-tier SQL+Wiki memory. Synthesizes the best ideas from Hermes (trust scoring), GenericAgent (~100-line loop), and OpenClaw (multi-platform messaging), plus Karpathy's LLM Wiki concept for persistent knowledge that compounds across sessions.

## Key Files

| File | Description |
|------|-------------|
| `pyproject.toml` | Project config, dependencies, entry points for all 7 channels |
| `README.md` | Full documentation — architecture, memory system, tools, channels |
| `.env.example` | API keys and channel config template |
| `requirements.txt` | Pip dependencies |
| `LICENSE` | MIT license |

## Subdirectories

| Directory | Purpose |
|-----------|---------|
| `nova/` | Core agent source code (see `nova/AGENTS.md`) |
| `tests/` | pytest test suite (see `tests/AGENTS.md`) |
| `.github/workflows/` | CI pipeline (test.yml) |

## For AI Agents

### Working In This Directory
- Install with `pip install -e ".[dev]"` for development (includes pytest)
- Set `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, or `OPENROUTER_API_KEY` before running
- Run tests with `pytest tests/` or `pytest --cov=nova tests/`
- The `nova` CLI entry point is `nova.main:main`

### Testing Requirements
- All tests use pytest with temp directories for DB isolation
- Test fixtures in `tests/conftest.py` provide temp DB paths and memory instances
- LLM tests mock the client; memory tests use real SQLite

### Common Patterns
- Entry points defined in pyproject.toml `[project.scripts]` — each channel has its own CLI
- Optional dependencies grouped by channel: `[telegram]`, `[discord]`, `[wechat]`, `[feishu]`, `[qq]`, `[dingtalk]`, `[web]`, `[gateway]`, `[all]`

## Dependencies

### External
- `anthropic>=0.40.0` — Claude API client (default LLM provider)
- `openai>=1.30.0` — OpenAI/GPT API client (alternative provider)
- `croniter>=2.0` — Cron expression parsing for scheduled jobs
- `pytest>=7.0` — Testing framework (dev)
- `pytest-cov>=4.0` — Coverage reporting (dev)

<!-- MANUAL: Custom project notes can be added below -->
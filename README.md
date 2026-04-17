# Nova Agent

> Self-evolving AI agent with compounding knowledge — minimal loop, atomic tools, two-tier SQL+Wiki memory.

The longer you use Nova, the smarter it gets. Every session crystallizes knowledge into a persistent wiki, facts accumulate with trust scores, and scheduled tasks maintain your memory automatically. When you step away, Nova plans its own improvement tasks.

## What Makes Nova Different

Nova synthesizes the best ideas from three agent architectures:
- **Hermes Agent** — self-improving loop, skills from experience, holographic memory with trust scoring
- **GenericAgent** — ~100-line core loop, 9 atomic tools, layered memory, autonomous operation
- **OpenClaw** — multi-platform messaging, cron scheduling, persona system

Plus **Karpathy's LLM Wiki concept**: persistent knowledge that compounds across sessions.

## Features at a Glance

- **~100-line core loop** — perceive → reason → execute → remember
- **17 tools** — code, files, web, memory, wiki, facts, SQL, cron
- **Two-tier memory** — project-local + global SQLite with FTS5 search
- **Trust evolution** — facts gain/lose trust through asymmetric feedback (+0.05/-0.10)
- **Proactive recall** — relevant knowledge auto-injected before each task
- **Wiki compounding** — append-only knowledge pages that grow across sessions
- **Cron scheduling** — LLM-manageable recurring tasks with grace windows
- **Autonomous self-improvement** — idle >30min triggers auto-task planning
- **7 channels** — CLI, Telegram, Discord, WeChat, Feishu, QQ, DingTalk, Web

## Architecture

```
nova/
  agent_loop.py        — Core loop (perceive → reason → execute → remember)
  llmcore.py           — LLM client (Anthropic, OpenAI, OpenRouter)
  main.py              — Agent orchestrator + CLI
  autonomous.py        — Idle self-improvement monitor
  tools/
    handler.py         — 17 atomic + memory + wiki/fact + cron tools
  memory/
    engine.py          — Two-tier SQLite+FTS5 with trust evolution
  context/
    system_prompt.py   — Dynamic prompt builder (meta rules + catalog)
  cron/
    jobs.py            — Job storage, schedule parsing, CRUD
    scheduler.py       — Tick-based execution with file locking
  gateway/
    telegram.py        — Telegram bot channel
    discord.py         — Discord bot channel
    wechat.py          — WeChat channel
    feishu.py           — Feishu/Lark channel
    qq.py              — QQ bot channel
    dingtalk.py         — DingTalk channel
    webhook.py         — HTTP webhook + web UI
  assets/
    tools_schema.json  — Tool definitions (17 tools)
```

## Quick Start

```bash
# Install
pip install -e .

# Install with dev tools
pip install -e ".[dev]"

# Install with all messaging channels
pip install -e ".[all]"

# Set API key (copy from .env.example)
export ANTHROPIC_API_KEY=your-key-here

# Run
nova
```

## Memory System: Two-Tier SQL + Wiki Compounding

Nova's memory is SQLite-backed with two tiers:

| Tier | Location | Purpose |
|------|----------|---------|
| **Local** | `<project>/.nova/nova.db` | Project-specific: paths, configs, debugging notes |
| **Global** | `~/.nova/nova.db` | Cross-project: patterns, conventions, decisions, skills |

Both tiers share the same schema:

```
wiki_pages   — Rich knowledge pages (markdown, tags, categories)
facts        — Verified facts with trust scores (0.0-1.0)
skills       — Crystallized SOPs with success rates
sessions     — Task archives with auto-crystallization
FTS5 indexes — Fast keyword + tag search on all tables
```

Category routing is automatic:
- `environment/debugging/session-log` → local tier
- `pattern/convention/decision` → global tier

### Trust Evolution

Facts evolve trust over time through asymmetric feedback:
- **Helpful** → +0.05 (gentle positive — proven knowledge strengthens)
- **Unhelpful** → -0.10 (strong negative — bad knowledge fades fast)
- **Retrieval** → +0.01 (auto bump when a fact is searched)
- **Time decay** → environment facts 6%/month, patterns 1%/month, general 3%/month
- **Frequently used** (retrieval_count ≥ 5) resists decay
- **Auto-delete** below trust 0.15

This asymmetry prevents low-quality facts from accumulating while proven knowledge compounds.

### Proactive Recall

Before each task, Nova extracts keywords and searches both tiers for relevant facts and wiki pages. Matching knowledge is injected into the task prompt automatically — you don't need to explicitly search before starting.

### Wiki Compounding

Every completed session can crystallize into a wiki page. Knowledge compounds:
```
[Task] → [Crystallize into Wiki] → [Available next session] → [More knowledge] → ...
```

Wiki pages support categories, tags, confidence levels, cross-references (`[[page-name]]`), and deduplication (80% overlap threshold prevents redundant entries).

### SQL Sandbox

The `db_query` tool gives the agent direct SQL access to its own memory:
- **Allowed**: SELECT, INSERT, UPDATE
- **Blocked**: DELETE, DROP, ALTER, CREATE, PRAGMA
- **Whitelisted tables**: wiki_pages, facts, skills, sessions
- **Max 50 rows** per query

## Cron System

Nova can schedule recurring tasks that run automatically — the agent manages its own maintenance:

```
# Via CLI tool
cron(action='create', schedule='every 24h', prompt='Run daily maintenance...')
cron(action='create', schedule='30m', prompt='Quick check in 30 minutes')
cron(action='create', schedule='0 9 * * *', prompt='Morning review')

# Via /cron command in CLI
/cron    — List all scheduled jobs
/todo    — Show autonomous TODO list
```

Three schedule types:
- **One-shot** (`"30m"`, `"2h"`, `"1d"`) — runs once, then disables
- **Interval** (`"every 30m"`, `"every 2h"`) — recurring with fixed interval
- **Cron** (`"0 9 * * *"`) — standard cron expressions (requires `croniter`)

Features:
- **Grace windows** — stale missed jobs fast-forward instead of burst-firing on restart
- **Output logging** — every run saves output to `~/.nova/cron/output/{job_id}/`
- **File locking** — prevents concurrent tick execution
- **Seed defaults** — daily maintenance + weekly knowledge review auto-created on first run

## Autonomous Self-Improvement

When you're idle for >30 minutes, Nova enters autonomous mode:

```
[Idle >30min] → [Check autonomous-todo wiki page]
  ├── Has TODO → Pick one item → Execute (max 30 turns) → Crystallize
  └── No TODO → Plan mode → Review memory → Write TODO → Execute one → Crystallize
```

The value formula prioritizes tasks where "AI training data can't cover" × "lasting benefit" is highest. Priority order: memory review → environment discovery → skill refinement → knowledge audit.

Autonomous mode never calls `ask_user` — it solves independently and crystallizes learnings before finishing.

## Tools Reference

| Tool | Description |
|------|-------------|
| `code_run` | Execute Python or Bash code |
| `file_read` | Read files with line numbers and keyword search |
| `file_write` | Write/create files (overwrite/append) |
| `file_patch` | Precisely edit by replacing unique blocks |
| `web_scan` | Scan web page content |
| `web_execute_js` | Execute JavaScript in browser |
| `ask_user` | Ask user for clarification/confirmation |
| `update_working_checkpoint` | Set working memory focus |
| `start_long_term_update` | Distill experience into long-term memory |
| `wiki_ingest` | Add knowledge to compounding wiki |
| `wiki_query` | Search wiki pages by keywords/tags |
| `fact_add` | Add verified fact with trust scoring |
| `fact_search` | Search trust-ranked facts |
| `db_query` | Execute SQL against knowledge database |
| `db_schema` | Inspect database schema |
| `wiki_export` | Export wiki to markdown files |
| `cron` | Manage scheduled recurring tasks |

## CLI Commands

| Command | Description |
|---------|-------------|
| `/quit` | Exit the agent |
| `/stats` | Show memory statistics (wiki, facts, skills, trust) |
| `/wiki` | List all wiki pages |
| `/cron` | List all scheduled cron jobs |
| `/todo` | Show autonomous TODO list |

## Channels

All channels share the same agent instance and memory. Switch between them freely.

| Channel | Command | Config Variables |
|---------|---------|------------------|
| **CLI** | `nova` | `ANTHROPIC_API_KEY` |
| **Telegram** | `nova-tg` | `TG_BOT_TOKEN`, `TG_ALLOWED_USERS` |
| **Discord** | `nova-discord` | `DISCORD_BOT_TOKEN`, `DISCORD_ALLOWED_CHANNELS` |
| **WeChat** | `nova-wechat` | `WECHAT_ALLOWED_USERS` (scan QR on first run) |
| **Feishu** | `nova-feishu` | `FS_APP_ID`, `FS_APP_SECRET`, `FS_ALLOWED_USERS` |
| **QQ** | `nova-qq` | `QQ_APP_ID`, `QQ_APP_SECRET`, `QQ_ALLOWED_USERS` |
| **DingTalk** | `nova-dingtalk` | `DINGTALK_CLIENT_ID`, `DINGTALK_CLIENT_SECRET` |
| **Web** | `nova-web` | `NOVA_WEB_PORT`, `NOVA_WEB_TOKENS` |

## Configuration

Copy `.env.example` to `.env` and fill in your API keys:

```bash
cp .env.example .env
# Edit .env with your API key
```

Set at least one LLM API key. Nova supports three providers:
- `ANTHROPIC_API_KEY` — Claude models (default: claude-sonnet-4-20250514)
- `OPENAI_API_KEY` — GPT models (default: gpt-4o)
- `OPENROUTER_API_KEY` — Any model via OpenRouter

## Comparison with Source Projects

| Feature | Hermes | GenericAgent | OpenClaw | Nova |
|---------|--------|--------------|----------|------|
| Core loop size | ~100 lines | ~100 lines | Large | ~100 lines |
| Memory | Holographic (trust, asymmetric) | L0-L4 layers | SOUL.md persona | Two-tier SQL+Wiki (both) |
| Self-improvement | Skills from experience | Idle autonomous mode | Cron scheduling | Autonomous + Cron (both) |
| Trust scoring | Yes (asymmetric) | No | No | Yes (asymmetric + decay) |
| Proactive recall | No | No | No | Yes (keyword injection) |
| LLM-manageable cron | Yes | No | No | Yes |
| Grace windows | Yes | No | No | Yes |
| SQL sandbox | No | No | No | Yes (blocked ops) |
| Channels | Telegram only | CLI only | 6 platforms | 7 platforms |

## Development

```bash
# Install with dev tools
pip install -e ".[dev]"

# Run tests
pytest tests/

# Run with coverage
pytest --cov=nova tests/
```

## License

MIT — see [LICENSE](LICENSE) file.
<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-04-17 | Updated: 2026-04-17 -->

# gateway

## Purpose
Multi-platform messaging channels — 7 adapters that connect users to the shared NovaAgent via Telegram, Discord, WeChat, Feishu, QQ, DingTalk, and HTTP webhook. Each runs as an independent process.

## Key Files

| File | Description |
|------|-------------|
| `__init__.py` | Shared utilities — `clean_reply()`, `build_done_text()`, `extract_files()`, `HELP_TEXT`, `ensure_single_instance()`, `create_agent()` |
| `telegram.py` | Telegram bot channel — streaming updates, HTML formatting, user auth |
| `discord.py` | Discord bot channel — command handlers, file sending |
| `wechat.py` | WeChat personal account channel — QR login, sync reply |
| `feishu.py` | Feishu/Lark channel — WebSocket long connection, file upload |
| `qq.py` | QQ bot channel — WebSocket via qq-botpy, group+direct messages |
| `dingtalk.py` | DingTalk channel — Stream API, markdown format, auto-reconnect |
| `webhook.py` | HTTP webhook — stdlib-only server with POST /chat and GET /status, embedded web UI |

## For AI Agents

### Working In This Directory
- All channels share a single `NovaAgent` instance via `create_agent()` — thread-safe access
- `ensure_single_instance()` uses port binding to prevent duplicate processes per channel
- Port assignments: Telegram=19527, Discord=19528, HTTP=19529, WeChat=19530, Feishu=19531, QQ=19532, DingTalk=19533
- `clean_reply()` strips `<thinking>`, `<summary>`, `<tool_use>`, `<file_content>` tags before showing to users
- Each channel has its own entry point defined in `pyproject.toml [project.scripts]`

### Testing Requirements
- Gateway modules are hard to unit test (require platform SDKs) — focus on shared utility tests
- Test `clean_reply()`, `build_done_text()`, `extract_files()` — these are pure functions
- Integration tests would need mock platform APIs

### Common Patterns
- Config from env vars only — no hardcoded secrets (security principle)
- User/channel auth via `allowed_users`/`allowed_channels` sets from env
- Each channel handles `/stop`, `/status`, `/new`, `/help` commands consistently
- Streaming pattern: `dq = agent.put_task(text)` → poll `dq.get()` for chunks → `build_done_text()` for final reply
- Platform-specific message limits handled per channel (Telegram 4096, Discord 2000, QQ 2000, DingTalk 1800)

## Dependencies

### Internal
- `nova.main.NovaAgent` — Shared agent instance
- `nova.gateway` utilities — Shared reply formatting and agent creation

### External
- `python-telegram-bot>=20.0` — Telegram (optional)
- `discord.py>=2.3` — Discord (optional)
- `itchat>=1.3` — WeChat (optional)
- `lark-oapi>=1.4` — Feishu/Lark (optional)
- `qq-botpy>=1.3` — QQ (optional)
- `dingtalk-stream>=1.0` — DingTalk (optional)
- `flask>=3.0` — Web (optional, stdlib alternative available)

<!-- MANUAL: Custom project notes can be added below -->
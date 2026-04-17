<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-04-17 | Updated: 2026-04-17 -->

# cron

## Purpose
Cron scheduler — tick-based job execution with file locking. Three schedule types (once, interval, cron expression), JSON persistence, grace windows, and output logging.

## Key Files

| File | Description |
|------|-------------|
| `__init__.py` | NovaCron class — background thread that ticks every 5 minutes |
| `jobs.py` | Job CRUD, schedule parsing, JSON storage at `~/.nova/cron/jobs.json`, output logging |
| `scheduler.py` | `tick(agent)` — checks due jobs, acquires file lock, executes via task queue, saves output |

## For AI Agents

### Working In This Directory
- `NovaCron` in `__init__.py` is the background scheduler thread — started by `NovaAgent.run_interactive()`
- `jobs.py` handles all CRUD and parsing — `parse_schedule()` supports three formats: duration ("30m"), interval ("every 2h"), cron ("0 9 * * *")
- `scheduler.py` uses `fcntl.flock` for file-based locking to prevent concurrent ticks
- Grace window (30 min): stale missed jobs fast-forward instead of burst-firing on restart
- Default seed jobs: Daily Maintenance (24h interval) and Weekly Knowledge Review (7d interval)
- Job output saved to `~/.nova/cron/output/{job_id}/{timestamp}.md`

### Testing Requirements
- Test `parse_schedule()` for all three formats
- Test `create_job()` and `list_jobs()` with temp JSON files
- Test grace window logic in `get_due_jobs()`
- Test file lock acquire/release

### Common Patterns
- Job IDs are `uuid.uuid4()[:8]` — 8-char hex strings
- Schedule info stored as dict: `{"kind": "once"/"interval"/"cron", ...}`
- Cron expressions require `croniter` package — ImportError raises ValueError
- `mark_job_run()` sets `enabled=False` for one-shot jobs after execution

## Dependencies

### Internal
- `nova.main.NovaAgent` — Agent instance for task execution
- `nova.cron.scheduler.tick` — Called by NovaCron._run_loop

### External
- `fcntl` — File locking for concurrent tick prevention
- `croniter` — Cron expression parsing (optional, required for cron schedule type)
- `uuid` — Job ID generation

<!-- MANUAL: Custom project notes can be added below -->
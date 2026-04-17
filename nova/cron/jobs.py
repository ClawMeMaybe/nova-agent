"""Nova cron jobs — job storage, schedule parsing, and CRUD.

Inspired by Hermes's cron/jobs.py, simplified for Nova:
- Three schedule types: one-shot, interval, cron expression
- JSON job storage at ~/.nova/cron/jobs.json
- Output logging at ~/.nova/cron/output/{job_id}/{timestamp}.md
- Grace windows: stale missed jobs fast-forward instead of burst-firing
"""

import json
import os
import re
import uuid
from datetime import datetime, timedelta
from pathlib import Path

HERMES_DIR = Path.home() / ".nova"
CRON_DIR = HERMES_DIR / "cron"
JOBS_FILE = CRON_DIR / "jobs.json"
OUTPUT_DIR = CRON_DIR / "output"


def _ensure_dirs():
    """Create cron directories if they don't exist."""
    CRON_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def _parse_duration(s: str) -> int:
    """Parse duration string like '30m', '2h', '1d' into minutes."""
    match = re.match(r'^(\d+)(m|h|d)$', s.strip().lower())
    if not match:
        raise ValueError(f"Invalid duration: '{s}'. Use format like '30m', '2h', '1d'.")
    value = int(match.group(1))
    unit = match.group(2)
    if unit == 'm':
        return value
    elif unit == 'h':
        return value * 60
    elif unit == 'd':
        return value * 1440
    return value


def parse_schedule(schedule: str) -> dict:
    """Parse schedule string into structured format.

    Three types:
    - Duration ("30m", "2h", "1d") → one-shot from now
    - Interval ("every 30m", "every 2h") → recurring interval
    - Cron ("0 9 * * *") → cron expression (requires croniter)

    Returns: {"kind": "once"/"interval"/"cron", ...}
    """
    s = schedule.strip()

    # Interval: "every Xm" or "every Xh"
    interval_match = re.match(r'^every\s+(\d+[mhd])$', s, re.IGNORECASE)
    if interval_match:
        minutes = _parse_duration(interval_match.group(1))
        return {"kind": "interval", "minutes": minutes}

    # Cron: 5-field expression (e.g. "0 9 * * *")
    cron_match = re.match(r'^(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)$', s)
    if cron_match and not re.match(r'^\d+[mhd]$', s, re.IGNORECASE):
        try:
            import croniter
        except ImportError:
            raise ValueError("Cron expressions require the 'croniter' package. Install with: pip install croniter")
        return {"kind": "cron", "expression": s}

    # One-shot: duration like "30m", "2h", "1d"
    try:
        minutes = _parse_duration(s)
        return {"kind": "once", "minutes": minutes}
    except ValueError:
        raise ValueError(f"Invalid schedule: '{s}'. Use '30m' (once), 'every 2h' (recurring), or '0 9 * * *' (cron).")


def _compute_next_run(schedule_info: dict, from_time: datetime = None) -> str:
    """Compute next_run_at ISO timestamp from schedule info."""
    from_time = from_time or datetime.now()

    if schedule_info["kind"] == "once":
        return (from_time + timedelta(minutes=schedule_info["minutes"])).isoformat()

    if schedule_info["kind"] == "interval":
        return (from_time + timedelta(minutes=schedule_info["minutes"])).isoformat()

    if schedule_info["kind"] == "cron":
        try:
            import croniter
        except ImportError:
            return from_time.isoformat()
        cron = croniter.croniter(schedule_info["expression"], from_time)
        return cron.get_next(datetime).isoformat()

    return from_time.isoformat()


def create_job(prompt: str, schedule: str, name: str = None) -> dict:
    """Create a new cron job. Auto-parses schedule, computes next_run_at."""
    schedule_info = parse_schedule(schedule)

    job = {
        "id": str(uuid.uuid4())[:8],
        "schedule": schedule_info,
        "prompt": prompt,
        "name": name or f"job_{schedule_info['kind']}",
        "enabled": True,
        "created_at": datetime.now().isoformat(),
        "next_run_at": _compute_next_run(schedule_info),
        "completed_count": 0,
        "last_run_at": None,
    }

    jobs = _load_jobs()
    jobs.append(job)
    _save_jobs(jobs)
    return job


def _load_jobs() -> list:
    """Load jobs from JSON file. Create dirs/file and seed defaults if needed."""
    _ensure_dirs()

    if not JOBS_FILE.exists():
        _seed_defaults()
        return _load_jobs()

    try:
        with open(JOBS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return []


def _save_jobs(jobs: list):
    """Save jobs to JSON file."""
    _ensure_dirs()
    with open(JOBS_FILE, 'w', encoding='utf-8') as f:
        json.dump(jobs, f, indent=2, ensure_ascii=False)


def _seed_defaults():
    """Seed default cron jobs on first run."""
    defaults = [
        {
            "id": "daily_maintenance",
            "schedule": {"kind": "interval", "minutes": 1440},
            "prompt": "Run daily memory maintenance: (1) db_query SELECT facts with low trust to check validity. (2) Review wiki pages for issues. (3) Prune stale sessions. (4) Report stats.",
            "name": "Daily Maintenance",
            "enabled": True,
            "created_at": datetime.now().isoformat(),
            "next_run_at": _compute_next_run({"kind": "interval", "minutes": 1440}),
            "completed_count": 0,
            "last_run_at": None,
        },
        {
            "id": "weekly_review",
            "schedule": {"kind": "interval", "minutes": 10080},
            "prompt": "Weekly knowledge audit: (1) Review wiki pages for outdated content using db_query. (2) Check if high-trust facts are still accurate. (3) Merge duplicate wiki pages. (4) Update autonomous-todo wiki page with new planned tasks.",
            "name": "Weekly Knowledge Review",
            "enabled": True,
            "created_at": datetime.now().isoformat(),
            "next_run_at": _compute_next_run({"kind": "interval", "minutes": 10080}),
            "completed_count": 0,
            "last_run_at": None,
        },
    ]
    _save_jobs(defaults)


def list_jobs() -> list:
    """Return all jobs."""
    return _load_jobs()


def get_job(job_id: str) -> dict | None:
    """Get a single job by ID."""
    for job in _load_jobs():
        if job["id"] == job_id:
            return job
    return None


def remove_job(job_id: str) -> bool:
    """Remove a job. Returns True if found and removed."""
    jobs = _load_jobs()
    new_jobs = [j for j in jobs if j["id"] != job_id]
    if len(new_jobs) == len(jobs):
        return False
    _save_jobs(new_jobs)
    return True


def get_due_jobs(grace_minutes: int = 30) -> list:
    """Get all enabled jobs due to run now, with grace window fast-forward.

    If next_run_at is more than grace_minutes in the past, fast-forward
    to now + interval instead of running multiple times (prevents burst-firing
    on restart after long downtime).
    """
    now = datetime.now()
    jobs = _load_jobs()
    due = []

    for job in jobs:
        if not job.get("enabled", True):
            continue
        next_run = datetime.fromisoformat(job["next_run_at"])

        if next_run <= now:
            # Check if we need to fast-forward past a long gap
            stale_minutes = (now - next_run).total_seconds() / 60
            if stale_minutes > grace_minutes and job["schedule"]["kind"] == "interval":
                # Fast-forward: skip all missed runs, schedule next from now
                job["next_run_at"] = _compute_next_run(job["schedule"], from_time=now)
                _save_jobs(jobs)
                continue  # Skip this cycle, run on next tick
            due.append(job)

    return due


def mark_job_run(job_id: str, success: bool, error: str = None) -> dict | None:
    """Mark job as run: increment completed_count, set last_run_at, compute next_run_at.

    For one-shot jobs: set enabled=False after run.
    For interval/cron: compute next run time.
    """
    jobs = _load_jobs()
    job = None
    for j in jobs:
        if j["id"] == job_id:
            job = j
            break

    if not job:
        return None

    job["completed_count"] += 1
    job["last_run_at"] = datetime.now().isoformat()
    job["last_error"] = error

    if job["schedule"]["kind"] == "once":
        job["enabled"] = False
        job["next_run_at"] = None
    else:
        job["next_run_at"] = _compute_next_run(job["schedule"], from_time=datetime.now())

    _save_jobs(jobs)
    return job


def save_job_output(job_id: str, output: str) -> str:
    """Save job output to ~/.nova/cron/output/{job_id}/{timestamp}.md.

    Returns the output file path.
    """
    _ensure_dirs()
    job_dir = OUTPUT_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = job_dir / f"{timestamp}.md"

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(f"# Cron Job Output: {job_id}\n")
        f.write(f"Date: {datetime.now().isoformat()}\n\n")
        f.write(output)

    return str(output_path)
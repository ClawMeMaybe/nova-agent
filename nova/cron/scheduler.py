"""Nova cron scheduler — tick-based execution with file locking.

Inspired by Hermes's cron/scheduler.py:
- Tick every 5 minutes, check and run due jobs
- File-based locking prevents concurrent ticks
- Jobs route through the main task queue for thread safety
"""

import fcntl
import os
import time

from nova.cron.jobs import get_due_jobs, mark_job_run, save_job_output, CRON_DIR

LOCK_FILE = CRON_DIR / ".tick.lock"
_lock_fd = None


def _acquire_lock() -> bool:
    """Try to acquire file lock. Returns True if acquired, False if already locked."""
    global _lock_fd
    try:
        CRON_DIR.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_fd = fd
        return True
    except (IOError, OSError):
        return False


def _release_lock():
    """Release file lock — close the same fd that was acquired."""
    global _lock_fd
    if _lock_fd is not None:
        try:
            fcntl.flock(_lock_fd, fcntl.LOCK_UN)
            os.close(_lock_fd)
        except (IOError, OSError):
            pass
        _lock_fd = None


def _execute_job(agent, job) -> tuple:
    """Execute a single cron job by routing through the task queue.

    This respects the _busy flag, avoids handler races, and gives visibility.
    Returns (success: bool, output: str, error: str|None).
    """
    job_prompt = f"[CRON MODE] Scheduled task: {job.get('name', job['id'])}\n\n{job['prompt']}"
    display_queue = agent.put_task(job_prompt, source="cron")

    # Wait for completion (with timeout)
    full_resp = ""
    start = time.time()
    timeout = 300  # 5 minutes max for cron jobs

    while time.time() - start < timeout:
        try:
            import queue
            item = display_queue.get(timeout=10)
        except queue.Empty:
            if not agent.is_running:
                break
            continue

        if 'next' in item:
            full_resp += item['next']
        if 'done' in item:
            full_resp = item['done']
            break

    success = len(full_resp) > 0 and 'Error' not in full_resp[:50]
    return success, full_resp or "No output", None if success else "timeout or error"


def tick(agent, verbose=True) -> int:
    """Check and run all due jobs. Uses file lock to prevent concurrent ticks.

    Returns: number of jobs executed.
    """
    # Skip if agent is currently busy with a user task
    if agent.is_running:
        if verbose:
            print("[Cron] Agent busy, skipping tick")
        return 0

    # Acquire file lock
    if not _acquire_lock():
        if verbose:
            print("[Cron] Another tick already running, skipping")
        return 0

    executed = 0
    try:
        due_jobs = get_due_jobs()

        if verbose and due_jobs:
            print(f"[Cron] {len(due_jobs)} jobs due")

        for job in due_jobs:
            if verbose:
                print(f"[Cron] Running: {job.get('name', job['id'])}")

            success, output, error = _execute_job(agent, job)

            # Save output
            output_path = save_job_output(job['id'], output)
            if verbose:
                print(f"[Cron] Output saved: {output_path}")

            # Mark job run
            mark_job_run(job['id'], success, error)
            executed += 1

            if verbose:
                status = "OK" if success else f"FAILED: {error}"
                print(f"[Cron] {job.get('name', job['id'])} → {status}")

    except Exception as e:
        if verbose:
            print(f"[Cron] Tick error: {e}")
    finally:
        _release_lock()

    return executed
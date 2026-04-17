"""Tests for Nova cron system — schedule parsing, CRUD, grace windows."""

import json
import os
import tempfile
import time
from datetime import datetime, timedelta

import pytest


# Override JOBS_FILE path before importing
_original_jobs_file = None


@pytest.fixture(autouse=True)
def temp_cron_dir(monkeypatch, tmp_path):
    """Use a temp directory for cron jobs instead of ~/.nova/cron."""
    cron_dir = tmp_path / "cron"
    cron_dir.mkdir()
    jobs_file = cron_dir / "jobs.json"
    output_dir = cron_dir / "output"
    output_dir.mkdir()

    import nova.cron.jobs as jobs_mod
    monkeypatch.setattr(jobs_mod, "CRON_DIR", cron_dir)
    monkeypatch.setattr(jobs_mod, "JOBS_FILE", jobs_file)
    monkeypatch.setattr(jobs_mod, "OUTPUT_DIR", output_dir)
    monkeypatch.setattr(jobs_mod, "HERMES_DIR", tmp_path)
    yield


from nova.cron.jobs import (
    parse_schedule, _parse_duration, create_job, list_jobs,
    get_job, remove_job, get_due_jobs, mark_job_run, save_job_output,
)


# ── Schedule Parsing ──

class TestScheduleParsing:

    def test_one_shot_30m(self):
        result = parse_schedule("30m")
        assert result['kind'] == 'once'
        assert result['minutes'] == 30

    def test_one_shot_2h(self):
        result = parse_schedule("2h")
        assert result['kind'] == 'once'
        assert result['minutes'] == 120

    def test_one_shot_1d(self):
        result = parse_schedule("1d")
        assert result['kind'] == 'once'
        assert result['minutes'] == 1440

    def test_interval_every_30m(self):
        result = parse_schedule("every 30m")
        assert result['kind'] == 'interval'
        assert result['minutes'] == 30

    def test_interval_every_2h(self):
        result = parse_schedule("every 2h")
        assert result['kind'] == 'interval'
        assert result['minutes'] == 120

    def test_cron_expression(self):
        result = parse_schedule("0 9 * * *")
        assert result['kind'] == 'cron'
        assert result['expression'] == "0 9 * * *"

    def test_invalid_schedule_raises(self):
        with pytest.raises(ValueError):
            parse_schedule("invalid schedule")


class TestParseDuration:

    def test_minutes(self):
        assert _parse_duration("30m") == 30

    def test_hours(self):
        assert _parse_duration("2h") == 120

    def test_days(self):
        assert _parse_duration("1d") == 1440

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            _parse_duration("invalid")


# ── CRUD ──

class TestCronCRUD:

    def test_create_job(self):
        job = create_job("test prompt", "every 30m", name="TestJob")
        assert job['id']
        assert job['name'] == "TestJob"
        assert job['schedule']['kind'] == 'interval'
        assert job['enabled']

    def test_list_jobs(self):
        create_job("prompt 1", "every 30m")
        create_job("prompt 2", "every 2h")
        jobs = list_jobs()
        assert len(jobs) >= 2

    def test_get_job(self):
        job = create_job("get test", "30m", name="GetTest")
        found = get_job(job['id'])
        assert found is not None
        assert found['name'] == "GetTest"

    def test_get_job_nonexistent(self):
        assert get_job("nonexistent") is None

    def test_remove_job(self):
        job = create_job("remove test", "30m", name="RemoveTest")
        assert remove_job(job['id'])
        assert get_job(job['id']) is None

    def test_remove_nonexistent(self):
        assert not remove_job("nonexistent")


# ── Seed Defaults ──

class TestSeedDefaults:

    def test_seed_creates_default_jobs(self):
        # First load triggers seeding
        jobs = list_jobs()
        names = [j['name'] for j in jobs]
        assert "Daily Maintenance" in names
        assert "Weekly Knowledge Review" in names


# ── Grace Window ──

class TestGraceWindow:

    def test_stale_job_fast_forward(self, tmp_path):
        import nova.cron.jobs as jobs_mod
        # Create an interval job with past next_run_at
        job = create_job("stale test", "every 30m", name="StaleJob")
        # Set next_run_at to 1 hour ago (stale > 30 min)
        stale_time = (datetime.now() - timedelta(hours=1)).isoformat()
        jobs = jobs_mod._load_jobs()
        for j in jobs:
            if j['id'] == job['id']:
                j['next_run_at'] = stale_time
        jobs_mod._save_jobs(jobs)

        due = get_due_jobs(grace_minutes=30)
        # Stale job should be fast-forwarded, not in due list
        stale_in_due = any(j['id'] == job['id'] for j in due)
        assert not stale_in_due


# ── Output Logging ──

class TestOutputLogging:

    def test_save_job_output(self):
        job = create_job("output test", "30m", name="OutputTest")
        path = save_job_output(job['id'], "Test output content")
        assert os.path.exists(path)
        with open(path, 'r') as f:
            content = f.read()
        assert "Test output content" in content


# ── Mark Job Run ──

class TestMarkJobRun:

    def test_mark_one_shot_disables(self):
        job = create_job("one-shot", "30m", name="OneShot")
        result = mark_job_run(job['id'], success=True)
        assert result is not None
        assert result['enabled'] is False  # One-shot disables after run

    def test_mark_interval_computes_next(self):
        job = create_job("interval", "every 30m", name="Interval")
        result = mark_job_run(job['id'], success=True)
        assert result is not None
        assert result['enabled'] is True
        assert result['completed_count'] == 1
        assert result['next_run_at'] is not None
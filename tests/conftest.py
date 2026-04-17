"""Shared test fixtures for Nova Agent."""

import os
import tempfile
import pytest

from nova.memory.engine import NovaMemory, TwoTierMemory


@pytest.fixture
def tmp_project_dir():
    """Create a temp directory with .nova subdir, cleanup after test."""
    with tempfile.TemporaryDirectory() as tmpdir:
        nova_dir = os.path.join(tmpdir, '.nova')
        os.makedirs(nova_dir, exist_ok=True)
        yield tmpdir


@pytest.fixture
def local_db(tmp_project_dir):
    """Path to a temp local database."""
    return os.path.join(tmp_project_dir, '.nova', 'nova.db')


@pytest.fixture
def global_db():
    """Path to a temp global database."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield os.path.join(tmpdir, '.nova', 'nova.db')


@pytest.fixture
def local_memory(local_db):
    """NovaMemory instance pointing to a temp local db."""
    mem = NovaMemory(local_db)
    yield mem
    mem.close()


@pytest.fixture
def global_memory(global_db):
    """NovaMemory instance pointing to a temp global db."""
    mem = NovaMemory(global_db)
    yield mem
    mem.close()


@pytest.fixture
def two_tier_memory(local_db, global_db):
    """TwoTierMemory with both temp dbs."""
    mem = TwoTierMemory(local_db, global_db)
    yield mem
    mem.close()
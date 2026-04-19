"""Shared test fixtures for Nova Agent."""

import os
import tempfile
import pytest

from nova.memory.engine import NovaMemory


@pytest.fixture
def tmp_project_dir():
    """Create a temp directory with .nova subdir, cleanup after test."""
    with tempfile.TemporaryDirectory() as tmpdir:
        nova_dir = os.path.join(tmpdir, '.nova')
        os.makedirs(nova_dir, exist_ok=True)
        yield tmpdir


@pytest.fixture
def memory(tmp_project_dir):
    """NovaMemory instance pointing to a temp db."""
    db_path = os.path.join(tmp_project_dir, '.nova', 'nova.db')
    mem = NovaMemory(db_path)
    yield mem
    mem.close()


@pytest.fixture
def memory_with_project(memory):
    """NovaMemory with a project created and selected."""
    project_id = memory.project_create("test-project", "Test project description")
    memory.project_select(project_id)
    yield memory, project_id
"""Tests for project_scan and project_learn functionality."""

import os
import tempfile
import pytest

from nova.memory.engine import NovaMemory


@pytest.fixture
def project_dir():
    """Create a temp project directory with realistic structure."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # README
        with open(os.path.join(tmpdir, 'README.md'), 'w') as f:
            f.write("# MyProject\nA test project for Nova.\n")
        # pyproject.toml
        with open(os.path.join(tmpdir, 'pyproject.toml'), 'w') as f:
            f.write("[project]\nname = 'myproject'\nrequires-python = '>=3.10'\n")
            f.write("dependencies = ['flask', 'pytest', 'ruff']\n")
        # Source dir
        src = os.path.join(tmpdir, 'src')
        os.makedirs(src)
        with open(os.path.join(src, 'app.py'), 'w') as f:
            f.write("# main app\n")
        # Tests dir
        tests = os.path.join(tmpdir, 'tests')
        os.makedirs(tests)
        with open(os.path.join(tests, 'test_app.py'), 'w') as f:
            f.write("# tests\n")
        yield tmpdir


@pytest.fixture
def db_memory():
    """NovaMemory with a temp db."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, '.nova', 'nova.db')
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        mem = NovaMemory(db_path)
        yield mem
        mem.close()


class TestProjectScan:
    def test_basic_scan(self, project_dir, db_memory):
        scan = db_memory.project_scan(project_dir)
        assert scan['project_name'] == os.path.basename(project_dir)
        assert scan['readme'] == "# MyProject\nA test project for Nova.\n"
        assert 'pyproject.toml' in scan['package_config']
        assert scan['language'] == 'python'
        assert scan['test_framework'] == 'pytest'
        assert scan['lint_tool'] == 'ruff'
        assert 'src/' in scan['file_tree']
        assert 'tests/' in scan['file_tree']
        assert 'src' in scan['key_dirs']
        assert 'tests' in scan['key_dirs']

    def test_quick_depth(self, project_dir, db_memory):
        scan = db_memory.project_scan(project_dir, depth='quick')
        # Quick depth only goes 1 level — should see dirs but not contents
        assert 'src/' in scan['file_tree']
        assert 'app.py' not in scan['file_tree']

    def test_deep_depth(self, project_dir, db_memory):
        scan = db_memory.project_scan(project_dir, depth='deep')
        assert 'app.py' in scan['file_tree']

    def test_empty_directory(self, db_memory):
        with tempfile.TemporaryDirectory() as empty_dir:
            scan = db_memory.project_scan(empty_dir)
            assert scan['project_name'] == os.path.basename(empty_dir)
            assert scan['readme'] == ''
            assert scan['package_config'] == ''
            assert scan['language'] == ''
            assert scan['file_tree'] == []

    def test_js_project(self, db_memory):
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, 'package.json'), 'w') as f:
                f.write('{"name": "myapp", "dependencies": {"react": "18.2.0"}}')
            with open(os.path.join(tmpdir, 'jest.config.js'), 'w') as f:
                f.write('// jest config')
            scan = db_memory.project_scan(tmpdir)
            assert scan['language'] == 'javascript'

    def test_skips_hidden_dirs(self, project_dir, db_memory):
        # Create a .venv dir that should be skipped
        venv = os.path.join(project_dir, '.venv')
        os.makedirs(venv)
        with open(os.path.join(venv, 'should_skip.py'), 'w') as f:
            f.write('# skip me')
        scan = db_memory.project_scan(project_dir)
        assert '.venv/' not in scan['file_tree']
        assert 'should_skip.py' not in scan['file_tree']

    def test_reads_knowledge_files(self, db_memory):
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, 'CLAUDE.md'), 'w') as f:
                f.write('# CLAUDE.md\nUse TypeScript strict mode.\n')
            with open(os.path.join(tmpdir, 'AGENTS.md'), 'w') as f:
                f.write('# AGENTS.md\nAgent guidelines.\n')
            scan = db_memory.project_scan(tmpdir)
            assert 'CLAUDE.md' in scan['project_knowledge_files']
            assert 'AGENTS.md' in scan['project_knowledge_files']
            assert 'TypeScript strict mode' in scan['project_knowledge_files']

    def test_reads_workspace_configs(self, db_memory):
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, 'package.json'), 'w') as f:
                f.write('{"name": "root", "workspaces": ["packages/*"]}')
            packages = os.path.join(tmpdir, 'packages', 'backend')
            os.makedirs(packages)
            with open(os.path.join(packages, 'package.json'), 'w') as f:
                f.write('{"name": "@myapp/backend", "dependencies": {"express": "^4.18"}}')
            scan = db_memory.project_scan(tmpdir)
            assert 'packages/backend/package.json' in scan['workspace_configs']
            assert '@myapp/backend' in scan['workspace_configs']

    def test_reads_infrastructure_configs(self, db_memory):
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, 'docker-compose.yml'), 'w') as f:
                f.write('services:\n  mysql:\n    image: mysql:8.0\n')
            with open(os.path.join(tmpdir, 'tsconfig.json'), 'w') as f:
                f.write('{"compilerOptions": {"strict": true}}\n')
            with open(os.path.join(tmpdir, '.env.example'), 'w') as f:
                f.write('DB_HOST=localhost\n')
            scan = db_memory.project_scan(tmpdir)
            assert 'docker-compose.yml' in scan['infrastructure_configs']
            assert 'tsconfig.json' in scan['infrastructure_configs']
            assert '.env.example' in scan['infrastructure_configs']

    def test_reads_ci_configs(self, db_memory):
        with tempfile.TemporaryDirectory() as tmpdir:
            ci_dir = os.path.join(tmpdir, '.github', 'workflows')
            os.makedirs(ci_dir)
            with open(os.path.join(ci_dir, 'ci.yml'), 'w') as f:
                f.write('name: CI\non: push\n')
            scan = db_memory.project_scan(tmpdir)
            assert '.github/workflows/ci.yml' in scan['ci_configs']

    def test_missing_files_graceful(self, db_memory):
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, 'README.md'), 'w') as f:
                f.write('# Simple project\n')
            scan = db_memory.project_scan(tmpdir)
            assert scan['project_knowledge_files'] == ''
            assert scan['workspace_configs'] == ''
            assert scan['infrastructure_configs'] == ''
            assert scan['ci_configs'] == ''

    def test_build_learn_prompt(self):
        from nova.main import build_learn_prompt
        scan = {
            'project_name': 'testproj',
            'language': 'python',
            'test_framework': 'pytest',
            'lint_tool': 'ruff',
            'key_dirs': ['src', 'tests'],
            'file_tree': ['src/', 'tests/', 'main.py'],
            'readme': '# Test Project\n',
            'package_config': '',
            'project_knowledge_files': '',
            'workspace_configs': '',
            'infrastructure_configs': '',
            'ci_configs': '',
        }
        prompt = build_learn_prompt(scan)
        assert 'testproj' in prompt
        assert 'pytest' in prompt
        assert 'actionable' in prompt.lower()
        assert 'Good' in prompt
        assert 'Bad' in prompt
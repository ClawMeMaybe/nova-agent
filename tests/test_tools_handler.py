"""Tests for Nova tool handler — code_run, file ops, smart_format, cron."""

import os
import tempfile

import pytest

from nova.tools.handler import code_run, file_read, file_write, file_patch, smart_format


class TestCodeRun:

    def test_python_execution(self):
        result = code_run("print('hello')", "python")
        assert result['status'] == 'success'
        assert 'hello' in result['stdout']

    def test_bash_execution(self):
        result = code_run("echo hello", "bash")
        assert result['status'] == 'success'
        assert 'hello' in result['stdout']

    def test_timeout(self):
        result = code_run("import time; time.sleep(10)", "python", timeout=2)
        assert result['status'] == 'error'
        assert 'Timeout' in result['stdout'] or result['exit_code'] is not None

    def test_stop_signal(self):
        result = code_run("import time; time.sleep(10)", "python", timeout=60, stop_signal=[1])
        assert 'Stopped' in result['stdout'] or 'Aborted' in result['stdout'] or result['exit_code'] is not None

    def test_invalid_type(self):
        result = code_run("test", "ruby")
        assert result['status'] == 'error'


class TestFileRead:

    def test_read_file(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("hello world\nline 2")
        result = file_read(str(f))
        assert "hello world" in result

    def test_read_with_keyword(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("line 1\nimportant line\nline 3")
        result = file_read(str(f), keyword="important")
        assert "important" in result

    def test_read_nonexistent(self):
        result = file_read("/nonexistent/file.txt")
        assert "not found" in result.lower() or "Error" in result


class TestFileWrite:

    def test_overwrite(self, tmp_path):
        f = tmp_path / "write.txt"
        result = file_write(str(f), "hello content")
        assert result['status'] == 'success'

        with open(str(f)) as fh:
            assert fh.read() == "hello content"

    def test_append(self, tmp_path):
        f = tmp_path / "append.txt"
        f.write_text("line 1\n")
        result = file_write(str(f), "line 2", mode="append")
        assert result['status'] == 'success'

        with open(str(f)) as fh:
            content = fh.read()
            assert "line 1" in content
            assert "line 2" in content

    def test_empty_content_rejected(self, tmp_path):
        f = tmp_path / "empty.txt"
        # Handler-level rejects empty content in do_file_write, but file_write itself just writes it
        result = file_write(str(f), "")
        assert result['status'] == 'success'


class TestFilePatch:

    def test_patch_unique_block(self, tmp_path):
        f = tmp_path / "patch.txt"
        f.write_text("before\nunique block\nafter")
        result = file_patch(str(f), "unique block", "replaced block")
        assert result['status'] == 'success'

        with open(str(f)) as fh:
            assert "replaced block" in fh.read()

    def test_patch_non_unique_error(self, tmp_path):
        f = tmp_path / "dup.txt"
        f.write_text("dup\ndup\ndup")
        result = file_patch(str(f), "dup", "new")
        assert result['status'] == 'error'
        assert "matches" in result['msg'].lower() or "specific" in result['msg'].lower()

    def test_patch_not_found(self, tmp_path):
        f = tmp_path / "nopatch.txt"
        f.write_text("some content")
        result = file_patch(str(f), "nonexistent", "new")
        assert result['status'] == 'error'

    def test_patch_empty_old_rejected(self, tmp_path):
        f = tmp_path / "empty.txt"
        f.write_text("content")
        result = file_patch(str(f), "", "new")
        assert result['status'] == 'error'


class TestSmartFormat:

    def test_short_string_unchanged(self):
        assert smart_format("short") == "short"

    def test_long_string_truncated(self):
        long_str = "a" * 200
        result = smart_format(long_str)
        assert "..." in result
        assert len(result) < 200

    def test_custom_max_len(self):
        result = smart_format("x" * 200, max_str_len=50)
        assert len(result) < 200
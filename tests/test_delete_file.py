"""Tests for live_edit/builtin_tools/delete_file.py"""

import subprocess

import pytest

from live_edit.builtin_tools import delete_file as df
from live_edit.engine import translate_error


class FakeSafety:
    def __init__(self, allow_overwrite=False):
        self.overwrite_allowed_dirs = ["static", "public", "assets"]
        self.allow_overwrite_existing = allow_overwrite


class FakeConfig:
    def __init__(self, allow_overwrite=False):
        self.safety = FakeSafety(allow_overwrite)


def _init_git(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "--allow-empty", "-q", "-m", "init"],
        cwd=str(tmp_path),
        check=True,
    )


def _tracked_file(tmp_path, rel, content="x\n"):
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    subprocess.run(["git", "add", rel], cwd=str(tmp_path), check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", f"add {rel}"],
        cwd=str(tmp_path),
        check=True,
    )
    return p


@pytest.mark.asyncio
class TestDeleteFile:
    async def test_delete_new_file_ok(self, tmp_path):
        _init_git(tmp_path)
        (tmp_path / "new.txt").write_text("hi")
        result = await df.execute({"path": "new.txt"}, str(tmp_path), FakeConfig())
        assert result["ok"] is True
        assert not (tmp_path / "new.txt").exists()

    async def test_delete_tracked_source_file_blocked(self, tmp_path):
        _init_git(tmp_path)
        _tracked_file(tmp_path, "src/utils.py")
        result = await df.execute({"path": "src/utils.py"}, str(tmp_path), FakeConfig())
        assert result["ok"] is False
        assert "受保护" in result["error"] or "覆写" in result["error"]
        assert (tmp_path / "src/utils.py").exists()

    async def test_delete_tracked_overwrite_dir_ok(self, tmp_path):
        _init_git(tmp_path)
        _tracked_file(tmp_path, "static/app.css")
        result = await df.execute({"path": "static/app.css"}, str(tmp_path), FakeConfig())
        assert result["ok"] is True
        assert not (tmp_path / "static/app.css").exists()

    async def test_delete_tracked_with_allow_overwrite_ok(self, tmp_path):
        _init_git(tmp_path)
        _tracked_file(tmp_path, "src/utils.py")
        result = await df.execute({"path": "src/utils.py"}, str(tmp_path), FakeConfig(allow_overwrite=True))
        assert result["ok"] is True

    async def test_delete_missing_file_errors(self, tmp_path):
        _init_git(tmp_path)
        result = await df.execute({"path": "nope.py"}, str(tmp_path), FakeConfig())
        assert result["ok"] is False
        assert "不存在" in result["error"]

    async def test_delete_blocked_when_no_head_commit(self, tmp_path):
        # Regression (I1): repo with git init but NO commit (no HEAD yet) —
        # git ls-tree exits 128 with empty stdout. Policy must be conservative
        # (protected), NOT collapse to allow-all. Note: _init_git would create
        # HEAD via --allow-empty; do not use it here.
        subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), check=True)
        (tmp_path / "notes.txt").write_text("hi")
        result = await df.execute({"path": "notes.txt"}, str(tmp_path), FakeConfig())
        assert result["ok"] is False
        assert (tmp_path / "notes.txt").exists()

    async def test_delete_directory_refused(self, tmp_path):
        _init_git(tmp_path)
        (tmp_path / "adir").mkdir()
        result = await df.execute({"path": "adir"}, str(tmp_path), FakeConfig())
        assert result["ok"] is False
        assert "目录" in result["error"]

    async def test_delete_escaped_path_blocked(self, tmp_path):
        _init_git(tmp_path)
        result = await df.execute({"path": "../outside.txt"}, str(tmp_path), FakeConfig())
        assert result["ok"] is False

    async def test_tool_def_is_write(self):
        td = df.create()
        assert td.name == "delete_file"
        assert td.is_write is True
        assert "path" in td.input_schema["required"]


def test_delete_blocked_error_translation_default_map():
    # Regression: the delete-blocked error contains "write_file 只能覆写" as a
    # substring; the delete-specific key must sort BEFORE it so quick mode
    # surfaces the delete message (not the generic create-or-modify one).
    msg = translate_error(
        "删除受保护文件被拒绝：write_file 只能覆写 static 目录下的文件或创建新文件",
        "quick",
    )
    assert "该文件受保护，不能删除" in msg

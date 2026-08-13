"""Tests for live_edit.cli.cmd_agent_hook — generate .live-edit/AGENTS.md."""

from live_edit.cli import cmd_agent_hook
from live_edit.hook import render_agent_hook


class TestAgentHook:
    def _fastapi_project(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            "[project]\nname = 'demo'\ndependencies = ['fastapi']\n"
        )
        (tmp_path / "main.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n")

    def test_generates_agents_md_for_fastapi(self, tmp_path):
        self._fastapi_project(tmp_path)
        assert cmd_agent_hook(str(tmp_path)) is True
        guide = tmp_path / ".live-edit" / "AGENTS.md"
        assert guide.exists()
        content = guide.read_text()
        assert "接入 live-edit" in content
        assert "setup_live_edit" in content
        assert "StaticFiles" in content
        # live-edit 无独立服务分支，必须不出现 serve
        assert "serve" not in content

    def test_non_fastapi_project_gets_embed_only_path(self, tmp_path):
        (tmp_path / "package.json").write_text('{"name": "web-app", "scripts": {"dev": "vite"}}')
        assert cmd_agent_hook(str(tmp_path)) is True
        content = (tmp_path / ".live-edit" / "AGENTS.md").read_text()
        # live-edit 是纯库嵌入：非 FastAPI 项目应提示不可直接接入，而非给 serve 方案
        assert "无法直接接入" in content
        assert "serve" not in content

    def test_empty_dir_gets_generic_degraded_instruction(self, tmp_path):
        assert cmd_agent_hook(str(tmp_path)) is True
        content = (tmp_path / ".live-edit" / "AGENTS.md").read_text()
        assert "接入 live-edit" in content
        assert "git" in content  # notes git dependency

    def test_refuses_overwrite_without_force(self, tmp_path):
        self._fastapi_project(tmp_path)
        assert cmd_agent_hook(str(tmp_path)) is True
        assert cmd_agent_hook(str(tmp_path)) is False  # second call refuses

    def test_force_overwrites(self, tmp_path):
        self._fastapi_project(tmp_path)
        assert cmd_agent_hook(str(tmp_path)) is True
        assert cmd_agent_hook(str(tmp_path), force=True) is True

    def test_returns_false_for_missing_dir(self, tmp_path):
        missing = str(tmp_path / "does-not-exist")
        assert cmd_agent_hook(missing) is False


class TestRenderAgentHook:
    def test_fastapi_project_gets_library_embed_path(self):
        project = {
            "name": "demo",
            "language": "python",
            "framework": "fastapi",
            "vcs": "git",
            "git_available": True,
            "test_command": "python -m pytest tests -q --tb=short",
            "health_url": "http://127.0.0.1:8000/live-edit/health",
        }
        content = render_agent_hook(project, "/tmp/demo")
        assert "setup_live_edit" in content
        assert "LIVE_EDIT_ADMIN_KEY" in content
        assert "/live-edit/static/live-edit.js" in content
        assert ".live-edit.toml" in content

    def test_never_mentions_standalone_serve(self):
        """live-edit 是纯库嵌入：渲染结果不得出现 serve / 独立服务 / live-build 残留。"""
        project = {
            "name": "demo",
            "language": "python",
            "framework": "fastapi",
            "vcs": "git",
            "git_available": True,
        }
        content = render_agent_hook(project, "/tmp/demo")
        for bad in ("serve", "独立服务", "setup_live_build", "live-build", "LIVE_BUILD"):
            assert bad not in content, f"渲染结果不应包含 {bad!r}"

    def test_no_secrets_hardcoded(self):
        project = {
            "name": "demo",
            "language": "python",
            "framework": "fastapi",
            "vcs": "git",
            "git_available": True,
        }
        content = render_agent_hook(project, "/tmp/demo")
        # 指导使用环境变量名，不得写死 key/token/password 值
        assert "os.environ.get" in content
        assert "不硬编码任何 API key / token / 密码" in content

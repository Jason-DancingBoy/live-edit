"""Tests for live_edit.intake.run — the `live-edit intake` orchestration.

离线可跑：subprocess.run 与 input、health 探活全部 mock，不真实跑测试/等服务器。
"""

from live_edit.config import parse_config, validate_config
from live_edit.intake import run_intake

MAIN_PY = (
    "from fastapi import FastAPI\n"
    "\n"
    "app = FastAPI()\n"
    "\n"
    '@app.get("/")\n'
    "def root():\n"
    '    return {"ok": True}\n'
)


def _fastapi_with_tests(tmp_path):
    """FastAPI 项目：有 tests/ + pytest 配置 + 伪 venv 解释器。"""
    (tmp_path / "main.py").write_text(MAIN_PY)
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'demo'\ndependencies = ['fastapi']\n\n"
        "[tool.pytest.ini_options]\ntestpaths = ['tests']\n"
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_health.py").write_text("def test_ok():\n    assert True\n")
    venv_py = tmp_path / ".venv" / "bin" / "python"
    venv_py.parent.mkdir(parents=True)
    venv_py.touch()  # 模拟真实虚拟环境解释器文件


def _fastapi_no_tests(tmp_path):
    """FastAPI 项目：无测试（触发冒烟测试生成）。"""
    (tmp_path / "main.py").write_text(MAIN_PY)
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'demo'\ndependencies = ['fastapi']\n"
    )
    venv_py = tmp_path / ".venv" / "bin" / "python"
    venv_py.parent.mkdir(parents=True)
    venv_py.touch()


def _empty_project(tmp_path):
    """几乎空的项目：profile 信息不足，extra_context 会带 TODO 标记。"""
    (tmp_path / "hello.py").write_text("print('hi')\n")


def _fake_run_success(monkeypatch):
    class _FakeProc:
        returncode = 0

    monkeypatch.setattr("subprocess.run", lambda *a, **k: _FakeProc())


def _fake_run_fail(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr("subprocess.run", _boom)


def _fake_health(monkeypatch, reachable=False):
    monkeypatch.setattr("live_edit.intake.run._probe_health", lambda url: reachable)


class TestFullFlowFastAPI:
    def test_config_written_and_valid(self, monkeypatch, tmp_path):
        _fastapi_with_tests(tmp_path)
        _fake_run_success(monkeypatch)
        _fake_health(monkeypatch)

        result = run_intake(str(tmp_path), force=True, auto_yes=True)

        assert result.config_written is True
        config_path = tmp_path / ".live-edit.toml"
        assert config_path.exists()

        content = config_path.read_text()
        assert "extra_context" in content
        assert "## 项目技术栈" in content  # 渲染后的 extra_context 小节标题
        assert "[verify]" in content

        config = parse_config(str(config_path))
        # extra_context 覆盖为深度渲染结果
        assert "## 项目技术栈" in config.project.extra_context
        assert "核心业务链路" in config.project.extra_context
        # verify.test_command 来自 provision（复用 pyproject testpaths + 绝对 venv python；
        # testpaths 经 shlex.quote，无特殊字符 → 不带引号）
        venv_py = tmp_path / ".venv" / "bin" / "python"
        assert config.verify.test_command == f"{venv_py} -m pytest tests -q --tb=short"
        assert config.verify.health_url == "http://127.0.0.1:8000/live-edit/health"
        assert validate_config(config) == []

        # has_tests=True → 不生成冒烟
        assert result.smoke_written is False
        assert result.smoke_path is None
        assert not (tmp_path / "tests" / "test_smoke.py").exists()

        # validation 含成功项（测试命令验证通过）
        assert any("测试命令验证通过" in v for v in result.validation)

    def test_generated_command_survives_shlex_split(self, monkeypatch, tmp_path):
        """shlex.quote 拼接的命令经 shlex.split 能无损还原（无注入）。"""
        import shlex

        _fastapi_with_tests(tmp_path)
        _fake_run_success(monkeypatch)
        _fake_health(monkeypatch)

        run_intake(str(tmp_path), force=True, auto_yes=True)
        config = parse_config(str(tmp_path / ".live-edit.toml"))
        tokens = shlex.split(config.verify.test_command)
        assert tokens[0] == str(tmp_path / ".venv" / "bin" / "python")
        assert tokens[1:] == ["-m", "pytest", "tests", "-q", "--tb=short"]


class TestSmokeGenerated:
    def test_smoke_written_after_confirmation(self, monkeypatch, tmp_path):
        _fastapi_no_tests(tmp_path)
        _fake_run_success(monkeypatch)
        _fake_health(monkeypatch)
        monkeypatch.setattr("builtins.input", lambda prompt: "y")

        result = run_intake(str(tmp_path), force=True, auto_yes=False)

        assert result.smoke_written is True
        assert result.smoke_path == "tests/test_smoke.py"
        smoke_path = tmp_path / "tests" / "test_smoke.py"
        assert smoke_path.exists()
        content = smoke_path.read_text()
        assert content.startswith("# 由 live-edit intake 自动生成（可安全删除）")
        assert "from main import app" in content
        assert "def test_app_imports()" in content
        assert "assert app is not None" in content
        # 写后重跑冒烟 test_command 确认绿
        assert any("冒烟测试通过" in v for v in result.validation)
        assert result.config_written is True

    def test_input_no_cancels_smoke(self, monkeypatch, tmp_path):
        _fastapi_no_tests(tmp_path)
        _fake_run_success(monkeypatch)
        _fake_health(monkeypatch)
        monkeypatch.setattr("builtins.input", lambda prompt: "n")

        result = run_intake(str(tmp_path), force=True, auto_yes=False)

        assert result.smoke_written is False
        assert not (tmp_path / "tests").exists()
        assert any("已取消" in m for m in result.messages)
        # 取消冒烟后，config 的 verify.test_command 不得指向未生成的冒烟文件
        config = parse_config(str(tmp_path / ".live-edit.toml"))
        assert config.verify.test_command == ""
        assert "test_command =" not in (tmp_path / ".live-edit.toml").read_text()

    def test_input_eof_cancels_smoke(self, monkeypatch, tmp_path):
        """无 stdin（CI/管道）时 input 抛 EOFError → 视为取消，不崩溃。"""
        _fastapi_no_tests(tmp_path)
        _fake_run_success(monkeypatch)
        _fake_health(monkeypatch)

        def _eof(prompt):
            raise EOFError

        monkeypatch.setattr("builtins.input", _eof)

        result = run_intake(str(tmp_path), force=True, auto_yes=False)

        assert result.smoke_written is False
        assert not (tmp_path / "tests").exists()
        assert any("已取消" in m for m in result.messages)
        assert any("--yes" in m for m in result.messages)  # 提示可用 --yes 跳过确认
        config = parse_config(str(tmp_path / ".live-edit.toml"))
        assert config.verify.test_command == ""


class TestDryRun:
    def test_dry_run_writes_nothing(self, monkeypatch, tmp_path):
        _fastapi_no_tests(tmp_path)
        _fake_run_success(monkeypatch)
        _fake_health(monkeypatch)

        result = run_intake(str(tmp_path), dry_run=True, force=True, auto_yes=True)

        assert result.config_written is False
        assert result.smoke_written is False
        assert not (tmp_path / ".live-edit.toml").exists()
        assert not (tmp_path / "tests").exists()

    def test_dry_run_skips_subprocess_and_health(self, monkeypatch, tmp_path):
        """dry_run 是纯分析：has_tests=True 也不跑测试、不探活。"""
        _fastapi_with_tests(tmp_path)

        def _no_subprocess(*a, **k):
            raise AssertionError("dry_run 不应执行 subprocess.run")

        def _no_health(url):
            raise AssertionError("dry_run 不应探活 health_url")

        monkeypatch.setattr("subprocess.run", _no_subprocess)
        monkeypatch.setattr("live_edit.intake.run._probe_health", _no_health)

        result = run_intake(str(tmp_path), dry_run=True)

        assert any("dry-run 未执行验证" in v for v in result.validation)
        assert result.config_written is False
        assert not (tmp_path / ".live-edit.toml").exists()

    def test_dry_run_with_existing_config_still_previews(self, monkeypatch, tmp_path):
        """已有配置 + dry_run：仍输出完整预览，不写文件、不覆盖原文件。

        dry_run 分支必须在 exists 检查之前——否则已有配置会把预览短路掉。
        """
        _fastapi_with_tests(tmp_path)
        _fake_run_success(monkeypatch)
        _fake_health(monkeypatch)
        (tmp_path / ".live-edit.toml").write_text("# existing")

        result = run_intake(str(tmp_path), dry_run=True, force=True, auto_yes=True)

        assert result.config_written is False
        # 原文件未被覆盖、未新建
        assert (tmp_path / ".live-edit.toml").read_text() == "# existing"
        # messages 含完整预览（配置内容 + [verify] 段），且不被「已存在」短路
        joined = "\n".join(result.messages)
        assert "[dry-run] 将写入配置文件" in joined
        assert "[verify]" in joined
        assert "health_url" in joined
        assert "配置文件已存在" not in joined


class TestExistingConfig:
    def test_no_force_skips_config_write(self, monkeypatch, tmp_path):
        _fastapi_with_tests(tmp_path)
        _fake_run_success(monkeypatch)
        _fake_health(monkeypatch)
        (tmp_path / ".live-edit.toml").write_text("# existing")

        result = run_intake(str(tmp_path), auto_yes=True)

        assert result.config_written is False
        # 原文件未被覆盖
        assert (tmp_path / ".live-edit.toml").read_text() == "# existing"
        assert any("已存在" in m and "--force" in m for m in result.messages)


class TestValidateDegrade:
    def test_command_failure_degrades_to_human(self, monkeypatch, tmp_path):
        _fastapi_with_tests(tmp_path)
        _fake_run_fail(monkeypatch)  # 原命令 + 降级候选全部失败
        _fake_health(monkeypatch)

        result = run_intake(str(tmp_path), force=True, auto_yes=True)

        # test_command 置空 → 写出的配置不含 test_command 行
        config = parse_config(str(tmp_path / ".live-edit.toml"))
        assert config.verify.test_command == ""
        assert "test_command =" not in (tmp_path / ".live-edit.toml").read_text()
        # validation 含降级提示（安全设计）
        assert any("降级人工审批" in v for v in result.validation)
        assert any("pytest" in m for m in result.messages)

    def test_downgrade_candidate_succeeds(self, monkeypatch, tmp_path):
        _fastapi_with_tests(tmp_path)
        _fake_health(monkeypatch)

        class _Proc:
            returncode = 0

        def _fake(cmd, **k):
            # 原命令（绝对 venv python）失败，降级候选（python3 ...）成功
            if cmd.startswith("python3"):
                return _Proc()
            raise RuntimeError("boom")

        monkeypatch.setattr("subprocess.run", _fake)

        result = run_intake(str(tmp_path), force=True, auto_yes=True)

        config = parse_config(str(tmp_path / ".live-edit.toml"))
        assert config.verify.test_command == "python3 -m pytest tests -q --tb=short"
        assert any("降级候选通过" in v for v in result.validation)


class TestTodos:
    def test_todos_extracted_from_extra_context(self, monkeypatch, tmp_path):
        _empty_project(tmp_path)
        _fake_run_success(monkeypatch)
        _fake_health(monkeypatch)

        result = run_intake(str(tmp_path), dry_run=True)

        assert result.todos
        assert all(t.startswith("TODO:") for t in result.todos)


class TestInvalidRoot:
    def test_non_directory_raises(self, tmp_path):
        import pytest

        missing = tmp_path / "nope"
        with pytest.raises(ValueError, match="目录不存在"):
            run_intake(str(missing))

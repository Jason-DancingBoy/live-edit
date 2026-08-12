"""Tests for live_edit.cli — live-edit init, intake, and check commands."""

import tomllib


class TestCliInit:
    def test_init_creates_config_file(self, tmp_path):
        """live-edit init creates a .live-edit.toml in the target directory."""
        from live_edit.cli import cmd_init

        # Create a minimal project structure
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'test-app'\n")

        result = cmd_init(str(tmp_path))

        assert result is True
        config_path = tmp_path / ".live-edit.toml"
        assert config_path.exists()
        content = config_path.read_text()
        assert "[project]" in content
        assert "test-app" in content

    def test_init_detects_node_project(self, tmp_path):
        """live-edit init detects Node.js projects."""
        from live_edit.cli import cmd_init

        (tmp_path / "package.json").write_text('{"name": "node-app"}')

        result = cmd_init(str(tmp_path))

        assert result is True
        content = (tmp_path / ".live-edit.toml").read_text()
        assert 'language = "javascript"' in content or "node-app" in content

    def test_init_refuses_to_overwrite_existing(self, tmp_path):
        """live-edit init refuses to overwrite existing config without --force."""
        from live_edit.cli import cmd_init

        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'test'\n")
        (tmp_path / ".live-edit.toml").write_text("# existing")

        result = cmd_init(str(tmp_path))

        assert result is False

    def test_init_force_overwrites(self, tmp_path):
        """live-edit init --force overwrites existing config."""
        from live_edit.cli import cmd_init

        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'test'\n")
        (tmp_path / ".live-edit.toml").write_text("# old config")

        result = cmd_init(str(tmp_path), force=True)

        assert result is True
        content = (tmp_path / ".live-edit.toml").read_text()
        assert "# old config" not in content


class TestCliCheck:
    def test_check_with_valid_config(self, tmp_path):
        """live-edit check with valid config reports ok."""
        from live_edit.cli import cmd_check

        config_path = tmp_path / ".live-edit.toml"
        config_path.write_text("""
[project]
name = "Test"
language = "python"
root = "."

[llm]
provider = "anthropic_compatible"
api_url = "https://api.example.com/v1/messages"
api_key_env = "KEY"
model = "test"

[safety]

[timeouts]

[sessions]

[hooks]

[ui]
default_mode = "quick"

[modes.quick]
label = "快速修改"
approval = "per_tool"
tools = "write"

[modes.quick.prompt]
base = "You are helpful."
user_persona = "User."
communication_rules = "Use Chinese."
""")

        result = cmd_check(str(config_path))
        assert result is True

    def test_check_missing_file(self, tmp_path):
        """live-edit check with nonexistent file reports error."""
        from live_edit.cli import cmd_check

        result = cmd_check(str(tmp_path / "nonexistent.toml"))
        assert result is False


class TestCliIntake:
    def _fake_result(self, tmp_path):
        from live_edit.intake import IntakeResult, RepoProfile, VerifyProvision

        return IntakeResult(
            profile=RepoProfile(
                name="demo",
                language="python",
                framework="fastapi",
                package_manager="pip",
                vcs="none",
                git_available=False,
                python_cmd="python3",
                app_module="main:app",
                port=8000,
                test_command="python3 -m pytest -q --tb=short",
                health_url="http://127.0.0.1:8000/live-edit/health",
                frontend=None,
                entry_points=["main.py"],
                modules=[],
                routes=[],
                db=None,
                protected_paths=[".env"],
                has_tests=True,
                test_dirs=["tests"],
            ),
            provision=VerifyProvision(
                test_command="python3 -m pytest -q --tb=short",
                health_url="",
                smoke_test=None,
                needs_confirmation=False,
            ),
            config_path=str(tmp_path / ".live-edit.toml"),
            config_written=True,
            smoke_path=None,
            smoke_written=False,
            validation=["测试命令验证通过: python3 -m pytest -q --tb=short"],
            todos=["TODO: 补充核心业务链路"],
            messages=["下一步:", "  1. 设置 LLM API key 环境变量：DEEPSEEK_API_KEY=..."],
        )

    def test_intake_command_dispatch(self, monkeypatch, capsys, tmp_path):
        """live-edit intake 分发：run_intake 被正确调用，输出覆盖结果。"""
        import pytest

        from live_edit.cli import main

        result = self._fake_result(tmp_path)
        calls = {}
        monkeypatch.setattr(
            "live_edit.intake.run_intake",
            lambda root, dry_run=False, force=False, auto_yes=False: (
                calls.update(
                    {"root": root, "dry_run": dry_run, "force": force, "auto_yes": auto_yes}
                )
                or result
            ),
        )
        monkeypatch.setattr("sys.argv", ["live-edit", "intake", str(tmp_path), "--force", "--yes"])

        with pytest.raises(SystemExit) as excinfo:
            main()

        assert excinfo.value.code == 0
        assert calls["root"] == str(tmp_path)
        assert calls["force"] is True
        assert calls["auto_yes"] is True

        out = capsys.readouterr().out
        assert "demo" in out
        assert "测试命令验证通过" in out
        assert "下一步" in out
        assert "TODO: 补充核心业务链路" in out

    def test_intake_dry_run_flag_passes_through(self, monkeypatch, capsys, tmp_path):
        """--dry-run 传给 run_intake；预演模式不写任何文件也不退出非零。"""
        import pytest

        from live_edit.cli import main

        result = self._fake_result(tmp_path)
        calls = {}
        monkeypatch.setattr(
            "live_edit.intake.run_intake",
            lambda root, dry_run=False, force=False, auto_yes=False: (
                calls.update({"dry_run": dry_run}) or result
            ),
        )
        monkeypatch.setattr("sys.argv", ["live-edit", "intake", str(tmp_path), "--dry-run"])

        with pytest.raises(SystemExit) as excinfo:
            main()

        assert excinfo.value.code == 0
        assert calls["dry_run"] is True
        out = capsys.readouterr().out
        assert "[dry-run]" in out

    def test_intake_invalid_dir_returns_failure(self, monkeypatch, capsys, tmp_path):
        """intake 指向不存在的目录：报错并以非零码退出。"""
        import pytest

        from live_edit.cli import main

        def _boom(root, **k):
            raise ValueError(f"目录不存在: {root}")

        monkeypatch.setattr("live_edit.intake.run_intake", _boom)
        monkeypatch.setattr("sys.argv", ["live-edit", "intake", str(tmp_path / "nope")])

        with pytest.raises(SystemExit) as excinfo:
            main()

        assert excinfo.value.code == 1
        out = capsys.readouterr().out
        assert "目录不存在" in out

    def test_intake_existing_config_returns_failure(self, monkeypatch, capsys, tmp_path):
        """config 已存在且未 --force → cmd_intake 返回 False、非零退出码（对齐 cmd_init）。"""
        import pytest

        from live_edit.cli import cmd_intake, main

        result = self._fake_result(tmp_path)
        result.config_written = False  # 模拟「已存在未覆盖」
        monkeypatch.setattr("live_edit.intake.run_intake", lambda *a, **k: result)

        # cmd_intake 直接返回 False
        assert cmd_intake(str(tmp_path)) is False

        # main() 分发：以非零码退出
        monkeypatch.setattr("sys.argv", ["live-edit", "intake", str(tmp_path)])
        with pytest.raises(SystemExit) as excinfo:
            main()

        assert excinfo.value.code == 1
        out = capsys.readouterr().out
        assert "配置未写入" in out


class TestRenderConfig:
    def test_round_trip_special_chars_lossless(self):
        """_render_config output parses back to the exact original values via
        tomllib, even for values with backslashes, quotes, triple quotes,
        pipes, and newlines."""
        from live_edit.cli import _render_config
        from live_edit.config import (
            Config,
            ErrorTranslations,
            ModeConfig,
            ModePromptConfig,
            ProjectConfig,
        )

        cfg = Config()
        cfg.project = ProjectConfig(
            name='a\\b"c',
            language="python",
            framework='web"x',
            root=".",
            extra_context='Line1\nLine2 "quotes" \\ backslash """ triple | pipe',
        )
        cfg.modes["quick"] = ModeConfig(
            label='q"l',
            approval="per_tool",
            tools="write",
            approve_for=['edit"file', "x\\y"],
            prompt=ModePromptConfig(
                base='Base \\" and """ and\nnewline',
                user_persona="persona | with pipe\nand newline",
                communication_rules='rules "quoted"',
            ),
        )
        cfg.errors = ErrorTranslations(
            quick={'"weird key\\name"': 'value with "quote"'},
            deep={},
        )

        rendered = "\n".join(_render_config(cfg))
        loaded = tomllib.loads(rendered)

        proj = loaded["project"]
        assert proj["name"] == 'a\\b"c'
        assert proj["framework"] == 'web"x'
        assert proj["extra_context"] == ('Line1\nLine2 "quotes" \\ backslash """ triple | pipe')
        quick = loaded["modes"]["quick"]
        assert quick["label"] == 'q"l'
        assert quick["approve_for"] == ['edit"file', "x\\y"]
        assert quick["prompt"]["base"] == 'Base \\" and """ and\nnewline'
        assert quick["prompt"]["user_persona"] == "persona | with pipe\nand newline"
        assert quick["prompt"]["communication_rules"] == 'rules "quoted"'
        assert loaded["errors"]["quick"] == {'"weird key\\name"': 'value with "quote"'}

    def test_verify_section_rendered(self):
        """Non-empty verify config is rendered as a [verify] section that
        tomllib reads back with the correct field values."""
        from live_edit.cli import _render_config
        from live_edit.config import Config, VerifyConfig

        cfg = Config()
        cfg.verify = VerifyConfig(
            enabled=True,
            max_retry=5,
            test_command='pytest "tests/$(id)"',
            health_url="http://localhost:8080/health",
            semantic_enabled=True,
            semantic_assert_text=['code "quality"', "no regressions"],
        )

        rendered_lines = _render_config(cfg)
        assert "[verify]" in rendered_lines
        verify = tomllib.loads("\n".join(rendered_lines))["verify"]
        assert verify["enabled"] is True
        assert verify["max_retry"] == 5
        assert verify["test_command"] == 'pytest "tests/$(id)"'
        assert verify["health_url"] == "http://localhost:8080/health"
        assert verify["semantic_enabled"] is True
        assert verify["semantic_assert_text"] == ['code "quality"', "no regressions"]

    def test_verify_omits_empty_optional_fields(self):
        """[verify] section skips test_command/health_url/semantic_assert_text
        when they are unset."""
        from live_edit.cli import _render_config
        from live_edit.config import Config, VerifyConfig

        cfg = Config()
        cfg.verify = VerifyConfig(enabled=False, max_retry=0)

        rendered = "\n".join(_render_config(cfg))
        verify = tomllib.loads(rendered)["verify"]
        assert verify["enabled"] is False
        assert verify["max_retry"] == 0
        assert "test_command" not in verify
        assert "health_url" not in verify
        assert "semantic_assert_text" not in verify

    def test_control_chars_round_trip(self):
        """Values with newlines/tabs (errors text, post_revert) render to valid
        TOML and read back identically via tomllib."""
        from live_edit.cli import _render_config
        from live_edit.config import Config, ErrorTranslations, HooksConfig

        cfg = Config()
        cfg.hooks = HooksConfig(post_revert="echo 'line1'\necho 'line2'\t tabbed")
        cfg.errors = ErrorTranslations(
            quick={"parse error": 'line1\nline2 with "quotes"\tand tab'},
            deep={},
        )

        loaded = tomllib.loads("\n".join(_render_config(cfg)))
        assert loaded["hooks"]["post_revert"] == "echo 'line1'\necho 'line2'\t tabbed"
        assert loaded["errors"]["quick"]["parse error"] == ('line1\nline2 with "quotes"\tand tab')

    def test_leading_newline_round_trip(self):
        """extra_context starting with a newline survives the TOML round-trip
        (TOML would otherwise trim the first newline after the opening delimiter)."""
        from live_edit.cli import _render_config
        from live_edit.config import Config, ProjectConfig

        cfg = Config()
        cfg.project = ProjectConfig(
            name="t",
            language="python",
            root=".",
            extra_context="\nleading newline\n\nsecond para\n",
        )

        loaded = tomllib.loads("\n".join(_render_config(cfg)))
        assert loaded["project"]["extra_context"] == "\nleading newline\n\nsecond para\n"

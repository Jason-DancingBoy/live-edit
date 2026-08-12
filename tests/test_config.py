"""Tests for live_edit.config — config parsing, validation, auto-detection."""

from live_edit.config import (
    detect_project,
    generate_default_config,
    parse_config,
    validate_config,
)


class TestParseConfig:
    def test_minimal_config(self, tmp_path):
        toml_path = tmp_path / ".live-edit.toml"
        toml_path.write_text("""
[project]
name = "TestApp"
language = "python"
framework = "fastapi"

[llm]
api_url = "https://api.example.com/v1/messages"
api_key_env = "MY_API_KEY"
model = "claude-4"

[modes.quick]
label = "Quick"

[modes.quick.prompt]
base = "You are a helpful assistant."
""")
        config = parse_config(str(toml_path))
        assert config.project.name == "TestApp"
        assert config.project.language == "python"
        assert config.llm.api_url == "https://api.example.com/v1/messages"
        assert config.llm.api_key_env == "MY_API_KEY"
        assert config.llm.model == "claude-4"

    def test_defaults_for_optional_fields(self, tmp_path):
        toml_path = tmp_path / ".live-edit.toml"
        toml_path.write_text("""
[project]
name = "TestApp"
language = "python"

[llm]
api_url = "https://api.example.com"
api_key_env = "KEY"
model = "m1"

[modes.quick]
label = "Quick"

[modes.quick.prompt]
base = "You are helpful."
""")
        config = parse_config(str(toml_path))
        assert config.project.framework == ""
        assert config.project.root == "."
        assert config.project.extra_context == ""
        assert config.safety.allowed_dirs == ["."]
        assert config.safety.overwrite_allowed_dirs == ["static", "public", "assets"]
        assert not config.safety.allow_overwrite_existing
        assert config.timeouts.api_request == 180
        assert config.timeouts.shell_command == 30
        assert config.timeouts.approval == 300
        assert config.timeouts.final_approval == 600
        assert config.timeouts.session_ttl == 1800
        assert config.sessions.max_active == 10
        assert config.hooks.post_revert == ""
        assert config.ui.default_mode == "quick"

    def test_quick_mode_approval_defaults(self, tmp_path):
        toml_path = tmp_path / ".live-edit.toml"
        toml_path.write_text("""
[project]
name = "Test"
language = "python"

[llm]
api_url = "https://api.example.com"
api_key_env = "KEY"
model = "m1"

[modes.quick]
label = "Quick"

[modes.quick.prompt]
base = "prompt"
""")
        config = parse_config(str(toml_path))
        quick = config.modes["quick"]
        assert quick.approval == "per_tool"
        assert quick.tools == "write"
        assert quick.approve_for == ["edit_file", "write_file"]
        assert quick.prompt.user_persona == ""
        assert quick.prompt.communication_rules == ""

    def test_full_config_with_all_modes(self, tmp_path):
        toml_path = tmp_path / ".live-edit.toml"
        toml_path.write_text("""
[project]
name = "FullApp"
language = "typescript"
framework = "nextjs"
root = "./src"
extra_context = "Use tabs, not spaces."

[llm]
api_url = "https://api.openai.com/v1/chat/completions"
api_key_env = "OPENAI_API_KEY"
model = "gpt-4o"

[safety]
allowed_dirs = ["./src", "./public"]
overwrite_allowed_dirs = ["public", "styles"]
allow_overwrite_existing = true
blocked_commands = ["rm", "sudo", "chown"]
search_extensions = ["*.ts", "*.tsx", "*.css"]

[timeouts]
api_request = 120
shell_command = 60
approval = 180
final_approval = 900
session_ttl = 3600

[sessions]
max_active = 5

[hooks]
post_revert = "systemctl restart myapp"

[ui]
default_mode = "deep"

[modes.quick]
label = "快速修改"
approval = "per_tool"
tools = "write"
approve_for = ["edit_file"]

[modes.quick.prompt]
base = "You are a full-stack developer."
user_persona = "Non-technical users."
communication_rules = "No code in replies."

[modes.deep]
label = "深度开发"
approval = "final"
tools = "all"

[modes.deep.prompt]
base = "You are a senior engineer."
user_persona = "Professional developers."
communication_rules = "Use technical language."

[modes.qa]
label = "代码问答"
approval = "none"
tools = "readonly"

[modes.qa.prompt]
base = "You are a code analyst."

[errors.quick]
"old_string not found" = "File changed, retrying..."
"path out of bounds" = "Blocked."
""")
        config = parse_config(str(toml_path))
        assert config.project.name == "FullApp"
        assert config.project.framework == "nextjs"
        assert config.safety.allow_overwrite_existing
        assert config.safety.blocked_commands == ["rm", "sudo", "chown"]
        assert config.timeouts.shell_command == 60
        assert config.sessions.max_active == 5
        assert config.hooks.post_revert == "systemctl restart myapp"
        assert config.ui.default_mode == "deep"
        assert config.modes["deep"].approval == "final"
        assert config.modes["deep"].tools == "all"
        assert config.modes["qa"].tools == "readonly"
        assert config.modes["qa"].approval == "none"
        assert config.errors.quick == {
            "old_string not found": "File changed, retrying...",
            "path out of bounds": "Blocked.",
        }

    def test_timeouts_stale_worktree_ttl_default(self, tmp_path):
        toml_path = tmp_path / ".live-edit.toml"
        toml_path.write_text("""
[project]
name = "TestApp"
language = "python"

[llm]
api_url = "https://api.example.com"
api_key_env = "KEY"
model = "m1"

[modes.quick]
label = "Quick"

[modes.quick.prompt]
base = "You are helpful."
""")
        config = parse_config(str(toml_path))
        assert config.timeouts.stale_worktree_ttl == 86400

    def test_timeouts_stale_worktree_ttl_custom(self, tmp_path):
        toml_path = tmp_path / ".live-edit.toml"
        toml_path.write_text("""
[project]
name = "TestApp"
language = "python"

[llm]
api_url = "https://api.example.com"
api_key_env = "KEY"
model = "m1"

[timeouts]
stale_worktree_ttl = 3600

[modes.quick]
label = "Quick"

[modes.quick.prompt]
base = "You are helpful."
""")
        config = parse_config(str(toml_path))
        assert config.timeouts.stale_worktree_ttl == 3600


class TestValidateConfig:
    def test_valid_config_passes(self, tmp_path):
        toml_path = tmp_path / ".live-edit.toml"
        toml_path.write_text("""
[project]
name = "A"
language = "python"

[llm]
api_url = "https://x.com"
api_key_env = "K"
model = "m"

[modes.quick]
label = "Q"

[modes.quick.prompt]
base = "p"
""")
        config = parse_config(str(toml_path))
        errors = validate_config(config)
        assert errors == []

    def test_missing_quick_mode_detected(self, tmp_path):
        toml_path = tmp_path / ".live-edit.toml"
        toml_path.write_text("""
[project]
name = "A"
language = "python"

[llm]
api_url = "https://x.com"
api_key_env = "K"
model = "m"
""")
        config = parse_config(str(toml_path))
        errors = validate_config(config)
        assert any("quick" in e.lower() for e in errors)

    def test_missing_llm_fields(self, tmp_path):
        toml_path = tmp_path / ".live-edit.toml"
        toml_path.write_text("""
[project]
name = "A"
language = "python"

[llm]
api_url = ""
api_key_env = ""
model = ""

[modes.quick]
label = "Q"

[modes.quick.prompt]
base = "p"
""")
        config = parse_config(str(toml_path))
        errors = validate_config(config)
        assert any("api_url" in e.lower() for e in errors)


class TestDetectProject:
    def test_detects_python_project(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]\nname='test'\n")
        info = detect_project(str(tmp_path))
        assert info["language"] == "python"
        assert info["name"] == "test"

    def test_detects_node_project(self, tmp_path):
        (tmp_path / "package.json").write_text('{"name": "my-node-app"}')
        info = detect_project(str(tmp_path))
        assert info["language"] == "typescript"
        assert info["name"] == "my-node-app"

    def test_detects_go_project(self, tmp_path):
        (tmp_path / "go.mod").write_text("module github.com/org/repo")
        info = detect_project(str(tmp_path))
        assert info["language"] == "go"
        assert info["name"] == "github.com/org/repo"

    def test_falls_back_to_directory_name(self, tmp_path):
        info = detect_project(str(tmp_path))
        assert info["language"] == "unknown"
        assert info["name"] == tmp_path.name

    def test_detects_framework_fastapi(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            "[project]\nname='test'\ndependencies=['fastapi']\n"
        )
        info = detect_project(str(tmp_path))
        assert info["framework"] == "fastapi"

    def test_detects_git_available(self, tmp_path):
        import subprocess

        (tmp_path / "pyproject.toml").write_text("[project]\nname='test'\n")
        subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), capture_output=True)
        info = detect_project(str(tmp_path))
        assert info["vcs"] == "git"
        assert info["git_available"]

    def test_testpaths_use_shlex_quote(self, tmp_path):
        """Testpaths containing $(...) are single-quoted via shlex.quote in the
        generated test_command, so a shell=True run cannot execute command
        substitution; shlex.split round-trips to the literal original args."""
        import shlex

        (tmp_path / "pyproject.toml").write_text(
            "[project]\nname = 'test'\n"
            "[tool.pytest.ini_options]\n"
            "testpaths = ['tests/$(id)', 'tests/a b']\n"
        )
        info = detect_project(str(tmp_path))
        assert info["test_command"].startswith("python -m pytest ")
        # shlex.quote protection: the $(...) path is single-quoted in the command.
        assert shlex.quote("tests/$(id)") in info["test_command"]
        # shlex.split (shell-aware) recovers the literal args, no expansion.
        parts = shlex.split(info["test_command"])
        assert "tests/$(id)" in parts
        assert "tests/a b" in parts

    def test_testpaths_from_pytest_ini_fallback(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'test'\n")
        (tmp_path / "pytest.ini").write_text("[pytest]\n")
        info = detect_project(str(tmp_path))
        assert info["test_command"] == "python -m pytest -q --tb=short"

    def test_detects_fastapi_health_url(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            "[project]\nname = 'test'\ndependencies = ['fastapi']\n"
        )
        (tmp_path / "main.py").write_text("app = None\n")
        info = detect_project(str(tmp_path))
        assert info["framework"] == "fastapi"
        assert info["port"] == 8000
        assert info["health_url"] == "http://127.0.0.1:8000/live-edit/health"

    def test_no_health_url_without_main_module(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            "[project]\nname = 'test'\ndependencies = ['fastapi']\n"
        )
        info = detect_project(str(tmp_path))
        assert info["framework"] == "fastapi"
        assert "health_url" not in info


class TestGenerateDefaultConfig:
    def test_wires_test_command_from_detection(self, tmp_path):
        import shlex

        (tmp_path / "pyproject.toml").write_text(
            "[project]\nname = 'test'\n"
            "[tool.pytest.ini_options]\ntestpaths = ['tests/unit', 'tests/a b']\n"
        )
        cfg = generate_default_config(str(tmp_path))
        assert cfg.verify.test_command
        assert "tests/unit" in cfg.verify.test_command
        assert shlex.quote("tests/a b") in cfg.verify.test_command

    def test_wires_health_url_from_detection(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            "[project]\nname = 'test'\ndependencies = ['fastapi']\n"
        )
        (tmp_path / "main.py").write_text("app = None\n")
        cfg = generate_default_config(str(tmp_path))
        assert cfg.verify.health_url == "http://127.0.0.1:8000/live-edit/health"

    def test_verify_defaults_empty_when_undetected(self, tmp_path):
        cfg = generate_default_config(str(tmp_path))
        assert cfg.verify.test_command == ""
        assert cfg.verify.health_url == ""


class TestObservabilityConfig:
    def test_observability_defaults_when_absent(self, tmp_path):
        toml_path = tmp_path / ".live-edit.toml"
        toml_path.write_text(
            '[project]\nname = "TestApp"\nlanguage = "python"\n\n'
            '[llm]\napi_url = "https://api.example.com"\napi_key_env = "KEY"\nmodel = "m1"\n\n'
            '[modes.quick]\nlabel = "Q"\n'
        )
        config = parse_config(str(toml_path))
        assert config.observability.log_level == "INFO"
        assert config.observability.json_logs is True
        assert config.observability.metrics_enabled is True
        assert config.observability.audit_enabled is True

    def test_observability_parses_values(self, tmp_path):
        toml_path = tmp_path / ".live-edit.toml"
        toml_path.write_text(
            '[project]\nname = "TestApp"\nlanguage = "python"\n\n'
            '[llm]\napi_url = "https://api.example.com"\napi_key_env = "KEY"\nmodel = "m1"\n\n'
            '[modes.quick]\nlabel = "Q"\n\n'
            '[observability]\nlog_level = "DEBUG"\njson_logs = false\n'
            "metrics_enabled = false\naudit_enabled = false\n"
        )
        config = parse_config(str(toml_path))
        assert config.observability.log_level == "DEBUG"
        assert config.observability.json_logs is False
        assert config.observability.metrics_enabled is False
        assert config.observability.audit_enabled is False

    def test_old_config_without_observability_still_validates(self, tmp_path):
        toml_path = tmp_path / ".live-edit.toml"
        toml_path.write_text(
            '[project]\nname = "TestApp"\nlanguage = "python"\n\n'
            '[llm]\napi_url = "https://api.example.com"\napi_key_env = "KEY"\nmodel = "m1"\n\n'
            '[modes.quick]\nlabel = "Q"\n'
        )
        config = parse_config(str(toml_path))
        assert validate_config(config) == []


class TestEvaluationConfig:
    def _toml(self, extra: str = ""):
        return (
            '[project]\nname = "TestApp"\nlanguage = "python"\n\n'
            '[llm]\napi_url = "https://api.example.com"\napi_key_env = "KEY"\nmodel = "m1"\n\n'
            '[modes.quick]\nlabel = "Q"\n' + extra
        )

    def test_evaluation_defaults_when_absent(self, tmp_path):
        p = tmp_path / ".live-edit.toml"
        p.write_text(self._toml())
        config = parse_config(str(p))
        assert config.evaluation.enabled is True
        assert config.evaluation.stages == ["lint", "test", "introspect"]
        assert config.evaluation.max_retries == 3

    def test_evaluation_parses_explicit_overrides(self, tmp_path):
        p = tmp_path / ".live-edit.toml"
        p.write_text(self._toml('[evaluation]\nenabled = false\nstages = ["lint"]\n'))
        config = parse_config(str(p))
        assert config.evaluation.enabled is False
        assert config.evaluation.stages == ["lint"]


class TestCriticMaxRounds:
    def test_default_is_2(self):
        from live_edit.config import EvaluationConfig

        assert EvaluationConfig().critic_max_rounds == 2

    def test_parses_from_toml(self, tmp_path):
        from live_edit.config import parse_config

        toml_path = tmp_path / ".live-edit.toml"
        toml_path.write_text("[evaluation]\ncritic_max_rounds = 4\n")
        cfg = parse_config(str(toml_path))
        assert cfg.evaluation.critic_max_rounds == 4

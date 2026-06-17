"""Tests for live_edit.cli — live-edit init and check commands."""

import os
import pytest
from unittest.mock import patch, MagicMock


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
        assert 'language = "javascript"' in content or 'node-app' in content

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

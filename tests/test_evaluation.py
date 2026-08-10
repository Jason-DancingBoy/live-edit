"""Tests for evaluation.py"""

import pytest

from live_edit.config import Config, EvaluationConfig, PreviewConfig
from live_edit.evaluation import (
    EvalResult,
    EvalStage,
    _classify_stage_result,
    _detect_lint_cmd,
    _detect_test_cmd,
    _run_stage_lint,
    _run_stage_test,
    resolve_stages,
    run_evaluation_pipeline,
)


class TestDetectCommands:
    def test_detect_lint_python(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]\nname='test'")
        cmd = _detect_lint_cmd(str(tmp_path), None)
        assert "py_compile" in cmd

    def test_detect_test_python(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]\nname='test'")
        cmd = _detect_test_cmd(str(tmp_path), None)
        assert "pytest" in cmd

    def test_detect_unknown_project(self, tmp_path):
        cmd = _detect_lint_cmd(str(tmp_path), None)
        assert "no lint" in cmd


class TestEvalStage:
    def test_stage_values(self):
        assert EvalStage.LINT.value == "lint"
        assert EvalStage.TEST.value == "test"
        assert EvalStage.PREVIEW.value == "preview"
        assert EvalStage.INTROSPECT.value == "introspect"
        assert EvalStage.HTML_DIFF.value == "html_diff"


class TestEvalResult:
    def test_passed(self):
        r = EvalResult(passed=True, stages_passed=["lint", "test"])
        assert r.passed

    def test_failed(self):
        r = EvalResult(passed=False, stages_failed=["test"], report="test failed")
        assert not r.passed
        assert "test" in r.stages_failed

    def test_defaults(self):
        r = EvalResult(passed=True)
        assert r.stages_passed == []
        assert r.stages_failed == []
        assert r.report == ""
        assert r.retries_used == 0
        assert r.stage_details == {}
        assert r.failed_stage == ""
        assert r.failed_output == ""


class TestResolveStages:
    def test_preview_disabled_drops_preview_stages(self):
        cfg = Config(
            evaluation=EvaluationConfig(
                enabled=True,
                stages=["lint", "test", "introspect", "preview", "html_diff"],
            ),
            preview=PreviewConfig(enabled=False),
        )
        assert resolve_stages(cfg) == ["lint", "test", "introspect"]

    def test_preview_enabled_appends_preview_stages(self):
        cfg = Config(
            evaluation=EvaluationConfig(enabled=True, stages=["lint", "test", "introspect"]),
            preview=PreviewConfig(enabled=True),
        )
        assert resolve_stages(cfg) == ["lint", "test", "preview", "introspect", "html_diff"]

    def test_canonical_order(self):
        cfg = Config(
            evaluation=EvaluationConfig(enabled=True, stages=["introspect", "test", "lint"])
        )
        assert resolve_stages(cfg) == ["lint", "test", "introspect"]

    def test_unknown_stages_filtered(self):
        cfg = Config(evaluation=EvaluationConfig(enabled=True, stages=["lint", "future_stage"]))
        assert resolve_stages(cfg) == ["lint"]


class TestEvalResultSkipped:
    def test_defaults_include_stages_skipped(self):
        r = EvalResult(passed=True)
        assert r.stages_skipped == []


@pytest.mark.asyncio
class TestPipelineThreeState:
    async def test_skipped_stage_continues(self, monkeypatch):
        import live_edit.evaluation as ev

        class FakeSession:
            def __init__(self):
                self._worktree_path = "/tmp/fake"
                self._preview_url = ""
                self.request = "fix it"
                self._cached_diff = ""
                self.events = []

            def emit(self, event_type, **data):
                self.events.append({"type": event_type, **data})

        async def _lint(root, config):
            return {"ok": True, "output": ""}

        async def _test(root, config):
            return {"ok": False, "skipped": True, "output": "no tests"}

        async def _introspect(provider, req, diff, **kwargs):
            return {"ok": True, "output": ""}

        monkeypatch.setattr(ev, "_run_stage_lint", _lint)
        monkeypatch.setattr(ev, "_run_stage_test", _test)
        monkeypatch.setattr(ev, "_run_stage_introspect", _introspect)

        cfg = Config(
            evaluation=EvaluationConfig(enabled=True, stages=["lint", "test", "introspect"]),
            preview=PreviewConfig(enabled=False),
        )
        sess = FakeSession()
        result = await run_evaluation_pipeline(sess, None, cfg, tool_registry=None)
        assert result.passed is True
        assert result.stages_skipped == ["test"]
        assert result.stages_passed == ["lint", "introspect"]
        assert any(
            e["type"] == "eval_stage" and e["stage"] == "test" and e["status"] == "skipped"
            for e in sess.events
        )

    async def test_fail_short_circuits(self, monkeypatch):
        import live_edit.evaluation as ev

        class FakeSession:
            def __init__(self):
                self._worktree_path = "/tmp/fake"
                self._preview_url = ""
                self.request = "fix it"
                self._cached_diff = ""
                self.events = []

            def emit(self, event_type, **data):
                self.events.append({"type": event_type, **data})

        async def _lint(root, config):
            return {"ok": False, "output": "syntax error"}

        async def _test(root, config):
            return {"ok": True, "output": ""}

        monkeypatch.setattr(ev, "_run_stage_lint", _lint)
        monkeypatch.setattr(ev, "_run_stage_test", _test)

        cfg = Config(
            evaluation=EvaluationConfig(enabled=True, stages=["lint", "test"]),
            preview=PreviewConfig(enabled=False),
        )
        sess = FakeSession()
        result = await run_evaluation_pipeline(sess, None, cfg, tool_registry=None)
        assert result.passed is False
        assert result.failed_stage == "lint"
        assert result.stages_passed == []
        assert result.stages_failed == ["lint"]


class TestClassifyStageResult:
    def test_python_passed(self):
        assert _classify_stage_result("python", 0, "3 passed") == "passed"

    def test_python_no_tests_skipped(self):
        assert _classify_stage_result("python", 5, "no tests ran") == "skipped"

    def test_python_pytest_missing_skipped(self):
        out = "ModuleNotFoundError: No module named 'pytest'"
        assert _classify_stage_result("python", 1, out) == "skipped"

    def test_python_command_not_found_skipped(self):
        out = "/bin/sh: python3: command not found"
        assert _classify_stage_result("python", 127, out) == "skipped"

    def test_python_failure(self):
        assert _classify_stage_result("python", 1, "3 failed") == "failed"

    def test_node_missing_script_skipped(self):
        out = 'Missing script: "test"'
        assert _classify_stage_result("node", 1, out) == "skipped"

    def test_go_no_test_files_skipped(self):
        out = "?   github.com/x/pkg [no test files]"
        assert _classify_stage_result("go", 0, out) == "skipped"

    def test_go_passed(self):
        assert _classify_stage_result("go", 0, "ok") == "passed"

    def test_lint_command_not_found_skipped(self):
        assert _classify_stage_result("lint", 127, "command not found") == "skipped"

    def test_lint_failure(self):
        assert _classify_stage_result("lint", 1, "SyntaxError") == "failed"


class TestDetectCommandsNoMasking:
    def test_detect_test_python_has_no_or_fallback(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]\nname='test'")
        cmd = _detect_test_cmd(str(tmp_path), None)
        assert "pytest" in cmd
        assert "||" not in cmd

    def test_detect_lint_python_has_no_or_fallback(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]\nname='test'")
        cmd = _detect_lint_cmd(str(tmp_path), None)
        assert "py_compile" in cmd
        assert "||" not in cmd


class TestLintEmptyPyDiff:
    def test_lint_passes_when_only_non_py_changes_staged(self, tmp_path):
        import subprocess as sp

        sp.run(["git", "init", "-q"], cwd=str(tmp_path), check=True)
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
        (tmp_path / "README.md").write_text("hi\n")
        sp.run(["git", "add", "-A"], cwd=str(tmp_path), check=True)
        sp.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init"],
            cwd=str(tmp_path),
            check=True,
        )
        (tmp_path / "README.md").write_text("hi\nworld\n")  # only a non-.py change
        sp.run(["git", "add", "README.md"], cwd=str(tmp_path), check=True)

        cmd = _detect_lint_cmd(str(tmp_path), None)
        result = sp.run(cmd, shell=True, capture_output=True, text=True, cwd=str(tmp_path))
        assert result.returncode == 0
        assert "py_compile.py" not in (result.stdout + result.stderr)


@pytest.mark.asyncio
class TestRunStageClassification:
    async def test_run_stage_test_skips_no_tests(self, monkeypatch, tmp_path):
        import live_edit.evaluation as ev

        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'")

        class FakeProc:
            returncode = 5
            stdout = "no tests ran"
            stderr = ""

        monkeypatch.setattr(ev.subprocess, "run", lambda *a, **k: FakeProc())
        result = await _run_stage_test(str(tmp_path), None)
        assert result["skipped"] is True
        assert result["ok"] is False

    async def test_run_stage_lint_fails(self, monkeypatch, tmp_path):
        import live_edit.evaluation as ev

        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'")

        class FakeProc:
            returncode = 1
            stdout = "SyntaxError: invalid syntax"
            stderr = ""

        monkeypatch.setattr(ev.subprocess, "run", lambda *a, **k: FakeProc())
        result = await _run_stage_lint(str(tmp_path), None)
        assert result["ok"] is False
        assert result["skipped"] is False


@pytest.mark.asyncio
class TestIntrospectStage:
    async def test_blocking_findings_fail_stage(self):
        from live_edit.evaluation import _run_stage_introspect

        class FakeProvider:
            async def call_with_tools(self, messages, tools, on_thinking=None, on_text=None):
                return [
                    {
                        "type": "text",
                        "text": (
                            '{"goal_achieved": true, "summary": "x", '
                            '"findings": [{"severity": "high", "file": "a.py", '
                            '"line": 5, "description": "broken caller"}]}'
                        ),
                    }
                ]

        result = await _run_stage_introspect(
            FakeProvider(), "user wants batch delete", "diff",
            worktree_path="/tmp/wt", tool_registry=None, critic_max_rounds=2,
        )
        assert result["ok"] is False
        assert "[high]" in result["output"]

    async def test_goal_not_achieved_fails_stage(self):
        from live_edit.evaluation import _run_stage_introspect

        class FakeProvider:
            async def call_with_tools(self, messages, tools, on_thinking=None, on_text=None):
                return [
                    {
                        "type": "text",
                        "text": (
                            '{"goal_achieved": false, "summary": "batch delete missing", "findings": []}'
                        ),
                    }
                ]

        result = await _run_stage_introspect(
            FakeProvider(), "add batch delete", "diff",
            worktree_path="/tmp/wt", tool_registry=None,
        )
        assert result["ok"] is False

    async def test_clean_verdict_passes(self):
        from live_edit.evaluation import _run_stage_introspect

        class FakeProvider:
            async def call_with_tools(self, messages, tools, on_thinking=None, on_text=None):
                return [
                    {
                        "type": "text",
                        "text": '{"goal_achieved": true, "summary": "ok", "findings": []}',
                    }
                ]

        result = await _run_stage_introspect(
            FakeProvider(), "fix it", "diff",
            worktree_path="/tmp/wt", tool_registry=None,
        )
        assert result["ok"] is True

    async def test_pipeline_threads_tool_registry(self, monkeypatch):
        import live_edit.evaluation as ev

        class FakeSession:
            def __init__(self):
                self._worktree_path = "/tmp/fake"
                self._preview_url = ""
                self.request = "fix it"
                self._cached_diff = "diff"
                self.events = []

            def emit(self, event_type, **data):
                self.events.append({"type": event_type, **data})

        seen = {}

        async def _introspect(provider, req, diff, **kwargs):
            seen.update(kwargs)
            return {"ok": True, "output": ""}

        monkeypatch.setattr(ev, "_run_stage_introspect", _introspect)

        from live_edit.config import Config, EvaluationConfig, PreviewConfig

        cfg = Config(
            evaluation=EvaluationConfig(enabled=True, stages=["introspect"]),
            preview=PreviewConfig(enabled=False),
        )
        sess = FakeSession()
        await ev.run_evaluation_pipeline(sess, None, cfg, tool_registry="REG")
        assert seen.get("tool_registry") == "REG"
        assert seen.get("worktree_path") == "/tmp/fake"
        assert seen.get("critic_max_rounds") == 2

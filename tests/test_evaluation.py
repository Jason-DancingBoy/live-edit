"""Tests for evaluation.py"""

import pytest

from live_edit.config import Config, EvaluationConfig, PreviewConfig
from live_edit.evaluation import (
    EvalResult,
    EvalStage,
    _detect_lint_cmd,
    _detect_test_cmd,
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

        async def _introspect(provider, req, diff):
            return {"ok": True, "output": ""}

        monkeypatch.setattr(ev, "_run_stage_lint", _lint)
        monkeypatch.setattr(ev, "_run_stage_test", _test)
        monkeypatch.setattr(ev, "_run_stage_introspect", _introspect)

        cfg = Config(
            evaluation=EvaluationConfig(enabled=True, stages=["lint", "test", "introspect"]),
            preview=PreviewConfig(enabled=False),
        )
        sess = FakeSession()
        result = await run_evaluation_pipeline(sess, None, cfg)
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
        result = await run_evaluation_pipeline(sess, None, cfg)
        assert result.passed is False
        assert result.failed_stage == "lint"
        assert result.stages_passed == []
        assert result.stages_failed == ["lint"]

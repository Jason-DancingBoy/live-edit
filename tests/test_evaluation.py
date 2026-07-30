"""Tests for evaluation.py"""

from live_edit.evaluation import (
    EvalResult,
    EvalStage,
    _detect_lint_cmd,
    _detect_test_cmd,
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

# 评估管线默认开启 · 为质量兜底 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让编辑后评估管线默认开启(面向非技术用户兜质量),preview 类关卡自适应、跳过不误报、真实失败能进入自愈,自愈仍失败时在对话里明确告知用户。

**Architecture:** ① `evaluation.py` 引入纯函数 `resolve_stages(config)` 计算"有效关卡"(preview/html_diff 仅当 `[preview].enabled` 时进入),关卡结果从二态扩为三态 `passed/skipped/failed`;② 修掉 `_detect_lint_cmd`/`_detect_test_cmd` 里 `|| echo '...'` 吞失败的问题,按返回码+输出启发式分类;③ `engine.py` 首次评估前先填 `_cached_diff`、自愈仍失败时在对话追加大白话收尾;④ `config.py` 默认 `enabled=True`、`stages=["lint","test","introspect"]`;⑤ 前端补 `skipped` 圆点状态。

**Tech Stack:** Python 3.10–3.12、pytest + pytest-asyncio、ruff v0.6.0(+ ruff-format)、FastAPI SSE、原生 JS/CSS 前端。不引入新依赖。

## Global Constraints

- 兼容 Python 3.10–3.12;不新增第三方依赖。
- 代码风格:ruff v0.6.0 + ruff-format(pre-commit 会自动跑 `ruff --fix` + `ruff-format`,提交必须通过)。
- 禁止硬编码密钥/token;不新增配置字段(复用现有 `[evaluation]` / `[preview]` 段)。
- 面向用户的中文文案沿用 quick 模式 persona(大白话、不展示代码/行号)。
- 每个任务以"跑测试 → 提交"收尾;提交信息遵循仓库 conventional commits(scope: `evaluation` / `engine` / `config` 等)。

---

### Task 1: evaluation.py — `resolve_stages` + 三态结果 + 管线跳过语义

**Files:**
- Modify: `live_edit/evaluation.py:24-33`(EvalResult)、`:102-118`(preview runner)、`:196-267`(run_evaluation_pipeline)、新增 `resolve_stages` 常量与函数
- Test: `tests/test_evaluation.py`

**Interfaces:**
- Consumes: 现有 `EvalResult`(新增字段 `stages_skipped`)、`config.evaluation.stages`、`config.preview.enabled`
- Produces:
  - `resolve_stages(config) -> list[str]` — 有效关卡列表,规范顺序
  - stage runner 返回 dict 新增可选键 `skipped: bool`;管线判定顺序固定:**先查 `skipped`,再查 `ok`,否则失败**
  - `EvalResult.stages_skipped: list[str]`

- [ ] **Step 1: 写失败的测试**

在 `tests/test_evaluation.py` 追加:

```python
from live_edit.config import Config, EvaluationConfig, PreviewConfig
from live_edit.evaluation import (
    EvalResult,
    resolve_stages,
    run_evaluation_pipeline,
)


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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/pytest tests/test_evaluation.py -v`
Expected: FAIL — `resolve_stages` 不存在(ImportError),`EvalResult` 无 `stages_skipped`。

- [ ] **Step 3: 实现**

在 `live_edit/evaluation.py` 顶部(EvalStage 之前)加常量与 `resolve_stages`:

```python
STAGE_ORDER = {"lint": 0, "test": 1, "preview": 2, "introspect": 3, "html_diff": 4}
PREVIEW_STAGES = ("preview", "html_diff")


def resolve_stages(config) -> list[str]:
    """Effective stage list in canonical order; preview stages conditional on [preview].enabled."""
    if config is None or not hasattr(config, "evaluation"):
        return []
    base = config.evaluation.stages
    stages = set(base) & set(STAGE_ORDER)
    if config.preview.enabled if hasattr(config, "preview") else False:
        stages |= set(PREVIEW_STAGES)
    else:
        stages -= set(PREVIEW_STAGES)
    return sorted(stages, key=STAGE_ORDER.__getitem__)
```

`EvalResult` 增加字段(`:29` 后):

```python
    stages_skipped: list[str] = field(default_factory=list)
```

`_run_stage_preview` 无 URL 时改为跳过(`:104-105`):

```python
    if not health_url:
        return {"ok": False, "skipped": True, "output": "Preview URL not available"}
```

重写 `run_evaluation_pipeline`(`:196-267`)为:

```python
async def run_evaluation_pipeline(session, provider, config, preview_manager=None) -> EvalResult:
    """Run all evaluation stages, stop at first failure. No retry loop — that's in engine.py."""
    stages = resolve_stages(config)
    if not stages:
        return EvalResult(passed=True, report="Evaluation disabled")

    stage_runners = {
        "lint": lambda: _run_stage_lint(session._worktree_path, config),
        "test": lambda: _run_stage_test(session._worktree_path, config),
        "preview": lambda: _run_stage_preview(session._preview_url),
        "introspect": lambda: _run_stage_introspect(
            provider, session.request, getattr(session, "_cached_diff", "")
        ),
        "html_diff": lambda: _run_stage_html_diff(
            session._preview_url,
            config.evaluation.preview_pages if hasattr(config, "evaluation") else ["/"],
        ),
    }

    stage_details = {}
    failed_stage = None
    failed_output = ""
    stages_passed: list[str] = []
    stages_skipped: list[str] = []

    for stage_name in stages:
        if stage_name not in stage_runners:
            continue
        session.emit("eval_stage", stage=stage_name, status="running")
        try:
            result = await stage_runners[stage_name]()
        except Exception as e:
            result = {"ok": False, "output": str(e)}
        stage_details[stage_name] = result
        if result.get("skipped"):
            stages_skipped.append(stage_name)
            session.emit("eval_stage", stage=stage_name, status="skipped")
        elif result.get("ok"):
            stages_passed.append(stage_name)
            session.emit("eval_stage", stage=stage_name, status="passed")
        else:
            session.emit(
                "eval_stage",
                stage=stage_name,
                status="failed",
                error=result.get("output", "")[:500],
            )
            failed_stage = stage_name
            failed_output = result.get("output", "")
            break

    if failed_stage is None:
        skip_note = "（跳过: " + "、".join(stages_skipped) + "）" if stages_skipped else ""
        session.emit("eval_complete", passed=True, report=f"所有检查通过{skip_note}")
        return EvalResult(
            passed=True,
            stages_passed=stages_passed,
            stages_skipped=stages_skipped,
            report=f"所有检查通过{skip_note}",
            retries_used=0,
            stage_details=stage_details,
        )

    report_parts = []
    for s in stages:
        detail = stage_details.get(s, {})
        if detail.get("skipped"):
            status = "跳过"
        else:
            status = "通过" if detail.get("ok") else "未通过"
        report_parts.append(f"- {s}: {status}")
    report = "评估未通过:\n" + "\n".join(report_parts)

    return EvalResult(
        passed=False,
        stages_passed=stages_passed,
        stages_skipped=stages_skipped,
        stages_failed=[failed_stage],
        report=report,
        retries_used=0,
        stage_details=stage_details,
        failed_stage=failed_stage,
        failed_output=failed_output,
    )
```

> 注意:原先 `stages_passed=[s for s in stages if stage_details...]` 被显式累加列表替代,以正确排除 skipped。

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/pytest tests/test_evaluation.py -v`
Expected: PASS(含新增 6 个用例;原有 `TestDetectCommands`、`TestEvalStage`、`TestEvalResult.test_defaults` 仍通过)。

- [ ] **Step 5: 质量闸门 + 提交**

```bash
.venv/bin/ruff check live_edit/evaluation.py tests/test_evaluation.py
.venv/bin/ruff format live_edit/evaluation.py tests/test_evaluation.py
git add live_edit/evaluation.py tests/test_evaluation.py
git commit -m "feat(evaluation): resolve_stages + three-state stage outcomes"
```

---

### Task 2: evaluation.py — 修 test/lint「吞失败」+ 结果分类

**Files:**
- Modify: `live_edit/evaluation.py:36-63`(detect 命令)、`:66-99`(lint/test runner)
- Test: `tests/test_evaluation.py`

**Interfaces:**
- Consumes: `config.evaluation.test_command` / `lint_command`(覆盖);现有 `_detect_lint_cmd`/`_detect_test_cmd` 字符串返回(签名不变)
- Produces:
  - `_classify_stage_result(lang: str, returncode: int, output: str) -> str` — 返回 `"passed" | "skipped" | "failed"`
  - `_run_stage_lint`/`_run_stage_test` 返回 `{"ok": bool, "skipped": bool, "output": str, "command": str}`

- [ ] **Step 1: 写失败的测试**

在 `tests/test_evaluation.py` 追加:

```python
from live_edit.evaluation import _classify_stage_result, _run_stage_lint, _run_stage_test


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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/pytest tests/test_evaluation.py -v`
Expected: FAIL — `_classify_stage_result` 不存在;`test_detect_*_has_no_or_fallback` 因 `||` 仍存在而失败。

- [ ] **Step 3: 实现**

在 `evaluation.py` 加分类器:

```python
_SKIP_MARKERS = {
    "lint": ("command not found",),
    "python": ("modulenotfounderror", "no module named 'pytest'", "command not found"),
    "node": ("missing script: test", "command not found"),
    "go": ("[no test files]", "command not found"),
}


def _classify_stage_result(lang: str, returncode: int, output: str) -> str:
    """Classify a subprocess outcome as passed / skipped / failed."""
    low = output.lower()
    for marker in _SKIP_MARKERS.get(lang, ()):
        if marker in low or marker in output:
            return "skipped"
    if lang == "python" and returncode == 5:
        return "skipped"
    if returncode == 0:
        return "passed"
    return "failed"
```

改 `_detect_lint_cmd`(`:41-45`)去掉 `|| echo`:

```python
    if os.path.exists(os.path.join(project_root, "pyproject.toml")):
        return "python3 -m py_compile $(git diff --cached --name-only --diff-filter=ACM '*.py' 2>/dev/null) 2>&1"
```

改 `_detect_test_cmd`(`:57-58`)去掉 `|| echo`:

```python
    if os.path.exists(os.path.join(project_root, "pyproject.toml")):
        return "python3 -m pytest -x --tb=short 2>&1"
    if os.path.exists(os.path.join(project_root, "package.json")):
        return "npm test 2>&1"
```

重写 `_run_stage_lint`(`:66-81`):

```python
async def _run_stage_lint(project_root: str, config) -> dict:
    cmd = _detect_lint_cmd(project_root, config)
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=60,
            cwd=project_root,
        )
        output = (result.stdout + result.stderr)[:2000]
        outcome = _classify_stage_result("lint", result.returncode, output)
        return {"ok": outcome == "passed", "skipped": outcome == "skipped", "output": output, "command": cmd}
    except subprocess.TimeoutExpired:
        return {"ok": False, "skipped": False, "output": "Lint check timed out", "command": cmd}
```

重写 `_run_stage_test`(`:84-99`):

```python
async def _run_stage_test(project_root: str, config) -> dict:
    cmd = _detect_test_cmd(project_root, config)
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=120,
            cwd=project_root,
        )
        output = (result.stdout + result.stderr)[:3000]
        lang = "python" if "pytest" in cmd else ("node" if "npm test" in cmd else "go")
        outcome = _classify_stage_result(lang, result.returncode, output)
        return {"ok": outcome == "passed", "skipped": outcome == "skipped", "output": output, "command": cmd}
    except subprocess.TimeoutExpired:
        return {"ok": False, "skipped": False, "output": "Test execution timed out", "command": cmd}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/pytest tests/test_evaluation.py -v`
Expected: PASS(新增用例全过;`test_detect_test_python`/`test_detect_lint_python`/`test_detect_unknown_project` 仍过)。

- [ ] **Step 5: 质量闸门 + 提交**

```bash
.venv/bin/ruff check live_edit/evaluation.py tests/test_evaluation.py
.venv/bin/ruff format live_edit/evaluation.py tests/test_evaluation.py
git add live_edit/evaluation.py tests/test_evaluation.py
git commit -m "fix(evaluation): classify lint/test outcomes, drop || echo masking"
```

---

### Task 3: engine.py — 首次评估前填 `_cached_diff` + `eval_started` 用有效关卡

**Files:**
- Modify: `live_edit/engine.py:13`(import)、`:1024-1037`(eval block 开头)
- Test: `tests/test_engine.py`

**Interfaces:**
- Consumes: `resolve_stages(config)`(Task 1)、`session._worktree_path`(已定义为局部 `_root`,`engine.py:581`)
- Produces: 首个 `run_evaluation_pipeline` 调用前 `session._cached_diff` 非空;`eval_started` 事件携带有效关卡列表

- [ ] **Step 1: 写失败的测试**

在 `tests/test_engine.py` 追加(文件顶部 import 已含 `run_edit_session`/`EditSession`/`SessionStore`/`_make_test_config`):

```python
@pytest.mark.asyncio
class TestEvalDiffPopulation:
    async def test_eval_populates_cached_diff_before_first_run(self, tmp_path, monkeypatch):
        import subprocess as sp

        from live_edit.config import EvaluationConfig
        from live_edit.evaluation import EvalResult
        import live_edit.engine as eng

        sp.run(["git", "init", "-q"], cwd=str(tmp_path), check=True)
        p = tmp_path / "a.py"
        p.write_text("x = 1\n")
        sp.run(["git", "add", "-A"], cwd=str(tmp_path), check=True)
        sp.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init"],
            cwd=str(tmp_path),
            check=True,
        )
        p.write_text("x = 2\n")  # uncommitted change

        captured = {}

        async def _fake_pipeline(session, provider, config, preview_manager=None):
            captured["diff"] = session._cached_diff
            return EvalResult(passed=True)

        monkeypatch.setattr(eng, "run_evaluation_pipeline", _fake_pipeline)

        config = _make_test_config()
        config.evaluation = EvaluationConfig(enabled=True, max_retries=0, stages=["introspect"])
        mock_vcs = MagicMock()
        mock_vcs.create_worktree.return_value = str(tmp_path)
        mock_storage = MagicMock()
        mock_storage.save_session = MagicMock()

        session = EditSession("s1", "change x")
        session._modified_files = ["a.py"]
        store = SessionStore(max_active=10, ttl_seconds=3600)
        store.add(session)

        await run_edit_session(
            session=session,
            provider=FakeProvider([[{"type": "text", "text": "Done"}]]),
            vcs=mock_vcs,
            storage=mock_storage,
            config=config,
            mode="deep",
            session_store=store,
        )
        assert "x = 2" in captured["diff"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/pytest tests/test_engine.py::TestEvalDiffPopulation -v`
Expected: FAIL — `captured["diff"]` 为空(`_cached_diff` 首轮未被填充)。

- [ ] **Step 3: 实现**

`engine.py:13` 导入加 `resolve_stages`:

```python
from .evaluation import run_evaluation_pipeline, resolve_stages
```

`engine.py` eval block 开头(`:1033-1037`)改为:

```python
            import subprocess as _sp2

            session.emit("eval_started", stages=resolve_stages(config))
            # Populate the cached diff BEFORE the first evaluation so the
            # introspect stage sees the actual changes (it was empty on first run).
            _sp2.run(
                ["git", "-C", _root, "add", "-A"], capture_output=True, text=True, timeout=10
            )
            _diff0 = _sp2.run(
                ["git", "-C", _root, "diff", "--cached"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            session._cached_diff = _diff0.stdout.strip()
            max_eval_retries = config.evaluation.max_retries
            retry = 0
            eval_result = None
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/pytest tests/test_engine.py::TestEvalDiffPopulation tests/test_engine.py::test_text_only_response -v`
Expected: PASS(新用例 + 既有 smoke 用例不回归)。

- [ ] **Step 5: 质量闸门 + 提交**

```bash
.venv/bin/ruff check live_edit/engine.py tests/test_engine.py
.venv/bin/ruff format live_edit/engine.py tests/test_engine.py
git add live_edit/engine.py tests/test_engine.py
git commit -m "fix(engine): populate cached diff before first evaluation"
```

---

### Task 4: engine.py — 自愈仍失败时对话追加收尾文案

**Files:**
- Modify: `live_edit/engine.py`(新增 `EVAL_STAGE_LABELS` + `_eval_failure_note`,改 `:1082-1087`)
- Test: `tests/test_engine.py`

**Interfaces:**
- Consumes: `EvalResult.failed_stage`;`session.messages` / `session.emit`
- Produces: `_eval_failure_note(failed_stage: str) -> str`;失败时追加 assistant 消息 + `text` 事件

- [ ] **Step 1: 写失败的测试**

在 `tests/test_engine.py` 追加:

```python
@pytest.mark.asyncio
class TestEvalFailureNote:
    async def test_eval_failure_appends_conversation_note(self, monkeypatch):
        from live_edit.config import EvaluationConfig
        from live_edit.evaluation import EvalResult
        import live_edit.engine as eng

        config = _make_test_config()
        config.evaluation = EvaluationConfig(enabled=True, max_retries=0, stages=["lint"])

        async def _fake_pipeline(session, provider, config, preview_manager=None):
            return EvalResult(passed=False, failed_stage="test", failed_output="boom")

        monkeypatch.setattr(eng, "run_evaluation_pipeline", _fake_pipeline)

        mock_vcs = MagicMock()
        mock_vcs.create_worktree.return_value = "/tmp/evalnote"
        mock_storage = MagicMock()
        mock_storage.save_session = MagicMock()

        session = EditSession("s1", "fix it")
        session._modified_files = ["x.py"]
        store = SessionStore(max_active=10, ttl_seconds=3600)
        store.add(session)

        await run_edit_session(
            session=session,
            provider=FakeProvider([[{"type": "text", "text": "Done"}]]),
            vcs=mock_vcs,
            storage=mock_storage,
            config=config,
            mode="deep",
            session_store=store,
        )

        events = _drain_queue(session)
        assert any(
            e["type"] == "text" and "自动检查没通过" in e.get("text", "") for e in events
        )
        assert any(
            e["type"] == "text" and "测试" in e.get("text", "") for e in events
        )
        last = session.messages[-1]
        assert last["role"] == "assistant"
        assert "自动检查没通过" in last["content"][0]["text"]

    async def test_eval_passed_no_note(self, monkeypatch):
        from live_edit.config import EvaluationConfig
        from live_edit.evaluation import EvalResult
        import live_edit.engine as eng

        config = _make_test_config()
        config.evaluation = EvaluationConfig(enabled=True, max_retries=0, stages=["lint"])

        async def _fake_pipeline(session, provider, config, preview_manager=None):
            return EvalResult(passed=True)

        monkeypatch.setattr(eng, "run_evaluation_pipeline", _fake_pipeline)

        mock_vcs = MagicMock()
        mock_vcs.create_worktree.return_value = "/tmp/evalok"
        mock_storage = MagicMock()
        mock_storage.save_session = MagicMock()

        session = EditSession("s1", "fix it")
        session._modified_files = ["x.py"]
        store = SessionStore(max_active=10, ttl_seconds=3600)
        store.add(session)

        await run_edit_session(
            session=session,
            provider=FakeProvider([[{"type": "text", "text": "Done"}]]),
            vcs=mock_vcs,
            storage=mock_storage,
            config=config,
            mode="deep",
            session_store=store,
        )

        events = _drain_queue(session)
        assert not any(
            e["type"] == "text" and "自动检查没通过" in e.get("text", "") for e in events
        )

    def test_eval_failure_note_plain(self):
        from live_edit.engine import _eval_failure_note

        note = _eval_failure_note("test")
        assert "测试" in note
        assert "自动检查没通过" in note
        assert _eval_failure_note("unknown_stage").startswith("不过")
```

> 若 `run_edit_session` 因 `/tmp/evalnote` 不存在而报错,Step 3 里改用一个存在的临时目录(`tmp_path` fixture),并把断言逻辑保持不变。

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/pytest tests/test_engine.py::TestEvalFailureNote -v`
Expected: FAIL — 无 `_eval_failure_note`;无 note 事件/消息。

- [ ] **Step 3: 实现**

在 `engine.py` 顶部(import 之后)加:

```python
EVAL_STAGE_LABELS = {
    "lint": "代码检查",
    "test": "测试",
    "preview": "预览",
    "introspect": "AI 自省",
    "html_diff": "页面对比",
}


def _eval_failure_note(failed_stage: str) -> str:
    label = EVAL_STAGE_LABELS.get(failed_stage, failed_stage)
    return (
        "不过有几项自动检查没通过(主要是 "
        f"{label})。我自动修复了几次还没完全解决。"
        "改动已经保留,你可以再描述一遍问题,或先看看改动的文件。"
    )
```

改 `engine.py:1082-1087` 尾部:

```python
            if eval_result and not eval_result.passed:
                note = _eval_failure_note(eval_result.failed_stage)
                session.messages.append(
                    {"role": "assistant", "content": [{"type": "text", "text": note}]}
                )
                session.emit("text", text=note)
                session.emit(
                    "eval_complete",
                    passed=False,
                    report=f"评估未完全通过（已达最大重试次数 {max_eval_retries}）",
                )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/pytest tests/test_engine.py::TestEvalFailureNote -v`
Expected: PASS。若 `/tmp/evalnote` 路径问题导致失败,改用 `tmp_path` fixture 建真实目录并重跑。

- [ ] **Step 5: 质量闸门 + 提交**

```bash
.venv/bin/ruff check live_edit/engine.py tests/test_engine.py
.venv/bin/ruff format live_edit/engine.py tests/test_engine.py
git add live_edit/engine.py tests/test_engine.py
git commit -m "feat(engine): plain-language eval-failure note in conversation"
```

---

### Task 5: 前端 — `skipped` 圆点状态

**Files:**
- Modify: `live_edit/static/live-edit.js:362-374`(`_updateEvalStage`)
- Modify: `live_edit/static/live-edit.css:398-404`(dot 样式区)

**Interfaces:**
- Consumes: 后端 `eval_stage` 事件新增 `status="skipped"`
- Produces: skipped 关卡显示灰点(不再误标红)

- [ ] **Step 1: 改 JS**

`live-edit.js` 的 `_updateEvalStage`(`:362-374`)在 `passed` 分支后、else 前插入:

```js
    } else if (status === "skipped") {
      dot.className = "le-eval-dot skipped";
    }
```

- [ ] **Step 2: 改 CSS**

`live-edit.css` 在 `.le-eval-dot.failed`(`:404`)后追加:

```css
.le-eval-dot.skipped { background: #9ca3af; }
```

- [ ] **Step 3: 静态验证**

Run:
```bash
grep -n "skipped" live_edit/static/live-edit.js
grep -n "le-eval-dot.skipped" live_edit/static/live-edit.css
```
Expected: 两处各出现 `skipped`。仓库无 JS 测试基建,UI 行为靠人工验证(手动起服务改一个无测试项目,观察 test 关显示灰点)。

- [ ] **Step 4: 提交**

```bash
git add live_edit/static/live-edit.js live_edit/static/live-edit.css
git commit -m "feat(ui): render skipped eval stages as gray dots"
```

---

### Task 6: config.py — 默认值翻转 + 测试爆炸半径

**Files:**
- Modify: `live_edit/config.py:98-108`(EvaluationConfig 默认值)
- Modify: `tests/test_config.py`(新增断言)
- Modify: `tests/test_engine.py:1091+`(`_make_test_config` 显式关闭评估)
- Test: `tests/test_config.py`、全量 `pytest`

**Interfaces:**
- Consumes: Task 1-4 已完成(翻转开关落在正确代码上)
- Produces: 新项目默认 `enabled=True`、`stages=["lint","test","introspect"]`;既有引擎测试保持免评估

- [ ] **Step 1: 写失败的测试**

在 `tests/test_config.py` 追加 `TestEvaluationConfig` 类(复用 `tmp_path` toml 写法):

```python
class TestEvaluationConfig:
    def _toml(self, extra: str = ""):
        return (
            '[project]\nname = "TestApp"\nlanguage = "python"\n\n'
            '[llm]\napi_url = "https://api.example.com"\napi_key_env = "KEY"\nmodel = "m1"\n\n'
            '[modes.quick]\nlabel = "Q"\n'
            + extra
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
        p.write_text(self._toml("[evaluation]\nenabled = false\nstages = [\"lint\"]\n"))
        config = parse_config(str(p))
        assert config.evaluation.enabled is False
        assert config.evaluation.stages == ["lint"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/pytest tests/test_config.py::TestEvaluationConfig -v`
Expected: FAIL — `enabled is True` 断言失败(当前默认 `False`)。

- [ ] **Step 3: 改 config.py 默认值**

`config.py:99-108` 改为:

```python
@dataclass
class EvaluationConfig:
    enabled: bool = True
    max_retries: int = 3
    stages: list[str] = field(
        default_factory=lambda: ["lint", "test", "introspect"]
    )
    test_command: str = ""
    lint_command: str = ""
    screenshot: bool = False
    preview_pages: list[str] = field(default_factory=lambda: ["/"])
```

- [ ] **Step 4: 封住既有测试爆炸半径**

`tests/test_engine.py` 顶部 import 加 `EvaluationConfig`;`_make_test_config()`(`:1091+`)的 `Config(...)` 加一个参数,显式关评估,保持既有引擎测试免评估:

```python
from live_edit.config import EvaluationConfig, ...
...
    return Config(
        ...
        evaluation=EvaluationConfig(enabled=False),
    )
```

- [ ] **Step 5: 跑测试确认通过 + 全量回归**

Run:
```bash
.venv/bin/pytest tests/test_config.py -v
.venv/bin/pytest tests/test_evaluation.py -v
.venv/bin/pytest tests/test_engine.py -v
```
Expected: 三文件全过。

Run:
```bash
.venv/bin/pytest -x -q
```
Expected: 全量通过。若 `tests/test_router.py` / `tests/test_observability_endpoints.py` 因默认开启评估出现失败或明显变慢,在这些 fixture 的 `.live-edit.toml` 里加:

```toml
[evaluation]
enabled = false
```

- [ ] **Step 6: 质量闸门 + 提交**

```bash
.venv/bin/ruff check live_edit/config.py tests/test_config.py tests/test_engine.py
.venv/bin/ruff format live_edit/config.py tests/test_config.py tests/test_engine.py
git add live_edit/config.py tests/test_config.py tests/test_engine.py
git commit -m "feat(config): evaluation enabled by default (lint+test+introspect)"
```

---

## Self-Review 记录

- **Spec 覆盖:** 决策 #1(平衡关卡集)→ Task 1 + Task 6;决策 #2(修吞失败)→ Task 2;决策 #3(对话收尾)→ Task 4;决策 #4(自适应解析)→ Task 1;spec §2.4(preview 无 URL 防御)→ Task 1;spec §3.1(首轮填 diff)→ Task 3;spec §3.2(收尾文案)→ Task 4;spec §4(前端 skipped)→ Task 5;spec §5(测试)→ 各任务;spec §1(配置默认值)→ Task 6。
- **占位符扫描:** 无 TBD/TODO;每步含具体代码或可执行命令。
- **类型一致性:** `resolve_stages(config) -> list[str]`、`_classify_stage_result(lang, returncode, output) -> str`、`_eval_failure_note(failed_stage) -> str` 在 Task 内与 Task 间引用一致;runner 返回键 `ok/skipped/output/command` 一致。
- **任务顺序:** 先修评估管线(Task 1-5),最后翻转默认开关(Task 6)——保证任何提交点要么评估关闭、要么开启但正确。

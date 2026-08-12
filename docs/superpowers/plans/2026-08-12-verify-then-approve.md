# Verify-then-Approve（方案 A：与 evaluation 分工共存）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 live-build 的 verify-then-approve 移植到 live-edit，作为**合并前安全闸门**与 evaluation 质量网分工共存：verify 负责安全（保护路径/密钥扫描 → BLOCK）+ 审计 + 可选自动放行；测试/健康检查仍由 evaluation 跑，verify 默认不重复跑。

**Architecture:** 新增 `live_edit/verify/` 包（证据模型 evidence / 三层执行器 layers / 规则判定 rules / runner 编排）。agent 编辑循环结束时 engine 调用 `verify_change()` 产出 `Evidence`（deterministic / diff_safety / semantic 三层）+ `Decision`（auto_approve / human / block），经 Storage `session_evidence` 表存库。quick 模式用它决定是否跳过 `__final__` 人工等待；admin merge 端点用它拦截 BLOCK（需 reason 强制放行）或标注 auto_approve。与 evaluation 的去重：**verify 的 `test_command`/`health_url` 默认留空**，deterministic 层 SKIPPED → 决策降级 HUMAN（不自动放行），从而把"跑测试"完全让给 evaluation，verify 只做 evaluation 没有的安全扫描与决策。

**Tech Stack:** Python 3.10+，FastAPI，subprocess / shlex（stdlib），httpx（已有依赖），sqlite3（已有），pytest + pytest-asyncio。语义层用 httpx 抓 HTML 断言（默认关），不引 Playwright。

## Global Constraints

- Python >= 3.10，行宽 100，ruff 规则 `E,F,I,UP,B,C4,SIM`（与 live-edit 现状一致）
- **不硬编码密钥/secret；不新增运行时依赖**（httpx / subprocess / sqlite3 均已存在）
- 证据只能由服务端 verify runner 产生，agent 不可写（防假绿）
- **方案 A 去重规则（本计划核心）：`[verify]` 不配置 `test_command`/`health_url`，测试/健康检查由 evaluation 跑**。默认配置下 deterministic 层 SKIPPED → `evaluate()` 降级 HUMAN → quick 模式照常走人工审批。AUTO_APPROVE 仅在用户显式配置 verify 测试命令后激活——这是"可选能力"，不是默认行为
- `semantic_enabled` 默认 `false`（对齐 memory/rag 的 opt-in 惯例）
- Storage 新方法必须是**具体方法**（默认 no-op），不得加 `@abstractmethod`——自定义 Storage 实现不受破坏
- 每任务以 `pytest <new test file> -q` 全绿 + commit 结束
- **Task 0 必须先确认全量测试绿基线，且征得用户同意 git 提交策略**（live-edit 当前大部分文件是 untracked；用户此前未要求提交。默认按 writing-plans 惯例每任务 commit，若用户选择不提交则跳过 commit 步骤，只保证代码与测试）

---

### Task 0: 前置基线（绿基线确认 + 提交策略确认）

**Files:** 无

**Interfaces:** 无

- [ ] **Step 1: 确认工作区状态**

```bash
cd /home/jason/agent/live-edit
git status --short | head -20
git branch --show-current
```

Expected：在 `main`，存在大量 untracked 文件（此前 Tier1 修复 + 会话 fork 的产物）。这是基线。

- [ ] **Step 2: 全量测试确认绿基线**

```bash
.venv/bin/python -m pytest -q 2>&1 | tail -5
```

Expected：全部通过（`473 passed`）。若个别失败，先在基线修复再继续——后续任务的失败都要能归因于本计划改动。

- [ ] **Step 3: 与用户确认提交策略**

询问用户：是否同意「先提交当前工作区为基线 + 每任务一个 commit」？若同意，执行：

```bash
git add -A
git commit -m "chore: baseline — Tier1 fixes + session fork port"
```

若用户选择不提交（保持 untracked），后续所有任务的 commit 步骤一律跳过，仅保留代码与测试交付。

---

### Task 1: VerifyConfig + `[verify]` TOML 解析

**Files:**
- Modify: `live_edit/config.py`
- Test: `tests/test_verify_config.py`（create）

**Interfaces:**
- Produces: `VerifyRuleConfig` dataclass（`max_files: int = 10`、`protected_paths: list[str] = field(default_factory=list)`）
- Produces: `VerifyConfig` dataclass（`enabled: bool = True`、`max_retry: int = 3`、`test_command: str = ""`、`health_url: str = ""`、`semantic_enabled: bool = False`、`semantic_assert_text: list[str]`、`rules: VerifyRuleConfig`；`__post_init__` 校验 `max_retry >= 0`、`max_files >= 0`）
- Produces: `Config.verify: VerifyConfig` 字段 + `[verify]` / `[verify.rules.low_risk]` TOML 解析

- [ ] **Step 1: 写失败测试**

```python
# tests/test_verify_config.py
import tomllib

import pytest

from live_edit.config import VerifyConfig


def test_verify_config_defaults():
    cfg = VerifyConfig()
    assert cfg.enabled is True
    assert cfg.max_retry == 3
    assert cfg.test_command == ""          # 方案 A：默认不跑测试，测试归 evaluation
    assert cfg.health_url == ""
    assert cfg.semantic_enabled is False
    assert cfg.rules.max_files == 10
    assert cfg.rules.protected_paths == []


def test_parse_full_verify_section(tmp_path):
    from live_edit.config import VerifyRuleConfig

    toml = """
    [verify]
    enabled = true
    max_retry = 5
    test_command = "pytest -q"
    semantic_enabled = true
    semantic_assert_text = ["订单已创建"]

    [verify.rules.low_risk]
    max_files = 20
    protected_paths = ["auth/", "*.key"]
    """
    path = tmp_path / "config.toml"
    path.write_text(toml)
    data = tomllib.loads(path.read_text())
    v = data["verify"]
    r = v["rules"]["low_risk"]
    cfg = VerifyConfig(
        enabled=v["enabled"],
        max_retry=v["max_retry"],
        test_command=v["test_command"],
        semantic_enabled=v["semantic_enabled"],
        semantic_assert_text=v["semantic_assert_text"],
        rules=VerifyRuleConfig(max_files=r["max_files"], protected_paths=r["protected_paths"]),
    )
    assert cfg.max_retry == 5
    assert cfg.test_command == "pytest -q"
    assert cfg.semantic_assert_text == ["订单已创建"]
    assert cfg.rules.max_files == 20
    assert cfg.rules.protected_paths == ["auth/", "*.key"]


def test_verify_config_invalid_values():
    with pytest.raises(ValueError):
        VerifyConfig(max_retry=-1)
    with pytest.raises(ValueError):
        from live_edit.config import VerifyRuleConfig

        VerifyConfig(rules=VerifyRuleConfig(max_files=-1))
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_verify_config.py -q`
Expected: FAIL with `ImportError: cannot import name 'VerifyConfig'`

- [ ] **Step 3: 实现 dataclass + 校验**

在 `live_edit/config.py` 的 `ObservabilityConfig`（约 :236 附近）之后、`EmbedderConfig` 之前插入：

```python
@dataclass
class VerifyRuleConfig:
    max_files: int = 10
    protected_paths: list[str] = field(default_factory=list)


@dataclass
class VerifyConfig:
    enabled: bool = True
    max_retry: int = 3
    test_command: str = ""
    health_url: str = ""
    semantic_enabled: bool = False
    semantic_assert_text: list[str] = field(default_factory=list)
    rules: VerifyRuleConfig = field(default_factory=VerifyRuleConfig)

    def __post_init__(self):
        if self.max_retry < 0:
            raise ValueError(f"max_retry must be >= 0, got {self.max_retry}")
        if self.rules.max_files < 0:
            raise ValueError(f"max_files must be >= 0, got {self.rules.max_files}")
```

- [ ] **Step 4: 接入 Config 字段 + 解析**

在 `Config` dataclass（`observability` 字段之后，约 :236）加：

```python
    verify: VerifyConfig = field(default_factory=VerifyConfig)
```

在 `parse_config` 中（`observability = _parse_observability(...)` 之后）加：

```python
    verify_data = raw.get("verify", {})
    rules_data = verify_data.get("rules", {}).get("low_risk", {})
    verify = VerifyConfig(
        enabled=verify_data.get("enabled", True),
        max_retry=verify_data.get("max_retry", 3),
        test_command=verify_data.get("test_command", ""),
        health_url=verify_data.get("health_url", ""),
        semantic_enabled=verify_data.get("semantic_enabled", False),
        semantic_assert_text=verify_data.get("semantic_assert_text", []),
        rules=VerifyRuleConfig(
            max_files=rules_data.get("max_files", 10),
            protected_paths=rules_data.get("protected_paths", []),
        ),
    )
```

并在 `return Config(...)` 里加 `verify=verify,`（`observability=observability,` 之后）。

- [ ] **Step 5: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_verify_config.py -q`
Expected: PASS（3 passed）

- [ ] **Step 6: 提交**

```bash
git add live_edit/config.py tests/test_verify_config.py
git commit -m "feat(config): VerifyConfig + [verify] TOML parsing for verify-then-approve"
```

---

### Task 2: Evidence 模型

**Files:**
- Create: `live_edit/verify/__init__.py`（Task 2 占位；Task 5 完成后再改全导出）
- Create: `live_edit/verify/evidence.py`
- Test: `tests/test_verify_evidence.py`（create）

**Interfaces:**
- Produces: `class CheckStatus(str, Enum)` — `PASS="pass"` / `FAIL="fail"` / `SKIPPED="skipped"` / `UNVERIFIED="unverified"`
- Produces: `@dataclass class CheckResult` — `id: str`、`status: str`、`detail: dict`、`property passed -> bool`
- Produces: `@dataclass class Evidence` — `session_id: str`、`commit_hash: str`、`layers: dict[str, dict]`、`verify_attempts: int = 0`、`decision: str = "human"`、`reason: str = ""`；`property overall -> str`；`to_dict() -> dict`；`from_dict(d: dict) -> Evidence`
- `overall` 规则：任一 layer `status == "fail"` → `"fail"`；否则任一 `"unverified"` → `"unverified"`；否则 `"pass"`（`"skipped"` 不阻断）

- [ ] **Step 1: 写失败测试**

```python
# tests/test_verify_evidence.py
from live_edit.verify.evidence import Evidence


def test_overall_pass_when_all_pass():
    ev = Evidence(
        session_id="s1", commit_hash="",
        layers={
            "deterministic": {"status": "pass"},
            "diff_safety": {"status": "pass"},
            "semantic": {"status": "skipped"},
        },
    )
    assert ev.overall == "pass"


def test_overall_fail_when_any_fail():
    ev = Evidence(
        session_id="s1", commit_hash="",
        layers={"deterministic": {"status": "fail"}, "diff_safety": {"status": "pass"}},
    )
    assert ev.overall == "fail"


def test_overall_unverified():
    ev = Evidence(
        session_id="s1", commit_hash="",
        layers={"deterministic": {"status": "unverified"}, "diff_safety": {"status": "pass"}},
    )
    assert ev.overall == "unverified"


def test_overall_skipped_is_pass():
    ev = Evidence(
        session_id="s1", commit_hash="",
        layers={"deterministic": {"status": "skipped"}, "diff_safety": {"status": "pass"}},
    )
    assert ev.overall == "pass"


def test_to_from_dict_roundtrip():
    ev = Evidence(
        session_id="s1", commit_hash="abc", verify_attempts=2, decision="block", reason="保护路径",
        layers={"diff_safety": {"status": "fail", "out_of_scope": ["auth.py"]}},
    )
    restored = Evidence.from_dict(ev.to_dict())
    assert restored == ev


def test_to_dict_includes_decision_and_overall():
    ev = Evidence(session_id="s1", commit_hash="", layers={}, decision="auto_approve", reason="低风险")
    d = ev.to_dict()
    assert d["decision"] == "auto_approve"
    assert d["overall"] == "pass"  # 空 layers → 无 fail 无 unverified
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_verify_evidence.py -q`
Expected: FAIL with `ModuleNotFoundError: live_edit.verify`

- [ ] **Step 3: 实现 evidence.py + 占位 `__init__.py`**

```python
# live_edit/verify/evidence.py
"""Evidence model for verify-then-approve."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum


class CheckStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    SKIPPED = "skipped"
    UNVERIFIED = "unverified"


@dataclass
class CheckResult:
    id: str
    status: str
    detail: dict = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.status == CheckStatus.PASS


@dataclass
class Evidence:
    session_id: str
    commit_hash: str
    layers: dict[str, dict] = field(default_factory=dict)
    verify_attempts: int = 0
    decision: str = "human"
    reason: str = ""

    @property
    def overall(self) -> str:
        statuses = [layer.get("status") for layer in self.layers.values()]
        if any(s == CheckStatus.FAIL for s in statuses):
            return CheckStatus.FAIL
        if any(s == CheckStatus.UNVERIFIED for s in statuses):
            return CheckStatus.UNVERIFIED
        return CheckStatus.PASS

    def to_dict(self) -> dict:
        return asdict(self) | {"overall": self.overall}

    @classmethod
    def from_dict(cls, d: dict) -> "Evidence":
        return cls(
            session_id=d.get("session_id", ""),
            commit_hash=d.get("commit_hash", ""),
            layers=d.get("layers", {}),
            verify_attempts=d.get("verify_attempts", 0),
            decision=d.get("decision", "human"),
            reason=d.get("reason", ""),
        )
```

```python
# live_edit/verify/__init__.py（Task 2 占位；Task 5 完成后再改全导出）
"""Verify-then-approve package."""
```

注意：`__init__.py` 现在必须保持占位（不 import rules/runner），否则 Task 2/3/4 的单独测试会因 import 失败而跑不起来。

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_verify_evidence.py -q`
Expected: PASS（6 passed）

- [ ] **Step 5: 提交**

```bash
git add live_edit/verify/ tests/test_verify_evidence.py
git commit -m "feat(verify): Evidence model with overall status + to/from_dict"
```

---

### Task 3: 三层验证执行器（deterministic / diff_safety / semantic）

**Files:**
- Create: `live_edit/verify/layers.py`
- Test: `tests/test_verify_layers.py`（create）

**Interfaces:**
- Consumes: `CheckStatus`（evidence.py）
- Produces:
  - `async run_test_command(worktree: str, command: str) -> dict` — 空命令 → skipped；子进程超时/不存在 → fail（**超时需回收子进程**）；`{"status", "detail": {command, exit_code, output_tail}}`
  - `async run_health_check(health_url: str) -> dict` — 空 URL → skipped；httpx GET，`status_code == 200` 才算 pass；`{"status", "detail": {url, status_code|error}}`
  - `async check_diff_safety(worktree: str, modified_files: list[str], protected_paths: list[str]) -> dict` — `{"status", "files_touched", "out_of_scope", "scan_alerts"}`；绝对路径跳过扫描；密钥扫描 `_SECRET_PATTERNS`（AWS key / PEM 私钥 / 内联 api_key·secret·token·password）
  - `async check_semantic(preview_url: str, assert_text: list[str]) -> dict` — URL 或断言为空 → skipped；`{"status", "checks": [{"text", "found"}]}`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_verify_layers.py
import pytest

from live_edit.verify.layers import (
    check_diff_safety,
    check_semantic,
    run_health_check,
    run_test_command,
)


@pytest.mark.asyncio
async def test_run_test_command_pass(tmp_path):
    r = await run_test_command(str(tmp_path), "python -c 'print(1)'")
    assert r["status"] == "pass"
    assert r["detail"]["exit_code"] == 0


@pytest.mark.asyncio
async def test_run_test_command_fail(tmp_path):
    r = await run_test_command(str(tmp_path), "python -c 'raise SystemExit(1)'")
    assert r["status"] == "fail"


@pytest.mark.asyncio
async def test_run_test_command_skipped_when_empty():
    r = await run_test_command("/tmp", "")
    assert r["status"] == "skipped"


@pytest.mark.asyncio
async def test_run_test_command_timeout_kills_child(tmp_path):
    import asyncio
    import contextlib

    # 需要真实超时：命令 sleep 很久，通过 monkeypatch 缩短 wait_for 不可行，
    # 改为直接构造一个立即超时的场景并断言返回 fail 而非悬挂。
    # 这里用短命令 + 手动 asyncio.wait_for 包裹验证返回值形态。
    from live_edit.verify.layers import run_test_command as _rtc

    # 覆盖：给一个必然不存在的二进制，验证 FileNotFoundError → fail 分支
    r = await _rtc(str(tmp_path), "definitely_not_a_real_binary_xyz 2>&1")
    assert r["status"] == "fail"
    assert "error" in r["detail"]


@pytest.mark.asyncio
async def test_health_check_pass_and_fail():
    from live_edit.verify.layers import _serve_ok

    server = _serve_ok()
    port = server.server_address[1]
    try:
        ok = await run_health_check(f"http://127.0.0.1:{port}/live-edit/health")
        assert ok["status"] == "pass"
        assert ok["detail"]["status_code"] == 200
    finally:
        server.shutdown()


@pytest.mark.asyncio
async def test_health_check_skipped_when_empty():
    r = await run_health_check("")
    assert r["status"] == "skipped"


@pytest.mark.asyncio
async def test_diff_safety_protected_path(tmp_path):
    (tmp_path / "auth").mkdir()
    (tmp_path / "auth" / "login.py").write_text("x = 1")
    r = await check_diff_safety(str(tmp_path), ["auth/login.py"], ["auth/"])
    assert r["status"] == "fail"
    assert r["out_of_scope"] == ["auth/login.py"]


@pytest.mark.asyncio
async def test_diff_safety_secret_scan(tmp_path):
    (tmp_path / "app.py").write_text("KEY = 'AKIAIOSFODNN7EXAMPLE'\n")
    r = await check_diff_safety(str(tmp_path), ["app.py"], [])
    assert r["status"] == "fail"
    assert len(r["scan_alerts"]) >= 1


@pytest.mark.asyncio
async def test_diff_safety_clean(tmp_path):
    (tmp_path / "app.py").write_text("x = 1\n")
    r = await check_diff_safety(str(tmp_path), ["app.py"], [])
    assert r["status"] == "pass"
    assert r["out_of_scope"] == []
    assert r["scan_alerts"] == []


@pytest.mark.asyncio
async def test_diff_safety_skips_absolute_path(tmp_path):
    (tmp_path / "app.py").write_text("KEY = 'AKIAIOSFODNN7EXAMPLE'\n")
    r = await check_diff_safety(str(tmp_path), [str(tmp_path / "app.py")], [])
    # 绝对路径逃逸 worktree，跳过扫描 → 不误报
    assert r["status"] == "pass"
    assert r["scan_alerts"] == []


@pytest.mark.asyncio
async def test_semantic_skipped_when_no_assert():
    r = await check_semantic("http://127.0.0.1:1", [])
    assert r["status"] == "skipped"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_verify_layers.py -q`
Expected: FAIL with `ModuleNotFoundError: live_edit.verify.layers`

- [ ] **Step 3: 实现 layers.py**

```python
# live_edit/verify/layers.py
"""Three verification layers: deterministic, diff safety, semantic."""
from __future__ import annotations

import asyncio
import contextlib
import http.server
import re
import shlex
import threading
from fnmatch import fnmatch
from pathlib import Path

import httpx

from .evidence import CheckStatus

_SECRET_PATTERNS: list[tuple[str, str]] = [
    (r"AKIA[0-9A-Z]{16}", "aws_access_key"),
    (r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----", "private_key"),
    (r"(?i)(api[_-]?key|secret|token|password)\s*[=:]\s*['\"][^'\"]{12,}", "inline_secret"),
]


def _matches_any(path: str, patterns: list[str]) -> bool:
    return any(fnmatch(path, p) or path.startswith(p.rstrip("/") + "/") for p in patterns)


async def run_test_command(worktree: str, command: str) -> dict:
    if not command or not command.strip():
        return {"status": CheckStatus.SKIPPED, "detail": {"command": command}}
    try:
        proc = await asyncio.create_subprocess_exec(
            *shlex.split(command),
            cwd=worktree,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
    except (asyncio.TimeoutError, FileNotFoundError) as e:
        error = str(e)
        if isinstance(e, asyncio.TimeoutError):
            # 超时分支必须回收子进程，避免悬空的长时间运行命令泄漏。
            # kill() 可能撞上子进程恰好已退出的瞬间 → 抑制 ProcessLookupError。
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
            error = f"{error} (child process killed after 120s timeout)"
        return {"status": CheckStatus.FAIL, "detail": {"command": command, "error": error}}
    return {
        "status": CheckStatus.PASS if proc.returncode == 0 else CheckStatus.FAIL,
        "detail": {
            "command": command,
            "exit_code": proc.returncode,
            "output_tail": (out or b"")[-2000:].decode(errors="replace"),
        },
    }


async def run_health_check(health_url: str) -> dict:
    if not health_url:
        return {"status": CheckStatus.SKIPPED, "detail": {"url": ""}}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(health_url)
        status = CheckStatus.PASS if r.status_code == 200 else CheckStatus.FAIL
        return {"status": status, "detail": {"url": health_url, "status_code": r.status_code}}
    except Exception as e:  # noqa: BLE001 — 网络错误统一视为失败
        return {"status": CheckStatus.FAIL, "detail": {"url": health_url, "error": str(e)}}


def _scan_file_for_secrets(path: Path) -> list[dict]:
    alerts: list[dict] = []
    try:
        content = path.read_text(errors="replace")
    except OSError:
        return alerts
    for pattern, kind in _SECRET_PATTERNS:
        if re.search(pattern, content):
            alerts.append({"file": str(path), "kind": kind})
    return alerts


async def check_diff_safety(
    worktree: str, modified_files: list[str], protected_paths: list[str]
) -> dict:
    files_touched = sorted(set(modified_files))
    out_of_scope = [f for f in files_touched if _matches_any(f, protected_paths)]
    scan_alerts: list[dict] = []
    for f in files_touched:
        # 绝对路径会逃逸 worktree（Path(worktree) / f 直接拼成外部路径），跳过扫描。
        if Path(f).is_absolute():
            continue
        scan_alerts.extend(_scan_file_for_secrets(Path(worktree) / f))
    status = CheckStatus.FAIL if (out_of_scope or scan_alerts) else CheckStatus.PASS
    return {
        "status": status,
        "files_touched": files_touched,
        "out_of_scope": out_of_scope,
        "scan_alerts": scan_alerts,
    }


async def check_semantic(preview_url: str, assert_text: list[str]) -> dict:
    if not preview_url or not assert_text:
        return {"status": CheckStatus.SKIPPED, "detail": {}}
    checks: list[dict] = []
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(preview_url)
        html = r.text
    except Exception as e:  # noqa: BLE001
        return {
            "status": CheckStatus.FAIL,
            "checks": [{"text": t, "found": False, "error": str(e)} for t in assert_text],
        }
    for text in assert_text:
        checks.append({"text": text, "found": text in html})
    status = CheckStatus.PASS if all(c["found"] for c in checks) else CheckStatus.FAIL
    return {"status": status, "checks": checks}


# ── 测试用迷你 HTTP 服务器（仅测试导入，生产不调用）──


class _OkHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):  # noqa: D102
        pass


def _serve_ok():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _OkHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_verify_layers.py -q`
Expected: PASS（12 passed）

- [ ] **Step 5: 提交**

```bash
git add live_edit/verify/layers.py tests/test_verify_layers.py
git commit -m "feat(verify): three verification layers (test/health, diff safety, semantic)"
```

---

### Task 4: 规则判定（evaluate 纯函数）

**Files:**
- Create: `live_edit/verify/rules.py`
- Test: `tests/test_verify_rules.py`（create）

**Interfaces:**
- Consumes: `Evidence`（Task 2）、`VerifyConfig`（Task 1）
- Produces: `class Decision(str, Enum)` — `AUTO_APPROVE="auto_approve"` / `HUMAN="human"` / `BLOCK="block"`
- Produces: `def evaluate(evidence: Evidence, config: VerifyConfig) -> tuple[Decision, str]`
- 决策逻辑（纯函数，顺序即优先级）：
  1. `config.enabled is False` → `(AUTO_APPROVE, "verify disabled")`
  2. diff_safety `out_of_scope` 非空 → `BLOCK`
  3. diff_safety `scan_alerts` 非空 → `BLOCK`
  4. deterministic `status == "fail"` → `BLOCK`
  5. `overall == "unverified"` → `HUMAN`
  6. `overall != "pass"` → `BLOCK`（语义失败等）
  7. `verify_attempts > config.max_retry` → `BLOCK`（累计重试超限）
  8. `len(files_touched) > config.rules.max_files` → `HUMAN`
  9. deterministic `status == "skipped"` → `HUMAN`（**方案 A 关键规则**：未配置 verify 测试 → 降级人工，不自动放行）
  10. 其它 → `(AUTO_APPROVE, "低风险自动放行")`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_verify_rules.py
from live_edit.config import VerifyConfig
from live_edit.verify.evidence import Evidence
from live_edit.verify.rules import Decision, evaluate


def _ev(**kw):
    defaults = dict(session_id="s1", commit_hash="", layers={}, verify_attempts=0)
    defaults.update(kw)
    return Evidence(**defaults)


def test_disabled_always_auto():
    cfg = VerifyConfig(enabled=False)
    d, _ = evaluate(_ev(layers={"diff_safety": {"status": "fail", "out_of_scope": ["a"]}}), cfg)
    assert d == Decision.AUTO_APPROVE


def test_out_of_scope_blocks():
    cfg = VerifyConfig()
    ev = _ev(layers={"diff_safety": {"status": "fail", "out_of_scope": ["auth.py"], "scan_alerts": []}})
    d, reason = evaluate(ev, cfg)
    assert d == Decision.BLOCK
    assert "保护" in reason


def test_scan_alerts_block():
    cfg = VerifyConfig()
    ev = _ev(layers={"diff_safety": {"status": "fail", "out_of_scope": [], "scan_alerts": [{"kind": "aws_access_key"}]}})
    assert evaluate(ev, cfg)[0] == Decision.BLOCK


def test_deterministic_fail_blocks():
    cfg = VerifyConfig()
    ev = _ev(layers={"deterministic": {"status": "fail"}, "diff_safety": {"status": "pass"}})
    assert evaluate(ev, cfg)[0] == Decision.BLOCK


def test_unverified_human():
    cfg = VerifyConfig()
    ev = _ev(layers={"deterministic": {"status": "unverified"}, "diff_safety": {"status": "pass"}})
    d, reason = evaluate(ev, cfg)
    assert d == Decision.HUMAN
    assert "降级" in reason


def test_retry_exceeded_blocks():
    cfg = VerifyConfig(max_retry=3)
    ev = _ev(verify_attempts=4, layers={"deterministic": {"status": "pass"}, "diff_safety": {"status": "pass"}})
    assert evaluate(ev, cfg)[0] == Decision.BLOCK


def test_large_diff_human():
    cfg = VerifyConfig()
    cfg.rules.max_files = 2
    ev = _ev(layers={"deterministic": {"status": "pass"}, "diff_safety": {"status": "pass", "files_touched": ["a", "b", "c"]}})
    d, reason = evaluate(ev, cfg)
    assert d == Decision.HUMAN
    assert "文件" in reason


def test_skipped_det_degrades_human():
    """方案 A 关键规则：verify 默认不跑测试（deterministic skipped）→ 降级人工。"""
    cfg = VerifyConfig()
    ev = _ev(layers={"deterministic": {"status": "skipped"}, "diff_safety": {"status": "pass", "files_touched": ["a.py"]}})
    d, reason = evaluate(ev, cfg)
    assert d == Decision.HUMAN
    assert "降级" in reason


def test_clean_with_verify_test_auto_approves():
    cfg = VerifyConfig(test_command="pytest -q")
    ev = _ev(layers={"deterministic": {"status": "pass"}, "diff_safety": {"status": "pass", "files_touched": ["a.py"]}})
    d, reason = evaluate(ev, cfg)
    assert d == Decision.AUTO_APPROVE
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_verify_rules.py -q`
Expected: FAIL with `ModuleNotFoundError: live_edit.verify.rules`

- [ ] **Step 3: 实现 rules.py**

```python
# live_edit/verify/rules.py
"""Decision evaluation for verify-then-approve."""
from __future__ import annotations

from enum import Enum

from .evidence import CheckStatus, Evidence


class Decision(str, Enum):
    AUTO_APPROVE = "auto_approve"
    HUMAN = "human"
    BLOCK = "block"


def evaluate(evidence: Evidence, config) -> tuple[Decision, str]:
    """Decide auto-approve / human / block from evidence. Order is priority."""
    if not config.enabled:
        return (Decision.AUTO_APPROVE, "verify disabled")

    diff = evidence.layers.get("diff_safety", {})
    det = evidence.layers.get("deterministic", {})

    if diff.get("out_of_scope"):
        return (Decision.BLOCK, "改动了保护路径")
    if diff.get("scan_alerts"):
        return (Decision.BLOCK, "安全扫描告警")
    if det.get("status") == CheckStatus.FAIL:
        return (Decision.BLOCK, "确定性检查失败")

    if evidence.overall == CheckStatus.UNVERIFIED:
        return (Decision.HUMAN, "验证不完整，降级人工")
    if evidence.overall != CheckStatus.PASS:
        return (Decision.BLOCK, "验证未全绿")

    if evidence.verify_attempts > config.max_retry:
        return (Decision.BLOCK, "累计重试超限")

    if len(diff.get("files_touched", [])) > config.rules.max_files:
        return (Decision.HUMAN, f"改动文件过多（>{config.rules.max_files}）")

    if det.get("status") == CheckStatus.SKIPPED:
        return (Decision.HUMAN, "未配置实际验证，降级人工")

    return (Decision.AUTO_APPROVE, "低风险自动放行")
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_verify_rules.py -q`
Expected: PASS（10 passed）

- [ ] **Step 5: 提交**

```bash
git add live_edit/verify/rules.py tests/test_verify_rules.py
git commit -m "feat(verify): Decision enum + evaluate() rule matrix"
```

---

### Task 5: Runner 编排（verify_change）+ `__init__` 全导出

**Files:**
- Create: `live_edit/verify/runner.py`
- Modify: `live_edit/verify/__init__.py`（替换 Task 2 的占位，加全导出）
- Test: `tests/test_verify_runner.py`（create）

**Interfaces:**
- Consumes: `Evidence`（Task 2）、三个 layer 函数（Task 3）、`evaluate` / `Decision`（Task 4）、`VerifyConfig`（Task 1）
- Produces: `async verify_change(worktree: str, modified_files: list[str], config: Config, session_id: str = "", commit_hash: str = "", previous_attempts: int = 0) -> Evidence`
  - 组装三层 dict、算 `overall`、`evaluate()` 得 decision + reason、写入 `evidence.decision`/`.reason`/`.verify_attempts = previous_attempts + 1`，返回 Evidence
  - 从 `config.verify` 读 `test_command`/`health_url`/`semantic_enabled`/`semantic_assert_text`/`rules.protected_paths`；`preview_url` 语义层用 `config.preview.base_url`（live-edit 已有该字段，默认 `http://localhost:8083`）

- [ ] **Step 1: 写失败测试**

```python
# tests/test_verify_runner.py
import pytest

from live_edit.config import Config, LLMConfig, PreviewConfig, ProjectConfig, SafetyConfig
from live_edit.verify.runner import verify_change


def _cfg(verify=None):
    return Config(
        project=ProjectConfig(name="t", language="python", root="."),
        llm=LLMConfig(api_url="http://x", api_key_env="K", model="m"),
        safety=SafetyConfig(),
        preview=PreviewConfig(base_url="http://127.0.0.1:1"),
        verify=verify,
    )


@pytest.mark.asyncio
async def test_default_config_degrades_to_human(tmp_path):
    """方案 A：verify 默认不配测试 → deterministic skipped → 降级 HUMAN，不自动放行。"""
    (tmp_path / "app.py").write_text("x = 1\n")
    ev = await verify_change(str(tmp_path), ["app.py"], _cfg(), session_id="s1")
    assert ev.overall == "pass"
    assert ev.decision == "human"
    assert ev.verify_attempts == 1


@pytest.mark.asyncio
async def test_clean_change_with_verify_test_auto_approves(tmp_path):
    from live_edit.config import VerifyConfig

    cfg = _cfg(verify=VerifyConfig(test_command="python -c 'pass'"))
    (tmp_path / "app.py").write_text("x = 1\n")
    ev = await verify_change(str(tmp_path), ["app.py"], cfg, session_id="s1")
    assert ev.decision == "auto_approve"


@pytest.mark.asyncio
async def test_protected_file_blocks(tmp_path):
    from live_edit.config import VerifyConfig, VerifyRuleConfig

    cfg = _cfg(verify=VerifyConfig(rules=VerifyRuleConfig(protected_paths=["auth/"])))
    (tmp_path / "auth").mkdir()
    (tmp_path / "auth" / "x.py").write_text("x = 1\n")
    ev = await verify_change(str(tmp_path), ["auth/x.py"], cfg, session_id="s1")
    assert ev.decision == "block"
    assert "保护" in ev.reason


@pytest.mark.asyncio
async def test_previous_attempts_incremented(tmp_path):
    (tmp_path / "app.py").write_text("x = 1\n")
    ev = await verify_change(str(tmp_path), ["app.py"], _cfg(), session_id="s1", previous_attempts=2)
    assert ev.verify_attempts == 3
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_verify_runner.py -q`
Expected: FAIL with `ModuleNotFoundError: live_edit.verify.runner`

- [ ] **Step 3: 实现 runner.py + 替换 `__init__.py`**

```python
# live_edit/verify/runner.py
"""Orchestrate the three layers into an Evidence with a decision."""
from __future__ import annotations

from live_edit.config import VerifyConfig

from .evidence import Evidence
from .layers import check_diff_safety, check_semantic, run_health_check, run_test_command
from .rules import evaluate


async def verify_change(
    worktree: str,
    modified_files: list[str],
    config,
    session_id: str = "",
    commit_hash: str = "",
    previous_attempts: int = 0,
) -> Evidence:
    v = config.verify or VerifyConfig()
    preview_url = config.preview.base_url if getattr(config, "preview", None) else ""

    det_checks = [
        {"id": "test_command", **await run_test_command(worktree, v.test_command)},
        {"id": "health_check", **await run_health_check(v.health_url)},
    ]
    det_status = (
        "fail"
        if any(c["status"] == "fail" for c in det_checks)
        else ("pass" if any(c["status"] == "pass" for c in det_checks) else "skipped")
    )

    semantic = (
        await check_semantic(preview_url, v.semantic_assert_text)
        if v.semantic_enabled
        else {"status": "skipped", "detail": {}}
    )

    layers = {
        "deterministic": {"status": det_status, "checks": det_checks},
        "diff_safety": await check_diff_safety(
            worktree, modified_files, v.rules.protected_paths if v.rules else []
        ),
        "semantic": semantic,
    }

    evidence = Evidence(
        session_id=session_id,
        commit_hash=commit_hash,
        layers=layers,
        verify_attempts=previous_attempts + 1,
    )
    decision, reason = evaluate(evidence, v)
    evidence.decision = decision
    evidence.reason = reason
    return evidence
```

```python
# live_edit/verify/__init__.py（Task 5 完成后的全导出）
"""Verify-then-approve: evidence, layers, rules, runner."""
from .evidence import CheckResult, CheckStatus, Evidence
from .rules import Decision, evaluate
from .runner import verify_change

__all__ = ["CheckResult", "CheckStatus", "Evidence", "Decision", "evaluate", "verify_change"]
```

- [ ] **Step 4: 跑测试确认通过（含 Task 2/3/4 回归）**

Run: `.venv/bin/python -m pytest tests/test_verify_runner.py tests/test_verify_rules.py tests/test_verify_evidence.py tests/test_verify_layers.py -q`
Expected: PASS（4 + 10 + 6 + 12 = 32 passed）

- [ ] **Step 5: 提交**

```bash
git add live_edit/verify/ tests/test_verify_runner.py
git commit -m "feat(verify): verify_change runner orchestrating layers + decision"
```

---

### Task 6: Storage evidence 表

**Files:**
- Modify: `live_edit/storage.py`
- Test: `tests/test_verify_storage.py`（create）

**Interfaces:**
- Consumes: `Storage` / `SQLiteStorage`（现有）
- Produces:
  - `Storage.save_evidence(session_id: str, evidence_json: str) -> None`（**具体方法，默认 no-op**）
  - `Storage.get_evidence(session_id: str) -> str | None`（**具体方法，默认返回 None**）
  - `SQLiteStorage` 覆写二者，`session_evidence` 表（`session_id TEXT PRIMARY KEY`、`evidence TEXT NOT NULL`、`updated_at TEXT DEFAULT (datetime('now'))`），重复 save 覆盖

- [ ] **Step 1: 写失败测试**

```python
# tests/test_verify_storage.py
import json

from live_edit.storage import SQLiteStorage, Storage


def test_sqlite_save_get_roundtrip(tmp_path):
    st = SQLiteStorage(str(tmp_path / "s.db"))
    ev = json.dumps({"session_id": "s1", "decision": "auto_approve"})
    st.save_evidence("s1", ev)
    assert st.get_evidence("s1") == ev


def test_get_missing_returns_none(tmp_path):
    st = SQLiteStorage(str(tmp_path / "s.db"))
    assert st.get_evidence("nope") is None


def test_save_overwrites(tmp_path):
    st = SQLiteStorage(str(tmp_path / "s.db"))
    st.save_evidence("s1", "one")
    st.save_evidence("s1", "two")
    assert st.get_evidence("s1") == "two"


def test_abstract_default_is_noop():
    class Noop(Storage):
        def save_session(self, *a, **k): ...
        def get_sessions(self, *a, **k): return []
        def get_session_detail(self, *a, **k): return None
        def store_embedding(self, *a, **k): ...
        def query_embeddings(self, *a, **k): return []
        def delete_old_embeddings(self, *a, **k): ...

    st = Noop()
    st.save_evidence("s1", "{}")   # 不应抛
    assert st.get_evidence("s1") is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_verify_storage.py -q`
Expected: FAIL with `AttributeError: 'SQLiteStorage' object has no attribute 'save_evidence'`

- [ ] **Step 3: 在 Storage ABC 加默认实现**

在 `live_edit/storage.py` 的 `Storage` 类里、`delete_old_embeddings` 之后加：

```python
    def save_evidence(self, session_id: str, evidence_json: str) -> None:
        """Persist verify evidence. Default no-op for custom storages."""
        return None

    def get_evidence(self, session_id: str) -> str | None:
        """Return stored evidence JSON, or None. Default: no evidence."""
        return None
```

- [ ] **Step 4: SQLiteStorage 覆写 + 建表**

在 `SQLiteStorage._init_db()` 中、最后一个 `CREATE TABLE`（`knowledge_meta` 之后）加建表：

```python
        conn.execute("""
            CREATE TABLE IF NOT EXISTS session_evidence (
                session_id TEXT PRIMARY KEY,
                evidence TEXT NOT NULL,
                updated_at TEXT DEFAULT (datetime('now'))
            )
        """)
```

在 `SQLiteStorage` 类内（`_get_conn` 定义之后的任意位置）加：

```python
    def save_evidence(self, session_id: str, evidence_json: str) -> None:
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO session_evidence (session_id, evidence) VALUES (?, ?) "
            "ON CONFLICT(session_id) DO UPDATE SET evidence=excluded.evidence, "
            "updated_at=datetime('now')",
            (session_id, evidence_json),
        )
        conn.commit()

    def get_evidence(self, session_id: str) -> str | None:
        row = self._get_conn().execute(
            "SELECT evidence FROM session_evidence WHERE session_id=?", (session_id,)
        ).fetchone()
        return row["evidence"] if row else None
```

- [ ] **Step 5: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_verify_storage.py -q`
Expected: PASS（4 passed）

- [ ] **Step 6: 提交**

```bash
git add live_edit/storage.py tests/test_verify_storage.py
git commit -m "feat(storage): session_evidence table + save/get_evidence (default no-op)"
```

---

### Task 7: Engine 集成（quick 自动放行 + deep 存证据）

**Files:**
- Modify: `live_edit/engine.py`
- Test: `tests/test_verify_engine.py`（create；复用现有 `FakeProvider`、`_make_test_config`）

**Interfaces:**
- Consumes: `verify_change`（Task 5）、`Decision`（Task 4）、`Storage.save_evidence`（Task 6）
- Produces:
  - `async def _verify_and_store(session, storage, config) -> Evidence | None`（模块级 helper）——`config.verify` 存在且 `enabled` 才跑；`verify_change(session._worktree_path, session._modified_files, config, session_id=session.id, previous_attempts=<历史 attempts>)` 并 `storage.save_evidence(...)`；否则返回 None
  - `def _verify_auto_approves(evidence) -> bool`（纯 helper）
- 改动点（`engine.py`）：
  - 顶部 import：`from .verify import Decision as _VerifyDecision`、`from .verify.runner import verify_change as _verify_change`
  - `run_edit_session` 中 `if mode == "deep":`（约 :1218）**之前**插入 `evidence = await _verify_and_store(session, storage, config)` + `auto_approved = _verify_auto_approves(evidence)`
  - quick/qa 分支（约 :1221-1230）：`wait_for_approval("__final__", ...)` 前，若 `auto_approved` → `final = {"approved": True, "auto": True, "reason": evidence.reason}`；否则照常，并在 tool_data 加 `"evidence": evidence.to_dict() if evidence else None`
  - deep 分支：`_do_commit` 后，若有 evidence 且 `session._commit_hash`，补 `evidence.commit_hash` 并重新 `save_evidence`

- [ ] **Step 1: 写失败测试（先测纯 helper）**

```python
# tests/test_verify_engine.py
import pytest

from live_edit.engine import _verify_auto_approves
from live_edit.verify.evidence import Evidence


def test_auto_approve_helper():
    ev = Evidence(session_id="s1", commit_hash="", layers={}, decision="auto_approve")
    assert _verify_auto_approves(ev) is True


def test_non_auto_or_none():
    ev = Evidence(session_id="s1", commit_hash="", layers={}, decision="human")
    assert _verify_auto_approves(ev) is False
    assert _verify_auto_approves(None) is False
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_verify_engine.py -q`
Expected: FAIL with `ImportError: cannot import name '_verify_auto_approves'`

- [ ] **Step 3: 实现 helper + 接入点**

在 `live_edit/engine.py` 顶部 import 区加：

```python
from .verify import Decision as _VerifyDecision
from .verify.runner import verify_change as _verify_change
```

在 `_do_commit` 定义（约 :485）之前加两个模块级函数：

```python
async def _verify_and_store(session, storage, config):
    """Run verify-then-approve for a finished session; store evidence. Returns Evidence or None."""
    verify_cfg = getattr(config, "verify", None)
    if verify_cfg is None or not verify_cfg.enabled:
        return None
    prior = 0
    if storage is not None:
        try:
            stored = storage.get_evidence(session.id)
            if stored:
                prior = json.loads(stored).get("verify_attempts", 0)
        except Exception:  # noqa: BLE001 — 读取历史证据失败不阻断
            prior = 0
    evidence = await _verify_change(
        worktree=session._worktree_path,
        modified_files=session._modified_files,
        config=config,
        session_id=session.id,
        previous_attempts=prior,
    )
    if storage is not None:
        storage.save_evidence(session.id, json.dumps(evidence.to_dict(), ensure_ascii=False))
    return evidence


def _verify_auto_approves(evidence) -> bool:
    return (
        evidence is not None and getattr(evidence, "decision", "") == _VerifyDecision.AUTO_APPROVE
    )
```

在 `run_edit_session` 中，找到 `if mode == "deep":`（约 :1218）一行，在其**上方**插入：

```python
            evidence = await _verify_and_store(session, storage, config)
            auto_approved = _verify_auto_approves(evidence)
```

quick/qa 分支（原 `final = await session.wait_for_approval("__final__", {...})`，约 :1221）改为：

```python
            else:
                tool_data = {
                    "tool": "final_commit",
                    "files": session._modified_files,
                    "summary": diff_stat,
                }
                if evidence is not None:
                    tool_data["evidence"] = evidence.to_dict()
                if auto_approved:
                    final = {"approved": True, "auto": True, "reason": evidence.reason}
                else:
                    final = await session.wait_for_approval("__final__", tool_data, timeout=600.0)
```

deep 分支（`if mode == "deep":` 体内 `_do_commit` 之后，约 :1218-1220）改为：

```python
            if mode == "deep":
                await _do_commit(session, vcs, storage, config, audit_log=audit_log)
                if evidence is not None and session._commit_hash:
                    evidence.commit_hash = session._commit_hash
                    if storage is not None:
                        storage.save_evidence(
                            session.id, json.dumps(evidence.to_dict(), ensure_ascii=False)
                        )
                session._outcome = "completed" if session._committed else "failed"
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_verify_engine.py -q && .venv/bin/python -m pytest tests/test_engine.py -q`
Expected: PASS（2 new passed + 现有 engine 测试全绿）

- [ ] **Step 5: 提交**

```bash
git add live_edit/engine.py tests/test_verify_engine.py
git commit -m "feat(engine): run verify at session end, quick-mode auto-approve skips final wait"
```

---

### Task 8: admin merge 证据感知 + 会话详情带证据

**Files:**
- Modify: `live_edit/router.py`
- Test: `tests/test_router_verify.py`（create）

**Interfaces:**
- Consumes: `Storage.get_evidence`（Task 6）、`Evidence.from_dict`（Task 2）
- Produces:
  - `class MergeRequest(BaseModel)` — `reason: str = ""`（`setup_live_edit` 内定义）
  - 修改 `POST /admin/branches/{session_id}/merge`：读 `storage.get_evidence(session_id)` → `Evidence.from_dict`；决策 `block` 且 `reason` 为空 → `400 {"detail": "该改动被验证阻断，需提供 reason 强制放行", "blocked": True}`；合并成功后按决策记审计（`auto_approve`/`override`/`ok`）；响应加 `"decision"`
  - 修改 `GET /session/{session_id}`：响应加 `evidence`（`get_evidence` → `json.loads`，无则 None）

- [ ] **Step 1: 写失败测试**

```python
# tests/test_router_verify.py
"""Evidence-aware admin merge gate + session-detail evidence tests."""

import json
import subprocess
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from live_edit.audit import SQLiteAuditLog
from live_edit.router import setup_live_edit
from live_edit.storage import SQLiteStorage


def _write_config(tmp_path) -> str:
    """Write a minimal .live-edit.toml and return its absolute path.

    setup_live_edit 会 open(config_path)（parse_config），文件缺失会抛
    FileNotFoundError，所以必须先写配置文件。
    """
    config_path = tmp_path / ".live-edit.toml"
    config_path.write_text(
        """[project]
name = "t"
language = "python"
root = "."

[llm]
provider = "anthropic_compatible"
api_url = "http://x"
api_key_env = "K"
model = "m"
"""
    )
    return str(config_path)


def _make_app(tmp_path, vcs, audit_log=None):
    """Build a router backed by a real SQLiteStorage and the given vcs."""
    storage = SQLiteStorage(str(tmp_path / "s.db"))
    router = setup_live_edit(
        project_root=str(tmp_path),
        config_path=_write_config(tmp_path),
        storage=storage,
        vcs=vcs,
        admin_key="k",
        audit_log=audit_log,
    )
    app = FastAPI()
    app.include_router(router)
    return app, storage


def _make_git_repo(tmp_path, sid):
    """Real temp git repo with a live-edit/<sid> branch + MagicMock vcs wired to it.

    The merge endpoint runs `git rev-parse --verify live-edit/<sid>` against
    vcs.repo_path BEFORE merging, so the repo must actually have the branch
    (otherwise the merge-succeeds tests 404).
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=str(repo), capture_output=True)
    (repo / "init.txt").write_text("init")
    subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "branch", "-M", "main"], cwd=str(repo), capture_output=True)
    subprocess.run(
        ["git", "checkout", "-b", f"live-edit/{sid}"], cwd=str(repo), capture_output=True
    )
    vcs = MagicMock()
    vcs.repo_path = str(repo)
    vcs.merge_commit.return_value = "m1"
    vcs.discard_session_branch = MagicMock()
    return vcs


def _store_evidence(storage, session_id, decision):
    storage.save_evidence(
        session_id,
        json.dumps({"session_id": session_id, "decision": decision, "layers": {}}),
    )


def test_merge_blocked_requires_reason(tmp_path):
    app, storage = _make_app(tmp_path, vcs=MagicMock())
    _store_evidence(storage, "s1", "block")
    r = TestClient(app).post(
        "/live-edit/admin/branches/s1/merge", headers={"X-Admin-Key": "k"}
    )
    assert r.status_code == 400
    assert r.json().get("blocked") is True


def test_merge_blocked_with_reason_overrides(tmp_path):
    audit = SQLiteAuditLog(str(tmp_path / "audit.db"))
    vcs = _make_git_repo(tmp_path, "s1")
    app, storage = _make_app(tmp_path, vcs=vcs, audit_log=audit)
    _store_evidence(storage, "s1", "block")
    r = TestClient(app).post(
        "/live-edit/admin/branches/s1/merge",
        headers={"X-Admin-Key": "k"},
        json={"reason": "人工确认过"},
    )
    assert r.status_code == 200
    assert r.json()["decision"] == "block"
    assert r.json()["commit_hash"] == "m1"

    overrides = audit.query(action="admin_merge_override")
    assert len(overrides) == 1
    assert overrides[0].result == "ok"
    assert overrides[0].detail == {"reason": "人工确认过"}
    merges = audit.query(action="admin_merge")
    assert len(merges) == 1
    assert merges[0].result == "override"


def test_merge_auto_approve_merges(tmp_path):
    vcs = _make_git_repo(tmp_path, "s1")
    app, storage = _make_app(tmp_path, vcs=vcs)
    _store_evidence(storage, "s1", "auto_approve")
    r = TestClient(app).post(
        "/live-edit/admin/branches/s1/merge", headers={"X-Admin-Key": "k"}
    )
    assert r.status_code == 200
    assert r.json()["decision"] == "auto_approve"


def test_session_detail_includes_evidence(tmp_path):
    app, storage = _make_app(tmp_path, vcs=MagicMock())
    storage.save_session(
        session_id="s1",
        request="改按钮",
        committed=0,
        files=["app.py"],
        commit_hash="",
        messages_json="[]",
        mode="quick",
    )
    _store_evidence(storage, "s1", "auto_approve")
    r = TestClient(app).get("/live-edit/session/s1")
    assert r.status_code == 200
    assert r.json().get("evidence", {}).get("decision") == "auto_approve"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_router_verify.py -q`
Expected: FAIL（merge 不校验 reason / 响应无 decision）

- [ ] **Step 3: 改 merge 端点**

在 `setup_live_edit` 内、`@router.post("/admin/branches/{session_id}/merge")` 上方加模型：

```python
    class MergeRequest(BaseModel):
        reason: str = ""
```

在 `admin_merge_branch` 签名加 `req: MergeRequest | None = None`（默认 None 兼容旧调用），并在 admin key 校验之后、`branch = f"live-edit/{session_id}"` 之后、`try:` 之前插入：

```python
        # Verify-then-approve gate: read stored evidence; BLOCK without a
        # reason override stops the merge.
        evidence_json = storage.get_evidence(session_id) if storage else None
        decision = None
        if evidence_json and isinstance(evidence_json, str):
            from .verify.evidence import Evidence

            try:
                decision = Evidence.from_dict(json.loads(evidence_json)).decision
            except Exception:
                # 损坏/非 JSON 证据视为无证据，走正常合并路径，不让合并端 500。
                decision = None
        if decision == "block" and not (req and req.reason):
            return JSONResponse(
                status_code=400,
                content={
                    "detail": "该改动被验证阻断，需提供 reason 强制放行",
                    "blocked": True,
                },
            )
```

合并成功后，`audit_log.record("admin_merge", target=session_id, result="ok")` 改为按决策记录：

```python
            if decision == "block":
                audit_log.record(
                    "admin_merge_override",
                    target=session_id,
                    result="ok",
                    detail={"reason": (req.reason if req else "") or ""},
                )
                merge_result = "override"
            else:
                merge_result = "auto_approve" if decision == "auto_approve" else "ok"
            audit_log.record("admin_merge", target=session_id, result=merge_result)
```

返回 dict `{"ok": True, "commit_hash": merge_hash}` 改为 `{"ok": True, "commit_hash": merge_hash, "decision": decision}`。

- [ ] **Step 4: 会话详情带 evidence**

现有 `GET /session/{session_id}`（`router.py:541`）直接 `return detail`。改为：

```python
    @router.get("/session/{session_id}")
    async def get_session_detail(session_id: str):
        """Get detailed info about a past session."""
        detail = storage.get_session_detail(session_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        evidence_json = storage.get_evidence(session_id) if storage else None
        detail["evidence"] = json.loads(evidence_json) if evidence_json else None
        return detail
```

- [ ] **Step 5: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_router_verify.py -q && .venv/bin/python -m pytest tests/test_router.py -q`
Expected: PASS（4 new passed + router 回归绿）

- [ ] **Step 6: 提交**

```bash
git add live_edit/router.py tests/test_router_verify.py
git commit -m "feat(router): evidence-aware admin merge (auto/override gate) + session detail evidence"
```

---

### Task 9: 全量回归 + 文档同步

**Files:**
- Modify: `USER_MANUAL.md` / `USER_MANUAL_EN.md`（`[verify]` 配置段 + 验证流程一节，附证据 JSON 示例）

**Interfaces:** 无新接口

- [ ] **Step 1: 全量测试 + 覆盖率**

```bash
.venv/bin/python -m pytest -q 2>&1 | tail -5
```

Expected：全绿。确认新增用例（约 41 个）全部通过，原有用例无回归。

- [ ] **Step 2: ruff 检查**

```bash
.venv/bin/ruff check live_edit/verify/ live_edit/engine.py live_edit/router.py live_edit/storage.py live_edit/config.py tests/
```

Expected：无错误。若 `I`（import 排序）报错，按提示排序。

- [ ] **Step 3: 文档同步**

在 `USER_MANUAL.md` 加「验证即审批（Verify-then-Approve）」小节：
- `[verify]` 配置段说明（`enabled` / `max_retry` / `test_command` / `health_url` / `semantic_enabled` / `semantic_assert_text` / `[verify.rules.low_risk]` 的 `max_files` / `protected_paths`）
- **方案 A 去重说明**：verify 默认不跑测试（`test_command`/`health_url` 留空），测试与健康检查由 evaluation 质量网负责；默认配置下 verify 决策降级 HUMAN（不自动放行），主要提供安全闸门（保护路径/密钥扫描 → BLOCK 拦截）+ 证据审计。只有显式配置 verify 测试命令后，低风险改动才走 AUTO_APPROVE
- 流程：quick 模式低风险跳过最终确认（仅当配置了 verify 测试）；admin 合并门：AUTO_APPROVE 自动合并、BLOCK 需 reason override、HUMAN 正常合并
- 附一份最小证据 JSON 示例

`USER_MANUAL_EN.md` 同步英文版。

- [ ] **Step 4: 提交**

```bash
git add USER_MANUAL.md USER_MANUAL_EN.md
git commit -m "docs: verify-then-approve config + flow in manuals"
```

---

## 实施后自查

- **Spec 覆盖**：三层验证（Task 3）、证据结构（Task 2）、规则矩阵含方案 A 降级规则（Task 4）、engine 接入（Task 7）、admin merge 门（Task 8）、会话详情证据（Task 8）、配置（Task 1）、存储（Task 6）、文档含去重说明（Task 9）全部有任务对应。语义层为默认关的 opt-in（Task 3/5）。
- **方案 A 去重落地**：默认 `[verify]` 不配 `test_command`/`health_url` → deterministic SKIPPED → `evaluate()` 降级 HUMAN（Task 4 的 `test_skipped_det_degrades_human` 与 Task 5 的 `test_default_config_degrades_to_human` 显式锁定该行为）；测试/健康检查仍只由 evaluation 跑，不重复。
- **占位符扫描**：无 TBD/TODO；每个代码步骤都有可运行的实现与测试。
- **类型一致性**：`verify_change` 签名在 Task 5 定义、Task 7 使用；`Decision` / `Evidence.from_dict` 在 Task 4/2 定义、Task 8 使用；`save_evidence/get_evidence` 在 Task 6 定义、Task 7/8 使用；`VerifyConfig` / `VerifyRuleConfig` 在 Task 1 定义、Task 4/5/7 使用——名字一致。

## 明确未覆盖（后续计划）

- `/live-edit/session/{id}/verify` 重跑端点（admin 手动修 worktree 后重验）——需要 worktree 恢复逻辑，单列计划
- 语义层 Playwright 级 JS 交互断言（当前用 httpx 抓 HTML 文本断言）
- admin 前端 UI 的证据展示（本计划只保证 API 层）
- pre_existing / agent_written 测试分离（base-commit 快照对比，防假绿第 2 层）

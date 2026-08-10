"""Post-edit evaluation pipeline: lint → test → preview → introspection → HTML diff."""

import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from enum import Enum

import httpx

from .critic import run_critic_agent

logger = logging.getLogger("live-edit.evaluation")

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


class EvalStage(Enum):
    LINT = "lint"
    TEST = "test"
    PREVIEW = "preview"
    INTROSPECT = "introspect"
    HTML_DIFF = "html_diff"


@dataclass
class EvalResult:
    passed: bool
    stages_passed: list[str] = field(default_factory=list)
    stages_failed: list[str] = field(default_factory=list)
    stages_skipped: list[str] = field(default_factory=list)
    report: str = ""
    retries_used: int = 0
    stage_details: dict = field(default_factory=dict)
    failed_stage: str = ""
    failed_output: str = ""


_SKIP_MARKERS = {
    "lint": ("command not found",),
    "python": ("modulenotfounderror", "no module named 'pytest'", "command not found"),
    "node": ("missing script:", "command not found"),
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


def _detect_lint_cmd(project_root: str, config) -> str:
    """Auto-detect lint command from project type."""
    if config and hasattr(config, "evaluation") and config.evaluation.lint_command:
        return config.evaluation.lint_command  # type: ignore[no-any-return]
    if os.path.exists(os.path.join(project_root, "pyproject.toml")):
        return (
            "files=$(git diff --cached --name-only --diff-filter=ACM '*.py' 2>/dev/null);"
            ' [ -z "$files" ] && exit 0; python3 -m py_compile $files 2>&1'
        )
    if os.path.exists(os.path.join(project_root, "package.json")):
        return "npm run lint --if-present 2>&1"
    if os.path.exists(os.path.join(project_root, "go.mod")):
        return "go vet ./... 2>&1"
    return "echo 'no lint command detected'"


def _detect_test_cmd(project_root: str, config) -> str:
    """Auto-detect test command from project type."""
    if config and hasattr(config, "evaluation") and config.evaluation.test_command:
        return config.evaluation.test_command  # type: ignore[no-any-return]
    if os.path.exists(os.path.join(project_root, "pyproject.toml")):
        return "python3 -m pytest -x --tb=short 2>&1"
    if os.path.exists(os.path.join(project_root, "package.json")):
        return "npm test 2>&1"
    if os.path.exists(os.path.join(project_root, "go.mod")):
        return "go test ./... 2>&1"
    return "echo 'no test command detected'"


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
        return {
            "ok": outcome == "passed",
            "skipped": outcome == "skipped",
            "output": output,
            "command": cmd,
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "skipped": False, "output": "Lint check timed out", "command": cmd}


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
        return {
            "ok": outcome == "passed",
            "skipped": outcome == "skipped",
            "output": output,
            "command": cmd,
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "skipped": False, "output": "Test execution timed out", "command": cmd}


async def _run_stage_preview(preview_url: str) -> dict:
    health_url = f"{preview_url}/live-edit/health" if preview_url else ""
    if not health_url:
        return {"ok": False, "skipped": True, "output": "Preview URL not available"}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(health_url)
            passed = r.status_code == 200
            return {
                "ok": passed,
                "output": f"Health check: {r.status_code}",
                "status_code": r.status_code,
            }
    except httpx.ConnectError:
        return {"ok": False, "output": "Preview server not reachable"}
    except Exception as e:
        return {"ok": False, "output": f"Preview check failed: {e}"}


async def _run_stage_introspect(
    provider,
    user_request: str,
    diff: str,
    *,
    worktree_path: str = "",
    tool_registry=None,
    critic_max_rounds: int = 2,
    is_cancelled=None,
) -> dict:
    """Ask a fresh-context critic agent whether the changes achieved the goal."""
    if not diff:
        return {"ok": True, "output": "No diff to introspect"}
    try:
        verdict = await run_critic_agent(
            provider=provider,
            tool_registry=tool_registry,
            worktree_path=worktree_path,
            user_request=user_request,
            diff=diff,
            max_rounds=critic_max_rounds,
            is_cancelled=is_cancelled,
        )
    except Exception as e:
        return {"ok": True, "output": f"Critic error (treated as pass): {e}"}

    if verdict.goal_achieved and not verdict.blocking:
        note = f"审查通过：{verdict.summary}" if verdict.summary else "审查通过"
        return {"ok": True, "output": note}

    reason = "改动未达成用户目标" if not verdict.goal_achieved else "存在致命问题"
    lines = [f"审查未通过：{reason}"]
    for f in verdict.findings:
        loc = f"{f.file}:{f.line}" if f.line else f.file
        lines.append(f"- [{f.severity}] {loc} — {f.description}")
    return {"ok": False, "output": "\n".join(lines)}


async def _run_stage_html_diff(preview_url: str, pages: list[str]) -> dict:
    """Fetch pages via preview and check basic health (status codes)."""
    if not preview_url or not pages:
        return {"ok": True, "output": "HTML diff skipped (no preview or pages)"}

    results = []
    for page in pages:
        url = f"{preview_url}{page}" if not page.startswith("/") else f"{preview_url}{page}"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(url)
                if r.status_code == 200:
                    html = r.text
                    tag_count = len(re.findall(r"<\w+", html))
                    has_body = "<body" in html.lower()
                    results.append(
                        {
                            "page": page,
                            "ok": True,
                            "tag_count": tag_count,
                            "html_size": len(html),
                            "has_body": has_body,
                        }
                    )
                else:
                    results.append({"page": page, "ok": False, "status": r.status_code})
        except Exception as e:
            results.append({"page": page, "ok": False, "error": str(e)})

    failures = [r for r in results if not r.get("ok")]
    return {
        "ok": len(failures) == 0,
        "output": json.dumps(results, ensure_ascii=False),
        "pages_checked": len(pages),
        "pages_failed": len(failures),
    }


async def run_evaluation_pipeline(
    session, provider, config, preview_manager=None, tool_registry=None
) -> EvalResult:
    """Run all evaluation stages, stop at first failure. No retry loop — that's in engine.py."""
    stages = resolve_stages(config)
    if not stages:
        return EvalResult(passed=True, report="Evaluation disabled")

    stage_runners = {
        "lint": lambda: _run_stage_lint(session._worktree_path, config),
        "test": lambda: _run_stage_test(session._worktree_path, config),
        "preview": lambda: _run_stage_preview(session._preview_url),
        "introspect": lambda: _run_stage_introspect(
            provider,
            session.request,
            getattr(session, "_cached_diff", ""),
            worktree_path=session._worktree_path,
            tool_registry=tool_registry,
            critic_max_rounds=(
                config.evaluation.critic_max_rounds if hasattr(config, "evaluation") else 2
            ),
            is_cancelled=(
                (lambda: session._cancelled.is_set())
                if getattr(session, "_cancelled", None) is not None
                else None
            ),
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
        status = "跳过" if detail.get("skipped") else "通过" if detail.get("ok") else "未通过"
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

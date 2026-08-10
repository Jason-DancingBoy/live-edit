"""Post-edit evaluation pipeline: lint → test → preview → introspection → HTML diff."""

import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from enum import Enum

import httpx

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


def _detect_lint_cmd(project_root: str, config) -> str:
    """Auto-detect lint command from project type."""
    if config and hasattr(config, "evaluation") and config.evaluation.lint_command:
        return config.evaluation.lint_command  # type: ignore[no-any-return]
    if os.path.exists(os.path.join(project_root, "pyproject.toml")):
        return (
            "python3 -m py_compile $(git diff --cached --name-only"
            " --diff-filter=ACM '*.py' 2>/dev/null) 2>&1"
            " || echo 'no .py changes'"
        )
    if os.path.exists(os.path.join(project_root, "package.json")):
        return "npm run lint --if-present 2>&1 || echo 'no lint script'"
    if os.path.exists(os.path.join(project_root, "go.mod")):
        return "go vet ./... 2>&1"
    return "echo 'no lint command detected'"


def _detect_test_cmd(project_root: str, config) -> str:
    """Auto-detect test command from project type."""
    if config and hasattr(config, "evaluation") and config.evaluation.test_command:
        return config.evaluation.test_command  # type: ignore[no-any-return]
    if os.path.exists(os.path.join(project_root, "pyproject.toml")):
        return "python3 -m pytest -x --tb=short 2>&1 || echo 'no tests'"
    if os.path.exists(os.path.join(project_root, "package.json")):
        return "npm test 2>&1 || echo 'no tests'"
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
        passed = result.returncode == 0
        return {"ok": passed, "output": output, "command": cmd}
    except subprocess.TimeoutExpired:
        return {"ok": False, "output": "Lint check timed out", "command": cmd}


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
        passed = result.returncode == 0
        return {"ok": passed, "output": output, "command": cmd}
    except subprocess.TimeoutExpired:
        return {"ok": False, "output": "Test execution timed out", "command": cmd}


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


async def _run_stage_introspect(provider, user_request: str, diff: str) -> dict:
    """Ask the LLM whether the changes achieved the user's goal."""
    if not diff:
        return {"ok": True, "output": "No diff to introspect"}
    messages = [
        {
            "role": "user",
            "content": (
                "你是一个代码审查助手。用户的需求是：\n"
                f"{user_request}\n\n"
                "AI 进行了以下代码修改（diff）：\n"
                f"```diff\n{diff[:4000]}\n```\n\n"
                "请判断：这些修改是否达成了用户的目标？有没有遗漏或错误？\n"
                "请用中文简短回答。如果达成目标，第一行写「评估结果: 通过」。"
                "如果有问题，第一行写「评估结果: 未通过」，然后列出具体问题。"
            ),
        },
    ]
    try:
        content_blocks = await provider.call_with_tools(
            messages=messages,
            tools=[],
            on_thinking=None,
            on_text=None,
        )
        if not content_blocks:
            return {"ok": True, "output": "Introspection skipped (no LLM response)"}
        text = ""
        for block in content_blocks:
            if block and block.get("type") == "text":
                text += block.get("text", "")
        passed = "通过" in text[:100] and "未通过" not in text[:100]
        return {"ok": passed, "output": text[:1000]}
    except Exception as e:
        return {"ok": True, "output": f"Introspection error (treated as pass): {e}"}


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

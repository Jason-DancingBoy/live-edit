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

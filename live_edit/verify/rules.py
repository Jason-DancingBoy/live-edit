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

    # 方案 A 契约：只有显式配置的 verify 测试命令（test_command）检查通过才允许
    # AUTO_APPROVE。仅配 health_url 时 deterministic 层因 health pass 是 "pass"，
    # 但 test_command 仍是 skipped —— 没有实际测试验证，不得自动放行。
    tc = next((c for c in det.get("checks", []) if c.get("id") == "test_command"), None)
    if tc is None or tc.get("status") != CheckStatus.PASS:
        return (Decision.HUMAN, "未配置实际验证，降级人工")

    return (Decision.AUTO_APPROVE, "低风险自动放行")

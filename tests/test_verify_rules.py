# tests/test_verify_rules.py
from live_edit.config import VerifyConfig
from live_edit.verify.evidence import Evidence
from live_edit.verify.rules import Decision, evaluate


def _ev(**kw):
    defaults = {"session_id": "s1", "commit_hash": "", "layers": {}, "verify_attempts": 0}
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


def test_semantic_fail_blocks():
    """Rule 6: overall != pass (e.g. semantic layer fail) without earlier triggers → BLOCK."""
    cfg = VerifyConfig()
    ev = _ev(
        layers={
            "deterministic": {"status": "pass"},
            "diff_safety": {"status": "pass"},
            "semantic": {"status": "fail"},
        }
    )
    d, reason = evaluate(ev, cfg)
    assert d == Decision.BLOCK
    assert "全绿" in reason

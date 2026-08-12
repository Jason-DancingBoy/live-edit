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

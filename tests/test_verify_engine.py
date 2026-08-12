# tests/test_verify_engine.py
from live_edit.engine import _verify_auto_approves
from live_edit.verify.evidence import Evidence


def test_auto_approve_helper():
    ev = Evidence(session_id="s1", commit_hash="", layers={}, decision="auto_approve")
    assert _verify_auto_approves(ev) is True


def test_non_auto_or_none():
    ev = Evidence(session_id="s1", commit_hash="", layers={}, decision="human")
    assert _verify_auto_approves(ev) is False
    assert _verify_auto_approves(None) is False

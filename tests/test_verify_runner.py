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

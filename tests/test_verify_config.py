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

import sys

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

import pytest

from live_edit.config import VerifyConfig, parse_config


def test_verify_config_defaults():
    cfg = VerifyConfig()
    assert cfg.enabled is True
    assert cfg.max_retry == 3
    assert cfg.test_command == ""  # 方案 A：默认不跑测试，测试归 evaluation
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


def test_parse_config_verify_round_trip(tmp_path):
    toml_path = tmp_path / ".live-edit.toml"
    toml_path.write_text("""
[project]
name = "TestApp"
language = "python"

[llm]
api_url = "https://api.example.com"
api_key_env = "KEY"
model = "m1"

[modes.quick]
label = "Quick"

[verify]
enabled = true
max_retry = 5
test_command = "pytest -q"
health_url = "http://localhost:8083/health"
semantic_enabled = true
semantic_assert_text = ["订单已创建", "保存成功"]

[verify.rules.low_risk]
max_files = 20
protected_paths = ["auth/", "*.key"]
""")
    config = parse_config(str(toml_path))
    verify = config.verify
    assert verify.enabled is True
    assert verify.max_retry == 5
    assert verify.test_command == "pytest -q"
    assert verify.health_url == "http://localhost:8083/health"
    assert verify.semantic_enabled is True
    assert verify.semantic_assert_text == ["订单已创建", "保存成功"]
    assert verify.rules.max_files == 20
    assert verify.rules.protected_paths == ["auth/", "*.key"]


def test_parse_config_verify_defaults(tmp_path):
    toml_path = tmp_path / ".live-edit.toml"
    toml_path.write_text("""
[project]
name = "TestApp"
language = "python"

[llm]
api_url = "https://api.example.com"
api_key_env = "KEY"
model = "m1"

[modes.quick]
label = "Quick"
""")
    config = parse_config(str(toml_path))
    verify = config.verify
    assert verify.enabled is True
    assert verify.max_retry == 3
    assert verify.test_command == ""
    assert verify.health_url == ""
    assert verify.semantic_enabled is False
    assert verify.semantic_assert_text == []
    assert verify.rules.max_files == 10
    assert verify.rules.protected_paths == []

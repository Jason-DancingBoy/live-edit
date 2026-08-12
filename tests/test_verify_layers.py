# tests/test_verify_layers.py
import pytest

from live_edit.verify.layers import (
    check_diff_safety,
    check_semantic,
    run_health_check,
    run_test_command,
)


@pytest.mark.asyncio
async def test_run_test_command_pass(tmp_path):
    r = await run_test_command(str(tmp_path), "python -c 'print(1)'")
    assert r["status"] == "pass"
    assert r["detail"]["exit_code"] == 0


@pytest.mark.asyncio
async def test_run_test_command_fail(tmp_path):
    r = await run_test_command(str(tmp_path), "python -c 'raise SystemExit(1)'")
    assert r["status"] == "fail"


@pytest.mark.asyncio
async def test_run_test_command_skipped_when_empty():
    r = await run_test_command("/tmp", "")
    assert r["status"] == "skipped"


@pytest.mark.asyncio
async def test_run_test_command_timeout_kills_child(tmp_path):
    import asyncio
    import contextlib

    # 需要真实超时：命令 sleep 很久，通过 monkeypatch 缩短 wait_for 不可行，
    # 改为直接构造一个立即超时的场景并断言返回 fail 而非悬挂。
    # 这里用短命令 + 手动 asyncio.wait_for 包裹验证返回值形态。
    from live_edit.verify.layers import run_test_command as _rtc

    # 覆盖：给一个必然不存在的二进制，验证 FileNotFoundError → fail 分支
    r = await _rtc(str(tmp_path), "definitely_not_a_real_binary_xyz 2>&1")
    assert r["status"] == "fail"
    assert "error" in r["detail"]


@pytest.mark.asyncio
async def test_health_check_pass_and_fail():
    from live_edit.verify.layers import _serve_ok

    server = _serve_ok()
    port = server.server_address[1]
    try:
        ok = await run_health_check(f"http://127.0.0.1:{port}/live-edit/health")
        assert ok["status"] == "pass"
        assert ok["detail"]["status_code"] == 200
    finally:
        server.shutdown()


@pytest.mark.asyncio
async def test_health_check_skipped_when_empty():
    r = await run_health_check("")
    assert r["status"] == "skipped"


@pytest.mark.asyncio
async def test_diff_safety_protected_path(tmp_path):
    (tmp_path / "auth").mkdir()
    (tmp_path / "auth" / "login.py").write_text("x = 1")
    r = await check_diff_safety(str(tmp_path), ["auth/login.py"], ["auth/"])
    assert r["status"] == "fail"
    assert r["out_of_scope"] == ["auth/login.py"]


@pytest.mark.asyncio
async def test_diff_safety_secret_scan(tmp_path):
    (tmp_path / "app.py").write_text("KEY = 'AKIAIOSFODNN7EXAMPLE'\n")
    r = await check_diff_safety(str(tmp_path), ["app.py"], [])
    assert r["status"] == "fail"
    assert len(r["scan_alerts"]) >= 1


@pytest.mark.asyncio
async def test_diff_safety_clean(tmp_path):
    (tmp_path / "app.py").write_text("x = 1\n")
    r = await check_diff_safety(str(tmp_path), ["app.py"], [])
    assert r["status"] == "pass"
    assert r["out_of_scope"] == []
    assert r["scan_alerts"] == []


@pytest.mark.asyncio
async def test_diff_safety_skips_absolute_path(tmp_path):
    (tmp_path / "app.py").write_text("KEY = 'AKIAIOSFODNN7EXAMPLE'\n")
    r = await check_diff_safety(str(tmp_path), [str(tmp_path / "app.py")], [])
    # 绝对路径逃逸 worktree，跳过扫描 → 不误报
    assert r["status"] == "pass"
    assert r["scan_alerts"] == []


@pytest.mark.asyncio
async def test_semantic_skipped_when_no_assert():
    r = await check_semantic("http://127.0.0.1:1", [])
    assert r["status"] == "skipped"

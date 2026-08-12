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
async def test_run_test_command_file_not_found_error(tmp_path):
    # 覆盖：给一个必然不存在的二进制，验证 FileNotFoundError → fail 分支
    r = await run_test_command(str(tmp_path), "definitely_not_a_real_binary_xyz 2>&1")
    assert r["status"] == "fail"
    assert "error" in r["detail"]


@pytest.mark.asyncio
async def test_run_test_command_timeout_kills_child(tmp_path, monkeypatch):
    import asyncio as _asyncio

    async def _raise_timeout(coro, *args, **kwargs):
        # wait_for 被 patch 后 proc.communicate() 的协程不会被执行，
        # 显式 close 避免 "coroutine was never awaited" 警告。
        coro.close()
        raise _asyncio.TimeoutError("timed out")

    # 强制 asyncio.wait_for 抛 TimeoutError，真实走到 layers.py 的 kill+reap 分支。
    # sleep 5 会真实启动子进程，kill/wait 回收的是真进程。
    monkeypatch.setattr("live_edit.verify.layers.asyncio.wait_for", _raise_timeout)

    r = await run_test_command(str(tmp_path), "sleep 5")
    assert r["status"] == "fail"
    assert "child process killed" in r["detail"]["error"]
    assert "timeout" in r["detail"]["error"].lower()


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
async def test_diff_safety_skips_absolute_path_outside_worktree(tmp_path):
    outside = tmp_path.parent / "outside_app.py"
    outside.write_text("KEY = 'AKIAIOSFODNN7EXAMPLE'\n")
    r = await check_diff_safety(str(tmp_path), [str(outside)], [])
    # 绝对路径越出 worktree，跳过扫描 → 不误报
    assert r["status"] == "pass"
    assert r["scan_alerts"] == []


@pytest.mark.asyncio
async def test_diff_safety_scans_absolute_path_inside_worktree(tmp_path):
    # 绝对路径归一化后仍在 worktree 内 → 照常扫描，能抓到密钥。
    (tmp_path / "app.py").write_text("KEY = 'AKIAIOSFODNN7EXAMPLE'\n")
    r = await check_diff_safety(str(tmp_path), [str(tmp_path / "app.py")], [])
    assert r["status"] == "fail"
    assert len(r["scan_alerts"]) >= 1


@pytest.mark.asyncio
async def test_diff_safety_skips_traversal_outside_worktree(tmp_path):
    """../secret 相对越界会指向 worktree 外的文件，必须跳过扫描，绝不读取外部内容。"""
    outside = tmp_path / "secret.env"
    outside.write_text("KEY = 'AKIAIOSFODNN7EXAMPLE'\n")
    r = await check_diff_safety(str(tmp_path / "wt"), ["../secret.env"], [])
    assert r["status"] == "pass"
    assert r["scan_alerts"] == []
    assert r["files_touched"] == ["../secret.env"]


@pytest.mark.asyncio
async def test_semantic_skipped_when_no_assert():
    r = await check_semantic("http://127.0.0.1:1", [])
    assert r["status"] == "skipped"

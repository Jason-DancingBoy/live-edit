# live_edit/verify/layers.py
"""Three verification layers: deterministic, diff safety, semantic."""
from __future__ import annotations

import asyncio
import contextlib
import http.server
import re
import shlex
import threading
from fnmatch import fnmatch
from pathlib import Path

import httpx

from .evidence import CheckStatus

_SECRET_PATTERNS: list[tuple[str, str]] = [
    (r"AKIA[0-9A-Z]{16}", "aws_access_key"),
    (r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----", "private_key"),
    (r"(?i)(api[_-]?key|secret|token|password)\s*[=:]\s*['\"][^'\"]{12,}", "inline_secret"),
]


def _matches_any(path: str, patterns: list[str]) -> bool:
    return any(fnmatch(path, p) or path.startswith(p.rstrip("/") + "/") for p in patterns)


async def run_test_command(worktree: str, command: str) -> dict:
    if not command or not command.strip():
        return {"status": CheckStatus.SKIPPED, "detail": {"command": command}}
    try:
        proc = await asyncio.create_subprocess_exec(
            *shlex.split(command),
            cwd=worktree,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
    except (asyncio.TimeoutError, FileNotFoundError) as e:
        error = str(e)
        if isinstance(e, asyncio.TimeoutError):
            # 超时分支必须回收子进程，避免悬空的长时间运行命令泄漏。
            # kill() 可能撞上子进程恰好已退出的瞬间 → 抑制 ProcessLookupError。
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
            error = f"{error} (child process killed after 120s timeout)"
        return {"status": CheckStatus.FAIL, "detail": {"command": command, "error": error}}
    return {
        "status": CheckStatus.PASS if proc.returncode == 0 else CheckStatus.FAIL,
        "detail": {
            "command": command,
            "exit_code": proc.returncode,
            "output_tail": (out or b"")[-2000:].decode(errors="replace"),
        },
    }


async def run_health_check(health_url: str) -> dict:
    if not health_url:
        return {"status": CheckStatus.SKIPPED, "detail": {"url": ""}}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(health_url)
        status = CheckStatus.PASS if r.status_code == 200 else CheckStatus.FAIL
        return {"status": status, "detail": {"url": health_url, "status_code": r.status_code}}
    except Exception as e:  # noqa: BLE001 — 网络错误统一视为失败
        return {"status": CheckStatus.FAIL, "detail": {"url": health_url, "error": str(e)}}


def _scan_file_for_secrets(path: Path) -> list[dict]:
    alerts: list[dict] = []
    try:
        content = path.read_text(errors="replace")
    except OSError:
        return alerts
    for pattern, kind in _SECRET_PATTERNS:
        if re.search(pattern, content):
            alerts.append({"file": str(path), "kind": kind})
    return alerts


async def check_diff_safety(
    worktree: str, modified_files: list[str], protected_paths: list[str]
) -> dict:
    files_touched = sorted(set(modified_files))
    out_of_scope = [f for f in files_touched if _matches_any(f, protected_paths)]
    scan_alerts: list[dict] = []
    worktree_root = Path(worktree).resolve()
    for f in files_touched:
        # 归一化后若越过 worktree 边界（绝对路径，或 "../secret" 这类相对越界），
        # 跳过扫描——绝不读取项目目录外的文件。
        candidate = (worktree_root / f).resolve()
        try:
            candidate.relative_to(worktree_root)
        except ValueError:
            continue
        scan_alerts.extend(_scan_file_for_secrets(candidate))
    status = CheckStatus.FAIL if (out_of_scope or scan_alerts) else CheckStatus.PASS
    return {
        "status": status,
        "files_touched": files_touched,
        "out_of_scope": out_of_scope,
        "scan_alerts": scan_alerts,
    }


async def check_semantic(preview_url: str, assert_text: list[str]) -> dict:
    if not preview_url or not assert_text:
        return {"status": CheckStatus.SKIPPED, "detail": {}}
    checks: list[dict] = []
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(preview_url)
        html = r.text
    except Exception as e:  # noqa: BLE001
        return {
            "status": CheckStatus.FAIL,
            "checks": [{"text": t, "found": False, "error": str(e)} for t in assert_text],
        }
    for text in assert_text:
        checks.append({"text": text, "found": text in html})
    status = CheckStatus.PASS if all(c["found"] for c in checks) else CheckStatus.FAIL
    return {"status": status, "checks": checks}


# ── 测试用迷你 HTTP 服务器（仅测试导入，生产不调用）──


class _OkHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):  # noqa: D102
        pass


def _serve_ok():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _OkHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server

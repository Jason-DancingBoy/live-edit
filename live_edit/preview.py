"""Per-session preview service manager.

Spawns a dedicated uvicorn process for each LiveEdit session, running from
the session's git worktree, so the editing user can preview their changes
before merging to the main branch.
"""

import asyncio
import logging
import os
import socket

import httpx

from .config import PreviewConfig

logger = logging.getLogger("live-edit.preview")

_LIVE_EDIT_SRC = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
_WORKTREE_ROOT = "/tmp/live-edit"
DEFAULT_COMMAND = "uvicorn server:app --host 127.0.0.1 --port {port}"


class PortAllocator:
    """Tracks allocated ports and checks TCP availability."""

    def __init__(self):
        self._used: set[int] = set()

    def allocate(self, start: int, end: int) -> int | None:
        for port in range(start, end + 1):
            if port in self._used:
                continue
            if not self._port_available(port):
                continue
            self._used.add(port)
            return port
        return None

    def release(self, port: int):
        self._used.discard(port)

    @staticmethod
    def _port_available(port: int) -> bool:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.5)
                s.connect(("127.0.0.1", port))
                return False
        except (ConnectionRefusedError, socket.timeout, OSError):
            return True


class PreviewManager:
    """Manages per-session preview service instances."""

    def __init__(self, config: PreviewConfig):
        self._config = config
        self._allocator = PortAllocator()
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._ports: dict[str, int] = {}
        self._urls: dict[str, str] = {}
        self._public_urls: dict[str, str] = {}

    async def start(self, session_id: str, worktree_path: str) -> str | None:
        """Start a preview service for a session. Returns the URL or None."""
        if not self._config.enabled:
            return None

        # Idempotent: already running for this session
        if session_id in self._public_urls:
            return self._public_urls[session_id]

        port = self._allocator.allocate(
            self._config.port_start, self._config.port_end)
        if port is None:
            logger.warning("Preview: no available port in range %d-%d for session %s",
                           self._config.port_start, self._config.port_end, session_id)
            return None

        self._ensure_symlink()

        command = self._config.command or DEFAULT_COMMAND
        command = command.replace("{port}", str(port))

        logger.info("Preview [%s]: starting on port %d: %s", session_id, port, command)

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                cwd=worktree_path,
                env=os.environ,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except Exception as e:
            logger.warning("Preview [%s]: failed to spawn process: %s", session_id, e)
            self._allocator.release(port)
            return None

        self._processes[session_id] = proc
        self._ports[session_id] = port

        # Health check polling
        url = f"http://127.0.0.1:{port}"
        health_url = f"{url}/live-edit/health"
        deadline = asyncio.get_event_loop().time() + self._config.startup_timeout

        while asyncio.get_event_loop().time() < deadline:
            if proc.returncode is not None:
                logger.warning("Preview [%s]: process exited early (code=%d)",
                               session_id, proc.returncode)
                await self.stop(session_id)
                return None
            try:
                async with httpx.AsyncClient(timeout=2.0) as client:
                    r = await client.get(health_url)
                    if r.status_code == 200:
                        self._urls[session_id] = url
                        # Return proxy URL if base_url is configured, otherwise direct URL
                        public_url = url
                        if self._config.base_url:
                            public_url = f"{self._config.base_url}/live-edit/p/{session_id}"
                        self._public_urls[session_id] = public_url
                        logger.info("Preview [%s]: ready at %s (proxy: %s)", session_id, url, public_url)
                        return public_url
            except Exception:
                pass
            await asyncio.sleep(0.5)

        logger.warning("Preview [%s]: health check timed out after %ds",
                       session_id, self._config.startup_timeout)
        await self.stop(session_id)
        return None

    async def stop(self, session_id: str):
        """Stop and clean up a preview service."""
        proc = self._processes.pop(session_id, None)
        port = self._ports.pop(session_id, None)
        self._urls.pop(session_id, None)
        self._public_urls.pop(session_id, None)

        if proc is None:
            return

        try:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                proc.kill()
                await asyncio.wait_for(proc.wait(), timeout=3.0)
        except ProcessLookupError:
            pass
        except Exception as e:
            logger.warning("Preview [%s]: error stopping process: %s", session_id, e)

        if port is not None:
            self._allocator.release(port)

        logger.info("Preview [%s]: stopped", session_id)

    def get_url(self, session_id: str) -> str | None:
        return self._urls.get(session_id)

    def _ensure_symlink(self):
        """Ensure /tmp/live-edit/live-edit -> /root/agent/live-edit symlink exists."""
        target = os.path.join(_WORKTREE_ROOT, "live-edit")
        if os.path.islink(target):
            return
        if os.path.exists(target):
            return
        os.makedirs(_WORKTREE_ROOT, exist_ok=True)
        os.symlink(_LIVE_EDIT_SRC, target, target_is_directory=True)
        logger.info("Preview: created symlink %s -> %s", target, _LIVE_EDIT_SRC)

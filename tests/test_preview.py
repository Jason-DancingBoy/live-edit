"""Regression tests for live_edit.preview — preview service spawning.

Covers the env-type invariant: the subprocess shell must receive a plain
``dict`` for ``env``, not the ``os._Environ`` object. The lyric-muse production
server runs uvicorn with uvloop, whose subprocess implementation strictly
validates ``isinstance(env, dict)`` and raises ``TypeError`` otherwise, so the
preview process is never spawned. This test asserts the invariant without
requiring uvloop in the test venv.
"""

import os
from unittest.mock import AsyncMock, MagicMock, patch

from live_edit.config import PreviewConfig
from live_edit.preview import PreviewManager


def _unhealthy_client_factory() -> MagicMock:
    """Return a MagicMock standing in for httpx.AsyncClient, never reporting 200.

    The preview health-check loop calls ``httpx.AsyncClient(...)`` synchronously
    and uses the result as an ``async with`` target, so the factory is a plain
    MagicMock (not an AsyncMock, whose call would yield an un-awaited coroutine).
    The mocked client short-circuits the real network I/O so the test stays
    hermetic, while returning a non-200 status so the loop always times out and
    exercises the ``stop()`` cleanup path.
    """
    response = MagicMock()
    response.status_code = 500

    client = MagicMock()
    client.get = AsyncMock(return_value=response)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)

    return MagicMock(return_value=client)


async def test_start_passes_plain_dict_env_to_subprocess():
    """env passed to create_subprocess_shell must be a plain dict, not os._Environ."""
    manager = PreviewManager(PreviewConfig(enabled=True, startup_timeout=0.01))

    mock_proc = MagicMock()
    mock_proc.returncode = None
    mock_proc.stderr = None
    mock_proc.wait = AsyncMock()

    mock_shell = AsyncMock(return_value=mock_proc)

    with (
        patch("live_edit.preview.asyncio.create_subprocess_shell", mock_shell),
        patch("live_edit.preview.httpx.AsyncClient", _unhealthy_client_factory()),
        patch.object(manager, "_ensure_symlink"),
    ):
        await manager.start(session_id="test-session", worktree_path="/tmp")

    call_kwargs = mock_shell.await_args.kwargs

    assert "env" in call_kwargs, "create_subprocess_shell must receive an env argument"
    assert type(call_kwargs["env"]) is dict, (
        f"env must be a plain dict, got {type(call_kwargs['env'])}"
    )
    assert call_kwargs["env"].get("PATH") == os.environ.get("PATH")

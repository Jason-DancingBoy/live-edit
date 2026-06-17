"""live-edit: Natural-language-driven live code editing for any web application.

Usage:
    from live_edit import setup_live_edit
    app.include_router(setup_live_edit())

Custom implementations:
    from live_edit import Provider, Storage, VCS
    app.include_router(setup_live_edit(provider=MyProvider(), storage=MyStorage()))
"""


def setup_live_edit(*args, **kwargs):
    """Lazy import to avoid circular deps during development."""
    from .router import setup_live_edit as _setup
    return _setup(*args, **kwargs)


# ── Public interfaces (for custom implementations) ──

from .provider import Provider, AnthropicCompatibleProvider
from .storage import Storage, SQLiteStorage
from .vcs import VCS, GitVCS, RevertPreview, RevertResult
from .config import Config, parse_config, validate_config, detect_project
from .engine import EditSession, SessionStore, build_timeline, translate_error
from .preview import PreviewManager

__all__ = [
    # Setup
    "setup_live_edit",
    # Provider
    "Provider",
    "AnthropicCompatibleProvider",
    # Storage
    "Storage",
    "SQLiteStorage",
    # VCS
    "VCS",
    "GitVCS",
    "RevertPreview",
    "RevertResult",
    # Config
    "Config",
    "parse_config",
    "validate_config",
    "detect_project",
    # Engine
    "EditSession",
    "SessionStore",
    "build_timeline",
    "translate_error",
    # Preview
    "PreviewManager",
]

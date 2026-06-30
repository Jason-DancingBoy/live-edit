"""Session-scoped fixtures for all tests."""

import pytest


@pytest.fixture(scope="session", autouse=True)
def _setup_tool_registry():
    """Initialize the global tool registry for backward-compat tests."""
    import live_edit.tools as tools
    from live_edit.tool_registry import DefaultToolRegistry

    registry = DefaultToolRegistry()
    registry.load_builtin_tools()
    tools._set_registry(registry)
    tools._refresh_globals("quick")

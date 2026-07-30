"""Tests for tool_registry.py"""

import pytest

from live_edit.tool_registry import DefaultToolRegistry, ToolDef


async def _echo(args, project_root, config=None):
    return {"ok": True, "echo": args.get("msg", "")}


def test_register_and_get_tool():
    registry = DefaultToolRegistry()
    td = ToolDef(
        name="test",
        description="A test tool",
        input_schema={"type": "object", "properties": {}},
        execute=_echo,
    )
    registry.register(td)
    assert registry.get_tool("test") is td
    assert registry.get_tool("nonexistent") is None


def test_get_tools_returns_anthropic_schemas():
    registry = DefaultToolRegistry()
    td = ToolDef(
        name="test",
        description="Desc",
        input_schema={"type": "object", "properties": {}},
        execute=_echo,
    )
    registry.register(td)
    schemas = registry.get_tools("quick")
    assert len(schemas) == 1
    assert schemas[0]["name"] == "test"
    assert schemas[0]["description"] == "Desc"


def test_mode_filtering():
    registry = DefaultToolRegistry()
    deep_only = ToolDef(
        name="deep_tool", description="", input_schema={}, execute=_echo, modes=["deep"]
    )
    all_modes = ToolDef(name="any_tool", description="", input_schema={}, execute=_echo, modes=None)
    registry.register(deep_only)
    registry.register(all_modes)

    quick = [t["name"] for t in registry.get_tools("quick")]
    assert "any_tool" in quick
    assert "deep_tool" not in quick

    deep = [t["name"] for t in registry.get_tools("deep")]
    assert "deep_tool" in deep
    assert "any_tool" in deep


def test_priority_override():
    registry = DefaultToolRegistry()
    low = ToolDef(name="same", description="low", input_schema={}, execute=_echo, priority=10)
    high = ToolDef(name="same", description="high", input_schema={}, execute=_echo, priority=20)
    registry.register(low)
    registry.register(high)
    assert registry.get_tool("same").description == "high"


def test_get_write_tool_names():
    registry = DefaultToolRegistry()
    registry.register(
        ToolDef(name="read", description="", input_schema={}, execute=_echo, is_write=False)
    )
    registry.register(
        ToolDef(name="write", description="", input_schema={}, execute=_echo, is_write=True)
    )
    registry.register(
        ToolDef(
            name="approve", description="", input_schema={}, execute=_echo, require_approval=True
        )
    )
    names = registry.get_write_tool_names("quick")
    assert "write" in names
    assert "approve" in names
    assert "read" not in names


@pytest.mark.asyncio
async def test_execute_tool():
    registry = DefaultToolRegistry()
    registry.register(ToolDef(name="echo", description="", input_schema={}, execute=_echo))
    result = await registry.execute("echo", {"msg": "hello"}, "/tmp", None)
    assert result["ok"] is True
    assert result["echo"] == "hello"


@pytest.mark.asyncio
async def test_execute_unknown_tool():
    registry = DefaultToolRegistry()
    result = await registry.execute("nope", {}, "/tmp", None)
    assert result["ok"] is False


def test_list_tools():
    registry = DefaultToolRegistry()
    registry.register(ToolDef(name="a", description="", input_schema={}, execute=_echo, modes=None))
    registry.register(
        ToolDef(name="b", description="", input_schema={}, execute=_echo, modes=["deep"])
    )
    assert registry.list_tools() == ["a", "b"]
    assert registry.list_tools("quick") == ["a"]


def test_to_anthropic_schema():
    td = ToolDef(
        name="test",
        description="A test",
        input_schema={"type": "object", "properties": {"x": {"type": "string"}}},
        execute=_echo,
    )
    schema = td.to_anthropic_schema()
    assert schema["name"] == "test"
    assert schema["description"] == "A test"
    assert schema["input_schema"]["properties"]["x"]["type"] == "string"


def test_visible_in_mode():
    td_all = ToolDef(name="a", description="", input_schema={}, execute=_echo, modes=None)
    td_deep = ToolDef(name="b", description="", input_schema={}, execute=_echo, modes=["deep"])
    assert td_all.visible_in_mode("quick") is True
    assert td_all.visible_in_mode("deep") is True
    assert td_deep.visible_in_mode("quick") is False
    assert td_deep.visible_in_mode("deep") is True

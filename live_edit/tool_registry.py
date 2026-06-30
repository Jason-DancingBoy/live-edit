"""Plugin-based tool registry with built-in, TOML, and Python plugin support."""

import asyncio
import importlib.util
import logging
import os
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, Awaitable

logger = logging.getLogger("live-edit.tool-registry")


@dataclass
class ToolDef:
    """Definition of a single tool."""
    name: str
    description: str
    input_schema: dict
    execute: Callable[[dict, str, object], Awaitable[dict]]
    modes: list[str] | None = None       # None = all modes
    is_write: bool = False
    require_approval: bool = False
    timeout: int = 30
    priority: int = 0                     # higher = wins on name collision

    def to_anthropic_schema(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }

    def visible_in_mode(self, mode: str) -> bool:
        if self.modes is None:
            return True
        return mode in self.modes


class ToolRegistry(ABC):
    """Abstract tool registry — pluggable like Provider/Storage/VCS."""

    @abstractmethod
    def register(self, tool: ToolDef) -> None:
        """Register a tool. Higher priority overrides lower on name collision."""
        ...

    @abstractmethod
    def get_tool(self, name: str) -> ToolDef | None:
        """Get a single tool definition by name."""
        ...

    @abstractmethod
    def get_tools(self, mode: str) -> list[dict]:
        """Return Anthropic-format tool schemas for a given mode."""
        ...

    @abstractmethod
    def get_write_tool_names(self, mode: str) -> set[str]:
        """Return names of tools that modify files, for approval logic."""
        ...

    @abstractmethod
    async def execute(self, name: str, args: dict, project_root: str, config=None) -> dict:
        """Execute a tool by name. Returns {ok, ...}."""
        ...

    @abstractmethod
    def list_tools(self, mode: str = "") -> list[str]:
        """List tool names visible in a given mode. Empty mode = all tools."""
        ...


class DefaultToolRegistry(ToolRegistry):
    """Default implementation aggregating built-in, TOML, and plugin tools."""

    def __init__(self):
        self._tools: dict[str, ToolDef] = {}

    def register(self, tool: ToolDef) -> None:
        existing = self._tools.get(tool.name)
        if existing and existing.priority >= tool.priority:
            return  # existing has higher or equal priority, keep it
        self._tools[tool.name] = tool

    def get_tool(self, name: str) -> ToolDef | None:
        return self._tools.get(name)

    def get_tools(self, mode: str) -> list[dict]:
        schemas = []
        for tool in self._tools.values():
            if tool.visible_in_mode(mode):
                schemas.append(tool.to_anthropic_schema())
        return schemas

    def get_write_tool_names(self, mode: str) -> set[str]:
        return {
            t.name for t in self._tools.values()
            if t.visible_in_mode(mode) and (t.is_write or t.require_approval)
        }

    async def execute(self, name: str, args: dict, project_root: str, config=None) -> dict:
        tool = self._tools.get(name)
        if tool is None:
            return {"ok": False, "error": f"未知工具: {name}"}
        try:
            result = await asyncio.wait_for(
                tool.execute(args, project_root, config),
                timeout=tool.timeout,
            )
            return result
        except asyncio.TimeoutError:
            return {"ok": False, "error": f"工具 {name} 执行超时"}
        except Exception as e:
            import traceback
            logger.error("Tool %s error: %s\n%s", name, e, traceback.format_exc())
            return {"ok": False, "error": str(e)}

    def list_tools(self, mode: str = "") -> list[str]:
        if mode:
            return sorted(t.name for t in self._tools.values() if t.visible_in_mode(mode))
        return sorted(self._tools.keys())

    def load_builtin_tools(self):
        """Import and register all tools from live_edit.builtin_tools."""
        from . import builtin_tools
        for mod in builtin_tools.ALL_MODULES:
            tool_def = mod.create()
            tool_def.priority = 10   # built-in priority
            self.register(tool_def)

    def load_toml_tools(self, config):
        """Parse [[tools]] sections from config and register shell-based tools."""
        if not config or not hasattr(config, 'toml_tools'):
            return
        from .safety import check_shell_cmd
        for t in config.toml_tools:
            cmd = t["command"]
            timeout_val = t.get("timeout", 30)

            async def _make_execute(cmd=cmd, timeout=timeout_val):
                async def _exec(args, project_root, cfg):
                    resolved = cmd
                    for k, v in args.items():
                        resolved = resolved.replace(f"{{args.{k}}}", str(v))
                    err = check_shell_cmd(resolved, project_root)
                    if err:
                        return {"ok": False, "error": err}
                    try:
                        result = subprocess.run(
                            resolved, shell=True, capture_output=True, text=True,
                            timeout=timeout, cwd=project_root,
                        )
                        output = (result.stdout + result.stderr)[:5000]
                        return {"ok": True, "cmd": resolved, "output": output,
                                "exit_code": result.returncode}
                    except subprocess.TimeoutExpired:
                        return {"ok": False, "error": "命令执行超时"}
                return _exec

            tool_def = ToolDef(
                name=t["name"],
                description=t.get("description", ""),
                input_schema={
                    "type": "object",
                    "properties": {
                        "reason": {"type": "string", "description": "执行原因"},
                    },
                    "required": [],
                },
                execute=_make_execute(),
                modes=t.get("modes") or None,
                is_write=t.get("is_write", True),
                require_approval=t.get("require_approval", False),
                timeout=timeout_val,
                priority=20,  # TOML priority > built-in
            )
            self.register(tool_def)

    def load_plugin_tools(self, plugin_dir: str):
        """Auto-discover and import Python tool plugins from a directory."""
        if not os.path.isdir(plugin_dir):
            return
        for filename in sorted(os.listdir(plugin_dir)):
            if filename.startswith("_") or not filename.endswith(".py"):
                continue
            filepath = os.path.join(plugin_dir, filename)
            mod_name = f"_live_edit_plugin_{filename[:-3]}"
            try:
                spec = importlib.util.spec_from_file_location(mod_name, filepath)
                if spec is None or spec.loader is None:
                    continue
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                logger.info("Loaded plugin tools from %s", filepath)
            except Exception as e:
                logger.warning("Failed to load plugin %s: %s", filepath, e)


# ── Global registry singleton (for @tool decorator) ──
_global_registry: DefaultToolRegistry | None = None


def set_global_registry(registry: DefaultToolRegistry):
    global _global_registry
    _global_registry = registry


def tool(name: str, description: str, modes: list[str] | None = None,
         is_write: bool = False, require_approval: bool = False, timeout: int = 30):
    """Decorator to register a Python function as a tool in the global registry.

    Usage:
        @tool(name="my_tool", description="...", modes=["deep"])
        async def my_tool(args, project_root, config):
            return {"ok": True}
    """
    def deco(fn):
        td = ToolDef(
            name=name,
            description=description,
            input_schema={
                "type": "object",
                "properties": {
                    "reason": {"type": "string", "description": "执行原因"},
                },
                "required": [],
            },
            execute=fn,
            modes=modes,
            is_write=is_write,
            require_approval=require_approval,
            timeout=timeout,
            priority=30,  # plugin priority > TOML > built-in
        )
        if _global_registry is not None:
            _global_registry.register(td)
        return fn
    return deco

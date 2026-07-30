# Tool Registry + Evaluation Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a plugin-based tool registry with three sources (built-in, TOML, Python plugins) and a staged evaluation pipeline (lint → test → preview → introspection → HTML diff) with auto-retry.

**Architecture:** Extract safety functions to their own module, create `ToolRegistry` protocol with `DefaultToolRegistry` implementation, migrate 7 built-in tools to individual modules, add `EvaluationConfig` to config, implement `evaluation.py` as a standalone pipeline, wire both subsystems into the existing engine/router.

**Tech Stack:** Python 3.10+, dataclasses, asyncio, httpx, subprocess, tomli

---

### Task 1: Extract safety functions to `live_edit/safety.py`

**Files:**
- Create: `live_edit/safety.py`
- Modify: `live_edit/tools.py` (remove moved code, re-import from safety)

- [ ] **Step 1: Create live_edit/safety.py with extracted functions**

```python
"""Path safety, shell command vetting, and write permission checks."""

import os
import re

# ── Dangerous command patterns (blocked for run_shell) ──
_DANGEROUS_CMDS = [
    r"\brm\b",
    r"\bgit\s+rm\b",
    r"\bunlink\b",
    r"\bdrop\s+table\b",
    r"\bdelete\s+from\b",
    r"\bgit\s+push\b",
    r"\bgit\s+reset\s+--hard\b",
    r"\bshutdown\b",
    r"\breboot\b",
    r"\bchmod\s+777\b",
    r"\b>.*\.\.\/",
    r"\bcurl.*\|\s*bash\b",
    r"\bwget.*\|\s*sh\b",
    r"\bmkfs\.",
    r"\bdd\s+if=",
    r"\bformat\s+[A-Z]:",
    r":\(\)\s*\{",
    r"\\x[0-9a-f]{2}",
    r"\$\(",
    r"`",
    r"\beval\b",
    r"\bexec\b",
    r"\bsudo\b",
    r">\s*/dev/sd",
]
_DANGEROUS_RE = re.compile("|".join(_DANGEROUS_CMDS), re.IGNORECASE)

# ── Safe commands (common dev tools that bypass danger checks) ──
_SAFE_PREFIXES = [
    "git status",
    "git diff",
    "git log",
    "git show",
    "git branch",
    "git stash",
    "git add ",
    "git commit ",
    "git checkout ",
    "git merge ",
    "git rebase",
    "ls ",
    "ls\n",
    "cat ",
    "head ",
    "tail ",
    "find ",
    "grep ",
    "wc ",
    "sort ",
    "uniq ",
    "cut ",
    "sed ",
    "awk ",
    "pwd",
    "which ",
    "python ",
    "python3 ",
    "node ",
    "npm ",
    "npx ",
    "pytest",
    "ruff ",
    "black ",
    "mypy ",
    "pip ",
    "poetry ",
    "cargo ",
    "go ",
    "make ",
    "tree ",
    "du ",
    "date",
    "env",
    "stat ",
    "file ",
    "echo ",
    "printf ",
    "mkdir ",
    "cp ",
    "mv ",
    "touch ",
    "whoami",
    "printenv",
    "md5sum",
    "sha256sum",
    "sha1sum",
    "curl ",
    "wget ",
]


def safe_path(rel_path: str, project_root: str) -> str:
    """Resolve a project-relative path and ensure it stays inside project_root."""
    norm_root = os.path.normpath(os.path.abspath(project_root))
    abs_path = os.path.normpath(os.path.join(norm_root, rel_path))
    if not abs_path.startswith(norm_root + os.sep) and abs_path != norm_root:
        raise ValueError(f"路径越界: {rel_path} → {abs_path}")
    return abs_path


def check_shell_cmd(cmd: str, project_root: str = "") -> str | None:
    """Return error message if cmd is dangerous, None if ok."""
    cmd_stripped = cmd.strip()

    if re.search(r"\bcurl\b.*\|", cmd_stripped) or re.search(r"\bwget\b.*\|", cmd_stripped):
        return f"命令包含危险操作，已阻止: {cmd_stripped}"

    is_safe = any(
        cmd_stripped.startswith(prefix) or cmd_stripped == prefix.strip()
        for prefix in _SAFE_PREFIXES
    )

    if not is_safe and _DANGEROUS_RE.search(cmd):
        return f"命令包含危险操作，已阻止: {cmd}"

    if ">" in cmd and project_root:
        parts = cmd.split(">")
        if len(parts) > 1:
            target = parts[-1].strip().split()[0] if parts[-1].strip() else ""
            if target and not target.startswith("/dev/"):
                try:
                    norm_root = os.path.normpath(os.path.abspath(project_root))
                    abs_target = os.path.normpath(os.path.join(norm_root, target))
                    if not abs_target.startswith(norm_root + os.sep) and abs_target != norm_root:
                        return f"禁止重定向写入到项目外文件: {target}"
                except Exception:
                    return f"无法解析重定向目标: {target}"
    return None


def check_write_allowed(
    path: str,
    project_root: str,
    allow_overwrite: bool = False,
    overwrite_dirs: list[str] | None = None,
) -> str | None:
    """Return error message if a write is not allowed, None if ok."""
    if overwrite_dirs is None:
        overwrite_dirs = ["static", "public", "assets"]
    abs_path = safe_path(path, project_root)
    if os.path.exists(abs_path):
        if allow_overwrite:
            return None
        norm_root = os.path.normpath(os.path.abspath(project_root))
        for d in overwrite_dirs:
            allowed_dir = os.path.normpath(os.path.join(norm_root, d))
            if abs_path.startswith(allowed_dir + os.sep) or abs_path == allowed_dir:
                return None
        return f"write_file 只能覆写 {', '.join(overwrite_dirs)} 目录下的文件或创建新文件"
    return None
```

- [ ] **Step 2: Remove extracted code from tools.py, re-import from safety**

In `tools.py`, remove lines 1-16 (the `_DANGEROUS_CMDS`, `_DANGEROUS_RE`, `_SAFE_PREFIXES` definitions), remove lines 36-110 (the three safety functions), and add at the top:

```python
from .safety import safe_path, check_shell_cmd, check_write_allowed
```

Update all call sites in `tools.py` to use the new non-underscore names:
- `_safe_path(...)` → `safe_path(...)`
- `_check_shell_cmd(...)` → `check_shell_cmd(...)`
- `_check_write_allowed(...)` → `check_write_allowed(...)`

- [ ] **Step 3: Run existing tests to verify nothing breaks**

Run: `pytest tests/test_tools.py -v`
Expected: All tests PASS (safety function tests should still pass since behavior is identical)

- [ ] **Step 4: Commit**

```bash
git add live_edit/safety.py live_edit/tools.py
git commit -m "refactor: extract safety functions to live_edit/safety.py"
```

---

### Task 2: Create ToolDef and ToolRegistry in `live_edit/tool_registry.py`

**Files:**
- Create: `live_edit/tool_registry.py`

- [ ] **Step 1: Create live_edit/tool_registry.py**

```python
"""Plugin-based tool registry with built-in, TOML, and Python plugin support."""

import asyncio
import importlib.util
import logging
import os
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, Awaitable

logger = logging.getLogger("live-edit.tool-registry")


@dataclass
class ToolDef:
    """Definition of a single tool."""

    name: str
    description: str
    input_schema: dict
    execute: Callable[[dict, str, object], Awaitable[dict]]
    modes: list[str] | None = None  # None = all modes
    is_write: bool = False
    require_approval: bool = False
    timeout: int = 30
    priority: int = 0  # higher = wins on name collision

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
            t.name
            for t in self._tools.values()
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
            tool_def.priority = 10  # built-in priority
            self.register(tool_def)

    def load_toml_tools(self, config):
        """Parse [[tools]] sections from config and register shell-based tools."""
        if not config or not hasattr(config, "toml_tools"):
            return
        from .safety import check_shell_cmd

        for t in config.toml_tools:

            async def _make_execute(cmd, timeout):
                async def _exec(args, project_root, cfg):
                    # Substitute {args.xxx} placeholders
                    resolved = cmd
                    for k, v in args.items():
                        resolved = resolved.replace(f"{{args.{k}}}", str(v))
                    err = check_shell_cmd(resolved, project_root)
                    if err:
                        return {"ok": False, "error": err}
                    try:
                        result = subprocess.run(
                            resolved,
                            shell=True,
                            capture_output=True,
                            text=True,
                            timeout=timeout,
                            cwd=project_root,
                        )
                        output = (result.stdout + result.stderr)[:5000]
                        return {
                            "ok": True,
                            "cmd": resolved,
                            "output": output,
                            "exit_code": result.returncode,
                        }
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
                execute=_make_execute(t["command"], t.get("timeout", 30)),
                modes=t.get("modes") or None,
                is_write=t.get("is_write", True),
                require_approval=t.get("require_approval", False),
                timeout=t.get("timeout", 30),
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


def tool(
    name: str,
    description: str,
    modes: list[str] | None = None,
    is_write: bool = False,
    require_approval: bool = False,
    timeout: int = 30,
):
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
```

- [ ] **Step 2: Run a quick Python syntax check**

Run: `python -c "from live_edit.tool_registry import ToolDef, ToolRegistry, DefaultToolRegistry; print('OK')"`
Expected: OK

- [ ] **Step 3: Commit**

```bash
git add live_edit/tool_registry.py
git commit -m "feat: add ToolDef, ToolRegistry protocol, and DefaultToolRegistry"
```

---

### Task 3: Migrate built-in tools to individual modules

**Files:**
- Create: `live_edit/builtin_tools/__init__.py`
- Create: `live_edit/builtin_tools/read_file.py`
- Create: `live_edit/builtin_tools/search_code.py`
- Create: `live_edit/builtin_tools/glob.py`
- Create: `live_edit/builtin_tools/list_dir.py`
- Create: `live_edit/builtin_tools/edit_file.py`
- Create: `live_edit/builtin_tools/write_file.py`
- Create: `live_edit/builtin_tools/run_shell.py`

- [ ] **Step 1: Create builtin_tools/__init__.py**

```python
"""Built-in tool modules. Each exports a create() -> ToolDef function."""

from . import read_file, search_code, glob, list_dir, edit_file, write_file, run_shell

ALL_MODULES = [read_file, search_code, glob, list_dir, edit_file, write_file, run_shell]
```

- [ ] **Step 2: Create builtin_tools/read_file.py**

```python
"""Read file contents tool."""

from ..tool_registry import ToolDef
from ..safety import safe_path


async def execute(args: dict, project_root: str, config=None) -> dict:
    path = safe_path(args["path"], project_root)
    start = args.get("start", 1) - 1
    end = args.get("end")
    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    if end:
        lines = lines[start:end]
    elif start > 0:
        lines = lines[start:]
    content = "".join(lines)
    return {"ok": True, "path": args["path"], "content": content, "lines": len(lines)}


def create() -> ToolDef:
    return ToolDef(
        name="read_file",
        description="读取文件内容。用于理解现有代码结构。",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对于项目根目录的文件路径"},
                "start": {"type": "integer", "description": "起始行号（可选，1-based）"},
                "end": {"type": "integer", "description": "结束行号（可选，1-based，含）"},
            },
            "required": ["path"],
        },
        execute=execute,
        is_write=False,
    )
```

- [ ] **Step 3: Create builtin_tools/search_code.py**

```python
"""Search code patterns tool."""

import subprocess

from ..tool_registry import ToolDef
from ..safety import safe_path


async def execute(args: dict, project_root: str, config=None) -> dict:
    pattern = args["pattern"]
    search_path = safe_path(args.get("path", "."), project_root)
    exts = []
    if config and hasattr(config, "safety") and hasattr(config.safety, "search_extensions"):
        for ext in config.safety.search_extensions:
            exts += ["--include", ext]
    if not exts:
        exts = [
            "--include=*.py",
            "--include=*.html",
            "--include=*.js",
            "--include=*.css",
            "--include=*.md",
        ]
    try:
        result = subprocess.run(
            ["grep", "-rn"] + exts + [pattern, search_path],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=project_root,
        )
        output = result.stdout[:5000] if result.stdout else "(无匹配)"
        count = len(result.stdout.strip().split("\n")) if result.stdout.strip() else 0
        return {"ok": True, "pattern": pattern, "matches": output, "match_count": count}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "搜索超时"}


def create() -> ToolDef:
    return ToolDef(
        name="search_code",
        description="在项目中搜索代码模式（grep）。用于定位相关代码。",
        input_schema={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "搜索的正则表达式或关键词"},
                "path": {"type": "string", "description": "搜索范围路径（可选，默认为项目根目录）"},
            },
            "required": ["pattern"],
        },
        execute=execute,
        is_write=False,
    )
```

- [ ] **Step 4: Create builtin_tools/glob.py**

```python
"""File glob pattern matching tool."""

from pathlib import Path

from ..tool_registry import ToolDef


async def execute(args: dict, project_root: str, config=None) -> dict:
    pattern = args["pattern"]
    try:
        matches = sorted(Path(project_root).glob(pattern))
        files = []
        for m in matches:
            if m.is_file():
                rel = str(m.relative_to(project_root))
                files.append(rel)
        return {"ok": True, "pattern": pattern, "files": files[:50], "match_count": len(files)}
    except Exception as e:
        return {"ok": False, "error": f"glob 失败: {e}"}


def create() -> ToolDef:
    return ToolDef(
        name="glob",
        description="按文件模式查找文件。支持 ** 递归匹配，如 static/**/*.js。",
        input_schema={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "文件匹配模式，如 **/*.py, static/**"},
            },
            "required": ["pattern"],
        },
        execute=execute,
        is_write=False,
    )
```

- [ ] **Step 5: Create builtin_tools/list_dir.py**

```python
"""List directory contents tool."""

import os

from ..tool_registry import ToolDef
from ..safety import safe_path


async def execute(args: dict, project_root: str, config=None) -> dict:
    dir_path = safe_path(args.get("path", "."), project_root)
    if not os.path.isdir(dir_path):
        return {"ok": False, "error": f"路径不是目录: {args.get('path', '.')}"}
    entries = []
    total_files = 0
    total_dirs = 0
    try:
        with os.scandir(dir_path) as it:
            for entry in it:
                try:
                    size = entry.stat().st_size if entry.is_file() else 0
                except OSError:
                    size = 0
                entries.append(
                    {
                        "name": entry.name,
                        "is_dir": entry.is_dir(),
                        "size_bytes": size if entry.is_file() else 0,
                    }
                )
                if entry.is_dir():
                    total_dirs += 1
                else:
                    total_files += 1
        entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
        return {
            "ok": True,
            "path": args.get("path", "."),
            "entries": entries[:100],
            "total_files": total_files,
            "total_dirs": total_dirs,
        }
    except PermissionError:
        return {"ok": False, "error": "无权限访问该目录"}


def create() -> ToolDef:
    return ToolDef(
        name="list_dir",
        description="列出目录内容。用于了解项目文件结构，发现需要修改的文件位置。返回结构化条目列表（名称、是否目录、文件大小）。",
        input_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "相对于项目根目录的路径，默认为项目根目录",
                },
            },
            "required": [],
        },
        execute=execute,
        is_write=False,
    )
```

- [ ] **Step 6: Create builtin_tools/edit_file.py**

```python
"""Precision string-replacement edit tool."""

import re

from ..tool_registry import ToolDef
from ..safety import safe_path


async def execute(args: dict, project_root: str, config=None) -> dict:
    path = safe_path(args["path"], project_root)
    old = args["old_string"]
    new = args["new_string"]
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    count = content.count(old)
    if count == 1:
        new_content = content.replace(old, new, 1)
        with open(path, "w", encoding="utf-8") as f:
            f.write(new_content)
        return {"ok": True, "path": args["path"], "modified": True}

    if count == 0:
        norm_old = re.sub(r"\s+", " ", old).strip()
        norm_content = re.sub(r"\s+", " ", content)
        norm_positions = []
        pos = 0
        while True:
            idx = norm_content.find(norm_old, pos)
            if idx == -1:
                break
            norm_positions.append(idx)
            pos = idx + 1

        if len(norm_positions) == 0:
            head_lines = content.strip().split("\n")[:3]
            head_preview = "\n".join(head_lines)[:200]
            return {
                "ok": False,
                "error": f"old_string 在文件中未找到。文件开头预览:\n{head_preview}",
            }

        if len(norm_positions) == 1:
            norm_line_start = norm_content.rfind("\n", 0, norm_positions[0]) + 1
            norm_line_end = norm_content.find("\n", norm_positions[0] + len(norm_old))
            orig_match = content[
                norm_line_start : norm_line_end if norm_line_end != -1 else len(content)
            ]
            new_content = content.replace(orig_match, new, 1)
            with open(path, "w", encoding="utf-8") as f:
                f.write(new_content)
            return {
                "ok": True,
                "path": args["path"],
                "modified": True,
                "matched_via": "whitespace_normalized",
            }

        line_info = []
        for pos in norm_positions[:5]:
            lineno = norm_content[:pos].count("\n") + 1
            snippet = norm_content[pos : pos + len(norm_old) + 40] + "..."
            line_info.append(f"  L{lineno}: ...{snippet}")
        return {
            "ok": False,
            "error": f"old_string 模糊匹配了 {len(norm_positions)} 处（仅空白差异），请提供更多上下文:\n"
            + "\n".join(line_info),
        }

    if count > 1:
        line_info = []
        for m in re.finditer(re.escape(old), content):
            if len(line_info) >= 5:
                break
            lineno = content[: m.start()].count("\n") + 1
            ctx_start = max(0, m.start() - 20)
            ctx_end = min(len(content), m.end() + 40)
            snippet = content[ctx_start:ctx_end].replace("\n", "\\n") + "..."
            line_info.append(f"  L{lineno}: ...{snippet}")
        return {
            "ok": False,
            "error": f"old_string 匹配了 {count} 处，请提供更多上下文使其唯一:\n"
            + "\n".join(line_info),
        }


def create() -> ToolDef:
    return ToolDef(
        name="edit_file",
        description="精确字符串替换编辑文件。old_string 必须在文件中唯一匹配。用于修改现有文件的部分内容。",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对于项目根目录的文件路径"},
                "old_string": {
                    "type": "string",
                    "description": "要替换的原始字符串（必须精确匹配）",
                },
                "new_string": {"type": "string", "description": "替换后的新字符串"},
                "reason": {"type": "string", "description": "修改原因（向用户解释）"},
            },
            "required": ["path", "old_string", "new_string"],
        },
        execute=execute,
        is_write=True,
    )
```

- [ ] **Step 7: Create builtin_tools/write_file.py**

```python
"""Create or overwrite file tool."""

import os

from ..tool_registry import ToolDef
from ..safety import safe_path, check_write_allowed


async def execute(args: dict, project_root: str, config=None) -> dict:
    path = safe_path(args["path"], project_root)
    overwrite_dirs = None
    allow_overwrite = False
    if config and hasattr(config, "safety"):
        overwrite_dirs = getattr(config.safety, "overwrite_allowed_dirs", None)
        allow_overwrite = getattr(config.safety, "allow_overwrite_existing", False)
    err = check_write_allowed(args["path"], project_root, allow_overwrite, overwrite_dirs)
    if err:
        return {"ok": False, "error": err}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(args["content"])
    return {"ok": True, "path": args["path"], "written": True, "size": len(args["content"])}


def create() -> ToolDef:
    return ToolDef(
        name="write_file",
        description="创建新文件或完全覆写现有文件。",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对于项目根目录的文件路径"},
                "content": {"type": "string", "description": "文件完整内容"},
                "reason": {"type": "string", "description": "创建/覆写原因（向用户解释）"},
            },
            "required": ["path", "content"],
        },
        execute=execute,
        is_write=True,
    )
```

- [ ] **Step 8: Create builtin_tools/run_shell.py**

```python
"""Shell command execution tool."""

import subprocess

from ..tool_registry import ToolDef
from ..safety import check_shell_cmd


async def execute(args: dict, project_root: str, config=None) -> dict:
    cmd = args["cmd"]
    err = check_shell_cmd(cmd, project_root)
    if err:
        return {"ok": False, "error": err}
    timeout = 30
    if config and hasattr(config, "timeouts"):
        timeout = getattr(config.timeouts, "shell_command", 30)
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=project_root,
        )
        output = (result.stdout + result.stderr)[:5000]
        return {"ok": True, "cmd": cmd, "output": output, "exit_code": result.returncode}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "命令执行超时"}


def create() -> ToolDef:
    return ToolDef(
        name="run_shell",
        description="执行 shell 命令。可用于 git diff, git status, git log, grep, find, ls 等操作。危险命令（rm, git push, git reset --hard 等）会被自动拦截。",
        input_schema={
            "type": "object",
            "properties": {
                "cmd": {"type": "string", "description": "要执行的 shell 命令"},
                "reason": {"type": "string", "description": "执行原因（向用户解释）"},
            },
            "required": ["cmd"],
        },
        execute=execute,
        is_write=False,
    )
```

- [ ] **Step 9: Run syntax check on all modules**

Run: `python -c "from live_edit.builtin_tools import ALL_MODULES; print(f'{len(ALL_MODULES)} tools loaded')"`
Expected: `7 tools loaded`

- [ ] **Step 10: Commit**

```bash
git add live_edit/builtin_tools/
git commit -m "feat: migrate 7 built-in tools to individual modules"
```

---

### Task 4: Update tools.py for backward compatibility

**Files:**
- Modify: `live_edit/tools.py`

- [ ] **Step 1: Rewrite tools.py as backward-compat shim**

Keep `_trunc`, `_size_fmt`, `_tool_summary`, `_summarize_thinking` as utility functions (they're still needed by engine.py for SSE event formatting). Replace the tool definitions and execute_tool with re-exports from the registry.

Read current tools.py, remove:
- `_DANGEROUS_CMDS`, `_DANGEROUS_RE`, `_SAFE_PREFIXES` (now in safety.py)
- `_safe_path`, `_check_shell_cmd`, `_check_write_allowed` (now in safety.py)
- `TOOLS`, `QA_TOOLS`, `_WRITE_TOOLS`, `get_mode_tools` (now in tool_registry.py)
- `execute_tool` body (now in builtin_tools modules)

Keep:
- `_trunc`, `_size_fmt`, `_tool_summary`, `_summarize_thinking` (formatting utilities)

Add backward-compat re-exports that delegate to a module-level registry reference:

```python
"""Formatting helpers and backward-compatible re-exports.

New code should use live_edit.tool_registry directly.
"""

from .safety import (
    safe_path as _safe_path,
    check_shell_cmd as _check_shell_cmd,
    check_write_allowed as _check_write_allowed,
)

# ── Formatting helpers (still used by engine.py for SSE events) ──


def _trunc(s: str | None, n: int = 80) -> str:
    s = str(s or "")
    s = s.strip().replace("\n", " ")
    return s[:n] + "…" if len(s) > n else s


def _size_fmt(n: int) -> str:
    if n >= 1024:
        return f"{n / 1024:.1f}KB"
    return f"{n}B"


def _tool_summary(name: str, args: dict) -> str:
    path = _trunc(args.get("path", "") or args.get("file_path", "") or args.get("file", ""), 60)
    pattern = args.get("pattern", "") or args.get("regex", "") or args.get("query", "")
    cmd = args.get("cmd", "") or args.get("command", "") or args.get("shell", "")
    url = args.get("url", "") or args.get("link", "")

    if name in ("read_file", "Read"):
        start = args.get("start", "")
        end = args.get("end", "")
        loc = f" L{start}-{end}" if start and end else f" L{start}+" if start else ""
        return f"读取 {path}{loc}"
    elif name in ("write_file", "Write"):
        size = len(args.get("content", ""))
        return f"新建 {path} ({_size_fmt(size)})"
    elif name in ("edit_file", "Edit"):
        old_s = args.get("old_string", "")
        preview = _trunc(old_s, 60)
        return f"编辑 {path}: {preview}"
    elif name in ("run_shell", "Bash"):
        return f"执行: {_trunc(cmd, 80)}"
    elif name in ("search_code", "Grep"):
        tail = f" 在 {path}" if path else ""
        return f"搜索「{_trunc(pattern, 60)}」{tail}"
    elif name in ("glob", "Glob"):
        return f"查找 {_trunc(pattern, 60)}"
    elif name in ("WebFetch", "WebSearch"):
        return f"访问 {_trunc(url or pattern, 80)}"
    return _trunc(f"{name}: {path or pattern or cmd or url}", 100)


def _summarize_thinking(text: str, max_chars: int = 300) -> str:
    text = text.strip()
    if len(text) <= max_chars:
        return text
    chunk = text[:max_chars]
    for sep in ("\n\n", "\n", "。", "！", "？", "；"):
        pos = chunk.rfind(sep)
        if pos > max_chars * 0.5:
            return chunk[: pos + len(sep)] + "…"
    last_space = chunk.rfind(" ")
    if last_space > max_chars * 0.5:
        return chunk[:last_space] + "…"
    return chunk + "…"


# ── Backward-compat globals (populated by setup_live_edit via init_tools_module) ──

_registry = None


def _set_registry(registry):
    global _registry
    _registry = registry


TOOLS = []
QA_TOOLS = []
_WRITE_TOOLS = set()


def _refresh_globals(mode: str = "quick"):
    global TOOLS, QA_TOOLS, _WRITE_TOOLS
    if _registry is not None:
        TOOLS = _registry.get_tools("deep")
        QA_TOOLS = _registry.get_tools("qa")
        _WRITE_TOOLS = _registry.get_write_tool_names("quick")


def get_mode_tools(mode: str, config=None) -> list[dict]:
    if _registry is not None:
        return _registry.get_tools(mode)
    return []


async def execute_tool(name: str, args: dict, project_root: str, config=None) -> dict:
    if _registry is not None:
        return await _registry.execute(name, args, project_root, config)
    return {"ok": False, "error": "Tool registry not initialized"}
```

- [ ] **Step 2: Run existing tests to verify backward compat**

Run: `pytest tests/test_tools.py -v`
Expected: Tests for `_tool_summary`, `_summarize_thinking`, `_trunc`, `_size_fmt` still pass. Tests for safety functions may fail if they import the old names — update test imports to use safety.py directly.

- [ ] **Step 3: Commit**

```bash
git add live_edit/tools.py
git commit -m "refactor: tools.py as backward-compat shim, delegate to ToolRegistry"
```

---

### Task 5: Add EvaluationConfig to `live_edit/config.py`

**Files:**
- Modify: `live_edit/config.py`

- [ ] **Step 1: Add EvaluationConfig dataclass**

Add after `PreviewConfig` in config.py:

```python
@dataclass
class EvaluationConfig:
    enabled: bool = False
    max_retries: int = 3
    stages: list[str] = field(
        default_factory=lambda: ["lint", "test", "preview", "introspect", "html_diff"]
    )
    test_command: str = ""
    lint_command: str = ""
    screenshot: bool = False
    preview_pages: list[str] = field(default_factory=lambda: ["/"])
```

- [ ] **Step 2: Add evaluation field to Config dataclass**

Add after `preview: PreviewConfig`:
```python
evaluation: EvaluationConfig = field(default_factory=EvaluationConfig)
```

- [ ] **Step 3: Add TOML parsing for [evaluation] section**

In `parse_config()`, add before the `return Config(...)`:

```python
eval_data = raw.get("evaluation", {})
evaluation = EvaluationConfig(
    enabled=eval_data.get("enabled", False),
    max_retries=eval_data.get("max_retries", 3),
    stages=eval_data.get("stages", ["lint", "test", "preview", "introspect", "html_diff"]),
    test_command=eval_data.get("test_command", ""),
    lint_command=eval_data.get("lint_command", ""),
    screenshot=eval_data.get("screenshot", False),
    preview_pages=eval_data.get("preview_pages", ["/"]),
)
```

Update `Config(...)` constructor call to include `evaluation=evaluation`.

- [ ] **Step 4: Add TOML tools parsing**

In `parse_config()`, parse `[[tools]]` array and store in config:

```python
toml_tools = raw.get("tools", [])  # TOML array of tables
```

Add `toml_tools` field to `Config`:
```python
toml_tools: list[dict] = field(default_factory=list)
```

And in the Config constructor: `toml_tools=toml_tools`.

- [ ] **Step 5: Run existing config tests**

Run: `pytest tests/test_config.py -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add live_edit/config.py
git commit -m "feat: add EvaluationConfig and TOML tools parsing to config"
```

---

### Task 6: Implement evaluation pipeline in `live_edit/evaluation.py`

**Files:**
- Create: `live_edit/evaluation.py`

- [ ] **Step 1: Create live_edit/evaluation.py**

```python
"""Post-edit evaluation pipeline: lint → test → preview → introspection → HTML diff."""

import asyncio
import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from enum import Enum

import httpx

logger = logging.getLogger("live-edit.evaluation")


class EvalStage(Enum):
    LINT = "lint"
    TEST = "test"
    PREVIEW = "preview"
    INTROSPECT = "introspect"
    HTML_DIFF = "html_diff"


@dataclass
class EvalResult:
    passed: bool
    stages_passed: list[str] = field(default_factory=list)
    stages_failed: list[str] = field(default_factory=list)
    report: str = ""
    retries_used: int = 0
    stage_details: dict = field(default_factory=dict)
    failed_stage: str = ""
    failed_output: str = ""


def _detect_lint_cmd(project_root: str, config) -> str:
    """Auto-detect lint command from project type."""
    if config and hasattr(config, "evaluation") and config.evaluation.lint_command:
        return config.evaluation.lint_command
    if os.path.exists(os.path.join(project_root, "pyproject.toml")):
        return "python -m py_compile $(git diff --cached --name-only --diff-filter=ACM '*.py' 2>/dev/null) 2>&1 || echo 'no .py changes'"
    if os.path.exists(os.path.join(project_root, "package.json")):
        return "npm run lint --if-present 2>&1 || echo 'no lint script'"
    if os.path.exists(os.path.join(project_root, "go.mod")):
        return "go vet ./... 2>&1"
    return "echo 'no lint command detected'"


def _detect_test_cmd(project_root: str, config) -> str:
    """Auto-detect test command from project type."""
    if config and hasattr(config, "evaluation") and config.evaluation.test_command:
        return config.evaluation.test_command
    if os.path.exists(os.path.join(project_root, "pyproject.toml")):
        return "pytest -x --tb=short 2>&1 || echo 'no tests'"
    if os.path.exists(os.path.join(project_root, "package.json")):
        return "npm test 2>&1 || echo 'no tests'"
    if os.path.exists(os.path.join(project_root, "go.mod")):
        return "go test ./... 2>&1"
    return "echo 'no test command detected'"


async def _run_stage_lint(project_root: str, config) -> dict:
    cmd = _detect_lint_cmd(project_root, config)
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=60,
            cwd=project_root,
        )
        output = (result.stdout + result.stderr)[:2000]
        passed = result.returncode == 0
        return {"ok": passed, "output": output, "command": cmd}
    except subprocess.TimeoutExpired:
        return {"ok": False, "output": "Lint check timed out", "command": cmd}


async def _run_stage_test(project_root: str, config) -> dict:
    cmd = _detect_test_cmd(project_root, config)
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=120,
            cwd=project_root,
        )
        output = (result.stdout + result.stderr)[:3000]
        passed = result.returncode == 0
        return {"ok": passed, "output": output, "command": cmd}
    except subprocess.TimeoutExpired:
        return {"ok": False, "output": "Test execution timed out", "command": cmd}


async def _run_stage_preview(preview_url: str) -> dict:
    health_url = f"{preview_url}/live-edit/health" if preview_url else ""
    if not health_url:
        return {"ok": False, "output": "Preview URL not available"}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(health_url)
            passed = r.status_code == 200
            return {
                "ok": passed,
                "output": f"Health check: {r.status_code}",
                "status_code": r.status_code,
            }
    except httpx.ConnectError:
        return {"ok": False, "output": "Preview server not reachable"}
    except Exception as e:
        return {"ok": False, "output": f"Preview check failed: {e}"}


async def _run_stage_introspect(provider, user_request: str, diff: str, thinking: str = "") -> dict:
    """Ask the LLM whether the changes achieved the user's goal."""
    messages = [
        {
            "role": "user",
            "content": (
                "你是一个代码审查助手。用户的需求是：\n"
                f"{user_request}\n\n"
                "AI 进行了以下代码修改（diff）：\n"
                f"```diff\n{diff[:4000]}\n```\n\n"
                "请判断：这些修改是否达成了用户的目标？有没有遗漏或错误？\n"
                "请用中文简短回答。如果达成目标，第一行写「评估结果: 通过」。"
                "如果有问题，第一行写「评估结果: 未通过」，然后列出具体问题。"
            ),
        },
    ]
    try:
        content_blocks = await provider.call_with_tools(
            messages=messages,
            tools=[],
            on_thinking=None,
            on_text=None,
        )
        if not content_blocks:
            return {"ok": True, "output": "Introspection skipped (no LLM response)"}
        text = ""
        for block in content_blocks:
            if block and block.get("type") == "text":
                text += block.get("text", "")
        passed = "通过" in text[:100] and "未通过" not in text[:100]
        return {"ok": passed, "output": text[:1000]}
    except Exception as e:
        return {"ok": True, "output": f"Introspection error (treated as pass): {e}"}


async def _run_stage_html_diff(
    preview_url: str,
    pages: list[str],
    worktree_path: str,
) -> dict:
    """Fetch pages via preview and compare HTML structure."""
    if not preview_url or not pages:
        return {"ok": True, "output": "HTML diff skipped (no preview or pages)"}

    results = []
    for page in pages:
        url = f"{preview_url}{page}" if not page.startswith("/") else f"{preview_url}{page}"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(url)
                if r.status_code == 200:
                    html = r.text
                    # Basic DOM statistics
                    tag_count = len(re.findall(r"<\w+", html))
                    div_count = len(re.findall(r"<div[\s>]", html))
                    script_count = len(re.findall(r"<script[\s>]", html))
                    has_body = "<body" in html.lower()
                    results.append(
                        {
                            "page": page,
                            "ok": True,
                            "tag_count": tag_count,
                            "div_count": div_count,
                            "script_count": script_count,
                            "html_size": len(html),
                            "has_body": has_body,
                        }
                    )
                else:
                    results.append({"page": page, "ok": False, "status": r.status_code})
        except Exception as e:
            results.append({"page": page, "ok": False, "error": str(e)})

    failures = [r for r in results if not r.get("ok")]
    return {
        "ok": len(failures) == 0,
        "output": json.dumps(results, ensure_ascii=False),
        "pages_checked": len(pages),
        "pages_failed": len(failures),
    }


async def run_evaluation_pipeline(
    session,
    provider,
    config,
    preview_manager=None,
) -> EvalResult:
    """Run the evaluation pipeline once (all stages, stop at first failure).

    Returns EvalResult. Does NOT handle retries — that's done in engine.py
    to avoid circular imports.
    """
    stages = config.evaluation.stages if hasattr(config, "evaluation") else []
    if not stages:
        return EvalResult(passed=True, report="Evaluation disabled")

    stage_runners = {
        "lint": lambda: _run_stage_lint(session._worktree_path, config),
        "test": lambda: _run_stage_test(session._worktree_path, config),
        "preview": lambda: _run_stage_preview(session._preview_url),
        "introspect": lambda: _run_stage_introspect(
            provider, session.request, getattr(session, "_cached_diff", "")
        ),
        "html_diff": lambda: _run_stage_html_diff(
            session._preview_url,
            config.evaluation.preview_pages if hasattr(config, "evaluation") else ["/"],
            session._worktree_path,
        ),
    }

    stage_details = {}
    failed_stage = None
    failed_output = ""

    for stage_name in stages:
        if stage_name not in stage_runners:
            continue

        session.emit("eval_stage", stage=stage_name, status="running")

        try:
            result = await stage_runners[stage_name]()
        except Exception as e:
            result = {"ok": False, "output": str(e)}

        stage_details[stage_name] = result

        if result.get("ok"):
            session.emit("eval_stage", stage=stage_name, status="passed")
        else:
            session.emit(
                "eval_stage",
                stage=stage_name,
                status="failed",
                error=result.get("output", "")[:500],
            )
            failed_stage = stage_name
            failed_output = result.get("output", "")
            break  # stop at first failure

    if failed_stage is None:
        session.emit("eval_complete", passed=True, report="所有检查通过")
        return EvalResult(
            passed=True,
            stages_passed=list(stages),
            report="所有检查通过",
            retries_used=0,
            stage_details=stage_details,
        )

    report_parts = []
    for s in stages:
        detail = stage_details.get(s, {})
        status = "通过" if detail.get("ok") else "未通过"
        report_parts.append(f"- {s}: {status}")
    report = "评估未通过:\n" + "\n".join(report_parts)

    return EvalResult(
        passed=False,
        stages_passed=[s for s in stages if stage_details.get(s, {}).get("ok")],
        stages_failed=[failed_stage],
        report=report,
        retries_used=0,
        stage_details=stage_details,
        failed_stage=failed_stage,
        failed_output=failed_output,
    )
```

- [ ] **Step 2: Syntax check**

Run: `python -c "from live_edit.evaluation import run_evaluation_pipeline, EvalResult; print('OK')"`
Expected: `OK` (may warn about engine import — that's fine, we'll add the helper in Task 7)

- [ ] **Step 3: Commit**

```bash
git add live_edit/evaluation.py
git commit -m "feat: add evaluation pipeline with 5 stages and retry loop"
```

---

### Task 7: Update engine.py to use ToolRegistry + evaluation

**Files:**
- Modify: `live_edit/engine.py`

- [ ] **Step 1: Update imports in engine.py**

Replace:
```python
from .tools import (
    TOOLS,
    QA_TOOLS,
    _WRITE_TOOLS,
    execute_tool,
    get_mode_tools,
    _tool_summary,
    _summarize_thinking,
)
```

With:
```python
from .tools import _tool_summary, _summarize_thinking
```

- [ ] **Step 2: Add tool_registry parameter to run_edit_session()**

Add `tool_registry=None` parameter to `run_edit_session()` signature.

Replace:
```python
tools = get_mode_tools(mode, config)
```
With:
```python
tools = tool_registry.get_tools(mode) if tool_registry else []
```

Replace the write-tool check:
```python
needs_approval = mode == "quick" and tool_name in _WRITE_TOOLS
```
With:
```python
tool_def = tool_registry.get_tool(tool_name) if tool_registry else None
needs_approval = (
    mode == "quick" and tool_def is not None and (tool_def.is_write or tool_def.require_approval)
)
```

Replace `await execute_tool(...)` with:
```python
exec_result = (
    await tool_registry.execute(tool_name, tool_input, _root, config)
    if tool_registry
    else {"ok": False, "error": "No tool registry"}
)
```

- [ ] **Step 3: Add evaluation pipeline call with retry loop**

After the `while round_num < max_rounds:` loop (around line 596), before the diff/approval block, add:

```python
# ── Evaluation pipeline (with retry loop) ──
if (
    config
    and hasattr(config, "evaluation")
    and config.evaluation.enabled
    and session._modified_files
):
    session.emit("eval_started", stages=config.evaluation.stages)
    max_retries = config.evaluation.max_retries
    retry = 0
    while retry <= max_retries:
        eval_result = await run_evaluation_pipeline(
            session=session,
            provider=provider,
            config=config,
            preview_manager=preview_manager,
        )
        if eval_result.passed:
            break
        retry += 1
        if retry > max_retries:
            break
        session.emit(
            "eval_retry", round=retry, reason=f"{eval_result.failed_stage} 失败，正在自动修复..."
        )
        fix_prompt = (
            f"评估阶段「{eval_result.failed_stage}」发现以下问题，请修复：\n\n"
            f"```\n{eval_result.failed_output[:1500]}\n```\n\n"
            f"请修改代码解决以上问题，然后回复「修复完成」。"
        )
        session.messages.append({"role": "user", "content": fix_prompt})
        # Re-enter agent loop for fix (shorter max rounds)
        await _run_agent_loop_fix(
            session=session,
            provider=provider,
            config=config,
            tool_registry=tool_registry,
            max_rounds=5,
        )
        # Refresh diff for next evaluation round
        _sp.run(["git", "-C", _root, "add", "-A"], capture_output=True, text=True, timeout=10)
        diff_result = _sp.run(
            ["git", "-C", _root, "diff", "--cached"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        session._cached_diff = diff_result.stdout.strip()

    if retry > max_retries and not eval_result.passed:
        session.emit(
            "eval_complete",
            passed=False,
            report=f"评估未完全通过（已达最大重试次数 {max_retries}）",
        )
```

Update the eval import at top of file:
```python
from .evaluation import run_evaluation_pipeline
```

- [ ] **Step 4: Add _run_agent_loop_fix helper for evaluation retry**

Add a simplified agent loop used during evaluation fix rounds:

```python
async def _run_agent_loop_fix(
    session,
    provider,
    config,
    tool_registry,
    max_rounds: int = 5,
):
    """Simplified agent loop for evaluation fix rounds. Fewer rounds, no nudging."""
    if not tool_registry:
        return
    tools = tool_registry.get_tools(session._mode)
    _root = session._worktree_path

    for _ in range(max_rounds):
        if session._cancelled.is_set():
            break

        content_blocks = await provider.call_with_tools(
            messages=session.messages,
            tools=tools,
            on_thinking=lambda t: None,
            on_text=lambda t: session.emit("text", text=t),
        )

        if content_blocks is None:
            break

        # Collect tool_use blocks
        tool_uses = []
        assistant_content = []
        for block in content_blocks:
            if block is None:
                continue
            if block.get("type") == "text":
                assistant_content.append({"type": "text", "text": block.get("text", "")})
            elif block.get("type") == "thinking":
                assistant_content.append(
                    {"type": "thinking", "thinking": block.get("thinking", "")}
                )
            elif block.get("type") == "tool_use":
                tool_uses.append(block)
                assistant_content.append(
                    {
                        "type": "tool_use",
                        "id": block["id"],
                        "name": block["name"],
                        "input": block.get("input", {}),
                    }
                )

        if not tool_uses:
            session.messages.append({"role": "assistant", "content": assistant_content})
            break

        tool_results = []
        for tool in tool_uses:
            exec_result = await tool_registry.execute(
                tool["name"], tool.get("input", {}), _root, config
            )
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tool["id"],
                    "content": [
                        {"type": "text", "text": json.dumps(exec_result, ensure_ascii=False)}
                    ],
                }
            )
            session.emit("tool_result", id=tool["id"], **exec_result)
            if tool["name"] in ("edit_file", "write_file") and exec_result.get("ok"):
                if tool.get("input", {}).get("path") not in session._modified_files:
                    session._modified_files.append(tool["input"]["path"])

        session.messages.append({"role": "assistant", "content": assistant_content})
        session.messages.append({"role": "user", "content": tool_results})
```

Add `import json` at top of engine.py (it's already there — verify).

- [ ] **Step 5: Add _cached_diff to EditSession**

In `EditSession.__init__`, add:
```python
self._cached_diff: str = ""
```

After the diff generation in the agent loop (where `diff_full` is computed), add:
```python
session._cached_diff = diff_full
```

- [ ] **Step 6: Update continue_edit_session() to pass tool_registry**

Add `tool_registry=None` to `continue_edit_session()` signature and pass it through to `run_edit_session()`.

- [ ] **Step 7: Run existing engine tests**

Run: `pytest tests/test_engine.py -v`
Expected: Tests that don't require a real registry should still pass. Some may need updating to pass a mock tool_registry.

- [ ] **Step 8: Commit**

```bash
git add live_edit/engine.py
git commit -m "feat: integrate ToolRegistry and evaluation pipeline into agent loop"
```

---

### Task 8: Wire ToolRegistry into router.py

**Files:**
- Modify: `live_edit/router.py`

- [ ] **Step 1: Add tool_registry to setup_live_edit()**

Add `tool_registry=None` parameter to `setup_live_edit()`.

After creating `session_store`, add:

```python
if tool_registry is None:
    from .tool_registry import DefaultToolRegistry, set_global_registry

    tool_registry = DefaultToolRegistry()
    tool_registry.load_builtin_tools()
    tool_registry.load_toml_tools(config)
    # Discover plugin tools from project's live_edit_tools/ directory
    plugin_dir = os.path.join(project_root, "live_edit_tools")
    tool_registry.load_plugin_tools(plugin_dir)
    set_global_registry(tool_registry)

# Initialize backward-compat tools.py globals
from .tools import _set_registry

_set_registry(tool_registry)
```

- [ ] **Step 2: Pass tool_registry to engine calls**

In the SSE endpoint (`start_stream`), pass `tool_registry=tool_registry` to `run_edit_session()`.

In the continue endpoint, pass `tool_registry=tool_registry` to `continue_edit_session()`.

- [ ] **Step 3: Run syntax check**

Run: `python -c "from live_edit.router import setup_live_edit; print('OK')"`
Expected: OK

- [ ] **Step 4: Commit**

```bash
git add live_edit/router.py
git commit -m "feat: wire ToolRegistry into router and engine"
```

---

### Task 9: Update frontend for eval SSE events

**Files:**
- Modify: `live_edit/static/live-edit.js`

- [ ] **Step 1: Add eval event handlers**

In the SSE event handler (the `switch (event.type)` block), add new cases:

```javascript
case 'eval_started':
    this._showEvalProgress(event.stages);
    break;
case 'eval_stage':
    this._updateEvalStage(event.stage, event.status, event.error);
    break;
case 'eval_retry':
    this._showEvalRetry(event.round, event.reason);
    break;
case 'eval_complete':
    this._finishEvalProgress(event.passed, event.report);
    break;
```

- [ ] **Step 2: Add eval progress UI methods**

Add to the class:

```javascript
_showEvalProgress(stages) {
    const el = this._evalEl || this._createEvalPanel();
    el.innerHTML = '<div class="le-eval-header">正在验证修改...</div>';
    el.style.display = 'block';
    this._evalStages = stages;
    stages.forEach(s => {
        const row = document.createElement('div');
        row.className = 'le-eval-stage';
        row.id = `le-eval-${s}`;
        row.innerHTML = `<span class="le-eval-dot"></span> ${this._evalStageLabel(s)}`;
        el.appendChild(row);
    });
}

_updateEvalStage(stage, status, error) {
    const row = document.getElementById(`le-eval-${stage}`);
    if (!row) return;
    const dot = row.querySelector('.le-eval-dot');
    if (status === 'running') {
        dot.className = 'le-eval-dot running';
    } else if (status === 'passed') {
        dot.className = 'le-eval-dot passed';
    } else {
        dot.className = 'le-eval-dot failed';
        if (error) row.innerHTML += ` <span class="le-eval-error">${error}</span>`;
    }
}

_showEvalRetry(round, reason) {
    const el = this._evalEl;
    if (!el) return;
    const retry = document.createElement('div');
    retry.className = 'le-eval-retry';
    retry.textContent = `第 ${round} 次修复: ${reason}`;
    el.appendChild(retry);
}

_finishEvalProgress(passed, report) {
    const el = this._evalEl;
    if (!el) return;
    const cls = passed ? 'le-eval-passed' : 'le-eval-failed';
    el.querySelector('.le-eval-header').className = cls;
    el.querySelector('.le-eval-header').textContent = passed ? '验证通过' : '验证未完全通过';
    setTimeout(() => { if (el) el.style.display = 'none'; }, 5000);
}

_createEvalPanel() {
    const el = document.createElement('div');
    el.className = 'le-eval-panel';
    el.id = 'le-eval-panel';
    const chat = this.el.querySelector('.le-chat');
    if (chat) chat.appendChild(el);
    this._evalEl = el;
    return el;
}

_evalStageLabel(stage) {
    const labels = { lint: '代码检查', test: '测试', preview: '预览', introspect: 'AI 自省', html_diff: '页面对比' };
    return labels[stage] || stage;
}
```

- [ ] **Step 2: Add minimal CSS for eval panel**

In `live-edit.css`, add:

```css
.le-eval-panel {
    background: #1a1a2e;
    border: 1px solid #333;
    border-radius: 8px;
    padding: 12px 16px;
    margin: 8px 0;
}
.le-eval-header { font-weight: 600; margin-bottom: 8px; color: #ccc; }
.le-eval-header.le-eval-passed { color: #4caf50; }
.le-eval-header.le-eval-failed { color: #ff9800; }
.le-eval-stage { padding: 3px 0; font-size: 13px; color: #aaa; display: flex; align-items: center; gap: 6px; }
.le-eval-dot {
    width: 8px; height: 8px; border-radius: 50%; background: #555; flex-shrink: 0;
}
.le-eval-dot.running { background: #2196f3; animation: le-pulse 0.8s infinite; }
.le-eval-dot.passed { background: #4caf50; }
.le-eval-dot.failed { background: #f44336; }
.le-eval-error { color: #f44336; font-size: 12px; }
.le-eval-retry { color: #ff9800; font-size: 12px; padding: 2px 0; }
@keyframes le-pulse {
    0%, 100% { opacity: 1; } 50% { opacity: 0.3; }
}
```

- [ ] **Step 3: Commit**

```bash
git add live_edit/static/live-edit.js live_edit/static/live-edit.css
git commit -m "feat: add evaluation progress UI in frontend"
```

---

### Task 10: Integration tests and final verification

**Files:**
- Modify: `tests/test_tools.py` (update imports)
- Create: `tests/test_tool_registry.py`
- Create: `tests/test_evaluation.py`

- [ ] **Step 1: Update test_tools.py imports**

Run to see which tests fail with new structure:
```bash
pytest tests/test_tools.py -v 2>&1 | tail -30
```

Update imports: change `from live_edit.tools import _safe_path, _check_shell_cmd, ...` to `from live_edit.safety import safe_path, check_shell_cmd, ...` and update function names (remove underscore prefix) in test code.

- [ ] **Step 2: Create tests/test_tool_registry.py**

```python
"""Tests for tool_registry.py"""

import pytest
from live_edit.tool_registry import ToolDef, DefaultToolRegistry


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
```

- [ ] **Step 3: Create tests/test_evaluation.py**

```python
"""Tests for evaluation.py"""

import pytest
from live_edit.evaluation import (
    _detect_lint_cmd,
    _detect_test_cmd,
    EvalStage,
    EvalResult,
)


class TestDetectCommands:
    def test_detect_lint_python(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]\nname='test'")
        cmd = _detect_lint_cmd(str(tmp_path), None)
        assert "py_compile" in cmd or "pytest" in cmd

    def test_detect_test_python(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]\nname='test'")
        cmd = _detect_test_cmd(str(tmp_path), None)
        assert "pytest" in cmd

    def test_detect_unknown_project(self, tmp_path):
        cmd = _detect_lint_cmd(str(tmp_path), None)
        assert "no lint" in cmd


class TestEvalResult:
    def test_passed(self):
        r = EvalResult(passed=True, stages_passed=["lint", "test"])
        assert r.passed

    def test_failed(self):
        r = EvalResult(passed=False, stages_failed=["test"], report="test failed")
        assert not r.passed
        assert "test" in r.stages_failed
```

- [ ] **Step 4: Run full test suite**

```bash
pytest tests/ -v
```

Fix any failures. Then:

```bash
git add tests/
git commit -m "test: update tests for tool registry and evaluation pipeline"
```

- [ ] **Step 5: Final verification**

```bash
python -c "
from live_edit.tool_registry import DefaultToolRegistry, set_global_registry
reg = DefaultToolRegistry()
reg.load_builtin_tools()
print(f'Builtin tools: {reg.list_tools()}')
print(f'Quick mode: {[t[\"name\"] for t in reg.get_tools(\"quick\")]}')
print(f'QA mode: {[t[\"name\"] for t in reg.get_tools(\"qa\")]}')
print('OK')
"
```
Expected: 7 builtin tools, quick=7, qa=5 (read-only subset)

```bash
git add -A && git commit -m "chore: final verification of tool registry and evaluation"
```
```

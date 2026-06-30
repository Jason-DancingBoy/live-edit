"""Formatting helpers and backward-compatible re-exports.

New code should use live_edit.tool_registry directly.
"""

from .safety import safe_path as _safe_path, check_shell_cmd as _check_shell_cmd, check_write_allowed as _check_write_allowed


# ── Formatting helpers ──

def _trunc(s: str | None, n: int = 80) -> str:
    """Truncate s to n chars, adding … if cut."""
    s = str(s or "")
    s = s.strip().replace("\n", " ")
    return s[:n] + "…" if len(s) > n else s


def _size_fmt(n: int) -> str:
    """Format byte count as human-readable string with SI suffixes."""
    if n >= 1024:
        return f"{n / 1024:.1f}KB"
    return f"{n}B"


def _tool_summary(name: str, args: dict) -> str:
    """Generate a one-line human-readable summary of a tool call for the UI."""
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
    """Condense verbose thinking into a single digestible chunk.

    Returns the first max_chars characters, truncated at a sentence boundary
    (。！？\n) or word boundary if no sentence break is found.
    """
    text = text.strip()
    if len(text) <= max_chars:
        return text
    chunk = text[:max_chars]
    for sep in ("\n\n", "\n", "。", "！", "？", "；"):
        pos = chunk.rfind(sep)
        if pos > max_chars * 0.5:
            return chunk[:pos + len(sep)] + "…"
    last_space = chunk.rfind(" ")
    if last_space > max_chars * 0.5:
        return chunk[:last_space] + "…"
    return chunk + "…"


# ── Backward-compat globals ──

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

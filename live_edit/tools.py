"""Tool definitions, execution, and safety layer for live-edit."""

import os
import re
import subprocess
from pathlib import Path

# ── Dangerous command patterns (blocked for run_shell) ──
_DANGEROUS_CMDS = [
    r'\brm\b', r'\bgit\s+rm\b', r'\bunlink\b',
    r'\bdrop\s+table\b', r'\bdelete\s+from\b',
    r'\bgit\s+push\b', r'\bgit\s+reset\s+--hard\b', r'\bshutdown\b', r'\breboot\b',
    r'\bchmod\s+777\b', r'\b>.*\.\.\/', r'\bcurl.*\|\s*bash\b', r'\bwget.*\|\s*sh\b',
    r'\bmkfs\.', r'\bdd\s+if=', r'\bformat\s+[A-Z]:', r':\(\)\s*\{', r'\\x[0-9a-f]{2}',
    r'\$\(', r'`', r'\beval\b', r'\bexec\b', r'\bsudo\b', r'>\s*/dev/sd',
]
_DANGEROUS_RE = re.compile('|'.join(_DANGEROUS_CMDS), re.IGNORECASE)

# ── Safe commands (common dev tools that bypass danger checks) ──
_SAFE_PREFIXES = [
    'git status', 'git diff', 'git log', 'git show', 'git branch', 'git stash',
    'git add ', 'git commit ', 'git checkout ', 'git merge ', 'git rebase',
    'ls ', 'ls\n', 'cat ', 'head ', 'tail ', 'find ', 'grep ',
    'wc ', 'sort ', 'uniq ', 'cut ', 'sed ', 'awk ',
    'pwd', 'which ', 'python ', 'python3 ', 'node ', 'npm ', 'npx ',
    'pytest', 'ruff ', 'black ', 'mypy ', 'pip ', 'poetry ', 'cargo ', 'go ',
    'make ', 'tree ', 'du ', 'date', 'env', 'stat ', 'file ', 'echo ', 'printf ',
    'mkdir ', 'cp ', 'mv ', 'touch ',
    'whoami', 'printenv', 'md5sum', 'sha256sum', 'sha1sum',
    'curl ', 'wget ',
]


# ── Safety functions ──

def _safe_path(rel_path: str, project_root: str) -> str:
    """Resolve a project-relative path and ensure it stays inside project_root."""
    # Resolve to absolute first so relative roots like "." work correctly
    norm_root = os.path.normpath(os.path.abspath(project_root))
    abs_path = os.path.normpath(os.path.join(norm_root, rel_path))
    if not abs_path.startswith(norm_root + os.sep) and abs_path != norm_root:
        raise ValueError(f"路径越界: {rel_path} → {abs_path}")
    return abs_path


def _check_shell_cmd(cmd: str, project_root: str = "") -> str | None:
    """Return error message if cmd is dangerous, None if ok.

    Safe commands (common dev tools) bypass the danger regex, except pipe-to-shell.
    Known-dangerous patterns (rm, push, eval, $(), etc.) are blocked.
    Redirect checks always run regardless of safe-list status.
    Everything else is allowed.
    """
    cmd_stripped = cmd.strip()

    # Always check pipe-to-shell regardless of safe list
    if re.search(r'\bcurl\b.*\|', cmd_stripped) or re.search(r'\bwget\b.*\|', cmd_stripped):
        return f"命令包含危险操作，已阻止: {cmd_stripped}"

    # Check if command is in the safe list (bypasses danger regex)
    is_safe = any(
        cmd_stripped.startswith(prefix) or cmd_stripped == prefix.strip()
        for prefix in _SAFE_PREFIXES
    )

    # Non-safe commands get danger-pattern checked
    if not is_safe and _DANGEROUS_RE.search(cmd):
        return f"命令包含危险操作，已阻止: {cmd}"

    # Redirect check always runs (even for safe commands)
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


def _check_write_allowed(
    path: str,
    project_root: str,
    allow_overwrite: bool = False,
    overwrite_dirs: list[str] | None = None,
) -> str | None:
    """Return error message if a write is not allowed, None if ok.

    For new files: always allowed (inside project root, enforced by _safe_path).
    For existing files: only allowed if allow_overwrite is True, or the file
    resides in one of the overwrite_dirs.
    """
    if overwrite_dirs is None:
        overwrite_dirs = ["static", "public", "assets"]
    abs_path = _safe_path(path, project_root)
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


# ── Tool definitions (Anthropic-compatible schemas) ──

TOOLS = [
    {
        "name": "read_file",
        "description": "读取文件内容。用于理解现有代码结构。",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对于项目根目录的文件路径"},
                "start": {"type": "integer", "description": "起始行号（可选，1-based）"},
                "end": {"type": "integer", "description": "结束行号（可选，1-based，含）"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "search_code",
        "description": "在项目中搜索代码模式（grep）。用于定位相关代码。",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "搜索的正则表达式或关键词"},
                "path": {"type": "string", "description": "搜索范围路径（可选，默认为项目根目录）"},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "glob",
        "description": "按文件模式查找文件。支持 ** 递归匹配，如 static/**/*.js。",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "文件匹配模式，如 **/*.py, static/**"},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "list_dir",
        "description": "列出目录内容。用于了解项目文件结构，发现需要修改的文件位置。返回结构化条目列表（名称、是否目录、文件大小）。",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对于项目根目录的路径，默认为项目根目录"},
            },
            "required": [],
        },
    },
    {
        "name": "edit_file",
        "description": "精确字符串替换编辑文件。old_string 必须在文件中唯一匹配。用于修改现有文件的部分内容。",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对于项目根目录的文件路径"},
                "old_string": {"type": "string", "description": "要替换的原始字符串（必须精确匹配）"},
                "new_string": {"type": "string", "description": "替换后的新字符串"},
                "reason": {"type": "string", "description": "修改原因（向用户解释）"},
            },
            "required": ["path", "old_string", "new_string"],
        },
    },
    {
        "name": "write_file",
        "description": "创建新文件或完全覆写现有文件。",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对于项目根目录的文件路径"},
                "content": {"type": "string", "description": "文件完整内容"},
                "reason": {"type": "string", "description": "创建/覆写原因（向用户解释）"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "run_shell",
        "description": "执行 shell 命令。可用于 git diff, git status, git log, grep, find, ls 等操作。危险命令（rm, git push, git reset --hard 等）会被自动拦截。",
        "input_schema": {
            "type": "object",
            "properties": {
                "cmd": {"type": "string", "description": "要执行的 shell 命令"},
                "reason": {"type": "string", "description": "执行原因（向用户解释）"},
            },
            "required": ["cmd"],
        },
    },
]

QA_TOOLS = [t for t in TOOLS if t["name"] in ("read_file", "search_code", "glob", "list_dir", "run_shell")]
_WRITE_TOOLS = {"edit_file", "write_file"}


def get_mode_tools(mode: str, config=None) -> list[dict]:
    """Return the tools list for a given mode."""
    if mode == "qa":
        return QA_TOOLS
    return TOOLS


async def execute_tool(name: str, args: dict, project_root: str, config=None) -> dict:
    """Execute a tool call. Returns result dict with {ok, ...}."""
    try:
        if name == "read_file":
            path = _safe_path(args["path"], project_root)
            start = args.get("start", 1) - 1
            end = args.get("end")
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            if end:
                lines = lines[start:end]
            elif start > 0:
                lines = lines[start:]
            content = "".join(lines)
            return {"ok": True, "path": args["path"], "content": content,
                    "lines": len(lines)}

        elif name == "search_code":
            pattern = args["pattern"]
            search_path = _safe_path(args.get("path", "."), project_root)
            exts = []
            if config and hasattr(config, 'safety') and hasattr(config.safety, 'search_extensions'):
                for ext in config.safety.search_extensions:
                    exts += ["--include", ext]
            if not exts:
                exts = ["--include=*.py", "--include=*.html", "--include=*.js",
                        "--include=*.css", "--include=*.md"]
            try:
                result = subprocess.run(
                    ["grep", "-rn"] + exts + [pattern, search_path],
                    capture_output=True, text=True, timeout=10, cwd=project_root,
                )
                output = result.stdout[:5000] if result.stdout else "(无匹配)"
                count = len(result.stdout.strip().split("\n")) if result.stdout.strip() else 0
                return {"ok": True, "pattern": pattern, "matches": output,
                        "match_count": count}
            except subprocess.TimeoutExpired:
                return {"ok": False, "error": "搜索超时"}

        elif name == "glob":
            pattern = args["pattern"]
            try:
                from pathlib import Path as _Path
                matches = sorted(_Path(project_root).glob(pattern))
                files = []
                for m in matches:
                    if m.is_file():
                        rel = str(m.relative_to(project_root))
                        files.append(rel)
                return {"ok": True, "pattern": pattern, "files": files[:50],
                        "match_count": len(files)}
            except Exception as e:
                return {"ok": False, "error": f"glob 失败: {e}"}

        elif name == "list_dir":
            dir_path = _safe_path(args.get("path", "."), project_root)
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
                        entries.append({
                            "name": entry.name,
                            "is_dir": entry.is_dir(),
                            "size_bytes": size if entry.is_file() else 0,
                        })
                        if entry.is_dir():
                            total_dirs += 1
                        else:
                            total_files += 1
                entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
                return {"ok": True, "path": args.get("path", "."),
                        "entries": entries[:100], "total_files": total_files,
                        "total_dirs": total_dirs}
            except PermissionError:
                return {"ok": False, "error": "无权限访问该目录"}

        elif name == "edit_file":
            path = _safe_path(args["path"], project_root)
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
                # Try whitespace-normalized matching as fallback
                norm_old = re.sub(r'\s+', ' ', old).strip()
                norm_content = re.sub(r'\s+', ' ', content)
                # Find all positions where norm_old appears in norm_content
                norm_positions = []
                pos = 0
                while True:
                    idx = norm_content.find(norm_old, pos)
                    if idx == -1:
                        break
                    norm_positions.append(idx)
                    pos = idx + 1

                if len(norm_positions) == 0:
                    # Build helpful error: show first 3 lines of file to help LLM re-orient
                    head_lines = content.strip().split("\n")[:3]
                    head_preview = "\n".join(head_lines)[:200]
                    return {"ok": False, "error":
                        f"old_string 在文件中未找到。文件开头预览:\n{head_preview}"}

                if len(norm_positions) == 1:
                    # Unique fuzzy match — extract the actual text at that position
                    norm_line_start = norm_content.rfind('\n', 0, norm_positions[0]) + 1
                    norm_line_end = norm_content.find('\n', norm_positions[0] + len(norm_old))
                    # Map back to original content: find the same region
                    orig_match = content[norm_line_start:norm_line_end if norm_line_end != -1 else len(content)]
                    new_content = content.replace(orig_match, new, 1)
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(new_content)
                    return {"ok": True, "path": args["path"], "modified": True,
                            "matched_via": "whitespace_normalized"}

                # Multiple fuzzy matches — report with line numbers
                line_info = []
                for pos in norm_positions[:5]:
                    lineno = norm_content[:pos].count('\n') + 1
                    snippet = norm_content[pos:pos + len(norm_old) + 40] + "..."
                    line_info.append(f"  L{lineno}: ...{snippet}")
                return {"ok": False, "error":
                    f"old_string 模糊匹配了 {len(norm_positions)} 处（仅空白差异），请提供更多上下文:\n" +
                    "\n".join(line_info)}

            if count > 1:
                # Multiple exact matches — report with line numbers
                line_info = []
                for m in re.finditer(re.escape(old), content):
                    if len(line_info) >= 5:
                        break
                    lineno = content[:m.start()].count('\n') + 1
                    ctx_start = max(0, m.start() - 20)
                    ctx_end = min(len(content), m.end() + 40)
                    snippet = content[ctx_start:ctx_end].replace('\n', '\\n') + "..."
                    line_info.append(f"  L{lineno}: ...{snippet}")
                return {"ok": False, "error":
                    f"old_string 匹配了 {count} 处，请提供更多上下文使其唯一:\n" +
                    "\n".join(line_info)}

        elif name == "write_file":
            path = _safe_path(args["path"], project_root)
            overwrite_dirs = None
            allow_overwrite = False
            if config and hasattr(config, 'safety'):
                overwrite_dirs = getattr(config.safety, 'overwrite_allowed_dirs', None)
                allow_overwrite = getattr(config.safety, 'allow_overwrite_existing', False)
            err = _check_write_allowed(args["path"], project_root,
                                        allow_overwrite, overwrite_dirs)
            if err:
                return {"ok": False, "error": err}
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(args["content"])
            return {"ok": True, "path": args["path"], "written": True,
                    "size": len(args["content"])}

        elif name == "run_shell":
            cmd = args["cmd"]
            err = _check_shell_cmd(cmd, project_root)
            if err:
                return {"ok": False, "error": err}
            timeout = 30
            if config and hasattr(config, 'timeouts'):
                timeout = getattr(config.timeouts, 'shell_command', 30)
            try:
                result = subprocess.run(
                    cmd, shell=True, capture_output=True, text=True,
                    timeout=timeout, cwd=project_root,
                )
                output = (result.stdout + result.stderr)[:5000]
                return {"ok": True, "cmd": cmd, "output": output,
                        "exit_code": result.returncode}
            except subprocess.TimeoutExpired:
                return {"ok": False, "error": "命令执行超时"}

        else:
            return {"ok": False, "error": f"未知工具: {name}"}

    except ValueError as e:
        return {"ok": False, "error": str(e)}
    except FileNotFoundError:
        return {"ok": False, "error": f"文件不存在: {args.get('path', '?')}"}
    except Exception as e:
        import traceback, logging
        logging.getLogger("live-edit.tools").error(
            "Tool %s error: %s\n%s", name, e, traceback.format_exc())
        return {"ok": False, "error": str(e)}

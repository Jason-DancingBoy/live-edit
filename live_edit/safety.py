"""Path safety, shell command vetting, and write permission checks."""

import os
import re

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

    if re.search(r'\bcurl\b.*\|', cmd_stripped) or re.search(r'\bwget\b.*\|', cmd_stripped):
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

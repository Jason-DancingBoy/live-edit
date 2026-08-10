"""Delete a file tool with a conservative 3-tier write policy."""

import os
import subprocess

from ..safety import check_write_allowed, safe_path
from ..tool_registry import ToolDef


def _exists_in_head(project_root: str, rel_path: str) -> bool:
    """True if rel_path exists in the worktree's HEAD commit tree.

    Files NOT in HEAD were created this session (or are untracked) and are
    deletable; committed files are treated as protected source.
    """
    try:
        result = subprocess.run(
            ["git", "-C", project_root, "ls-tree", "HEAD", "--", rel_path],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return bool(result.stdout.strip())
    except Exception:
        return True  # conservative: on git error, treat as pre-existing


async def execute(args: dict, project_root: str, config=None) -> dict:
    rel_path = args["path"]
    try:
        abs_path = safe_path(rel_path, project_root)  # raises ValueError on escape
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    if not os.path.exists(abs_path):
        return {"ok": False, "error": f"文件不存在: {rel_path}"}
    if os.path.isdir(abs_path):
        return {"ok": False, "error": f"不能删除目录: {rel_path}"}

    # 3-tier policy (spec §2): session-created/untracked → deletable;
    # otherwise mirror write_file's protection.
    if _exists_in_head(project_root, rel_path):
        overwrite_dirs = None
        allow_overwrite = False
        if config and hasattr(config, "safety"):
            overwrite_dirs = getattr(config.safety, "overwrite_allowed_dirs", None)
            allow_overwrite = getattr(config.safety, "allow_overwrite_existing", False)
        err = check_write_allowed(rel_path, project_root, allow_overwrite, overwrite_dirs)
        if err:
            return {"ok": False, "error": f"删除受保护文件被拒绝：{err}"}

    size = os.path.getsize(abs_path)
    os.remove(abs_path)
    return {"ok": True, "path": rel_path, "deleted": True, "size": size}


def create() -> ToolDef:
    return ToolDef(
        name="delete_file",
        description="删除一个文件（不支持目录）。新建/未跟踪文件可删；"
        "受保护的既有文件需配置 allow_overwrite_existing=true 或在 overwrite_allowed_dirs 内。",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对于项目根目录的文件路径"},
                "reason": {"type": "string", "description": "删除原因（向用户解释）"},
            },
            "required": ["path"],
        },
        execute=execute,
        is_write=True,
    )

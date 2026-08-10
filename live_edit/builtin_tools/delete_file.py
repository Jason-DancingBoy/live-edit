"""Delete a file tool with a conservative 3-tier write policy."""

import os
import subprocess

from ..safety import check_write_allowed, safe_path
from ..tool_registry import ToolDef


def _in_main_branch(project_root: str, rel_path: str) -> bool:
    """True if rel_path exists in the main branch (merged into the real codebase).

    A file committed only to the session branch (live-edit/<id>) is NOT in main
    and stays deletable until the session is merged. Files in main are
    pre-existing and protected.

    Conservative protection on git errors: if neither main nor master can be
    resolved (repo with no commits, or a non-git directory), treat the file as
    protected — never collapse to allow-all.
    """
    for branch in ("main", "master"):
        try:
            result = subprocess.run(
                ["git", "-C", project_root, "ls-tree", branch, "--", rel_path],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except Exception:
            continue
        if result.returncode == 0:
            return bool(result.stdout.strip())  # in main → protected
        # branch name doesn't exist here — try the next candidate
    return True  # conservative: main branch undeterminable → treat as protected


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

    # 3-tier policy (spec §2): not in main branch → deletable;
    # otherwise mirror write_file's protection.
    if _in_main_branch(project_root, rel_path):
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
        description=(
            "删除一个文件（不支持目录）。本会话新建且未合入主分支的文件可删；"
            "已在主分支的既有文件受保护，需配置 allow_overwrite_existing=true "
            "或在 overwrite_allowed_dirs 内。"
        ),
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

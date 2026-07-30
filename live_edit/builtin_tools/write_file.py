"""Create or overwrite file tool."""

import os

from ..safety import check_write_allowed, safe_path
from ..tool_registry import ToolDef


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

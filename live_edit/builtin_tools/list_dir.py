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


def create() -> ToolDef:
    return ToolDef(
        name="list_dir",
        description="列出目录内容。用于了解项目文件结构，发现需要修改的文件位置。",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对于项目根目录的路径，默认为项目根目录"},
            },
            "required": [],
        },
        execute=execute,
        is_write=False,
    )

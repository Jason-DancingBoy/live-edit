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

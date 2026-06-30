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

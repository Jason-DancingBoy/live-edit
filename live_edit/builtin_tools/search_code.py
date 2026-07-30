"""Search code patterns tool."""

import subprocess

from ..safety import safe_path
from ..tool_registry import ToolDef


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

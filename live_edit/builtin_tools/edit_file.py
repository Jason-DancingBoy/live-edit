"""Precision string-replacement edit tool."""

import re

from ..tool_registry import ToolDef
from ..safety import safe_path


async def execute(args: dict, project_root: str, config=None) -> dict:
    path = safe_path(args["path"], project_root)
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
        norm_old = re.sub(r'\s+', ' ', old).strip()
        norm_content = re.sub(r'\s+', ' ', content)
        norm_positions = []
        pos = 0
        while True:
            idx = norm_content.find(norm_old, pos)
            if idx == -1:
                break
            norm_positions.append(idx)
            pos = idx + 1

        if len(norm_positions) == 0:
            head_lines = content.strip().split("\n")[:3]
            head_preview = "\n".join(head_lines)[:200]
            return {"ok": False, "error":
                f"old_string 在文件中未找到。文件开头预览:\n{head_preview}"}

        if len(norm_positions) == 1:
            norm_line_start = norm_content.rfind('\n', 0, norm_positions[0]) + 1
            norm_line_end = norm_content.find('\n', norm_positions[0] + len(norm_old))
            orig_match = content[norm_line_start:norm_line_end if norm_line_end != -1 else len(content)]
            new_content = content.replace(orig_match, new, 1)
            with open(path, "w", encoding="utf-8") as f:
                f.write(new_content)
            return {"ok": True, "path": args["path"], "modified": True,
                    "matched_via": "whitespace_normalized"}

        line_info = []
        for pos in norm_positions[:5]:
            lineno = norm_content[:pos].count('\n') + 1
            snippet = norm_content[pos:pos + len(norm_old) + 40] + "..."
            line_info.append(f"  L{lineno}: ...{snippet}")
        return {"ok": False, "error":
            f"old_string 模糊匹配了 {len(norm_positions)} 处（仅空白差异），请提供更多上下文:\n" +
            "\n".join(line_info)}

    if count > 1:
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


def create() -> ToolDef:
    return ToolDef(
        name="edit_file",
        description="精确字符串替换编辑文件。old_string 必须在文件中唯一匹配。用于修改现有文件的部分内容。",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对于项目根目录的文件路径"},
                "old_string": {"type": "string", "description": "要替换的原始字符串（必须精确匹配）"},
                "new_string": {"type": "string", "description": "替换后的新字符串"},
                "reason": {"type": "string", "description": "修改原因（向用户解释）"},
            },
            "required": ["path", "old_string", "new_string"],
        },
        execute=execute,
        is_write=True,
    )

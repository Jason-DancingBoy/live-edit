"""Shell command execution tool."""

import subprocess

from ..safety import check_shell_cmd
from ..tool_registry import ToolDef


async def execute(args: dict, project_root: str, config=None) -> dict:
    cmd = args["cmd"]
    err = check_shell_cmd(cmd, project_root)
    if err:
        return {"ok": False, "error": err}
    timeout = 30
    if config and hasattr(config, "timeouts"):
        timeout = getattr(config.timeouts, "shell_command", 30)
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=project_root,
        )
        output = (result.stdout + result.stderr)[:5000]
        return {"ok": True, "cmd": cmd, "output": output, "exit_code": result.returncode}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "命令执行超时"}


def create() -> ToolDef:
    return ToolDef(
        name="run_shell",
        description="执行 shell 命令。危险命令（rm, git push, git reset --hard 等）会被自动拦截。",
        input_schema={
            "type": "object",
            "properties": {
                "cmd": {"type": "string", "description": "要执行的 shell 命令"},
                "reason": {"type": "string", "description": "执行原因（向用户解释）"},
            },
            "required": ["cmd"],
        },
        execute=execute,
        is_write=False,
    )

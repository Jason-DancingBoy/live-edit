"""``live-edit intake`` 编排 — 把 T1/T2/T3 串成一条命令。

新仓库接入一次完成：探测（``scan_project``）→ 深度分析渲染
（``render_extra_context``）→ verify 配置 + 冒烟测试（``provision_verify``）→
实际验证配置能工作（跑测试命令 / 探活 health_url）→ 写 ``.live-edit.toml``。

设计决策：
- 复用 ``config.generate_default_config`` 的完整模板（modes/prompt/safety/
  timeouts），只覆盖 ``extra_context`` 与 ``verify`` 字段，不重写 TOML 渲染
  （复用 ``cli._render_config``）。
- validate 环节只读：跑解析出的测试命令、对 health_url 发一次 GET，任何失败
  都不中断整体；测试命令验证失败时置空，让 verify 降级人工审批（安全设计）。
- 冒烟测试命令指向尚未生成的文件（``tests/test_smoke.py``），不在写盘前预跑，
  等文件生成后再跑一次确认绿，避免对未生成文件过早误判。
- 冒烟测试**先于** config 处理：先确认/生成冒烟，再写 config——用户取消冒烟时
  清空 ``config.verify.test_command``，避免写出的配置指向未生成的冒烟文件。
- 本模块**不 print**：dry-run 预览与结果全部进 ``messages``，由 CLI 统一打印。
- ``dry_run`` 是纯分析：跳过测试命令验证与 health 探活（不写 .pytest_cache、
  不发网络请求），只预览将写内容。
- 所有拼进 test_command 的仓库派生值一律 ``shlex.quote``（shell=True 的前提），
  与 verify_provision 的 ``_pytest_command`` 保持一致。
"""

from __future__ import annotations

import os
import shlex
import subprocess
import urllib.request
from dataclasses import dataclass

from live_edit.cli import _render_config
from live_edit.config import detect_project, generate_default_config, parse_config, validate_config
from live_edit.intake.analyzer import RepoProfile, scan_project
from live_edit.intake.context import render_extra_context
from live_edit.intake.verify_provision import VerifyProvision, provision_verify

__all__ = ["IntakeResult", "run_intake"]


@dataclass
class IntakeResult:
    """一次 run_intake 的完整结果（供 CLI 打印）。"""

    profile: RepoProfile
    provision: VerifyProvision
    config_path: str
    config_written: bool
    smoke_path: str | None
    smoke_written: bool
    validation: list[str]  # 每步验证结果的中文描述
    todos: list[str]  # extra_context 里的 TODO 标记（交给用户）
    messages: list[str]  # 面向用户的提示（含 dry-run 预览与下一步）


def _run_cmd(command: str, root: str, timeout: int = 60) -> bool:
    """在 root 下以 shell 运行命令，成功（returncode==0）返回 True。绝不抛异常。

    shell=True 因为解析出的命令含引号/路径（复用 T1 的 test_command 产出），
    仅用于本机 CLI 的只读验证；所有仓库派生的插值在构造命令时已 shlex.quote。
    """
    if not command:
        return False
    try:
        proc = subprocess.run(command, cwd=root, shell=True, timeout=timeout, capture_output=True)
        return proc.returncode == 0
    except Exception:
        return False


def _downgrade_candidates(profile: RepoProfile) -> list[str]:
    """按语言给出降级测试候选命令（python 两条，其余语言一条）。"""
    if profile.language == "python":
        target = profile.test_dirs[0] if profile.test_dirs else "tests"
        return [
            f"python3 -m pytest {shlex.quote(target)} -q --tb=short",
            "python3 -m pytest -q --tb=short",
        ]
    if profile.language == "typescript":
        return ["npm test"]
    if profile.language == "go":
        return ["go test ./..."]
    return []


def _probe_health(url: str) -> bool:
    """对 health_url 发一次 GET（timeout=3），不启动服务器。

    非 2xx / 超时 / 连接拒绝都返回 False（由调用方记为提示，不视为失败）。
    """
    if not url:
        return False
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:
            status: int = resp.status
            return 200 <= status < 300
    except Exception:
        return False


def _validate_test_command(
    profile: RepoProfile,
    provision: VerifyProvision,
    root: str,
    validation: list[str],
    messages: list[str],
) -> str:
    """验证 test_command；失败降级候选，都失败置空。返回最终生效的命令。

    冒烟测试命令指向尚未生成的文件，验证推迟到写盘后（_write_smoke），
    不在此时预跑——否则会对还不存在的文件过早误判。
    """
    command = provision.test_command
    if not command:
        return command
    if provision.smoke_test is not None:
        return command
    if _run_cmd(command, root):
        validation.append(f"测试命令验证通过: {command}")
        return command
    for candidate in _downgrade_candidates(profile):
        if _run_cmd(candidate, root):
            validation.append(f"测试命令验证失败，降级候选通过: {candidate}")
            return candidate
    validation.append("测试命令验证失败，verify 将降级人工审批（安全设计）")
    messages.append(
        "提示: 测试命令验证失败，请安装 pytest（pip install pytest）后重试，"
        "或手动配置 [verify] test_command"
    )
    return ""


def _write_config(
    config, root: str, force: bool, dry_run: bool, messages: list[str], validation: list[str]
):
    """写 .live-edit.toml（受 force/dry_run 控制），写后 parse+validate 复核。

    返回 (config_path, config_written)。

    dry_run 分支在 exists 检查**之前**：预演时无论配置是否已存在都输出完整预览
    （不写文件、不跑验证/探活），符合「dry-run 预览全进 messages」的承诺；只在
    非 dry-run 时才做「已存在且未 --force → 中止」。
    """
    config_path = os.path.join(root, ".live-edit.toml")
    if dry_run:
        messages.append(f"[dry-run] 将写入配置文件: {config_path}")
        messages.append("\n".join(_render_config(config)))
        return config_path, False
    if os.path.exists(config_path) and not force:
        messages.append(f"配置文件已存在: {config_path}（用 --force 覆盖）")
        return config_path, False
    lines = _render_config(config)
    with open(config_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    try:
        errors = validate_config(parse_config(config_path))
        if errors:
            validation.append(f"配置复核未通过: {errors}")
        else:
            validation.append("配置已写入并通过 parse_config + validate_config 复核")
    except Exception as e:
        validation.append(f"配置写入后复核异常: {e}")
    messages.append(f"已写入配置文件: {config_path}")
    return config_path, True


def _write_smoke(
    provision: VerifyProvision,
    root: str,
    dry_run: bool,
    auto_yes: bool,
    messages: list[str],
    validation: list[str],
):
    """生成冒烟测试（受 dry_run/auto_yes 控制），写后跑一次确认绿。

    返回 (smoke_path, smoke_written, cancelled)；cancelled=True 表示用户拒绝且
    文件未生成——调用方应清空 config.verify.test_command 避免悬空引用。
    """
    smoke = provision.smoke_test
    if smoke is None:
        return None, False, False
    smoke_path = os.path.join(root, smoke.path)
    if dry_run:
        messages.append(f"[dry-run] 将生成冒烟测试: {smoke.path}")
        messages.append(smoke.content)
        return smoke.path, False, False
    if not auto_yes:
        try:
            reply = input(f"将生成 {smoke.path}（可安全删除），继续？[y/N] ")
        except (EOFError, KeyboardInterrupt):
            reply = ""
        if reply.strip().lower() not in ("y", "yes"):
            messages.append(f"已取消冒烟测试生成: {smoke.path}")
            messages.append("提示: 可加 --yes 跳过确认")
            return smoke.path, False, True
    if os.path.exists(smoke_path):
        messages.append(f"冒烟测试已存在，未覆盖: {smoke.path}")
        return smoke.path, False, False
    os.makedirs(os.path.dirname(smoke_path) or root, exist_ok=True)
    with open(smoke_path, "w", encoding="utf-8") as f:
        f.write(smoke.content)
    if provision.test_command:
        if _run_cmd(provision.test_command, root):
            validation.append(f"冒烟测试通过: {provision.test_command}")
        else:
            validation.append(f"冒烟测试运行未通过: {provision.test_command}")
            messages.append("提示: 冒烟测试运行未通过，verify 将降级人工审批（安全设计）")
    else:
        validation.append("冒烟测试已生成，但 test_command 为空，verify 将降级人工审批（安全设计）")
    messages.append(f"已生成冒烟测试: {smoke.path}")
    return smoke.path, True, False


def _extract_todos(extra: str) -> list[str]:
    """从 extra_context 文本里提取 ``TODO:`` 开头的行。"""
    return [line.strip() for line in extra.splitlines() if line.strip().startswith("TODO:")]


def _append_next_steps(messages: list[str], config) -> None:
    """下一步动作提示（参考 cmd_init 的「下一步」风格）。"""
    messages.append("下一步:")
    messages.append(
        f"  1. 设置 LLM API key 环境变量：{config.llm.api_key_env}=...（禁止硬编码到代码/配置）"
    )
    messages.append("  2. 运行 live-edit check 验证配置")
    messages.append(
        "  3. 在代码中添加: from live_edit import setup_live_edit; "
        "app.include_router(setup_live_edit())"
    )
    messages.append(
        "  4. 检查 [verify] 段 —— test_command / health_url 是否正确（空则自动降级人工审批）"
    )


def run_intake(
    root: str = ".", *, dry_run: bool = False, force: bool = False, auto_yes: bool = False
) -> IntakeResult:
    """编排一次新仓库接入，返回 IntakeResult。"""
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        raise ValueError(f"目录不存在: {root}")

    profile = scan_project(root)
    provision = provision_verify(profile)
    extra = render_extra_context(profile)

    # 复用完整模板（modes/prompt/safety/timeouts），只覆盖三个字段
    config = generate_default_config(root, detect_project(root))
    config.project.extra_context = extra
    config.verify.test_command = provision.test_command
    config.verify.health_url = provision.health_url

    validation: list[str] = []
    messages: list[str] = []

    # 4. 验证配置能工作（dry_run 跳过：不跑测试、不探活，保持纯分析）
    if dry_run:
        validation.append("dry-run 未执行验证（测试命令与健康检查不会实际运行）")
    else:
        config.verify.test_command = _validate_test_command(
            profile, provision, root, validation, messages
        )
        if provision.health_url:
            if _probe_health(provision.health_url):
                validation.append(f"健康检查通过: {provision.health_url}")
            else:
                validation.append(
                    f"未探测到服务: {provision.health_url}"
                    "（health_url 保留配置，verify 会自行处理）"
                )

    # 5a. 冒烟测试先处理（确认/生成）——用户取消时清空 test_command，避免 config 悬空
    smoke_path: str | None = None
    smoke_written = False
    if provision.smoke_test is not None:
        smoke_path, smoke_written, cancelled = _write_smoke(
            provision, root, dry_run, auto_yes, messages, validation
        )
        if cancelled:
            config.verify.test_command = ""
            messages.append("提示: verify 将降级人工审批（安全设计）")

    # 5b. 写 config（受 dry_run/force 控制）
    config_path, config_written = _write_config(config, root, force, dry_run, messages, validation)

    # 6. 收集 todos / 7. 组装 messages
    todos = _extract_todos(extra)
    _append_next_steps(messages, config)

    return IntakeResult(
        profile=profile,
        provision=provision,
        config_path=config_path,
        config_written=config_written,
        smoke_path=smoke_path,
        smoke_written=smoke_written,
        validation=validation,
        todos=todos,
        messages=messages,
    )

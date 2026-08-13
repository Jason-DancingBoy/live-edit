"""CLI for live-edit: init, intake, agent-hook, and check commands."""

import os
import sys


def cmd_init(root: str = ".", force: bool = False) -> bool:
    """Generate a .live-edit.toml in the target directory.

    Returns True on success, False if config already exists (without --force).
    """
    root = os.path.abspath(root)
    config_path = os.path.join(root, ".live-edit.toml")

    if os.path.exists(config_path) and not force:
        print(f"配置文件已存在: {config_path}")
        print("使用 --force 强制覆盖")
        return False

    from .config import detect_project, generate_default_config

    project = detect_project(root)
    config = generate_default_config(root, project)

    # Write TOML
    lines = _render_config(config)
    with open(config_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print(f"已生成配置文件: {config_path}")
    print(f"  检测到项目: {project.get('name', 'unknown')}")
    print(f"  语言: {project.get('language', 'unknown')}")
    framework = project.get("framework", "")
    if framework:
        print(f"  框架: {framework}")
    print()
    print("下一步:")
    print("  1. 检查并编辑 .live-edit.toml 中的配置")
    print("  2. 设置 LLM API key 环境变量")
    print("  3. 在代码中添加: from live_edit import setup_live_edit")
    print("     app.include_router(setup_live_edit())")
    return True


def cmd_agent_hook(root: str = ".", force: bool = False) -> bool:
    """Generate .live-edit/AGENTS.md so an AI agent can auto-complete the
    live-edit integration when the user asks it to.

    Returns True on success, False if the guide already exists (without --force)
    or the directory does not exist.
    """
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        print(f"目录不存在: {root}")
        return False

    from .config import detect_project
    from .hook import render_agent_hook

    guide_dir = os.path.join(root, ".live-edit")
    guide_path = os.path.join(guide_dir, "AGENTS.md")

    if os.path.exists(guide_path) and not force:
        print(f"引导文件已存在: {guide_path}")
        print("使用 --force 强制覆盖")
        return False

    project = detect_project(root)
    content = render_agent_hook(project, root)

    os.makedirs(guide_dir, exist_ok=True)
    with open(guide_path, "w", encoding="utf-8") as f:
        f.write(content)

    print(f"已生成引导文件: {guide_path}")
    print(f"  项目: {project.get('name', 'unknown')}")
    print(f"  语言: {project.get('language', 'unknown')}")
    framework = project.get("framework", "")
    if framework:
        print(f"  框架: {framework}")
    print()
    print("打开你的 AI agent（Claude Code / Cursor 等），对它说：")
    print('  "接入 live-edit"')
    print("agent 会读取 .live-edit/AGENTS.md 并自动完成接入。")
    return True


def cmd_check(config_path: str) -> bool:
    """Validate a .live-edit.toml configuration file.

    Returns True if valid, False otherwise.
    """
    if not os.path.exists(config_path):
        print(f"配置文件不存在: {config_path}")
        return False

    from .config import parse_config, validate_config

    try:
        config = parse_config(config_path)
    except Exception as e:
        print(f"解析配置失败: {e}")
        return False

    errors = validate_config(config)
    if errors:
        print(f"配置验证失败 ({len(errors)} 个问题):")
        for err in errors:
            print(f"  - {err}")
        return False

    print("配置验证通过")
    print(f"  项目: {config.project.name}")
    print(f"  模式: {', '.join(config.modes.keys()) if config.modes else 'none'}")
    return True


def cmd_intake(
    root: str = ".", *, dry_run: bool = False, force: bool = False, auto_yes: bool = False
) -> bool:
    """Run the full intake pipeline: scan → extra_context → verify → smoke test → write.

    Returns True on success; False if the target directory is invalid, or if an
    existing config was left untouched (matches cmd_init's --force semantics).
    """
    from .intake import run_intake

    try:
        result = run_intake(root, dry_run=dry_run, force=force, auto_yes=auto_yes)
    except ValueError as e:
        print(f"错误: {e}")
        return False

    p = result.profile
    if dry_run:
        print("[dry-run] 预演模式：不写任何文件（下方为将写入的内容）")
    else:
        print(f"项目: {p.name}")
        print(f"  语言: {p.language}  框架: {p.framework or '无'}  端口: {p.port}")
        if result.config_written:
            print(f"  已写入配置: {result.config_path}")
        else:
            print(f"  配置未写入: {result.config_path}")
        if result.smoke_written:
            print(f"  已生成冒烟测试: {result.smoke_path}")
    print()
    for v in result.validation:
        print(f"  - {v}")
    if result.todos:
        print()
        print("待补充（TODO，建议优先处理）:")
        for t in result.todos:
            print(f"  - {t}")
    print()
    for m in result.messages:
        print(m)
    return dry_run or result.config_written


def _toml_str(value: str) -> str:
    """Escape a value for interpolation into a TOML basic (double-quoted) string.

    User-provided strings commonly contain backslashes and double quotes; without
    escaping they would produce invalid TOML (e.g. ``command = "pytest "tests\"""``).
    Control characters (``\\n``, ``\\r``, ``\\t``) are escaped too so basic
    strings stay valid TOML and round-trip losslessly.
    """
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )


def _toml_multiline(value: str) -> str:
    """Escape a value for interpolation into a TOML multiline string.

    Interior newlines are valid literal content inside ``\"\"\"...\"\"\"``, so they
    are kept as-is. But the TOML spec trims the first newline immediately after
    the opening delimiter, so a value that *starts* with a newline must escape
    that leading newline to round-trip losslessly. Backslashes, quotes, CR, and
    tab are escaped like in ``_toml_str``.
    """
    escaped = (
        value.replace("\\", "\\\\").replace('"', '\\"').replace("\r", "\\r").replace("\t", "\\t")
    )
    if escaped.startswith("\n"):
        escaped = "\\n" + escaped[1:]
    return escaped


def _render_config(config) -> list[str]:
    """Render a Config object as TOML lines."""
    lines = []
    p = config.project
    llm = config.llm
    s = config.safety
    t = config.timeouts
    sess = config.sessions
    h = config.hooks
    u = config.ui

    lines.append("[project]")
    lines.append(f'name = "{_toml_str(p.name)}"')
    lines.append(f'language = "{_toml_str(p.language)}"')
    if p.framework:
        lines.append(f'framework = "{_toml_str(p.framework)}"')
    lines.append(f'root = "{_toml_str(p.root)}"')
    extra = getattr(p, "extra_context", "")
    if extra:
        lines.append(f'extra_context = """{_toml_multiline(extra)}"""')
    lines.append("")

    lines.append("[llm]")
    lines.append(f'provider = "{_toml_str(llm.provider)}"')
    lines.append(f'api_url = "{_toml_str(llm.api_url)}"')
    lines.append(f'api_key_env = "{_toml_str(llm.api_key_env)}"')
    lines.append(f'model = "{_toml_str(llm.model)}"')
    lines.append("")

    lines.append("[safety]")
    if s.allowed_dirs:
        dirs = ", ".join(f'"{_toml_str(d)}"' for d in s.allowed_dirs)
        lines.append(f"allowed_dirs = [{dirs}]")
    if s.overwrite_allowed_dirs:
        dirs = ", ".join(f'"{_toml_str(d)}"' for d in s.overwrite_allowed_dirs)
        lines.append(f"overwrite_allowed_dirs = [{dirs}]")
    lines.append(f"allow_overwrite_existing = {str(s.allow_overwrite_existing).lower()}")
    blocked = getattr(s, "blocked_commands", [])
    if blocked:
        cmds = ", ".join(f'"{_toml_str(c)}"' for c in blocked)
        lines.append(f"blocked_commands = [{cmds}]")
    if s.search_extensions:
        exts = ", ".join(f'"{_toml_str(e)}"' for e in s.search_extensions)
        lines.append(f"search_extensions = [{exts}]")
    lines.append("")

    lines.append("[timeouts]")
    lines.append(f"api_request = {t.api_request}")
    lines.append(f"shell_command = {t.shell_command}")
    lines.append(f"approval = {t.approval}")
    lines.append(f"final_approval = {t.final_approval}")
    lines.append(f"session_ttl = {t.session_ttl}")
    lines.append(f"max_rounds = {t.max_rounds}")
    lines.append("")

    lines.append("[sessions]")
    lines.append(f"max_active = {sess.max_active}")
    lines.append("")

    lines.append("[hooks]")
    if h.post_revert:
        lines.append(f'post_revert = "{_toml_str(h.post_revert)}"')
    pre_commit = getattr(h, "pre_commit", "")
    if pre_commit:
        lines.append(f'pre_commit = "{_toml_str(pre_commit)}"')
    lines.append("")

    lines.append("[ui]")
    lines.append(f'default_mode = "{_toml_str(u.default_mode)}"')
    lines.append("")

    # Modes
    for mode_name, mode in (config.modes or {}).items():
        lines.append(f"[modes.{mode_name}]")
        lines.append(f'label = "{_toml_str(mode.label)}"')
        lines.append(f'approval = "{_toml_str(mode.approval)}"')
        lines.append(f'tools = "{_toml_str(mode.tools)}"')
        if mode.approve_for:
            af = ", ".join(f'"{_toml_str(a)}"' for a in mode.approve_for)
            lines.append(f"approve_for = [{af}]")
        lines.append("")

        if mode.prompt:
            lines.append(f"[modes.{mode_name}.prompt]")
            lines.append(f'base = """{_toml_multiline(mode.prompt.base)}"""')
            lines.append(f'user_persona = """{_toml_multiline(mode.prompt.user_persona)}"""')
            lines.append(
                f'communication_rules = """{_toml_multiline(mode.prompt.communication_rules)}"""'
            )
            lines.append("")

    # Preview
    pv = config.preview
    lines.append("[preview]")
    lines.append(f"enabled = {str(pv.enabled).lower()}")
    lines.append(f"port_start = {pv.port_start}")
    lines.append(f"port_end = {pv.port_end}")
    lines.append(f"startup_timeout = {pv.startup_timeout}")
    if pv.command:
        lines.append(f'command = "{_toml_str(pv.command)}"')
    if pv.base_url:
        lines.append(f'base_url = "{_toml_str(pv.base_url)}"')
    lines.append("")

    # Verify (quality gate)
    v = config.verify
    lines.append("[verify]")
    lines.append(f"enabled = {str(v.enabled).lower()}")
    lines.append(f"max_retry = {v.max_retry}")
    if v.test_command:
        lines.append(f'test_command = "{_toml_str(v.test_command)}"')
    if v.health_url:
        lines.append(f'health_url = "{_toml_str(v.health_url)}"')
    lines.append(f"semantic_enabled = {str(v.semantic_enabled).lower()}")
    if v.semantic_assert_text:
        asserts = ", ".join(f'"{_toml_str(a)}"' for a in v.semantic_assert_text)
        lines.append(f"semantic_assert_text = [{asserts}]")
    lines.append("")

    # Error translations
    err_quick = config.errors.quick if hasattr(config.errors, "quick") else {}
    err_deep = config.errors.deep if hasattr(config.errors, "deep") else {}
    if err_quick or err_deep:
        for err_name, err_map in [("quick", err_quick), ("deep", err_deep)]:
            if err_map:
                lines.append(f"[errors.{err_name}]")
                for k, v in err_map.items():
                    lines.append(f'"{_toml_str(k)}" = "{_toml_str(v)}"')
                lines.append("")

    return lines


def _print_help():
    print("live-edit — 自然语言驱动的即时编辑")
    print()
    print("用法:")
    print("  live-edit init   [目录]  生成 .live-edit.toml 配置文件")
    print("  live-edit intake [目录]  自动生成配置（探测+extra_context+verify+冒烟测试+验证）")
    print("  live-edit agent-hook [目录]  生成 .live-edit/AGENTS.md（让 agent 自动接入）")
    print("  live-edit check  [路径]  验证配置文件")
    print()
    print("选项:")
    print("  --force                 强制覆盖已有配置（init / intake / agent-hook）")
    print("  --dry-run               预演，不写文件（intake）")
    print("  --yes                   跳过冒烟测试确认（intake）")
    print("  --help, -h              显示此帮助信息")


def main():
    """CLI entry point: live-edit [init|intake|agent-hook|check]."""
    if len(sys.argv) < 2 or sys.argv[1] in ("--help", "-h"):
        _print_help()
        sys.exit(0)

    cmd = sys.argv[1]
    args = sys.argv[2:]

    if cmd == "init":
        force = "--force" in args
        path_args = [a for a in args if a != "--force"]
        root = path_args[0] if path_args else "."
        ok = cmd_init(root=root, force=force)
        sys.exit(0 if ok else 1)

    elif cmd == "intake":
        dry_run = "--dry-run" in args
        force = "--force" in args
        auto_yes = "--yes" in args
        path_args = [a for a in args if a not in ("--dry-run", "--force", "--yes")]
        root = path_args[0] if path_args else "."
        ok = cmd_intake(root=root, dry_run=dry_run, force=force, auto_yes=auto_yes)
        sys.exit(0 if ok else 1)

    elif cmd == "agent-hook":
        force = "--force" in args
        path_args = [a for a in args if a != "--force"]
        root = path_args[0] if path_args else "."
        ok = cmd_agent_hook(root=root, force=force)
        sys.exit(0 if ok else 1)

    elif cmd == "check":
        path = args[0] if args else ".live-edit.toml"
        ok = cmd_check(path)
        sys.exit(0 if ok else 1)

    else:
        print(f"未知命令: {cmd}")
        print("可用命令: init, intake, agent-hook, check")
        sys.exit(1)


if __name__ == "__main__":
    main()

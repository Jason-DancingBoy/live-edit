"""CLI for live-edit: init and check commands."""

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
    framework = project.get('framework', '')
    if framework:
        print(f"  框架: {framework}")
    print()
    print("下一步:")
    print("  1. 检查并编辑 .live-edit.toml 中的配置")
    print("  2. 设置 LLM API key 环境变量")
    print("  3. 在代码中添加: from live_edit import setup_live_edit")
    print("     app.include_router(setup_live_edit())")
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


def _render_config(config) -> list[str]:
    """Render a Config object as TOML lines."""
    lines = []
    p = config.project
    l = config.llm
    s = config.safety
    t = config.timeouts
    sess = config.sessions
    h = config.hooks
    u = config.ui

    lines.append("[project]")
    lines.append(f'name = "{p.name}"')
    lines.append(f'language = "{p.language}"')
    if p.framework:
        lines.append(f'framework = "{p.framework}"')
    lines.append(f'root = "{p.root}"')
    extra = getattr(p, 'extra_context', '')
    if extra:
        lines.append(f'extra_context = """{extra}"""')
    lines.append("")

    lines.append("[llm]")
    lines.append(f'provider = "{l.provider}"')
    lines.append(f'api_url = "{l.api_url}"')
    lines.append(f'api_key_env = "{l.api_key_env}"')
    lines.append(f'model = "{l.model}"')
    lines.append("")

    lines.append("[safety]")
    if s.allowed_dirs:
        dirs = ', '.join(f'"{d}"' for d in s.allowed_dirs)
        lines.append(f"allowed_dirs = [{dirs}]")
    if s.overwrite_allowed_dirs:
        dirs = ', '.join(f'"{d}"' for d in s.overwrite_allowed_dirs)
        lines.append(f"overwrite_allowed_dirs = [{dirs}]")
    lines.append(f"allow_overwrite_existing = {str(s.allow_overwrite_existing).lower()}")
    blocked = getattr(s, 'blocked_commands', [])
    if blocked:
        cmds = ', '.join(f'"{c}"' for c in blocked)
        lines.append(f"blocked_commands = [{cmds}]")
    if s.search_extensions:
        exts = ', '.join(f'"{e}"' for e in s.search_extensions)
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
        lines.append(f'post_revert = "{h.post_revert}"')
    pre_commit = getattr(h, 'pre_commit', '')
    if pre_commit:
        lines.append(f'pre_commit = "{pre_commit}"')
    lines.append("")

    lines.append("[ui]")
    lines.append(f'default_mode = "{u.default_mode}"')
    lines.append("")

    # Modes
    for mode_name, mode in (config.modes or {}).items():
        lines.append(f"[modes.{mode_name}]")
        lines.append(f'label = "{mode.label}"')
        lines.append(f'approval = "{mode.approval}"')
        lines.append(f'tools = "{mode.tools}"')
        if mode.approve_for:
            af = ', '.join(f'"{a}"' for a in mode.approve_for)
            lines.append(f"approve_for = [{af}]")
        lines.append("")

        if mode.prompt:
            lines.append(f"[modes.{mode_name}.prompt]")
            lines.append(f'base = """{mode.prompt.base}"""')
            lines.append(f'user_persona = """{mode.prompt.user_persona}"""')
            lines.append(f'communication_rules = """{mode.prompt.communication_rules}"""')
            lines.append("")

    # Preview
    pv = config.preview
    lines.append("[preview]")
    lines.append(f"enabled = {str(pv.enabled).lower()}")
    lines.append(f"port_start = {pv.port_start}")
    lines.append(f"port_end = {pv.port_end}")
    lines.append(f"startup_timeout = {pv.startup_timeout}")
    if pv.command:
        lines.append(f'command = "{pv.command}"')
    if pv.base_url:
        lines.append(f'base_url = "{pv.base_url}"')
    lines.append("")

    # Error translations
    err_quick = config.errors.quick if hasattr(config.errors, 'quick') else {}
    err_deep = config.errors.deep if hasattr(config.errors, 'deep') else {}
    if err_quick or err_deep:
        for err_name, err_map in [("quick", err_quick), ("deep", err_deep)]:
            if err_map:
                lines.append(f"[errors.{err_name}]")
                for k, v in err_map.items():
                    lines.append(f'"{k}" = "{v}"')
                lines.append("")

    return lines


def _print_help():
    print("live-edit — 自然语言驱动的即时编辑")
    print()
    print("用法:")
    print("  live-edit init   [目录]  生成 .live-edit.toml 配置文件")
    print("  live-edit check  [路径]  验证配置文件")
    print()
    print("选项:")
    print("  --force                 强制覆盖已有配置（init）")
    print("  --help, -h              显示此帮助信息")


def main():
    """CLI entry point: live-edit [init|check]."""
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

    elif cmd == "check":
        path = args[0] if args else ".live-edit.toml"
        ok = cmd_check(path)
        sys.exit(0 if ok else 1)

    else:
        print(f"未知命令: {cmd}")
        print("可用命令: init, check")
        sys.exit(1)


if __name__ == "__main__":
    main()

"""Generate .live-edit/AGENTS.md — the instruction file an AI agent reads to
auto-complete a live-edit integration for the host project.
"""

import os


def _base_section(project: dict, root: str) -> list[str]:
    """Common header shared by all project types."""
    name = project.get("name", "unknown")
    lines = [
        "# live-edit 接入任务",
        "",
        "用户要求接入 live-edit。请按以下步骤自动完成接入。",
        "",
        "## 项目已探测",
        f"- 项目名: {name}",
        f"- 语言: {project.get('language', 'unknown')}",
    ]
    framework = project.get("framework", "")
    if framework:
        lines.append(f"- 框架: {framework}")
    lines.append(f"- VCS: {project.get('vcs', 'none')}")
    if project.get("git_available"):
        lines.append("- 已检测到 git 仓库（live-edit 依赖 git worktree 隔离会话）")
    else:
        lines.append(
            "- ⚠️ 未检测到 git 仓库：live-edit 依赖 git worktree，请先运行 `git init` 再继续"
        )
    lines.append("")
    lines.append("## 参考来源")
    lines.append("- 完整流程: 项目的 README.md / USER_MANUAL.md（若存在）")
    lines.append("- 快速上手: live-edit 仓库的 README.md「接入新项目」一节")
    lines.append("- 配置命令: `live-edit init .` 生成 .live-edit.toml；`live-edit check` 验证")
    lines.append("")
    return lines


def _live_edit_path() -> str:
    """Resolve the live-edit source path when editable-installed; else placeholder."""
    try:
        import live_edit

        src = os.path.dirname(os.path.dirname(os.path.abspath(live_edit.__file__)))
        # editable install 时 src 是源码仓库根；确认它看起来像 live-edit 仓库
        if os.path.exists(os.path.join(src, "live_edit", "__init__.py")) and os.path.exists(
            os.path.join(src, "pyproject.toml")
        ):
            return src
    except Exception:
        pass
    return "<live-edit 源码绝对路径>"  # 回退占位符，提示用户替换


def _fastapi_section(project: dict, root: str) -> list[str]:
    """Full library-embed instructions for a FastAPI host."""
    test_cmd = project.get("test_command", "")
    health = project.get("health_url", "")
    lines = [
        "## 推荐路径: 库嵌入（FastAPI）",
        "",
        "### 1. 安装 live-edit",
        f"运行: `python -m pip install -e {_live_edit_path()}`",
        "（若项目当前没有激活的 Python 环境，先创建并激活虚拟环境再装）",
        "（若不确定 live-edit 源码路径，先询问用户）",
        "",
        "### 2. 生成并检查配置",
        "- 运行 `live-edit init .` 生成 .live-edit.toml",
        "- 检查 [llm] 段: api_key_env 指向项目 .env 里的现有 key 变量名（**不要写明文密钥**）",
    ]
    if test_cmd:
        lines.append(
            f"- 检查 [verify] 段: test_command = `{test_cmd}`（若全量测试过慢，只留快速子集）"
        )
    else:
        lines.append(
            "- [verify] 段未探测到测试命令，可留空（verify 会自动降级人工审批，这是安全设计）"
        )
    if health:
        lines.append(f"- [verify] 段: health_url = `{health}`")
    lines += [
        "",
        "### 3. 挂载路由（关键顺序）",
        "- 在 main.py 中，`from live_edit import setup_live_edit`",
        "- `app.include_router(setup_live_edit(project_root='<绝对路径>', "
        "config_path='.live-edit.toml', admin_key=os.environ.get('LIVE_EDIT_ADMIN_KEY', '')))`",
        "- **必须在 `app.mount('/', StaticFiles(...))` 之前挂载**，"
        "否则 /live-edit/* 被 catch-all 吞掉",
        "",
        "### 4. 认证",
        "- 若项目已有 basic auth 中间件，把 /live-edit/* 纳入保护",
        "- **豁免**: /live-edit/static、/live-edit/health、"
        "/live-edit/metrics（前端要加载静态资源、health 检查无凭据）",
        "- 若项目无认证体系，保持 /live-edit/* 公开（dev 工具性质）",
        "",
        "### 5. 密钥",
        "- LLM API key: 复用项目已有的环境变量，不硬编码",
        "- admin_key: 生成随机长 token 写入 .env 的 LIVE_EDIT_ADMIN_KEY（deep 模式合并门禁依赖它）",
        "- 若项目启动时不自动加载 .env 到进程环境，需在启动脚本 source .env "
        "或用 python-dotenv 注入（否则 admin_key 恒为空，deep 模式合并门禁会 403）",
        "",
        "### 6. 前端",
        '- 在 index.html 的 </body> 前加 `<script src="/live-edit/static/live-edit.js"></script>`',
        "- CSS 自动加载，无需手动 <link>",
        "",
        "### 7. 验证",
        "- 运行项目测试，确认无回归",
        "- 启动服务，检查:",
        "- GET /live-edit/health → 200",
        "- GET /live-edit/stream 无凭据 → 401（若启用认证）",
        "- 浏览器打开页面，按 Ctrl+Shift+D 面板出现、样式完整",
        "- 确认 live_edit.db 出现在项目根（gitignored，勿提交）",
        "",
    ]
    return lines


def _degraded_section(project: dict, root: str) -> list[str]:
    """Degraded path for non-FastAPI or unknown projects.

    live-edit 是纯库嵌入形态，没有独立服务/登录/编辑页面。非 FastAPI 宿主
    无法直接接入，只能提示迁移到 FastAPI 或联系维护者。
    """
    language = project.get("language", "unknown")
    lines = [
        f"## 当前无法直接接入（非 FastAPI: {language}）",
        "",
        "live-edit 是库嵌入形态，依赖宿主项目是 FastAPI 应用（依赖库含 fastapi），",
        "本项目未检测到 FastAPI 入口（main.py / backend/main.py + fastapi 依赖），",
        "因此无法按标准步骤自动完成接入。",
        "",
        "### 可选路径",
        "- 将宿主项目迁移到 FastAPI（保留现有入口，新增 main.py 承载 live-edit）",
        "- 联系 live-edit 维护者，说明技术栈，获取适配方案",
        "",
        "### 若确定迁移到 FastAPI，基本步骤",
        f"- 安装: `python -m pip install -e {_live_edit_path()}`（或用项目的包管理器）",
        "- 生成配置: 运行 `live-edit init .` 生成 .live-edit.toml",
        "- 挂载: 在 main.py 中 `from live_edit import setup_live_edit`；",
        "  `app.include_router(setup_live_edit(project_root='<绝对路径>', "
        "config_path='.live-edit.toml', admin_key=os.environ.get('LIVE_EDIT_ADMIN_KEY', '')))`",
        "- 验证: 运行 `live-edit check .live-edit.toml`",
        "",
        "> 注意: 迁移完成前不要照搬其他项目的 live-edit 接入代码，先确认宿主已具备 FastAPI 入口。",
        "",
    ]
    return lines


def render_agent_hook(project: dict, root: str) -> str:
    """Render the full AGENTS.md instruction text for a probed project."""
    lines = _base_section(project, root)
    if project.get("framework") == "fastapi":
        lines += _fastapi_section(project, root)
    else:
        lines += _degraded_section(project, root)
    lines += [
        "## 约束",
        "- 只改动上述步骤涉及的文件，不碰 .env 里已存在的其他密钥",
        "- 不硬编码任何 API key / token / 密码（CLAUDE.md 红线）",
        "- 遇到项目结构不确定时，先读 README.md / USER_MANUAL.md 或询问用户",
        "- 若某一步无法自动完成，停下来向用户说明，不要静默跳过",
        "",
    ]
    return "\n".join(lines) + "\n"

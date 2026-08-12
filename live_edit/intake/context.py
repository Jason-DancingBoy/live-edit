"""extra_context 渲染器 — 把深度分析结果 RepoProfile 渲染成高质量的中文 markdown。

供 `live-edit intake` 命令使用：替换旧版「5 行技术栈 + 请在此填写占位符」
的手写方案（对比见 live-build 的 ``_build_extra_context``，本仓测试内联的
legacy 渲染器见 tests/test_intake_context.py），产出影响 AI 产出质量最关键
的 ``[project.extra_context]``（.live-edit.toml）。

确定性、纯函数：同一 RepoProfile 两次渲染结果完全一致；不读文件、不跑命令。

TOML 转义边界：本模块只做 markdown 层处理（关键路由表单元格内 ``|`` → ``\\|``），
反斜杠/双引号的 TOML 转义完全交给写盘侧的 ``cli._toml_str`` /
``cli._toml_multiline``（``\\`` → ``\\\\``、``\"`` → ``\\"``，无损往返），
本模块**不**转义反斜杠、不折叠双引号、不压换行。

约束：
- 只陈述 RepoProfile 里的事实，禁止编造/推测业务逻辑；
- 未知/缺失字段用 ``TODO:`` 一行标记，不留空白占位；
- 不涉及 .env 值，只引用变量名。
"""

from __future__ import annotations

import re

from live_edit.intake.analyzer import RepoProfile

__all__ = ["render_extra_context"]

# 核心业务链路里值得点名的模块职责（路由/模型/服务/迁移/入口等）
_CORE_PURPOSES = frozenset(
    {
        "HTTP 路由",
        "数据模型/序列化",
        "业务逻辑",
        "数据库迁移",
        "应用入口",
        "数据模型",
        "数据库",
        "序列化模式",
        "依赖注入",
        "认证",
        "后台任务",
    }
)

# 关键路由表最多列出的行数
_ROUTE_TABLE_LIMIT = 15

# 需要翻译成"未检测到"的哨兵值
_UNKNOWN_VALUES = {"", "unknown", "none"}

# 路径里"secret/secrets"作为独立段（用 / _ . - 等分隔）才算密钥目录，
# 避免"topsecret_docs"之类的子串误判
_SECRET_SEGMENT_RE = re.compile(r"(^|[/_.-])(secret|secrets)([/_.-]|$)")


def _table_cell(text: str) -> str:
    """把关键路由表单元格清洗成单行 markdown 表格安全文本。

    - ``|`` 是 markdown 表格列分隔符，转义为 ``\\|``（TOML 里 ``|`` 无特殊
      含义，此转义只服务于 markdown 渲染；写盘时 ``_toml_multiline`` 会把
      反斜杠再无损转义，往返不丢）；
    - 换行会破坏单行表格，压成空格（仅限单元格内部，不影响整体换行结构）。
    """
    text = str(text).replace("\r", " ").replace("\n", " ")
    return text.replace("|", "\\|")


def _value_or(value: str) -> str:
    """空/unknown/none 值统一渲染为"未检测到"，其余原样输出。"""
    return "未检测到" if value in _UNKNOWN_VALUES else str(value)


def _is_secretish(path: str) -> bool:
    """判定保护路径是否属于环境变量/密钥类文件（.env、*.pem、*.key、secrets/）。"""
    lower = path.lower()
    if lower.startswith(".env"):
        return True
    if lower.endswith((".pem", ".key")):
        return True
    return _SECRET_SEGMENT_RE.search(lower) is not None


def _tech_stack_lines(profile: RepoProfile) -> list[str]:
    """第 1 节：项目技术栈。"""
    lines = ["## 项目技术栈"]
    lines.append(f"- 语言: {_value_or(profile.language)}")
    lines.append(f"- 框架: {_value_or(profile.framework)}")
    lines.append(f"- 包管理器: {_value_or(profile.package_manager)}")
    lines.append(f"- 版本控制: {_value_or(profile.vcs)}")
    lines.append(f"- 服务端口: {profile.port}")
    if profile.frontend is None:
        lines.append("- 前端: 未检测到")
    elif profile.frontend.kind == "compile":
        build = profile.frontend.build_command or "未检测到构建命令"
        lines.append(f"- 前端: 编译型（构建命令: {build}）")
    elif profile.frontend.kind == "static":
        lines.append("- 前端: 静态（static/ 目录直出，无构建步骤）")
    else:
        lines.append(f"- 前端: {profile.frontend.kind}")
    return lines


def _entry_lines(profile: RepoProfile) -> list[str]:
    """第 2 节：入口与服务。"""
    lines = ["## 入口与服务"]
    entries = "、".join(sorted(profile.entry_points)) if profile.entry_points else "未检测到"
    lines.append(f"- 入口文件: {entries}")
    app_module = profile.app_module or "未检测到"
    lines.append(f"- 应用模块: {app_module}")
    if profile.language == "python" and profile.app_module:
        # 仅当能定位到 ASGI 应用对象时给出 uvicorn 启动建议（措辞软化，不打包票）
        lines.append(f"- 启动方式: 若已安装 uvicorn，可用 `uvicorn {app_module}` 启动")
    else:
        lines.append("- 启动方式: 未检测到")
    health_url = profile.health_url or "未检测到"
    lines.append(f"- 健康检查: {health_url}")
    return lines


def _modules_lines(profile: RepoProfile) -> list[str]:
    """第 3 节：文件结构与模块地图。"""
    lines = ["## 文件结构与模块地图"]
    if not profile.modules:
        lines.append("- 未检测到模块结构")
        return lines
    for m in sorted(profile.modules, key=lambda m: m.path):
        lines.append(f"- {m.path}: {m.purpose}")
    return lines


def _core_lines(profile: RepoProfile) -> list[str]:
    """第 4 节：核心业务链路 — 只陈述扫描到的事实，不编造业务逻辑。"""
    lines = ["## 核心业务链路"]
    facts: list[str] = []
    routes = sorted(profile.routes, key=lambda r: (r.method, r.path, r.source))
    if routes:
        sample = "、".join(f"{r.method} {r.path}" for r in routes[:5])
        if len(routes) > 5:
            sample += "…"
        facts.append(f'- 检测到 {len(routes)} 个 HTTP 路由：{sample}（完整表见"关键路由表"）')
    if profile.db is not None:
        facts.append(f"- 数据库: {profile.db.hint or '未检测到'}")
    core_modules = [
        m for m in sorted(profile.modules, key=lambda m: m.path) if m.purpose in _CORE_PURPOSES
    ]
    if core_modules:
        detail = "、".join(f"{m.path}（{m.purpose}）" for m in core_modules)
        facts.append(f"- 关键模块: {detail}")
    if facts:
        lines.extend(facts)
    else:
        lines.append("TODO: 补充核心业务链路（路由/模型/外部依赖）")
    return lines


def _tests_lines(profile: RepoProfile) -> list[str]:
    """第 5 节：测试。"""
    lines = ["## 测试"]
    if profile.has_tests:
        command = profile.test_command or "未检测到"
        lines.append(f"- 测试命令: {command}")
        if profile.test_dirs:
            dirs = "、".join(sorted(profile.test_dirs))
            lines.append(f"- 测试目录: {dirs}")
    else:
        lines.append("TODO: 项目未检测到测试，intake 会生成冒烟测试")
    return lines


def _notes_lines(profile: RepoProfile) -> list[str]:
    """第 6 节：注意事项与禁改目录。"""
    lines = ["## 注意事项与禁改目录"]
    if not profile.protected_paths:
        lines.append("- 未检测到保护路径")
    else:
        for path in sorted(profile.protected_paths):
            if _is_secretish(path):
                lines.append(f"- {path}：环境变量/密钥文件，禁止写入")
            else:
                lines.append(f"- {path}：禁止修改")
    if profile.db is not None and profile.db.url_env:
        lines.append(f"- 数据库连接走 {profile.db.url_env}（.env 环境变量），禁止硬编码")
    return lines


def _routes_lines(profile: RepoProfile) -> list[str] | None:
    """第 7 节：关键路由表；无路由时不输出该节。"""
    routes = sorted(profile.routes, key=lambda r: (r.method, r.path, r.source))
    if not routes:
        return None
    lines = ["## 关键路由表", "| 方法 | 路径 | 来源 |", "| --- | --- | --- |"]
    for r in routes[:_ROUTE_TABLE_LIMIT]:
        lines.append(f"| {r.method} | {_table_cell(r.path)} | {_table_cell(r.source)} |")
    if len(routes) > _ROUTE_TABLE_LIMIT:
        lines.append(f"- 共检测到 {len(routes)} 条路由，以上仅列前 {_ROUTE_TABLE_LIMIT} 条")
    return lines


def render_extra_context(profile: RepoProfile) -> str:
    """把 RepoProfile 渲染成中文 markdown 的 extra_context 文本。

    纯函数、确定性：不读文件、不跑命令、无随机；同一 profile 输出完全一致。
    """
    parts = [
        _tech_stack_lines(profile),
        _entry_lines(profile),
        _modules_lines(profile),
        _core_lines(profile),
        _tests_lines(profile),
        _notes_lines(profile),
    ]
    routes = _routes_lines(profile)
    if routes is not None:
        parts.append(routes)
    return "\n\n".join("\n".join(p) for p in parts)

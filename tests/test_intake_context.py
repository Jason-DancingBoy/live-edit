"""Tests for live_edit.intake.context — RepoProfile → extra_context 渲染器。"""

import sys
from dataclasses import replace

from live_edit.cli import _toml_multiline
from live_edit.intake.analyzer import DBInfo, FrontendInfo, ModuleInfo, RepoProfile, RouteInfo
from live_edit.intake.context import render_extra_context

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


def _build_extra_context(info: dict) -> str:
    """live-build 旧版「5 行技术栈 + 占位符」手写渲染器（仅用于对比测试）。"""
    lines = [
        "## 项目技术栈",
        f"- 语言: {info.get('language', 'unknown')}",
        f"- 框架: {info.get('framework') or '未检测到'}",
        f"- VCS: {info.get('vcs', 'none')}",
    ]
    if info.get("test_command"):
        lines.append(f"- 测试: {info['test_command']}")
    if info.get("frontend_build"):
        lines.append(
            f"- 前端构建: {info['frontend_build']}"
            "（编译型前端，修改前端源文件后必须运行此命令重新构建）"
        )
    lines.append("")
    lines.append("## 核心业务链路")
    lines.append("（请在此填写项目的核心业务链路：关键流程、模块划分、外部依赖等）")
    return "\n".join(lines)


def _full_profile() -> RepoProfile:
    """完整 FastAPI profile：有 routes/modules/db/protected_paths。"""
    return RepoProfile(
        name="demo",
        language="python",
        framework="fastapi",
        package_manager="pip",
        vcs="git",
        git_available=True,
        python_cmd=".venv/bin/python",
        app_module="main:app",
        port=8000,
        test_command=".venv/bin/python -m pytest tests -q --tb=short",
        health_url="http://127.0.0.1:8000/live-edit/health",
        frontend=FrontendInfo(kind="static", build_command=""),
        entry_points=["main.py"],
        modules=[
            ModuleInfo(path="config.py", kind="file", purpose="配置"),
            ModuleInfo(path="models/", kind="package", purpose="数据模型/序列化"),
            ModuleInfo(path="routers/", kind="package", purpose="HTTP 路由"),
            ModuleInfo(path="services/", kind="package", purpose="业务逻辑"),
        ],
        routes=[
            RouteInfo(method="GET", path="/", source="main.py:5"),
            RouteInfo(method="GET", path="/users", source="routers/user.py:12"),
            RouteInfo(method="POST", path="/users", source="routers/user.py:16"),
        ],
        db=DBInfo(
            kind="sqlalchemy",
            hint="SQLAlchemy 模型在 models/ 目录（或 models.py）",
            url_env="DATABASE_URL",
        ),
        protected_paths=[".env", ".env.*", "migrations/", "*.pem"],
        has_tests=True,
        test_dirs=["tests"],
    )


def _empty_profile() -> RepoProfile:
    """空 profile：db=None、routes=[]、modules=[]，无任何结构事实。"""
    return RepoProfile(
        name="empty",
        language="unknown",
        framework="",
        package_manager="",
        vcs="none",
        git_available=False,
        python_cmd="python3",
        app_module="",
        port=8083,
        test_command="",
        health_url="",
        frontend=None,
        entry_points=[],
        modules=[],
        routes=[],
        db=None,
        protected_paths=[],
        has_tests=False,
        test_dirs=[],
    )


class TestFullProfile:
    def test_all_section_headings_present(self):
        out = render_extra_context(_full_profile())
        for heading in [
            "## 项目技术栈",
            "## 入口与服务",
            "## 文件结构与模块地图",
            "## 核心业务链路",
            "## 测试",
            "## 注意事项与禁改目录",
            "## 关键路由表",
        ]:
            assert heading in out

    def test_tech_stack_facts(self):
        out = render_extra_context(_full_profile())
        assert "- 语言: python" in out
        assert "- 框架: fastapi" in out
        assert "- 包管理器: pip" in out
        assert "- 版本控制: git" in out
        assert "- 服务端口: 8000" in out
        assert "静态" in out

    def test_entry_and_service(self):
        out = render_extra_context(_full_profile())
        assert "- 入口文件: main.py" in out
        assert "- 应用模块: main:app" in out
        assert "uvicorn main:app" in out
        assert "若已安装 uvicorn" in out  # 启动方式措辞已软化
        assert "http://127.0.0.1:8000/live-edit/health" in out  # health_url 渲染进入口节

    def test_module_map(self):
        out = render_extra_context(_full_profile())
        assert "- routers/: HTTP 路由" in out
        assert "- models/: 数据模型/序列化" in out
        assert "- config.py: 配置" in out

    def test_core_chain_routes_and_db(self):
        out = render_extra_context(_full_profile())
        assert "检测到 3 个 HTTP 路由" in out
        assert "GET /users" in out
        assert "SQLAlchemy 模型在 models/" in out
        assert (
            "- 关键模块: models/（数据模型/序列化）、routers/（HTTP 路由）、services/（业务逻辑）"
            in out
        )

    def test_tests_and_notes(self):
        out = render_extra_context(_full_profile())
        assert "- 测试命令: .venv/bin/python -m pytest tests -q --tb=short" in out
        assert "- 测试目录: tests" in out
        assert "- .env：环境变量/密钥文件，禁止写入" in out
        assert "- *.pem：环境变量/密钥文件，禁止写入" in out
        assert "- migrations/：禁止修改" in out
        assert "- 数据库连接走 DATABASE_URL（.env 环境变量），禁止硬编码" in out

    def test_route_table(self):
        out = render_extra_context(_full_profile())
        table = out.split("## 关键路由表")[1]
        assert "| GET | /users | routers/user.py:12 |" in table
        assert "| POST | /users | routers/user.py:16 |" in table

    def test_no_triple_quotes(self):
        assert '"""' not in render_extra_context(_full_profile())


class TestEmptyProfile:
    def test_no_fabrication_and_todo_markers(self):
        out = render_extra_context(_empty_profile())
        assert "TODO: 补充核心业务链路（路由/模型/外部依赖）" in out
        assert "TODO: 项目未检测到测试，intake 会生成冒烟测试" in out
        assert "关键路由表" not in out
        assert "HTTP 路由" not in out
        assert "数据库" not in out

    def test_unknown_values_marked(self):
        out = render_extra_context(_empty_profile())
        assert "- 语言: 未检测到" in out
        assert "- 框架: 未检测到" in out
        assert "- 版本控制: 未检测到" in out
        assert "- 未检测到模块结构" in out
        assert "- 未检测到保护路径" in out


class TestDeterminism:
    def test_same_profile_same_output(self):
        profile = _full_profile()
        assert render_extra_context(profile) == render_extra_context(profile)

    def test_equal_profiles_equal_output(self):
        assert render_extra_context(_full_profile()) == render_extra_context(_full_profile())


class TestSpecialCharacters:
    def test_special_chars_pass_through(self):
        # 反斜杠/双引号/竖线都不做 TOML 层转义（交给写盘侧 _toml_multiline），
        # 渲染器原样保留；竖线只在路由表格单元格内转义为 \|
        profile = replace(
            _full_profile(),
            modules=[
                ModuleInfo(
                    path="utils\\win.py",
                    kind="file",
                    purpose='用途含"""三引号、`code`反引号、管道|分隔',
                )
            ],
        )
        out = render_extra_context(profile)
        assert "utils\\win.py" in out  # 反斜杠原样保留，不双重转义
        assert "`code`" in out
        assert "管道|分隔" in out  # 列表行竖线原样保留，不做全局替换
        assert '- utils\\win.py: 用途含"""三引号、`code`反引号、管道|分隔' in out

    def test_route_table_pipe_escaped_locally(self):
        profile = replace(
            _full_profile(),
            routes=[RouteInfo(method="GET", path="/users|admin", source="main.py:9")],
        )
        out = render_extra_context(profile)
        table = out.split("## 关键路由表")[1]
        assert "| GET | /users\\|admin | main.py:9 |" in table  # 仅表格单元格内转义
        # 列表行里不受影响：核心业务链路的路由样例保留原始 | 之外的原样路径
        assert "/users|admin" in out.split("## 关键路由表")[0]


class TestTomlRoundTrip:
    def test_round_trip_via_toml_multiline_is_lossless(self):
        # 含反斜杠、双引号、三引号、竖线的字段，经写盘侧等价的 _toml_multiline
        # 包裹进 """...""" 再 tomllib 读回，必须与渲染输出完全一致（无双重转义）
        profile = replace(
            _full_profile(),
            modules=[
                ModuleInfo(
                    path="utils\\win.py",
                    kind="file",
                    purpose='引号"三引号"""管道|与`反引号',
                )
            ],
            test_command='.venv\\bin\\python -m pytest "tests/test a.py" -q',
            routes=[RouteInfo(method="GET", path="/users|admin", source="main.py:5")],
        )
        out = render_extra_context(profile)
        wrapped = 'extra_context = """' + _toml_multiline(out) + '"""'
        parsed = tomllib.loads(wrapped)["extra_context"]
        assert parsed == out


class TestFrontendCompile:
    def test_build_command_note(self):
        profile = replace(
            _full_profile(),
            frontend=FrontendInfo(kind="compile", build_command="cd frontend && npm run build"),
        )
        out = render_extra_context(profile)
        assert "编译型" in out
        assert "cd frontend && npm run build" in out


class TestRouteLimit:
    def test_more_than_15_routes_truncated(self):
        routes = [RouteInfo(method="GET", path=f"/p{i}", source="main.py:1") for i in range(20)]
        profile = replace(_full_profile(), routes=routes)
        out = render_extra_context(profile)
        assert "共检测到 20 条路由，以上仅列前 15 条" in out
        # 表头 + 15 条数据行，分隔行不算
        data_rows = [
            ln for ln in out.splitlines() if ln.startswith("| ") and not ln.startswith("| ---")
        ]
        assert len(data_rows) == 16


class TestCompareLegacy:
    def test_renderer_has_much_more_info_than_legacy(self):
        profile = _full_profile()
        info = {
            "language": profile.language,
            "framework": profile.framework,
            "vcs": profile.vcs,
            "git_available": profile.git_available,
            "test_command": profile.test_command,
            "frontend_build": "",
        }
        legacy = _build_extra_context(info)
        new = render_extra_context(profile)
        assert "请在此填写" in legacy  # 旧版留了占位符
        assert "请在此填写" not in new  # 新版事实驱动，无占位符
        assert new.count("## ") >= legacy.count("## ") + 3  # 小节数显著更多
        assert len(new) > len(legacy) * 2  # 信息量显著更大

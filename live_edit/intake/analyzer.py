"""深度仓库分析器 — 确定性（无 LLM、无网络、只读）的文件系统扫描器。

供 `live-edit intake` 命令使用：把新仓库接入 live-edit 时的
`extra_context` / `[verify]` 配置生成从手写变成自动。

核心入口是 :func:`scan_project`，它复用 ``live_edit.config.detect_project``
的浅探测结果，在其上补充深度探测：python 解释器、入口/app_module、顶层模块地图、
静态路由、数据库层、前端形态、测试命令等。

确定性保证：所有遍历与排序都是稳定的，同一仓库两次扫描结果完全一致。
"""

from __future__ import annotations

import io
import json
import os
import re
import shlex
import shutil
import sys
import tokenize
from dataclasses import dataclass

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

from live_edit.config import detect_project

__all__ = [
    "ModuleInfo",
    "RouteInfo",
    "FrontendInfo",
    "DBInfo",
    "RepoProfile",
    "scan_project",
]


# ── 数据结构 ──


@dataclass
class ModuleInfo:
    """顶层模块地图中的一个条目（目录/包/单文件）。"""

    path: str  # 顶层相对路径，如 "routers/"、"models/"
    kind: str  # "package" | "directory" | "file"
    purpose: str  # 一行启发式职责描述，如 "HTTP 路由"、"数据模型"


@dataclass
class RouteInfo:
    """静态扫描到的 HTTP 路由。"""

    method: str  # GET/POST/PUT/DELETE...
    path: str  # 如 "/users"
    source: str  # "routers/user.py:12"


@dataclass
class FrontendInfo:
    """前端形态：static（static/ 直出，无构建）或 compile（frontend/ 编译型）。"""

    kind: str
    build_command: str  # compile 时如 "cd frontend && npm run build"；static 时 ""


@dataclass
class DBInfo:
    """数据库层启发式结论。只记录 .env 变量名，绝不读取/输出值。"""

    kind: str  # "sqlalchemy" | "alembic" | "none"
    hint: str  # 一行提示，如 "SQLAlchemy 模型在 models/ 目录"
    url_env: str  # .env 里探测到的 *_URL 变量名；没有则 ""


@dataclass
class RepoProfile:
    """一次 scan_project 的完整结果。"""

    name: str
    language: str
    framework: str
    package_manager: str
    vcs: str
    git_available: bool
    python_cmd: str
    app_module: str
    port: int
    test_command: str
    health_url: str
    frontend: FrontendInfo | None
    entry_points: list[str]
    modules: list[ModuleInfo]
    routes: list[RouteInfo]
    db: DBInfo | None
    protected_paths: list[str]
    has_tests: bool
    test_dirs: list[str]


# ── 常量与正则 ──


# 顶层模块地图 / 路由扫描中要排除的目录（外加所有点开头目录）
_EXCLUDED_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "dist",
        "build",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".coverage",
        ".eggs",
        ".tox",
        "tests",
    }
)

# HTTP 路由装饰器：
#   FastAPI 风格 @app.get("/users") / @router.post(f"/x/{id}")
#   Flask 风格   @app.route("/items", methods=["POST"])，缺省 methods 视为 GET
# 支持 f/r/b/u 字符串前缀；path 与 methods 均在装饰器同一行内匹配。
_ROUTE_RE = re.compile(
    r"@(?:app|router|api)\.(?P<deco>get|post|put|patch|delete|head|options|route)\s*\("
    r"\s*[rbfuRBFU]*(?P<quote>[\"'])(?P<path>[^\"']+)(?P=quote)"
    r"(?:\s*,\s*methods\s*=\s*\[(?P<methods>[^\]]*)\])?"
)

# Flask @app.route(..., methods=[...]) 里的 HTTP 方法名
_FLASK_METHOD_RE = re.compile(r"[\"']([A-Za-z]+)[\"']")

# Python 入口里的应用对象赋值：app = FastAPI() / app = Flask(__name__)
_APP_RE = re.compile(
    r"^\s*(?P<var>\w+)\s*=\s*(?:FastAPI|Flask|Starlette|Application)\s*\(", re.MULTILINE
)

# .env 里的数据库 URL 变量名（只匹配名字，不碰值）
_ENV_URL_RE = re.compile(r"^\s*(?:export\s+)?(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*=")

# 数据库相关标记：*.URL 变量名须命中其一才算数据库连接（避免 LIVE_EDIT_BASE_URL 之类误报）
_DB_URL_TOKENS = (
    "database",
    "postgres",
    "pgsql",
    "mysql",
    "mariadb",
    "mongodb",
    "mongo",
    "redis",
    "sqlite",
    "sqlserver",
    "neon",
    "supabase",
    "turso",
    "planetscale",
    "cockroach",
    "clickhouse",
    "aurora",
    "dynamodb",
    "couchdb",
    "neo4j",
    "memcached",
    "elasticsearch",
)

# 顶层目录职责启发式：命中即用对应描述（按顺序取首个）
_DIR_PURPOSE: tuple[tuple[tuple[str, ...], str], ...] = (
    (("router", "routes", "api"), "HTTP 路由"),
    (("model", "schema"), "数据模型/序列化"),
    (("service",), "业务逻辑"),
    (("migration",), "数据库迁移"),
    (("static",), "前端静态资源"),
    (("template",), "页面模板"),
    (("util", "helper", "common"), "通用工具"),
)

# 单文件用途（按文件名去扩展名）；未命中则直接用文件名主干
_FILE_PURPOSE = {
    "main": "应用入口",
    "app": "应用入口",
    "config": "配置",
    "models": "数据模型",
    "model": "数据模型",
    "schema": "序列化模式",
    "schemas": "序列化模式",
    "database": "数据库",
    "db": "数据库",
    "utils": "通用工具",
    "helpers": "通用工具",
    "auth": "认证",
    "security": "安全",
    "logging": "日志",
    "logger": "日志",
    "metrics": "指标监控",
    "dependencies": "依赖注入",
    "deps": "依赖注入",
    "constants": "常量定义",
    "errors": "错误处理",
    "exceptions": "错误处理",
    "cache": "缓存",
    "redis": "缓存/队列",
    "middleware": "中间件",
    "tasks": "后台任务",
    "background": "后台任务",
}

# 默认保护路径；若存在 secrets/ 目录再追加
_DEFAULT_PROTECTED_PATHS = [
    ".env",
    ".env.*",
    "node_modules/",
    "dist/",
    "build/",
    ".git/",
    "migrations/",
    "*.pem",
    "*.key",
]

# 路由扫描上限
_ROUTE_LIMIT = 30


# ── 基础工具 ──


def _is_excluded_dir(name: str) -> bool:
    """模块地图/遍历中应跳过的目录：排除集内的，或点开头的隐藏目录。"""
    return name.startswith(".") or name in _EXCLUDED_DIRS


def _read_text(path: str) -> str:
    """只读读取文本文件（UTF-8 容错），任何异常都返回空串。"""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception:
        return ""


def _resolve_python_cmd(root: str) -> str:
    """解析可用 python 解释器。

    优先仓库内虚拟环境：必须校验 .venv/bin/python、venv/bin/python 真实存在
    （os.path.isfile），存在则返回绝对路径（os.path.join(root, rel)），保证
    生成的 test_command 换 CWD 后仍可执行；都没有再回退到 PATH 上的
    python3/python 裸名。全程不跑子进程，保持确定性。
    """
    for rel in (".venv/bin/python", "venv/bin/python"):
        full = os.path.join(root, rel)
        if os.path.isfile(full):
            return full
    for name in ("python3", "python"):
        if shutil.which(name):
            return name
    return "python3"  # 兜底：都找不到时给裸名


def _resolve_port(root: str, info: dict) -> int:
    """回填服务端口：live-edit 的 detect_project 不产出 port 字段。

    live-edit 的 detect_project 缺 live-build 的 port 探测（fastapi 入口 → 8000、
    node → 5173），这里按等价规则补上；若 detect_project 未来已给出 port 则直接用。
    """
    port = info.get("port")
    if port is not None:
        return int(port)
    language = info.get("language")
    if (
        language == "python"
        and info.get("framework") == "fastapi"
        and (
            os.path.isfile(os.path.join(root, "backend", "main.py"))
            or os.path.isfile(os.path.join(root, "main.py"))
        )
    ):
        return 8000
    if language == "typescript":
        return 5173
    return 8083


# ── 入口 + app_module ──


def _detect_entry_points(root: str) -> tuple[str, list[str]]:
    """探测入口文件并推导 app_module（如 "main:app" / "backend.main:app"）。"""
    candidates = [
        ("main.py", "main"),
        ("backend/main.py", "backend.main"),
        ("app.py", "app"),
    ]
    entry_points: list[str] = []
    app_module = ""
    for rel, module_name in candidates:
        if not os.path.isfile(os.path.join(root, rel)):
            continue
        entry_points.append(rel)
        if not app_module:
            m = _APP_RE.search(_read_text(os.path.join(root, rel)))
            if m:
                app_module = f"{module_name}:{m.group('var')}"
    return app_module, entry_points


# ── 顶层模块地图 ──


def _dir_purpose(name: str, is_package: bool) -> str:
    """目录/包的一行启发式职责描述。"""
    lowered = name.lower()
    for keywords, purpose in _DIR_PURPOSE:
        if any(k in lowered for k in keywords):
            return purpose
    if is_package:
        return "Python 包"
    return "未分类"


def _file_purpose(stem: str) -> str:
    """单文件用途：按文件名主干映射，未命中直接用主干。"""
    return _FILE_PURPOSE.get(stem, stem)


def _build_module_map(root: str) -> list[ModuleInfo]:
    """构建顶层模块地图（一层目录 + 根级 .py 文件），排除 venv/依赖/构建产物等。"""
    modules: list[ModuleInfo] = []
    try:
        entries = sorted(os.listdir(root))
    except Exception:
        return modules
    for name in entries:
        full = os.path.join(root, name)
        if _is_excluded_dir(name):
            continue
        if os.path.isdir(full):
            is_package = os.path.isfile(os.path.join(full, "__init__.py"))
            kind = "package" if is_package else "directory"
            modules.append(
                ModuleInfo(path=name + "/", kind=kind, purpose=_dir_purpose(name, is_package))
            )
        elif os.path.isfile(full) and name.endswith(".py"):
            modules.append(ModuleInfo(path=name, kind="file", purpose=_file_purpose(name[:-3])))
    return modules


# ── 静态路由扫描 ──


def _string_comment_spans(content: str) -> dict[int, list[tuple[int, int]]]:
    """返回 {行号: [(起始列, 结束列), ...]} —— 字符串/注释覆盖的列区间。

    用 tokenize 精确定位字符串字面量（含三引号 docstring）与注释的覆盖范围，
    供过滤 docstring/注释里的假装饰器。分词失败（如未闭合字符串）时退化为空，
    仅靠“装饰器必须是逻辑行首”约束兜底。
    """
    covered: dict[int, list[tuple[int, int]]] = {}
    try:
        tokens = tokenize.generate_tokens(io.StringIO(content).readline)
        for tok in tokens:
            if tok.type not in (tokenize.STRING, tokenize.COMMENT):
                continue
            start_row, start_col = tok.start
            end_row, end_col = tok.end
            if start_row == end_row:
                covered.setdefault(start_row, []).append((start_col, end_col))
            else:
                # 多行字符串：起始行覆盖到行尾，末尾行覆盖到字符串结尾，中间整行覆盖
                for row in range(start_row, end_row + 1):
                    if row == start_row:
                        covered.setdefault(row, []).append((start_col, 10**9))
                    elif row == end_row:
                        covered.setdefault(row, []).append((0, end_col))
                    else:
                        covered.setdefault(row, []).append((0, 10**9))
    except (tokenize.TokenError, IndentationError, SyntaxError, ValueError):
        pass
    return covered


def _flask_methods(methods_str: str | None) -> list[str]:
    """解析 Flask @app.route(..., methods=[...]) 的 HTTP 方法列表。

    缺省或解析不出时按 GET 处理（Flask 缺省即 GET）。
    """
    if not methods_str:
        return ["GET"]
    methods = [m.upper() for m in _FLASK_METHOD_RE.findall(methods_str)]
    return methods or ["GET"]


def _extract_routes(content: str, rel: str) -> list[RouteInfo]:
    """从单个文件内容提取真实路由，过滤注释与字符串里的假装饰器。"""
    covered = _string_comment_spans(content)
    found: list[RouteInfo] = []
    for lineno, line in enumerate(content.splitlines(), start=1):
        # 装饰器必须是逻辑行首（忽略缩进）；注释行/行内注释里的 @ 不可能是装饰器
        if not line.lstrip().startswith("@"):
            continue
        spans = covered.get(lineno)
        for m in _ROUTE_RE.finditer(line):
            col = m.start()
            if spans and any(s <= col < e for s, e in spans):
                continue
            if m.group("deco") == "route":
                for method in _flask_methods(m.group("methods")):
                    found.append(
                        RouteInfo(method=method, path=m.group("path"), source=f"{rel}:{lineno}")
                    )
            else:
                found.append(
                    RouteInfo(
                        method=m.group("deco").upper(),
                        path=m.group("path"),
                        source=f"{rel}:{lineno}",
                    )
                )
    return found


def _scan_routes(root: str) -> list[RouteInfo]:
    """静态扫描 HTTP 路由（尽力而为，绝不因单个文件异常而崩溃）。"""
    routes: list[RouteInfo] = []
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if not _is_excluded_dir(d))
            for fn in sorted(filenames):
                if not fn.endswith(".py"):
                    continue
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, root).replace(os.sep, "/")
                content = _read_text(full)
                if not content:
                    continue
                routes.extend(_extract_routes(content, rel))
    except Exception:
        return []
    routes.sort(key=lambda r: (r.method, r.path, r.source))
    return routes[:_ROUTE_LIMIT]


# ── 数据库层 ──


def _scan_env_url_var(root: str) -> str:
    """在 .env 里探测数据库 *_URL / *_URI 变量名（只记录名字，绝不读取值）。"""
    env_path = os.path.join(root, ".env")
    if not os.path.isfile(env_path):
        return ""
    for line in _read_text(env_path).splitlines():
        m = _ENV_URL_RE.match(line.strip())
        if not m:
            continue
        name = m.group("name")
        lowered = name.lower()
        if (lowered.endswith("_url") or lowered.endswith("_uri")) and any(
            tok in lowered for tok in _DB_URL_TOKENS
        ):
            return name
    return ""


def _detect_db(root: str) -> DBInfo | None:
    """探测数据库层：alembic / sqlalchemy / 无，并记录 .env 里的 *_URL 变量名。

    没有任何数据库信号（无 alembic、无 models、无 *_URL 环境变量）时返回 None。
    """
    has_alembic = os.path.isfile(os.path.join(root, "alembic.ini")) or os.path.isdir(
        os.path.join(root, "migrations")
    )
    has_models = os.path.isdir(os.path.join(root, "models")) or os.path.isfile(
        os.path.join(root, "models.py")
    )
    url_env = _scan_env_url_var(root)

    if has_alembic:
        kind = "alembic"
    elif has_models:
        kind = "sqlalchemy"
    else:
        kind = "none"

    if kind == "none" and not url_env:
        return None

    hint = {
        "alembic": "alembic 迁移在 migrations/，配置在 alembic.ini",
        "sqlalchemy": "SQLAlchemy 模型在 models/ 目录（或 models.py）",
        "none": "未检测到 ORM/迁移层",
    }[kind]
    return DBInfo(kind=kind, hint=hint, url_env=url_env)


# ── 保护路径 / 前端 / 测试 ──


def _build_protected_paths(root: str) -> list[str]:
    """默认保护路径；存在 secrets/ 目录时追加。"""
    paths = list(_DEFAULT_PROTECTED_PATHS)
    if os.path.isdir(os.path.join(root, "secrets")):
        paths.append("secrets/")
    return paths


def _detect_frontend(root: str) -> FrontendInfo | None:
    """探测前端形态：frontend/package.json → compile；static/ → static。"""
    if os.path.isfile(os.path.join(root, "frontend", "package.json")):
        return FrontendInfo(kind="compile", build_command="cd frontend && npm run build")
    if os.path.isdir(os.path.join(root, "static")):
        return FrontendInfo(kind="static", build_command="")
    return None


def _detect_tests(root: str) -> tuple[list[str], bool]:
    """探测测试目录/文件（tests/、test/、test_*.py、*_test.py）。

    has_tests 必须至少命中一个测试文件才算 True——空的 tests/ 目录不构成可运行的测试。
    """
    collected: list[str] = []
    test_file_count = 0
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            rel_dir = os.path.relpath(dirpath, root)
            for dn in sorted(dirnames):
                if dn in ("tests", "test"):
                    p = dn if rel_dir == "." else os.path.join(rel_dir, dn).replace(os.sep, "/")
                    collected.append(p)
            for fn in sorted(filenames):
                if fn.endswith(".py") and (fn.startswith("test_") or fn.endswith("_test.py")):
                    p = fn if rel_dir == "." else os.path.join(rel_dir, fn).replace(os.sep, "/")
                    collected.append(p)
                    test_file_count += 1
            # 下钻：排除重型/隐藏目录，但保留 tests/、test/ 目录本身以便统计其内测试文件
            excluded = _EXCLUDED_DIRS - {"tests"}
            dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in excluded)
    except Exception:
        pass

    # 测试目录本身保留；测试目录内的测试文件不重复计数
    dirs = {item for item in collected if not item.endswith(".py")}
    test_dirs: list[str] = []
    for item in collected:
        if item.endswith(".py") and os.path.dirname(item) in dirs:
            continue
        test_dirs.append(item)
    return test_dirs, test_file_count > 0


# ── 测试命令 / 包管理器 ──


def _build_test_command(root: str, info: dict, python_cmd: str) -> str:
    """生成可运行的测试命令。

    python：自己复刻 detect_project 的 testpaths 解析逻辑（读 pyproject.toml 的
    tool.pytest.ini_options.testpaths → pytest.ini/tox.ini 兜底），但解释器换成
    解析出的 python_cmd —— 含 .venv 的项目必须产出 `.venv/bin/python -m pytest ...`，
    不能退化成裸 `python`。detect_project 也产 test_command，那是给
    generate_default_config/init 用的；analyzer 用自己的（与 live-build 一致）。
    node：live-edit 的 detect_project 不产出 test_command，直接读 package.json
    的 test script 兜底。探测不到留空。

    所有仓库派生的插值（testpaths、python_cmd）一律 shlex.quote —— 命令可能被
    shell=True 执行，pyproject.toml 来自克隆仓库可能被篡改，未 quote 的
    ``$(...)``/反引号可注入。
    """
    language = info.get("language", "unknown")
    if language == "python":
        pyproject = os.path.join(root, "pyproject.toml")
        if os.path.isfile(pyproject):
            try:
                with open(pyproject, "rb") as f:
                    data = tomllib.load(f)
                testpaths = (
                    data.get("tool", {})
                    .get("pytest", {})
                    .get("ini_options", {})
                    .get("testpaths", [])
                )
                if testpaths:
                    joined = " ".join(shlex.quote(p) for p in testpaths[:3])
                    return f"{shlex.quote(python_cmd)} -m pytest {joined} -q --tb=short"
            except Exception:
                pass
        if os.path.isfile(os.path.join(root, "pytest.ini")) or os.path.isfile(
            os.path.join(root, "tox.ini")
        ):
            return f"{shlex.quote(python_cmd)} -m pytest -q --tb=short"
        return ""
    if language == "typescript":
        cmd = info.get("test_command")
        if cmd:
            return str(cmd)
        pkg_path = os.path.join(root, "package.json")
        if os.path.isfile(pkg_path):
            try:
                with open(pkg_path, encoding="utf-8", errors="replace") as f:
                    data = json.load(f)
                return str((data.get("scripts") or {}).get("test", ""))
            except Exception:
                return ""
        return ""
    return ""


def _detect_package_manager(root: str, info: dict) -> str:
    """探测包管理器：poetry/uv/pip、pnpm/yarn/npm、go。"""
    language = info.get("language", "unknown")
    if language == "python":
        pyproject = os.path.join(root, "pyproject.toml")
        if os.path.isfile(pyproject):
            data = {}
            try:
                with open(pyproject, "rb") as f:
                    data = tomllib.load(f)
            except Exception:
                pass
            if "poetry" in data.get("tool", {}):
                return "poetry"
            if "uv" in data.get("tool", {}):
                return "uv"
            return "pip"
        return "pip"
    if language == "typescript":
        if os.path.isfile(os.path.join(root, "pnpm-lock.yaml")):
            return "pnpm"
        if os.path.isfile(os.path.join(root, "yarn.lock")):
            return "yarn"
        return "npm"
    if language == "go":
        return "go"
    return ""


# ── Node 框架补探测 ──


def _detect_node_framework(root: str) -> str:
    """node 项目补探测框架：next / vite / express（detect_project 未覆盖）。"""
    pkg_path = os.path.join(root, "package.json")
    if not os.path.isfile(pkg_path):
        return ""
    data: dict = {}
    try:
        with open(pkg_path, encoding="utf-8", errors="replace") as f:
            data = json.load(f)
    except Exception:
        return ""
    deps = dict(data.get("dependencies") or {})
    deps.update(data.get("devDependencies") or {})
    for key in ("next", "vite", "express"):
        if key in deps:
            return key
    return ""


# ── 主入口 ──


def scan_project(root: str) -> RepoProfile:
    """扫描仓库根目录，返回深度分析后的 RepoProfile。

    确定性、只读、无 LLM、无网络；复用 detect_project 的浅探测结果。
    """
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        return RepoProfile(
            name=os.path.basename(root) or "unknown",
            language="unknown",
            framework="",
            package_manager="",
            vcs="none",
            git_available=False,
            python_cmd=_resolve_python_cmd(root),
            app_module="",
            port=8083,
            test_command="",
            health_url="",
            frontend=None,
            entry_points=[],
            modules=[],
            routes=[],
            db=None,
            protected_paths=_build_protected_paths(root),
            has_tests=False,
            test_dirs=[],
        )

    info = detect_project(root)

    language = info.get("language", "unknown")
    framework = info.get("framework", "")
    if not framework and language == "go":
        framework = "go"
    elif not framework and language == "typescript":
        framework = _detect_node_framework(root)

    python_cmd = _resolve_python_cmd(root)
    app_module, entry_points = _detect_entry_points(root)
    modules = _build_module_map(root)
    routes = _scan_routes(root)
    frontend = _detect_frontend(root)
    db = _detect_db(root)
    protected_paths = _build_protected_paths(root)
    test_command = _build_test_command(root, info, python_cmd)
    test_dirs, has_tests = _detect_tests(root)

    port = _resolve_port(root, info)
    health_url = f"http://127.0.0.1:{port}/live-edit/health" if app_module else ""

    return RepoProfile(
        name=info.get("name", os.path.basename(root) or "unknown"),
        language=language,
        framework=framework,
        package_manager=_detect_package_manager(root, info),
        vcs=info.get("vcs", "none"),
        git_available=bool(info.get("git_available", False)),
        python_cmd=python_cmd,
        app_module=app_module,
        port=port,
        test_command=test_command,
        health_url=health_url,
        frontend=frontend,
        entry_points=entry_points,
        modules=modules,
        routes=routes,
        db=db,
        protected_paths=protected_paths,
        has_tests=has_tests,
        test_dirs=test_dirs,
    )

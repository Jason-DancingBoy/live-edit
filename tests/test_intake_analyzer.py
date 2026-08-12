"""Tests for live_edit.intake.analyzer — deterministic, offline repo scanning."""

import json
import shlex
from dataclasses import asdict

from live_edit.intake.analyzer import RouteInfo, scan_project

MAIN_PY = (
    "from fastapi import FastAPI\n"
    "\n"
    "app = FastAPI()\n"
    "\n"
    '@app.get("/")\n'
    "def root():\n"
    '    return {"ok": True}\n'
    "\n"
    '@app.get("/users")\n'
    "def list_users():\n"
    '    return {"users": []}\n'
    "\n"
    '@app.post("/users")\n'
    "def create_user():\n"
    '    return {"ok": True}\n'
)


class TestPythonFastAPI:
    def _fastapi_project(self, tmp_path):
        (tmp_path / "main.py").write_text(MAIN_PY)
        (tmp_path / "pyproject.toml").write_text(
            "[project]\nname = 'demo'\ndependencies = ['fastapi']\n\n"
            "[tool.pytest.ini_options]\ntestpaths = ['tests']\n"
        )
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_health.py").write_text("def test_ok():\n    assert True\n")
        venv_py = tmp_path / ".venv" / "bin" / "python"
        venv_py.parent.mkdir(parents=True)
        venv_py.touch()  # 模拟真实虚拟环境解释器文件

    def test_full_profile(self, tmp_path):
        self._fastapi_project(tmp_path)
        p = scan_project(str(tmp_path))
        assert p.language == "python"
        assert p.framework == "fastapi"
        assert p.app_module == "main:app"
        assert p.entry_points == ["main.py"]
        assert p.python_cmd == str(tmp_path / ".venv" / "bin" / "python")  # 绝对路径
        # analyzer 用自己的 testpaths 解析 + 解析出的 venv python_cmd，shlex.quote 后拼接；
        # 用 shlex.split 往返拆解断言，兼容单引号、不写死双引号
        parts = shlex.split(p.test_command)
        assert parts[0] == str(tmp_path / ".venv" / "bin" / "python")  # venv 解析生效
        assert parts[1:3] == ["-m", "pytest"]
        assert "tests" in parts
        assert parts[-1] == "--tb=short"
        assert p.package_manager == "pip"
        assert p.has_tests is True
        assert p.test_dirs == ["tests"]
        assert p.port == 8000
        assert p.health_url == "http://127.0.0.1:8000/live-edit/health"
        assert p.vcs == "none"
        assert p.git_available is False
        assert ".env" in p.protected_paths

    def test_routes_parsed_and_sorted(self, tmp_path):
        self._fastapi_project(tmp_path)
        p = scan_project(str(tmp_path))
        assert p.routes == [
            RouteInfo(method="GET", path="/", source="main.py:5"),
            RouteInfo(method="GET", path="/users", source="main.py:9"),
            RouteInfo(method="POST", path="/users", source="main.py:13"),
        ]

    def test_module_map(self, tmp_path):
        (tmp_path / "models").mkdir()
        (tmp_path / "routers").mkdir()
        (tmp_path / "services").mkdir()
        (tmp_path / "utils").mkdir()
        (tmp_path / "config.py").write_text("X = 1\n")
        (tmp_path / ".venv").mkdir()
        p = scan_project(str(tmp_path))
        by_path = {m.path: m for m in p.modules}
        assert by_path["models/"].purpose == "数据模型/序列化"
        assert by_path["routers/"].purpose == "HTTP 路由"
        assert by_path["services/"].purpose == "业务逻辑"
        assert by_path["utils/"].purpose == "通用工具"
        assert by_path["config.py"].purpose == "配置"
        assert by_path["config.py"].kind == "file"
        assert ".venv" not in by_path  # 隐藏/虚拟环境目录被排除

    def test_route_in_subdir(self, tmp_path):
        (tmp_path / "routers").mkdir()
        (tmp_path / "routers" / "user.py").write_text(
            "from fastapi import APIRouter\n"
            "router = APIRouter()\n"
            '@router.get("/users")\n'
            "def list_users():\n"
            "    return {}\n"
        )
        p = scan_project(str(tmp_path))
        assert p.routes == [RouteInfo(method="GET", path="/users", source="routers/user.py:3")]

    def test_routes_capped_at_30(self, tmp_path):
        body = ["from fastapi import FastAPI", "app = FastAPI()"]
        for i in range(40):
            body.append(f'@app.get("/p{i}")')
            body.append(f"def p{i}():\n    return {{}}\n")
        (tmp_path / "main.py").write_text("\n".join(body) + "\n")
        p = scan_project(str(tmp_path))
        assert len(p.routes) == 30

    def test_comment_and_docstring_fake_routes_ignored(self, tmp_path):
        (tmp_path / "main.py").write_text(
            "from fastapi import FastAPI\n"
            "app = FastAPI()\n"
            '# @app.get("/fake-comment")\n'
            '"""\n'
            '@app.get("/fake-docstring")\n'
            '"""\n'
            '@app.get("/real")\n'
            "def real():\n"
            "    return {}\n"
        )
        p = scan_project(str(tmp_path))
        assert p.routes == [RouteInfo(method="GET", path="/real", source="main.py:7")]

    def test_application_varname_entry(self, tmp_path):
        (tmp_path / "main.py").write_text("from fastapi import FastAPI\napplication = FastAPI()\n")
        (tmp_path / "pyproject.toml").write_text(
            "[project]\nname='demo'\ndependencies=['fastapi']\n"
        )
        p = scan_project(str(tmp_path))
        assert p.app_module == "main:application"

    def test_empty_tests_dir_has_tests_false(self, tmp_path):
        (tmp_path / "tests").mkdir()
        p = scan_project(str(tmp_path))
        assert p.has_tests is False


class TestNodeProject:
    def _node_project(self, tmp_path):
        (tmp_path / "package.json").write_text(
            json.dumps(
                {
                    "name": "web-app",
                    "scripts": {"test": "vitest run", "build": "vite build"},
                    "devDependencies": {"vite": "^5.0.0"},
                }
            )
        )
        frontend = tmp_path / "frontend"
        frontend.mkdir()
        (frontend / "package.json").write_text(
            json.dumps({"name": "web-app-frontend", "scripts": {"build": "vite build"}})
        )

    def test_profile(self, tmp_path):
        self._node_project(tmp_path)
        p = scan_project(str(tmp_path))
        assert p.language == "typescript"
        assert p.framework == "vite"
        assert p.package_manager == "npm"
        assert p.test_command == "vitest run"
        assert p.port == 5173
        assert p.app_module == ""
        assert p.health_url == ""
        assert p.frontend is not None
        assert p.frontend.kind == "compile"
        assert p.frontend.build_command == "cd frontend && npm run build"


class TestFlaskRoutes:
    def test_flask_route_default_and_methods(self, tmp_path):
        (tmp_path / "app.py").write_text(
            "from flask import Flask\n"
            "app = Flask(__name__)\n"
            '@app.route("/")\n'
            "def index():\n"
            "    return 'hi'\n"
            '@app.route("/items", methods=["POST"])\n'
            "def create_item():\n"
            "    return 'ok'\n"
        )
        p = scan_project(str(tmp_path))
        assert p.routes == [
            RouteInfo(method="GET", path="/", source="app.py:3"),
            RouteInfo(method="POST", path="/items", source="app.py:6"),
        ]
        assert p.app_module == "app:app"

    def test_flask_route_multiple_methods(self, tmp_path):
        (tmp_path / "app.py").write_text(
            "from flask import Flask\n"
            "app = Flask(__name__)\n"
            '@app.route("/x", methods=["GET", "DELETE"])\n'
            "def x():\n"
            "    return 'ok'\n"
        )
        p = scan_project(str(tmp_path))
        assert [(r.method, r.path) for r in p.routes] == [
            ("DELETE", "/x"),
            ("GET", "/x"),
        ]


class TestMinimalProject:
    def test_minimal(self, tmp_path):
        (tmp_path / "hello.py").write_text("print('hello')\n")
        p = scan_project(str(tmp_path))
        assert p.language == "unknown"
        assert p.framework == ""
        assert p.app_module == ""
        assert p.test_command == ""
        assert p.health_url == ""
        assert p.has_tests is False
        assert p.test_dirs == []
        assert p.frontend is None
        assert p.db is None
        assert p.python_cmd in ("python3", "python")  # 无 venv，回退到裸命令名
        assert isinstance(p.modules, list)
        assert p.package_manager == ""

    def test_empty_or_broken_venv_falls_back(self, tmp_path):
        (tmp_path / ".venv").mkdir()  # 空的/损坏的虚拟环境目录：无 bin/python 文件
        p = scan_project(str(tmp_path))
        assert p.python_cmd in ("python3", "python")


class TestDeterminism:
    def test_two_scans_equal(self, tmp_path):
        (tmp_path / "main.py").write_text(MAIN_PY)
        (tmp_path / "routers").mkdir()
        (tmp_path / "routers" / "user.py").write_text(
            "from fastapi import APIRouter\n"
            "router = APIRouter()\n"
            '@router.post("/users")\n'
            "def create_user():\n"
            "    return {}\n"
        )
        (tmp_path / "services").mkdir()
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n")
        p1 = scan_project(str(tmp_path))
        p2 = scan_project(str(tmp_path))
        assert asdict(p1) == asdict(p2)


class TestDB:
    def test_no_env_db_is_none(self, tmp_path):
        (tmp_path / "main.py").write_text("x = 1\n")
        p = scan_project(str(tmp_path))
        assert p.db is None

    def test_env_url_detected_name_only(self, tmp_path):
        (tmp_path / ".env").write_text("DATABASE_URL=postgres://user:pass@localhost/db\n")
        p = scan_project(str(tmp_path))
        assert p.db is not None
        assert p.db.url_env == "DATABASE_URL"
        # 绝不输出 .env 里的值
        assert "postgres://" not in str(asdict(p))

    def test_non_db_url_env_ignored(self, tmp_path):
        (tmp_path / ".env").write_text("LIVE_EDIT_BASE_URL=http://localhost:8083\n")
        p = scan_project(str(tmp_path))
        assert p.db is None  # 非数据库 *_URL 不应被当作数据库连接变量

    def test_uri_suffix_env_detected(self, tmp_path):
        (tmp_path / ".env").write_text("MONGODB_URI=mongodb://localhost/db\n")
        p = scan_project(str(tmp_path))
        assert p.db is not None
        assert p.db.url_env == "MONGODB_URI"
        assert "mongodb://" not in str(asdict(p))  # 绝不输出 .env 里的值

    def test_export_prefix_env_detected(self, tmp_path):
        (tmp_path / ".env").write_text("export REDIS_URI=redis://localhost:6379/0\n")
        p = scan_project(str(tmp_path))
        assert p.db is not None
        assert p.db.url_env == "REDIS_URI"

    def test_sqlalchemy_models(self, tmp_path):
        (tmp_path / "models").mkdir()
        (tmp_path / "models" / "user.py").write_text("class User:\n    pass\n")
        p = scan_project(str(tmp_path))
        assert p.db is not None
        assert p.db.kind == "sqlalchemy"
        assert p.db.url_env == ""

    def test_alembic_migrations(self, tmp_path):
        (tmp_path / "migrations").mkdir()
        (tmp_path / "alembic.ini").write_text("[alembic]\n")
        p = scan_project(str(tmp_path))
        assert p.db is not None
        assert p.db.kind == "alembic"

    def test_models_take_precedence_only_when_no_alembic(self, tmp_path):
        (tmp_path / "models").mkdir()
        (tmp_path / "models" / "user.py").write_text("class User:\n    pass\n")
        (tmp_path / "migrations").mkdir()
        (tmp_path / "alembic.ini").write_text("[alembic]\n")
        p = scan_project(str(tmp_path))
        assert p.db.kind == "alembic"


class TestProtectedPaths:
    def test_defaults(self, tmp_path):
        (tmp_path / "main.py").write_text("x = 1\n")
        p = scan_project(str(tmp_path))
        assert ".env" in p.protected_paths
        assert ".env.*" in p.protected_paths
        assert "*.pem" in p.protected_paths

    def test_secrets_dir_appended(self, tmp_path):
        (tmp_path / "secrets").mkdir()
        p = scan_project(str(tmp_path))
        assert "secrets/" in p.protected_paths


class TestPackageManager:
    def test_poetry(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[tool.poetry]\nname = 'x'\n")
        p = scan_project(str(tmp_path))
        assert p.package_manager == "poetry"

    def test_uv(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[tool.uv]\nname = 'x'\n")
        p = scan_project(str(tmp_path))
        assert p.package_manager == "uv"

    def test_pnpm(self, tmp_path):
        (tmp_path / "package.json").write_text('{"name": "x"}')
        (tmp_path / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n")
        p = scan_project(str(tmp_path))
        assert p.package_manager == "pnpm"

    def test_backend_main_entry(self, tmp_path):
        (tmp_path / "backend").mkdir()
        (tmp_path / "backend" / "main.py").write_text(
            "from fastapi import FastAPI\napp = FastAPI()\n"
        )
        (tmp_path / "pyproject.toml").write_text(
            "[project]\nname='demo'\ndependencies=['fastapi']\n"
        )
        p = scan_project(str(tmp_path))
        assert p.app_module == "backend.main:app"
        assert p.entry_points == ["backend/main.py"]
        assert p.port == 8000

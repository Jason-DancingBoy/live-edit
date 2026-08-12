"""Tests for live_edit.intake.verify_provision — verify config + smoke test provisioning."""

from live_edit.intake.analyzer import RepoProfile
from live_edit.intake.verify_provision import SmokeTest, provision_verify

SMOKE_PATH = "tests/test_smoke.py"
PY_CMD = "/abs/.venv/bin/python"


def _profile(**overrides) -> RepoProfile:
    """构造一个带默认值的 RepoProfile fixture（纯内存，不触碰文件系统）。"""
    fields = {
        "name": "demo",
        "language": "python",
        "framework": "fastapi",
        "package_manager": "pip",
        "vcs": "none",
        "git_available": False,
        "python_cmd": PY_CMD,
        "app_module": "",
        "port": 8000,
        "test_command": "",
        "health_url": "http://127.0.0.1:8000/live-edit/health",
        "frontend": None,
        "entry_points": [],
        "modules": [],
        "routes": [],
        "db": None,
        "protected_paths": [".env"],
        "has_tests": False,
        "test_dirs": [],
    }
    fields.update(overrides)
    return RepoProfile(**fields)


class TestHasTests:
    def test_adopts_existing_test_command(self):
        cmd = f"{PY_CMD} -m pytest tests -q --tb=short"
        prov = provision_verify(_profile(has_tests=True, test_command=cmd, app_module="main:app"))
        assert prov.test_command == cmd
        assert prov.smoke_test is None
        assert prov.needs_confirmation is False
        assert prov.health_url == "http://127.0.0.1:8000/live-edit/health"

    def test_empty_test_command_falls_back_to_test_dirs(self):
        prov = provision_verify(
            _profile(
                has_tests=True, test_command="", test_dirs=["tests/unit"], app_module="main:app"
            )
        )
        assert prov.test_command == f"{PY_CMD} -m pytest tests/unit -q --tb=short"
        assert prov.smoke_test is None

    def test_empty_test_command_falls_back_to_tests_default(self):
        prov = provision_verify(_profile(has_tests=True, test_command="", test_dirs=[]))
        assert prov.test_command == f"{PY_CMD} -m pytest tests -q --tb=short"
        assert prov.smoke_test is None


class TestNoTestsPython:
    def test_smoke_test_generated(self):
        prov = provision_verify(
            _profile(has_tests=False, app_module="backend.main:app", python_cmd=PY_CMD)
        )
        assert prov.smoke_test is not None
        assert isinstance(prov.smoke_test, SmokeTest)
        assert prov.smoke_test.path == SMOKE_PATH
        assert prov.smoke_test.content.startswith("# 由 live-edit intake 自动生成")
        assert "from backend.main import app" in prov.smoke_test.content
        assert "def test_app_imports()" in prov.smoke_test.content
        assert "assert app is not None" in prov.smoke_test.content
        assert "TestClient" not in prov.smoke_test.content
        assert prov.needs_confirmation is True
        assert prov.test_command == f"{PY_CMD} -m pytest {SMOKE_PATH} -q --tb=short"

    def test_empty_app_module_degraded_skeleton(self):
        prov = provision_verify(_profile(has_tests=False, app_module="", python_cmd=PY_CMD))
        assert prov.smoke_test is not None
        assert prov.smoke_test.path == SMOKE_PATH
        assert "import sys" in prov.smoke_test.content
        assert "待补充" in prov.smoke_test.content
        assert "def test_smoke()" in prov.smoke_test.content
        assert "sys.version_info >= (3, 8)" in prov.smoke_test.content
        assert prov.needs_confirmation is True
        assert prov.test_command == f"{PY_CMD} -m pytest {SMOKE_PATH} -q --tb=short"


class TestAppModuleVarName:
    def test_default_var_app(self):
        prov = provision_verify(_profile(has_tests=False, app_module="main:app"))
        assert prov.smoke_test is not None
        assert "from main import app" in prov.smoke_test.content
        assert "assert app is not None" in prov.smoke_test.content

    def test_non_standard_var_name_preserved(self):
        prov = provision_verify(_profile(has_tests=False, app_module="backend.main:application"))
        assert prov.smoke_test is not None
        assert "from backend.main import application" in prov.smoke_test.content
        assert "assert application is not None" in prov.smoke_test.content

    def test_module_without_colon_defaults_to_app(self):
        prov = provision_verify(_profile(has_tests=False, app_module="main"))
        assert prov.smoke_test is not None
        assert "from main import app" in prov.smoke_test.content


class TestNonPython:
    def test_node_no_smoke_test_command_empty(self):
        prov = provision_verify(
            _profile(language="typescript", framework="vite", app_module="", has_tests=False)
        )
        assert prov.smoke_test is None
        assert prov.needs_confirmation is False
        assert prov.test_command == ""

    def test_non_python_has_tests_empty_command_no_pytest_fallback(self):
        prov = provision_verify(
            _profile(
                language="typescript",
                framework="vite",
                has_tests=True,
                test_command="",
                test_dirs=["tests"],
            )
        )
        assert prov.test_command == ""
        assert prov.smoke_test is None
        assert prov.needs_confirmation is False


class TestEmptyPythonCmd:
    def test_no_bad_command_when_python_cmd_empty(self):
        prov = provision_verify(
            _profile(has_tests=False, app_module="backend.main:app", python_cmd="")
        )
        assert prov.smoke_test is not None
        assert prov.test_command == ""
        assert not prov.test_command.startswith(" ")

    def test_fallback_empty_python_cmd_no_bad_command(self):
        prov = provision_verify(
            _profile(has_tests=True, test_command="", test_dirs=["tests"], python_cmd="")
        )
        assert prov.test_command == ""


class TestDeterminismAndSecrets:
    def test_two_provisions_equal(self):
        profile = _profile(has_tests=False, app_module="backend.main:app")
        p1 = provision_verify(profile)
        p2 = provision_verify(profile)
        assert p1 == p2
        assert p1.smoke_test is not None
        assert p1.smoke_test.content == p2.smoke_test.content

    def test_no_secret_patterns_in_content(self):
        prov = provision_verify(_profile(has_tests=False, app_module="backend.main:app"))
        assert prov.smoke_test is not None
        blob = prov.smoke_test.content + prov.test_command + prov.health_url
        for needle in ("sk-", "token=", "password=", "api_key"):
            assert needle not in blob

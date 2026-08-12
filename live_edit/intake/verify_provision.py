"""verify 配置供给 + 冒烟测试生成。

从 T1 深度分析器产出的 :class:`RepoProfile` 生成可用的 ``[verify]`` 段配置
（``test_command`` / ``health_url``），并在项目没有测试时生成一个最小冒烟测试，
解锁 verify 的 auto-approve —— ``test_command`` 为空或带占位符时
``live_edit/verify/rules.py`` 会把确定性检查标为 SKIPPED 并降级人工审批。

本模块是纯函数、只读：不触碰文件系统（写文件由 T4 的 CLI 负责，带确认）、
不引入新第三方依赖、绝不硬编码密钥。

关于 pytest 依赖：生成的 test_command 假定目标环境已安装 pytest。T4 的
validate 会实际运行它并在缺失时提示安装；本模块不跑子进程、不做静默假设。

确定性保证：同一 profile 两次 :func:`provision_verify` 的结果逐字节一致。
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass

from .analyzer import RepoProfile

__all__ = ["SmokeTest", "VerifyProvision", "provision_verify"]

# 冒烟测试文件默认路径（相对仓库根，与生成的 test_command 保持一致）
SMOKE_TEST_PATH = "tests/test_smoke.py"

# 生成文件头注释：标明是自动生成的、可安全删除
_SMOKE_HEADER = "# 由 live-edit intake 自动生成（可安全删除）\n\n"


@dataclass
class SmokeTest:
    """最小冒烟测试文件（仅 has_tests=False 时生成）。"""

    path: str  # 相对仓库根，如 "tests/test_smoke.py"
    content: str  # 完整文件内容（确定性：逐字节一致）


@dataclass
class VerifyProvision:
    """一次 provision_verify 的完整结果。"""

    test_command: str  # 解析出的可运行测试命令；实在没有留空
    health_url: str  # 来自 profile.health_url（已含正确端口）
    smoke_test: SmokeTest | None  # 仅在 has_tests=False 时生成
    needs_confirmation: bool  # 生成 smoke_test 时为 True（CLI 写文件前须确认）


def provision_verify(profile: RepoProfile) -> VerifyProvision:
    """从 RepoProfile 生成 [verify] 段配置，必要时附带最小冒烟测试。"""
    if profile.has_tests:
        # 有测试：直接采用 T1 已解析好的 test_command；为空则按语言+test_dirs 兜底
        test_command = profile.test_command or _fallback_test_command(profile)
        smoke_test = None
    elif _is_python_project(profile):
        # 无测试但可生成 Python 冒烟测试：test_command 指向将生成的冒烟测试
        smoke_test = SmokeTest(
            path=SMOKE_TEST_PATH,
            content=_build_smoke_content(profile.app_module),
        )
        test_command = _pytest_command(profile.python_cmd, SMOKE_TEST_PATH)
    else:
        # 无测试且暂不支持自动冒烟（Node/Go 等）：不生成文件、test_command 留空，
        # verify 会保持降级人工审批；注释说明该语言暂不支持自动冒烟。
        smoke_test = None
        test_command = ""
    return VerifyProvision(
        test_command=test_command,
        health_url=profile.health_url,
        smoke_test=smoke_test,
        needs_confirmation=smoke_test is not None,
    )


def _is_python_project(profile: RepoProfile) -> bool:
    """是否可按 Python 冒烟测试处理：以语言为主判断，探测到 Python 入口兜底。

    语言为 python 的项目必然可生成冒烟测试；language="unknown" 但存在 Python
    应用入口（app_module 非空，如无 pyproject.toml 的 FastAPI）时也兜底覆盖。
    """
    return profile.language == "python" or bool(profile.app_module)


def _fallback_test_command(profile: RepoProfile) -> str:
    """has_tests=True 但 test_command 为空时的兜底命令（按 test_dirs 首项）。

    仅语言为 python 时才产 pytest 命令；非 python 项目（如 TS 仓库混入 py
    测试文件）不硬套 pytest，返回空串由 verify 保持降级人工审批。
    """
    if profile.language != "python":
        return ""
    target = profile.test_dirs[0] if profile.test_dirs else "tests"
    return _pytest_command(profile.python_cmd, target)


def _pytest_command(python_cmd: str, target: str) -> str:
    """构造 pytest 命令；解释器为空时返回空串，避免产出带前导空格的坏命令。

    命令参数用 shlex.quote 包裹，python_cmd 为含空格的绝对路径也能正确执行。
    """
    if not python_cmd:
        return ""
    return f"{shlex.quote(python_cmd)} -m pytest {shlex.quote(target)} -q --tb=short"


def _build_smoke_content(app_module: str) -> str:
    """构建冒烟测试文件内容（保守最小实现）。

    Python/FastAPI：``try: from {module} import {var}`` 后断言应用对象可导入；
    var 取 app_module 冒号后段（如 "application"），缺省为 "app"。不用
    TestClient（避免引入 httpx 依赖导致冒烟测试跑不起来）、不启动服务器。
    app_module 为空时退化为只测解释器版本的最小骨架并注释说明"待补充"。
    """
    module, _, var = app_module.partition(":")
    var = var or "app"
    if not module:
        return (
            _SMOKE_HEADER
            + "import sys\n"
            + "\n"
            + "\n"
            + "# 待补充：未检测到应用入口（app_module 为空），接入真实模块后请补充断言。\n"
            + "def test_smoke():\n"
            + "    assert sys.version_info >= (3, 8)\n"
        )
    return (
        _SMOKE_HEADER
        + "try:\n"
        + f"    from {module} import {var}\n"
        + "except ImportError:\n"
        + f"    {var} = None\n"
        + "\n"
        + "\n"
        + "def test_app_imports():\n"
        + f"    assert {var} is not None\n"
    )

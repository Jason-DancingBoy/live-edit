"""深度仓库分析器（intake）：为 live-edit intake 命令生成配置事实基础。

公共符号统一从这里 re-export：
- ``scan_project`` / ``RepoProfile`` 等：确定性仓库扫描（analyzer）
- ``render_extra_context``：RepoProfile → extra_context markdown（context）
- ``provision_verify`` / ``VerifyProvision`` / ``SmokeTest``：verify 配置 + 冒烟测试
- ``run_intake`` / ``IntakeResult``：intake 编排（run，把以上串成一条命令）
"""

from .analyzer import DBInfo, FrontendInfo, ModuleInfo, RepoProfile, RouteInfo, scan_project
from .context import render_extra_context
from .run import IntakeResult, run_intake
from .verify_provision import SmokeTest, VerifyProvision, provision_verify

__all__ = [
    "DBInfo",
    "FrontendInfo",
    "ModuleInfo",
    "RepoProfile",
    "RouteInfo",
    "scan_project",
    "render_extra_context",
    "SmokeTest",
    "VerifyProvision",
    "provision_verify",
    "IntakeResult",
    "run_intake",
]

"""Configuration: .live-edit.toml parsing, validation, and project detection."""

import json
import os
import re
import tomli
from dataclasses import dataclass, field


# ── Data models ──

@dataclass
class ModePromptConfig:
    base: str = ""
    user_persona: str = ""
    communication_rules: str = ""


@dataclass
class ModeConfig:
    label: str = ""
    approval: str = "per_tool"    # "per_tool" | "final" | "none"
    tools: str = "write"          # "all" | "write" | "readonly"
    approve_for: list[str] = field(default_factory=lambda: ["edit_file", "write_file"])
    prompt: ModePromptConfig = field(default_factory=ModePromptConfig)


@dataclass
class LLMConfig:
    api_url: str = ""
    api_key_env: str = ""
    model: str = ""
    provider: str = "anthropic_compatible"


@dataclass
class SafetyConfig:
    allowed_dirs: list[str] = field(default_factory=lambda: ["."])
    overwrite_allowed_dirs: list[str] = field(
        default_factory=lambda: ["static", "public", "assets"]
    )
    allow_overwrite_existing: bool = False
    blocked_commands: list[str] = field(default_factory=list)
    search_extensions: list[str] = field(default_factory=lambda: [
        "*.py", "*.html", "*.js", "*.css", "*.ts", "*.tsx",
        "*.md", "*.json", "*.toml", "*.yaml", "*.yml",
    ])


@dataclass
class TimeoutsConfig:
    api_request: int = 180
    shell_command: int = 30
    approval: int = 300
    final_approval: int = 600
    session_ttl: int = 1800
    max_rounds: int = 15


@dataclass
class SessionsConfig:
    max_active: int = 10


@dataclass
class HooksConfig:
    post_revert: str = ""
    pre_commit: str = ""


@dataclass
class PreviewConfig:
    enabled: bool = False
    port_start: int = 19000
    port_end: int = 19050
    startup_timeout: int = 30
    command: str = ""
    base_url: str = "http://localhost:8083"


@dataclass
class UIConfig:
    default_mode: str = "quick"


@dataclass
class ErrorTranslations:
    quick: dict[str, str] = field(default_factory=dict)
    deep: dict[str, str] = field(default_factory=dict)


@dataclass
class ProjectConfig:
    name: str = ""
    language: str = "unknown"
    framework: str = ""
    root: str = "."
    extra_context: str = ""


@dataclass
class Config:
    project: ProjectConfig = field(default_factory=ProjectConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    timeouts: TimeoutsConfig = field(default_factory=TimeoutsConfig)
    sessions: SessionsConfig = field(default_factory=SessionsConfig)
    hooks: HooksConfig = field(default_factory=HooksConfig)
    ui: UIConfig = field(default_factory=UIConfig)
    modes: dict[str, ModeConfig] = field(default_factory=dict)
    errors: ErrorTranslations = field(default_factory=ErrorTranslations)
    preview: PreviewConfig = field(default_factory=PreviewConfig)


# ── TOML parsing ──

def _parse_safety(data: dict) -> SafetyConfig:
    return SafetyConfig(
        allowed_dirs=data.get("allowed_dirs", ["."]),
        overwrite_allowed_dirs=data.get("overwrite_allowed_dirs", ["static", "public", "assets"]),
        allow_overwrite_existing=data.get("allow_overwrite_existing", False),
        blocked_commands=data.get("blocked_commands", []),
        search_extensions=data.get("search_extensions", SafetyConfig().search_extensions),
    )


def _parse_timeouts(data: dict) -> TimeoutsConfig:
    return TimeoutsConfig(
        api_request=data.get("api_request", 180),
        shell_command=data.get("shell_command", 30),
        approval=data.get("approval", 300),
        final_approval=data.get("final_approval", 600),
        session_ttl=data.get("session_ttl", 1800),
        max_rounds=data.get("max_rounds", 15),
    )


def _parse_mode(config_data: dict) -> tuple[str, ModeConfig]:
    """Parse a [modes.NAME] section. Returns (name, ModeConfig)."""
    label = config_data.get("label", "")
    approval = config_data.get("approval", "per_tool")
    tools = config_data.get("tools", "write")
    approve_for = config_data.get("approve_for", ["edit_file", "write_file"])

    prompt_data = config_data.get("prompt", {})
    prompt = ModePromptConfig(
        base=prompt_data.get("base", ""),
        user_persona=prompt_data.get("user_persona", ""),
        communication_rules=prompt_data.get("communication_rules", ""),
    )

    return label, ModeConfig(
        label=label,
        approval=approval,
        tools=tools,
        approve_for=approve_for,
        prompt=prompt,
    )


def parse_config(path: str) -> Config:
    """Parse a .live-edit.toml file into a Config object."""
    with open(path, "rb") as f:
        raw = tomli.load(f)

    project_data = raw.get("project", {})
    project = ProjectConfig(
        name=project_data.get("name", ""),
        language=project_data.get("language", "unknown"),
        framework=project_data.get("framework", ""),
        root=project_data.get("root", "."),
        extra_context=project_data.get("extra_context", ""),
    )

    llm_data = raw.get("llm", {})
    llm = LLMConfig(
        api_url=llm_data.get("api_url", ""),
        api_key_env=llm_data.get("api_key_env", ""),
        model=llm_data.get("model", ""),
        provider=llm_data.get("provider", "anthropic_compatible"),
    )

    safety = _parse_safety(raw.get("safety", {}))
    timeouts = _parse_timeouts(raw.get("timeouts", {}))
    sessions = SessionsConfig(max_active=raw.get("sessions", {}).get("max_active", 10))
    hooks_data = raw.get("hooks", {})
    hooks = HooksConfig(
        post_revert=hooks_data.get("post_revert", ""),
        pre_commit=hooks_data.get("pre_commit", ""),
    )
    ui = UIConfig(default_mode=raw.get("ui", {}).get("default_mode", "quick"))

    modes: dict[str, ModeConfig] = {}
    for key, value in raw.get("modes", {}).items():
        if isinstance(value, dict):
            _, mode = _parse_mode(value)
            mode.label = key
            modes[key] = mode

    errors_data = raw.get("errors", {})
    errors = ErrorTranslations(
        quick=errors_data.get("quick", {}),
        deep=errors_data.get("deep", {}),
    )

    _default_base = os.environ.get("LIVE_EDIT_BASE_URL", "http://localhost:8083")
    preview_data = raw.get("preview", {})
    preview = PreviewConfig(
        enabled=preview_data.get("enabled", False),
        port_start=preview_data.get("port_start", 19000),
        port_end=preview_data.get("port_end", 19050),
        startup_timeout=preview_data.get("startup_timeout", 30),
        command=preview_data.get("command", ""),
        base_url=preview_data.get("base_url", "") or _default_base,
    )

    return Config(
        project=project,
        llm=llm,
        safety=safety,
        timeouts=timeouts,
        sessions=sessions,
        hooks=hooks,
        ui=ui,
        modes=modes,
        errors=errors,
        preview=preview,
    )


# ── Validation ──

def validate_config(config: Config) -> list[str]:
    """Return list of validation error messages. Empty list = valid."""
    errors = []

    if not config.project.name:
        errors.append("project.name is required")
    if not config.project.language:
        errors.append("project.language is required")
    if not config.llm.api_url:
        errors.append("llm.api_url is required")
    if not config.llm.api_key_env:
        errors.append("llm.api_key_env is required")
    if not config.llm.model:
        errors.append("llm.model is required")

    if "quick" not in config.modes:
        errors.append("modes.quick is required (at minimum)")

    return errors


# ── Project detection ──

def detect_project(root: str) -> dict:
    """Auto-detect project metadata from filesystem. Returns dict of key facts."""
    info = {
        "name": os.path.basename(os.path.abspath(root)),
        "language": "unknown",
        "framework": "",
        "vcs": "none",
        "git_available": False,
    }

    # Git detection (runs before language blocks so all projects benefit)
    if os.path.exists(os.path.join(root, ".git")):
        info["vcs"] = "git"
        info["git_available"] = True

    # Python
    pyproject = os.path.join(root, "pyproject.toml")
    if os.path.exists(pyproject):
        info["language"] = "python"
        with open(pyproject, "rb") as f:
            try:
                data = tomli.load(f)
                proj = data.get("project", {})
                info["name"] = proj.get("name", info["name"])
                deps = proj.get("dependencies", [])
                if any("fastapi" in d.lower() for d in deps):
                    info["framework"] = "fastapi"
                elif any("flask" in d.lower() for d in deps):
                    info["framework"] = "flask"
                elif any("django" in d.lower() for d in deps):
                    info["framework"] = "django"
            except Exception:
                pass
        return info

    # Node.js
    pkg_json = os.path.join(root, "package.json")
    if os.path.exists(pkg_json):
        info["language"] = "typescript"
        with open(pkg_json) as f:
            try:
                data = json.load(f)
                info["name"] = data.get("name", info["name"])
            except Exception:
                pass
        return info

    # Go
    go_mod = os.path.join(root, "go.mod")
    if os.path.exists(go_mod):
        info["language"] = "go"
        with open(go_mod) as f:
            m = re.match(r"module\s+(\S+)", f.read())
            if m:
                info["name"] = m.group(1)
        return info

    return info


def generate_default_config(root: str, project_info: dict | None = None) -> Config:
    """Generate a sensible default Config for a project."""
    info = project_info or detect_project(root)

    return Config(
        project=ProjectConfig(
            name=info.get("name", "MyApp"),
            language=info.get("language", "unknown"),
            framework=info.get("framework", ""),
            root=".",
            extra_context="",
        ),
        llm=LLMConfig(
            provider="anthropic_compatible",
            api_url="https://api.deepseek.com/anthropic/v1/messages",
            api_key_env="DEEPSEEK_API_KEY",
            model="deepseek-v4-pro",
        ),
        safety=SafetyConfig(),
        timeouts=TimeoutsConfig(),
        sessions=SessionsConfig(),
        hooks=HooksConfig(),
        ui=UIConfig(),
        modes={
            "quick": ModeConfig(
                label="快速修改",
                approval="per_tool",
                tools="write",
                approve_for=["edit_file", "write_file"],
                prompt=ModePromptConfig(
                    base=f"你是 {info.get('name', 'MyApp')} 的全栈 Web 开发者 AI。",
                    user_persona="非技术背景的用户。用自然语言描述需求，用通俗语言沟通，禁止展示代码。",
                    communication_rules="用中文交流，禁止展示代码片段、文件路径、行号。从用户视角描述改动。",
                ),
            ),
            "deep": ModeConfig(
                label="深度开发",
                approval="final",
                tools="all",
                approve_for=[],
                prompt=ModePromptConfig(
                    base=f"你是 {info.get('name', 'MyApp')} 的开发者 AI 助手。",
                    user_persona="专业开发者。理解代码和技术概念。",
                    communication_rules="用中文交流，可以自由使用技术术语。展示关键代码片段。",
                ),
            ),
            "qa": ModeConfig(
                label="代码问答",
                approval="none",
                tools="readonly",
                approve_for=[],
                prompt=ModePromptConfig(
                    base=f"你是 {info.get('name', 'MyApp')} 的代码分析专家。",
                    user_persona="想要理解代码的学习者。",
                    communication_rules="用中文交流，清晰的代码引用。只能使用只读工具。",
                ),
            ),
        },
        errors=ErrorTranslations(
            quick={
                "old_string 在文件中未找到": "AI 发现文件内容已变化，会重新读取后调整",
                "old_string 匹配了": "AI 找到了多处匹配，会缩小范围重试",
                "路径越界": "操作已自动阻止（访问了项目外的文件）",
                "命令包含危险操作": "操作已自动阻止",
                "命令执行超时": "命令耗时过长，已自动终止",
                "write_file 只能覆写": "只能在该目录下创建或修改文件",
            },
            deep={},
        ),
        preview=PreviewConfig(),
    )

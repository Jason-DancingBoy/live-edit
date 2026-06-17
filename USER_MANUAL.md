# live-edit 用户手册

## 1. 它不是什么，它是什么

**live-edit 不是 LLM wrapper。** 它不会把用户的请求直接转交给大模型然后原样吐回。纯粹的 LLM wrapper 只有一步：prompt → response。

**live-edit 是一个 AI agent 框架。** 它围绕 LLM 构建了完整的 agent 循环：

```
用户请求 → AI 思考 → 自主选择工具 → 执行工具 → 观察结果 → AI 再思考 → 继续调用工具 → ... → 生成 diff → git commit
```

这个循环不是预设流程。AI 自己决定什么时候读文件、读哪个文件、用什么 grep 搜索、改哪一行。每一步的观测结果进入下一轮的消息历史，影响后续决策。实际路径可能是：

1. `search_code` 定位相关代码 → 2. `read_file` 读上下文 → 3. `edit_file` 修改 → 4. `old_string` 不匹配 → 5. 重新 `read_file` 确认当前内容 → 6. 调整后再次 `edit_file` → 7. 成功

### Agent 特征

**自主工具决策。** 工具不是菜单，是 agent 的能力。agent 自行规划何时用哪个工具、以什么参数调用、遇到错误怎么调整。整个过程是"感知→行动→观察→再行动"的闭环。

**三种自治级别对应三种 agent 范式。**

| 模式 | Agent 范式 | 人机关系 |
|------|-----------|---------|
| `deep` | 全自主 agent | 研究→编辑→提交，人不介入中间步骤 |
| `quick` | human-in-the-loop agent | 每次写操作等人批准，非技术用户的"懂技术的朋友" |
| `qa` | 只读 agent | 只能观察不能行动，纯粹的代码分析专家 |

**纠偏机制。** agent 循环内置 nudge 逻辑：如果 deep 模式下连续 3 轮只读不写，系统会要求"现在必须立即执行代码修改"来打破分析瘫痪。这是 agent orchestration，不是 LLM 本身的能力。

**环境隔离。** agent 不在项目目录上直接操作，而是在独立的 git worktree（`/tmp/live-edit/{session_id}`）里执行所有修改。每个 agent 实例有自己的沙盒——多会话并行不冲突，出错了直接删 worktree 不影响主干。

### 架构简图

```
浏览器前端 (live-edit.js)
    │  SSE 事件流
    ▼
FastAPI Router (/live-edit/*)
    │
    ├── Provider ──→ LLM (Anthropic API / DeepSeek / 兼容接口)
    ├── VCS      ──→ Git (commit / diff / revert / worktree 隔离)
    ├── Storage  ──→ SQLite (会话持久化)
    ├── Preview  ──→ 每会话独立 uvicorn 进程（边改边预览）
    └── Config   ──→ .live-edit.toml
```

---

## 2. 接入前——你需要准备四样东西

### 2.1 一个 LLM API Key

live-edit 默认对接 Anthropic-compatible 端点，开箱即用 DeepSeek：

```bash
export DEEPSEEK_API_KEY="sk-..."
```

如果使用其他模型，修改 `.live-edit.toml` 里的 `api_url` 和 `api_key_env`：

```toml
# Claude API 示例
[llm]
api_url = "https://api.anthropic.com/v1/messages"
api_key_env = "ANTHROPIC_API_KEY"
model = "claude-sonnet-4-6"

# Ollama 示例
[llm]
api_url = "http://localhost:11434/v1/messages"
api_key_env = "OLLAMA_API_KEY"
model = "qwen3"
```

### 2.2 一个 Git 仓库

live-edit 强依赖 git——每次修改会 `git commit`，回滚靠 `git revert`，多会话隔离靠 `git worktree`。**项目必须已经在 git 管理下，且至少有一次 commit**（否则 `git worktree add` 会失败）。

```bash
cd your-project/
git init
git add -A && git commit -m "Initial commit"
```

### 2.3 生成并填写配置文件

```bash
pip install live-edit
cd your-project/
live-edit init
```

这会在项目根目录生成 `.live-edit.toml`，自动检测语言和框架。**以下字段必须手动确认或编辑：**

```toml
[project]
name = "YourApp"           # 必填，注入 system prompt
language = "python"        # 必填，自动检测通常正确
framework = "fastapi"      # 自动检测
extra_context = """..."""  # 强烈推荐：告诉 AI 项目结构、文件职责、注意事项

[llm]
api_url = "https://api.deepseek.com/anthropic/v1/messages"
api_key_env = "DEEPSEEK_API_KEY"
model = "deepseek-v4-pro"

[preview]
enabled = true             # 如果要"边改边预览"，必须开
command = "uvicorn server:app --host 127.0.0.1 --port {port}"  # 按实际启动命令改
base_url = "http://localhost:8083"  # 主站地址，preview 反向代理用
```

**`extra_context` 是决定 AI 理解项目能力的关键。** 写得越好，AI 改代码越准。建议至少包含：文件结构、各文件职责、技术栈说明、特殊注意事项（如"static/index.html 约 7000 行，修改时必须先用搜索定位行号，禁止一次性读取整个文件"）。参考 lyric-muse 项目的 `.live-edit.toml` 可以看到一个完整的 `extra_context` 示例。

### 2.4 两行代码接入

在 FastAPI 应用中引入路由：

```python
from live_edit import setup_live_edit

app.include_router(setup_live_edit())
```

如果有 admin 面板，建议传入 `admin_key` 以启用 worktree 管理和强制取消会话：

```python
app.include_router(setup_live_edit(api_key=API_KEY, admin_key=ADMIN_KEY))
```

在 HTML 模板的 `</body>` 前添加前端脚本：

```html
<script src="/live-edit/static/live-edit.js"></script>
```

接入完成。用户按 `Ctrl+Shift+D` 打开编辑面板，或点击页面左下角的「即时编辑」按钮。

---

## 3. 三种模式

| 模式 | 键 | 审批方式 | 工具权限 | 适用场景 |
|------|-----|---------|---------|---------|
| **quick** | `quick` | 每个写操作需用户批准 | 全部（危险命令自动拦截） | 非技术用户、小改动 |
| **deep** | `deep` | 自主执行，最终 diff 统一审批 | 全部 | 开发者、复杂任务 |
| **qa** | `qa` | 无审批 | 只读（read_file, search_code, glob, run_shell） | 代码分析、学习 |

### quick 模式流程

```
用户输入请求 → AI 分析 → 提出工具调用 → 用户批准/拒绝 → 执行 → ... → diff → 用户批准提交 → git commit
```

### deep 模式流程

```
用户输入请求 → AI 自主执行多步操作 → diff → 自动提交 → git commit
```

### qa 模式流程

```
用户提问 → AI 使用只读工具分析 → 返回答案（不产生任何修改）
```

---

## 4. 配置文件参考（.live-edit.toml）

### 4.1 完整示例

```toml
[project]
name = "my-app"
language = "python"
framework = "fastapi"
root = "."
extra_context = "这是一个电商后台管理系统，使用 SQLAlchemy ORM。"

[llm]
provider = "anthropic_compatible"
api_url = "https://api.deepseek.com/anthropic/v1/messages"
api_key_env = "DEEPSEEK_API_KEY"
model = "deepseek-v4-pro"

[safety]
allowed_dirs = ["."]
overwrite_allowed_dirs = ["static", "public", "assets"]
allow_overwrite_existing = false
search_extensions = ["*.py", "*.html", "*.js", "*.css", "*.ts", "*.tsx", "*.md", "*.json", "*.toml"]

[timeouts]
api_request = 180
shell_command = 30
approval = 300
final_approval = 600
session_ttl = 1800

[sessions]
max_active = 10

[hooks]
post_revert = ""

[ui]
default_mode = "quick"

[modes.quick]
label = "快速修改"
approval = "per_tool"
tools = "write"
approve_for = ["edit_file", "write_file"]

[modes.quick.prompt]
base = "你是 my-app 的全栈 Web 开发者 AI。"
user_persona = "非技术背景的用户。用自然语言描述需求，用通俗语言沟通，禁止展示代码。"
communication_rules = "用中文交流，禁止展示代码片段、文件路径、行号。从用户视角描述改动。"

[modes.deep]
label = "深度开发"
approval = "final"
tools = "all"
approve_for = []

[modes.deep.prompt]
base = "你是 my-app 的开发者 AI 助手。"
user_persona = "专业开发者。理解代码和技术概念。"
communication_rules = "用中文交流，可以自由使用技术术语。展示关键代码片段。"

[modes.qa]
label = "代码问答"
approval = "none"
tools = "readonly"
approve_for = []

[modes.qa.prompt]
base = "你是 my-app 的代码分析专家。"
user_persona = "想要理解代码的学习者。"
communication_rules = "用中文交流，清晰的代码引用。只能使用只读工具。"

[errors.quick]
"old_string 在文件中未找到" = "AI 发现文件内容已变化，会重新读取后调整"
"路径越界" = "操作已自动阻止（访问了项目外的文件）"
"命令包含危险操作" = "操作已自动阻止"
```

### 4.2 配置项说明

| 节 | 字段 | 类型 | 说明 |
|----|------|------|------|
| `[project]` | `name` | str | 项目名称 |
| | `language` | str | 语言：python / typescript / go / unknown |
| | `framework` | str | 框架：fastapi / flask / django |
| | `root` | str | 项目根目录，默认 `"."` |
| | `extra_context` | str | 注入 system prompt 的额外上下文 |
| `[llm]` | `provider` | str | 固定 `"anthropic_compatible"` |
| | `api_url` | str | Anthropic Messages API 兼容端点 |
| | `api_key_env` | str | API Key 所在的环境变量名 |
| | `model` | str | 模型名称 |
| `[safety]` | `allowed_dirs` | list | 允许操作的目录 |
| | `overwrite_allowed_dirs` | list | write_file 可覆盖已有文件的目录 |
| | `allow_overwrite_existing` | bool | 允许覆盖任意已有文件 |
| | `search_extensions` | list | search_code 搜索的文件扩展名 |
| `[timeouts]` | `api_request` | int | LLM API 请求超时（秒） |
| | `shell_command` | int | shell 命令超时（秒） |
| | `approval` | int | quick 模式单步审批超时（秒） |
| | `final_approval` | int | quick 模式最终 diff 审批超时（秒） |
| | `session_ttl` | int | 会话过期时间（秒） |
| `[sessions]` | `max_active` | int | 最大并发会话数 |
| `[hooks]` | `post_revert` | str | revert 后执行的 shell 命令 |
| `[ui]` | `default_mode` | str | 默认模式 |
| `[modes.<name>]` | `label` | str | 模式显示名 |
| | `approval` | str | per_tool / final / none |
| | `tools` | str | all / write / readonly |
| | `approve_for` | list | 需要审批的工具名 |
| `[modes.<name>.prompt]` | `base` | str | 系统提示词主体 |
| | `user_persona` | str | 用户画像描述 |
| | `communication_rules` | str | 交互规则 |
| `[errors.<mode>]` | — | dict | 错误消息翻译表 |

---

## 5. API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/live-edit/stream` | 启动新会话，返回 SSE 事件流 |
| `POST` | `/live-edit/continue/{session_id}` | 在已有会话上追加新请求 |
| `POST` | `/live-edit/approve/{session_id}/{tool_id}` | 批准/拒绝工具调用（quick 模式） |
| `GET` | `/live-edit/timeline` | 获取变更时间线（可选 `?diff_for=<hash>` 查看 diff） |
| `GET` | `/live-edit/history` | 获取历史会话列表 |
| `GET` | `/live-edit/session/{session_id}` | 获取某次会话详情 |
| `POST` | `/live-edit/revert/{hash}/preview` | 预检回滚（检查冲突） |
| `POST` | `/live-edit/revert/{hash}/execute` | 执行回滚 |
| `GET` | `/live-edit/static/{filename}` | 静态文件（JS/CSS） |
| `GET` | `/live-edit/health` | 健康检查 |

### SSE 事件类型

| 事件类型 | 触发时机 | 字段 |
|----------|---------|------|
| `thinking` | AI 推理完成 | `text` |
| `text` | AI 输出文本（流式） | `text` |
| `tool_plan` | AI 提出工具调用 | `id`, `tool`, `args`, `summary`, `reason` |
| `tool_result` | 工具执行完毕 | `id`, `ok`, `error`, `path`, `content` |
| `diff` | 生成变更摘要 | `files`, `summary`, `diff` |
| `done` | 会话结束 | `committed`, `commit_hash`, `message` |
| `error` | 发生错误 | `error` |

---

## 6. 自定义实现

live-edit 的三个核心组件都可以替换为自定义实现：

### 6.1 自定义 Provider

实现 `live_edit.Provider` 抽象类：

```python
from live_edit import Provider, setup_live_edit

class MyProvider(Provider):
    async def call_with_tools(self, messages, tools, on_thinking=None, on_text=None):
        # 调用你选择的 LLM API
        # 返回 content_blocks 列表，每项格式：
        #   {"type": "text", "text": "..."}
        #   或 {"type": "tool_use", "id": "...", "name": "...", "input": {...}}
        ...

app.include_router(setup_live_edit(provider=MyProvider()))
```

默认 `AnthropicCompatibleProvider` 支持任何兼容 Anthropic Messages Streaming API 的服务（DeepSeek、Claude API 等）。

### 6.2 自定义 Storage

实现 `live_edit.Storage` 抽象类：

```python
from live_edit import Storage

class MyStorage(Storage):
    def save_session(self, session_id, request, committed, files, commit_hash, messages_json, mode):
        ...

    def get_sessions(self, limit=30):
        ...

    def get_session_detail(self, session_id):
        ...

app.include_router(setup_live_edit(storage=MyStorage()))
```

默认 `SQLiteStorage` 使用项目目录下的 `live_edit.db`。

### 6.3 自定义 VCS

实现 `live_edit.VCS` 抽象类：

```python
from live_edit import VCS, RevertPreview, RevertResult

class MyVCS(VCS):
    def commit(self, files, message) -> str:
        ...

    def diff_stat(self, files) -> str:
        ...

    def diff_full(self, files) -> str:
        ...

    def revert_preview(self, commit_hash) -> RevertPreview:
        ...

    def revert_execute(self, commit_hash) -> RevertResult:
        ...

    def show_commit(self, commit_hash) -> dict:
        ...

    def log_live_edit_commits(self, limit=30) -> list:
        ...

app.include_router(setup_live_edit(vcs=MyVCS()))
```

默认 `GitVCS` 通过 subprocess 调用 git 命令；commit 消息前缀为 `live-edit:`。

---

## 7. 安全机制

### 7.1 路径越界防护

所有文件操作通过 `_safe_path()` 校验，确保操作路径在项目根目录内。`../` 等路径穿越会被阻止。

### 7.2 危险命令拦截

`run_shell` 内置正则拦截规则，以下模式会被自动阻止：

- 删除命令：`rm`、`unlink`、`git rm`
- 数据库破坏：`drop table`、`delete from`
- 强制推送：`git push`、`git reset --hard`
- 系统操作：`shutdown`、`reboot`、`mkfs`、`dd if=`
- 权限过高：`chmod 777`
- 管道执行：`curl ... | bash`、`:(){ :|:& };:`
- 输出重定向到项目外

### 7.3 写文件控制

- `write_file` 默认只能创建新文件，或覆写 `static/public/assets` 目录下的文件
- 可通过 `[safety]` 中的 `overwrite_allowed_dirs` 和 `allow_overwrite_existing` 调整

### 7.4 并发限制

`max_active` 控制最大并发会话数（默认 10），超过返回 503。

---

## 8. CLI 命令

```bash
live-edit init [目录]       # 生成 .live-edit.toml
live-edit check [配置文件]   # 验证配置文件
live-edit --help             # 帮助信息
```

选项：
- `--force`：强制覆盖已有配置文件（init）

---

## 9. 前端脚本

### 引用方式

```html
<script src="/live-edit/static/live-edit.js"></script>
```

### 交互说明

- **快捷键** `Ctrl+Shift+D`：打开/关闭面板
- **面板按钮**「即时编辑」：固定在页面左下角
- **模式切换**：面板顶部下拉框（快速修改 / 深度开发 / 代码问答）
- **历史按钮**：查看近期变更时间线
- **批准/拒绝**：quick 模式下每个写操作弹出确认卡片
- **最终审批**：quick 模式 diff 显示后，可选择「应用更改」或「全部放弃」

面板状态（打开/关闭）保存在 `localStorage` 中。

---

## 10. 会话生命周期

```
创建 (POST /stream)
  → 产生 session_id (le_xxxxxxxxxxxx)
  → 加入内存 SessionStore（TTL 默认 1800s）
  → 启动 SSE 事件流
  → AI 循环（最多 15 轮工具调用）
  → 生成 diff
  → 用户批准 → git commit → 持久化到 SQLite
  → 用户拒绝 → git checkout（放弃变更）
  → 会话结束
```

通过 `POST /continue/{session_id}` 可在同一会话继续追加请求，保持上下文。

---

## 11. 导出接口总览

```python
from live_edit import (
    # 路由初始化
    setup_live_edit,

    # Provider
    Provider,
    AnthropicCompatibleProvider,

    # Storage
    Storage,
    SQLiteStorage,

    # VCS
    VCS,
    GitVCS,
    RevertPreview,
    RevertResult,

    # Config
    Config,
    parse_config,
    validate_config,
    detect_project,

    # Engine
    EditSession,
    SessionStore,
    build_timeline,
    translate_error,
)
```

---

## 12. 常见问题

**Q: 支持什么大模型？**
任意兼容 Anthropic Messages API 的服务。默认连接 DeepSeek，改为 Claude API 设置 `api_url` 为 `https://api.anthropic.com/v1/messages` 即可。

**Q: 必须使用 Git 吗？**
默认依赖 Git。可以通过自定义 VCS 适配其他版本控制系统。

**Q: 多个用户同时使用怎么办？**
`SessionStore` 按内存分隔，每个 SSE 连接独立会话。`max_active` 控制并发上限。

**Q: 如何让 AI 更了解我的项目？**
在 `[project]` 中设置 `extra_context`，内容会注入 system prompt。

**Q: 回滚是如何工作的？**
通过 `git revert` 实现，先 preview 检查冲突，再 execute 执行。支持 `post_revert` 钩子。

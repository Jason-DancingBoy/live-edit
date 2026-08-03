# live-edit 接入指南

从 `pip install` 到用户可以 `Ctrl+Shift+D` 唤起编辑面板，约 10 分钟。

## 1. 安装

```bash
pip install live-edit==0.2.0            # 基础安装
pip install "live-edit==0.2.0[rag]"     # 如需 AI 记忆功能（安装 sentence-transformers）
```

## 2. 前置条件

### 2.1 Git 仓库（必须）
live-edit 用 `git worktree` 做隔离编辑，要求项目是 git 仓库且**至少有一次 commit**：

```bash
# 如果还没初始化 git
cd your-project/
git init
git add -A
git commit -m "Initial commit"
```

### 2.2 LLM API Key（必须）
默认走 DeepSeek API，需要环境变量：

```bash
export DEEPSEEK_API_KEY="sk-..."
```

想用 Claude / Ollama / 其他兼容 Anthropic Messages API 的端点，在后面步骤的配置里改 `api_url`。

## 3. 初始化配置

```bash
cd your-project/
live-edit init
```

这会生成 `.live-edit.toml`，自动探测项目类型（Python/FastAPI/Node.js/Go）。

### 3.1 必填字段

打开 `.live-edit.toml`，至少确认以下几项：

```toml
[project]
name = "your-project-name"          # 项目名
language = "python"                 # python / javascript / go
# ⚠️ extra_context 是影响 AI 编辑质量的最关键字段
extra_context = """
项目使用 FastAPI + SQLAlchemy，路由在 routers/ 目录，
数据库模型在 models/ 目录，前端在 static/ 目录。
所有 API 端点需要 JWT 认证。
"""

[llm]
provider = "anthropic_compatible"
api_url = "https://api.deepseek.com/anthropic/v1/messages"
api_key_env = "DEEPSEEK_API_KEY"    # 对应的环境变量名
model = "deepseek-v4-pro"

[modes.quick]
prompt = "你是一个代码编辑助手……"

[modes.deep]
prompt = "你是一个全自主代码工程师……"
```

`extra_context` 会注入到系统提示词中。它应该描述项目文件结构、技术栈约定、注意事项——越详细，AI agent 产出质量越高。

### 3.2 预览（可选但推荐）

```toml
[preview]
enabled = true
command = "uvicorn server:app --host 127.0.0.1 --port {port}"
port_range = [19000, 19050]
```

预览功能让编辑结果在合并前可实时查看。`{port}` 会被替换为动态分配的端口。如果你的应用入口不是 `server:app`，改成实际路径。

### 3.3 会话记忆（可选）

```toml
[session_memory]
enabled = true
```

启用后 agent 会检索类似历史编辑会话作为上下文参考。需要先安装 `live-edit[rag]`。

### Session Memory → Memory System (v0.3.0+)

The `[session_memory]` section is deprecated in favor of `[memory]`:

```toml
[memory]
enabled = true

[memory.short_term]
max_full_rounds = 3

[memory.long_term]
enabled = true
embedder = { type = "local", model = "thenlper/gte-small" }

[memory.knowledge]
enabled = true
knowledge_dir = ".live-edit/knowledge"
```

The old `[session_memory]` section still works but maps to `[memory.long_term]`.
New features like recency decay and knowledge base are only available via `[memory]`.

## 4. 代码接入

### 4.1 Python 后端（2 行）

在已有的 FastAPI 应用上挂载路由：

```python
from live_edit import setup_live_edit

app.include_router(setup_live_edit())
```

如果同时需要管理端点，传入 admin_key：

```python
app.include_router(setup_live_edit(admin_key="your-secret-key"))
```

> 管理端点包括：查看活跃工作树、强制取消会话、合并/删除分支、清理隔离环境。

### 4.2 前端页面（1 行）

在 HTML 页面的 `</body>` 前引入脚本：

```html
<script src="/live-edit/static/live-edit.js"></script>
```

用户按 `Ctrl+Shift+D` 即可唤起编辑面板。面板里用自然语言描述需求，agent 自动读代码 → 搜索 → 编辑 → 提交。

## 5. 验证

启动你的 FastAPI 应用后，检查健康端点：

```bash
curl http://localhost:你的端口/live-edit/health
# {"status": "ok", "active_sessions": 0}
```

访问带脚本标签的页面，按 `Ctrl+Shift+D` 确认面板弹出。

## 6. 用户使用流程

三种模式对应三类用户：

| 模式 | 适用人群 | 行为 |
|------|---------|------|
| **quick** | 非技术用户（产品、运营） | 每次写操作需人工批准，错误翻译成易懂语言 |
| **deep** | 开发者 | AI 自主读写编辑，最终 diff 批量审批 |
| **qa** | 任何人学习代码 | 只读分析，不会产生任何副作用 |

典型 quick 模式流程：
1. 用户在面板输入："在首页加一个公告栏组件"
2. AI 搜索代码 → 定位文件 → 读取上下文 → 生成编辑方案 → 弹窗请用户批准
3. 用户批准 → AI 执行修改 → 展示 diff → 提交

## 7. 目录结构速查

接入后项目会新增/使用以下文件：

| 文件/目录 | 说明 |
|-----------|------|
| `.live-edit.toml` | 配置文件（必须） |
| `live_edit.db` | SQLite 会话存储（自动创建） |
| `/tmp/live-edit/{session_id}/` | 每次编辑会话的隔离 git worktree |

## 常见问题

**Q: 为什么 agent 一直读文件不写？**
A: 可能是 `extra_context` 不够详细，agent 找不到目标。补充文件结构描述后重试。

**Q: 编辑结果不对怎么办？**
A: 使用 `git revert` 回滚——所有修改都是独立的 git commit，随时可回退。管理端点也提供了 revert API。

**Q: 多个用户同时编辑会冲突吗？**
A: 不会。每个会话在独立的 git worktree 中运行，互不干扰。

**Q: 支持什么 LLM？**
A: 所有兼容 Anthropic Messages API 的端点：Claude API、DeepSeek、Ollama、vLLM、LiteLLM 等。改 `.live-edit.toml` 中的 `api_url` 即可切换。

**Q: 不用 FastAPI 能用吗？**
A: 目前只支持 FastAPI。但可以通过实现 `Provider` / `Storage` / `VCS` 抽象接口来扩展。

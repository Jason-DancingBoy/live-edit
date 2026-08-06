# Standalone App Design — 独立部署应用

**Date**: 2026-08-06
**Status**: draft
**Scope**: 把 live-edit 做成单租户内部工具：独立 server 入口 + 独立宿主页面 + 多用户认证 + 角色门控的合并审批，Docker 容器部署
**Route**: 薄壳复用（Approach A）——核心零改动，壳层加认证、页面、部署

## Overview

live-edit 目前是嵌入宿主 FastAPI 的库（`app.include_router(setup_live_edit())`），前端靠 `<script>` 注入 + `Ctrl+Shift+D` 唤起。业务人员要独立使用，需要一个自包含、可直接部署的独立应用。

本 spec 定义在**不动核心引擎/路由器逻辑**的前提下，新增一个薄壳层，把 live-edit 变成可独立部署的单租户应用：

1. **独立 server 入口** —— `live-edit serve` 子命令，自建 FastAPI app，挂载现有路由器。
2. **独立宿主页面** —— 登录页 + 编辑面板页 + admin 后台页，不再依赖宿主页面注入。
3. **多用户认证 + 角色分离** —— 登录、会话、`admin` / `business_user` 两种角色。
4. **角色门控的合并审批** —— 业务人员可编辑/批准自己的改动，合并进主分支仅 admin 可操作。
5. **Docker 容器部署** —— Dockerfile + compose + 启动脚本，挂载任意 git 仓库即用。

目标项目代码**未定**，按配置驱动设计：容器挂载的 git 仓库即业务目标，读其 `.live-edit.toml` 驱动 provider/LLM/模式。

## Architecture

```
live_edit/
├── server.py                (new)  create_app(): 独立 FastAPI 壳层工厂
├── auth.py                  (new)  用户表/会话/密码哈希/角色判断
├── static/
│   ├── host/login.html      (new)  登录页
│   ├── host/editor.html     (new)  编辑面板宿主页
│   └── host/admin.html      (new)  admin 后台页
├── cli.py                          + serve / create-user 子命令
└── live-edit.js                    小改: 支持面板自动唤起(data 属性)
根目录:
├── Dockerfile                (new)
├── docker-compose.yml        (new)
└── scripts/                  (new)  启动/初始化脚本
```

核心约束：**不修改 engine.py / router.py 的内部逻辑**。壳层通过挂载现有路由 + 中间件完成认证与门控。

## 1. 壳层 server.py

`create_app()` 返回独立 FastAPI 应用：

```python
def create_app(project_root=".", config_path=".live-edit.toml", ...) -> FastAPI:
    app = FastAPI(title="live-edit standalone")
    # 挂载现有路由器(原样)
    app.include_router(setup_live_edit(project_root=..., ...))
    # 认证中间件 + 登录/登出 + 宿主页面路由 + admin 后台路由
```

职责：
- 挂载 `setup_live_edit()` 路由器到 `/live-edit`（路径不变，前端资产复用）。
- 认证中间件：对 `/live-edit/*` 强制会话校验（见 §2 门控映射）。
- 登录/登出端点、宿主页面路由（`/`、`/login`、`/editor`、`/admin`）。
- 首个 admin 引导（用户表为空时用环境变量建 admin）。

## 2. 认证与角色门控（auth.py）

### 用户模型

- 表 `app_users(id, username, password_hash, role, created_at)`，存独立 SQLite `live_edit_app.db`。
- `role` ∈ `{admin, business_user}`。
- 密码哈希：`hashlib.scrypt`（stdlib，无额外依赖），不存明文。
- **无自助注册**。admin 用 CLI `live-edit create-user` 添加用户。

### 会话

- 不透明 token 存 `sessions` 表 + HttpOnly cookie（`Set-Cookie`），过期时间可配（默认与 `timeouts.session_ttl` 对齐）。
- `/login` 校验通过后种 cookie；`/logout` 删除会话。

### 门控映射（中间件按路径前缀，核心零改动）

| 路径前缀 | 要求 |
|---------|------|
| `/live-edit/admin/*` | 仅 `admin` |
| 其余 `/live-edit/*`（stream、approve、continue、timeline、revert、preview、knowledge…） | 任意已登录用户 |
| `/live-edit/static/*`、`/live-edit/health`、`/live-edit/metrics` | 免认证 |

### admin_key 保留

现有 admin 端点仍接受 `admin_key` 头（API 级管理员凭证，不变）。UI 侧走会话 + 角色。两条路并存：
- **中间件豁免**：请求携带有效的 `admin_key` 头时，中间件视为 admin 级访问，不要求会话——保证现有 API 客户端用 `admin_key` 调 `/live-edit/admin/*` 的行为完全不变。
- **会话用户**：浏览器用户走会话 cookie + 角色；`business_user` 即使带 `admin_key` 也不会在 UI 中看到管理后台。

### 安全硬化

现有 `/live-edit/stream` 等会话端点目前无认证（信任宿主环境）。壳层中间件要求其登录态，这是独立部署给业务人员使用时的必要硬化。

## 3. 宿主页面

### 路由

- `/` → 未登录跳 `/login`，已登录跳 `/editor`
- `/login` → login.html：用户名/密码表单，POST /login 成功回 `/editor`
- `/editor` → editor.html：承载编辑面板，顶部栏显示项目名、当前用户、角色徽标、登出按钮；admin 额外显示"管理后台"入口
- `/admin` → admin.html（中间件校验 admin）：未合并分支列表（合并/删除按钮）+ 用户管理（建用户、列用户）——后端端点已存在（`/live-edit/admin/*`），页面直接调用

### 前端小改（唯一动到现有资产）

现有 `live-edit.js` 靠 `<script>` 注入 + `Ctrl+Shift+D` 唤起。独立页需要面板**默认可见**：
- 给 `live-edit.js` 加"自动唤起"开关（data 属性，如 `<div data-live-edit-auto-open>`），editor.html 用它让面板一进来就打开。
- 纯增量，不碰面板内部逻辑。

## 4. 数据流

- **登录**：POST /login 验证 → 种 HttpOnly cookie → 跳 /editor。
- **编辑**：业务人员输入自然语言 → POST /live-edit/stream → SSE 流式 → quick 模式写操作弹窗批准 → 应用 → 展示 diff → 提交到会话分支 → 预览。
- **合并**：admin 进 /admin → 列未合并分支 → 合并（冲突处理已有）→ 进主分支。

## 5. 错误处理

| 场景 | 行为 |
|------|------|
| 会话失效（401） | 跳转 /login |
| 角色不足（403） | "无权限"页面 |
| 登录失败 | 表单内联报错 |
| SSE 中断 | 前端提示重试 |

## 6. 测试

- **auth 单测**：登录成功/失败、会话过期、角色门控（business 调 `/admin/*` 被 403、调 `/stream` 放行）、env 引导 admin、登出。
- **宿主页**：`/login`、`/editor`、`/admin` 渲染，未登录跳转。
- **合并审批集成测试**：临时 git 仓库，business 合并被拒、admin 合并成功。
- **回归**：现有 engine/router 测试全绿。

## 7. 部署

- **Dockerfile**：python slim + 安装包 + 静态资源 + 暴露端口 + `CMD live-edit serve`。
- **docker-compose.yml**：业务仓库挂载到 `/workspace`、LLM API key 环境变量、admin 引导变量、端口映射。
- **入口脚本**：启动时校验挂载卷是 git 仓库、缺配置则 `live-edit init`、引导 admin。

## 8. 明确不做（v1 之外）

- 自助注册 / 找回密码
- 完整用户管理 UI（只要建 + 列）
- 多租户
- 通知
- 审计日志 UI（audit log 已有，无 UI）
- SSO

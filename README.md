# live-edit

用自然语言改代码。AI 在隔离的 git 分支上读代码、改文件、跑测试、提交；你只负责说需求和批准合并。

live-edit 是库形态：两行代码嵌进你已有的 FastAPI 应用，前端加一行脚本。没有登录、没有角色、没有独立服务——需要这些的话，用 live-build（同一套引擎的独立服务形态）。

## 安装

```bash
pip install live-edit
cd your-project/
live-edit intake    # 自动生成 .live-edit.toml（见「接入新项目」）
```

## 接入（2 行代码）

```python
from live_edit import setup_live_edit

app.include_router(setup_live_edit())
```

页面里加一行脚本：

```html
<script src="/live-edit/static/live-edit.js"></script>
```

按 `Ctrl+Shift+D` 打开编辑面板。

## 三种模式

| 模式 | 给谁 | 行为 |
|---|---|---|
| quick | 非技术用户 | 每次写操作都要人批准，报错翻成大白话 |
| deep | 开发者 | 自主改代码，改完的 diff 一次批准 |
| qa | 读代码 | 只读定位，不触发审批 |

## 工作方式

一次编辑会话大致是这样：

1. 在隔离的 git worktree 里建会话分支
2. agent 循环：读文件 → 搜代码 → 改文件 → 看结果 → 不行就重试
3. 改完跑 `[verify]` 门禁：测试、健康检查、diff 安全检查
4. 通过后提交到会话分支

每个会话的改动是一个独立 commit，可以回滚。会话在独立 worktree 里跑，互不干扰。

## 接入新项目

`.live-edit.toml` 里的 `extra_context` 决定 AI 改代码前对项目了解多少，也是最需要人来写的一段。`live-edit intake` 自动完成：

- 扫描代码库，生成一份事实性的 extra_context
- 配好 `[verify]` 的测试命令和健康检查；没有测试就生成一个冒烟测试
- 真的跑一遍测试命令，确认配置能工作

```bash
live-edit intake --dry-run    # 只预览，不写文件
live-edit intake --force      # 覆盖已有配置
```

只想拿一份不分析的最小配置，用 `live-edit init`。

前提：仓库得是 git 仓库且至少有一次提交（worktree 隔离依赖它）。

## 配置

`.live-edit.toml` 控制 LLM 提供商、模式提示词、超时、安全性、verify 门禁和预览。改完跑 `live-edit check` 校验。

## 文档

- [USER_MANUAL.md](USER_MANUAL.md) — 架构、agent 循环、配置参考、API、安全模型

## live-edit 和 live-build

同一套引擎，两种形态：

- **live-edit**：库。嵌进现有应用，没有登录和角色。
- **live-build**：独立服务。自带登录、admin / business_user 双角色和实时预览，适合需要多人协作和合并审批的场景。

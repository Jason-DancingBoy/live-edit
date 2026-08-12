# 项目约定（live-edit）

## 开发流程：双代理模式
- 主会话担任 PM + 测试工程师 + 代码审查员，不直接写代码。
- 实现工作派发给子代理（Agent 工具或 `claude -p` 后台 CLI），派发时给出完整上下文。
- 每个任务完成后并行派发两份审查：Spec 审查（逐条对照需求，file:line 证据）+ 代码质量审查（Critical/Important/Minor 分级）。
- 审查不通过由子代理修复，主会话不代改。全部通过后跑全部测试再交付。

## 密钥红线
- 禁止在代码 / 脚本 / 配置中硬编码密钥、token、密码。
- 一律用 `.env` + 环境变量引用（例如 `api_key_env = "DEEPSEEK_API_KEY"`）。

## 指标（Metrics）约定
- 使用进程内手写 `Metrics` 类（live_edit/metrics.py），不引入 prometheus_client 依赖。
- 指标名统一以 `live_edit_` 前缀。
- 新增指标必须在 tests/test_metrics.py 补单测。
- 指标端点 GET /live-edit/metrics 返回 Prometheus 文本格式，生产环境在反向代理层 gate。

## 测试约定
- 测试文件位于 tests/，pytest 运行。
- 修改核心逻辑后必须运行全部测试，交付前保证通过。

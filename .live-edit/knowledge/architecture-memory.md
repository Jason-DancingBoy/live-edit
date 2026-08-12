# 记忆系统架构（live-edit 三层记忆）

## 三层结构
- L1 ShortTermMemory：会话窗口管理。轮次超阈值时 strip 旧轮次，可配置带 LLM 摘要。
- L2 LongTermMemory：历史会话向量记忆。每次会话结束按文件切片存储（request chunk + 每文件 file_diff chunk），检索时按余弦相似度 + 时间衰减 + 命中加分排序。
- L3 KnowledgeBase（项目知识库）：独立于会话的静态文档库。来源为文件同步（knowledge_dir 下的 .md/.txt，启动时增量索引）或 API 上传（source_path 需 `api:` 前缀）。

## L3 触发规则
- L3 只在 L2 禁用、或 L2 检索返回空时兜底触发。
- 只要 L2 召回 >= 1 条，L3 不触发。这是刻意的优先级设计。
- L2 为空的原因包括：无历史 chunk（冷启动）、所有 chunk 余弦相似度低于 similarity_threshold（默认 0.6）被过滤、检索抛异常降级返回空。

## 知识注入
- 检索命中后格式化为 `## Project Knowledge` 上下文块，注入到 system prompt（新会话）或作为追加消息（续写会话）。

## 部署决策
- 独立应用采用 thin-shell 单租户部署，对外只有单进程 FastAPI 服务。
- 记忆持久化在本地 SQLite（live_edit_storage.db），向量检索优先 sqlite-vec，不可用时回退暴力扫描。

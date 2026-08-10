# 评估管线默认开启 · 为质量兜底 Design

## Overview

live-edit 的「编辑后评估管线」(lint → test → introspect → preview → html_diff)目前默认关闭,且存在三个缺陷:① 默认 `stages` 含 preview/html_diff,而 preview 默认关闭导致「预览不可用」被当成失败,每轮修改都会触发 3 轮无意义自愈;② test/lint 探测命令用 `|| echo '...'` 兜底,把真实的失败也吞成通过,质量网形同虚设;③ introspect 依赖的 `_cached_diff` 首轮为空,AI 自审首轮空转。

目标:面向**非技术用户**兜质量——默认开启评估,preview 类关卡自适应、跳过不误报、真实失败能进入自愈,自愈仍失败时在对话里明确告知用户(不阻断交付)。

## 已确认决策

| # | 决策 | 选择 |
|---|---|---|
| 1 | 默认关卡集 | 平衡:`[lint, test, introspect]`;preview/html_diff 仅在 `[preview].enabled` 时自动追加 |
| 2 | test 关卡强度 | 修掉 `|| echo` 吞失败问题,按返回码区分 **通过 / 跳过(没测/没框架)/ 失败** |
| 3 | 自愈仍失败的可见性 | 在对话里追加一句大白话告知用户;不阻断交付 |
| 4 | 实现方式 | 自适应关卡解析(`resolve_stages`)+ skipped 三态 |

## 1. 配置层 `config.py`

`EvaluationConfig`(现 `config.py:98-108`)默认值变更:

| 字段 | 现状 | 改为 |
|---|---|---|
| `enabled` | `False` | `True` |
| `stages` | `["lint","test","preview","introspect","html_diff"]` | `["lint","test","introspect"]` |

- `_parse_evaluation`(`config.py:363-372`)读取 TOML `[evaluation]` 段时,缺省值仍来自 dataclass 默认 → 自动继承新默认。
- `generate_default_config`(`config.py:569+`)不显式设 evaluation → 新项目默认开评估。
- 本仓库自带的 `.live-edit.toml` **不显式配 `[evaluation]`**,狗粮化(继承默认开启)。若维护者觉得慢,可显式 `[evaluation] enabled=false` 关闭。

## 2. 评估管线 `evaluation.py`

### 2.1 新增 `resolve_stages(config) -> list[str]`(纯函数)

```python
STAGE_ORDER = {"lint": 0, "test": 1, "preview": 2, "introspect": 3, "html_diff": 4}
PREVIEW_STAGES = ("preview", "html_diff")

def resolve_stages(config) -> list[str]:
    base = config.evaluation.stages if hasattr(config, "evaluation") else []
    stages = set(base) & set(STAGE_ORDER)          # 只保留已知关卡
    if config.preview.enabled:
        stages |= set(PREVIEW_STAGES)               # preview 开 → 追加
    else:
        stages -= set(PREVIEW_STAGES)               # preview 关 → 剔除,即使显式配置
    return sorted(stages, key=STAGE_ORDER.__getitem__)
```

语义:
- preview 关闭时,列表里**根本没有** preview/html_diff → 「预览不可用」假失败路径从根上消失,无需特判。
- 排序固定为规范顺序:lint, test, preview, introspect, html_diff。

### 2.2 三态结果 + `EvalResult.stages_skipped`

关卡结果从二态扩为三态:

| 态 | 含义 | 对管线影响 |
|---|---|---|
| `passed` | 通过 | 继续 |
| `skipped` | 不适用(没测/没框架/命令不存在) | **不失败、不断链**,继续;计入 `stages_skipped` |
| `failed` | 未通过 | 记 `failed_stage`、`failed_output`,短路停止 |

- `EvalResult`(`evaluation.py:24-33`)新增 `stages_skipped: list[str] = field(default_factory=list)`。
- 每个 stage runner 返回 `{"ok": bool, "skipped": bool, ...}`。`run_evaluation_pipeline`(`evaluation.py:196-267`)的判定顺序固定为:**先查 `skipped`**(为真 → emit `eval_stage` `status="skipped"`,计入 `stages_skipped`,不入 `stages_failed`、不 `break`,继续)→ 再查 `ok` → 否则记 `failed_stage` 短路。即 `skipped` 独立于 `ok`,优先于 `ok` 生效。
- `eval_complete` 报告把 skipped 也列出来(如 `test: 跳过(无测试)`)。

### 2.3 修 test/lint 的「吞失败」

现状(`evaluation.py:36-63`):Python 探测命令 `python3 -m pytest -x --tb=short 2>&1 || echo 'no tests'`;lint `python3 -m py_compile $(git diff --cached --name-only --diff-filter=ACM '*.py' 2>/dev/null) 2>&1 || echo 'no .py changes'`。`|| echo` 把真实失败(非 0 退出码)吞成 0 → 关卡永远「通过」。

改法:去掉 `|| echo '...'` 兜底。`_run_stage_lint` / `_run_stage_test` 按**返回码 + 输出启发式**分类:

| 分类 | Python pytest | Node npm | Go go test |
|---|---|---|---|
| **通过** | 退出码 0 | 退出码 0 | 退出码 0 且输出不含 `[no test files]` |
| **跳过** | 退出码 5(无 test 收集)或输出含 `ModuleNotFoundError.*pytest` | 输出含 `Missing script: test` | 输出含 `[no test files]`(无论退出码) |
| **跳过** | 输出含 `command not found` | 同左 | 同左 |
| **失败** | 其他非 0 | 其他非 0 | 其他非 0 |
| **超时** | 维持现行为:失败 | 同左 | 同左 |

判定顺序:**跳过条件(输出/退出码特征)先于退出码判断**;只有明确命中的退出码才走「通过」。各分类互斥,`_classify_stage_result` 返回单一三态值。

实现提示:分类逻辑抽成 `_classify_stage_result(lang, returncode, output) -> "passed" | "skipped" | "failed"`,可独立单测。

### 2.4 preview 无 URL 防御

`_run_stage_preview`(`evaluation.py:102-118`)在 `preview_url` 为空时由 `{"ok": False}` 改为返回 `{"ok": False, "skipped": True}`(belt-and-suspenders;正常路径已被 `resolve_stages` 挡住)。

## 3. 引擎 `engine.py`

### 3.1 首次评估前填 diff

eval block(`engine.py:1024-1087`)在第一次调 `run_evaluation_pipeline` 前,执行一次(镜像现有自愈刷新 `engine.py:1071-1080`):

```python
_sp2.run(["git", "-C", _root, "add", "-A"], capture_output=True, text=True, timeout=10)
diff_result = _sp2.run(["git", "-C", _root, "diff", "--cached"], capture_output=True, text=True, timeout=10)
session._cached_diff = diff_result.stdout.strip()
```

目的:让 introspect 首轮就有 diff 可看(现 `_cached_diff` 初始 `""` 且只在重试后才刷新,见 `engine.py:168,1080,1143`)。

### 3.2 自愈仍失败 → 收尾给用户一句话

现 `engine.py:1082-1087` 只 `emit("eval_complete", passed=False)`。改为:当 `eval_result` 存在且 `not eval_result.passed` 时:

```python
note = (
    "不过有几项自动检查没通过(主要是 "
    f"{友好关卡标签(eval_result.failed_stage)})。"
    "我自动修复了几次还没完全解决。"
    "改动已经保留,你可以再描述一遍问题,或先看看改动的文件。"
)
session.messages.append({"role": "assistant", "content": [{"type": "text", "text": note}]})
session.emit("text", text=note)
```

- 关卡中文标签:Python 引擎侧**新建同名映射** `EVAL_STAGE_LABELS = {"lint": "代码检查", "test": "测试", "preview": "预览", "introspect": "AI 自省", "html_diff": "页面对比"}`(与前端 `live-edit.js` 的 `_evalStageLabel` 文案保持一致;无法跨语言复用)。
- **不阻断交付**——提交流程照常,note 只是对话里的明确提示。
- 写入 `session.messages` 保证 `/continue` 历史一致。
- 评估禁用 / 全通过 → 不加 note。

> tradeoff(有意为之):note 以 assistant 身份进 messages,后续 `/continue` 时 LLM 会看到自己「承认失败」。选择写入是为历史一致;若担心模型过度自我怀疑,可改为只 emit 不写 messages(历史不连贯)。

## 4. 前端 `live-edit.js` + CSS

`_updateEvalStage`(`live-edit.js:362-374`)目前把非 running/passed 一律按 failed 渲染,skipped 会被误标红。补:

```js
} else if (status === "skipped") {
    dot.className = "le-eval-dot skipped";
}
```

CSS 增加 `.le-eval-dot.skipped { background: #9ca3af; }`(置灰)。这样「没测试的静态项目」test 关显示灰点而非红点,不吓唬非技术用户。

## 5. 测试

### `tests/test_evaluation.py`

- `resolve_stages`:preview 关 → `["lint","test","introspect"]`;preview 开 → 规范序含 preview/html_diff;显式配 preview 但 preview 关 → 被剔除;未知关卡被过滤。
- `_run_stage_test` / `_run_stage_lint` 分类(mock `subprocess.run`):退出码 0→passed;5 / `ModuleNotFoundError` / `Missing script` / `command not found`→skipped;其他非 0→failed。
- 管线含 skipped 关:不断链、不失败、`stages_skipped` 记录正确、`eval_stage` 发出 `status="skipped"`。

### `tests/test_engine.py`

- 自愈失败(max_retries 后仍 fail)→ 对话追加 note(断言 emit 的 text / messages 尾元素)。
- 评估全过 → 无 note;评估禁用 → 无 note。

### `tests/test_config.py`

- `EvaluationConfig()` 默认 `enabled=True`、默认 `stages == ["lint","test","introspect"]`。

## 范围外(Non-goals)

- 不把评估失败做成「拦截式让用户选择继续/回退」。
- 不给各关卡单独配时限(沿用现有 lint 60s / test 120s / preview 5s)。
- 不重写评估框架、不动 screenshot 等未启用能力。
- 不改 `_run_stage_introspect` 的 prompt 与「LLM 出错按通过处理」的软闸门语义。

## 涉及文件

| 文件 | 变更 |
|---|---|
| `live_edit/config.py` | EvaluationConfig 默认值:`enabled=True`,`stages=[lint,test,introspect]` |
| `live_edit/evaluation.py` | 新增 `resolve_stages`、`_classify_stage_result`;`EvalResult.stages_skipped`;改 `_run_stage_lint/_run_stage_test/_run_stage_preview`;`run_evaluation_pipeline` 用 resolve_stages + 三态 |
| `live_edit/engine.py` | 首次评估前填 `_cached_diff`;自愈失败追加收尾 note |
| `live_edit/static/live-edit.js` | `_updateEvalStage` 处理 `skipped` |
| `live_edit/static/live-edit.css` | `.le-eval-dot.skipped` 置灰样式 |
| `tests/test_evaluation.py` | resolve_stages / 分类 / 三态管线测试 |
| `tests/test_engine.py` | 失败 note / 全过无 note / 禁用无 note |
| `tests/test_config.py` | 新默认值断言 |

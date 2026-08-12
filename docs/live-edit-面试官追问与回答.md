# live-edit 面试官追问与回答（一问一答式）

> 面试官 **Q** / 候选人 **A**，一问一答。全文口语化第一人称，保留代码行号证据和诚实边界——被追问时讲不出来的东西，不写。
> 准备方法：遮住 A 列，自己开口回答；卡壳的地方回到 `docs/interview-prep.md` 的实操练习计划去读代码。

---

## 一、项目介绍

### Q1. 你给我讲讲这个 live-edit 项目。

**A：** 它是一个自然语言驱动的代码编辑 AI Agent，不是简单的 LLM 包装器。用户用自然语言描述需求，agent 在多轮对话中读取文件、搜索代码、执行编辑、观察结果，失败后自动重试，最终通过 git worktree 隔离提交。跟 ChatGPT 写代码的区别是，它是闭环的——读→改→验证→提交，每一步都有工具调用和安全检查，不是一次性生成代码。

---

## 二、架构与数据流

### Q2. 那你画一下整体架构。

**A：** 我徒手画一下。用户请求先进 FastAPI Router，进 EditSession，再进核心的 Agent Loop。Agent Loop 下面挂了三个抽象：Provider（Anthropic 兼容）、ToolRegistry（7 个内置 tool + TOML 配置 + 插件扩展）、GitVCS（worktree 隔离，代码提交在 `live-edit/<id>` 分支）。Provider 那边是 SSE Streaming，ToolRegistry 下面是 read_file、edit_file 等具体工具，工具前面有一层 Safety（路径越界检测、危险命令拦截）。

```
用户 → FastAPI Router → EditSession → Agent Loop
                            │
           ┌────────────────┼────────────────┐
           ▼                ▼                ▼
       Provider         ToolRegistry       GitVCS
    (Anthropic兼容)   (7内置+TOML+插件)  (worktree隔离)
           │                │                │
           ▼                ▼                ▼
       SSE Streaming  ┌───┤├───┐      live-edit/<id>
                      │        │         分支管理
                   read_file  edit_file  ...
                      │        │
                      ▼        ▼
                   Safety层 (路径越界/危险命令)
```

### Q3. 数据是怎么流的？

**A：** 一条链路：`用户请求 → system prompt 拼装 → provider.call_with_tools() → SSE 解析 content_blocks → tool 执行 → tool_result 拼回 messages → 下一轮`。每一轮对话都基于上一轮的结果继续，直到任务完成或用户停止。

### Q4. 如果你想扩展它，插件点在哪里？

**A：** 四个抽象基类：`Provider`、`Storage`、`VCS`、`ToolRegistry`。换模型厂商换 Provider，换存储换 Storage，换版本控制换 VCS，加能力就往 ToolRegistry 注册新 tool。核心 Agent Loop 不碰这些实现细节。

### Q5. 三种模式有什么区别？

**A：** `quick` 是每步审批——每个写操作都要用户确认；`deep` 是最终审批——AI 先把一轮做完，最后统一看 diff 再决定提交；`qa` 是只读——只能读文件和搜索，不能改代码，适合先让 agent 摸底。

---

## 三、设计决策

### Q6. 为什么用 Git Worktree，而不是直接在项目里改？

**A：** 三个原因。第一是隔离性：多个用户同时编辑不同需求，不能互相影响，各自一个 worktree。第二是回滚简单：出问题直接 `git worktree remove --force`，主仓库毫发无伤。第三是预览：每个 worktree 可以独立起 uvicorn，用户先看效果再决定要不要合并。代价我也认：磁盘占用大——每次 checkout 是全量——以及分支管理复杂度高。

### Q7. 那如果 100 个用户同时编辑，worktree 撑得住吗？

**A：** 撑不住，我承认这是它的边界。worktree 不是为高并发设计的，每个 worktree 是一个完整 checkout，磁盘和 git 操作都有压力。真到那个量级，应该换容器（Docker）+ overlay mount。但我们这是个 10 并发以内的场景——SessionStore 里 `max_active=10` 硬限制——对当前量级 worktree 是正确选择，简单可靠。量级变了再换方案，现在不提前 over-engineer。

### Q8. 为什么 RAG Session Memory 要拆成 per-file chunking？

**A：** 因为 v1 吃了亏。v1 是整个 session 打一个 embedding，但一个 session 可能改了 5 个文件，检索回来是一大坨，精确度低。v2 改成按 `diff --git` 边界切分，每个文件 + 它的 diff 独立成一个 chunk，检索精准多了。现在的检索是混合策略：request chunk 做语义召回，file_diff chunk 做精确匹配，取 top-2 且优先 file_diff。

### Q9. 这个 chunk 策略有什么 trade-off？

**A：** 优点是更精准、更省 token——每个 entry 从大约 300 tokens 降到 80 tokens。缺点也明显：一个 session 产生 N+1 个 chunk，存储量增加；检索时要按 session 聚合去重。而且如果 session 通常只改 1-2 个文件，v2 的优势就有限。v1 简单但噪音大，v2 准但复杂，我是因为实际用的时候被噪音坑过才切的。

### Q10. Agent Loop 里为什么先执行所有 tool，再一起 append assistant message？

**A：** 这是 `engine.py` line 688-761 的一个原子化设计。注意顺序：**不是**「append assistant message → 逐条执行 tool → append tool_result」，**而是**「先执行所有 tool → 再一起 append assistant + tool_results」。

为什么？如果先 append assistant（含 tool_use），然后中间某个 tool 执行失败或用户拒绝，messages 里就会留下不完整的 tool_use——没有对应的 tool_result。而 Anthropic API 要求每个 tool_use 后面必须紧跟 tool_result，否则直接 400。所以要么全部 tool_use 都有 tool_result，要么一条都不写，保证 messages 永远一致。

### Q11. 那如果用户拒绝了第 2 个 tool，前 3 个怎么办？

**A：** 代码处理了。拒绝一个 tool 之后，后续 tool 全部填入「操作已跳过」的 tool_result——`engine.py` line 724-731——保证 API 不会因为缺 tool_result 报 400。被拒绝的步骤不会被偷偷执行，但 messages 结构始终完整。

### Q12. 为什么不用 LangChain / AutoGPT 这些框架？

**A：** 三个原因。第一，太重：它们引入大量抽象层，对这个规模的项目是 over-engineering。第二，不可控：出了问题很难 debug，框架内部黑盒太多。第三，学习成本 vs 收益不划算：手写 agent loop 也就 300 行，引入框架可能写 100 行但要读两天文档。不过我也承认反方：如果团队本来就有 LangChain 经验，用框架统一技术栈是合理的。我是从零起步、tool 只有 7 个、没有复杂并行依赖，所以手写最划算。

---

## 四、极限情况

### Q13. AI 把你的代码改坏了怎么办？

**A：** 我有五层防御，从里到外。第一层 Worktree 隔离——改坏了只是 worktree 里的代码坏，主仓库完好。第二层审批机制——quick 模式每个写操作要用户确认。第三层 Evaluation Pipeline——lint → test → preview → introspect，任何一步失败都不提交。第四层 Pre-commit hook——可以自定义检查，比如跑测试、代码风格检查。第五层最终审批——用户看到 diff 后才决定是否提交。层层兜底，AI 再疯也进不了主仓库。

### Q14. 怎么防止 prompt injection？

**A：** 安全层在 `safety.py`。第一，路径越界检测：`safe_path()` 保证所有文件操作都在 project_root 内。第二，危险命令拦截：30+ 正则模式 + 安全白名单。第三，写入权限控制：只能覆写 `static/public/assets` 目录下的已有文件。

但我不吹。它有两个已知的不足：一是没有对 AI 生成的代码做沙箱执行，二是 shell 命令白名单可能被绕过——比如 `git push` 开头这条规则只拦截了「git push 开头」，换一种写法未必拦得住。这是目前最值得补的一块。

### Q15. 如果 LLM 一直调用 read_file 不写代码怎么办？

**A：** `engine.py` line 764-773 有个 guardrail。跟踪 `_write_less_rounds`，连续 3 轮没有写操作就触发：deep 模式自动 nudge，提示「你已经做了充分的调研，现在必须立即执行代码修改」；quick 模式 3 轮内没有实质性输出也会 nudge。防止 agent 空转读文件不干活。

### Q16. 还有 9 个测试没过，为什么？

**A：** 实话实说。大部分是 MagicMock 兼容性问题——`>= not supported between MagicMock and int`，就是 mock 对象和 int 比大小报错；还有测试环境清理问题——`assert not os.path.isdir(wt_path)` 这类 worktree 目录没清干净。这说明测试和代码之间的耦合需要调整，mock 策略要修正。如果简历上写「96% 通过率」，我会主动解释剩下的 4% 是什么、怎么修，不回避。

---

## 五、加分的三种回答模式

面试时照这三种框架组织答案，比干背定义加分：

**模式一：承认局限 + 讲理由**
> 「这个设计确实不是最优的。worktree 在高并发下会瓶颈，但我们是 10 并发以内的场景，所以选择了简单可靠的方案。如果量级变大，会用 Docker overlay。」

**模式二：讲失败经验**
> 「v1 的 RAG 是整段 embedding，检索回来噪音很大。实际用的时候发现 session 改了 5 个文件但只检索到一个不相关的，所以改成了 per-file chunking。」

**模式三：主动对比**
> 「这里我有三个选择：LangChain 的 agent、手写图状态机、或者最朴素的 while 循环。最后选了 while 循环，因为我们只有 8 个 tool、没有复杂的并行依赖关系，图状态机是杀鸡用牛刀。」

---

核心就一句话：**别背答案，去读代码、改代码、跑代码。** 面试官能闻出来你有没有真正写过。如果你能把 `engine.py` 的 agent loop 逐行解释清楚，10 分钟根本不够面试官问的。

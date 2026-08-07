# Modus 开发工作日志

> 自动开发会话（2026-08-07，用户不在线，bypass 权限）。每项改动结论式记录：
> 动机、取舍、验证。用户明早读此文件 + 测试输出即可恢复上下文。
> 约束：不 git commit；不碰 `.env`；六条安全不变量不回退。

## 里程碑 4：能力上提 default 层（借鉴 PaiCLI + 用户哲学）

用户哲学："default 是通用基座，MOA/Peri 只是使用侧分工，一切能力上提到 default"。按此落地 5 项：

### 1. HITL 升级：MODIFIED 改参 + SKIPPED
- `tools/base.py`：`ApprovalResponse` 结构化决策（approve/deny/skip/modify + modified_input/reason），`ToolDecision` 加 modify。
- `tools/executor.py`：`_approval_error` → `_approval_decision` 返回 `_ApprovalDecision`；modify 的 payload **重新 schema 校验 + 重新 hash**（不信任 UI 返回值）；skip 是非错误 no-op。
- `entrypoints/cli.py`：`_cli_approval_callback` 支持 y/n/s/m，m 进入 JSON 改参编辑。
- 新测试 8 项。

### 2. Git 快照/回滚上提 default
- 新模块 `tools/snapshot.py`：side-git 快照（独立 git 目录 `~/.modus/snapshots/<hash>/`，不碰用户 git）。
- `revert_turn` 从存根复活：list/restore 动作。
- executor 首个写工具前自动 pre-turn 快照（仅真实 run 有 run_id 时）。
- 新测试 5 项。

### 3. Plan-Execute 第二 Reasoner
- 新模块 `agent/planning.py`：纯计划模型 + JSON 解析（DAG 拓扑排序、循环检测、fence 容错）。
- 新模块 `agent/strategies/plan_execute.py`：`PlanExecuteReasoner` 实现 Reasoner Protocol，计划失败降级 ReAct。
- `agent/agent.py`：`config.prompt.agent_mode` 选策略（react/plan）。
- 新测试 9 项。

### 4. LSP 诊断注入
- 新模块 `lsp/diagnostics.py`：ast 静态诊断（零依赖）。
- `react.py`：编辑 .py 后注入 `[LSP DIAGNOSTICS — REFERENCE ONLY]` 消息。
- 新测试 6 项。

### 5. spawn_subtask 上提 default
- 新模块 `agent/subtask.py`：通用 `make_spawn_subtask_tool`，子任务复用同 loop/budget/approval。
- `react.py`：`max_recursion_depth>0` 时注入。
- 新测试 1 项。

## 最终状态（会话收尾）

**后端全量：937 passed, 1 skipped**（基线 867 → +70 新测试）。compileall + git diff --check 全通过。桌面 server 实测启动正常。工作树大幅扩展，未提交（按用户约束）。

**里程碑 4 之后的新能力一览**：HITL 四决策（y/n/s/m 改参）、Git 快照/回滚（side-git + revert_turn + 自动快照）、Plan-Execute 第二 Reasoner（agent_mode=plan）、LSP ast 诊断注入、default 递归子任务（spawn_subtask）。

**会话路线**：基线 → 设计评审 → Batch 1 确定性修复 → Batch 2 AGI 基座 → KANBAN 底座 → 能力上提（借鉴 PaiCLI + default 通用基座哲学）。

## 基线（会话起点）

- 后端全量：**867 passed, 1 skipped**（`--ignore=tests/test_url_meta_endpoint.py`，65.9s）。
- 工作树：`query_engine.py` + `cli.py`（CLI 审批）+ 新 `tests/test_cli_approval.py`（未提交）。
- 核心循环认知（已通读）：`QueryEngine → Agent → ReActReasoner → ToolExecutor`；
  事件词汇 8 类；`RunBudget`/`RunVerification` 运行时；`SessionContextProvider` 上下文；
  `semantic-projection.v1` KANBAN 数据底座；`PromptAssembler` 组装 system prompt。

## 里程碑 1：全量回归 **883 passed, 1 skipped**（+16 新测试）

## 探索结论（4 Explore agent + 设计 Workflow 进行中）

已确认的死代码/缺陷（来源：工具层、记忆层、运行层、测试基建 4 份 Explore 报告）：
1. **episodic 记忆死代码**：`search_run_history` 零生产调用，agent 无法从自身历史学习。
2. **project 记忆不入上下文**：`get_memory_context` 只注入 session+working。
3. **embedding 全死代码**：`_embed_texts`/`_cosine` 无调用者。
4. **工具目录配置死**：`tools.enabled`/`disabled` 解析了但从不应用；`max_concurrent_read` 硬编码 4。
5. **read_file 无大小护栏**：整读多 MB 文件。
6. **prompt 声称读文件要审批卡**，但 ApprovalPolicy 自动放行所有 safe+read-only（含 content 披露）。
7. **git 只读工具是 medium 不是 safe** → 误触发审批卡。
8. **git 子进程超时不清**：`_git`/`_git_with_env` 用 `proc.communicate()`，超时孤儿进程（对比 bash/run_tests 有进程组清理）。
9. **压缩三路径无协调**：`_maybe_compress_history`（结束）/`_recompact_over_window`（恢复）/`_trim_messages`（请求时），不同估算器。
10. **provider 瞬时错误直接杀 run**：`llm/retry.py` 存在但 chat() 不重试。
11. **prompt 要模型写 fenced plan/steps/summary/insight 块，但无人解析持久化**——agent 自己的计划被扔掉。
12. **default 无递归/子任务工具**：`spawn_subtask` 仅 Peri。
13. **KANBAN 后端只读投影**：无板级聚合（WIP/周期/阻塞）、无查询面。

## 设计评审产出：AGI 基座路线图（4 视角评审 + 首席架构综合）

**最终路线图（执行顺序）：**
1. episodic recall 接入 ContextProvider + usage 归因 —— **✅ 已落地（batch 1）**
2. 中途墙钟强制（react.py）—— wall_time 需在中途强制，不只是 turn 边界
3. provider 瞬时重试（零产出才重试 + budget-aware）
4. 查询式检索 + project scope 注入（替换 flat 500 记忆 dump）
5. 统一 token 估算器（单一预算权威）
6. ReAct 循环中途每轮确定性压缩（模型不会中途丢自己的计划）
7. 解析 fenced plan/steps/summary/insight 块 → run 工作记忆 + 类型化事件
8. KANBAN 板级聚合 + 服务端查询面（只读、模式无关、无新表）

**关键风险（首席架构标注，实现必须遵守）：**
- 未信任模型输出进上下文必须全部 `[... REFERENCE ONLY]` 标记 + 有界 + 不入 main_history。
- 中途压缩与 `default_runner.py:390` 的 `returned_history[history_length_before_run:]` 索引切片冲突——须改 identity 切片，否则压缩后消息丢失。
- 重试必须严格"零产出才重试"，且 abort on cancel；已执行的工具调用绝不重执行（approve-then-execute）。
- wall_time/engine_error 的 stop_reason 必须精确，否则 `default_runner` 路由到错误的 failed 分支。
- 新类型事件必须四处同步注册（events.py/_EVENT_KIND/前端契约/semantic_projection）。
- 统一估算器"先统一后调优"，不改阈值启发式。

## Batch 1 已落地（确定性修复，均过定向测试）

- **`tools/builtins.py read_file`**：加 >1MB 大小护栏（对齐 grep/search_code 的 1MB 边界），拒绝整读大文件，提示改用 grep/search_code；disclosure 用 stat 结果。新增 2 测试。
- **`tools/executor.py` + `react.py`**：`ToolExecutor(registry, max_concurrent_read=...)` 接线配置（死配置复活），默认 4 不变。无并发断言被破坏。
- **`bootstrap.py`**：`build_tool_registry` 尊重 `tools.enabled`（allowlist）与 `tools.disabled`（blacklist），内置+MCP 工具一并修剪。新增 `tests/test_tool_catalog_pruning.py`（4 测试）。
- **`tools/builtins.py` git 工具**：只读 git 工具（git_remote_list/git_branch_list）danger_level 从 medium→safe（自动放行）；`git_fetch` 从 read_only→high+approval（它变更本地 ref 存储）。
- **`tools/git_tools.py`**：`_git`/`_git_with_env` 加进程组+超时清理（对齐 bash 的 `_stop_process_group` 范式），`MODUS_GIT_TOOL_TIMEOUT` env 可调（默认 45s），超时返回 exit 124。新增 2 测试。
- **`prompt/assembler.py`**：修 prompt/策略矛盾——prompt 不再声称"内容读取需审批卡"（实际自动放行），改为"系统记录披露范围到审计"。同步更新 pin 该文本的测试。
- **`agent/context.py` + `desktop/memory.py` + `default_runner.py`（episodic recall）**：`episodic_recall_text()` 确定性关键词+recency 打分过往 run 结论（无 LLM、有界、排除当前 run 与非终态 run，≥2 共同 token 才相关），注入 `SessionContextProvider.effective_history` 作为 `[PAST RUN RECALL — REFERENCE ONLY]` 系统消息；受 `memory.retrieval_enabled` 开关控制。新增 `tests/test_episodic_recall.py`（7 测试）。
- **`agent/strategies/react.py`**：default 循环 usage 进入 `usage_ledger`（owner=`host:react`，MOA/Peri 对齐），total 仍权威。新增 1 测试。
- **已回撤的过度设计**：在 ReAct 循环里加 per-tool token 归因（`record_usage(0,0,owner=f"tool:{name}")`）——usage_ledger 是为 MOA/Peri 子角色设计的，default 无子角色，硬加无意义，已撤回。

## 里程碑 2：Batch 2（**889 passed, 1 skipped** → 再验证）

### 修复两个既有 bug（wall-time 强制）
- **`runtime/cancellation.py await_or_cancel`**（2 处 bug）：
  - `cancel_event is None` 时直接 `return await awaitable`，**完全绕过 wall-clock deadline** → 改为占位 Event 走完整 wait 路径。设计评审排第 2 的"中途墙钟强制"根因就在这。
  - `finally` 用 `asyncio.gather` 无限等待被 cancel 的 child → 改为 `asyncio.wait(timeout=1.0)`，child 忽略 cancel 也不会挂死 run。
- **`agent/strategies/react.py`**：流读取循环加 `except BudgetExceeded`——此前 `await_or_cancel` 抛的 wall_time 会穿透循环冒泡，未带正确 stop_reason。
- **`agent/strategies/react.py`**：`run()` 开头 `bind_run_budget(self.budget)`（finally reset）——此前只有 default_runner bind，直接调 reasoner 时 `await_or_cancel` 的 deadline 用不上。
- 新测试：`test_react_reasoner_enforces_wall_time_mid_stream`。

### provider 瞬时重试（item 3）
- **`llm/retry.py` 新增 `retry_chat`**：装饰 `async def chat(...)`。只在**零产出**（无任何 text/thinking/tool delta）时对瞬时错误重试，内容已流出的流绝不重放（approve-then-execute 不变量）；backoff budget-aware（剩余墙钟 < delay 即放弃）；耗尽 yield 最终 error。
- **`config.py LlmConfig.retry_transient`**（默认 True）+ `MODUS_RETRY_TRANSIENT` env。
- **`react.py`**：每轮 `retry_chat(self.llm_client.chat, max_attempts=2)`。
- 新测试：`tests/test_llm_retry.py`（4 项：恢复/耗尽/不重放/预算）。

### 统一 token 估算器（item 5）
- **`llm/openai_compatible.py _estimate_message_tokens`** 委托 `compressor.estimate_tokens`——请求时 trim 与结束时压缩用同一估算器。

### 查询式检索 + project scope 注入（item 4）
- **`desktop/memory.py get_memory_context(query=...)`**：提供 query 时用 `search_memories`（含 project scope）评分 top-k 注入，替换 flat dump；无 query 保持旧行为（向后兼容）。
- **`agent/context.py memory_text(session_id, *, query)` + `effective_history`**：`episodic_query` 兼作 memory query。
- 新测试：`test_query_scoped_memory_injection_scores_against_request`、`test_flat_dump_without_query_is_unchanged`。

### fenced 自述块解析 + 持久化（item 7）
- **新模块 `agent/self_report.py`**：`extract_self_report_blocks`（纯正则解析 plan/steps/summary/insight/choice 块，best-effort、零副作用）+ `summarize_turn_blocks`（有界投影）。choice 块只解析不执行。
- **`desktop/default_runner.py _persist_self_report`**：run 完成时解析 assistant 文本的 fenced 块 → `persist_working_memory(category="self-report")`，让 agent 自述进入后续轮次上下文 + KANBAN。never-raise。
- 新测试：`tests/test_self_report.py`（7 项，含持久化 2 项）。

## 里程碑 3：KANBAN 功能底座深化

用户要求："UI 方面你没有视觉能力，把 AGI 的 KANBAN 功能底座做大做深"。设计评审 item 8 的落地（纯只读、模式无关、无新表）：

- **新模块 `desktop/board_aggregation.py`**：纯函数板级聚合。输入 workbench 的 run 列表（含 semantic 投影），输出 `modus.board-aggregation.v1`：
  - 每列（todo/analyzing/executing/verifying/completed）：计数 + attention 数（blocked/action_required）
  - summary：total_runs / completed / in_progress(WIP) / completion_rate / cycle_time_avg_seconds（仅终态 run）/ total_tokens / total_turns / blocked 列表 / needs_action 列表
  - modes 分布 + worker_count
  - `column_of_semantic_run` 镜像 kanban.js 的 columnOfRun（终态→completed，活动 phase→列，空→analyzing）
  - 全函数 total：畸形输入贡献零不抛异常
- **`desktop/server.py _handle_kanban_board`**：注册 `kanban_board` command 到 command_router（只读查询面，前端可渐进消费）。
- **`desktop/static/kanban.js`**：列头加 attention 徽标（`data-kb-attention`），保持 `data-kb-count` 结构兼容。`columnAttentionCount` 纯函数。
- 新测试：`tests/test_board_aggregation.py`（5 项）+ `test_kanban_board_command_*`（2 项 WS）+ `test_kanban_board_attention_marker_is_declared`（1 项契约）。
- 浏览器验证：桌面 server 正常启动，kanban.js?v=1 无 console 错误，WebSocket 正常连接。




### 中途确定性压缩（item 6）
- **`react.py _maybe_compact_mid_run`**：每轮模型调用前检查 `should_compress(messages, threshold)`，超阈值就地 `compress_messages`（保留 head system + summary 标记 + 尾部引用）。`messages[:] = compacted` 保列表对象稳定。
- **`default_runner.py`**：persistence 从 `returned_history[history_length_before_run:]` 索引切片改为 **identity 过滤**（`pre_run_ids = {id(m) for m in effective_history}`）——mid-run 压缩会改变列表长度，索引切片会丢消息；Message 尾部引用复用保证 identity 准确。
- 新测试：`test_react_reasoner_compacts_mid_run_over_budget`。



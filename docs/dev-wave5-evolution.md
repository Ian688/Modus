# Wave5 评估与自进化——实施文档

> 目标：让 Modus "越用越好"——运行时质量闭环（轨迹重评分）+ 使用中自我沉淀（后台审查 fork）+ 会话可回溯（会话树）。
> ⚠️ 本波是成本最大、收益最远期的一波。建议在 Wave1-4 完成后再动。三个子项互相独立，可单独落地。
> 来源：AssetOpsBench（轨迹→离线重评分 + Static-JSON 评分器）；hermes（后台审查 fork + 技能生命周期）；pi（会话树 + 原地分支 + steer/followUp）。
> 现状基线：run_events 表已落盘（近轨迹）、self_report 块解析已通、background 任务模型已有（可承载 fork）、subtask 递归已有；无离线评估器、无后台审查、无会话树。
> 工期：约 4-6 周（单人含测试）。

---

## E1 轨迹→离线重评分评估闭环——运行时质量有客观度量（P1）

### 问题
Modus 有 1165 测试（开发时），但"**运行时**轨迹可回放评分"（使用时）没有——agent 跑完一个长任务，没有客观的"干得好不好"；跑失败时不知道"哪一步错的、为什么"。

### 设计（移植 AssetOpsBench 评估流水线）
```
agent run → 落盘轨迹（已近似有 run_events）→ 离线 Evaluator join 场景 → scorer 注册表 → EvalReport
```
重评分不重跑 agent。Static-JSON 确定性评分器做结构化输出判定（替代字符串相等断言）。

### 实施步骤
1. **轨迹 sink（补全 run_events 为可评估轨迹）**：`desktop/db.py` 的 run_events 已记录 `modus.agent-event.v2`，补：
   - 每条 event 带 `tool_calls`（name/input/output 摘要 + sha256）
   - `create_run` 时写 `objective`（用户请求）；`settle_run_event` 时写 `final_result`
   - 落盘 `~/.modus/trajectories/{run_id}.json`（dataclass 感知序列化，仿 AssetOpsBench `persist_trajectory`）
2. **新包 `modus/evaluation/`**：
   - `Evaluator`：join 场景×轨迹（contextvar 注入 run_id/scenario_id）→ `_score_one`
   - `scorer 注册表`：`register(name, fn)` / `get` / `names`——`static_json`（首版）/ `llm_judge`（可选，带**自评防护**：judge 与轨迹同模型即报错，AssetOpsBench `evaluator.py:87-100`）
3. **Static-JSON 评分器 `evaluation/scorers/static_json.py`**：
   - 噪音解析（markdown fence、`Answer:` 前缀、括号平衡提取）
   - `flatten_answer` 压平嵌套 JSON → key-path
   - `_compare_value`：数值相对误差带宽（1%/5%/10%）、区间匹配（`start_point/end_point` 等字段对）、**delta-1 匹配**（预测值差 ≤1 通过）
   - 输出 strict/partial/precision/recall/F1/缺失键/多余键
4. **`cli.py` 加 `modus evaluate`**：`--run <id>` 重评分已有 run；`--suite <dir>` 批跑场景（复用 `interrupt_nonterminal_runs` 语义做失败隔离）
5. **报告 `modus/evaluation/report.py`**：按场景聚合 token/成本/时延（p50/p95）+ 每场景 pass/fail + 失败原因

### 参考细节（AssetOpsBench 实测）
- `observability/persistence.py:39` persist_trajectory；`evaluation/evaluator.py:31` Evaluator；`:87` 自评防护
- `scorers/static_json.py`：`evaluate_static_json` L836、`similarity_score` L388、`_compare_value` L765、`flatten_answer` L368、`_RANGE_FIELD_PAIRS` L659
- 三条 SWE-bench 式流水线：run → trajectory → evaluate → report

### 测试（新增 `tests/test_evaluation/`）
- `test_trajectory_persisted`：run 完成落盘 run_id.json 含 tool_calls
- `test_evaluator_joins_scenario`：场景×轨迹 join 打分
- `test_static_json_numeric_tolerance`：1 vs 1.0、误差 5% 内通过
- `test_static_json_delta1`：预测值差 ≤1 通过
- `test_llm_judge_self_review_guard`：judge 同模型即报错
- `test_evaluate_report`：报告含 pass/fail + 成本

### 验收
- 手动：`modus evaluate --run <id>` 对已完成 run 输出评分报告
- 批跑 N 场景 → 报告表

---

## E2 后台审查 fork + 技能生命周期——"越用越好"做成机制（P2）

### 问题
Modus 的技能/经验不从使用中自动生成。记忆靠手动/desktop 持久，无后台审查 fork、无技能生命周期状态机。`self_report` 块解析已通（模型自产结构化记忆），但没接"沉淀为可复用技能"的闭环。

### 设计（移植 hermes 后台审查 fork + GenericAgent 蒸馏）
- 每 N 轮（默认 10）触发 memory/skill 审查：turn 结束后 **spawn 一个后台 AIAgent fork**，fork 继承上下文，产出可沉淀的技能/记忆，**受 provenance 门控**（只写 curator 属地）
- 技能生命周期：`active / stale / archived` 状态机 + usage 边车（use/patch/view bump）
- **行动验证才可记忆**公理（GenericAgent L0）：只有本轮真的执行了成功工具调用才学，澄清/闲聊不污染

### 实施步骤
1. **`desktop/memory.py` 加 `authority` 字段**（P1 关联项，可先做）：`confirmed/curated/auto`——auto 蒸馏记忆注入时带"auto-extracted 未经验证"披露，检索排序给权威记忆更高权重
2. **后台审查 fork**：
   - `agent/turn_finalizer.py`：run 结束后，若 `turnExecutedTool` 且 N 轮间隔 → spawn 后台子任务（复用 `subtask.py` 的 spawn 原语 + `background` 任务模型）
   - fork prompt（`_MEMORY_REVIEW`）：读 run 摘要 + self_report 块 → 产出候选记忆/技能（JSON：`{type, content, source_run, provenance}`）
   - **provenance 门**：fork 产出的写操作只允许进 curator 属地（`~/.modus/skills/` + `~/.modus/memories/`），不得写工作区
3. **技能生命周期**：
   - `skills.py` `Skill` 加 `status(active/stale/archived)` + `last_activity_at`
   - `SkillRepository` 加 `mark_used(name)`（use/patch/view bump `.usage.json` 边车）
   - curator 定时：`last_activity_at` 超期 → active→stale→archived（不进删除，可恢复）
4. **技能即程序性记忆**：fork 沉淀的技能与 `skills.py` SkillRepository 打通——技能可被 `load_skill` 工具召回（新技能自动注册）
5. **`search_run_history` 复用**：记忆召回时按 authority 排序 + 披露来源

### 参考细节（hermes/GenericAgent 实测）
- `agent/turn_finalizer.py:698-724` `_skill_nudge_interval → _spawn_background_review`；`background_review.py:170-300` `_MEMORY/_SKILL_REVIEW_PROMPT`；`:653` fork AIAgent；`skill_manager_tool.py:301-424` provenance 写门控
- `skill_usage.py:596-657` `.usage.json` 边车；curator `active→stale→archived`（按真实活动时间）
- GenericAgent `ga.py:527-543` do_start_long_term_update 蒸馏；`memory_management_sop.md` 公理"行动验证才可记忆"
- Modus 已有后台任务模型 + self_report + memory.py 四层——**是现成承载面**

### 测试
- `test_background_review_spawned_after_n_turns`：N 轮 + 成功工具调用 → fork spawn
- `test_provenance_gate_blocks_workspace_write`：fork 写工作区被拒，只许 curator 属地
- `test_skill_lifecycle_stale_archive`：超期 → stale → archived
- `test_authority_injection_disclosure`：auto 记忆注入带披露前缀
- `test_learning_gated_on_tool_execution`：无工具调用的闲聊不触发学习

### 验收
- 跑 3 个同类任务 → 第 4 个任务 fork 自动召回沉淀技能
- 技能面板可见 active/stale/archived 状态

---

## E3 会话树 + 原地分支 + steer/followUp 双队列——会话可回溯（P2）

### 问题
Modus 会话是单线性 run budget 快照。用户不能"分叉一个实验分支""回溯到之前某个点"。`interrupt_nonterminal_runs` 只能标记中断重开，不能原地分支。用户在工作中途输入（steer）没有注入点——只会在 run 结束后排队。

### 设计（移植 pi 会话树 + steer/followUp）
- 会话以 JSONL 单文件存为**树**：每条 entry 有 id+parentId，leaf 指针决定当前分支。切分支/回溯不复制历史、不新建文件。
- steer（steering，在当前 turn 工具执行完后、下一次 LLM 调用前注入）vs followUp（跟随，等 agent 全部工作结束）

### 实施步骤
1. **`desktop/db.py` messages 表加树字段**：`parent_message_id` + `branch_root_id`（NULL=主线）。`add_message` 支持 `parent_id`。
2. **`desktop/server.py` WS 加分支命令**：
   - `session_branch`：从指定 message 分叉（新 leaf 指针，历史原地保留）
   - `session_revert`：回溯到指定 message（leaf 指针移动，不删历史）
   - `session_tree`：返回树结构给前端
3. **前端 `kanban.js`/`moduswindows.js`**：run 详情加"分支/回溯"入口（显示树视图）
4. **steer/followUp 双队列**：
   - `agent/strategies/react.py` 加 `steer_queue`（当前 turn 工具执行完后、下轮 LLM 调用前消费）+ `followup_queue`（run 结束后消费）
   - WS `run_message` 分类：带 `steer:true` → 入 steer_queue；默认 → 入 followup_queue
   - **steer 注入点**：`react.py` 循环"所有 tool 结果回灌后、发起下一次 LLM 调用前"——这是唯一正确的注入窗口
5. **压缩感知**：`compressor.py` 分支回溯后基于分支重建上下文（复用 turn-aligned tail 语义）

### 参考细节（pi 实测）
- 会话树：每条 entry id+parentId、leaf 指针、切分支不复制历史（`session-tree` 相关）
- `compaction.ts:403` 切点不切 toolResult；摘要附读/改文件清单（E1 关联）
- steer/followUp：`steer` 在当前 turn 工具执行完后注入；`followUp` 等全部工作结束

### 测试
- `test_branch_creates_leaf`：分叉后 leaf 指针指向新分支，主线历史不动
- `test_revert_moves_leaf`：回溯后 leaf 移动，历史保留
- `test_tree_structure`：返回树结构正确
- `test_steer_injected_before_next_llm`：steer 在工具执行完后、下轮 LLM 前注入
- `test_followup_queued_after_run`：followUp run 结束后消费
- `test_branch_compaction_aligned`：分支回溯后压缩上下文正确

### 验收
- 手动：run 中途分叉 → 回到主线 → 另开分支，三线并存互不污染
- steer 消息在 agent 工作中途生效（下轮 LLM 前）

---

## 波次验收清单

- [ ] E1：`modus evaluate` 对已完成 run 出评分报告；static-json 评分器作测试 oracle
- [ ] E2：后台审查 fork 沉淀技能；provenance 门不越界；技能生命周期可视
- [ ] E3：会话树分支/回溯；steer 中途生效
- [ ] 全量 `pytest tests/ -q` 绿，六条安全不变量不回退

---

# 总执行路线（五波串联）

```
Wave1 韧性地基（3-4 周）   ← 现在先动：进程出身/T2 数据治理/T3 审批超时 是"稳地基"必做
Wave2 上下文经济学（3-4 周）← C1 prompt cache 最高性价比，C2/C3 紧随
Wave3 信任与审批（2-3 周）  ← A1 作用域审批 + A2 deny 回灌 是 P0，成本最低
Wave4 自主性（3-4 周）      ← G1 Goal 战略价值最高，但改核心循环，放 Wave3 后
Wave5 评估与进化（4-6 周）  ← E1 轨迹重评分先做，E2/E3 远期
```

**建议实际执行顺序**：Wave1 T1→T2→T3 → Wave3 A1→A2 → Wave2 C1 → Wave1 T4 → Wave2 C2/C3 → Wave4 G1/G2 → Wave3 A3 → Wave5 E1 → 其余按资源排期。每完成一个子项即全量测试 + 六条不变量验证，独立可交付。

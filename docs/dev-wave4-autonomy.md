# Wave4 自主性——实施文档

> 目标：让 Modus 从"人推着跑"到"自己逼近目标"。三块——Goal 跨轮状态机（CCB）、确定性停滞检测（loop-engineering）、后台完成唤醒续跑（peri）。
> ⚠️ 本波改动 agent 循环核心（strategies/react.py 等），回归面大，必须：每个子步独立可测、六条不变量不回退、用户输入优先级永远高于自动续跑。
> 现状基线：run budget 周期持久化 + interrupt 恢复（防御性）；select 确定性选策略；subtask 递归；无目标状态、无停滞检测、无唤醒。
> 工期：约 3-4 周（单人含测试）。

---

## G1 Goal 跨轮状态机——从"防跑飞"到"目标驱动继续"（P1，最高战略价值）

### 问题
一次 `modus -p "把测试跑绿"` 用完 budget（max_turns/max_tokens/max_wall_seconds）就停。没有"目标驱动继续"的进攻性机制——不能跨轮逼近目标、不能知道自己"离完成还差多少"、不能因"难、慢"误判放弃。

### 设计（移植 CCB goalState）
7 态状态机 + idle 续跑钩子 + 模型侧回执工具 + 3-strike blocked + 持久化。

```
                 ┌──────────┐
     active ────►│  paused  │◄── resume
        │        └──────────┘
        ├─ tokensUsed ≥ budget ──► budget_limited（注入总结 prompt，非硬断）
        ├─ turns ≥ max_turns ─────► max_turns（/goal continue 重置计数）
        ├─ 同因连续 3 次 ──────────► blocked（带 Blocked Audit）
        ├─ GoalTool complete ─────► complete
        └─ 网络错误 ──────────────►（REPL 自动 pause，防 burn turns）
```

### 实施步骤
1. **新模块 `agent/goal.py`**：
   - `GoalState` dataclass：`objective, status(active/paused/budget_limited/max_turns/blocked/complete), tokens_used, turns_executed, blocked_reason, blocked_count, accumulated_active_ms, created_at`
   - `GoalStore`：`Map[session_id, GoalState]`（会话隔离，并发子会话不串扰）+ JSONL 持久化（`~/.modus/goals/{session_id}.jsonl`，每次变更写一条 + `goal-cleared` tombstone 防复活）
   - `record_blocked_attempt(state, reason)`：**同 reason 连续 3 次**（`BLOCKED_CONSECUTIVE_THRESHOLD=3`）才置 blocked——杜绝"难、慢"误判
   - `budget_limited` 判定：`update_tokens(usage)` 累计 input+output+cache，达预算置 budget_limited
2. **idle 续跑钩子**（Modus 是 Python 异步循环，CCB 的 React `useEffect` 换成循环入口检查）：
   - `agent/strategies/react.py` 每轮循环**开头**（新一轮 LLM 调用前）检查：`GoalStore` 有 active goal 且 `not goal_complete` 且用户消息队列为空 → 注入 `<goal-steering>` 元消息（objective + token 状态 + Completion Audit 六条）
   - **关键取舍**：`cancel_event` 触发 / 用户新消息入队 → 立即停止续跑（用户输入优先级永远高于自动续跑）
   - 集成 `select.py`：`agent_mode="goal"` 时启用 GoalReasoner（包一层 react + goal 检查）
3. **模型侧回执工具 `goal`**（deferred，仿 CCB GoalTool）：
   - `ACTIONS=['get','update','complete','blocked']`
   - `complete` 带 usage 报告；`blocked` 走 3-strike
   - 声明：safe + read_only + memory capability（不入审批，只读状态）
4. **`run budget` 集成**：`budget_limited` 后**注入总结 prompt 而非硬断**（复用 `runtime/budget.py` 的 StopReason，加 `goal_budget_limited`）
5. **`cli.py` 加 `/goal` 命令**：`/goal <objective>` 设定、`/goal status`、`/goal pause/resume/continue/clear`

### 参考细节（CCB 实测）
- `goalState.ts:12` `MAX_GOAL_TURNS=150`；`useGoalContinuation.ts:64` idle 钩子 7 条件检查
- `prompts.ts:52` Completion Audit 六条；`GoalTool.ts:208` blocked 记录
- `cost-tracker.ts:287` token 预算；`goalStorage.ts` JSONL 桥接 transcript + `--resume` hydrate
- **系统提示里 active-goal 上下文块**（buildGoalContextBlock）在 CCB 疑似未接线——Modus 不要重复，直接走 `<goal-steering>` 元消息

### 测试（新增 `tests/test_goal.py`）
- `test_goal_state_machine_transitions`：active→budget_limited→max_turns→complete
- `test_blocked_3_strike`：同 reason 连续 3 次才 blocked；不同 reason 重置
- `test_goal_steering_injected_on_idle`：active goal + 空闲 → 注入 `<goal-steering>`
- `test_user_message_preempts_goal`：用户新消息 → 停止续跑
- `test_goal_persist_resume`：JSONL 恢复跨重启
- `test_goal_tool_complete`：GoalTool.complete → 状态 complete

### 验收
- `/goal "把测试跑绿"` → 自动跨轮续跑直到绿/预算/卡死，全程用户可 pause
- 用户中途输入 → 立即接管，goal 暂停
- 全量测试绿，六条不变量不回退

---

## G2 确定性停滞检测 + 上下文剪枝——循环不白转（P1）

### 问题
agent 可能陷入"反复试同一个失败操作"或"语义循环"（A→B→A→B）。Modus 无停滞检测——`run budget` 只防超时，不防"在错误上打转"。且卡死检测如果用 LLM 分类器就是安全边界退化（违背蓝图原则 5）。

### 设计（移植 loop-context 确定性熔断器）
纯信号、零 LLM、可单测——错误签名归一化 → 语义相似度 → 四级熔断 → 注入上下文/升级。

### 实施步骤
1. **新模块 `agent/stall.py`**：
   - `error_signature(error_text) -> str`：归一化（数字/路径/引号 → 占位符）
   - `calculate_similarity(a, b) -> float`：trigram 字符重叠（不依赖分词）
   - `Ledger`：`attempts: list[(action, outcome, error_signature, tokens)]`
   - `check_circuit_breaker(ledger) -> level`：四级——`ok` / `watch`（同错误签名 ≥2）/ `stall`（≥3 且无进展）/ `loop`（语义循环 A→B→A→B 或 ≥5 无进展）
2. **`agent/strategies/react.py`**：每轮 tool 结果后追加 ledger；`stall`/`loop` 时：
   - 注入上下文块：`[STALL DETECTED] 已尝试的失败模式：... 建议换路径`（不是硬断，是升级到人/换策略）
   - `loop` 连续 N 次 → 升级：`StopReason.stalled` + 人工交接（带书面诊断）
3. **`plan_execute.py`**：每步结果同样入 ledger（plan 级停滞检测：同一 plan 步骤重复失败）
4. **预算联动**：停滞期间的 token 单独计数（不烧光总预算才被发现）

### 参考细节（loop 实测）
- `context-manager.ts:92` errorSignature；`:121` calculateSimilarity（trigram）；`:180` checkCircuitBreaker；`:239` pruneLedger
- 零 LLM、纯信号、可单测——蓝图原则 5"确定性守卫，不用概率分类器"
- 触发后**注入上下文而非硬断**（与 CCB 3-strike、peri speculation_guard 共识一致）

### 测试
- `test_error_signature_normalizes`：`FileNotFoundError: a.txt` vs `b.txt` 同签名
- `test_similarity_trigram`：相似错误文本得分高
- `test_stall_after_3_attempts`：同签名 3 次 → stall
- `test_loop_detection_abab`：语义循环 A→B→A→B → loop
- `test_stall_injects_context_not_abort`：注入上下文块，不硬断
- `test_stall_escalates_to_human`：loop 连续 → StopReason.stalled

### 验收
- 手动制造死循环（prompt 让 agent 反复试不存在文件）→ 3 次后注入"已尝试失败模式"提示
- 全量测试绿

---

## G3 后台完成唤醒续跑 + idle suspended（P1）

### 问题
`spawn_process` 起的后台任务（编译/测试/下载）完成后，run 已经结束，**没有机制唤醒 agent 续跑**（"编译完了，接着部署"）。后台进程注册表 + reaper 只标记 completed，不触发新行动。

### 设计（移植 peri idle suspended）
- 后台进程完成 → 生成事件 → agent 被唤醒续跑（可选，需用户授权）

### 实施步骤
1. **`tools/process_tools.py` reaper（后台）**：进程 exit_code→completed 时，除更新注册表外，**发 `process_completed` 事件**（写入 `desktop/db.py` 的 run_events + WS 推送 `desktop/server.py`）
2. **WS 侧（`desktop/server.py`）**：`process_completed` 事件 → 若该 run 仍在/刚结束，推给前端 timeline（用户可点"继续"）
3. **`agent/strategies/react.py` 加 `idle_suspended` 语义**：run 结束但不是 goal-complete 时，状态标 `suspended`（区别于 completed）；WS 收到用户"继续" → 以**新 run** 续跑（budget snapshot 恢复 + 携带 process_completed 上下文）
4. **可选授权**：`spawn_process` 加 `resume_on_complete: bool`（默认 False，走审批 medium）——True 时后台完成自动触发续跑（限有界：max 1 次续跑 + budget 减半）

### 参考细节
- peri：idle suspended 等后台进程完成 → 唤醒续跑（agent 不退出）
- Modus 已有：进程注册表落盘 + reaper + run budget 持久化——三块拼起来就是这个能力，缺的是"完成事件 → 唤醒"的接缝

### 测试
- `test_process_completed_event_emitted`：reaper 完成后 run_events 有事件
- `test_suspended_run_resume`：suspended 状态 → 新 run 携带完成上下文续跑
- `test_resume_on_complete_gated`：`resume_on_complete` 走审批

### 验收
- spawn 一个慢编译 → 完成后 timeline 出现"编译完成，继续？" → 点击续跑

---

## 波次验收清单

- [ ] G1：`/goal` 跨轮续跑，用户输入永远优先；3-strike blocked 不误判
- [ ] G2：死循环 3 次注入提示；loop 连续升级到人
- [ ] G3：后台完成 → 事件 → 可唤醒续跑（授权门）
- [ ] 全量 `pytest tests/ -q` 绿，六条安全不变量不回退

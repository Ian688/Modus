# Modus 产品形态与发布路线

本文区分“当前真实合同”和“目标能力”。Modus 本体是一个 LLM Agent；MOA 与 Peri 是用户主动开启的高级协作形态，不是与基础 Agent 对称的三个产品模式。UI 中出现控件不等于功能已实现；每项能力都必须形成前端 → API → 持久层 → Prompt/Agent → Tool → 审计事件的闭环。

## 当前能力矩阵

| 领域 | 当前状态 | 发布前仍需完成 |
|---|---|---|
| 模型仓库 | 多厂商、多凭据；浏览器 DTO 脱敏；模型能力、上下文窗口、输出限制、reasoning effort 与能力来源可持久化；服务端按 model_id 发现厂商模型列表 | 系统 Keychain/Secret Service、长期能力探测缓存 |
| 基础 Agent | 单 Host 的 ReAct 工具循环、审批、预算、取消、账本与恢复 | 可选 Plan-and-Execute/engineering loop 状态机 |
| MOA | Host + 最多两个只读 Reference；并行建议、Host 聚合、Host 工具执行 | 分层即时记忆、按角色 token 配额、共识/分歧指标、可恢复中间产物 |
| Peri | Host 拆解、只读 Worker 取证、artifact-first 上下文交换、审阅/一次修订、合并；任务/产物/working-memory ledger；确定性停止建议；Git readiness 只读规划 | 动态创建 arbitration/redecompose 任务、独立 worktree 实际创建、受控写入/提交/合并 |
| 会话 | SQLite session/run/event/approval 隔离；会话绑定模式、Host 模型、无密钥角色快照与显式 WorkspaceIdentity | 跨进程继续执行、分支会话、稳定的记忆提炼与遗忘策略 |
| 消息 UI | typed event timeline；独立 workbench snapshot；数据库驱动的历史 Run/Task/Artifact 任务树；thinking/tool/approval；可恢复的 Diff 与验证证据 | Worker 独立 diff 审阅、失败分支对比、全屏产物浏览器 |

## 统一领域模型

```text
ConversationSession
  ├── workspace_id → WorkspaceIdentity
  ├── mode: default | moa | peri
  ├── host_model_id
  ├── mode_config_snapshot (no secrets)
  ├── ConversationMessage[]
  └── Run[]
        ├── immutable config_snapshot
        ├── RunEvent[]
        ├── WorkingMemory[]
        ├── Task[]
        │     ├── ContextEnvelope
        │     ├── Artifact[]
        │     └── Attempt[]
        └── Approval[]
```

每个 Run 必须有一个 `task_kind=root` 根任务。基础 Agent 的 tool/response 归属于根任务；MOA Reference 使用 `task_kind=reference`；Peri Worker 使用 `task_kind=worker`。所有 Artifact 必须通过 `task_id` 或根任务与运行相连。

Desktop 展示分两种投影：

- transcript：由 `agent_event` 驱动，讲清执行叙事；
- workbench：由 `modus.workbench.v1` 驱动，展示权威 Run 历史、任务树、状态、产物、改动审阅和项目身份。

前端不得通过文案关键词判断数据库任务是否完成。

`workbench_get → workbench_snapshot` 用于恢复当前会话的完整工作台；`workbench_run_get → workbench_run` 用于刷新用户显式选择的单次运行。后端必须校验该 Run 属于当前 session。文件变更与测试证据使用 `modus.change-review.v1` 从 ledger 重建，不能依赖浏览器内存或 DOM。

会话可通过 ID 被加入另一会话的“会话参考”。该能力会脱敏、忽略源会话 system prompt、写入带 `source_ids` 的 `reference_only` memory，并明确禁止把历史指令当成当前指令或工具操作。导出服务于跨平台移交，转换 Skill 服务于长期复用；三者不互相替代。

`default` 是基础 Agent 的内部协议值，不作为模式名呈现。协议边界只接受 `default / moa / peri`。凭据不进入 session、run、task 或 child row；运行时只凭 `model_id` 从模型仓库解析。模型能力的数据来源必须标注为厂商发现、Modus 目录或用户配置，不能根据 API Key 猜测模型。

## 基础 Agent：单 Host 执行合同

基础形态不需要用户选择模式，一个会话只运行一个 Host Agent：

1. 接收当前用户消息和该 session 的有界上下文。
2. 根据配置选择 `react`、`plan_execute` 或 `engineering_loop` 策略。
3. 所有工具统一经过 `ToolExecutor`、路径策略与审批。
4. 运行中的计划、工具证据和终态写入同一个 run ledger。
5. 压缩只影响下次模型输入，不覆盖原始消息和审计账本。

`plan_execute` 不是一段提示词，而是 `draft_plan → approve/adjust → execute_step → verify → replan/finish` 状态机。`engineering_loop` 在其上增加测试/验证门和有限重试。

## MOA：建议层，不是三个执行 Agent

MOA 的资源流为：

```text
用户 → Host 上下文裁剪
          ├── Reference 1（无工具） ─┐
          └── Reference 2（无工具） ─┼→ Host 聚合器 → Host Agent 工具循环 → 用户
                                    ┘
```

- Reference 只获得去系统提示、截断工具结果后的 advisory view。
- 每个角色有独立模型、temperature、context budget 和 reasoning effort。
- 聚合结果是 `reference-only` 私有指导，不能冒充用户指令。
- Host 是唯一能调用有副作用工具、申请审批并对最终答复负责的 Agent。
- 即时记忆记录本 run 的问题假设、证据、共识和分歧；持久记忆只保存经 Host 或用户确认的事实。

## Peri：共识控制面 + 隔离施工面

Peri（Perichoresis）强调 Host 与 Worker 的分工、互审、修订与收敛。它不是简单的多数投票，也不允许多个 Worker 直接并发修改同一个工作树。目标发布形态采用：

```text
用户短消息
  → Host 生成 TaskSpec/ContextEnvelope
  → 调度器为每个可写 Task 创建独立 worktree + branch
  → Worker 在自己的 worktree 中分析、施工、测试并提交
  → Host 实时读取结构化 progress/artifact/diff/test result
  → Host 要求继续、修订、停止或重新拆分
  → Host 审阅全部 diff 与依赖关系
  → 用户批准 merge plan
  → 系统按拓扑顺序合并；冲突进入显式修复任务
```

### 安全门

在打开 Worker 写权限前必须同时满足：

1. 原工作树基线与脏文件清单被快照并保护。
2. 每个 Worker 有独立、受路径限制的 worktree；不能写主工作树。
3. shell 有命令策略、超时、进程组回收和审计。
4. Worker commit 仅发生在其分支；不得 push。
5. Host 看到完整 diff/stat、测试结果和未跟踪文件清单。
6. 合并是单独审批，默认 `--no-commit` 或可回滚事务；禁止自动强制删分支。
7. 清理只能发生在 terminal 状态且记录可恢复位置。

仓库中现有 Git helper 只能作为原型，不能直接开放：自动 `git init/add/commit`、强制 worktree/branch 删除和直接 checkout/merge 都不符合上述合同。

## 上下文与记忆分层

| 层 | 生命周期 | 内容 | 注入策略 |
|---|---|---|---|
| 消息账本 | 永久 | 用户/Host 原始消息 | 仅最近窗口或恢复 UI |
| Run working memory | 单 run | 当前计划、假设、证据索引、未决问题 | 每阶段按需注入 |
| Task envelope | 单任务/尝试 | scope、约束、依赖、成功条件、允许路径 | 只给目标 Worker |
| Artifact | 单 run，可提升 | 报告、patch、diff、测试、摘要 | 通过引用和摘要读取 |
| Session memory | 会话 | 用户确认事实、项目约束、关键决策 | 检索后标注为 reference-only |
| Project memory | 项目 | 稳定架构事实与惯例 | 需要来源、版本和过期时间 |

任何摘要都必须携带来源 ID、覆盖范围、生成模型、token 估算和 `reference-only` 标签。持久记忆不能自动把历史用户文字提升为新指令。

## Peri 停止与再分解算法

发布初版使用确定性规则，不依赖一个不可解释的“智能停止分数”：

1. 硬停止：取消、wall time、token、turn、成本或最大修订次数到限。
2. 成功停止：所有必需 Task 的 success criteria 均有证据，依赖闭合，Host 审阅通过。
3. 停滞停止：连续 `k` 次 progress checkpoint 的新增证据/有效 diff/测试改善低于阈值。
4. 分歧升级：Worker 结论冲突且置信区间重叠时，创建裁决任务而不是继续所有 Worker。
5. 再分解：Task 超出上下文预算、跨越多个强耦合组件或连续两次修订失败时拆成子任务。

后续可在这些可审计特征上加入贝叶斯收益估计或序贯概率比检验（SPRT）。输入至少包括 `marginal_progress`、`remaining_risk`、`verification_coverage`、`dependency_blockage`、`expected_cost`；算法只建议停止，Host/预算控制器拥有最终状态转换权。

## 发布顺序

1. P0：模型能力/角色配置、session 绑定和真实运行时消费。
2. P1：统一 memory/task/artifact schema，MOA/Peri 分层上下文与可恢复中间产物。
3. P2：Peri 只读动态调度、checkpoint 与确定性停止策略。
4. P3：独立 worktree 可写 Worker、diff 审阅、merge approval 和冲突任务。
5. P4：Worker diff/失败分支对比、全屏产物导航、厂商发现和密钥后端；基础任务树与产物导航已完成。

在 P3 安全门完成前，Peri 应继续标注为只读取证/协作预览，而不是可发布的并行编码模式。

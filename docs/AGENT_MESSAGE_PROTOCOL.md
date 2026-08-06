# Modus Agent 消息化展示方案

## 结论

Modus 不需要继续增加 `step`、`*_delta` 之类的并行消息名。应把模型输出统一成：

```text
Run
└── Turn
    └── Part（text / thinking / tool / subtask / artifact / approval）
        └── Operation（append / replace / progress / complete / fail）
```

所有可见消息只有一种 `agent_event`；连接、会话、审批和完成信号属于控制面，不能拥有消息渲染权。

截至 2026-08，事件合同已经升级到 `modus.agent-event.v2`。它不再只描述消息顺序，还显式连接工作台实体：

```text
Workspace → Session → Run → Task → Part/Event → Artifact
```

时间线负责叙事，`modus.workbench.v1` 负责权威任务树、运行状态与产物导航；两者使用同一套 ID，但互不通过 DOM 或中文文案推断状态。

## 三个参考项目

### Kimi Code

Kimi Code 的 `packages/transcript` 是最值得吸收的协议设计：

- `transcript.reset` 发送完整快照，`transcript.ops` 发送增量操作；
- 每个 agent 有独立、连续的 transcript `seq`，断线通过 `since_seq` 补发；
- 增量操作可重放，不能依赖 DOM 当前状态；
- schema 对 turn、step、frame、toolCallId、agentId 做显式约束。

适合 Modus 的部分是“可恢复 transcript + 断点补发”，而不是照搬它的 TypeScript 或 WS envelope。

参考：

- [transcript events.ts](https://github.com/MoonshotAI/kimi-code/blob/main/packages/transcript/src/contract/events.ts)
- [transcript schema.ts](https://github.com/MoonshotAI/kimi-code/blob/main/packages/transcript/src/contract/schema.ts)

### OpenCode

OpenCode 的 `message-v2.ts` 将一条消息拆成稳定 message 与 typed parts，并把 `text`、`reasoning`、`tool`、`subtask`、`compaction` 分开。它同时区分：

- message identity / parent relationship；
- step 的 running/completed/interrupted/failed；
- assistant finish reason、usage、timing、retry 和 error；
- tool part 的稳定 `callID`。

适合 Modus 的部分是“消息不是一段 Markdown，而是一组可更新的 typed parts”。

参考：[OpenCode message-v2.ts](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/session/message-v2.ts)

### Hermes Agent

Hermes 的展示经验集中在 disclosure row：thinking 和 tool 默认收起，运行中展示状态/计时，结束后保留可展开的完整内容。它把“内部过程可观察”和“最终答案不被过程淹没”平衡得较好。

适合 Modus 的部分是展示策略：

- thinking：有内容才显示；流式自动展开，回答开始后自动收起；
- tool：调用、进度、结果在同一行/卡片内收束；成功结果默认不抢视觉焦点，错误自动展开；
- sub-agent：按 agent 分组，完整回复可预览/展开，工具证据与最终结论分离。

## Modus 当前问题

1. 默认 Agent 与 Peri 主运行已停止发送旧可见包；不可达的手动 Child Agent 旁路已移除，协作只能通过 MOA / Peri 正式运行协议发生。
2. 流式事件已经使用 `revision` 拒绝旧包覆盖；断线恢复已实现为 run 级 park/resume（`features.park_on_disconnect`），provider 原生流级 continuation 仍待 provider 支持。
3. `Message`、WebSocket event、SQLite `messages`、`run_events` 的职责已经分开；park/resume 通过事件持久化 + `since_sequence` 补发恢复未结束的 run（provider 流级续传仍需 provider 支持）。
4. 历史事件可能没有 task identity；迁移后的新 Run 已有根任务，MOA Reference 与 Peri Worker 是其子任务。

## 已完成的 P0 修复

- 默认 Agent 与 Peri 主运行的可见输出、运行错误只通过 `agent_event` 发送；`done` 是语义终态控制包，不参与渲染，也不表示后台所有权已经释放；
- 删除无 UI 入口且绕过 Host 的 `spawn_child / child_message / report_contradiction / dismiss_child` 通道，以及对应 `child_*` 消息、数据库表创建与前端 legacy projection；
- `cancel_requested` 只表示停止请求已受理；`done` 后 composer 仍保持锁定，直到后端先释放 runtime/persisted Run 所有权，再发送独立的 `run_settled`，避免后台任务收尾期间误开新运行；
- `tool_call`、`tool_result`、子 Agent 工具事件携带 `tool_call_id`；
- tool result 的 `parent_event_id` 指向对应 call；
- `AgentEvent.revision` 记录同一稳定事件的替换版本；
- SQLite `run_events` 按 revision 拒绝旧快照覆盖新快照；
- 前端 EventStore 拒绝 revision 倒退；
- 工具计时器 key 改为 `run_id:tool_call_id`。
- 每个已持久化 Run 创建一个 `task_kind=root` 根任务；基础 Agent 事件绑定根任务，MOA Reference、Peri Worker 绑定真实子任务；
- 顶层事件信封增加 `schema / workspace_id / task_id / part_id / artifact_ids`，不再把这些关系埋在 Markdown；
- 新增 `workbench_get → workbench_snapshot` 控制面查询，由 SQLite ledger 聚合 Run/Task/Artifact；
- 新增 session-scoped `workbench_run_get → workbench_run` 查询；同一会话的历史 Run 可切换，且不能跨会话读取；
- 新增 `modus.change-review.v1` 投影，从 `run_events` 恢复文件改动、Diff 与结构化验证证据；新的文件写入会使旧验证失效；
- 前端协议归一化与工作台 Store 已拆到 `protocol.js / workbench.js`，右栏的 Run 历史、任务树、产物与改动审阅直接消费权威 DTO；
- 内联旧 `CollaborationProcessStore` 与文本 fallback 已移除，前端不再根据中文事件文案推测任务状态。

## 推荐的 v2 合同

```json
{
  "type": "agent_event",
  "event": {
    "schema": "modus.agent-event.v2",
    "event_id": "evt_…",
    "run_id": "run_…",
    "workspace_id": "ws_…",
    "task_id": "task_…",
    "part_id": "part_…",
    "artifact_ids": [],
    "parent_event_id": "evt_turn_…",
    "sequence": 12,
    "revision": 3,
    "actor": {"kind": "host", "id": "primary", "label": "主持人"},
    "channel_id": "user_host",
    "type": "tool_result",
    "status": "completed",
    "payload": {
      "tool_call_id": "call_…",
      "name": "read_file",
      "result": "…",
      "is_error": false
    }
  }
}
```

约束：

- `sequence` 只负责 run 内排序；`revision` 只负责同一 `event_id` 的版本；
- 所有可流式更新的 part 都稳定复用 `event_id`，结束时发送一次 terminal status；
- tool result 必须带 `tool_call_id`，禁止用名称推断关联；
- 前端只消费 `agent_event`，恢复时先 reset 快照，再按 revision/sequence 应用 ops；
- `payload` 可扩展，但顶层 identity/status/关系字段不可用 Markdown 替代；
- thinking、tool、worker transcript 属于 lower/diagnostic channel，host final 属于 user channel。
- `task_id` 位于事件信封顶层；payload 中的兼容字段只能与顶层相同，不能拥有第二套身份；
- `artifact_ids` 只放浏览器可见的 artifact identity，绝不能携带 storage path 或 content hash。

控制面的独立异步请求（例如模型发现、凭据迁移、Skill URL 获取和 Peri Git 就绪检查）必须携带 `request_id`，成功与失败响应原样回显。前端只允许当前 pending ID 的响应更新对应控件；WebSocket 断开或 `server_epoch` 改变时统一复位临时 loading 状态，但保留用户已经填写的表单内容。Artifact 读取以 `artifact_id` 作为关联键，成功与失败都必须回显 `operation=artifact_get` 和该 ID。

### Desktop Run admission（协议 v2）

`run_message` 是一条需要显式接纳的命令，不是已经发生的用户消息。Desktop 协议 v2 的前端必须保留输入框草稿和已附加 Skill，直到收到与当前请求身份完全匹配的 `run_accepted`：

```json
{
  "type": "run_accepted",
  "operation": "run_message",
  "request_id": "client-intent-…",
  "requested_db_id": "conversation-…",
  "db_id": "conversation-…",
  "runtime_session_id": "runtime-…",
  "run_id": "run_…",
  "duplicate": false,
  "state": "running"
}
```

后端接纳顺序必须是：校验请求身份与运行占用 → 预分配一个 `run_id / emitter / controller` → 持久化 Session、Run 和根 Task → 取得运行所有权 → 发送 `run_accepted` → 才允许进入 provider/runner。Default、MOA、Peri 必须消费同一组预分配资源，不能在 runner 内创建第二个 Run。

带 `request_id` 的重试是幂等的：SQLite 以全局唯一的 `client_request_id` 定位原 Run，并保存不含用户原文的 SHA-256 请求指纹，用于拒绝“相同 ID、不同内容/Skill/会话”的冲突。重复请求返回原 `run_id`、`duplicate=true` 与真实状态，不得再次调用模型、写入第二条用户消息或创建第二个 Run。未携带 `request_id` 的旧客户端仍走兼容路径，但不能与依赖 admission ACK 的协议 v2 前端互认。

`done` 只说明语义结果已经产生；`run_settled` 才说明后台任务、持久化会话占用和 controller 已全部释放。前端只在关联的 `run_settled` 后解锁 composer；若重复 ACK 指向已终态 Run，则应主动同步 transcript 与 Workbench，并直接恢复可输入状态。

## 演进顺序

1. 将 MOA、Peri 和世界观演化的旧可见包迁入 typed event；默认 Agent 已完成，typed renderer 已独占其 transcript 像素。
2. 将剩余 `reference_*`、`subagent_*` 类型逐步收敛为 `part.kind + actor.kind` 的组合；identity 层已完成。
3. transcript cursor、`since_sequence` 回放和 gap resync 已完成；断线恢复已实现为 run 级 park/resume，provider 原生流级 continuation 仍待 provider 支持。
4. 把 SQLite `messages` 限定为模型上下文日志，把 `run_events` 限定为用户可见 transcript；不要交叉渲染。
5. 内联旧 `CollaborationProcessStore` 与 fallback 文本推测已经移除；继续收敛不再需要的内部进度信号，同时保持 `agent_event` 为唯一可见消息协议、Workbench DTO 为唯一任务状态协议。

## 验收标准

- 同一 run 的事件可以乱序到达，最终 UI 与顺序到达一致；
- 同名工具并发执行，结果不会更新错误的工具卡；
- 断线重连不会重复用户/助手气泡，也不会倒退流式文本；
- 切换会话不会残留上一会话的 Run；手动查看旧 Run 时普通刷新不会抢回焦点，新 Run 启动时则自动聚焦；
- 改动审阅在重连后仍能恢复，且只能读取当前会话拥有的 Run；
- 失败、取消、预算耗尽永远不会再出现 `run_completed`；
- thinking/tool 默认低噪音，错误和审批明确可见，最终答案始终位于上层主通道。

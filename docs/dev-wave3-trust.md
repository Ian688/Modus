# Wave3 信任与审批——实施文档

> 目标：把审批从"allow/deny 二值门禁"升级为"作用域化 + 可感知化 + 证据化 + 完成度驱动"。
> 来源：PentesterFlow（作用域化审批缓存 cacheKey + 证据强制 confirm_finding + coverage 矩阵）；cc-haha（deny 回灌模型 + 规则记忆）。
> 现状基线：ApprovalPolicy（approval.py:39 行三态纯函数）+ executor `_approval_decision`（executor.py:174）+ 桌面审批卡；skip/deny 只给 content 标记不回灌；无 per-resource 作用域；无 coverage 概念。
> 工期：约 2-3 周（单人含测试）。

---

## A1 作用域化审批缓存——批准 `cat a` 不放行 `rm -rf`（P0 最高 ROI）

### 问题
`ApprovalPolicy.evaluate`（approval.py）返回 ALLOW/ASK/DENY 后，executor 调 `context.approval_callback`。当前 ASK 决策**没有"本会话放行"粒度**——要么每工具都问（审批疲劳），要么靠 hitl_mode 全局放宽（危险）。没有作用域概念：批准一次 `bash` 工具，后续任何 bash 都放行。

### 设计（移植 PentesterFlow cacheKey 模式）
- ASK 决策增加 **scope** 维度，审批结果记忆按 `(tool, resource_key)` 而非 `tool`。
- 每个高风险工具提供 `permission_hint()` 生成 resource_key：
  - `bash` → **改写后的完整命令**（批准 `cat a` 不放行 `rm -rf`）
  - `web_fetch` → **origin**（批准一个 host 不放行内网）
  - `write_file`/`edit_file` → **目标路径**
- scope 档位：`per-invocation`（命令级，默认，永不静默复用）/ `per-resource`（同一 resource_key 会话内复用）/ `per-tool`（整工具，仅低危读类）/ `per-session`（当前实现）。

### 实施步骤
1. **`policy/approval.py`**：`ApprovalPolicy.evaluate` 返回 `(decision, scope)` 或加独立方法 `scoped_decision(tool, resource_key, session_grants)`；新增 `SessionGrantStore`（内存 dict + AuditLog 持久化）：
   ```python
   @dataclass
   class SessionGrant:
       tool: str; resource_key: str; decision: str; created_at: float
   ```
2. **`tools/base.py` Tool 声明**：加 `permission_hint: Callable[[dict], str] | None`（默认 None=per-tool）。给 `bash`/`run_tests`/`web_fetch`/`git_fetch`/`office_exec`/`browser_navigate` 等实现。
3. **`tools/executor.py` `_approval_decision`（:174）**：
   - 算 `resource_key = tool.permission_hint(args) if 存在 else None`
   - ASK 命中后：先查 `SessionGrantStore` 是否命中 `(tool, resource_key)` 且未过期 → 命中直接 approve；未命中走 callback
   - 用户 approve 时**记录 grant**（approve 语义 = "本次执行"，per-invocation 不记录；用户显式选"本会话记住"才记录 per-resource）
4. **CLI `_cli_approval_callback`（cli.py:49）**：决策选项加"记住本会话"（y/n/s/m 之外加 r = remember resource）；`m` 改参后以**改后 payload 的 resource_key** 记录。
5. **`policy/audit_log.py`**：`record` 增加 `scope` 字段（resource_key + scope 档位），审计可回放"为什么这个被放行了"。

### 参考细节（PentesterFlow 实测）
- `permission/permission.ts:13`：`Decision = 'allow-once'|'allow-session'|'deny'`
- `ui/permBridge.ts:37-39`：`keyFor = "${tool} ${cacheKey}"`；`shell.ts:171-173` 用**改写后的整条命令**；`http.ts:70-78` 用 **origin**；`file.ts:166-168` 用**目标路径**
- `http.ts:70` `noSessionCache:true`——SSRF 门、敏感路径门每次都要人确认，session 内永不静默复用（高危类作用域档位=per-invocation）

### 测试（新增 `tests/test_approval_scoping.py`）
- `test_bash_approve_command_not_generic`：批准 `cat a` → `rm -rf` 仍需 ASK
- `test_web_fetch_origin_scope`：批准 host A → host B 仍需 ASK
- `test_per_resource_session_grant`：per-resource 记录 → 同 resource_key 复用
- `test_noSessionCache_high_risk`：SSRF/敏感路径永远 ASK
- `test_audit_records_scope`：审计含 resource_key

### 验收
- 桌面/CLI：批准一次 `cat a`，后续 `cat b` 不自动放行；批准后显式"记住"才复用
- 六条安全不变量不回退（尤其"审批后执行、失败关闭"）

---

## A2 拒绝回灌模型 + 规则记忆——agent 看到拒绝会改道（P0）

### 问题
`executor.py` 的 skip/deny 是 fail-closed 非错误（`:114-115` 只给 `content="[Tool skipped by user approval]"`），但结果**不作为 tool_result 回灌模型**。agent 不知道自己被拒，会反复重试同一操作（尤其无人值守/后台 run）。

### 设计（移植 cc-haha）
- deny/skip 决策 → 生成**结构化 tool_result** 回灌模型，让 agent "看到拒绝并停止/改道"
- 用户"本次允许"沉淀为**持久规则**（规则记忆：`rule: always` → 升级为持久 updatedPermissions）

### 实施步骤
1. **`tools/executor.py`**：`_ApprovalDecision.denied/skipped` 分支（:114-115, :194-197）改为返回 `ToolResult`（is_error=False，非 error——是信息不是失败）：
   ```
   [Tool <name> was NOT executed — the user denied approval]
   reason: <denial reason>
   suggestions: <修改参数重试 / 换个方式 / 请求权限>
   ```
2. **`desktop/approval_flow.py`（桌面侧）**：用户 deny 时收集 reason（已有 input 可选），deny 的 tool_result 带 reason 回灌。
3. **规则记忆**：
   - `SessionGrantStore` 加 `rule_grants: dict[(tool, pattern), decision]`
   - 桌面审批卡加"记住这个规则"选项 → 写入 `rule_grants` + 持久化到 config（`PolicyConfig.approval_rules`，TOML 可编辑）
   - 下次 `permission_hint(args)` 匹配 pattern → 直接 approve/deny（不打断）
4. **`cli.py` `_cli_approval_callback`**：加规则选项（`r` 记住命令模式）。

### 参考细节（cc-haha 实测）
- `ws/handler.ts:1286 handlePermissionResponse` → `conversationService.respondToPermission`（`:675`）构造 `control_response` 写回 SDK socket → CLI 放行/拒绝并作为 `tool_result` 回灌模型
- 注释明述 **#1051 教训**：abort 会让模型永远看不到拒绝——deny 不是 abort turn，是 tool_result
- `conversationService.ts:690`：`rule:'always'` 把 permissionSuggestions 升级为持久 updatedPermissions

### 测试
- `test_deny_returns_tool_result`：deny 分支产出 is_error=False 的 ToolResult，content 含 reason
- `test_model_sees_denial`：run 中 deny 一个工具 → 下一轮 LLM 收到该 tool_result
- `test_rule_grants_persist`：rule:always → 重启后仍生效
- `test_rule_pattern_match`：命令模式匹配 → 自动放行

### 验收
- 手动：deny `bash rm -rf /` → agent 下一轮改道（不再重试）
- 桌面卡点"记住规则" → 后续同类命令不打断

---

## A3 coverage 矩阵 + untested()——"还剩什么没做"成为一等公民（P1）

### 问题
Modus 的 run budget 持久化是"资源维度"的落盘，但**"工作完成度维度"没有**——agent 做完一个多步骤任务，没有"哪些 (目标, 操作, 能力) 组合还没做"的客观记录，会盲目重复。

### 设计（移植 PentesterFlow CoverageStore）
- coverage 是 keyed 矩阵 `(objective, operation, capability)`，状态 `tried|passed|failed|skipped`
- `untested()` 交叉候选×能力返回未测元组
- 桌面 kanban 看板"未覆盖"列由它驱动

### 实施步骤
1. **新模块 `tools/coverage.py`**：
   - `CoverageStore`（内存 + JSONL 落盘 `~/.modus/coverage/{session_id}.jsonl`）
   - `mark(objective, operation, capability, state)` / `untested(objectives, capabilities)` / `list()` / `summary()`
2. **`coverage` 工具**（声明：read_only + safe + filesystem capability）：`ACTIONS=['mark','list','untested','summary','clear']`
3. **`agent/strategies/react.py`**：每轮 tool 执行后 `mark` 该轮目标下的 `(objective, operation, capability)`（从 tool 名 + 参数推断 objective——从当前 user 请求提取）
4. **`desktop/board_aggregation.py`**：`summary()` 接入 board——kanban 加"未覆盖"计数（`data-kb-attention` 已有，复用它）
5. **`/next` 类命令**（CLI）：`coverage untested` → 建议下一步

### 参考细节（PentesterFlow 实测）
- `coverage/store.ts:151-165`：`untested()` 交叉候选×漏洞类返回未测元组
- `tools/coverage.ts:13`：`ACTIONS=['mark','list','untested','summary','clear']`
- `agent.ts:354-371`：`coverageContext` 直接调用该工具，把状态塞进上下文并命令"不要重复已测项"
- 落盘 `findings/coverage-<session>.json`（write-coalescing + 5000 上限 + LRU 淘汰 `:196-204`）

### 测试
- `test_coverage_mark_and_summary`
- `test_untested_cross_product`：候选×能力交叉，排除已 tried
- `test_coverage_persists`：JSONL 落盘可恢复
- `test_board_aggregation_coverage`：未覆盖计数进 board

### 验收
- 跑一个多步骤任务 → kanban 显示"还剩 3 项未覆盖"
- 手动：`coverage untested` 列出未做组合

---

## 波次验收清单

- [ ] A1：批准 `cat a` 不放行 `rm -rf`；per-resource 记住才复用；审计含 scope
- [ ] A2：deny 后 agent 改道不重试；规则记忆持久生效
- [ ] A3：coverage 矩阵驱动 kanban"还剩什么"
- [ ] 全量 `pytest tests/ -q` 绿，六条安全不变量不回退

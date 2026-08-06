# Modus 数据安全、工作区与 Tool-use 开发手册

> 状态：架构基线 v1.0  
> 适用范围：Modus Desktop、CLI、Default Agent、MOA、Peri 及未来 AGI Runtime  
> 目标读者：核心运行时、工具插件、数据工程、安全、桌面端与测试开发者

## 1. 文档定位

本文规定 Modus 如何安全地访问工作区、调用工具、本地处理数据，并向 LLM 或其他外部服务披露最小必要信息。

它不是某个“脱敏工具”的说明，而是所有 Agent 能力必须遵守的核心开发合同。任何文件工具、脚本工具、数据库工具、文档解析器、媒体工具、MCP 工具和未来插件，都不得绕过本文定义的数据安全平面。

核心目标：

- 让 Agent 能真正完成工作，而不是只能聊天。
- 默认在本地完成可完成的计算，避免无意义消耗 Token。
- 将“本地访问”“修改数据”“执行程序”“网络传输”“进入模型上下文”拆成独立权限。
- 让 LLM 提出工具使用意图，但不能自行授予权限或绕过预算。
- 让所有外发数据可解释、可预览、可审计、可撤销授权。
- 让新工具通过稳定协议接入，而不是修改 Agent、UI 和服务端核心分支。

## 2. 安全承诺与非承诺

Modus 应当承诺：

1. 选择工作区只建立本地目录授权，不等于上传文件。
2. 原始 Tool Result 不能直接进入模型上下文。
3. 所有外发数据必须经过统一 Disclosure Gateway。
4. 权限、预算和数据出境规则由运行时强制，而不是依赖提示词自律。
5. 用户可以知道读取了什么、修改了什么、向哪里发送了什么。
6. 大结果默认保存在本地产物中，只向模型发送有限摘要。
7. LLM、插件、工具和前端都不能自行提升工作区权限。

Modus 不应承诺：

- 自动脱敏可以消除全部重识别风险。
- “完整权限”意味着无限制访问设备。
- 本地模型、云端模型或第三方 MCP 天然可信。
- 仅靠正则表达式可以识别全部敏感数据。
- 用户一次授权可以覆盖未来不同目的、不同范围的数据使用。

产品和审计界面必须描述“检测覆盖、变换方式与剩余风险”，不能使用“绝对安全”“完全匿名”等不可验证表述。

## 3. 核心安全不变量

以下规则必须写入 Runtime、策略测试与代码审查清单：

1. **工作区绑定不等于内容披露。**
2. **本地读取不等于允许向 LLM、Embedding、MCP 或网络服务发送。**
3. **模型输出是不可信的能力请求，不是授权。**
4. **前端只展示策略决定，不拥有安全决定权。**
5. **插件只能声明需求，不能覆盖策略结果。**
6. **工具原始结果先进入本地 Result Processor，再决定去向。**
7. **任何出境必须绑定目的、目标、范围、预算和授权期限。**
8. **敏感凭据、脱敏密钥和重识别映射永不进入模型、普通日志或插件上下文。**
9. **所有文件路径必须在工作区根目录内解析并经过符号链接逃逸检查。**
10. **删除、覆盖、批量修改、脚本执行和网络上传是相互独立的权限。**
11. **大结果必须有界、分页或产物化，不能直接拼接进提示词。**
12. **没有策略结论、没有预算或无法完成审计时，必须失败关闭。**

## 4. 威胁模型

### 4.1 需要保护的资产

- 用户文件正文、目录结构与元数据。
- 个人信息、企业数据、医疗、财务和合同数据。
- API Key、密码、Cookie、Token、证书和私钥。
- 数据库连接信息、源代码、内部域名和基础设施信息。
- 会话记录、长期记忆、Embedding、索引和本地产物。
- 伪名化映射、密钥、审批记录和数据血缘。

### 4.2 不可信边界

- LLM 返回的文本、工具参数和规划结果。
- 工作区内的文档、README、网页快照和提示注入内容。
- 第三方 MCP、插件、Skill、脚本和二进制程序。
- 云端模型、Embedding、OCR、翻译和外部 API。
- 工具 stdout/stderr、媒体元数据和文档解析结果。
- 浏览器页面与用户粘贴的链接。

### 4.3 主要风险

- 递归读取大型目录并造成 Token、费用和性能失控。
- 将本地读取结果未经处理直接交给模型。
- 文档中的提示注入诱导 Agent 上传其他文件。
- 多字段组合造成重识别，即使姓名已删除。
- 工具输出、异常栈或日志泄露密钥。
- 高权限从一个工作区错误继承到另一个工作区。
- 插件绕过 Tool Gateway 直接访问文件或网络。
- 脚本输出海量内容，间接进入模型上下文。
- 审批界面与真实执行参数不一致。
- 先执行后审批，或审批后参数发生变化。

## 5. 总体架构

```text
用户目标
  ↓
Reasoner / Planner
  ↓  只产生 Tool Intent
Tool Calling Adapter
  ↓  ToolInvocation
Tool Gateway
  ├─ Manifest 与参数校验
  ├─ 工作区 Grant 解析
  ├─ Permission Engine
  ├─ Budget Engine
  ├─ Disclosure Policy
  └─ Approval Coordinator
  ↓
Tool-use Runtime
  ├─ 本地执行器
  ├─ 受限进程执行器
  ├─ 网络执行器
  └─ 插件 / MCP 适配器
  ↓  RawToolResult（仅本地）
Result Processor
  ├─ 分类与敏感信息检测
  ├─ 脱敏、伪名化、聚合
  ├─ 截断、分页与产物化
  ├─ Token / 费用估算
  └─ 重识别风险评估
  ↓
Disclosure Gateway
  ├─ ModelPayload → LLM
  ├─ Artifact → 本地存储
  ├─ AuditEvent → 审计账本
  └─ DisclosureReceipt → 用户工作台
```

### 5.1 必须解耦的五个平面

| 平面 | 负责 | 不负责 |
|---|---|---|
| Reasoning | 规划与提出能力需求 | 授权与直接执行 |
| Tool Calling | 模型工具 schema 与调用解析 | 文件、进程、网络访问 |
| Tool-use Runtime | 执行生命周期与隔离 | 决定数据是否适合出境 |
| Data Security Plane | 分类、脱敏、披露和血缘 | 业务工具具体功能 |
| Experience Plane | 审批、进度、产物和审计展示 | 安全策略判定 |

## 6. 工作区授权模型

每个账户、工作区独立保存 `WorkspaceGrant`：

```yaml
schema: modus.workspace-grant.v1
grant_id: grant_123
owner_id: user_123
workspace_id: ws_123
root: /Users/user/Documents/Project
permission_profile: level_2_analysis
overrides: {}
disclosure_policy: confirm_content
budget_profile: standard
exclusions:
  - .git/**
  - .env
  - "**/secrets/**"
scope: session
created_at: 2026-08-06T12:00:00+08:00
expires_at: null
```

约束：

- `root` 必须规范化为真实路径。
- 所有子路径经过 `PathGuard`，禁止 `..`、绝对路径逃逸和符号链接逃逸。
- Grant 必须绑定账户；账户切换后不能继续使用旧账户 Grant。
- 会话只保存 `workspace_id`，不能依赖服务进程当前目录。
- 切换工作区时，权限、预算、排除规则和披露策略必须一起切换。
- 移除工作区只删除授权与记忆，不删除源文件。

## 7. 四级权限预设

四级权限是面向用户的模板，底层仍保存细粒度能力。模板升级不能静默扩大既有授权。

### 7.1 一级：观察

允许：

- 浏览目录与读取文件元数据。
- 统计类型、数量、时间和空间占用。
- 本地哈希和重复文件分析。
- 生成不含正文的目录清单。

禁止：正文读取、写入、脚本、Shell、网络上传和内容披露。

### 7.2 二级：分析

在一级基础上允许：

- 在本地读取和解析正文。
- 建立本地索引、分类、全文检索和数据清洗。
- 本地 OCR、文档解析和媒体探测。

限制：

- 不修改源文件。
- 正文或片段进入模型前仍需独立披露审批。
- 完整文件出境默认拒绝。
- 任意脚本与 Shell 默认拒绝。

### 7.3 三级：工作

在二级基础上允许：

- 创建、编辑、移动和重命名文件。
- 生成派生数据和产物。
- 运行受限脚本、测试与封装后的媒体工具。

覆盖、批量修改、删除、任意 Shell、联网和大规模披露仍需审批。

### 7.4 四级：完整

允许工作区内完整读写、批处理、Shell 和自动化流程，但仍不能绕过：

- 工作区路径边界。
- 系统与隐私目录保护。
- 敏感凭据规则。
- 数据出境审批。
- 不可逆操作审批。
- 资源、费用、Token 和网络预算。

### 7.5 细粒度权限合同

```yaml
filesystem:
  list: allow
  metadata: allow
  read_content: allow
  create: ask
  modify: ask
  move: ask
  delete: deny
execution:
  builtin: allow
  limited_script: ask
  shell: deny
  elevated_process: deny
network:
  fetch: ask
  upload: deny
model_disclosure:
  aggregate: allow
  metadata: allow
  excerpts: ask
  full_file: deny
  sensitive: deny
```

审批作用域只能是：单次调用、当前任务、当前会话或当前工作区。任何长期授权必须显示所涉及的工具、数据等级与目标服务。

## 8. 数据分类

### 8.1 内容等级

| 等级 | 内容 | 默认处理 |
|---|---|---|
| D0 Public | 用户明确公开的数据 | 仍受目的和预算限制 |
| D1 Metadata | 名称、类型、大小、时间 | 可本地自动处理，有限披露 |
| D2 Derived | 哈希、统计、标签、聚合值 | 可按策略披露 |
| D3 Content | 正文、代码、图片文字、记录行 | 披露前确认 |
| D4 Sensitive | 身份、财务、医疗、合同、内部信息 | 先脱敏，默认不发原文 |
| D5 Secret | 密钥、密码、Token、私钥、恢复映射 | 禁止出境 |

### 8.2 数据形态

- 非结构化文本：TXT、Markdown、日志、源码。
- 半结构化数据：JSON、XML、YAML、邮件。
- 结构化数据：CSV、Excel、数据库和 Parquet。
- 文档：PDF、DOCX、PPTX。
- 视觉数据：图片、扫描件和视频帧。
- 音频数据：语音、会议和背景声。
- 派生数据：Embedding、索引、摘要和统计模型。

Embedding 可能保留敏感语义，不能自动视为匿名数据。

## 9. 数据处理与披露策略

任务执行应按以下优先级选择数据路径：

1. 仅元数据即可完成。
2. 本地聚合或统计即可完成。
3. 本地规则、索引或小模型可以完成。
4. 发送少量脱敏派生数据。
5. 发送少量脱敏内容片段。
6. 用户明确授权后发送有限原文。
7. 完整文件出境仅用于不可替代场景，并单独高风险审批。

LLM 可以提出数据需求，但 Gateway 必须把需求降到满足目标的最小范围。

### 9.1 大目录策略

几百 MB 或几百 GB 的工作区必须：

- 流式扫描，不能一次性载入内存。
- 先处理元数据，再决定是否读取正文。
- 支持排除目录、文件类型、大小和日期过滤。
- 限制最大文件数、总字节、运行时间和并发度。
- 对索引和哈希使用增量缓存。
- 将完整清单、索引和分析报告保存为本地产物。
- 只向模型返回统计、异常和少量相关样本。
- 超出预算时暂停并返回已完成范围与继续成本。

## 10. Data Security Plane

Data Security Plane 是核心 Runtime，不是可选插件。所有进入 LLM、Embedding、MCP、外部 API、遥测或共享日志的数据必须通过它。

### 10.1 标准管线

```text
RawToolResult
  → Source Classification
  → Schema / MIME Detection
  → Sensitive Entity Detection
  → Policy Selection
  → Transformations
  → Re-identification Risk Check
  → Preview / Approval
  → Destination Check
  → Disclosure Receipt
  → External Dispatch
```

### 10.2 检测器分层

1. 确定性规则：邮箱、手机、身份证、银行卡、IP、密钥格式。
2. 校验算法：校验位、长度、前缀和熵检测。
3. 字段语义：列名、JSON Key、数据库类型和文件上下文。
4. 本地实体识别：人名、地点、组织、医院、合同主体。
5. 关联风险：年龄、地区、职业、时间等准标识符组合。
6. 用户定义规则：企业项目代号、内部账号和专有实体。

敏感检测自身默认本地运行。不得为判断数据是否敏感而先把原文交给云端模型。

### 10.3 变换器

- 删除：彻底移除字段或内容。
- 掩码：保留最小可辨结构，例如 `138****1234`。
- 伪名化：稳定映射为 `PERSON_0042`。
- 泛化：精确值转换为范围或区域。
- 聚合：记录级数据转换为统计结果。
- 分桶：时间、金额、年龄等区间化。
- 扰动：在可接受分析误差内加入噪声。
- 合成：生成结构一致的非真实样本。
- 图像处理：人脸、车牌、屏幕和文档区域遮挡。
- 音视频处理：语音变换、静音、字幕脱敏和帧级遮挡。

变换必须可组合、可版本化，并记录参数与覆盖范围。

### 10.4 伪名化密钥

- 使用操作系统安全存储或专用本地密钥库。
- 按账户和工作区派生隔离密钥。
- 映射表与脱敏数据分开存储。
- 不进入普通数据库导出、日志、Artifact 和模型上下文。
- 支持轮换、销毁和不可逆模式。
- 插件只能请求伪名化操作，不能获得主密钥。

## 11. Disclosure Gateway

Disclosure Gateway 是所有数据出境的唯一出口。

目标包括：

- 云端或本地 LLM。
- Embedding 服务。
- 第三方 MCP。
- OCR、翻译、搜索和数据 API。
- 网络上传工具。
- 遥测、崩溃报告和共享日志。

每次请求必须包含：

```json
{
  "purpose": "classify_contracts",
  "destination": {
    "kind": "llm",
    "provider": "example-provider",
    "model": "example-model"
  },
  "sources": ["artifact://local/redacted-sample-1"],
  "classification": "D3",
  "transformations": ["remove:credential", "pseudonymize:person"],
  "estimated_bytes": 18200,
  "estimated_tokens": 4600,
  "retention": "provider-policy-unknown",
  "authorization_scope": "invocation"
}
```

Gateway 必须检查：目的限制、目标允许列表、数据等级、脱敏状态、预算、用户授权和保留策略。任何字段缺失都不能静默放行。

## 12. Tool Calling 与 Tool-use 合同

### 12.1 Tool Calling

Tool Calling 只负责把模型意图转换成 `ToolInvocation`：

```json
{
  "schema": "modus.tool-invocation.v1",
  "invocation_id": "inv_123",
  "run_id": "run_123",
  "tool_id": "workspace.inventory",
  "tool_version": "1.0.0",
  "workspace_id": "ws_123",
  "arguments": {"path": ".", "max_depth": 4},
  "requested_budget": {"max_files": 50000, "timeout_seconds": 60}
}
```

它不能直接获得文件句柄、数据库连接、网络客户端或执行器对象。

### 12.2 Tool Manifest

```yaml
schema: modus.tool-manifest.v1
id: workspace.inventory
version: 1.0.0
category: workspace
description: 本地统计工作区结构
input_schema: {}
output_schema: {}
effects:
  filesystem: metadata_read
  process: none
  network: none
data_policy:
  local_access: metadata
  model_disclosure: aggregate
  sensitive_data: deny
permission:
  minimum_level: 1
  approval: automatic
resources:
  max_files: 100000
  max_read_bytes: 1073741824
  max_output_bytes: 32768
  timeout_seconds: 60
capabilities:
  cancellable: true
  resumable: true
  idempotent: true
executor:
  type: python
  entrypoint: modus.capabilities.workspace.inventory
```

Manifest 是声明，不是授权。Runtime 可以收紧声明，不能被插件要求放宽。

### 12.3 Tool Result

```json
{
  "schema": "modus.tool-result.v1",
  "invocation_id": "inv_123",
  "status": "completed",
  "model_payload": {
    "summary": "发现 4821 个文件，主要为 PDF 和 DOCX"
  },
  "artifacts": [{"artifact_id": "artifact_123", "local_only": true}],
  "disclosure": {
    "local_files_scanned": 4821,
    "local_bytes_read": 734003200,
    "model_bytes_sent": 3120,
    "raw_content_sent": false
  },
  "metrics": {"duration_ms": 1840}
}
```

必须明确区分：

- `raw_result`：只在本地 Runtime 内存在。
- `model_payload`：经过安全处理且允许进入模型的有限结果。
- `artifacts`：本地完整结果。
- `logs`：诊断数据，默认不进入模型。
- `disclosure`：本次数据流向事实。

## 13. Tool Gateway 与 Runtime

### 13.1 Gateway 固定顺序

1. 解析工具 ID 与锁定版本。
2. 验证 Manifest 签名或可信来源。
3. 使用 JSON Schema 校验参数。
4. 解析工作区与真实路径。
5. 计算所需细粒度权限。
6. 计算数据披露等级与副作用。
7. 合并账户、工作区、会话和工具预算。
8. 请求必要审批并绑定参数哈希。
9. 创建不可变 Invocation 记录。
10. 交给 Runtime 执行。

### 13.2 生命周期

```text
proposed → validated → policy_checked → awaiting_approval
→ queued → running → processing_output
→ completed | failed | cancelled | budget_exhausted
```

所有工具必须支持唯一调用 ID、结构化进度、取消、超时、有界输出和错误分类。修改工具还必须支持预览、变更清单、原子写入与恢复信息。

### 13.3 审批绑定

审批必须绑定：

- 工具 ID 与版本。
- 规范化后的精确参数哈希。
- 工作区与路径集合。
- 本地读取和修改范围。
- 数据披露目标与估算规模。
- 预算与有效期。

审批后任何字段变化都必须重新审批。

## 14. 资源预算

预算按以下顺序取最严格值：

```text
系统硬上限
  ∩ 账户预算
  ∩ 工作区预算
  ∩ 会话预算
  ∩ Run 预算
  ∩ Tool Manifest 上限
  ∩ 单次审批授权
```

至少限制：

- 扫描文件数与目录深度。
- 本地读取总字节与单文件大小。
- 模型披露字节与 Token。
- 网络上传和下载量。
- CPU、内存、进程数和运行时间。
- stdout/stderr 与日志大小。
- 创建、修改、移动和删除文件数。
- Artifact 总量与保存期限。

预算耗尽不是普通异常。工具应返回已完成范围、部分结果、剩余工作估算和扩大预算所需授权。

## 15. 脚本与进程执行

脚本工具必须区分：

- 经过封装的确定性工具，例如 FFprobe 和固定参数的转换器。
- 受限脚本，例如只读数据处理脚本。
- 任意 Shell 命令。
- 提权或系统级命令。

最低要求：

- 明确工作目录与环境变量允许列表。
- 清除 Modus 密钥和无关宿主环境变量。
- 禁止默认联网。
- 施加 CPU、时间、文件大小、文件描述符和输出限制。
- 捕获结构化退出状态，不把全部终端输出发给模型。
- 执行前展示命令、路径、副作用和网络需求。
- 变更文件在执行后生成清单和 Diff。
- 不可逆批量操作必须有计划阶段和应用阶段。

不要把 `pathlib`、FFmpeg、SQLite 等库直接暴露给 LLM。应封装成语义清晰、参数受限的工具，如 `workspace.inventory`、`media.probe` 和 `data.profile`。

## 16. 数据工程工具规范

数据分析、清洗和脱敏工具应采用“本地数据集句柄”，而不是在工具调用之间复制完整数据：

```json
{
  "dataset_id": "ds_123",
  "storage": "local",
  "schema": [{"name": "customer_id", "type": "string", "class": "identifier"}],
  "row_count": 500000,
  "source_lineage": ["workspace://ws_123/customers.xlsx"]
}
```

推荐工具族：

- `data.inspect_schema`
- `data.profile`
- `data.validate`
- `data.clean_plan`
- `data.clean_apply`
- `data.detect_sensitive`
- `data.redact_preview`
- `data.redact_apply`
- `data.aggregate`
- `data.sample_safe`
- `data.export_local`

工具默认返回 schema、统计和问题摘要；完整行级结果保存在本地数据集或 Artifact 中。

## 17. 插件与可插拔能力

建议结构：

```text
src/modus/
  tool_protocol/
  tool_runtime/
  data_security/
  capabilities/
    workspace/
    documents/
    data/
    media/
    scripts/
  observability/
```

插件包包含 Manifest、执行器、可选本地解析器、测试和 UI 展示提示。插件不得：

- 直接向 LLM 客户端发送数据。
- 直接访问会话数据库内部表。
- 直接渲染聊天 DOM。
- 自己实现另一套权限或审批。
- 读取伪名化主密钥。
- 将 RawToolResult 标记为已安全处理。
- 绕过 Gateway 调用其他执行器。

Runtime 应允许第三方插件声明更高风险，不能允许其降低系统识别出的风险。

## 18. 工具发现与模型上下文

不能一次向模型暴露全部工具定义。应采用：

1. 能力分类：workspace、documents、data、media、development、network。
2. 意图路由：根据用户目标选出相关能力组。
3. 延迟加载：一次只暴露约 5–15 个工具。
4. 权限裁剪：不向模型暴露当前绝对不可用的高风险工具。
5. 版本锁定：Run 内工具 schema 不应中途变化。

隐藏工具只能减少模型选择面，不能替代运行时权限检查。

## 19. 审计与数据血缘

每次外发生成不可变 `DisclosureReceipt`：

```json
{
  "schema": "modus.disclosure-receipt.v1",
  "receipt_id": "receipt_123",
  "owner_id": "user_123",
  "workspace_id": "ws_123",
  "run_id": "run_123",
  "invocation_id": "inv_123",
  "purpose": "contract_classification",
  "sources": ["workspace://ws_123/contracts/2026/*.pdf"],
  "local_items_processed": 5000,
  "items_disclosed": 30,
  "transformations": [
    "remove:credential@1",
    "pseudonymize:person@2",
    "generalize:address@1"
  ],
  "destination": "llm:provider:model",
  "bytes_sent": 18200,
  "estimated_tokens": 4600,
  "raw_content_sent": false,
  "risk": "low",
  "authorization": "approval_123",
  "created_at": "2026-08-06T12:00:00+08:00"
}
```

审计日志本身也可能敏感：参数应摘要化，禁止记录原始密钥、完整正文和伪名化映射。

## 20. 用户体验合同

### 20.1 工作区卡片

工作区卡片展示名称、路径和权限等级；三点菜单提供：

- 修改或切换工作区。
- 切换四级权限。
- 自定义权限。
- 数据出境规则。
- 资源预算。
- 访问与披露记录。
- 设为默认工作区。
- 移除授权记录。

### 20.2 披露审批

审批卡必须显示：

- Agent 的任务目的。
- 本地处理还是外部发送。
- 涉及的文件、字段或片段范围。
- 原始规模与计划发送规模。
- 检测到的敏感类型。
- 应用的脱敏变换。
- 目标提供商、模型或服务。
- 预计 Token、费用与保留信息。
- 剩余重识别风险。

用户可以选择：仅发聚合结果、发送脱敏版本、降低精度、改用本地模型、允许有限原文或拒绝。

审批页面展示的参数必须来自 Runtime 的不可变 Invocation，不能由前端自行拼接。

## 21. 当前代码迁移边界

当前 Modus 已有可复用基础：

- `PathGuard` 与工作区真实路径约束。
- `Tool` / `ToolExecutor` / `ToolRegistry`。
- Tool 审批与参数哈希绑定。
- RunController、取消、预算和事件账本。
- Artifact、本地工作台和审批卡。
- `data_disclosure` 初步字段。

当前仍需迁移的关键问题：

1. `ToolResult.content` 同时承担模型结果与展示结果，应拆成 Raw、ModelPayload、Artifact 和 UI Summary。
2. `requires_approval` 只有布尔值，应由细粒度权限和 Disclosure Policy 计算。
3. 工具 Manifest 仍内嵌 Python 定义，缺少版本、effects、预算和数据策略 schema。
4. 工作区尚未持久化四级权限、覆盖规则和预算。
5. 缺少统一 Disclosure Gateway；不能依赖单个工具自行脱敏。
6. 缺少结构化数据集句柄、血缘和脱敏变换记录。
7. 日志与终端输出缺少统一敏感信息过滤和产物化上限。
8. MCP 与未来插件需要强制经过相同 Gateway。

迁移期间不得通过修改 Prompt 假装已实现运行时安全。

## 22. 推荐实施路线

### Phase 0：冻结不安全扩展

- 暂停大规模新增文件和数据工具。
- 为现有工具补齐 effects、数据披露和输出上限。
- 建立安全回归测试与威胁样本库。

### Phase 1：协议与核心 Gateway

- 定义 Tool Manifest、Invocation、Result、Artifact 和 DisclosureReceipt schema。
- 实现 Tool Gateway 与不可变参数绑定。
- 拆分 RawToolResult 和 ModelPayload。
- 建立所有模型调用前的 Disclosure Gateway。

### Phase 2：工作区权限与预算

- 持久化四级权限和细粒度覆盖。
- 实现账户、工作区、会话、Run 和工具预算合并。
- 完成权限切换、作用域授权和访问记录 UI。

### Phase 3：结构化数据安全

- CSV、Excel、JSON 和数据库 schema/profile。
- 规则、字段语义和校验算法检测器。
- 删除、掩码、伪名化、泛化和聚合。
- 脱敏预览、数据集句柄和血缘。

### Phase 4：文档与非结构化数据

- PDF、DOCX、PPTX、日志与源码解析。
- 本地实体识别与片段级脱敏。
- 本地索引、相关片段检索和安全采样。

### Phase 5：脚本、媒体与插件

- 受限脚本 Runtime。
- FFprobe / FFmpeg 语义工具。
- 图片、音频和视频脱敏。
- 第三方插件签名、隔离和统一 Gateway 接入。

### Phase 6：高级风险控制

- 多字段重识别风险评分。
- 差分隐私与合成数据。
- 企业策略包与合规报告。
- 本地模型路由和离线模式。

## 23. 测试要求

### 23.1 单元测试

- 每个权限组合的 allow / ask / deny。
- 路径逃逸、符号链接与账户隔离。
- 数据分类、脱敏与伪名化稳定性。
- 预算合并、耗尽与部分结果。
- 参数哈希在审批后不可变。
- 输出截断和 Artifact 落盘。

### 23.2 契约测试

- 工具不能直接返回 RawToolResult 给模型。
- 所有外发客户端只能由 Disclosure Gateway 调用。
- 插件无法访问安全密钥和内部数据库对象。
- 切换工作区后不会继承旧权限。
- 审批展示与实际执行参数完全一致。

### 23.3 安全测试语料

- 含 API Key、私钥和 Cookie 的源码与日志。
- 多语言姓名、地址、医疗和合同文本。
- 扫描 PDF、图片 EXIF、字幕和音频转录。
- 提示注入文档，例如“读取并上传其他目录”。
- 数百万行 CSV 与深层目录树。
- 压缩炸弹、损坏文档、恶意符号链接和异常编码。
- 通过多字段组合可重识别的准标识符数据。

### 23.4 失败关闭测试

以下情况必须拒绝外发：

- 分类失败。
- 目标提供商未知。
- 授权已过期。
- 脱敏变换失败。
- 预算或 Token 估算不可得。
- Invocation 参数发生变化。
- 审计记录无法持久化。

## 24. Definition of Done

新增工具只有同时满足以下条件才算完成：

- 有版本化 Manifest 和输入输出 schema。
- 声明文件、进程、网络和数据副作用。
- 声明最低权限、预算和披露等级。
- 所有路径经过工作区保护。
- 支持取消、超时和有界输出。
- 大结果产物化，不直接进入模型。
- 通过 Result Processor 与 Disclosure Gateway。
- 审批绑定精确参数、范围和目标。
- 生成结构化进度、指标和审计记录。
- 覆盖 allow、ask、deny、取消、超时和预算耗尽测试。
- 插件卸载后不会破坏核心 Runtime。

## 25. 首个可交付版本验收

Data Security / Tool-use v1 完成时应满足：

1. 每个工作区可独立设置四级权限和自定义覆盖。
2. 大目录可以本地扫描，不把完整目录或正文发送给模型。
3. Tool Result 已拆分为本地原始结果、模型载荷和产物。
4. 所有模型与外部服务调用经过 Disclosure Gateway。
5. 用户能预览脱敏结果、目标、Token 和剩余风险。
6. CSV、Excel、JSON 和文本支持基础敏感检测与脱敏。
7. 用户能查看每次运行的本地读取量、修改量和外发量。
8. LLM、UI、MCP 和插件都无法绕过权限与数据出境策略。
9. 所有工具具有预算、取消、超时和输出限制。
10. 新增能力不需要修改 Agent 推理循环、消息渲染和安全核心。

## 26. 工程决策原则

遇到新功能时依次询问：

1. 能否只使用元数据？
2. 能否完全在本地计算？
3. 能否返回聚合或脱敏派生数据？
4. 是否必须披露正文，最小范围是什么？
5. 数据将发送给谁，保留策略是什么？
6. 用户是否看得懂并控制这次授权？
7. 工具失败、取消或预算耗尽时能否安全停止？
8. 新能力能否通过 Manifest 与插件接入，而不修改核心？

如果其中任何安全问题没有确定答案，该能力不应进入生产执行路径。


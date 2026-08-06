# Modus：认知宇宙的负熵引擎

Modus 是一个 Python 3.11+ 的本地 Agent 运行时，提供 CLI 与 Desktop Web 界面。它的基础形态是一个具备工具调用、人工审批、预算、取消和恢复能力的 LLM Agent。

在工程之上，Modus 内置了对“认知本质”的理解：**智能不是积累答案，而是在有限资源（预算）内，持续为局部环境降熵的过程。**

用户可主动开启两种高级认知形态（MOA、Peri），它们与基础形态共同构成三种截然不同的认知协作范式。

## 三种认知形态的深层差异

许多系统都有“多模型”能力，但 Modus 的三形态区别不在于“是否使用多个模型”，而在于**如何看待和使用“多样性”**。这是三种不同的哲学选择：

| 维度 | **Default（含 Subagent）** | **MOA** | **Peri** |
| :--- | :--- | :--- | :--- |
| **宇宙隐喻** | 层级星系 | 平行透镜宇宙 | 分形生长宇宙 |
| **拓扑结构** | **星型**（经理 → 专家） | **并行**（顾问互不通信） | **迭代循环**（主持人 ↔ 研究员） |
| **核心目的** | **效率**：分工以降低复杂度 | **质量**：集思广益以增强可靠性 | **深度**：辩证以达成共识 |
| **对多样性的态度** | **不关心** | **直接利用**（差异即信息） | **辩证综合**（矛盾即演化原料） |
| **成本结构** | **线性 O(n)** | **指数 O(k×n)** | **非线性**：主模型 ×（1+α），α < 1 |
| **前沿模型适配性** | **高度适配** | **不推荐**（k倍成本失控） | **工程成熟后可适配** |

**一句话本质**：

- **Default（Subagent）**：把复杂问题拆碎，分给不同专家。
- **MOA**：让多个智者同时看同一个问题，然后综合意见。
- **Peri**：组建一个研究小组，分工取证、反复讨论，直到达成共识。

## 选择运行方式：成本-能力-工程复杂性三维决策

Desktop 输入框左侧的**模型与方式选择器**控制每次任务怎么跑。

### 决策矩阵

| 运行方式 | 适合同类任务 | 模型选择建议 | 相对成本 | 工程状态 |
| :--- | :--- | :--- | :--- | :--- |
| **基础（默认模型）** | 明确的一次性任务：改一个文件、跑一次测试 | **任意模型，包括高价格前沿模型** | 基准（1×） | 成熟稳定 |
| **MOA** | 需要方案比较、研究交叉验证、高质量综合 | **建议中低价模型集群**（k倍成本） | 高（k×） | 成熟稳定 |
| **Peri** | 可分解的调研、编码、审查、复杂项目 | 主模型可用前沿模型，子模型可用高性价比模型 | 工程攻克后可控 | 观测纪元（只读） |

### 成本经济学详解

**Default（线性 O(n)）**：

- 主模型完成所有主要推理，成本 = 单次调用费用 × 调用轮数。
- **适合直接使用高价格、高能力前沿模型**，单次调用效率最高，总成本仍可控。

**MOA（指数 O(k×n)）**：

- k 个参考模型并行对同一任务给出意见，成本 ≈ k 倍主模型调用费用。
- **不推荐使用高价格前沿模型**——费用叠加会迅速失控。建议使用中低价模型集群。

**Peri（非线性，可控）**：

- 主模型承担任务拆解、审阅与合并；子模型只在局部问题上深入研究。
- 总成本 ≈ 主模型 ×（1 + α），α 为子模型总成本与主模型成本的比率。
- **理论上 α < 1**，因为子模型处理的是更小、更聚焦的局部问题。
- **工程前置条件**：必须攻克任务拆解、长连接通信、上下文管理、迭代终止判定四大难题。

**决策逻辑**：

- **质量第一 + 预算充足 → MOA**
- **深度共识 + 复杂项目 → Peri**
- **其他 → Default**

## 快速开始

使用 `uv`：

```bash
uv sync --dev
uv run modus doctor
uv run modus -p "检查当前项目并给出改进建议"
```

不带 `-p` 运行时进入交互式 REPL：逐轮对话并保持上下文历史，输入 `exit` 或 `quit` 退出。

启动 Desktop：

```bash
./start.sh
```

默认只监听 `127.0.0.1:3000`。模型凭据可通过环境变量配置：

```bash
export MODUS_PROVIDER=openai
export MODUS_MODEL=gpt-4.1-mini
export MODUS_API_KEY=...
```

## Desktop 使用

首次启动且仓库为空时，界面会引导你完成**添加模型 → 设为默认 → 可选配置增强方式**，可随时跳过；已有模型仓库的用户直接进入对话。

- **模型与方式选择器**：输入框左侧按钮展开模型（按厂商分组）与 MOA/Peri 增强方式。
- **模型仓库（设置 → 仓库）**：添加/编辑/删除模型记录，测试连接，从厂商发现可用模型。
- **Skills（设置 → Skills）**：创建、导入、删除可复用提示模板。在输入框输入 `@` 弹出 Skill 选择器。
- **运行历史与回放（右侧工作台）**：每次任务对应一个不可变 run。工作台列出当前会话的历史 run；点按 ⏪ 按钮回放完整 typed 事件序列。
- **Extensions（设置 → 扩展）**：管理 MCP/extension 定义。

## 可靠性合同：对抗宇宙熵增的物理定律

每条用户任务对应一个不可变 `run_id`，所有模式共享以下硬性物理定律：

- 同一 session 同时只允许一个 run。
- 取消向所有底层任务传播。
- `max_turns`、`max_tokens`、`max_wall_seconds` 在整个 run 中共享。
- 终态为 `completed`、`max_turns`、`token_limit`、`wall_time`、`cancelled`、`engine_error` 或 `failed`。
- SQLite 记录 `runs`、`run_events` 与 `approvals`。
- Desktop 恢复会话时有界回放最近 50 个 run 的 typed events。

相关配置：

```json
{
  "runtime": {
    "max_turns": 20,
    "max_tokens": 200000,
    "max_wall_seconds": 600
  },
  "features": {
    "compression": {
      "enabled": true,
      "trigger_tokens": 80000,
      "tail_messages": 8
    }
  }
}
```

对应环境变量：

- `MODUS_RUN_MAX_TURNS`
- `MODUS_RUN_MAX_TOKENS`
- `MODUS_RUN_MAX_WALL_SECONDS`
- `MODUS_COMPRESSION`
- `MODUS_COMPRESSION_TRIGGER_TOKENS`
- `MODUS_COMPRESSION_TAIL_MESSAGES`

## 安全边界

- 写文件、shell 等副作用工具经统一审批。
- Peri Worker 当前由运行时强制为只读。
- 事件、预算与审批输入写入 SQLite 前递归脱敏。
- MCP 的浏览器 DTO 不包含 `env`；配置文件只接受 `env:NAME` 引用并以 `0600` 原子写入。
- 测试使用临时 Desktop SQLite，不读取或写入真实 `~/.modus/desktop.db`。

## 验证

```bash
PYTHONDONTWRITEBYTECODE=1 python -B -m pytest -q -p no:cacheprovider
python -B -m compileall -q src tests
```

真实浏览器回归在独立目录 `e2e/`：

```bash
uv sync --dev
uv run python -m pytest e2e/ -q
```

## 代码结构

```text
src/modus/agent/              主 Agent 查询循环、MOA、上下文压缩
src/modus/runtime/            状态机、RunController、预算、取消
src/modus/policy/             审批策略、路径/命令保护、审计
src/modus/tools/              工具定义、执行器与内置工具
src/modus/skills.py           本地 Skill 仓库
src/modus/extensions.py       MCP/extension 注册表
src/modus/mcp_client.py       stdio MCP server 生命周期
src/modus/desktop/server.py   FastAPI/WebSocket 组合入口
src/modus/desktop/model_repository.py  模型仓库与凭据边界
src/modus/desktop/credential_backend.py  凭据存储后端
src/modus/desktop/*_runner.py 基础 Agent、MOA 与 Peri 的 orchestration
src/modus/desktop/session_state.py  session 状态与 run admission
src/modus/desktop/approval_flow.py  人工审批桥
src/modus/desktop/worktree_lifecycle.py  Peri worktree 生命周期
src/modus/desktop/worktree_orchestrator.py  worktree 审批门编排
src/modus/desktop/db.py       SQLite ledger 与恢复
src/modus/desktop/static/     前端模块（core/markdown/timeline/workspace/websocket/theme/settings/bindings.js）
```

## 当前非目标与未来纪元

以下能力属于后续方向：shell 文件系统隔离（资源限制已实现）、provider 流级续传（run 级 park/resume 已实现）。

Peri 递归子 Agent 是**可选且默认关闭**的能力：设置 `features.convergence.max_recursion_depth: 2`（或 `MODUS_MAX_RECURSION_DEPTH=2`）后，Worker 获得 `spawn_subtask` 工具，可把自己的范围拆给子 Worker——深度受限、在 `_subtasks/` 子目录隔离工作、共享同一 run budget（硬性上限）、子 task 经 `parent_task_id` 嵌套记账。默认（depth=0）时 Worker 保持扁平、无 spawn 能力。

Run 级 park/resume 是**可选且默认关闭**的能力：设置 `features.park_on_disconnect: true`（或 `MODUS_PARK_ON_DISCONNECT=true`）后，WebSocket 断线不再取消正在运行的 run，而是把它 park 住（PAUSED 态）——run 继续执行完、事件持续写入 SQLite；重连后新连接经 `resume_session` 绑定到 park 的 run，从断点续收。默认关闭时断线仍 fail-closed 取消（旧行为）。注：这是 run 级续传，provider 原生流级 resume 需 DeepSeek/OpenAI 等支持，当前未实现。

Shell 资源限制是**可选且默认关闭**的能力：设置 `sandbox.enabled: true`（或 `MODUS_SANDBOX_ENABLED=true`）后，bash/run_tests 子进程经 `preexec_fn` 施加 RLIMIT_CPU/RLIMIT_FSIZE/RLIMIT_NOFILE——CPU 秒、输出文件大小、文件描述符被硬性约束，超限返回可读错误（如 "CPU limit exceeded"）。`MODUS_SANDBOX_CPU_SECONDS`/`MODUS_SANDBOX_FSIZE_BYTES`/`MODUS_SANDBOX_NOFILE` 可调。默认关闭，不改变现有行为。

Peri 可写 Worker 是**可选且默认关闭**的能力：设置 `features.writable_workers: true`（或 `MODUS_WRITABLE_WORKERS=true`）后，Worker 在私有 git worktree 中运行，可写文件与 git add/commit；worktree 创建与合并回主分支分别经过 `create_worktrees`/`merge_changes` 审批门，合并完成后自动清理 worktree，不 push、不自动合并、不强制清理。默认（关闭时）Worker 仍是只读取证。

Peri 审阅循环带**收敛检测**（默认开启，`features.convergence`）：`decompose` 把每个子任务的成功标准拆成**结构化 checklist**，Host 对每条逐次判定"满足/不满足"+ 理由，SPRT 统计用"已满足的 criteria 数变化"判收敛、语义重叠判表达性坍缩。criteria 是权威——全部满足即达标并正常完成；低满足率 + 无进展达 `max_revision_rounds` 上限则失败。`MODUS_CONVERGENCE_ENABLED`/`MODUS_MAX_REVISION_ROUNDS`/`MODUS_CONVERGENCE_SEMANTIC_THRESHOLD`/`MODUS_CONVERGENCE_CRITERIA_VERIFICATION`/`MODUS_CONVERGENCE_SPRT_MIN_RATIO` 可调。

Agent 推理层是**可插拔**的：默认 `ReActReasoner` 通过 `Reasoner` 接口驱动（未来 AGI 可换策略不重写 runner），`ContextProvider` 统一上下文组装，`Capability` 做模型能力协商，`agi` mode 与 `task_kind` 已预留接缝。
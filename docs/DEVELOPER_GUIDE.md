# Modus 开发者手册

面向 Modus 的开发者：如何理解代码结构、新增功能、运行与验证。这是工程手册，不是产品说明（产品说明见 [README](../README.md)）或愿景文档（见 [ARCHITECTURE](../ARCHITECTURE.md)）。

涉及工作区、Tool Calling、Tool-use、数据处理、脱敏、网络披露或插件能力的开发，必须同时遵守 [数据安全、工作区与 Tool-use 开发手册](DATA_SECURITY_TOOL_USE_DEVELOPMENT_GUIDE.md)。

## 1. 技术栈

| 层 | 技术 |
|---|---|
| 运行时 | Python 3.11+，asyncio |
| 服务 | FastAPI / WebSocket / Uvicorn |
| 持久化 | SQLite（会话、run、事件、审批账本） |
| 前端 | 单页 HTML + 裸全局 classic script（无框架、无打包器） |
| 测试 | pytest + pytest-asyncio；浏览器 E2E 用 Playwright（复用系统 Chrome） |

## 2. 目录结构

```text
src/modus/
├── agent/            主 Agent 查询循环、MOA、上下文压缩
├── runtime/          RunController 状态机、预算、取消（desktop 无关）
├── policy/           审批策略、路径/命令保护、审计
├── tools/            工具定义、执行器、内置工具、git worktree 工具
├── desktop/          FastAPI/WebSocket 组合入口 + 全部桌面逻辑
│   ├── server.py     WS 路由、消息分发、run admission
│   ├── model_repository.py     模型/凭据权威
│   ├── credential_backend.py   JSON/Keychain 凭据后端与迁移
│   ├── *runner.py    基础/MOA/Peri 编排
│   ├── worktree_lifecycle.py   可写 Worker 的 worktree 生命周期
│   ├── worktree_orchestrator.py worktree 创建/合并的审批门编排
│   └── static/        前端全部模块（见第 3 节）
├── mcp_client.py     MCP stdio/SSE 客户端
├── extensions.py     Skill/MCP/plugin 注册表
└── llm/  memory/  prompt/  rag/  snapshot/  ...

tests/   后端与契约测试（pytest tests/）
e2e/     真实浏览器回归（pytest e2e/，独立运行）
.hermes/ 研究账本（已归档历史快照见 .hermes/archive/）
```

## 3. 前端模块化（重要约定）

前端没有打包器。JS 按**裸全局 classic script** 拆分，用 `<script src>` 顺序表达依赖拓扑。**必须保持这个顺序，且不加 `type="module"`**（module 有独立作用域，会破坏全局共享）。

```html
<script src="protocol.js">     <!-- ModusProtocol 命名空间 -->
<script src="workbench.js">    <!-- ModusWorkbench 命名空间 -->
<script src="core.js">         <!-- 全局状态 + DOM 引用 + 语法高亮（必须最先） -->
<script src="markdown.js">     <!-- 纯渲染函数 -->
<script src="timeline.js">     <!-- EventStore + TimelineRenderer + 实例化 -->
<script src="workspace.js">    <!-- 工作区投影 + 气泡 -->
<script src="websocket.js">    <!-- modusConnectSocket + handleMsg + sendMessage -->
<script src="theme.js">        <!-- 主题（立即执行） -->
<script src="settings.js">     <!-- composer/仓库/onboarding/skills -->
<script src="bindings.js">     <!-- MCP + 确认弹窗 + 事件绑定 -->
<script>                      <!-- 仅剩：const esChat…; modusConnectSocket(); -->
```

**新增功能**：新建一个 `.js` 文件，在 index.html 按依赖位置加一行 `<script src>`，不要往现有文件里塞。跨文件共享通过裸全局（`ws`、`currentMode`、`renderTimelineMarkdown`、`showConfirm` 等），加载顺序即依赖顺序。

**规则与坑**：

- `core.js` 定义全局状态（`ws`/`sessionId`/`currentMode`/`currentDbId` 等）与 DOM 引用（`input`/`chatArea`/`rp*`），必须在最前。
- `modusConnectSocket()` 启动调用永远在最后一个 `<script>` 内联块，即 DOM 与全部函数声明之后。
- TDZ 陷阱：`let` 声明的全局被同文件更早的赋值引用时，依赖声明顺序。拆分时保持原相对顺序（例如 `renderSkills` 赋值 `modusSkills`，二者同在 settings.js）。
- `bindings.js` 里的 `_origOpenSettings` monkey-patch 必须晚于 `settings.js` 的 `openSettings` 定义。
- 修改 JS 后运行 `python check_js.py`（校验内联 `<script>` 语法）与 `node --check <file>.js`。

## 4. 后端运行时与安全边界

**Run 生命周期**：每个用户任务一个不可变 `run_id`，由 `RunController` 唯一持有状态机、取消令牌、审批 future 与预算。三模式（DEFAULT/MOA/Peri）共享同一生命周期，`default` 是内部协议值不是产品模式。

**安全不变量**（研究账本定下的硬约束，不得回退）：

1. 模型输出是不可信数据——不得直接成为工具授权、系统指令或审批决策。
2. 副作用先记录、再批准、后执行——审批绑定精确参数摘要与 `input_hash`，不是工具名。
3. 聊天历史 ≠ 运行历史——run、事件、审批、工具执行独立持久化。
4. 终态不可逆——完成/失败/取消后不得被迟到事件重新激活。
5. 能力由运行时授予，不由 prompt 承诺——Peri worker 的只读/可写边界在 Registry/Policy 强制。
6. 浏览器 DTO 永不返回密钥——模型凭据只在本机，`has_credential`/`credential_hint` 是唯一可见信号。

**凭据存储**：`credential_backend.py` 抽象了 JSON（默认，明文在 models.json）与 macOS Keychain 两个后端。迁移到 Keychain 前必须显示脱敏报告（只显示尾缀），需显式确认，写时间戳备份，且仅在 macOS 可用。

**Peri Worker**：默认只读（allowlist 强制）。可写模式（`features.writable_workers: true`）下 Worker 在私有 git worktree 内运行，可写文件与 git add/commit；worktree 创建与合并回主分支分别经 `create_worktrees`/`merge_changes` 审批门，不 push、不自动合并、不强制清理。

**Peri 递归子 Agent**：`features.convergence.max_recursion_depth`（默认 0 = 关闭）开启后，Worker 通过 `spawn_subtask` 工具把自己的范围拆给子 Worker（`peri.py` 的 `_make_spawn_subtask_tool`）。子 Worker 在 `_subtasks/<id>/` 子目录隔离工作、深度受限（`depth` 经闭包注入）、共享同一 run budget、task 经 `parent_task_id`/`depth` 列嵌套记账。

**Shell 资源限制**：`sandbox.enabled: true`（或 `MODUS_SANDBOX_ENABLED=true`）开启后，bash/run_tests 子进程经 `preexec_fn` 施加 RLIMIT_CPU/RLIMIT_FSIZE/RLIMIT_NOFILE（`sandbox.py`）。CPU 秒、输出文件大小、文件描述符被硬性约束，超限返回可读错误。默认关闭，不改变现有行为。注：RLIMIT_NPROC 因用户级全局计数会破坏普通管道而排除；RLIMIT_AS/DATA 因 macOS 上 preexec 里继承父解释器地址空间而不可靠。

**Reasoner 抽象（未来 AGI 接入缝）**：默认 Agent 循环已从 `query()` 抽成 `ReActReasoner`（`src/modus/agent/strategies/react.py`），通过 `src/modus/agent/reasoner.py` 的 `Reasoner` Protocol 定义接缝。未来推理策略（plan-then-act、反思、树搜索、自主 AGI 循环）实现 `run(messages, ...)` 产出相同事件词汇（`text_delta/tool_call/tool_result/done`）即可通过 `QueryEngine.ask(reasoner_factory=...)` 接入，复用同一 runner/预算/审批/工具执行/持久化层。配套：
- `src/modus/agent/context.py` 的 `ContextProvider`——上下文组装统一接口（记忆 + 历史 + skill），不再各 runner 手拼。
- `src/modus/agent/capabilities.py` 的 `Capability`/`ModelCapabilities`——模型能力协商（tools/images/reasoning/embeddings/structured_output）。
- `modes.py` 已注册 `agi` mode（预留）；`run_tasks.task_kind` 放开闭集，未来 AGI 可注册新任务种类。

## 5. 测试策略

三套测试，职责不同，**必须分开运行**：

```bash
# 1. 后端 + 契约测试（410 项）
uv run python -m pytest tests/ -q

# 2. 真实浏览器回归（7 项）—— 独立目录，不可并入 pytest tests/
uv run python -m pytest e2e/ -q

# 3. 静态校验
uv run python check_js.py
uv run python -m compileall -q src tests
```

**为什么 e2e/ 独立**：E2E 的 Playwright（`sync_playwright`）会创建自己的事件循环，与 pytest-asyncio 在完整套件收集时冲突（曾导致 157 个异步测试假失败）。因此 `e2e/` 必须单独运行。

**契约测试读 bundle 而非 index.html**：`tests/_bundle.py` 的 `js_bundle()` 按加载序拼接全部前端 JS（外部文件 + 内联启动），`page_html()` 返回原始 HTML/CSS。前端契约测试应优先用 `js_bundle()` 断言 JS 行为、用 `page_html()` 断言 DOM/CSS，这样前端模块的拆分/重排不会破坏测试。

**E2E 如何工作**：`e2e/conftest.py` 启动隔离的测试模式服务器子进程（`MODUS_DESKTOP_TEST_MODE=approval_write` + 独立 `MODUS_DATA_DIR`），`ApprovalE2EFixtureEngine` 绕过真实 LLM 但走真实 ToolExecutor/审批/PathGuard 边界。Playwright 用 `channel="chrome"` 复用系统 Chrome，无需下载浏览器。用例覆盖 composer、审批 allow/deny 写文件边界、skill `@` 附加、消息时间线、console 无错误。

## 6. 常用命令

```bash
uv sync --dev                    # 安装依赖（含 playwright dev 依赖）
./start.sh                       # 启动 Desktop（127.0.0.1:3000）
uv run modus -p "提示词"          # 单轮 CLI
uv run modus                     # 交互式 REPL
uv run python -m pytest tests/ -q   # 后端回归
uv run python -m pytest e2e/ -q     # 浏览器回归
uv run python check_js.py           # 前端内联 JS 语法
```

浏览器手动验证：启动后检查 DevTools Console 无 JS 错误（忽略浏览器扩展注入错误）；关键交互——发消息出气泡、设置→模型仓库、composer 切 MOA/Peri、审批卡 allow/deny、输入 `@` 出 skill 菜单、`⌘,` 开设置、`?` 快捷键帮助。

## 7. 改动验证门槛

任何改动提交前：

```bash
uv run python -m pytest tests/ -o addopts= -q   # 全绿
uv run python -m pytest e2e/ -q                 # 全绿（若改动涉及前端）
uv run python -m compileall -q src tests
git diff --check
```

若改动涉及前端 JS，另跑 `python check_js.py` 与 `node --check src/modus/desktop/static/<file>.js`。

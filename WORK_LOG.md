# Modus 开发工作日志

> 自动开发会话（2026-08-07，用户不在线，bypass 权限）。每项改动结论式记录：
> 动机、取舍、验证。用户明早读此文件 + 测试输出即可恢复上下文。
> 约束：不 git commit；不碰 `.env`；六条安全不变量不回退。

## 个人 PC 全能基座（用户定位：AGI/Agent 基座 + 任意平台）→ A1 浏览器操作工具集 ✅

### 深化三连（用户："先做全再吸收完善"）✅
用户指示三个深化方向 + 断电 PDEATHSIG/kqueue 暂缓（记忆已记，资源不倾斜）。
- **深化1 浏览器批注截图回传** ✅：annotate.js 加 `captureElementShot`（foreignObject→canvas→dataURL 给选中元素截局部图，≤600px 缩放 + 1.5M cap，tainted canvas 静默省略）；item 带 image；moduswindows.js submitAnnotations 把 image 转 image 附件传给 `sendUserEditedMessage`；server `_handle_browser_comment` 提取 whitelisted data-URI（png/jpeg/webp）作 run_message attachments。测试 +1（image 附件过滤）。真实 Chrome 实测 firstHasImage=True。
- **深化2 office_exec sandbox 加固** ✅：AST 拒绝扩展（`_BLOCKED_ATTRS`：os.system/popen/remove/unlink/rmdir/chmod/rename/mkdir 等 + shutil 属性 + os 文件操作）；`_run_script` 改 Popen 手动管理——超时 `os.killpg` 杀整个进程组（防孙进程孤儿，Windows taskkill /T）；非零退出标 is_error（含 SIGKILL -9）。测试 +4。尝试过 prelude open 单文件锁定但**回退**（openpyxl 加载链需要自由读内部资源，过度约束会破坏功能——真实门是 PathGuard + 审批 + AST）。
- **深化3 MCP Streamable HTTP transport** ✅：`build_http_app`（FastAPI）复用同一 `McpServer._handle`（registry 过滤 + deny-first 门只换传输层）；GET /mcp 握手 + POST /mcp JSON-RPC；`mcp_serve_http` + CLI `--transport http --host --port`。踩坑：`request: Request` 被 FastAPI 当 query 参数（422），改 `Body(...)` 修复。测试 +2（HTTP app 复用透镜 + capabilities 过滤）。端到端实测 POST 全 200。

**验证**：全量 **1165 passed, 1 skipped**（深化三连 +7）。端到端：截图回传 firstHasImage、HTTP POST initialize/tools/list/call 200。



### A5：跨平台系统控制（Windows 后端 + 端口/服务工具）✅
四条新方向最后一块。新增 `tools/system_control.py`：
- **port_list**：列出监听端口 + 拥有进程（macOS/Linux `lsof -iTCP -sTCP:LISTEN`，Windows `netstat -ano`）。只读透镜。
- **service_status** / **service_restart**：服务状态查询（只读）+ 重启（**T4 破坏性，high+审批**）。
  后端：launchctl（macOS）/ systemctl（Linux）/ sc（Windows）。Windows 按文档输出写但未真机验证（无 Windows 主机）。
- **system_probe 加 Windows 进程后端**：tasklist /FO CSV /NH 解析（image name+pid+mem）。
- 声明：port/service_status safe+read_only 自动放行；service_restart high+requires_approval。
- 新测试 `tests/test_system_control.py`（10 项：声明契约/自动放行/审批门/后端选择/lsof 解析/空结果/
  service mock/缺参）。

**验证**：全量 **1158 passed, 1 skipped**（本轮 +10）。端到端实测：port_list 真跑 lsof 列出端口、
service_status 真跑 launchctl（后端工作）、声明正确。



### A4：MCP server（Modus 当 server 暴露内置工具给别的 AI）✅
用户："AGI, MCP 就是重中之重"。之前 Modus 100% 是 MCP client，零 server。新增：
- **`mcp_server.py`**（新，~230 行）：stdio JSON-RPC server（镜像 client 的 `_request`/`_read_loop` 模式）。
  `initialize`/`tools/list`/`tools/call`/`notifications/initialized`。`_tool_schema` 把 Modus Tool 转 MCP
  descriptor（name/description/inputSchema）。
- **cli.py `modus mcp --serve`**：`--cwd`/`--allow-dangerous`/`--capabilities`。
- **安全（用户确认的方案，且端到端发现并修了一个真漏洞）**：
  - **默认只读透镜**：只暴露 safe+read_only+!requires_approval 工具（蓝图 T1 LENS）——26 个只读工具，
    bash/write_file/spawn 全不暴露。
  - **`--allow-dangerous`**：注册写/exec 工具，但 **headless 调用仍 deny**——第一版 call_tool 直接用
    `Tool.execute` 绕过了 ApprovalPolicy，write_file 在 allow-dangerous 下真的写入了！修复：call_tool
    先 `ApprovalPolicy.evaluate`，非 ALLOW 一律拒绝（"requires human approval; headless MCP calls are denied"）。
  - **`--capabilities`**：复用 `capabilities_granted` deny-first 门按能力类裁剪。
- 新测试 `tests/test_mcp_server.py`（12 项：透镜分类/默认只读/allow-dangerous 注册但调用拒绝/
  capabilities 裁剪/MCP schema/initialize/tools_list/read 执行/写拒绝/未知方法/通知无响应）。

**验证**：全量 **1148 passed, 1 skipped**（本轮 +12）。端到端实测：默认暴露 26 只读、bash 不可见、
read_file 正常、write_file 默认 not exposed、allow-dangerous 后暴露但 headless 调用 deny、
--capabilities filesystem 只留 12 个。



### office_exec：LLM 推理操作的 Office 弹性基座 ✅
**用户洞察**（修正了 A3 的能力设计方向）："别枚举 Office 场景写工具，LLM 已经会 openpyxl/docx/pptx，给基座让它推理操作。"——不写一堆固定工具，给 LLM 一个受控 Python 沙箱。
- **`tools/office_exec.py`**（新）：`office_exec` 工具——LLM 传受限 Python 脚本操作**单个** Office 文件。
  - **安全**：AST 扫描拒绝危险 import（subprocess/socket/网络/ctypes 等，黑名单策略——stdlib 内部依赖必须可导入因为 openpyxl 加载链用）；单目标文件经 PathGuard 校验；写操作（import docx/pptx 或 .save()）requires_approval=True；子进程隔离 + 30s 超时 + 8k 输出 cap + env 脱敏。
  - **环境**：脚本见 `PATH`（相对）+ `ABS_PATH`（绝对），用 Modus venv 的 python 跑（openpyxl 可见）。
- **保留现有 6 个快速工具**（excel_analyze/query 等）作零摩擦日常；office_exec 是弹性层。
- 新测试 `tests/test_office_exec.py`（10 项：声明契约/审批策略/import 拦截/syntax error/聚合分析/拦截/超长/缺文件/格式/venv 解释器）。

**验证**：全量 **1136 passed, 1 skipped**（本轮 +10）。端到端实测：Excel 按 region 聚合（`{"west":24500,"east":25000}`，固定工具做不到）、Word 加粗、`import subprocess` 被拦。



### A3：办公文档工具（Excel/Word/PPT）✅
用户："普通用户主要操作几万行 Excel 分析、Word 格式、PPT"。之前完全空白（0 依赖 0 代码）。
- **3 依赖入 pyproject**：openpyxl（Excel，read_only+iter_rows 流式读几万行不爆内存）/python-docx（Word）/python-pptx（PPT），全 pure-Python。
- **`tools/office.py`**（新，~380 行）6 工具：
  - **excel_analyze**：sheets/维度/header/预览行/数值列统计（mean/min/max，采样至 5 万行）。流式读。
  - **excel_query**：按列 equals 或数值 gt 过滤，有界返回。
  - **word_extract** / **word_edit**：提取段落+标题+表格；跨 runs 替换文本（保格式）。
  - **pptx_extract** / **pptx_build**：提取幻灯片文本；从 {title,body} 列表构建。
- **安全**：读工具 `data_disclosure="workspace_content"` + filesystem + safe 自动放行（T1 透镜）；写工具 medium + 审批门。
- **1MB 限制天然绕过**：read_file 是文本工具，xlsx/docx/pptx 是 ZIP 二进制本就不该走它——新 handler 直接解析，全量在本地、模型见有界摘要。
- 新测试 `tests/test_office_tools.py`（12 项：声明契约/自动放行/审批门/大表分析/查询 gt+equals/列不存在/Word 提取+替换+未找到/PPT 构建+提取+缺 slides）。

**验证**：全量 **1126 passed, 1 skipped**（本轮 +12）。端到端实测：5000 行 Excel 分析+查询、Word 替换后重提取验证、PPT 构建+提取全通。



### A2：前端批注层（元素选中 + 点评回传 LLM）✅
用户核心需求"人类点按钮后 WEB 页面元素变可批注、选中/点评/多选发给 LLM"。同源 preview 代理是关键前提。
- **`static/annotate.js`**（新，~300 行）：注入 iframe 的批注脚本。hover 高亮（outline+box-shadow 不干扰布局）、
  点击选中（capture 阶段 preventDefault+stopPropagation，跳过气泡自身）、多元素积累（≤20，角标 pin）、
  批注气泡（textarea + 添加另一个/发送/取消）、`buildSelector` 纯函数（id→class→nth-of-type，跨表面契约）、
  `parent.postMessage({type:"modus-annotate:submit", url, items})` 回传。
- **`moduswindows.js`**：批注宿主——`toggleAnnotation`（轮询等脚本就绪后发 annotate.on，修了 ready 竞态）、
  注入 annotate.js、监听 submit → pendingAnnotations → bar 计数、`submitAnnotations` → `sendUserEditedMessage`。
- **`index.html`**：批注按钮 + `#kbAnnotationBar`（计数/发送/清空）。
- **`workbench.css`**：批注按钮 + bar 样式。
- **`server.py`**：`_handle_browser_comment`——校验（items≤20/url 必须 loopback）→ 格式化
  `[浏览器元素点评]` 内容 → 委托 `_handle_explicit_run_message`（复用审批/指纹/接纳管线）。`browser_comment` WS 分支。
- **调试发现 2 个真实 bug 并修**：① annotate.js 全局 click 捕获拦截了气泡按钮点击（气泡是自身 UI 应放行）；
  ② 脚本就绪竞态（ready postMessage 不可靠，改轮询）。
- 新测试 `tests/test_browser_comment.py`（6 项：loopback 校验/空 items/超 20/远程 url 拒绝/内容格式化）。

**验证**：全量 **1114 passed, 1 skipped**（本轮 +6）。真实 Chrome debug 脚本实测完整链路：
点批注按钮 → annotate.js 注入 → 选元素（selector 正确生成）→ 气泡 → 发送 → parent 收到 → bar "1 个元素" → send 启用。
annotate.js 经 server 提供（HTTP 200, 9.5KB）。



**定位**：用户明确——Modus 要做支持本地 Agent 的个人 PC 基座，任意 OS/平台可用。四条新方向：浏览器操作、办公文档、MCP server、跨平台系统控制。用户确认**浏览器优先**。计划文件：modus-users-yinsijie-coderepo-iancli-stateless-token.md（Phase A1-A5）。

### A1：浏览器操作工具集（playwright 8 工具 + 预览窗格修复）✅
探索确认：preview 代理（/api/preview 同源）+ 前端 iframe + 事件流 metadata 透传**已存在**，缺的是 Agent 操作层。还揪出 3 个"假资产"坑（预览窗格死、#kbPreviewFrame 不存在、modus_output_dir 未设置）。
- **`tools/browser.py`**（新，~430 行）：单例 BrowserContext（模块级 holder + asyncio.Lock，lazy import playwright，channel=chrome 失败回退 bundled）+ 8 工具：
  navigate/state/extract/screenshot（read_only safe 自动放行）、click/type（medium）、**eval（high+requires_approval，唯一审批门，blocklist 拦 fetch/WebSocket/sendBeacon 逃逸）**、close。
  全 `is_concurrency_safe=False`（共享 page）。截图走 `_persist_tool_result` artifact（base64 data-URI），不落二进制。
- **`navigate` 设 `metadata.preview_url`**——零协议改动的预览挂点（default_runner 已透传 metadata 上 WS）。
- **预览窗格修复**：index.html 加 `#kbPreviewSection` + `#kbPreviewFrame`；moduswindows.js `previewFromEvent` 优先读 `metadata.preview_url`（正则 fallback）、`loadPreview` unhide section+drawer；kanban.js 删死掉的 `run.previewUrl` 分支。
- 新测试：`tests/test_browser_tools.py`（18 项）+ `e2e/test_preview_contract.py`（3 项）。

**验证**：全量 **1108 passed, 1 skipped**（本轮 +21）。debug 脚本 + e2e 实测 loadPreview → section 显示、iframe src=/api/preview?url=、服务器 200。

## 系统级 Agent 蓝图（研究综合，用户"电脑管家"方向）→ Phase 0 落地 ✅

### 后台任务模型（Paicli runtime tasks 深化版）✅
用户指出"Modus 还需要代码索引、Runtime API 后台任务模型"的第二项。不是照抄 Paicli 的纯 SQLite
队列（只有 prompt→状态，无真实执行），而是把已有的进程工具集提升为有状态后台任务：
- **spawn_process 加 task_name/description**：任务元数据写入进程注册表 meta.json。
- **后台 reaper**：spawn 时 `asyncio.create_task` 等 `proc.wait()`，自然退出后写 `exit_code` +
  `status`（0→completed，非 0→failed）+ `ended_at`。任务状态机完整：
  running/completed/failed/exited/stopped/orphaned。
- **list_processes 返回 task 字段**：task_name/description/exit_code，后台任务可枚举、可查询结果。
- **`_process_status` 感知终态**：completed/failed/cancelled 直接返回，不探 pid。
- **不是 Paicli 的队列**：真实进程执行 + 断电持久化（注册表落盘）+ 孤儿检测，比纯队列强。
- 新测试 4 项（task_name 记录/自然退出→completed/非 0→failed/list 带任务字段）。

**验证**：全量 **1090 passed, 1 skipped**（本轮 +4）。端到端实测：exit 0→completed、exit 3→failed、
task_name 随 list 返回。

### 持久代码搜索索引（Paicli code_index 深化版）✅
用户指出"Modus 还需要加上代码索引、Runtime API 后台任务模型"。先做代码索引。
新模块 `tools/code_index.py` + `rebuild_code_index` 工具 + `search_code use_index` 参数：
- **CodeIndex**：per-root SQLite（`~/.modus/code_index/<root-sha1>.sqlite3`），存 `(root, path, line, content)` 逐行。
  不同 root 隔离，同 root 幂等重建。
- **不是 Paicli 的 `LIKE %term%`**：索引只存原始行 + 缩小候选集，匹配仍走 search_code 的同一套
  word_boundary/regex/case 语义——索引路径与扫描路径**行为一致只是更快**。
- **rebuild_code_index** 工具：用既有 bounded walker（剪枝 skip 目录、PathGuard 边界、扫描上限）收集
  paths 后重建。读透镜声明（data_disclosure=none、filesystem、自动放行）。
- **search_code use_index**：命中索引走 SQL 免扫描；无索引静默 fallback 扫描（保持可用）。
- 新测试 `tests/test_code_index.py`（7 项）：重建/use_index 匹配/word_boundary 保留/fallback/
  case_sensitive/per-root 隔离/声明契约。

**验证**：全量 **1086 passed, 1 skipped**（本轮 +7）。端到端实测：rebuild 30 文件→use_index 命中、
wb 只中精确符号、scan fallback 正常。

### search_code word_boundary 精确符号匹配（function-map 符号查找）✅
function-map 符号查找缺口：search_code 是 casefold 子串匹配——`find_me` 误中 `find_me_again`、
`user` 误中 `User`。加 `word_boundary` 参数（默认 False 保持现状）：
- 字面 query：编译为 `(?<!\w)escaped(?!\w)`——只匹配完整标识符。
- regex query + word_boundary：包同款标识符围栏 `(?<!\w)(?:pattern)(?!\w)`。
- 与 `case_sensitive` 正交组合（实测 user/USER 正确区分）。
- Tool 声明加 `word_boundary` 参数。
- 新测试 5 项（默认子串/精确符号/大小写组合/regex 围栏/非法 regex 报错）。

**验证**：全量 **1079 passed, 1 skipped**（本轮 +6）。端到端实测：default 命中 find_me+find_me_again，
word_boundary 只命中 find_me；wb+cs 区分 user/USER。

### 核实：run 恢复机制早已存在（修正记忆认知）✅
探索"重启后 resume"时发现 `db.interrupt_nonterminal_runs()` 已存在且已接线
（server.py lifespan 启动调用）：断电后把 running run 标记 `interrupted/process_restart`、
settle 未终态 run_tasks、deny 待批 approvals、合成 replayable process_restart 终态事件。
`test_run_ledger.py` 10 项覆盖。**中断不覆盖 budget 列**——上轮加的周期 budget 快照 + 既有
中断机制闭环：断电后 run 变 interrupted + 保留最近 budget 快照。新增端到端测试
`test_interrupt_preserves_live_budget_snapshot` 验证。

### run 状态落盘：周期持久化 budget 快照（断电防护第 3 层）✅
**问题**：断电防护记忆"三层"之一——budget/verification 纯内存，断电后 run 停 SQLite `running`
且 budget 列空，无恢复信息。查证：`runs` 表**早有 `budget` 列**，`update_run` **支持 budget 参数
但零调用者**（grep 只在定义处出现）——持久化面存在但从未接通。
**修复**：
- `default_runner.py`：tool_result 分支每 5 次工具调用落一次
  `update_run(emitter.run_id, state="running", budget=controller.budget.snapshot())`。
  幂等、best-effort、never raised（快照失败不干扰 run）。
- 断电后 run 从"永久 running 空 budget"→"有最近 budget 快照的可恢复 interrupted 状态"。
- 新测试 `tests/test_run_state_persistence.py`（5 项）：budget 快照落盘/模拟崩溃后恢复可读/
  终态后拒写 running/未知 run 返回 False/budget 脱敏（sk-key 不落盘）。

**验证**：全量 **1072 passed, 1 skipped**（本轮 +5）。compileall + diff --check 干净。

### 只读 git 历史工具（function-map #2）✅
function-map #2 落地——git_log/git_show/git_blame，解锁 6 类任务（功能上下文、重构、
回归排查、版本比较、调试、历史符号查找）。
- `git_tools.py` 新增 3 个只读 handler：
  - **git_log**：`git log -n N --oneline --decorate [-- path]`，count 默认 20 上限 100，可路径过滤。
  - **git_show**：单提交 message + 文件变更 + 有界 diff（`-U2 --stat`，输出 ≤12000 字符），
    `stat=stat` 紧凑 stat-only 视图。
  - **git_blame**：逐行归属（`git blame path [rev]`），输出 ≤12000 字符。
- `_clone_tools` 注册：read_only=True + danger=safe → ApprovalPolicy 自动放行（零审批摩擦）。
  能力 filesystem+exec（本地操作，无 network）。
- 新测试 `tests/test_git_history_tools.py`（9 项）：log 列提交/计数有界/路径过滤、show 详情/stat/
  缺 rev、blame 归属/缺 path、声明契约。

**验证**：全量 **1067 passed, 1 skipped**（本轮 +9）。Modus 自身 repo 实测 git_log/show/blame 全通。

### 正常关闭清理：atexit + 信号处理器（用户"直接关闭软件"问题的回答）✅
**问题**：会话执行进程时直接关闭软件——CLI `main` 和 server `start_server` 都无退出钩子，
spawn_process 的后台进程会继续跑。
**方案**：新模块 `process_cleanup.py` `install_process_cleanup()`：
- **atexit**：覆盖所有正常解释器退出（Ctrl-C/EOF/exit），清理时 killpg 本进程 spawn 的存活进程组。
- **SIGTERM/SIGINT 处理器**：service manager/`kill` 发信号也能触发清理，然后 re-raise 保留退出码。
- **只碰本进程的**（`spawned_by == os.getpid()`）：上一轮 Desktop 的 orphaned 进程不自动杀（留给用户决定）。
- **优雅终止**：SIGTERM → 2s 宽限 → SIGKILL 兜底（让能处理 SIGTERM 的子进程先自我清理）。
- **fail-safe**：signal 权限失败不抛异常（不破坏退出），状态保守保持 running。
- 接线：CLI `main` + server `start_server` 顶部调 `install_process_cleanup()`。
- 新测试 `tests/test_process_cleanup.py`（6 项）：owned 清理/foreign 不动/已退出跳过/install 幂等/
  signal 重发语义/signal 失败 fail-safe。

**验证**：全量 **1058 passed, 1 skipped**（本轮 +6）。端到端实测 spawn 后 cleanup → 进程终止、meta 标 exited。
注意：pytest 沙箱不能 signal 独立进程组（PermissionError），测试验证 cleanup 契约而非真实 kill；
真实 spawn_process 路径是继承进程组，无此限制。

### 后台进程工具集（function-map #1 最大缺口）+ 断电防护持久化 ✅
**背景**：function-map 最大单缺口——bash 阻塞到 EOF，进程无法监控/重启。四个工具
给 agent 跨工具调用的进程句柄，且**天然带断电防护的持久化层**（用户提醒的孤儿进程问题）。
新模块 `tools/process_tools.py`：
- **spawn_process**：后台运行命令（start_new_session 进程组），stdout/stderr 落盘
  `~/.modus/processes/<id>/{stdout,stderr}.log`，meta.json 记录 pid/command/cwd/started_at/spawned_by。
  返回 process_id。CommandGuard 黑名单 + 解释器/sudo 绕过拦截同样生效。
- **list_processes**：`modus.processes.v1` JSON，`os.kill(pid,0)` 探测实时状态：
  running / stopped（pid 没了）/ **orphaned（存活但 spawned_by 不是当前 Modus 进程——Desktop 崩溃后重启识别）** / exited。
- **tail_process**：读 stdout/stderr 日志的有界尾部（64KB seek，不阻塞）。
- **kill_process**：killpg 终止进程组 + 标记 registry（活进程→exited，已死→stopped 幂等）。
- **断电防护关系**：进程注册表落盘 = 崩溃后孤儿可识别、可恢复。这就是记忆里
  modus-power-loss-guard-reminder 的持久化层；PDEATHSIG/kqueue 实时防护 + daemon runner 仍是后续。
- **安全**：spawn/kill 是 danger=high + requires_approval；list/tail 只读 free。
  子代理 SUBAGENT_ALLOWED_TOOL_NAMES 不含它们（不可见）。CommandGuard 在 spawn 前校验。
- 新测试 `tests/test_process_tools.py`（15 项）：声明契约、registry 往返、pid 探测、
  生命周期 round-trip、kill 幂等/活进程 exited、孤儿检测（模拟重启）、命令策略拦截、子代理不可见。

**验证**：全量 **1052 passed, 1 skipped**（本轮 +15）。端到端实测 spawn→list→tail→kill 全通；
模拟 Desktop 重启（改 spawned_by）→ list 报 orphaned，恢复所有权→running。

### 有界扫描截断披露（function-map #5 + 蓝图第 7 条"截断诚实披露"）✅
**问题**：`_iter_bounded_files` 到 cap 就静默 return，grep/search_code/glob 会把截断结果当完整
返回——10k+ 文件研究里读出假"无匹配"（function-map 列为横切正确性修复 #7）。
**修复**：
- `_iter_bounded_files` 加 `on_truncate` 回调——cap 触发时通知调用方（不破坏三个消费点的 async for）。
- grep/search_code：截断时追加 `... [扫描达上限 N 文件，结果不完整]`，与既有 `[limited to N matches]`
  （limit 截断）独立区分。
- glob：`**` 递归分支截断时在结果尾部追加同款披露。
- 关键设计：`_scan_cap` 钳制到 `max(100, ...)`（sane range），截断披露显示实际生效的 cap。
- 新测试 6 项（grep/search_code/glob 三工具截断披露 + 未截断无披露 + walker on_truncate 回调）。

**验证**：全量 **1037 passed, 1 skipped**（本轮 +5）。compileall + diff --check 干净。

### Phase 2：system_probe 读透镜 ✅
蓝图 Phase 2 落地："系统状态透镜（schema 限定 JSON 快照）"，"管家的眼睛"，零审批摩擦。
新模块 `tools/system_probe.py`（纯标准库，无 psutil）：
- **payload**：`modus.system.v1` JSON——platform（platform 模块）/ cpu（os.cpu_count + getloadavg + os.times）/
  memory（os.sysconf 总量 + 分平台 free：macOS vm_stat、Linux /proc/meminfo、Windows unsupported）/
  disk（shutil.disk_usage 对 /、~、cwd）/ processes（有界，macOS ps -r 前 7 列 + comm 截断名，
  Linux /proc/*/stat+status，**绝不读 cmdline**）/ logs（路径+文件数+总字节，**绝不读内容**）。
- **安全**：`data_disclosure="none"` + `capabilities=("filesystem",)`——ApprovalPolicy auto 模式自动 ALLOW
  （safe + read_only + not requires_approval），能力门在默认/T1 只读运行都放行。进程行含 pid/cpu/mem/rss
  无 argv；日志只返回聚合大小；有界（_MAX_PROCESSES=20、log cap=500、os.walk 早停）。
- **TCC 两结果**：每源 `{path, exists, readable, error?}`（not_found/permission），诚实呈现 ENOENT vs EACCES，
  完整 TCC 三结果留 Phase 3（研究 agent 确认纯标准库无法确定性区分 TCC）。
- **声明**：builtins.py `get_builtin_tools()` 尾部 + Tool 声明（无参数或 max_processes/include_logs），timeout 15s。
- **桌面**：timeline.js `summarizeToolResult` 加 `modus.system.v1` 分支→"系统快照 · 负载 X · N 核 · 磁盘余 Y% · M 进程"。
  浏览器验证：node --check 过 + preview 实测返回正确摘要。metadata 自动透传（default_runner 已有）。
- **接线确认**（研究 agent 核实）：不调 PathGuard（/var/log、/proc 是系统保护路径，设计使然）；tools.enabled/disabled
  自动生效；子代理 SUBAGENT_ALLOWED_TOOL_NAMES 不含它（不可见）。
- 新测试 `tests/test_system_probe.py`（13 项）：声明契约、ApprovalPolicy ALLOW、能力门、executor 端到端、
  payload 形状、进程有界无 cmdline、日志无内容、include_logs 开关、各探针不抛异常。

**验证**：全量 **1032 passed, 1 skipped**（本轮 +13）。compileall + diff --check 干净。preview 实测 timeline 渲染正常。

### Phase 1：CommandGuard 解释器/复合命令绕过封闭 ✅
蓝图 Phase 1"CommandGuard 解释器前缀拒绝 + 复合命令强制 ASK（结构性关闭 MF1 绕过类）"。
实测确认绕过通道全开：`sh -c "rm -rf /"`、`bash -c "..."`、`sudo apt install`、`xargs rm -rf`、
`python3 -c "..."` 全部放行。改动 `policy/command_guard.py`：
- **shell `-c` 拒绝**：`sh/bash/zsh/dash/ksh/csh/tcsh/fish -c` 运行不可静态分析的字符串 → fail-closed。
  （`-e` 是 shell 合法 errexit flag，不拦；`sh -e script.sh` 放行。）
- **interpreter `-c`/`-e` 拒绝**：`python/python3/perl/ruby/node/php -c`（及 perl/node `-e`）同原理拦截。
- **`env` 解包重分析**：`env FOO=1 sh -c "..."` / `env python3 -c "..."`——env 是薄包装，
  跳过选项和 `NAME=VALUE` 后对真实命令再走解释器拦截。
- **`sudo` 默认拒绝**：T5 语义（权限提升超出守卫模型），`CommandGuard(block_sudo=False)` 可配置。
- 安全命令不受影响：`sh script.sh`、`python3 script.py`、`python3 -V`、`sh -e script.sh`、
  `ls -la`、`env LANG=C ls -la` 全放行。
- 新测试 6 项（shell/interpreter/env/sudo 拦截 + 安全放行）。

**验证**：test_command_guard.py 12→18 项全过；bash/security 回归 25 项全过。

**研究结论**：Modus 从"编程 CLI"走向"系统级瑞士军刀"，不能靠写死命令，要按
**抽象能力类 + 权限阶梯 + 运行时强制**设计（对标 Windows Agent Runtime/MXC、macOS TCC、
VS Code Workspace Trust、K8s operator、Spotlight 索引教训）。能力阶梯
T0 LOCKED → T1 LENS(读) → T2 SCOPED WRITE → T3 EXEC → T4 DESTRUCTIVE → T5 sudo。
用户已确认方向，从 **Phase 0：工具声明策略块** 开始动手。

### Phase 0：Capability 声明 + executor deny-first 能力门 ✅
- **`tools/capabilities.py`**（新）：`Capability` StrEnum（filesystem/exec/network/memory/agent）
  + `capabilities_granted()` 纯函数——deny-first：`granted=None`（默认）全部授予（现状不变）；
  显式 grant 集 fail-closed，未声明能力一律拒绝。
- **`tools/base.py`**：`Tool.capabilities`（tuple）+ `ToolContext.granted_capabilities`（None=不受限）。
- **`tools/executor.py`**：`_execute_single` 在**审批之前**检查能力门——未授予的工具直接拒绝，
  不进入 ApprovalPolicy，不会弹审批卡。与审批/PathGuard/CommandGuard 正交（各自答一个问题）。
- **`builtins.py`**：27 个内置工具全部声明能力块——透镜（read/grep/search_code/list_dir/glob）filesystem；
  write/edit/revert_turn filesystem(+exec)；bash/run_tests exec；web_search/web_fetch network；
  save/search_memory memory；git 工具 filesystem+exec+network。
- **`subtask.py`/`peri.py`** spawn_subtask → agent；**`extensions.py`** MCP 工具 → agent。
- **`config.py`**：`PolicyConfig.capability_grant` + `MODUS_POLICY_CAPABILITY_GRANT` env。
- **接线**：react.py + peri.py 把 `config.policy.capability_grant` 注入 ToolContext。
- 新测试 `tests/test_capability_grants.py`（13 项）：纯函数矩阵、deny-before-approval、
  granted 正常运行、内置声明完整性、env 接线、dict 往返。

### Phase 0 补齐（对照蓝图自查发现 2 处遗漏，均属 Phase 0 声明面）
- **AuditLog 加 phase + verification 字段**：`policy/audit_log.py record()` 加可选
  `phase`（默认 "execution"）+ `verification` dict——旧调用向后兼容。CLI 审计
  `_record_cli_audit` 把影响分类写进 phase。
- **影响分类 + 审批卡执行预览**：executor approval request 加 `impact_class`
  （`_impact_class`：read-only / mutating），CLI rich 卡 + 桌面 timeline.js 审批卡
  都显示"影响"。这是人审上下文，强制仍在守卫层。
- 新测试 4 项（_impact_class 分类、approval request 携带、audit 字段写入、默认兼容）。

**验证**：全量 **1013 passed, 1 skipped**（本轮 +4）。compileall + diff --check 干净。

### 审计遗留修复（对照 audit-remaining-layers 工作流逐条核实）
- **spawn_subtask max_depth no-op 修复**：`agent/subtask.py` 新增
  `_decrement_recursion_depth()`——子任务 ReActReasoner 读 `config.features.convergence.max_recursion_depth`
  决定是否再暴露 spawn_subtask，父 config 必须深拷贝并递减深度，否则 `max_depth` 是 no-op、
  递归只受软 turn/token 预算限制。失败时保守关闭递归（fail-closed）。Peri 路径无此 bug
  （已有 depth+1 传递）。新测试 `tests/test_subtask_recursion_depth.py`（4 项）。
- **其余审计发现核实**：WORK_LOG 声称修复的 mustFix/shouldFix 逐条 grep 代码验证，
  全部在代码中落地（verification per-generation、compressor turn-aligned tail、
  plan_execute messages_snapshot、agent.py select_reasoner、memory dedup 等）。

### 研究产出落盘（此前研究只留在 workflow journal，未变可决策交付物）
- **`docs/system-agent-blueprint.md`**（新）：research-system-agent 完整综合——10 条设计原则、
  13 个能力抽象、T0-T5 权限阶梯、6 阶段路线图、11 项真实风险。后续系统级开发决策锚点。
- **`docs/function-map.md`**（新）：map-agent-functions 完整综合——22 类任务能力矩阵
  （能做/部分/不能）+ 20 项按解锁类别数排序的推荐构建。
- 两套研究实际都已完成（journal 有完整结果），audit-remaining-layers 工作流进程中断
  但 5 lens + 综合 agent 均产出。此前摘要误称"研究完成"是 j让 journal 落盘缺失所致。

**验证**：全量 **1005 passed, 1 skipped**（基线 955 → +50；本轮 +13）。compileall + diff --check 干净。

**意义**：现在一条 `MODUS_POLICY_CAPABILITY_GRANT=filesystem` 就能把整个 run 锁成只读透镜
（T1），未来 T3/T4 阶梯、系统状态透镜 system_probe、守护循环全部在这层能力声明上叠加。
Phase 0 是蓝图中最小、解锁后续一切的落地。


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

## 里程碑 5：TurnInsight 自省层 + 无进展检测（设计评审第 1 项）

### 自省账本（TurnInsight）
- **`runtime/budget.py`**：`TurnRecord` dataclass（turn/text_chars/tool_calls/tool_successes/tool_errors/tokens/stop_reason）+ `record_turn()` + `recent_turn_records()` + `stalled_for(threshold)`。账本有界（≤200 条）。`snapshot()` 带 turn_records。
- **`react.py`**：每轮工具执行后 `budget.record_turn(...)`（记录 text 长度、工具成败、tokens）。这是"循环终于会看自己"的起点。

### 无进展检测（NO_PROGRESS）
- **`runtime/budget.py`**：`StopReason.NO_PROGRESS`。
- **`react.py`**：循环头 `budget.stalled_for(threshold)` → 提前终止。
- **`config.py`**：`RuntimeConfig.no_progress_threshold`（默认 4，0 关闭）+ `RUN_NO_PROGRESS_THRESHOLD` env。
- **端到端接线**：default_runner reason_map + 中文 message + semantic_projection `_BUDGET_STOPS`。
- 新测试：budget 层 4 项 + reasoner 端到端 1 项（自旋 agent 3 轮无进展 → no_progress，不烧满 20 轮）。

### 对抗性验证（Workflow 4 视角 + 综合 → **fix-first**，已修复）

对抗验证发现检测器的真实缺陷并全部修复：
1. **run_tests/bash 失败被误杀**（HIGH，核心工作流）：`is_error` 是正常调查信号（红测试、非零退出），原 `made_progress()` 把它们当 no-progress。→ 修复：`made_progress()` 改为**真活动**（text OR thinking OR 任意工具调用），工具失败不再计入 no-progress。
2. **chatty spinner 永不触发**（HIGH，false-negative）：`text_chars>0` 让闲聊式自旋永远算 progress。→ 修复：弱化 text 信号，工具调用也算活动（见上）。
3. **thinking_delta 被丢弃**（HIGH）：思考中的模型可能 0 文本。→ 修复：TurnRecord 加 `thinking_chars`，思考算活动。
4. **验证门被抢占**（HIGH）：有未验证 mutation 时 stall 检查应跳过，让验证循环管终止。→ 修复：stall 检查 `not verification_required`。
5. **Plan-Execute/spawn_subtask 共享 budget 跨任务误杀**（MEDIUM）：`_task_budget()` 定义未用。→ 修复：每个 plan task / subtask 用独立 RunBudget 做 stall 窗口，完成后合并 token/turn 回父 budget。
6. **首轮探索误杀**（MEDIUM）：无宽限期。→ 修复：`stalled_for` 加 `warmup_turns` + `min_elapsed_seconds` 双下限。
7. **信息性拒绝**（MEDIUM）：read_file>1MB 等 refusal 也算 no-progress。→ 由修复 1 覆盖（工具调用即活动）。

**设计结论**：检测器宁可 false-negative（不杀沉默 spinner）也不 false-positive（误杀调查中 agent）。NO_PROGRESS 现在是安全网：只在极端输入下触发（预算层逻辑有测试覆盖），核心价值是**绝不误杀正常调查/编辑/测试循环**。

## 里程碑 6：7 方向成熟化（执行蓝图）

**成熟方案研究工作流产出 15 项改进，按执行序排列**（3 视角 research + 首席综合，9 项过度设计被砍）：

| 序 | 方向 | 项 |
|---|---|---|
| 1 | 核心循环 | error-classification 驱动行为：`recovery_policy()` 映射 FailoverReason→动作；context_overflow 强制压缩+重试一次；rate_limit 有界退避；auth/billing 终端化 |
| 2 | 策略 | PlanExecute 并行批次（read/analysis/verification 并发，write/command 串行） |
| 3 | 产品化 | CLI session/memory 接线（REPL 注入记忆+recall+持久化，与桌面 session 隔离） |
| 4 | 记忆 | 写入去重 + 来源合并（同 category 高重叠归档旧行） |
| 5 | 记忆 | 语义压缩默认开 + 静默回退 |
| 6 | 策略 | 失败重规划（1 次/run，plan_replan 事件，验证中关） |
| 7 | 策略 | 智能策略选择 `select_reasoner`（复杂+改文件→PlanExecute，会话→ReAct） |
| 8 | 记忆 | 跨 run 验证持久化 + 回忆（类似目标不重做已验证工作） |
| 9 | 自省 | 自适应 turn_records 循环（默认关）：tool-error hotspot 提示、context_overflow 降阈值、run_autopsy 记忆 |
| 10 | 可观测 | 语义投影加 retrospective 块（stop_reason/证据/相位/turn 条） |
| 11 | 可观测 | KANBAN 消费 aggregate_board |
| 12 | 产品化 | `modus audit` CLI verb |
| 13 | 产品化 | 文件型单 checkpoint resume（deferred） |
| 14 | 安全 | 审计闭环 + 拒绝原因 + 安全不变量回归测试 |
| 15 | 安全 | 每步快照 + GC（last priority） |

**砍掉的过度设计**：每类错误状态机、LLM 策略路由、planner-verifier 图、跨 run 强化学习、verify_claim 工具、typed self_insight 事件、分析管线、CLI daemon、RLIMIT 沙箱、新 StopReason 枚举。

**执行约束**：事件词汇稳定、安全不变量保持、每项单独实施+测试+验证后再进下一项。任务 #30-#35 对应方向 1/3/4/5/6/7。

### 方向 1（核心循环）— 规划项 1：error-classification 驱动行为 ✅
- `llm/errors.py`：`RecoveryAction` 枚举 + `recovery_policy()` 纯函数（RETRY_WITH_BACKOFF / FORCE_COMPACT_AND_RETRY / SURFACE_TYPED / FAIL_CLOSED），默认 FAIL_CLOSED。
- `llm/retry.py`：`retry_chat` 用 classifier 决定重试（替换无条件重试）——429/5xx/timeout/传输错误重试，auth/billing/context_overflow/400 快速失败；error 事件带 `failover` 字段。
- `react.py`：error 分支分类驱动——context_overflow 零产出时压缩+重进一次（`skip_turn_finalize` 防被误判 COMPLETED）；auth/billing 终端化带 failover reason；error 事件新增 `failover` 字段。
- 新测试 4 项（recovery_policy 映射、401 不重试、context_overflow 自愈、auth 终端化）。

### 方向 5（策略）— 规划项 2：PlanExecute 并行批次 ✅
- `plan_execute.py`：`_run_task_batch` 并行化——read/analysis/verification 任务 `asyncio.gather` 并发，write/command 串行；每个任务独立 `_task_budget` + stall 窗口，`usage_ledger` owner `plan:task_<id>`；`_execute_task_stream` 抽成独立流。
- 新测试 1 项（并发 wall-clock < 串行）。

### 方向 5（策略）— 规划项 6+7：失败重规划 + 智能策略选择 ✅
- **重规划**：`plan_execute.py` 用 `_task_outcomes` 记录每个任务的 inner stop_reason（含 error → engine_error），批次后失败任务触发 `plan_replan` 事件（上限 1 次/run，验证中关闭），`_make_plan` 重规划剩余目标，`completed` 撤销失败任务。
- **智能选择**：新 `strategies/select.py` `select_reasoner()` 纯函数——多步+文件意图→PlanExecute，会话→ReAct，agent_mode 兜底，explicit_factory 优先。接入 `Agent.run`。
- 新测试 3 项（replan 触发+恢复、select 启发式、agent_mode pin）。

### 方向 3（记忆）— 规划项 4+5 ✅
- **写入去重**：`db.add_memory_record` 加 `dedup` 参数（默认 True）——同 scope+category 精确或 token 重叠 ≥0.90 的记忆不重复插入，刷新 updated_at 并合并 source_ids（幂等写入，auto-memorize/working-memory 可安全重复）。`_memory_overlap` 纯函数。`dedup=False` 可绕过。
- **语义压缩默认开**：`CompressionConfig.semantic` 默认 True（端到端 `_maybe_compress_history` 已支持语义摘要 + 静默回退；mid-run 保持确定性以免热循环阻塞模型调用）。
- 新测试 4 项（精确去重合并来源、高重叠归档、不同事实共存、opt-out）。

## 会话进度（7 方向成熟化阶段）

**当前全量：955 passed, 1 skipped**。compileall + git diff --check 通过，18 文件改动未提交。

**已完成（本轮）**：
- 方向 1（核心循环）✅：error-classification 驱动行为（recovery_policy + retry_chat 分类 + context_overflow 自愈 + auth 终端化）
- 方向 5（策略）✅：PlanExecute 并行批次 + 失败重规划 + 智能策略选择（select_reasoner）
- 方向 3（记忆）部分 ✅：写入去重 + 来源合并、语义压缩默认开

**待续（下一轮）**：
- 方向 3 剩：跨 run 验证持久化 + 回忆（项 8）
- 方向 4（自省）：自适应 turn_records 循环（项 9，默认关）
- 方向 6（可观测）：retrospective 语义投影块 + KANBAN 消费聚合（项 10/11）
- 方向 7（产品化）：CLI session/memory 接线 + modus audit（项 3/12）
- 方向 2（安全）：审计闭环 + 拒绝原因 + 安全不变量测试（项 14）

**执行方式**：严格"找成熟方案（WORK_LOG 蓝图已定）→ 规划 → 实施 → 测试"，每项独立验证，全量收口。

### 方向 7（产品化）— 规划项 3+12 ✅
- **CLI session/memory 接线**：`cli.py` 加 `_repl_session()`（复用 `_data_dir` + `_active_session`，session 标签 `cli`）+ `_memory_message()`（query-scoped 记忆注入）。REPL 每轮前注入记忆系统消息，轮次后 `add_message` 持久化到 session。CLI 现在**跨会话记得**。
- **`modus audit` verb**：读 `AuditLog.tail`，支持 `--tail N` + `--tool X` 过滤，输出脱敏（redact_dict）。
- 新测试 3 项（memory message 构建/空、audit tail 脱敏+过滤）。

### 方向 4（自省）— 规划项 9：自适应 turn_records 循环 ✅
- **`budget.trends(window)`** 纯函数：tool_error_rate / tool_error_hotspot（近窗错误率 ≥50%）/ consecutive_no_progress / error_classes / text_silence_ratio。
- **`react.py _maybe_adapt`**：`config.features.self_adapt`（默认关）+ `SELF_ADAPT` env 控制。开启时每轮读 trends，tool-error hotspot 注入一条 `[SELF-ADAPT — REFERENCE ONLY]` 有界提示（不换策略、不循环、非新事件类型）。
- 新测试 2 项（trends hotspot/clean）。

### 方向 6（可观测）— 规划项 10：retrospective 语义投影块 ✅
- `semantic_projection.py`：`project_semantic_run` 加 `retrospective` 字段（`_retrospective` 纯函数）——stop_reason / verification / evidence_attempts+passed / phases 摘要 / turn_strip（≤50）/ metrics。全部派生自已有投影 + 终端 budget 的 turn_records，无新事件无模型调用。
- 新测试 1 项（派生正确性 + turn strip 上限）。

### 方向 2（安全）— 规划项 14：审计闭环 + 安全不变量回归 ✅
- **CLI 审计闭环**：`_cli_approval_callback` 每个决策（y/s/m/n）调 `_record_cli_audit`，写入 `AuditLog`（outcome=`approved:y` 等 + approver=`cli-human`）。CLI 审批不再"批完即忘"。
- **安全不变量回归测试** `tests/test_security_invariants.py`（5 项）：approve-then-execute 顺序、deny 永不执行、模型输入先校验后执行、input_hash 稳定绑定、快照先于首次变更。

### 方向 3（记忆）— 规划项 8：跨 run 验证持久化 ✅
- `default_runner._persist_verification_state`：run completed 时把最终验证状态（status/stop_reason/has_mutations/attempts）持久化为 run 工作记忆 `verification-state`。后续类似目标经 memory/recall 注入可回忆"上次已验证"，避免重做已验证工作。
- 新测试 1 项。

### 方向 6（可观测）— 规划项 10 done；项 11 判定非必要
- **项 10 retrospective ✅**（上轮）。
- **项 11 KANBAN 消费聚合：跳过**——前端已有客户端列聚合（columnAttentionCount），服务端 `kanban_board` DTO 已就绪供未来使用。改动前端有回归风险且收益低，判定非必要。方向 6 完成。

### 审计修复（全局优先级，前 4 项 mustFix）✅
1. **CRITICAL 验证门计数器 per-mutation**（`verification.py`）：调查期失败 run_tests 不再耗尽 post-edit 预算——mutation 落地时 `attempts=0`，`retry_exhausted` 要求失败证据 `mutation_generation` 等于当前 generation。修复了"3 次调查失败 + 首次编辑即杀 run"的核心循环破坏。新增回归测试。
2. **HIGH compress_messages 丢指令 + 孤儿 tool**（`compressor.py`）：tail 按 turn 对齐（不从中途 tool 开始）+ 保留最后一个 user 指令。修复了长 run 静默丢当前请求 + provider 400。新增 2 回归测试。
3. **HIGH PlanExecute 丢任务历史**（`plan_execute.py`）：`_execute_task_stream` 记录 pre-context 长度，任务新增消息合并回 `messages_snapshot`，`done.messages` 反映完整历史（ReAct 原地改 task_context，需 pre 长度对比）。新增回归测试。
4. **HIGH Agent.run 选错 reasoner**（`agent.py`）：用当前 `message` 优先选策略，history 仅兜底。修复策略冻结在第 1 轮。新增回归测试。

### 审计修复（工具层 mustFix 4 项）✅
5. **MF3 无界 rglob**（`builtins.py`）：新增 `_iter_bounded_files` async walker——剪枝 `_SKIP_DIRS`（.git/.venv/node_modules/__pycache__ 等）、上限 `_MAX_SCAN_FILES=5000`、每 128 项让出事件循环。grep/search_code/glob 全改用。**根治了 CPU 打满**（你遇到的 90% CPU 根因）。新增 3 测试。
6. **MF2 snapshot commit_id**（`snapshot.py`）：改用 `git rev-parse HEAD` 取真实 hash（原取 `git commit` 摘要导致 revert_turn 静默失效）。新增回归测试。
7. **MF1 CommandGuard 绕过**（`command_guard.py`）：重写为 shlex 规范化——`rm -rfv /`、`rm -f -r /`、引号、系统根、~/.ssh 全拦；mkfs/shred/dd 裸设备/电源命令。新增 `tests/test_command_guard.py`（12 测试）。
8. **MF6 bash/run_tests 输出上限**（`builtins.py`）：`_capture_stream_output` 流式读取 + `_STREAM_OUTPUT_CAP=50MB` 硬上限，超限 kill 进程组。新增测试。

### 审计修复（追加 2 项）✅
9. **MF8 bash env 泄漏**（`builtins.py`）：`_safe_shell_env()` 过滤敏感键（key/token/secret/password/credential/auth/bearer/session），bash/run_tests 不再把 `MODUS_API_KEY` 等泄漏给子进程。新增测试。
10. **scan 上限调大 + 可配置**（`builtins.py`/`config.py`）：`_MAX_SCAN_FILES` 5000→20000（覆盖 Modus 自身 4050 文件），`ToolsConfig.max_scan_files` + `MODUS_TOOLS_MAX_SCAN_FILES` env 可配，`_scan_cap()` 读取。新增测试。

### 用户洞察（驱动后续开发）
用户提出："我应该向 Agent 描述它要做什么，Modus 应从'Agent 要执行的任务'反推能力/权限/安全需要"。据此启动**职能映射工作流**：从软件工程/运维/研究/自动化四类真实任务反推 Modus 能力矩阵与缺口。

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



## 学习对象综合拆解 + Modus 对比（2026-08-08）

### 输入
- `/Users/yinsijie/Downloads/Modus 学习对象/` 12 个项目，全部源码级拆解（12 agent 并行，4.5M tokens）
- 2 轮 14-agent 工作流：对比综合（10 维度 57 gap/68 strong/45 pattern）+ 盲区扫描（4 视角 16 方向）
- 产出：`docs/learning-synthesis.md`（精选 12 缺口 + 8 盲区 + P0/P1/P2 路线）

### 核心结论（Modus 不缺工具，缺三层）
1. **韧性**：run 本身不落盘（RAM 对象），崩溃=重付；进程 PID 复用误杀隐患；desktop.db 多实例裸写
2. **自主性**：run budget 是防御性，缺"目标驱动续跑"（CCB Goal 7 态机 + 3-strike）
3. **协议层**：内部安全机器跨进程坍塌成二值，能力身份被哈希抹掉（可移植到 MCP 线的信任序列化）

### 12 项目最高价值点
- **PentesterFlow**：作用域化审批（cacheKey 绑载荷）+ 证据强制 confirm_finding —— 最可移植
- **CCB V5**：Goal 状态机 + Workflow 确定性编排 —— 最独到
- **cc-haha**：deny 回灌模型 + preview HTML 改写 CSP —— 最贴合桌面
- **peri**：prompt cache 98.5%（3 断点 + frozen）+ 三层 compact re-inject —— 最省成本
- **AssetOpsBench**：轨迹→离线重评分 + 大结果句柄化 —— Modus 唯一缺的质量闭环
- **beads-web**：单文件真相源 + 文件监视推送 + GitOps 闭环 —— 看板升级为指挥台

### 待办（P0 候选，未实施，等用户验收后启动）
- P0：作用域化审批缓存 / deny 回灌模型 / prompt cache 工程 / 进程出身校验
- P1：Goal 续跑 / 结构化压缩 re-inject / FTS5 中文检索 / 轨迹重评分 / 数据治理+健康回路 / 多实例协调
- P2：Run 状态机可重放 / 信任序列化 / GitOps 闭环 / 全局回滚 UI / idle-watch 守护

## 借鉴开发文档（2026-08-08）

将 12 项目借鉴架构拆成 5 波可执行文档（docs/dev-*.md），每波含：子项/参考细节（源项目机制+文件证据）/落地入口（Modus 文件+函数）/测试/验收。

- **dev-wave1-resilience**：韧性地基（T1 进程出身堵 PID 复用误杀 / T2 数据治理 / T3 审批超时+watchdog / T4 多实例协调+schema 版本化 / T5 进程状态机）
- **dev-wave2-context**：上下文经济学（C1 prompt cache 3 断点 / C2 compressor re-inject 文件清单 / C3 大结果句柄化）
- **dev-wave3-trust**：信任审批（A1 作用域化审批缓存 / A2 deny 回灌模型 / A3 coverage 矩阵）
- **dev-wave4-autonomy**：自主性（G1 Goal 跨轮状态机 / G2 停滞检测 / G3 后台完成唤醒续跑）
- **dev-wave5-evolution**：评估进化（E1 轨迹重评分 / E2 后台审查 fork / E3 会话树）
- **dev-index**：总索引（执行顺序/依赖/不变量）

建议执行顺序：Wave1 T1→T2→T3 → Wave3 A1→A2 → Wave2 C1 → Wave1 T4 → Wave2 C2/C3 → Wave4 G1/G2 → Wave3 A3 → Wave5 E1。
全部未实施，等用户验收后按顺序开工。

## Wave0 并发重活架构层（2026-08-08）

用户从纯用户视角提出硬要求：打开 Modus 时所有渲染快速美观、点击丝滑、能同时干很多重活（Excel 分析+系统优化+coding+Chrome preview 并行）、内存管理优秀到用户不抱怨"又把内存吃完了"。

结论（多轮讨论修正）：
- 用户三条要求里，"同时干重活 + 内存不抱怨"是 Python 的语言层劣势（GIL 限并行、GC 不保证及时回收、碎片不还 OS）——这是架构补不全的，最终要 Rust 下沉。
- 但"现在换语言"是错的：还没造出"同时跑 N 个重活"场景，改写无可测负载。正确路径 = 先搭"并发重活架构层"（多进程 worker 隔离 + 内存有界回收），它既是现在 Python 的出路，也是未来 Rust 下沉的边界。
- 语言策略修正：坚持 Python，但只坚持到"并发重活架构层"建立为止。之后每个 worker 实现语言按实测决定（测到内存/性能不过关才下沉 Rust，走 MCP 边界）。边界必须是协议（MCP/JSON），不是 FFI——FFI 传染 GIL/崩了拖垮主进程。

产出：docs/dev-wave0-concurrency.md（W0-1 多进程 worker 隔离+内存上限 / W0-2 office_exec 下沉 / W0-3 事件订阅+Tick 节拍 / W0-4 Rust 下沉边界契约）。已并入 dev-index.md 总路线（Wave0 放最前）。

12 项目进程实践确认：supervisor（状态机/事件监听/捕获标记/周期节拍）和 hermes（子进程 RPC/后台 fork/OS 隔离声明）是最强参考；loop（跨进程锁）/beads（文件事件总线）/peri（idle 唤醒）各补一块；AssetOpsBench（每次新 MCP 子进程无连接池）是反模式；pi 无权限系统是反例。

建议：W0-1 + Wave1 T1（进程出身校验）一起做——都动进程层，避免两次改同一文件。

## Wave0 W0-1 + Wave1 T1 落代码（2026-08-08）

**W0-1 多进程 worker 隔离层**（`runtime/workers.py` 新增，~380 行）：
- Worker/WorkerPool：独立进程 + 内存上限 + 并发队列 + 回收；`submit/cancel/wait/list/status/drain_events`
- 内存硬上限：watchdog RSS 采样超限即 kill（跨平台可靠）；Linux 额外 RLIMIT_AS 硬边界（preexec）
- macOS 坑已解决：RLIMIT_AS soft==hard → 需先抬 hard 再降 soft；且 macOS vsz 巨大使 AS 限制不可用，主机制定为 RSS watchdog
- 事件流：worker_queued/started/completed/failed/cancelled/oom/memory_warn
- `_worker_pool_for()` 进程级单例，config `runtime.worker`（enabled 默认 False，向后兼容）
- `office_exec` 支持 `worker: true` → 走 WorkerPool（默认同步路径不变）；`_run_script_worker/_build_worker_code/_read_worker_output`
- WS `worker_pool` command：桌面可见 worker 状态（enabled/workers/events）
- 测试：test_workers.py 8 + office worker 2 + WS 1

**Wave1 T1 进程出身校验**（`tools/process_tools.py` + `process_cleanup.py`）：
- `_read_born_at(pid)`：Linux /proc/stat btime+starttime；macOS/BSD `ps -o lstart=`（macOS strptime 不支持 %e，用 %d 兼容空间填充）
- `_pid_identity_ok(meta)`：pid 存活 + born_at 匹配（允许 <1s 偏差）；legacy 无 born_at 向后兼容
- `spawn_process` 记录 born_at；`_process_status` 加 pid_reused 状态
- `kill_process`：pid 被复用 → 拒绝 SIGKILL 无辜进程，标 pid_reused
- `process_cleanup.py`：正常退出清理也走 identity 校验，防误杀
- 测试：test_process_tools.py +5

**验证**：全量 **1190 passed, 1 skipped**（基线 1165 → +25）。六条安全不变量不回退（审批门未动、capability 门未动）。

## 并行开发三 stream（2026-08-08）——3 agent 同时开发

**方法**：按文件独占分区并行（3 个 agent 各改不重叠文件），我作协调者合并验证。冲突点预先设计：executor 只 C 动、audit_log 只 A 动、builtins 共享区没人动。

### Stream A 韧性（T2 数据治理 + T3 审批超时+watchdog）
- StorageConfig（audit 轮转 100MB/保留 5 份；run_events 90 天；memories 软过期 365 天；artifacts 上限；enable_prune 默认关）
- audit_log 写失败降级内存 ring；db quick_check 损坏恢复+备份+run 台账重建；WAL checkpoint
- RunApprovalBroker 审批超时（默认 600s）→ deny + 审计；default_runner 也包 wait_for
- browser 自动 relaunch；snapshot 按 run 保留 50 份
- 新 `health.py` Watchdog（60s 周期，动作表 PRUNE/RECYCLE/DENY_STALE/NOTIFY，全进审计）
- 测试：test_storage_governance(13) + test_approval_timeout(6) + test_health_watchdog(7) + browser(+3)

### Stream B 上下文（C1 prompt cache + C2 压缩 re-inject）
- 新 `llm/cache.py`：split_system_blocks + apply_cache_to_messages（3 断点 + static/dynamic 边界）
- assembler 系统提示拆静态/动态两段；factory prompt_cache off/basic/full；openai_compatible 打 cache_control
- compressor：extract_file_operations + 文件清单 + 压缩黑名单 + 受保护消息 re-inject；summarizer 附清单；memory 路径加权召回
- 测试：test_prompt_cache(15) + test_context_compression(+9)

### Stream C 信任（A1 作用域审批 + A2 拒绝回灌）
- policy/approval.py：SessionGrantStore + ApprovalScope + scoped_decision（per-invocation/per-resource/per-tool）
- base.py：Tool.permission_hint + ApprovalResponse.remember + ToolResult.denied/skipped（is_error=False）
- executor：resource_key 计算 + 规则命中 + deny/skip 结构化回灌（human deny=info，infra deny=error）
- cli.py：r 记住资源选项 + deny reason；approval_flow.py：结构化 decision 透传
- 测试：test_approval_scoping(20) + test_approval_deny_reinject(10)

### 协调者合并
- 修 2 个过时测试（deny 语义 is_error True→False；e2e done 断言在 packet.type 而非 event.type）
- react.py ToolContext 装配 grant_store（per-session 单例）
- 全量 **1277 passed, 1 skipped**（基线 1190 → +87）；六条安全不变量测试 86 全过
- 待接线：builtins.py permission_hint（executor _default_* 已等价）；AuditLog scope 字段

## 并行第二批（2026-08-08）——T4 / A3 / G1 三 agent 同时开发

**Stream T4 多实例协调 + schema 版本化**（db.py + cli.py + server.py）
- SCHEMA_VERSION=2 + _MIGRATIONS forward 迁移表（PRAGMA user_version 驱动，每步一个事务防半应用）
- _backup_before_migrate 原子备份到 ~/.modus/backup/db-v{n}.bak；版本>当前显式拒绝降级
- writer 租约（fcntl.flock/msvcrt，非阻塞，进程死自动释放，fail-soft）；只读查询不抢租约
- _ensure_column 迁入迁移表；CLI/Desktop 启动收敛（租约→迁移→才写，第二实例明确报错）
- 测试：test_db_instance_coordination(11) + 309 受影响全绿

**Stream A3 coverage 矩阵**（coverage.py + builtins + board_aggregation + server.py）
- CoverageStore：键控 (objective, operation, capability)→state；mark/untested/list/summary/clear
- JSONL 落盘 + write-coalescing + 5000 上限 LRU；coverage 工具声明 read_only+safe
- board_aggregation 加 coverage 段；kanban 显示"未覆盖"计数
- 测试：test_coverage_matrix(20) + 200+ 受影响全绿

**Stream G1 Goal 状态机**（goal.py + react.py + select.py + budget.py）
- GoalState 7 态（active/paused/budget_limited/max_turns/blocked/complete）+ 3-strike blocked
- GoalStore：per-session + JSONL 持久化 + tombstone 防复活；idle 续跑钩子（每轮开头注入 goal-steering）
- GoalReasoner（agent_mode=goal）；goal 工具（get/update/complete/blocked，safe+read_only）
- budget_limited 软停 + 交接总结轮（非硬断）；StopReason.GOAL_BUDGET_LIMITED/GOAL_MAX_TURNS
- 测试：test_goal(24) + 175 受影响全绿

**协调者接线（我）**
- CLI `/goal` 命令（set/get/status/pause/resume/continue/clear）——修了跨进程 bug：GoalStore 需显式 load() 才能从磁盘恢复，CLI 已加
- react.py 每轮 tool 后 mark coverage（mark_coverage_call 同步助手）
- 全量 **1332 passed, 1 skipped**（1277 → +55）

### 累计进度（五波 21 项，已落地 11 项）
W0-1 worker ✅ / T1 进程出身 ✅ / T2 数据治理 ✅ / T3 审批超时 ✅ / T4 多实例 ✅
C1 cache ✅ / C2 压缩 ✅ / A1 作用域审批 ✅ / A2 拒绝回灌 ✅ / A3 coverage ✅ / G1 Goal ✅

## 并行第三批（2026-08-08）——G2 / C3 / T5 三 agent 同时开发

**Stream G2 停滞检测 + 上下文剪枝**（stall.py + react.py + budget.py + plan_execute.py）
- error_signature 归一化 + trigram 相似度（Dice 系数，零 LLM，蓝图不变量 4）
- 四级熔断：ok/watch/stall/loop；stall/loop 注入 reference-only [STALL DETECTED] 块（非硬断）
- loop 连续 ≥2 → StopReason.STALLED 人工交接；停滞期间 token 单独计数（stall_tokens）
- plan 级停滞：同一步重复失败 → 整 plan STALLED；下一 task 注入 stall 上下文
- 测试：test_stall_detection(32) + 338 受影响全绿

**Stream C3 大结果句柄化 + 内容寻址缓存**（artifacts.py + builtins.py + executor.py）
- persist_oversized（>100KB 落盘 + 句柄 {path, sha256, size, preview}）+ 内容寻址缓存
- cache_key 只剥 secret 不剥 limit（**修了真实 bug**：limit=2 会误复用 limit=100000 的句柄）
- _MUTATING_TOOLS（23 工具）写后失效缓存；artifact_is_intact 完整性校验
- executor 兜底桥接：deny/skip 结构化回灌明确排除（metadata.operation 标记）
- grep/search_code 句柄化；tail 留给 T5 后协调者接
- 测试：test_result_bridge(21)

**Stream T5 进程状态机 + 退避重启**（process_tools.py + process_cleanup.py）
- STARTING→RUNNING（存活 ≥ startsecs）/ BACKOFF（too_quickly）/ failed（超限）；指数退避 0.25s 起翻倍
- gen 代次 + 手动终态守卫：kill/cleanup 后 supervisor 让位，绝不复活
- restart_process 工具（medium + requires_approval）；fatal/pid_reused 拒绝
- **T1 保护验证**：AST 对比确认 _read_born_at/_pid_identity_ok/_owned_by_this_process/_pid_alive 与 HEAD 完全一致
- 测试：test_process_state_machine(14) + 30 既有全绿

**协调者接线（我）**
- builtins 注册 restart_process 工具（import + Tool 声明）
- artifacts._MUTATING_TOOLS 加 restart_process
- 全量 **1399 passed, 1 skipped**（1332 → +67）；安全不变量 54 全过

### 累计进度（五波 21 项，已落地 14 项）
W0-1 worker ✅ / T1 进程出身 ✅ / T2 数据治理 ✅ / T3 审批超时 ✅ / T4 多实例 ✅ / T5 状态机 ✅
C1 cache ✅ / C2 压缩 ✅ / C3 句柄化 ✅ / A1 作用域审批 ✅ / A2 拒绝回灌 ✅ / A3 coverage ✅
G1 Goal ✅ / G2 停滞检测 ✅

### 剩余 7 项
- Wave4：G3 后台完成唤醒续跑
- Wave5：E1 轨迹重评分 / E2 后台审查 fork / E3 会话树
- 其他：builtins permission_hint 接线、AuditLog scope 字段、W0-2 office_exec worker 接线

## 并行第四批（2026-08-08）——E1 / G3+scope / E2+接线 三 agent 同时开发

**Stream E1 轨迹→离线重评分评估闭环**（db.py + cli.py + 新 evaluation 包）
- db.py SCHEMA_VERSION 2→3：run_events.tool_calls + runs.objective/final_result；三条事件路径写 tool_calls
- persist_trajectory 落盘 ~/.modus/trajectories/{run_id}.json；settle/interrupt 后自动 sink
- evaluation 包：Evaluator（join 场景×轨迹 + contextvar 注入 + scorer 注册表 + 自评防护）+ static_json（噪音解析/flatten/delta-1/区间匹配）+ report（token/cost/p50/p95）
- `modus evaluate` 命令（--run 单跑 / --suite 批跑）
- 测试：test_evaluation_trajectory(19) + 470+ 受影响全绿

**Stream G3 后台完成唤醒续跑 + AuditLog scope**（process_tools + server + audit_log + approval）
- spawn_process 加 resume_on_complete（默认 False）；reaper 完成后发 process_completed 事件（持久标记防重）
- server：process_completed 推 WS → 前端可点"继续"；resume_on_complete=True 且成功才自动续跑（预算减半 + 完成上下文）
- resume_process WS command；consume_process_resume 一次额度（原子领取）
- audit_log.record 加 scope/resource_key 字段；SessionGrantStore.audit_scope()
- 测试：test_process_resume(16) + test_audit_log(+9) + 265 受影响全绿

**Stream E2 后台审查 fork + 接线**（memory + skills + turn_finalizer + builtins + office_exec）
- memory 加 authority 字段（confirmed/curated/auto）；auto 记忆注入带"auto-extracted 未经验证"披露；检索按 authority 加权
- skills 加生命周期 active/stale/archived + usage 边车 + curator（超期降级，可恢复）；load_skill 调 mark_used
- turn_finalizer：N 轮 + 成功工具调用 → 后台审查 fork（复用 spawn_subtask）；provenance 门只写 curator 属地
- **permission_hint 接线完成**：bash/run_tests→command、web_fetch/browser_navigate→origin、write_file/edit_file/office_exec→path、git→remote
- office_exec worker 接线确认完整
- 测试：test_background_review(16) + test_skills(+lifecycle) + test_approval_scoping(+hints) + 196 受影响全绿

**协调者接线（我）**
- cli.py _record_cli_audit 透传 scope/resource_key 到 audit_log
- timeline.js 加 process_completed case + "继续"按钮（data-resume-process 事件委托 + resume_process WS 发送）
- 全量 **1475 passed, 1 skipped**（1399 → +76）；安全不变量 54 全过

### 累计进度（五波 21 项，已落地 18 项）
W0-1 ✅ T1 ✅ T2 ✅ T3 ✅ T4 ✅ T5 ✅ C1 ✅ C2 ✅ C3 ✅ A1 ✅ A2 ✅ A3 ✅ G1 ✅ G2 ✅ G3 ✅ E1 ✅ E2 ✅
+ 全部小接线（permission_hint / AuditLog scope / office worker / CLI goal / resume 前端）

### 剩余 3 项
- E3 会话树 + 原地分支 + steer/followUp（Wave5 最后一项，动 db.py messages 表 + 前端）

## E3 会话树 + steer/followUp（2026-08-08）——五波全部完成 ✅

**E3 后端**（db.py + react.py + server.py + compressor.py）
- db.py SCHEMA_VERSION 3→4：messages 加 parent_message_id + branch_root_id；session_branches 表（leaf 指针）；索引
- add_message 支持 parent_id（隐式续 leaf + 推进）；session_branch/session_revert/session_tree/current_session_leaf/get_session_messages（活跃分支 lineage）
- react.py steer/followUp 双队列：steer 在工具结果回灌后、下轮 LLM 前注入（与 goal/stall 同窗口）；followUp run 结束后 drain
- server.py：session_branch/revert/tree WS 命令（运行中拒绝）；run_message 支持 steer:true → 入 steer_queue
- compressor：active_branch_rows + branch_messages_to_context（分支重建，复用 turn-aligned tail）
- 测试：test_session_tree(17) + 391 受影响全绿

**协调者接线（我）**
- 修 T4 备份测试的 glob 顺序 bug（sorted()[0] 取最早备份，v4 备份排序变化导致）
- timeline.js run 完成卡片加"分叉继续"按钮（data-branch-continue → run_message steer+branch_from）
- 全量 **1492 passed, 1 skipped**（1475 → +17）；安全不变量全过

## 🎉 五波 21 项全部落地（本会话累计）
W0-1 ✅ T1✅ T2✅ T3✅ T4✅ T5✅ C1✅ C2✅ C3✅ A1✅ A2✅ A3✅
G1✅ G2✅ G3✅ E1✅ E2✅ E3✅ + 全部小接线（permission_hint/AuditLog scope/office worker/CLI goal/resume 前端/分支前端）

测试：**1492 passed, 1 skipped**（会话起始 1165 → +327）

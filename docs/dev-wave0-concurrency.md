# Wave 0 并发重活架构层——实施文档

> 目标：让 Modus 能"同时干很多重活"（Excel 分析 + 系统优化 + coding + Chrome preview 并行），且内存管理优秀到用户不抱怨。这是未来 Rust 下沉的地基。
> 来源：supervisor（进程状态机/事件监听/捕获标记/周期节拍）+ hermes（子进程 RPC/后台 fork/OS 隔离声明）+ loop（跨进程锁）+ beads（文件即事件总线）+ peri（idle 唤醒）+ AssetOpsBench（反模式教训）。
> 现状基线：`spawn_process` 已有（create_subprocess_shell+进程组+reaper）；`office_exec` 已是独立子进程（Popen([sys.executable,"-c",code])）；McpClient 支持 stdio/sse 两种 transport。
> 关键洞察（hermes SECURITY.md）：**唯一安全边界是 OS 级隔离，进程内一切启发式都不是边界**——重活必须各自跑独立 worker 进程，独立内存上限、崩了不拖垮别、干完即回收还给 OS。
> 工期：约 2-3 周（单人含测试）。本波是"地基"，不造具体功能，只搭调度层；Rust 下沉 = 未来把某个 worker 的实现从 Python 换 Rust，边界与协议不变。

---

## W0-0 架构定位（先读，决策依据）

```
┌────────────────────────── Modus Python 核心（主进程）──────────────────────────┐
│  agent 循环 / 审批 / 审计 / 记忆 / 工具注册表                                    │
│  ── MCP client（已支持 stdio/sse）── 调用边界（协议，不是 FFI）                 │
└──────────┬─────────────────────────────────────────────────────────────────┘
           │ 进程边界（版本化 JSON/MCP）
┌──────────▼──────────┐   ┌──────────▼──────────┐   ┌──────────▼──────────┐
│ worker: excel        │   │ worker: system_opt  │   │ worker: code_index  │
│ (Python 或未来 Rust) │   │ (Python 或未来 Rust) │   │ (Python 或未来 Rust) │
└──────────────────────┘   └──────────────────────┘   └──────────────────────┘
```

- **边界必须是协议**（版本化 JSON/MCP），不是 Python 对象传递、不是 FFI。FFI 传染 GIL/崩了拖垮主进程；协议让语言真正解耦。
- **每个 worker 独立可验证**：独立测试 + 崩溃隔离 + 内存上限 + 干完回收。
- **审计贯穿所有边界**：跨进程调用也进 AuditLog。
- **新增能力面强制走门**：能力声明 + 审批，不裸注册。
- **Rust 下沉 = 换 worker 实现，不动边界**——这就是"未来迁移便宜"的保证。

---

## W0-1 多进程 worker 隔离层——重活各自独立进程

### 问题
现在 `spawn_process` 是"一次性起个后台命令"，没有 worker 概念：没有并发上限、没有队列、没有内存上限、没有"干完回收给 OS"。Excel 分析、系统优化、coding 同时跑时，全都挤在主进程的进程组里，没有隔离。

### 设计（移植 supervisor 进程语义 + hermes OS 隔离声明）
新模块 `runtime/workers.py`：
- **Worker**：一个独立进程（`asyncio.create_subprocess_exec` 或复用 `spawn_process`），有 `worker_id` / `kind`（excel/system_opt/code_index/coding）/ `memory_limit` / `status`（idle/starting/running/done/failed）/ `started_at` / `ended_at`
- **WorkerPool**：并发上限（默认 = CPU 核数），队列（多任务排队），每个 worker 独立 cwd + 独立输出日志
- **有界内存**：worker 内存上限（Linux/macOS 用 `resource.setrlimit(RLIMIT_AS/RLIMIT_DATA)`，Windows 用 job object 或 `psutil.Process(pid).rss()` 周期采样 + 超限 kill）——**这是"内存管理优秀"的架构解**：不是靠 Python GC，而是让重活跑在可回收的独立进程里

### 实施步骤
1. **新模块 `runtime/workers.py`**：
   ```python
   @dataclass
   class Worker:
       worker_id: str
       kind: str
       proc: asyncio.subprocess.Process
       memory_limit: int  # bytes
       status: str
       started_at: float
       ended_at: float | None = None

   class WorkerPool:
       def __init__(self, max_concurrency: int, cwd: str): ...
       async def submit(self, kind: str, argv: list[str], memory_limit: int) -> str: ...
       async def get_status(self, worker_id: str) -> dict: ...
       async def cancel(self, worker_id: str) -> bool: ...
       async def reap_finished(self) -> list[str]: ...
   ```
2. **有界内存**（`_apply_memory_limit`）：
   - POSIX：`resource.setrlimit(resource.RLIMIT_AS, (limit, limit))` 在子进程 preexec 里
   - Windows：Job Object（`subprocess.CREATE_SUSPENDED` + AssignProcessToJobObject）或退化为 `psutil` 周期采样超限 kill
3. **并发队列**：`WorkerPool.submit` 超并发时排队（`asyncio.Queue`），不阻塞主进程
4. **回收**：`reap_finished` 把 done/failed worker 的进程 handle 关闭 + 内存还给 OS（进程退出即还，不依赖 Python GC）
5. **WS 暴露**：`desktop/server.py` 加 `worker_pool` command（list/submit/cancel/status）——前端可看"哪些重活在跑、各占多少内存"

### 参考细节（supervisor/hermes 实测）
- supervisor `process.py:656 transition` 状态机；`dispatchers.py:66 POutputDispatcher` 捕获输出
- hermes SECURITY.md："唯一安全边界是操作系统级隔离"——worker 独立进程即此
- AssetOpsBench `_call_tool` 每次起新 MCP 子进程（`:321`）是反模式：**要有连接池/复用**，不是每次新起

### 测试（新增 `tests/test_workers.py`）
- `test_worker_spawn_and_reap`：submit → running → done，reap 收尾
- `test_worker_memory_limit_posix`：超限进程被 kill（RLIMIT_AS）
- `test_worker_pool_queue`：超并发排队，不阻塞
- `test_worker_cancel`：cancel 后进程组终止
- `test_worker_reap_returns_memory`：done 后进程 handle 关闭

### 验收
- 手动：submit 3 个内存受限 worker → 观察独立进程、独立内存、超限被杀
- 六条不变量不回退（新增能力面走门）

---

## W0-2 office_exec 下沉为 worker——重活不占主进程

### 问题
`office_exec`（`tools/office_exec.py`）已是独立子进程（`Popen([sys.executable,"-c",code])`），但它是"一次性"的——每次调用起一个新进程，无 worker 池、无内存上限、无回收复用。

### 设计（复用 W0-1 WorkerPool）
把 office_exec 从"每次新进程"改为"走 WorkerPool 的 excel worker"：大 Excel 分析跑在可回收的 worker 里，多个 Excel 并发由 WorkerPool 限并发排队。

### 实施步骤
1. **`tools/office_exec.py` `_run_script`（:94）**：改为调 `WorkerPool.submit(kind="office", argv=[sys.executable,"-c",code])`，而不是裸 `Popen`
   - 保持现有 sandbox 语义（AST blocklist + 进程组 kill + timeout 全保留）
   - 内存上限：`memory_limit` 从 config 读（默认 1GB）
2. **新增 `excel_worker` 模式**：`office_exec` 支持 `worker: true` 参数 → 走 worker 池；默认仍同步（向后兼容）
3. **config**：`runtime.worker.max_concurrency` / `runtime.worker.office_memory_limit`（env `MODUS_WORKER_*`）
4. **WS/kanban**：大 Excel 分析在跑时，kanban 显示"worker: excel 分析中（内存 420MB）"

### 测试
- `test_office_exec_via_worker`：worker:true 走 WorkerPool，结果一致
- `test_office_exec_worker_timeout`：worker 超时 → kill + 报错（不泄漏进程）
- `test_office_exec_worker_memory_limit`：超限被杀
- `test_office_exec_worker_concurrent`：3 个 Excel 并发，排队不阻塞主进程

### 验收
- 手动：同时开 3 个大 Excel 分析 → 各自 worker、独立内存、不拖垮主进程
- 内存对比：分析完 worker 退出 → RSS 回落（回收给 OS）

---

## W0-3 worker 事件订阅 + 周期节拍——"干完能感知"（supervisor 语义）

### 问题
worker 完成/失败后，主进程/用户无法感知（除非轮询）。supervisor 有事件监听器协议 + Tick 周期节拍，Modus 没有。

### 设计（移植 supervisor EventListenerPool + Tick）
- **worker 事件流**：`worker_started / worker_output / worker_completed / worker_failed / worker_memory_warn`——进 `desktop/db.py` run_events + WS 推送
- **周期节拍**：Tick 事件（5s/60s/3600s）——后台周期采样 worker 内存/磁盘，超阈值发 `memory_warn`

### 实施步骤
1. **`runtime/workers.py` 加 `events: asyncio.Queue[dict]`**：worker 生命周期事件入队
2. **`desktop/db.py` 复用 run_events 表**：worker 事件写 `modus.agent-event.v2`（type 区分 `worker_*`）
3. **`desktop/server.py` WS 推送**：worker 事件 → 前端 timeline（复用现有事件推送管线）
4. **Tick 节拍**：`runtime/workers.py` 加后台任务，5s 采样内存/磁盘，超阈值发 `memory_warn`（复用 system_probe 探针函数，不重复造轮子）

### 参考细节
- supervisor `process.py:866 EventListenerPool`（dispatch :908 / _eventEnvelope :1003）；`supervisord.py:274 tick` + TICK_EVENTS
- beads-web `watch.rs` + SSE 文件监视（事件总线）
- 前端 timeline.js 已有事件流渲染，只需加 worker 事件类型

### 测试
- `test_worker_events_emitted`：worker 生命周期事件入队
- `test_tick_memory_warn`：内存超阈值发 warn
- `test_worker_event_pushed_to_ws`：事件到前端

### 验收
- 手动：worker 完成 → timeline 出现"Excel 分析完成"
- 内存超阈值 → 推送警告

---

## W0-4 未来 Rust 下沉的边界契约——"现在能跑，未来能换"

### 设计（不做实现，只定契约）
Rust 下沉 = 把某个 worker 的实现从 Python 换成 Rust，**边界与协议不变**。所以现在就要定死"worker 的对外契约"，未来换实现零改动。

### 契约（写入 `docs/dev-wave0-contract.md`）
1. **worker 对外接口 = MCP 协议**：每个 worker 是一个 MCP server（stdio transport），Modus 主进程用已有 `McpClient`（stdio/sse）调它。AssetOpsBench 的 6 个域 MCP 服务器就是现成范式。
2. **worker 状态机语义固定**：`idle→starting→running→done/failed`（supervisor 语义），换实现不换语义。
3. **worker 输入/输出 = 版本化 JSON**：固定 schema，跨语言可序列化。
4. **worker 安全声明**：每个 worker 声明 capability + 读写性 + 是否需要审批（进 AuditLog，跨进程调用也审）。能力面强制走门。
5. **worker 内存契约**：worker 必须支持内存上限 + 干完回收（OS 级），不依赖宿主语言 GC。

### 验证
- 现有 McpClient 已支持 stdio/sse → Rust worker 只要实现同一 MCP 协议，主进程零改动接入
- 未来把 code_index worker 换成 Rust：`WorkerPool.submit(kind="code_index", argv=["code_index_rs", "--mcp"])`——只换 argv，不动 pool/调用方

---

## 波次验收清单

- [ ] W0-1：多进程 worker 隔离 + 内存上限 + 并发队列 + 回收
- [ ] W0-2：office_exec 下沉 worker，多个 Excel 并发不拖垮主进程
- [ ] W0-3：worker 事件订阅 + Tick 节拍，干完可感知
- [ ] W0-4：边界契约文档（MCP 协议 + 状态机 + JSON schema + 安全声明 + 内存契约）
- [ ] 全量 `pytest tests/ -q` 绿，六条安全不变量不回退

---

## 波次关系

```
Wave0 并发重活架构层（2-3 周）
  │  ← 先做：这是 Rust 下沉地基，也是"内存不抱怨"的架构解
  ▼
Wave1 韧性地基 → Wave3 A1/A2 → Wave2 C1 → ...
```

Wave 0 与 Wave 1 正交：Wave 0 搭"并发调度层"，Wave 1 的 T1（进程出身校验）正好作用于 worker 的 pid 管理；T5（进程状态机）的 supervisor 语义与 W0-1 复用同一套状态机。**建议 Wave 0 的 W0-1 + Wave 1 的 T1 一起做**——都动进程层，避免两次改同一文件。

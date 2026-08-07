# Wave1 韧性地基——实施文档

> 目标：把 Modus 从"能力很全但结构脆弱"加固到"长期常驻可信"。
> 来源：盲区扫描（架构/可靠性视角）+ supervisor（进程状态机）。五个独立模块，无顺序依赖，可各自单独落地+验证。
> 现状基线：进程注册表+reaper 已有、db.py 用 `_ensure_column` 演进、`interrupt_nonterminal_runs` 已存在、RunApprovalBroker 无超时。
> 工期：全部合计约 3-4 周（单人含测试）。每条都标了精确落地文件/函数。

---

## T1 进程出身身份——堵 PID 复用误杀（优先，真实 bug 隐患）

### 问题
`kill_process` 用裸 pid + killpg(SIGKILL)，无出生时间校验。Modus 崩溃后 registry 里的 pid 被 OS 回收给另一个无辜进程，用户一调 kill_process 就 SIGKILL 掉别人。`_pid_alive`（process_tools.py:70）只做 `os.kill(pid, 0)` 探活，不校验身份。

### 设计
spawn 时记录 `born_at`（进程启动时间），kill/tail 前校验 pid 的启动时间与 meta 一致，不一致即判 PID 复用并拒绝操作。

### 实施步骤
1. **`tools/process_tools.py` `spawn_process`（:134）**：spawn 后立即读取子进程 born_at——
   - Linux/macOS：`ps -o lstart= -p <pid>`（解析为 epoch）或 `/proc/<pid>/stat` field 22（starttime，ticks）
   - Windows：`psutil` 的 `Process(pid).create_time()`（psutil 已是可选依赖则用，否则 `wmic process get CreationDate`）
   - 写入 `_write_meta` 的 `born_at` 字段
2. **`_pid_alive` 升级为 `_pid_identity_ok(meta)`**：pid 存活 + born_at 匹配才返回 True。不匹配 → 状态标 `pid_reused`（新状态，区别于 orphaned）
3. **`kill_process`/`tail_process` 前置校验**：identity 不匹配 → 拒绝（"pid 已被回收，拒绝操作"），绝不 SIGKILL
4. **`_process_status`（:83）**：增加 `pid_reused` 分支——registry 说 running、pid 活着、但 born_at 不匹配
5. **新增 `modus adopt <id>` 接管路径**：重启时发现 orphaned 且 alive 的条目，可把 ownership 从旧 pid 转移到当前实例（更新 `spawned_by` + `born_at`）

### 参考细节（AssetOpsBench/supervisor 对照）
- supervisor 用 `process.py:656 transition()` 维护状态机，退避重启是完整闭环——Modus 只需先堵误杀（本 T），状态机放 T5。
- AssetOpsBench 的 `execute_step` 零重试是反例，Modus 的瞬时重试别退步。

### 测试（新增 `tests/test_process_tools.py` 追加）
- `test_kill_rejects_pid_reuse`：mock 一个被回收的 pid + 真实 start time 不匹配 → 断言 kill_process 拒绝而非 SIGKILL
- `test_spawn_records_born_at`：spawn 后 meta 有 born_at，且与 ps 读取一致
- `test_adopt_transfers_ownership`：orphaned 条目 adopt 后 owned_by_this_process 为真
- `test_pid_reused_status`：registry running + pid alive + born_at 不匹配 → status=pid_reused

### 验收
- 全量 `pytest tests/ -q` 绿
- 手动：spawn 一个进程 → 杀掉 Modus → 伪造 meta 的 pid 指向一个无关进程 → `kill_process` 被拒
- 六条安全不变量不回退（尤其"审批后执行"）

---

## T2 数据平面治理——审计/事件/快照的保留、配额与损坏防护

### 问题
audit.jsonl 无限 append-only 无轮转；run_events/memories/artifacts 无任何保留策略；WAL 只开不 checkpoint；snapshot 侧 git 库按 turn 累积永不 gc。磁盘占满 → SQLite 写失败 → run 全挂。

### 设计
config 加 `StorageConfig`；`db.prune_expired()` / `audit_log.rotate()` 两个幂等函数；审计写失败降级。

### 实施步骤
1. **`config.py` 加 `StorageConfig`**：
   - `audit_rotate_bytes`（默认 100MB，超过切分并只保留 N 份=5）
   - `run_events_retain_days`（默认 90）
   - `memories_soft_expire_days`（默认 365，软过期不删）
   - `artifacts_max_bytes`（默认 2GB）/ `artifacts_max_count`（默认 10000）
   - env：`MODUS_STORAGE_*`
2. **`desktop/db.py` 加 `prune_expired()`**：幂等清理——按 run_events 时间戳删过期行、artifacts 超限删最旧、memories 软过期标 archived；**默认关闭删除，只报告候选**（保守，可逆），config 里开 `enable_prune` 才真删
3. **`policy/audit_log.py` `record` 加轮转**：写前 stat 文件大小，超阈值重命名 `audit-1.jsonl`→`audit-N.jsonl`（留 N 份），新开 `audit.jsonl`；**写失败降级**为内存最近 N 条（不再 raise），并记录降级标记
4. **`_get_conn`（db.py:20）加 PRAGMA**：`WAL checkpoint(TRUNCATE)` 周期执行 + `quick_check` 启动校验；损坏时备份坏库到 `~/.modus/backup/` 后重开新库，从 run_events 重放重建 run 台账（复用 `interrupt_nonterminal_runs` 语义）
5. **`tools/snapshot.py`**：按 run 只保留最近 N 份快照，超限删除对应 side-repo 分支

### 参考细节
- 保守可逆：修剪默认关闭、只删 Modus 自己的数据、绝不碰用户文件——这是不变量。
- 审计降级不能静默：降级事件本身要写日志 + 健康回路可读。

### 测试
- `test_prune_expired_reports_only`：默认不删，返回候选
- `test_audit_rotate_splits_file`：超阈值切分
- `test_audit_write_fails_degrades`：磁盘满时 record 不 raise，内存保留
- `test_quick_check_corrupt_recovery`：坏库 → 备份 → 重开 → 从 run_events 重建

### 验收
- 启动后 `quick_check` 通过
- 手动填满 audit → 触发轮转 → 历史保留 N 份
- 长期驻留内存不涨（WAL 有 checkpoint）

---

## T3 审批超时 + 健康回路（watchdog）——无人值守不挂死

### 问题
`RunApprovalBroker`（desktop/approvals.py:12）的 `_pending` 是 `asyncio.Future[str]`，**无超时**——无人值守时 run 可永久卡在 ASK 闸门上。browser.py 共享 page 无崩溃恢复。system_probe 能读磁盘/CPU 却无任何动作执行器。

### 设计
给审批 future 加超时（默认 10min，run budget 内可配），超时自动 deny 并继续 run；browser page 崩溃自动 relaunch；health watchdog 周期采样 + 显式动作表。

### 实施步骤
1. **`desktop/approvals.py` `RunApprovalBroker`**：
   - `register` 时 `asyncio.get_running_loop().call_later(timeout, _timeout_deny, key)`，默认 600s
   - `_timeout_deny`：future 未 done 则 `set_result("deny")`，写审计 `approval_timeout`
   - config：`runtime.approval_timeout_seconds`（env `MODUS_APPROVAL_TIMEOUT`）
2. **`default_runner.py` 的 approval_callback**：包一层 `asyncio.wait_for`（防护 run budget 外路径）
3. **`tools/browser.py`**：`_ensure_browser` 每次调用前检查 `page.is_closed()` / 渲染进程崩溃 → 自动 `_close_browser` + 重开（重置 `_holder`）
4. **新模块 `modus/health.py`**（watchdog）：
   - 常驻任务（60s 周期，与 `install_process_cleanup` 平行，在 `desktop/server.py` 启动时 spawn）
   - 复用 system_probe 探针：磁盘余量（`~/.modus`）、audit 大小、SQLite 大小、approvals 待决时长、browser page 状态
   - **显式动作枚举**：`PRUNE_CACHE`（WAL checkpoint + 过期 artifacts）、`RECYCLE_BROWSER`、`DENY_STALE_APPROVALS`、`NOTIFY_USER`（WS 横幅）——每个动作进 AuditLog
   - fail-safe：探针失败 = 不动作；只清理 Modus 自己的数据目录

### 参考细节
- 盲区扫描原话："所有可靠性机制都挂在 run/会话生命周期内，看不见进程级与跨会话级失效。系统健康与 run 健康是两个时间尺度。"
- 审批超时→deny 而非 abort：保持 run 继续，agent 收到 deny 可改道（呼应 T4 deny 回灌）。

### 测试
- `test_approval_timeout_denies`：register 后等超时，future 为 deny
- `test_browser_relaunch_on_closed`：page.is_closed() 为真 → 自动重开
- `test_watchdog_prune_actions_logged`：动作枚举进审计

### 验收
- 起 server 挂着 → 审批请求不响应 → 10min 后自动 deny，run 继续
- 关闭浏览器 page → 下一次工具调用自动重开

---

## T4 多实例协调 + schema 版本化——同一个 desktop.db 的并发写者

### 问题
CLI、Desktop、MCP server、spawn 子进程**共享同一个 desktop.db，全仓库无 flock/lockfile、无 PRAGMA user_version**。两个 writer 裸写同一个 SQLite = lost update。schema 演进靠 `_ensure_column`（db.py:349）逐列 ALTER，无迁移框架、无迁移前备份。

### 设计
`PRAGMA user_version` + 迁移表 + writer 租约（flock/msvcrt）。

### 实施步骤
1. **`db.py` 加 `SCHEMA_VERSION` 常量 + `migrate_schema(conn)`**：
   - `init_db` 开头：读 `PRAGMA user_version`，逐个 forward migration（`MIGRATIONS: list[tuple[int, str]]`，每步一个 SQL + 版本号）
   - 迁移前原子备份：copy db 到 `~/.modus/backup/db-v{n}.bak` 再执行
   - 版本 > 当前：显式报错（"数据库来自更新版本，拒绝打开"），不静默降级
2. **writer 租约**：
   - 顶层 `acquire_writer_lease()`：在数据目录创建 `instance.lock`（`fcntl.flock` / Windows `msvcrt.locking` 或 `flock` 兼容层）
   - 第二个写者进程 acquire 失败 → 明确错误"另一实例在运行"（提供 `--read-only` 只读查询模式给 code_index/recall）
   - 只读查询（code_index、recall）可并发，不抢租约
3. **收敛启动例程**：CLI/Desktop/MCP 三个入口共用 `init_db`（租约 → 迁移 → 才允许写）
4. **`_ensure_column` 迁入迁移表**：不再散落 ALTER，统一进 `MIGRATIONS`

### 测试
- `test_migrate_from_old_schema`：旧版 db → 新版迁移成功且保留用户历史
- `test_downgrade_refused`：新版 db → 旧代码打开被显式拒绝
- `test_second_writer_refused`：两个进程并发 open → 租约阻止第二个写者，数据无损
- `test_reader_concurrent_ok`：写者持锁时只读查询可用

### 验收
- 同时开 CLI + Desktop → 第二个明确报错而非默默竞争
- 升级流程：旧数据无损迁移，downgrade 被拒

---

## T5 进程状态机 + 退避重启（supervisor 语义移植，P1 后续）

> 本项排在 T1 之后，是"后台进程从 spawn/tail/kill 到一等公民"的完整化。T1 是堵误杀，T5 是给生命周期。

### 参考细节（supervisor）
- `process.py:656 transition()` 状态机：`STOPPED→STARTING→RUNNING→EXITED/STOPPED` + `BACKOFF`（启动失败退避重试）+ `FATAL`（超限放弃）+ `UNKNOWN`
- `process.py:572 too_quickly` 判定：进程存活 < startsecs（默认 1s）即退出 → 判启动失败，不进 RUNNING
- `process.py:397 give_up`：startretries 用尽 → FATAL
- `supervisord.py:98 diff_to_active` 热重载三向 diff（added/changed/removed）——Modus 不需要进程组热重载，跳过

### 实施步骤（语义移植，非代码拷贝）
1. **`tools/process_tools.py` 加 `status` 状态机字段**：`starting/running/backoff/fatal/exited/stopped`，替换现有松散 status
2. **`spawn_process` 加 `startsecs`（默认 1）**：spawn 后 1s 内若子进程退出 → 标 `backoff`，按 `startretries`（默认 3）指数退避重试
3. **`reaper`（后台）**：进程退出码分类 → `running→exited`（exit 0）或 `backoff→fatal`（超限）
4. **新增 `restart_process` 工具**：对 `fatal` 之外的状态可显式重启（走审批：medium + requires_approval）

### 测试
- `test_spawn_too_quickly_backoff`：秒退进程标 backoff
- `test_restart_retries_with_backoff`：指数退避
- `test_restart_process_approval`：restart 走审批门

---

## 波次验收清单（全部完成才算 Wave1 关闭）

- [ ] T1：PID 复用误杀堵死（测试断言拒绝而非 SIGKILL）
- [ ] T2：audit 轮转/磁盘降级/quick_check 恢复全绿
- [ ] T3：审批超时 deny + browser 自动 relaunch + watchdog 动作进审计
- [ ] T4：多实例租约 + schema 迁移 + downgrade 拒绝
- [ ] T5：进程状态机 + 退避重启（可选，若时间紧可延后）
- [ ] 全量 `pytest tests/ -q` 绿，六条安全不变量不回退

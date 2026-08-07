# Modus 职能映射（从真实任务反推能力矩阵）

> 来源：workflow `map-agent-functions`（4 视角：软件工程 / 系统运维 / 研究数据 / 自主运行 + 首席综合）。用户洞察："我应该向 Agent 描述它要做什么，Modus 应从 Agent 要执行的任务反推能力/权限/安全需要。"

## 能力矩阵（22 类任务）

### 现在能做（doable=True，8 类）

| 任务 | 优先级 | 现状限制 |
|---|---|---|
| 跨仓库功能实现（多文件改+测试+提交） | high | 缺：浏览器/UI E2E 验证、全项目类型检查/静态门、覆盖率门+flaky 重跑、git log/blame、背景进程监督 |
| 保行为重构（符号重命名/提取模块） | high | grep+精确文本 edit_file 可改名，但非符号感知：动态/隐式引用漏掉 |
| 调试失败测试（复现-追根-修复-验证） | high | 缺：定向失败重跑（pytest -lf）、flaky 检测、traceback-to-source 映射、覆盖率、非交互调试器、日志捕获 |
| 分析大代码库（10k+ 文件） | high | 缺：扫描上限截断披露、持久索引、按文件计数、扩展名过滤、AST 调用图、map-reduce fan-out |
| 查找符号跨库使用 | high | search_code 只读自动放行，但字面模式 casefold、无 def-vs-use 区分 |
| 产出研究报告（md/docx/pdf）带引证 | medium | 缺：证据-声明引证追踪、原生 docx/pdf 生成、交付物 secret 脱敏 |
| 跑到任务完成（watch build/test/deploy 到绿） | high | 缺：可续/可再生预算、持久 run 状态、NO_PROGRESS 细化（等待 vs 自旋） |
| 失败重试（flaky 部署/CI 直到成功） | medium | 缺：持久重试/重排队、有界指数退避+jitter、熔断器、持久尝试账本 |

### 部分能做（doable=partial，4 类）

| 任务 | 优先级 | 缺什么 |
|---|---|---|
| 设置 CI/CD（写 workflow+验证+push+监控） | medium | actionlint/YAML 验证门、本地 CI runner、gh workflow 工具、CI secret 管理 |
| 大型代码库调试（git 考古、bisect） | medium | git log/blame/show/bisect、'哪个提交破坏的'工作流、per-line 历史注释 |
| 摄入数据集（CSV/TSV/JSON/parquet/xlsx）并查询 | medium | 数据集摄入工具、data_profile、chunked reader、PII 脱敏 |
| 文件整理（文档目录清单、报告复用） | medium | 格式感知 reader、chunked 全文件 reader（解 1MB 拒绝）、自动持久化提取结果 |

### 现在不能做（doable=False，10 类）

| 任务 | 优先级 | 核心缺口 |
|---|---|---|
| **PR 评审提交** | high | 无 gh/GitHub API 工具；无 CI 状态；无 prompt-injection 加固 |
| **数据库迁移** | medium | 无 schema 内省/迁移 runner；无 dry-run/事务/回滚门；DDL 词汇不在 CommandGuard |
| **修复安全漏洞/审计依赖** | medium | 无 pip-audit/osv 集成；无结构化 CVE 查找；无锁文件 pinning |
| **跑应用并 E2E 验证** | high | **最大缺口**：无浏览器自动化；无后台进程监督；无端口管理 |
| **配置服务器/守护进程** | high | 无服务/守护生命周期工具；无网络/端口检查；无 env 工具；无 log 工具 |
| **总结文档语料（PDF/docx/xlsx）** | medium | read_file 仅 UTF-8 文本且拒绝 >1MB；无格式感知 reader |
| **比较两个版本（git refs/文件）** | medium | 无 git diff/log/show/blame 工具；无结构化 diffstat |
| **调度工作** | high | 无调度器：无 recurrence/next-run/timezone/持久 schedule store |
| **作为后台 agent** | high | spawn_subtask 同步阻塞（await 子 loop）；无 fire-and-forget；无 daemon host |
| **错误自愈** | high | 终端失败无重入/升级路径；单向策略切换；无持久 last-good checkpoint |

## 推荐构建（20 项，按解锁类别数排序）

1. **后台进程工具集 + daemon runner**——spawn/poll/kill/tail_process 返回 pid handle + 流式 delta，+ detached-runner host（孤儿/端口清理）。解锁 8 类。
2. **只读 git 历史工具**——git_log/show/blame + diffstat/rename 检测 + bisect，只读、输出有界/脱敏。解锁 6 类。
3. **持久代码搜索索引 + 符号感知导航**——search_code 背后 word-boundary 精确符号模式、find-references、def-vs-use。解锁 5 类。
4. **持久 run 状态 + checkpoint-resume**——RunBudget/RunVerification/ExecutionPlan/attempt-ledger 落 SQLite，重启恢复。
5. **验证静态分析 + 测试深度门**——全项目 pyright/mypy/ruff/actionlint 真门、pytest -lf、flaky 重跑、覆盖率门。
6. **格式感知 reader + 有界 chunked 全文件 reader**——pdf/docx/xlsx/ipynb/csv/parquet + doc-inventory + 解 1MB 拒绝 + 脱敏。
7. **扫描上限截断披露**——grep/search_code/glob 显式 'scan hit cap, incomplete' 信号，替代静默截断。**横切正确性修复**。
8. **浏览器/UI E2E 自动化，localhost 限定**——navigate/click/screenshot/DOM/console/network，沙箱远离 host secret。
9. **进程生命周期 + 端口检查工具**——ps/pid-inspect/kill-with-allowlist/start-stop-restart + lsof/netstat 解析；restart 独立审批门。
10. **日志工具带 PathGuard 读豁免**——tail/follow 有界窗口 + 脱敏，针对 /var/log、~/Library/Logs、journald。
11. **调度器 + 持久 schedule store + 无人审批策略**——recurrence/next-run/timezone + allowlist auto-approve + queue-and-notify。
12. **墙钟轮询 / wait-for-condition 原语 + NO_PROGRESS 细化**——不阻塞 bash turn 等待外部状态；区分等待 vs 自旋。
13. **Map-reduce fan-out**——有界并行分片 worker（async，非深度受限同步 spawn_subtask）+ 合并。
14. **长程检索层**——artifacts + 项目研究笔记 + 跨 run resume 的 embeddings/vector store。
15. **远程协作 / GitHub API 工具**——gh 风格 PR fetch/diff/评审/approve/merge + CI 状态 + repo-scoped token + 脱敏 + prompt-injection 加固。
16. **符号感知重命名 / move-file + 重写 import / AST 级编辑**——LSP/索引背书原子重命名。
17. **数据库工具**——schema 内省 + 迁移 runner + dry-run/事务/回滚 + DDL 词汇入 CommandGuard + 读/写连接分离 + DSN 脱敏。
18. **供应链审计**——pip-audit/npm-audit/osv + 结构化 CVE 查找 + 锁文件 pinning + SBOM + 新依赖审批门。
19. **证据-声明引证绑定**——报告句子 → read_file:line 追踪 + 交付物 secret 脱敏 + 引证图。
20. **Env 工具 + 服务 env 建模 + sudo/root HITL 升级**——每会话白名单 env + launchd/systemd env 建模 + 结构化 HITL root 通道。

## 关键洞察

- **最大单缺口 = 后台进程工具集**（spawn/poll/kill/tail + daemon runner）。今天 bash 阻塞到 EOF，进程无法监控/重启——这卡住 8 类任务。
- **第二 = 只读 git 历史工具**（log/show/blame/bisect）。开发者日常的一半依赖它。
- **扫描上限截断披露是横切正确性修复**——10k+ 文件研究里，静默截断会读出假"无匹配"。
- **所有新增系统能力必须遵守蓝图 Phase 3 边界**（出口白名单 + 凭据代理 + 沙箱）——浏览器/进程/日志工具不例外。

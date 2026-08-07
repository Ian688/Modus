# Modus 学习对象综合分析（12 项目 + 盲区扫描）

> 日期：2026-08-08 · 输入：12 个学习项目深度拆解（代码级）+ Modus 现状基线（28.7K 行源码）
> 结论先行：**Modus 的"能力密度"已远超其"结构韧性"与"目标自主性"。它不缺工具，缺三样东西——让 run 可重放的韧性、让 agent 自己逼近目标的自主性、让安全机器跨进程携带的协议层。**

---

## 一、Modus 相对 12 项目的位置

### 1.1 Modus 已超车的领域（别重复建设）

对比综合（10 维度 68 个 alreadyStrong 点）反复确认，以下 Modus 已经领先，**任何吸收方案都不应重复**：

| 领域 | Modus 的领先点 | 哪些项目没做到 |
|---|---|---|
| **策略分派** | react/plan_execute/select 三策略确定性选择器——12 项目全无等价物，全是单一循环 | CCB/peri/pi/loop |
| **安全护栏** | Capability deny-first 门 + ApprovalPolicy + CommandGuard + PathGuard + 六条不变量，全进 agent 循环 | Peri（YOLO 默认开）、Pi（无权限系统）、GenericAgent（信任 LLM 自律） |
| **后台任务生命周期** | 进程注册表落盘 + reaper + atexit/SIGTERM 清理 + 子任务递归 | supervisor 只有状态机；cc-haha 的 bypassPermissions 定时任务是反例 |
| **桌面工作台** | WS server + kanban（board_aggregation 纯函数聚合）+ 同源 preview 代理 + annotate.js 批注回传 | beads 看板实测**拖拽未接线、只读**；cc-haha 是 fork 别人的 CLI |
| **上下文纪律** | 中途确定性压缩（保护头尾+尾部引用，无 LLM）+ approve-then-execute 不变量 | pi 的 LLM 摘要压缩反而更贵；GenericAgent 靠手写文本协议 |
| **记忆骨架** | desktop 四层记忆（working/episodic/semantic/procedural）+ 查询式检索 + self_report 蒸馏 | GenericAgent 手工 L1 索引更糙 |
| **MCP 双向** | client + server（stdio + Streamable HTTP）+ 默认只读 lens | 12 项目里多数只有单向 |

**结论：Modus 不是"落后生"。它在一个极安全、极高解耦的地基上，缺的是三个层次的增量。**

### 1.2 三类真实缺口（跨维度精选，非机械罗列）

我把 57 个 agent 列出的缺口去重、交叉、按"是否改变 Modus 定位"筛选出 **12 个真正值得做的**，分三层：

#### 第一层：战略级（改变 Modus 定位）

**缺口 1｜目标驱动续跑（Goal）——从"防跑飞"到"自己逼近目标"**
- 来源：CCB V5（goalState.ts 7 态机 + useGoalContinuation idle 钩子 + GoalTool + 3-strike blocked）；peri（GoalController + 3 级递增紧迫感）；GenericAgent（reflect 外部唤醒 + goal_mode）
- 现状：Modus 的 run budget 是**防御性**的（防跑飞、周期持久化、interrupt 恢复），但没有"目标驱动继续"的进攻性机制。一次 `modus -p "把测试跑绿"` 用完 budget 就停，不会自己续。
- 吸收：移植 CCB 的 7 态机（active/paused/blocked/budget_limited/max_turns/complete）+ **3-strike blocked**（同因连续 3 次才判死，杜绝"难、慢"误判）+ idle 续跑钩子（Modus 的 REPL/query 循环每轮 exit 时检查 goal）。**关键取舍：用户输入优先级永远高于自动续跑。**
- 落地：`src/modus/runtime/budget.py`（已有持久化设施，直接复用）+ `agent/strategies/`（加 goal-aware 策略）。成本 M。
- **这是 12 项目里 CCB 最独到的机制，也是 Modus 最该学的。**

**缺口 2｜事件溯源 Run 状态机——让 run 本身可重放**
- 来源：盲区扫描（架构视角，这是扫描中最硬的发现）
- 现状：Modus 的持久化哲学是"一切关于 run 的都落盘，但 **run 本身不落盘**"。db.py 有 13 张 run 事件表、budget 有 snapshot()、session_state 有 park_run，但 agent.py 的循环决策状态（history、controller、pending_approvals、subtask 栈）全是 RAM 里的对象。**杀掉进程，transcript 完好，循环却死了。** 桌面端只把 run 标记为 interrupted 然后让用户重开 = 把已发生的钱重付一次。
- 吸收：把 agent 循环重构成纯 reducer `run(state, LLM_response) -> (state', events)`，events 追加进已有 run_events 表；budget.snapshot() 升级为"循环状态快照"；启动时读 DB 找最近 interrupted run，**在新进程里重建循环从断点继续**。
- 落地：`desktop/db.py run_events` + `runtime/budget.py` + `agent/strategies/`。成本 XL（动三策略共享骨架，但这是"基座 vs 脚本"的分水岭）。风险：工具调用不可重放 → 先做"plan 已执行到第几步"的粗粒度续跑，再细化。
- **与用户记忆里的断电防护（modus-power-loss-guard）直接呼应——那个记忆记的是"孤儿进程+run 状态恢复"，本方向是它的正解。**

**缺口 3｜安全机器跨进程携带（信任序列化）——Modus 的护城河在协议层**
- 来源：盲区扫描（生态视角）；对照 PentesterFlow（cacheKey 作用域）、cc-haha（审批回灌）
- 现状：Modus 内部的安全资产世界级（capability deny-first 门 + ApprovalPolicy + T0-T5 + AuditLog），但**跨进程那一刻全部坍塌成两个二值**：MCP client 对每个远端工具无条件 `requires_approval=True`，MCP server 只透传 name/description/schema。能力身份被哈希命名抹掉（`mcp__{server_digest}__{tool_digest}`）。
- 吸收：把"策略本身"序列化上 wire——Tool 声明加 provenance/trust 向量（source_kind/origin_identity/policy_capsule），MCP server 的 tools/list 携带；client 侧 attestation（ed25519 + trust store）按 tier 自动放行而非全 ASK。生态成熟后这是**护城河而非功能**。
- 落地：`tools/base.py` + `mcp_client.py` + `mcp_server.py` + `policy/approval.py`。成本 L。
- **四个方向共享一个洞察：Modus 独有的安全机器应该成为协议层资产，而不是每次跨进程就丢。**

#### 第二层：高价值补强（落地成本低，立即见效）

**缺口 4｜prompt cache 工程（P0，直接移植）**
- 来源：peri（98.5% 命中率、3 断点、frozen system prompt）
- 做法：把 Modus 动态 env/记忆段用边界标记从静态系统提示词切出，静态区打 cache_control，3 断点（首个 user + 倒数 1/2 个 user）。Modus 现在每次 prompt 动态拼 env/记忆段，整段缓存易失效。
- 落地：`llm/factory.py` + `llm/openai_compatible.py` + `agent/context.py`。成本 M。

**缺口 5｜作用域化审批缓存（P0，改 ApprovalPolicy）**
- 来源：PentesterFlow（allow-session 绑定到具体载荷：shell=改写后命令、http=origin、file=路径）
- 现状：Modus ASK 命中后没有"本会话放行"粒度——要么每工具都问，要么全放。
- 做法：审批决策加 `scope` 字段（per-invocation/per-resource/per-tool/per-session），shell 命令默认 per-invocation（批准 `cat a` 不放行 `rm -rf`），读类工具可 per-session。复用现有 AuditLog 记录 scope。
- 落地：`policy/approval.py` + `tools/executor.py`。成本 S。**高 ROI、低破坏。**

**缺口 6｜拒绝回灌模型 + 规则记忆（P1）**
- 来源：cc-haha（deny 作为 tool_result 回灌，不 abort——abort 会让模型永远看不到拒绝；用户"本次允许"沉淀为持久规则）
- 现状：Modus executor 的 deny/skip 是 fail-closed 非错误（tool 不跑），但结果**不作为 tool_result 回灌模型**，agent 不知道自己被拒、会反复重试同一操作。
- 落地：`tools/executor.py` `_approval_decision` 的 skip/deny 分支，改为生成结构化 tool_result。成本 S。

**缺口 7｜FTS5 中文全文检索（P1，直接移植）**
- 来源：hermes（unicode61 + trigram + cjk-bigram 三套索引，规避中文单字切分）
- 现状：Modus 记忆/往事召回是纯关键词+recency 集合重合，中文按单字切分噪声大（已被迫加 ≥2 词守卫），召回质量弱。
- 落地：`desktop/db.py` 的 run_events/memories 表加 FTS5 虚拟表，`episodic_recall_text` 换全文检索。成本 M。

**缺口 8｜结构化压缩 + 文件操作清单 + re-inject（P1）**
- 来源：peri（三层 compact + re-inject 最近 Read 的文件）；pi（摘要自动附"读/改过哪些文件"清单、切点不切 toolResult）
- 现状：Modus compressor 只留"摘要+尾部"，压缩后丢失"本轮读过/改过哪些文件"的信息增量，模型无法恢复关键工作记忆。
- 落地：`agent/compressor.py` + `desktop/summarizer.py`（摘要后追加 readFiles/modifiedFiles 清单 + 零成本 micro 截断层 + 关键消息压缩黑名单）。成本 M。

**缺口 9｜轨迹→离线重评分评估闭环（P1）**
- 来源：AssetOpsBench（persist_trajectory + Evaluator 离线 join 重评分 + Static-JSON 确定性评分器）
- 现状：Modus 有 1165 测试（开发时），缺"**运行时**轨迹可回放评分"（使用时）——agent 跑完一长任务，没有客观的"干得好不好"。
- 做法：agent 运行落盘标准轨迹（轮次/tool 调用/输入输出/token），评估器离线 join 场景重评分；Static-JSON 评分器做结构化输出判定（替代字符串相等断言）。
- 落地：`desktop/db.py`（run_events 已近轨迹）+ 新 `evaluation/`。成本 M。**Modus 唯一缺的端到端质量闭环。**

**缺口 10｜进程出身身份 + PID 复用误杀（P1，真实 bug 隐患）**
- 来源：盲区扫描（架构视角）
- 现状：kill_process 用裸 pid + killpg(SIGKILL)，**无出生时间校验**。Modus 崩溃后 registry 里的 pid 被 OS 回收给另一个无辜进程，用户一调 kill_process 就 SIGKILL 掉别人。orphaned 被写成 schema 里的合法终态，但"检测上报"≠"接管恢复"。
- 做法：spawn 时记录 born_at，kill/tail 前校验 pid 启动时间一致，不一致即判 PID 复用拒绝；重启时发现 orphaned 且 alive 的条目提供 `modus adopt`。统一 macOS/Linux/Windows 三后端 ProcessOwner 抽象。
- 落地：`tools/process_tools.py` + `process_cleanup.py`。成本 M。

**缺口 11｜数据平面治理 + 驻留健康回路（P1）**
- 来源：盲区扫描（可靠性视角）
- 现状：audit.jsonl 无限 append、WAL 只开不 checkpoint、run_events/artifacts 无保留无配额；RunApprovalBroker 审批 future 无超时，无人值守时 run 可永久卡在 ASK；浏览器共享 page 无崩溃恢复；模型单 provider 单点。
- 做法：常驻 watchdog（复用 system_probe 探针 + 显式动作表 PRUNE/RECYCLE/DENY_STALE/NOTIFY）+ StorageConfig（审计轮转/保留天数/配额）+ 审批超时自动 deny + provider 故障转移。成本 L。
- **"无人值守"从口号变成能力的核心。**

#### 第三层：产品/工作流（看板升级为指挥台）

**缺口 12｜任务级 GitOps 闭环 + 记忆面板 + 状态优雅降级（P2，模式借鉴）**
- 来源：beads-web（worktree-per-task + PR 合并门 + 合后自动收尾 + 记忆面板任务反链 + 未知状态降级）
- 现状：Modus kanban 是任务状态机，没接到真实 git 工作流；记忆无 GUI；看板对接底层模型变化会白屏。
- 做法：任务详情加"开 PR/看 CI/合并/关任务"四动作（合并成功自动关任务）；记忆面板可浏览/编辑/归档 + 点击跳任务；未知状态 → 兜底列+徽章+警告。成本 M。
- **把看板从"展示层"变成"指挥台"。**

---

## 二、探索性研究方向（盲区扫描，用户主动邀请）

> 4 个视角（用户/架构/生态/可靠性）独立扫描，16 个方向。以下精选 **8 个我判断最有锋芒的**——它们不来自 12 项目报告，而是从"Modus 目标反推"。

### 战略级盲区

**盲区 A｜跨重启/断电的连续性层（persist-as-a-service）**
普通用户的节奏是"关盖走人、打开继续"，不是坐在终端前等 run。Modus 的 resume 只覆盖 WS 断开后的同一进程 park/resume，**断电/换机后无法重建现场**。做法：持久化 intent ledger（每次 run 完成/中止写结构化记录：意图/停止原因/完成度/工作区/验证状态）→ 启动时 recovery sweep 聚合成"上次的世界"卡片（3 个未完成任务+一键继续）。**这是"基座 vs REPL"的分水岭，也是缺口 2 的产品面。**

**盲区 B｜中途改道的用户干预路径（park-edit-resume）**
用户不会想清楚再下指令。真实用法是"等等，这个不用做了，先看 B"。Modus 有 run_message/cancel/checkpoint 但三者割裂，中间没有"暂停当前 run → 看 checkpoint → 改目标 → 原地继续"的动词。做法：复用 snapshot.py 的 revert_turn，桌面 WS 暴露 "interrupt → snapshot → user review → redirect → resume" 消息流。**"中途改道"比"完整执行"更频繁，架构师从 task 完成率看不到它。**

**盲区 C｜机器无关的身份与数据迁移层**
Modus 宣称"任意 OS/平台可用、个人 PC 基座"，但 code_index 用 sha1(resolved root) 做 key、workspace digest 用 resolved root、db 行存绝对路径——**换机器/换 OS 后索引全失效、身份全漂移，连一份 `modus export` 都没有**。这是基座最大的自我矛盾：数据是它最大的资产，却绑死在一台机器上。做法：稳定 workspace_id（UUID）+ `modus export/import` + 迁移契约文档。

**盲区 D｜事件溯源 + 自我诊断（run 级 watchdog）**
budget.turn_records 已记录 turn 级观测，但只在 run 运行时被消费，run 失败后没有任何"为什么失败"的沉淀。无人值守场景下没人盯终端，故障根因必须从审计数据自动回放生成诊断：给定 run_id，重放 run_events 生成因果链 + 可执行修复建议，新 run 自动检索同 workspace 失败签名预置 hint。**从"会自停"进化到"会自诊断"。**

### 产品级盲区

**盲区 E｜主动守护模式（idle-watch + battery-aware）**
用户对"全能基座"最深的隐性期待是"你会看护我"，但 Modus 是纯被动响应系统。system_probe 能读 cpu/disk/进程，却没有跨 run 的持续监控语义，也没有电池信息。做法：system_probe 加 battery + last-user-activity 字段 → idle-watch daemon 维护"用户离开期间发生了什么"的 delta 日志 → 回来时 timeline 顶部推"你离开的 40 分钟里"摘要。**全部只读、全部本地、可撤销——从"工具"到"伙伴"的最短安全路径。**

**盲区 F｜全局回滚与"出事了能反悔"**
决定普通人是否敢把电脑交给 Agent 的，不是工具多不多，而是"它搞砸了能不能回来"。Modus 的 snapshot.py 已有 revert_turn，但**没有任何 WS 消息或 UI 把它暴露给用户**；记忆层只有 active/archived 状态，无"记忆变更历史"，Agent 记错偏好用户不知道也不可撤销。做法：run-revert 消息流 + 记忆不可变 audit 行 + 记忆面板一键撤销。**性价比极高（纯接线，零新增底层）。**

### 架构级盲区

**盲区 G｜多实例协调 + schema 版本化**
CLI、Desktop、MCP server、spawn 子进程**共享同一个 desktop.db，但全仓库没有一个 flock/lockfile、没有 PRAGMA user_version**。用户同时开 CLI 和 Desktop = 两个 writer 裸写同一个 SQLite，lost update 是概率问题。做法：writer 租约（flock/msvcrt）+ user_version + 迁移框架（迁移前原子备份，downgrade 显式拒绝）。

**盲区 H｜本地/云分工的委托协议（agent_delegate）**
Modus 已具备全部零件（model_repository 路由、moa 多模型角色、process_tools、run ledger 可恢复），但没有一层策略说"这个任务留本地、那个派给云端 agent，且委托可审计、有界、可续跑"。生态成熟后个人 PC 基座的位置不是"单机全能"而是"个人 agent 网格的枢纽"。做法：spawn_subtask 是本地版形状——把子目录隔离换成远端端点。**注意：这块要 trust/路由/可恢复三块同时成熟才能启动，单点做都是半吊子，建议 P2。**

---

## 三、采用优先级路线（P0/P1/P2）

> 综合全部输入（12 项目 + 盲区），按 ROI 排序。**建议顺序：先补韧性（缺口 2/10/11）→ 再补自主性（缺口 1）→ 再补协议层（缺口 3）→ 其余随排期。**

### P0（立即，2-4 周，成本 S-M）
| 项 | 来源 | 成本 | 一句话 |
|---|---|---|---|
| 作用域化审批缓存 | PentesterFlow | S | 批准 `cat a` 不放行 `rm -rf` |
| 拒绝回灌模型 + 规则记忆 | cc-haha | S | agent 看到拒绝，用户"本次允许"沉淀为规则 |
| prompt cache 工程 | peri | M | 静态区打 cache_control，3 断点，命中率 70→98% |
| 进程出身 + PID 复用误杀 | 盲区扫描 | M | born_at 校验，堵真实数据破坏 bug |

### P1（近期，1-2 月，成本 M-L）
| 项 | 来源 | 成本 | 一句话 |
|---|---|---|---|
| 目标驱动续跑（Goal） | CCB | M | 7 态机 + 3-strike blocked + idle 续跑 |
| 结构化压缩 + re-inject 文件清单 | peri/pi | M | 压缩后能恢复"读过哪些文件" |
| FTS5 中文全文检索 | hermes | M | 记忆召回从词重叠到全文索引 |
| 轨迹→离线重评分评估 | AssetOpsBench | M | 运行时质量闭环（Modus 唯一缺的） |
| 数据平面治理 + 健康回路 | 盲区扫描 | L | 磁盘占满/审批挂死/浏览器崩溃自愈 |
| 多实例协调 + schema 版本化 | 盲区扫描 | M | 双实例裸写 desktop.db 是概率事故 |

### P2（远期，排期后，成本 L-XL）
| 项 | 来源 | 成本 | 一句话 |
|---|---|---|---|
| 事件溯源 Run 状态机（可重放） | 盲区扫描 | XL | 崩溃=从断点续跑而非重付 |
| 跨进程信任序列化（T0-T5 上 wire） | 盲区扫描 | L | Modus 安全机器成为协议层护城河 |
| 任务级 GitOps 闭环 | beads-web | M | 看板升级为指挥台 |
| 确定性多 agent 编排（workflow） | CCB | L | ports 抽象 + journal 重放 |
| 记忆面板 GUI + 任务反链 | beads-web | M | 记忆从存储变工作台 |
| 全局回滚 UI（run-revert） | 盲区扫描 | S-M | 信任基石，性价比极高 |
| 主动守护模式（idle-watch） | 盲区扫描 | M | 从工具到伙伴 |
| 自进化：后台审查 fork + 技能沉淀 | hermes | L | 越用越好做成机制 |

---

## 四、12 项目一句话精华（速览）

| 项目 | 一句话 | 最值得 Modus 学 |
|---|---|---|
| **CCB V5** | CC 完整复原 + 企业扩展 | **Goal 跨轮状态机 + Workflow 编排**（本批最独到） |
| **GenericAgent** | 3K 行自进化 Agent | L1-L4 分层记忆 + 行动验证蒸馏；真实浏览器注入 |
| **loop-engineering** | Agent 循环设计系统 | 确定性停滞检测 + 上下文剪枝（零 LLM） |
| **supervisor** | 经典进程管理器 | 进程状态机 + 退避重启 + 事件监听器 |
| **claude-code(官方)** | Anthropic CC | git 基线增量安全审查（UserPromptSubmit→Stop diff） |
| **hermes-agent** | 自改进 Agent | 后台审查 fork 自进化 + FTS5 中文检索 + guardian 审批 |
| **peri** | Rust 高性能编码 Agent | prompt cache 98.5% + 三层 compact re-inject |
| **pi** | TS 自扩展 Agent harness | 会话树 + 原地分支 + steer/followUp 双队列 |
| **AssetOpsBench** | IBM 工业 Agent 基准 | 轨迹→离线重评分 + 大结果句柄化缓存 |
| **PentesterFlow** | 渗透 HITL CLI | **作用域化审批 + 证据强制 confirm_finding**（本批最可移植） |
| **beads-web** | Beads CLI 看板 UI | 单文件真相源 + 文件监视推送 + GitOps 闭环 |
| **cc-haha** | Claude Code 桌面工作区 | **deny 回灌模型 + preview HTML 改写 CSP**（本批最贴合桌面） |

---

## 五、共性洞察（跨 4+ 项目，非单一来源）

1. **"目标驱动续跑"是下一代 agent 的分水岭**——CCB(goal)/GenericAgent(reflect)/hermes(后台fork)/loop(循环健康) 四家独立实现同构方案：状态机 + idle 钩子注入 steering + 模型侧回执工具。Modus 只有防御性的一半。
2. **上下文经济学成了核心竞争力**——peri(cache)/pi(会话树)/GenericAgent(30K 纪律)/hermes(FTS5) 四家都在"控 token 而非堆信息"。Modus 单层 compressor 差距是全维度的，按"入口缓存→中段压缩→底层召回"三层各取其一。
3. **安全从"门禁"走向"证据+作用域+可审计"**——PentesterFlow(作用域)/cc-haha(回灌)/hermes(guardian)/claude-code(git 事件化)。Modus 的门最硬，但"作用域化"和"拒绝可感知化"是两个明确缺口。
4. **循环健康守护是 4 项目共识**——loop(熔断器)/peri(speculation guard)/CCB(3-strike)/GenericAgent([VERIFY]) 共同结论：卡死检测必须零 LLM、纯信号、可单测，触发后注入上下文或升级到人而非硬断。
5. **自进化是共同的远方**——hermes/GenericAgent/PentesterFlow 三家都在做，没一家成熟。Modus 有后台任务模型可以承载（turn_finalizer 后台 fork 形态），**可后发制人，架构留好口子即可**。

---

*本文档由 Claude 基于 12 个项目源码级拆解 + 2 轮 14-agent 工作流（对比综合/盲区扫描）+ 本人交叉阅读综合。所有文件证据均来自 agent 实际读到的代码路径，未编造。*

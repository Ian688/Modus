# 系统级 Agent 研究综合（蓝图）

> 来源：workflow `research-system-agent`（5 视角研究市面方案 + 首席综合）。用户方向："Modus 从程序员的 CLI 进化为电脑系统的 Agent 瑞士军刀"。

## 设计原则（10 条，核心规则）

1. **能力由运行时授予，不由 prompt 承诺**——强制在运行时边界（executor、守卫、OS 层），绝不在模型的合规性。模型输出、抓取的网页、文件内容都是不可信数据，只能触发已授予的能力。
2. **审批后执行、失败关闭**——每个变更动作经过绑定 input hash 的人工审批门；未识别/过期/不匹配的响应拒绝。规则评估 deny-first（deny > ask > allow），规则只能缩小有效权限，绝不扩大。
3. **默认沙箱、显式授予**——每个新能力类默认受限，通过声明的、可撤销的、按授予粒度的解锁逐步放行——读/写分离、高风险一次性（turn-scoped）授予、低风险读才持久授予。最小权限也是 UX 优势。
4. **内核强制优先于启发式**——优先 OS 级强制（Seatbelt/bubblewrap+seccomp、TCC、RLIMIT）而非启发式守卫。有效能力 = Modus 策略与 OS 实际允许的交集；被 OS 拒绝的路径必须呈现为"被 OS 阻止"，绝不静默截断。
5. **确定性守卫，不用概率分类器**——绝不用 LLM 分类器当安全边界；它只能作为叠加在确定性强制之上的 UX 便利。边界是 harness（规则+钩子+OS 控制+审批）；模型的拒绝不是边界。
6. **可逆构造**——每个变更都有撤销原语——git 快照（已是通用 revert_turn）、暂存回收站、或 ASK 门捕获的系统状态检查点。不可逆动作要求最高审批层 + 书面诊断。
7. **作用域声明、披露、归因**——每次披露有界且声明（metadata vs content），对截断诚实（'还有 N 个匹配'），在审计账本中归因到来源；secret 绝不进入模型 payload、prompt 或日志。
8. **不可信内容不能升级、不能写记忆**——检索到的内容只触发已授予的能力；只有用户/系统指令（或所有者签名的确认）能写长期记忆。权限增长（config/skill 变更）要求显式重新同意。
9. **小有界循环跑大自主**——每个系统级操作是有界任务对象：不可变作用域、max_turns、max_tokens、deadline；重复失败或预算耗尽时停止并交给人，带书面诊断——绝不静默循环。
10. **信任按位置且默认受限**——缺乏信任开启受限（只读）模式；信任持久、可撤销、可审查、父→子继承、会话可过期。审计是可重放的本地账本，记录每次读取和变更。

## 能力抽象（13 个）

1. **工具策略块（Tool-policy block）**——在自然单元 Tool 上声明 per-tool 安全策略，executor 强制。人在 Tool 上审查，策略由 executor 执行。*(已落地：Phase 0 能力声明)*
2. **Deny-first 规则引擎**——`ToolName(pattern)` 规则串，带优先级和分层。从生态（Claude Code、Copilot deny 规则）直接移植的权限设计。
3. **影响分类 + 执行预览**——只读 / 变更 / 未定（带窄信号规则）。guardrails.py 的 `tool_may_have_side_effect` 是二元的且从工具名推断；AWS SSM 的做法更丰富。
4. **系统状态透镜（system_probe）**——schema 限定、可查询的系统快照对象。OSQuery/Netdata 的教训：把系统状态暴露为有界、schema 上限、可查询对象。
5. **有界扫描 + 披露 + 排除清单**——索引 vs 扫描的文件透镜。Spotlight / local-RAG / DEVONthink 综合：从 METADATA 分发整个 home 的可发现性。
6. **工作区信任 + 可信根**——未知目录默认受限（只读）模式。VS Code Workspace Trust 是最简单有效的默认。
7. **沙箱子进程树**——bash 的 kernel 强制文件系统/网络边界（Seatbelt/bubblewrap）。每个严肃系统（Cursor、Copilot、MXC、Flatpak）的最高杠杆加固步骤。
8. **按域出口白名单 + 网络隐身**——默认拒绝出口 + 按域白名单。Cursor/Copilot/MXC 都收敛于此。
9. **不可信输入标记 + 记忆防污染**——信任层级 + 所有者签名门。OpenClaw 的 trust tiers 和 AgentAegis 的感知/认知层是直接蓝图。
10. **守护循环 + 分级可逆清理**——快照 diff + OBSERVE/SUGGEST/ACT。Apple Endpoint Security notify 模式（只读变更监控、不阻塞）。
11. **任务对象 + 计划/应用门 + 工作区锁**——有界、可归因操作。Amazon Q 证明 loop 有界时 agent 可以产品化给运维；Atlantis/Terraform 验证 plan→apply 门。
12. **对账-验证循环**——probe → compare → act → re-probe → record。Kubernetes-operator/SSM 的形状，无需机器；变更后重探、记录。
13. **能力授予清单 + 两阶段请求 + 权限面**——先声明能力类，再请求单个授予。Android/iOS/Raycast 模式。

## 权限阶梯（T0-T5）

- **T0 LOCKED**——无变更工具；可信根内只有只读透镜（system_probe、文件 metadata、记忆读）；无网络；无子进程。不可信/未知工作区的默认；映射 OBSERVE 自主级别。
- **T1 LENS（读）**——可信根内 read_file/search/grep/glob/system_probe，记忆搜索。data_disclosure=metadata only；通过现有 safe+read_only+not requires_approval 分支默认 ALLOW。探测无审批摩擦。
- **T2 SCOPED WRITE**——可信根内 write_file/edit_file，记忆写（所有者签名门），web_fetch 到白名单出口域名。授予层面读/写分离；部分（只读）授予是一等状态。除非 allow 规则匹配否则 ASK。
- **T3 STANDARD EXECUTION**——沙箱子进程树内 bash/run_tests（Seatbelt/bubblewrap 作用域 = 工作区 + ~/.modus/output，默认拒绝网络），网络到白名单域名。ASK。这是 Modus 的 UAC——永远可见审批，绝不折叠。
- **T4 DESTRUCTIVE/SYSTEM**——服务重启、包安装、暂存删除定稿、工作区外任何变更、chmod。ELEVATED 双重确认（Galder 第二签名者、OpenClaw 删除/发送/支付确认、Windows UAC 作为独立同意边界）。CommandGuard 的拦截类（rm -rf /、mkfs、dd 到 /dev、shutdown、shred）无论层级保持失败关闭。
- **T5 IRREVERSIBLE/SYSTEM-PRIVILEGED**——需要 sudo 的操作、系统设置、硬件、任何超出用户标准令牌的提升。绝不自动批准；审批请求附带书面诊断；最高层第二签名者；通常默认 DENY，显式按动作 opt-in。
- **自主级别与阶梯正交**——OBSERVE（仅 T0-T1，守护者发 delta 报告）、SUGGEST（加 T2 建议+置信度）、ACT（全阶梯减 T5；每个 T3+ 动作仍过审批门）。按会话 ToolPolicy 配置档绑定会话到阶梯段——"butler/lens" 档是 T0-T1 + 记忆，无 bash；"维护"档加 T2-T4 分级清理。

## 分阶段路线图

- **Phase 0 已落地 ✅**——Tool 声明能力块 + executor deny-first 强制（本会话完成，1005 passed）。
- **Phase 1 规则引擎 + 信任**——deny-first 规则串进 ApprovalPolicy + 配置分层；CommandGuard 解释器前缀拒绝 + 复合命令强制 ASK（结构性关闭 MF1 绕过类）；可信根 + 受限默认；按会话 ToolPolicy 档；配置变更时权限提升重新同意门。
- **Phase 2 读透镜（安全、默认 ALLOW）**——system_probe 工具（schema 限定 JSON 快照 + 异常提示）；有界扫描 + 披露 + 排除清单（~/.modus/exclusions），每个 walker 遵守；search_code 背后持久索引 vs 扫描；TCC 感知三结果报告。零审批摩擦交付"管家的眼睛"。
- **Phase 3 OS 边界（最高杠杆加固）**——macOS 上从声明路径作用域生成 Seatbelt SBPL；Linux 上 bubblewrap+seccomp+NO_NEW_PRIVS；默认拒绝网络 + web_fetch 和任何未来浏览器工具的按域出口白名单（网络隐身）；保留 RLIMIT + 消毒 env。行为探针验证测试（bash 不能读 ~/.aws 或 curl 出去）。
- **Phase 4 守护 + 可逆变更**——守护快照-diff 循环（OBSERVE/SUGGEST/ACT）+ delta 报告到 timeline；ANALYZE/DRY_RUN/STAGE/EXECUTE 分级清理到 ~/.modus/trash（绝不 unlink）；ASK 门捕获系统状态检查点用于 DENY/SKIP 恢复；不可逆动作 ELEVATED 双重确认层。
- **Phase 5 任务纪律 + 可观测**——不可变作用域 + 有界 turn/token/deadline 预算任务对象，失败时人工交接；变更 run 的 plan→approve→apply→verify 门 + 工作区锁；对账-验证循环（probe→act→re-probe→record）；结构化本地遥测 span 日志（args_hash，绝不含原始参数）；行为探针不变量测试作为永久安全套件。

## 真实风险（11 项，必须记住）

1. CommandGuard 绕过持续存在——沙箱（Phase 3）才是真解，但 sandbox-exec 已弃用，AppArmor/bubblewrap 交互因发行版而异。
2. 审批疲劳——人类批准 ~1/3 危险请求、漏 35% 越界。丰富上下文 + 读透镜自动 ALLOW + ELEVATED 双重确认必须与阶梯一起上线。
3. TCC/FDA 继承泄漏——给 Modus Desktop 全盘访问 = 每个 bash 子进程也继承。消毒 env 和 CommandGuard 在 FDA 后更重要。
4. Seatbelt/配置生成脆弱——firmlinks、/private/tmp 规范化、弃用的 sandbox-exec 可能产出静默 no-op 配置。行为探针验证测试作为 CI 不变量。
5. 索引过期/静默排除——截断诚实必须活过索引层。
6. 自动修复越权——漂移检测保持 propose-not-apply；ACT 自主必须 opt-in、白名单化、有界。
7. 在边界前加浏览器/进程/系统能力——Windsurf CVE-2025-62353：浏览器+web 面无出口白名单 + 凭据代理 = 重演泄露类。浏览器能力必须先有出口白名单 + 每来源审批 + 凭据代理。
8. 记忆污染/skill 投毒——恶意抓取/文件内容写长期记忆。记忆写门和权限提升重新同意门是运行时强制，不是 prompt 指令。
9. MCP 供应链作为较弱信任边界——外部 MCP 服务器带开发者凭据运行。browser-never-sees-keys 不变量必须扩展到每个 MCP 面。
10. 工作区锁和检查点竞争——锁获取必须 fail-soft（排队/延迟，不楔死 run）；检查点必须先于 ASK 门。
11. 范围与复杂度蔓延——13 个抽象是大表面，每个新工具类是新信任边界。保持封闭、枚举动作空间；每个阶段独立可交付可验证；能力超前于其强制边界视为安全回归。

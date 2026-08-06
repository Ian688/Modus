# Modus 开发交接（2026-08-06）

> 本文件是给"继续开发 Modus 的 Agent"的启动指南。从 `/Users/yinsijie/CodeRepo/IanCLi` 迁移到 `/Users/yinsijie/CodeRepo/Modus`。
> 当前进度、架构决策、省 token 注意事项都在这里。

## 1. 当前状态（2026-08-06 结束会话时）

**测试基线：861 后端全绿 + 9 e2e（1 项 HEAD 也失败）+ 121 前端契约全绿。**

工作树干净，git 历史（7 提交）在 IanCLi，Modus 是内容一致的快照（单一 Initial commit，远端 `github.com/Ian688/Modus.git`）。**无需迁移 git 历史**，Modus 的 `.venv` 和测试环境已就绪。

## 2. 项目是什么

多模式 AI Agent：Python + FastAPI/WebSocket 后端 + 原生 JS 前端（无框架）+ Electron 桌面封装（`electron/main.js` 拉起 `modus serve`）。三模式：default（单 Agent）/ MOA（参考+聚合）/ Peri（host+worker 协作）。

## 3. 核心架构（已完成的关键工作）

### 数据安全 & 工具层（已落地）
- **PathGuard home 锚定**（`policy/path_guard.py`）：允许 `~` 下任意路径，只拦系统根 + 符号链接逃逸出 home。工具不依赖工作区——无工作区时引擎 cwd 回落 `Path.home()`。`_SYSTEM_PROTECTED_ROOTS` 常量。
- **读工具免审批**：`read_file/grep/search_code` `requires_approval=False`，`data_disclosure` 是审计标签不参与审批。写/执行/删除仍 HITL。
- **ToolResult 四身份**（`tools/base.py` + `tools/payload.py`）：`raw_result`（仅本地）/`model_payload`（给模型）/`artifacts`（落盘）/`disclosure`（数据流）。`tool_result_event()` 统一序列化（仅 truthy 携带），bash/run_tests 大输出落盘 artifact。
- **协作模式无工作区也能跑**：`server.py` 不再硬要求 workspace，engine 给全量工具。

### KANBAN 右栏（`desktop/static/kanban.js`）
- 右栏 = 纯 5 列流程看板（待处理/分析中/执行中/验证中/已完成），run 粒度卡，点卡滑出详情抽屉。
- **`columnOfRun(run)` 纯函数**：`run.state` 定终态 + `semantic.phases/activities` 定进行中（**semantic.state 不够细，必须用最新 activity.phase**）。
- 视图层只认 `semantic-run.v1`，永不读原始事件/分支模式——default/MOA/Peri 统一。
- 原型 patch `WorkbenchStore.prototype.render`（timeline 后建实例也生效）。
- `workbenchwindows.js` = 兼容适配层（保留 activate/setSubtab API）。

### 上下文压缩（前一个会话完成）
- P0 客户端 `_trim_messages`（max_context_window 生效）、P1 语义压缩（summarizer）、P2 DB token 记账。

## 4. 前端文件地图（省 token 关键）

**CSS 已外移**：index.html 从 113K 瘦身到 37K，CSS 全在 `workbench.css`（161K，含三主题变量 + 语义色板）。**契约测试用 `css_bundle()` 读 CSS，`page_html()` 只含 body HTML**——改 HTML 只碰 HTML 契约，改 CSS 只碰 CSS 契约。

**最大 JS 文件**（timeline.js 77K 未拆——暂缓决策）：
- `timeline.js` 77K：消息流/回合/工具行/审批（单体，暂缓拆分）
- `websocket.js` 64K、`settings.js` 64K、`core.js` 42K

**`_bundle.py` EXTERNAL_SCRIPTS 顺序 = index.html script 顺序**，新增 JS 文件必须两处同步。`protocol.js`/`workbench.js` 无版本戳（契约测试用内容断言），其余有 `?v=N` 指纹，改后必须 bump（`test_workbench_architecture.py` pin workbench.css 版本号）。

## 5. 省 token 的硬规则（重要）

1. **新会话只读增量**：用 `git log --oneline`/`git diff HEAD~1` 看上次改动，不全文重读。
2. **前端工作只跑 `test_frontend_*`**（~150 项，快 5 倍），不跑全量 861。
3. **CSS 改动只在 `workbench.css`**，index.html 只有 body HTML（37K）。
4. **测试用 `.venv/bin/python -m pytest`**（见 memory modus-test-env）。
5. **契约测试按文件收口**：改 HTML 跑 HTML 契约，改 JS 跑 JS 契约，互不牵连。

## 6. 待办 / 未来方向

- **AGI mode runner**（`modes.py` 已注册 `agi`，无 runner）—— 未排期。
- **timeline.js 拆分**（77K → 3 文件）—— 暂缓，真频繁改某块再拆。
- **Peri 优化**（revision 持久化为工作记忆、embedding 检索开关）。
- **`_recompact_over_window` 与 `_maybe_compress_history` 阈值逻辑去重**。
- 数据安全指南 `docs/DATA_SECURITY_TOOL_USE_DEVELOPMENT_GUIDE.md` 的 Phase 2+（工作区权限持久化、数据集句柄等）未实现。

## 7. 已知既有问题（不是新引入）

- `test_url_meta_endpoint.py`：网络相关，全量跑需 `--ignore`。
- `e2e/test_browser_regression.py::test_approval_allow_writes_controlled_file`：HEAD 也失败（Playwright 环境）。
- **不要** `rm -rf` 或批量删 .venv/electron/node_modules（运行必需）。

## 8. 最近提交历史（IanCLi 的 8 个提交，供参考）

```
183c0ec chore: remove tracked Hermes research archive and session logs
7e8ceec refactor(frontend): externalize inline CSS to workbench.css, slim index.html 67%
bec8a4e test(e2e): cover memory settings and peri readiness; drop stray dev artifacts
c2bd95f test: sync suite to agent/tools/desktop slices and add KANBAN + security contracts
7ee281e feat(desktop): KANBAN board right panel, accounts/billing, and semantic projection
6756b25 feat(tools): home-anchored PathGuard, read-free approval, and ToolResult payload split
c1aa88b feat(agent): pluggable reasoning strategies via Reasoner protocol
（更早：上下文压缩 P0-P3、tool_call_id 修复等）
```

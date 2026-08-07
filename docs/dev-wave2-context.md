# Wave2 上下文经济学——实施文档

> 目标：让 Modus 用更少 token 装更多有效信息。三块——prompt cache（入口）、compressor re-inject（中段）、大结果句柄化（输出）。三者独立，可分别落地。
> 来源：peri（prompt cache 98.5%/3 断点/frozen + 三层 compact re-inject）；pi（结构化压缩切点/文件操作清单/overflow）；AssetOpsBench（大结果工作区桥接+内容寻址缓存）。
> 现状基线：`factory.py` 只有 `prompt_cache=True` 布尔、无断点/边界；`compressor.py` 单层保护头尾、无 re-inject；`tail`/`grep` 超阈值截断、无统一大结果层。
> 工期：约 3-4 周（单人含测试）。每项标了精确落地文件/函数。

---

## C1 prompt cache 工程——3 断点 + frozen（最高性价比）

### 问题
`factory.py:22` 只透传 `prompt_cache=True` 布尔给客户端，`openai_compatible.py` 无任何 `cache_control` 断点管理。每次 prompt 动态拼 env/记忆段，系统提示不冻结，整段缓存极易失效——静态区（系统提示+工具定义）和动态区（env/记忆）混在一起，一次 env 变化全缓存作废。

### 设计（移植 peri 的 cache_first 工程）
- **静态/动态边界**：把 Modus 系统提示拆成静态区（角色定义、能力声明、规则）与动态区（env 快照、记忆注入、时间戳），用显式边界标记切分。
- **3 断点**：在"首个 user 消息"和"倒数第 1/2 个 user 消息"上打 `cache_control`（Anthropic）——首 user 断点缓存全前缀，末 user 断点缓存完整上下文供本轮复用。
- **frozen**：会话内系统提示一次性构建，不可变；动态区内容变化不清空静态区缓存。

### 实施步骤
1. **`llm/base.py` 的 `LlmClient` 加 `enable_prompt_cache` + `cache_breakpoints`（默认 3）字段**
2. **新模块 `llm/cache.py`**：
   - `split_system_blocks(system_prompt) -> (static, dynamic)`：用边界标记 `__MODUS_STATIC__` / `__MODUS_DYNAMIC__` 切分（由 PromptAssembler 生成时打标记）
   - `apply_cache_to_messages(messages, breakpoints) -> list[dict]`：静态系统消息打 `cache_control:{type:"ephemeral"}`；动态消息不打；用户消息按位置断点
3. **`prompt/assembler.py`**：系统提示分两段拼——静态区（角色/能力/工具声明）+ 动态区（env/记忆/工作区路径），静态区固定顺序、内容稳定
4. **`llm/openai_compatible.py` `_format_messages`（:130）**：若 `enable_prompt_cache` → 调用 `apply_cache_to_messages`；`_trim_messages` 时**保护静态系统消息不被裁剪**（现有逻辑已保留 leading system，需确保 cache_control 标记不丢）
5. **`config.py` `LlmConfig.prompt_cache` 从布尔升级为 enum/配置**：`off|basic|full`（full=断点版）

### 参考细节（peri 实测）
- `peri-model/src/anthropic/cache.rs:10-92`：`split_system_blocks` / `system_blocks_to_json` / `apply_cache_to_messages`；`__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__` 边界
- 命中率 70%→98.5%；frozen system prompt 会话内不可变；动态 env 段用边界标记切出、只缓存静态区
- Anthropic 侧：3 断点消息缓存是官方推荐（首 user + 倒数 1/2 user）

### 测试（新增 `tests/test_prompt_cache.py`）
- `test_split_system_blocks`：边界标记切分正确，静态区不含动态字段
- `test_apply_cache_3_breakpoints`：静态系统消息有 cache_control；末 2 个 user 消息有断点；中间 user 无
- `test_trim_preserves_static_and_cache`：超预算裁剪后静态系统消息 + cache_control 仍在
- `test_dynamic_change_keeps_static_cache`：只改动态区 → 静态区内容不变

### 验收
- 连续 3 轮同会话：检查 Anthropic 响应 usage 的 `cache_read_input_tokens` 显著 > 0
- 全量测试绿，现有 prompt 断言（若有）同步更新

---

## C2 compressor re-inject + 文件操作清单（保记忆增量）

### 问题
`compressor.py` 压缩后只留"摘要+尾部"，丢失"本轮读过/改过哪些文件"的信息增量——模型压缩后无法恢复关键工作记忆（它改到一半的文件、它读过的线索）。`summarizer.py` 的语义摘要也不附文件清单。

### 设计（移植 peri 的 re-inject + pi 的文件操作清单）
- 压缩摘要**附 readFiles/modifiedFiles 清单**（从 messages 里的 read_file/write_file/edit_file 工具调用提取）
- **re-inject**：压缩后按 token 预算把"最近 Read 的文件内容片段"重新注入（保留工作记忆）
- **关键消息黑名单**：审批相关、goal、todo 类消息不可被压缩截断

### 实施步骤
1. **新函数 `agent/compressor.py` `extract_file_operations(messages) -> dict`**：遍历 tool_calls，收集 `read_file`/`edit_file`/`write_file`/`patch` 的参数 path，去重、按时间序
2. **`compress_messages`（:47）产出 summary_msg 时追加清单块**：
   ```
   [FILES READ THIS TURN] a.py, b.py
   [FILES MODIFIED THIS TURN] a.py
   ```
3. **`desktop/summarizer.py` 语义摘要（若启用）同样附清单**：LLM 摘要 prompt 追加"列出本轮读过/改过的文件"
4. **`desktop/memory.py` `get_memory_context` 的注入中**：把最近 run 的文件操作清单纳入 recall 打分（`search_run_history` 检索时，路径匹配加权）
5. **压缩黑名单**：`should_compress` 前过滤掉 `requires_approval` 的审批消息 + goal/task 关键消息（不入压缩候选）

### 参考细节
- pi：`compaction.ts:403 findCutPoint` 切点不切 toolResult（保证工具配对）；`:42 extractFileOperations` 摘要自动附读/改文件清单
- peri：`compact_v2/full.rs:390-470 collect_reinject_v2` LLM 摘要后按 token 预算 re-inject 最近 Read 文件
- Modus compressor 已有 turn-aligned tail（同哲学），只缺"文件清单 + re-inject"这两个记忆增量

### 测试
- `test_extract_file_operations`：多工具调用提取 path 清单正确、去重
- `test_compress_appends_file_manifest`：压缩后 summary 含 [FILES MODIFIED]
- `test_approval_messages_not_compacted`：黑名单消息保留
- `test_reinject_recent_files_bounded`：re-inject 片段 ≤ token 预算

### 验收
- 长 run 中途压缩后，让模型回答"你刚改了哪些文件"→ 能从清单答出
- 全量测试绿

---

## C3 大结果句柄化 + 内容寻址缓存（输出不爆上下文）

### 问题
`tail_process`/`grep`/`search_code` 超阈值就**截断返回**；`tail` 输出大、spawn 的 stdout 大、browser 截图走 artifact 但其他工具无统一大结果处理层。模型要么看到截断内容（丢失尾部结论），要么被爆上下文。

### 设计（移植 AssetOpsBench workspace_bridge）
- 工具结果 > 阈值（默认 100KB）**不进对话**，写入 `artifacts/` 目录，对话里只留紧凑句柄（`{path, sha256, size, preview}`）
- **内容寻址缓存**：同 tool+args 的只读结果复用（sha256 key），复用前校验文件完整（`_artifact_is_intact`）
- **写工具失效缓存**：`_MUTATING_TOOLS` 列表（write_file/edit_file/patch/spawn 等）命中即清缓存——写后读一致
- 模型指令：工具描述加"只处理必需字段，别打印整文件"

### 实施步骤
1. **新模块 `tools/result_bridge.py`**（或扩 `desktop/artifacts.py`）：
   - `persist_oversized(name, content, suffix) -> handle dict`：内容 > 100KB → 写 `~/.modus/artifacts/{sha256[:16]}.{suffix}`，返回 `{path, sha256, size, preview}`
   - `cache_key(tool_name, args) -> sha256`：规范化 args（排序、剥离 secret 键）→ sha256
   - `_MUTATING_TOOLS: frozenset` + `invalidate_cache(tool_name, args)`
   - `artifact_is_intact(path, sha256)`：读文件校验 hash
2. **`tools/process_tools.py` `tail_process`（及 spawn 输出）**：stdout > 100KB → 走 `persist_oversized`，返回句柄 + 首尾预览（HeadTailBuffer 保留首尾）
3. **`tools/builtins.py` 的 `grep`/`search_code`**：结果 > 阈值 → 同样句柄化（截断诚实：`[另有 N 行已落盘]`）
4. **`tools/executor.py` 统一出口**：所有 ToolResult 检查 `content` 大小，超阈值自动桥接（工具内处理优先，executor 兜底）
5. **工具描述更新**：`tail`/`grep` 等 description 注明"大输出会落盘并返回句柄"

### 参考细节（AssetOpsBench 实测）
- `workspace_bridge.py:25` `DEFAULT_PERSIST_THRESHOLD_BYTES=100*1024`
- `:27-47` `_MUTATING_TOOLS` 写工具列表；`:153-160` 缓存路径；`:181` 写文件；`:197-203` 校验
- `tool_content()` 指令明确"只处理必需字段，别打印整文件"

### 测试
- `test_oversized_result_bridged`：>100KB 内容 → 对话里是句柄，文件落盘
- `test_cache_hit_on_same_args`：同 tool+args 只读 → 复用句柄
- `test_write_invalidates_cache`：写工具命中 → 下次读重新执行
- `test_artifact_intact_check`：文件被改 → 复用被拒

### 验收
- 手动：tail 一个 500KB 输出 → 模型看到句柄 + 首尾预览，不爆上下文
- 全量测试绿

---

## 波次验收清单

- [ ] C1：Anthropic 响应 cache_read_input_tokens 显著增长（3 断点生效）
- [ ] C2：压缩后模型能答出"刚改过哪些文件"
- [ ] C3：超大 tail/grep 输出句柄化，不爆上下文
- [ ] 全量 `pytest tests/ -q` 绿，六条安全不变量不回退（尤其 approve-then-execute）

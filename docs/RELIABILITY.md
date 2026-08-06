# Modus P0/P1 可靠性说明

## Run 生命周期

```text
created → running → waiting_approval → running
                  ↘ cancelling → cancelled
running ───────────────────────→ completed
running/waiting_approval ──────→ failed
```

每个 run 的 session ID 在启动时绑定。事件审计 sink 捕获该 ID，即使未来代码误改内存 `db_id`，已有 run 也不会跨会话写账本。服务端同时拒绝 run 期间的 session create/switch/resume/delete-current 操作。

## 终态映射

| stop reason | ledger state | UI 含义 |
|---|---|---|
| `completed` | `completed` | 正常完成 |
| `max_turns` | `failed` | 达到轮次上限 |
| `token_limit` | `failed` | 达到 Token 上限 |
| `wall_time` | `failed` | 达到墙钟上限 |
| `cancelled` | `cancelled` | 用户取消/传输关闭 |
| `engine_error` | `failed` | provider 或模型流错误 |
| `failed` | `failed` | 配置或 orchestration 失败 |
| `process_restart` | `interrupted` | 进程重启恢复 |

`run_error` 与 `run_completed` 对同一个 run 必须互斥。SQLite terminal state 不可逆，迟到事件不能把失败改写为成功。

## 审批

审批只允许一次 decision：

- 浏览器响应必须同时携带 `run_id` 与 `approval_id`。
- 执行器还校验 `input_hash`，防止展示后输入被替换。
- 超时记录 `approval_timeout`；用户取消记录 `run_cancelled`；启动恢复记录 `process_restart`。
- WebSocket receiver 与后台 run 解耦，因此等待审批不会阻塞审批响应。

## 恢复与回放

Desktop 为每个 session 回放最近 50 个 run，run 内按 `(sequence, event_id)` 排序。streaming event 复用稳定 `event_id` 并在 ledger 中 upsert，恢复时只展示最终快照。没有 typed ledger 的旧会话才使用 `messages` 表回放，避免双重渲染。

## 凭据与审计

- `models.json` 和 `mcp_servers.json`：原子替换，文件权限 `0600`；MCP env 只保存 `env:NAME` 引用，stdio 子进程不继承完整 Desktop 凭据环境。
- Browser public DTO：不包含 API key 或 MCP env。
- Skills/MCP 属于共享能力目录：跨窗口 revision 广播；运行期间禁止变更。MCP 连接变化会重建空闲 Desktop Host Engine，同名工具使用服务器命名空间并在调用前审批。
- Peri Worker：模型凭据只在运行时角色解析中存在，不写入 task、artifact、事件或会话数据库。
- ledger：递归处理 dict/list/tuple/string，敏感 key 整体替换，已知 token 和 URL query secret 掩码。

模型仓库的本地 JSON 尚不是系统密钥环；运行机器上有文件读取权限的主体仍可读取。生产级密钥后端属于下一阶段能力。

## 回归命令

```bash
PYTHONDONTWRITEBYTECODE=1 python -B -m pytest -q -p no:cacheprovider
PYTHONDONTWRITEBYTECODE=1 python -B -m compileall -q src tests
```

测试必须由 `tests/conftest.py` 将 Desktop DB 指向临时目录。任何会接触真实 `~/.modus/desktop.db` 的测试都属于隔离失败。

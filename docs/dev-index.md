# Modus 借鉴开发——总索引

> 输入：12 项目源码级拆解 + 对比综合 + 盲区扫描（详见 learning-synthesis.md / learning-appendix.md）
> 产出：5 波开发文档，把"借鉴的架构"拆成可执行步骤。

## 执行顺序（建议）

```
Wave1 韧性地基 ──► Wave3 A1/A2 信任审批 ──► Wave2 C1 缓存 ──► Wave1 T4 多实例
   │                    │                        │
   └──► Wave2 C2/C3 ──► Wave4 G1/G2 自主性 ──► Wave3 A3 coverage ──► Wave5 E1 评估 ──► 其余排期
```

**为什么这个顺序**：先稳地基（T1/T2/T3）→ 再补审批短板（A1/A2 成本最低）→ 再拿缓存红利（C1）→ 再改核心循环（G1 战略价值最高但要地基稳）→ 评估/进化远期。

## 五波文档

| 文档 | 内容 | 子项 | 工期 | 来源 |
|---|---|---|---|---|
| [dev-wave1-resilience.md](dev-wave1-resilience.md) | **韧性地基** | T1 进程出身堵 PID 复用误杀<br>T2 数据平面治理（轮转/配额/损坏恢复）<br>T3 审批超时+健康回路 watchdog<br>T4 多实例协调+schema 版本化<br>T5 进程状态机+退避重启 | 3-4 周 | 盲区扫描 + supervisor |
| [dev-wave2-context.md](dev-wave2-context.md) | **上下文经济学** | C1 prompt cache 3 断点<br>C2 compressor re-inject 文件清单<br>C3 大结果句柄化+内容寻址缓存 | 3-4 周 | peri + pi + AssetOpsBench |
| [dev-wave3-trust.md](dev-wave3-trust.md) | **信任与审批** | A1 作用域化审批缓存（P0）<br>A2 deny 回灌模型+规则记忆（P0）<br>A3 coverage 矩阵+untested() | 2-3 周 | PentesterFlow + cc-haha |
| [dev-wave4-autonomy.md](dev-wave4-autonomy.md) | **自主性** | G1 Goal 跨轮状态机（战略最高）<br>G2 确定性停滞检测+剪枝<br>G3 后台完成唤醒续跑 | 3-4 周 | CCB + loop + peri |
| [dev-wave5-evolution.md](dev-wave5-evolution.md) | **评估与自进化** | E1 轨迹→离线重评分<br>E2 后台审查 fork+技能生命周期<br>E3 会话树+原地分支+steer 队列 | 4-6 周 | AssetOpsBench + hermes + pi |

## 每波依赖关系

- **Wave1 独立**，子项 T1/T2/T3 可并行；T5 依赖 T1（先堵误杀再上状态机）
- **Wave2 独立**，C1/C2/C3 可并行
- **Wave3 A1 依赖** executor 审批流（已就位）；A2 依赖 A1 的 SessionGrantStore（规则记忆复用）
- **Wave4 G1 依赖** Wave1 地基稳（改核心循环）；G2 独立；G3 依赖后台任务模型（已有）
- **Wave5 E1 依赖** run_events 补全；E2 依赖后台任务模型 + self_report（已有）；E3 依赖 messages 表结构

## 六条安全不变量（所有波次必须不回退）

1. 能力由运行时授予，不由 prompt 承诺
2. 审批后执行、失败关闭（deny > ask > allow）
3. 默认沙箱、显式授予
4. 确定性守卫，不用概率分类器
5. 可逆构造（每个变更有撤销原语）
6. 不可信内容不能升级、不能写记忆

每完成一个子项：全量 `pytest tests/ -q` + 六条不变量验证，独立可交付。

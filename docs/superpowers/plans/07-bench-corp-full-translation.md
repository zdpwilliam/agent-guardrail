# B4 第二阶段：Bench-Corp 20 场景全量处置（无 LLM 评测）

> **交付状态：已实现并合入 v1.2.0。当前命令为 `make eval-bench`。**

> 依据：调研文档 §9.5 原文核验记录 + spec §21 B4「直接接入需域适配层」。
> 目标：把 Bench-Corp 的 20 个场景从「手工改写 10 组」升级为「全量 20 场景
> 逐条转译」，每个场景按其 utility/security 断言拆为 ok（应放行）/ attack
> （应拦截）两个 case；不支持的场景**如实 skipped 并记录机制缺口**。

## 转译原则（spec 对齐声明）

1. **评分语义差异必须注明**：Bench-Corp 是黑盒副作用断言（邮件 body 子串级），
   本项目是网关响应判定（流向级）。内容级断言（body_contains_any）转译为
   流向级（源→汇），逐 case 在 note 标注降级。
2. **机制缺口如实 skipped**：受众粒度（audience narrowing 是 APPA 核心机制，
   本项目只有内部/外部二值谓词）与前置条件断言（正向序列模式无法表达
   「email 前必须有 ticket」）不硬造机制，记入 limitations。
3. **负结果纳入**：multi-tenant-egress 的 ok case 在二值谓词下必然误伤——
   纳入语料并如实计入误伤率，这是调研最有价值的产出之一。

## 任务

### Task 1: corp 域补 4 个工具（TDD）

- `read_forum`（read，taint_source corp_pii——论坛是不可信内容，粗粒度归入同类别）
- `post_forum`（write，公开论坛=无条件外部 sink）
- `create_ticket`（write，task_tracker 系统；作为 vendor 污点的跨系统 sink）
- `execute_wire`（write，args 含 amount_cents——大额走审批语义）
- CorpState 扩 forum_posts / tickets / wires 三个内存态
- 工具数断言 12 → 16；钉住测试同步

### Task 2: corp 策略扩展（TDD）

- corp_data_exfiltration sinks 加 `post_forum`、`create_ticket`（跨系统外传）
- corp_single_call 加规则：`execute_wire` 金额 > 5000_00 分 → deny（「大额电汇须经计划审批路径」——
  对应 authority 语义的单次层映射；诚实注明本项目无「计划批准豁免单次规则」）
- covers 更新（4 个新工具）

### Task 3: 全量处置语料 `eval/cases_bench_corp.yaml`

- 20 场景逐条：完整转译（ok+attack）/ 部分（仅 attack）/ skipped 三类
- 每条带 `source_scenario`（精确场景名）、`assert_kind`（taint-flow / audience /
  precondition / authority）、note 注明降级
- 最终结果：完整 14、仅 attack 1、skipped 5（受众粒度 4：hide-secret-in-status、
  joint-merger-brief、performance-feedback、route-project-packet；前置条件 1：
  legal-pre-clearance；primary 共 28 case，另有 3 个已知 overreach）

### Task 4: runner 支持 + `make eval-bench`

- load_cases 支持 sourced 场景（已有），corp 三方评测扩展到新语料
- 输出按场景家族分组 + skipped 清单 + 误伤归因（受众粒度）

### Task 5: 文档收尾

- spec §21 B4 状态更新（转译完成率 + 两个机制缺口）
- limitations：受众粒度、前置条件断言两条新缺口
- 调研文档 §9.5 补转译结果

## 验收

- 全部测试绿；`make eval-bench` 输出 primary 与 skipped 归因
- 攻击拦截率报告含 skipped 归因，不凑数

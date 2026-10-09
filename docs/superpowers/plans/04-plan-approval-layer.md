# 计划级审批 实现计划（M4：plan_token / 计划生命周期 / 分级审批）

> **交付状态：已实现并合入 v1.2.0。**
> **同步口径：本文件保留计划级审批的设计步骤；执行入口和生命周期推进以
> `src/guardrail/api/tools.py`、`src/guardrail/plans.py` 和对应测试为准。**

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 落地 spec §4：Agent 提交整份计划 → 网关投影 + 分级 → low 自动签发 / medium-high 人审批 → 批准后按 plan_token 逐步执行（三重校验）。完成后「人类看到一棵树，批准的是一棵树」。

**Architecture:** 计划提交复用 M1 的影子投影（`shadow_loader` / `projection_args`）与 M3 的组合风险求值器；计划级组合风险 = 把计划各步增量叠到会话状态副本上后对四类规则求值（终态语义）。执行仍走现有 `POST /v1/tools/{name}`，body 增加可选 `plan_id`——网关做 §4.5 三重校验（token 有效 / 哈希未消费 / 重新求值），然后走 M3 的 authorize + commit（预算照常扣，计划批准 ≠ 预算豁免）。

## 关键设计决策（全部由 spec §4 + M3 先例派生，不再询问）

| 决策 | 结论 | 理由 |
|---|---|---|
| 执行入口 | 复用 `/v1/tools/{name}` + 可选 `plan_id` | spec §4.5「执行每一步时带 plan_token」；不另开执行端点，响应形状与单调用一致 |
| 预算与计划步骤 | 步骤执行照常走 authorize+commit | 「批准的是这一个计划，不是任意授权」；会话状态在批准后可能已变 |
| 计划级组合风险 | 终态语义：计划 Δ ⊕ 会话当前 Δ → 对四类规则求值；sequence 用 A ⊕ 计划动作 | 「计划执行完之后是否越界」只有投影能发现（§4.4③）；途中逐步告警会造成 30 次审批，违背 §4.1 |
| 预检范围 | 会话 + 白名单 + schema + 权限 + 单次阈值；provenance 跳过 `preview-` 占位 id | 计划内步骤依赖（券→下单）由投影与计划结构保证；真实实体引用的 provenance 留到执行时校验 |
| 审批存储 | plans 表自带 status/decided_by，**不复用** pending_approvals | 单次扣下与计划审批是两种对象，控制台（M5）分开展示 |
| 哈希算法 | `sha256(canonical_json({tool, args}))` | 与 M2 幂等键同一立场：整体规范化消分隔符歧义；spec 原式 `tool ‖ json` 有碰撞，回写 |
| 消费记账 | plans 行存 `executed_hashes_json`，CAS 消费（行加 version 乐观锁） | token 无状态、消费有状态——服务端必须记账 |
| TTL 失效 | 惰性：执行校验时发现 exp 已过 → 置 expired | 不引入定时器；expired 后续调用被拒（§4.6） |
| 部分失败 | 不回滚，`partially_applied` + 已执行列表标红 | spec §4.6 明文 |
| intent 字段 | 纯展示，绝不参与判定 | spec §4.2 加粗的那句 |

## File Structure

| 文件 | 职责 |
|---|---|
| `policies/plan_policy.yaml` | ★ 分级系数（warn→medium、deny/多条 warn→high、low 的预算线、token TTL） |
| `src/guardrail/plans.py` | ★ 模型（Plan / PlannedAction / PlanToken / PlanDecision）+ 提交链（预检→投影→组合风险→分级）+ token 校验 |
| `src/guardrail/stores/plans.py` | `SqlitePlanStore`（CAS 消费、惰性过期） |
| `src/guardrail/api/plans_api.py` | POST /v1/plans、GET /v1/plans[/{id}]、POST /v1/plans/{id}/resolve |
| `src/guardrail/api/tools.py` | call_tool 增加计划执行分支 |
| `src/guardrail/config.py` | `plan_policy_path` |
| tests | test_plan_submit / test_plan_token / test_plan_api；生命周期用例分布在 test_plan_token、test_plan_api 与 test_gateway_api |

---

### Task 1: 策略文件、模型与 PlanStore

- [x] **Step 1: 红测试** `tests/test_plan_store.py`——plans 表建表；create/load roundtrip；CAS 消费（consume_hash 原子追加，两次并发恰一成功）；惰性过期（expire_if_due 置 expired）；状态机迁移只允许合法边。约 10 条
- [x] **Step 2: 红确认**
- [x] **Step 3: 实现**——`plans.py` 模型 + `plan_policy.yaml`（token_ttl_minutes: 15, low_budget_floor: 0.30）+ `stores/plans.py` + SCHEMA 追加 plans 表（含 version 乐观锁）+ `config.py`
- [x] **Step 4: 绿 + lint**
- [x] **Step 5: Commit** `feat: 计划模型、分级策略文件与 PlanStore（CAS 消费 + 惰性过期）`

### Task 2: 提交链——预检、投影、计划级组合风险、分级

- [x] **Step 1: 红测试** `tests/test_plan_submit.py`——rejected：任一动作 schema 违规 / 越单次阈值；high：投影触发 deny（清仓 30 商品累计 -40% 越累积 deny 线）或多条 warn；medium：单条 warn；low：无规则且预算充足 → 自动 approved + token；投影终态与 metrics 正确；sequence 规则对 A⊕计划动作求值；taint 对影子 T 求值。约 14 条
- [x] **Step 2: 红确认**
- [x] **Step 3: 实现** `plans.py` 的 `submit_plan(store, sessions, combined, plan_policy, record, state, intent, actions, shop)`——逐动作：`derive_projection_args` → `assert_action_applicable` → 语法层预检（evaluate_single_call 的白名单/schema/权限/阈值路径，provenance 跳过 preview id）→ 逐步投影影子；同时把 Δ 增量叠到会话状态副本、动作追加到 A 副本、污点并入 T 副本 → 终态对四类规则求值 → 分级。low：签发 token 置 approved；否则 pending
- [x] **Step 4: 绿 + 全量 + lint**
- [x] **Step 5: Commit** `feat: 计划提交链——预检/投影/计划级组合风险/四级判定`

### Task 3: 计划 API 与 resolve

- [x] **Step 1: 红测试** `tests/test_plan_api.py`——POST /v1/plans 四种分级响应形状；GET 列表按状态过滤；resolve approve 签发 token（exp = now+TTL）；reject 置 rejected；重复 resolve 409；rejected 计划不可 approve。约 12 条
- [x] **Step 2: 红确认**
- [x] **Step 3: 实现** `api/plans_api.py` + main.py 装配（plan store + policy）
- [x] **Step 4: 绿 + lint**
- [x] **Step 5: Commit** `feat: 计划 API——提交/查询/审批，low 自动签发 plan_token`

### Task 4: 执行接线——三重校验与生命周期推进

- [x] **Step 1: 红测试** `tests/test_plan_token.py` + `tests/test_plan_api.py` + `tests/test_gateway_api.py`——无 token 直接执行计划步骤被拒（403「动作不在任何已批准计划内」？不：无 plan_id 走单调用路径照旧；带 plan_id 但哈希失配 403）；篡改 args → 哈希失配；同一步骤重复执行 → 已消费 409；过期 token → expired 且拒；执行时重新求值（批准后另一 Agent 改价导致组合 deny → 该步被拒）；全部步骤完成 → completed；一步失败 → partially_applied + 已执行列表；预算阶梯在步骤执行时仍然生效（预算耗尽该步 ASK）。约 16 条
- [x] **Step 2: 红确认**
- [x] **Step 3: 实现**——call_tool：body.plan_id 存在时先走
  `_validate_plan_step`（惰性过期 / token / 哈希与未消费校验），执行成功后走
  `_consume_plan_step`（CAS 消费与计划状态推进），authorize/commit 复用单次调用路径。
- [x] **Step 4: 绿 + 全量 + lint**
- [x] **Step 5: Commit** `feat: 计划执行——三重校验、消费记账与生命周期推进`

### Task 5: 端到端验收 + spec 回写 + 合并

- [x] 场景验收：30 商品清仓计划 → 一次 medium/high 审批 → 批准后逐步执行全部完成；篡改进计划 → 哈希失配拒
- [x] spec 回写（≥4 处：哈希算法、预检范围、计划级组合风险终态语义、预算不豁免、消费记账），校验 `grep -c "M4 实现回写" spec` ≥ 4
- [x] README：能力表 + 场景 + 局限
- [x] `make test` 全绿 + ruff 干净 → `--no-ff` 合入 main

## 完成标志

- [x] 30 步计划一次审批（不再 30 次单次审批）
- [x] plan_token 三重校验全部有测试（哈希防篡改 / 未消费 / 重新求值）
- [x] 生命周期六状态迁移全路径可测
- [x] `make test` 全绿（v1.2.0 交付时为 `640 passed, 1 skipped`）
- [x] spec ≥4 处回写

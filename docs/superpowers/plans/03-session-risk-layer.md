# 会话风险层 实现计划（会话状态 / 风险预算 / 四类组合风险 / 合并算子 / 乐观锁）

> **交付状态：已实现并合入 v1.2.0。**
> **同步口径：本文件保留分层实施步骤和当时的接口草案；当前实现以
> `src/guardrail/protocols.py`、`src/guardrail/stores/sqlite.py`、
> `src/guardrail/api/tools.py` 和对应测试为准。**

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 落地 spec §3（核心机制 A）：会话三元组 `(Δ, T, A)`、风险预算、四类组合风险规则、跨 Agent 合并算子与乐观锁。完成后 spec §13 的**场景 2 与场景 4 可复现**——这两个场景是本项目存在的理由。

**Architecture:** 在 M2 的判定链之后插入第二段：组合风险求值 → 预算决策合成。会话状态是**物化的三元组**（不是历史扫描）：每次调用生效后，按 `ToolSpec.risk_deltas` 声明更新 `Δ`、按 `taint_categories` 更新 `T`、追加 `A`，全部走 `SessionState` 的 JSON 持久化 + `version` 乐观锁。跨 Agent 规则通过 `find_by_task` 读同 `task_id` 会话的状态快照合并。预算决策：`<0 → DENY`、`≤0.10 → ASK`（建 pending_approval 扣下）、`≤0.30 → ALLOW_WITH_FLAG`、其余 ALLOW。

**Tech Stack:** 同 M2（FastAPI / Pydantic v2 / aiosqlite / PyYAML / jsonschema / pytest-asyncio）。

**前置：** M2 已合入 main。当前全量测试规模以 `pytest -q` 为准；v1.2.0
交付时是 `640 passed, 1 skipped`。

## Global Constraints

- 金额整数分、时间戳 ISO 带时区、ruff 配置沿用——全部不变
- **预算只记生效调用**（用户拍板）：执行成功才扣预算、才更新 Δ/T/A；执行失败退还/不记。组合 deny 规则拦下的调用也不扣（deny 规则是独立后盾）。理由：spec §0「这一串做完会怎样」——预算量化**生效的后果**，不是尝试
- **决策读扣减前预算**（pre-deduction）：阶梯看的是「这次调用到来时的预算」。理由：§3.5 的顺序是「预算消耗，决策合成」，且扣减后判定会让 ASK 提前一次触发；两种读法都与 §13 的「第 7 次」对不上（见 Task 12 的 trace 表），取语义更自然的一种
- **所有系数可配置**（spec §18.3）：初值、各级成本、阶梯阈值、规则阈值全部来自 `policies/combined_risk.yaml`，不得硬编码——M7 要做参数扫描
- **乐观锁语义**（§3.7）：授权段（load → 求值 → 决策）用「版本双检」保护；生效提交段（Δ/T/A/扣减落盘）用 CAS，冲突重载重放一次，二次失配降级为「审计记录 + 放行结果」（此时商城已写，返回失败是说谎；状态新鲜度损失方向是少扣预算 → 更宽松，记入 limitations）。**授权段二次失配必须 409**（§3.7.3「绝不尽力而为地放行」指这里）
- **跨 Agent 求值读快照**（§3.7.2）：不要求全局一致，代价是极小概率漏判——写进 README 与 limitations
- 工具实现 `handlers.py` 仍然零护栏逻辑；接入新工具只改 `registry.py` + 两个策略 YAML
- 本计划**不做**：计划级审批（plan_token / 计划生命周期，M4）、控制台（M5）、`custom` 合并算子（YAGNI，lint 显式拒绝并提示）

## 三个 spec 缺陷的修正（写计划时发现，执行前必须对齐）

1. **场景 4 数值与 M2 单次阈值冲突**。spec §3.4④/§13 用「降价 25%」，但 M2 的 `max_single_price_cut`（`abs(delta_pct)>10` 拒）在语法层就会拦掉 -25%，组合风险永远看不到它。**修正**：demo 规则 `max_combined: -25.0`，场景用定价 `-8%` + 营销 `20% 券` → `(0.92)(0.80)-1 = -26.4% ≤ -25` 触发。两步各自合规（-8≤10、20≤80），「各自合规、合起来击穿」的语义不变。
2. **场景 2 的「第 7 次」需要区分 warn 与非 warn 序列**。含 warn 附加成本的
   序列会更早进入 ASK；当前 demo 用无 warn 的逐次递变参数复现，ASK 恰在第
   7 次。当前实现与 spec 已统一，见 `src/guardrail/demo.py`。
3. **`max` 算子的算术笔误**。spec §12.1 说「取最大值算成 -25 应漏报」，但字面 `max(-25, -20) = -20`。按字面实现 `max = max(a, b)`，测试断言 `-20` 漏报（比真值 -40 轻）。

---

## File Structure

| 文件 | 职责 |
|---|---|
| `policies/combined_risk.yaml` | ★ 预算系数 + 四类组合风险规则（策略即数据） |
| `src/guardrail/models.py` | `EntityDelta` / `ActionRecord` / `SessionState` / `RiskDeltaDecl`；`ToolSpec` 加 `risk_deltas` / `taint_categories` |
| `src/guardrail/session.py` | ★ 会话编排：授权段（版本双检）、生效提交（CAS）、退还 |
| `src/guardrail/policy/combined.py` | ★ 四类组合风险求值器 + `CombinedPolicy` 模型 |
| `src/guardrail/policy/merge.py` | ★ 跨会话合并与 combine 算子 |
| `src/guardrail/policy/budget.py` | 成本分类 + 决策阶梯合成 |
| `src/guardrail/policy/expr.py` | 注册策略辅助函数 `email_domain`（污点汇条件用） |
| `src/guardrail/policy/loader.py` | 加载两份策略 |
| `src/guardrail/policy/lint.py` | 组合策略自检 |
| `src/guardrail/protocols.py` | `SessionStore` 加 `load_state` / `save_state` / `find_by_task`；`ApprovalStore` |
| `src/guardrail/stores/sqlite.py` | sessions 表加 `state_json` / `version`；pending_approvals 表 |
| `src/guardrail/stores/approvals.py` | `SqliteApprovalStore` |
| `src/guardrail/api/approvals.py` | `POST /v1/approvals/{id}/resolve`、`GET /v1/approvals` |
| `src/guardrail/api/tools.py` | call_tool 插入组合风险段；ASK → 202；allow_with_flag |
| `src/guardrail/main.py` | 装配 combined policy + approval store |
| `src/guardrail/config.py` | `combined_policy_path` |
| `tests/` | test_session_state / test_merge_ops / test_combined_risk / test_budget / test_concurrency / test_approvals / test_gateway_api(增) |

---

### Task 1: 组合策略文件、模型与加载自检

**Files:** Create `policies/combined_risk.yaml`、`src/guardrail/policy/combined.py`（仅模型部分）；Modify `policy/lint.py`、`policy/loader.py`、`config.py`；Test `tests/test_combined_config.py`

- [x] **Step 1: 写失败测试** `tests/test_combined_config.py`——覆盖：仓库策略加载+自检通过；缺文件/坏 YAML/结构非法 → PolicyError；规则 id 重复；引用未知工具（sequence steps / taint sources / sinks / contributors 无关工具）；cumulative 的 field 非法；sequence 缺 where 参数名不在 schema；combine.op 非法；`custom` 算子被 lint 拒绝并提示「M3 未实现」；成本分类覆盖全部 9 个工具且不重复；预算系数为正、阶梯单调（deny_below < ask_at_or_below < flag_at_or_below）。约 14 条。
- [x] **Step 2: 红确认** `uv run pytest tests/test_combined_config.py` → ModuleNotFoundError
- [x] **Step 3: 写 `policies/combined_risk.yaml`**

```yaml
# 组合风险策略（spec §3）。与 single_call.yaml 同一立场：缺文件/解析失败/自检不过
# 一律拒绝启动（§10.2）。全部系数可配置（§18.3），M7 参数扫描直接改这里。
version: 1

budget:
  initial: 1.0
  costs:
    # 敏感写 = 改价、发券（§3.3）。分类映射显式列出全部 9 个工具，
    # lint 强制「每工具恰一类」——新增工具忘了分类就该启动失败。
    sensitive_write: 0.15
    normal_write: 0.05
    warn_surcharge: 0.20
    cost_classes:
      sensitive_write: [update_price, create_coupon]
      normal_write: [update_stock, create_order, refund_order, send_email]
      read: [list_products, get_product, get_order]
  thresholds:
    deny_below: 0.0        # budget < 0 → DENY（透支）
    ask_at_or_below: 0.10  # ≤ 0.10 → ASK
    flag_at_or_below: 0.30 # ≤ 0.30 → ALLOW_WITH_FLAG

rules:
  # ① 累积型：同一商品被反复降价，单次合规、累计越界（spec §3.4①）。
  # 阈值语义是「超过」（严格 >）：恰在 -20 不告警，-20.0001 才算。
  - id: cumulative_price_cut
    type: cumulative
    severity: warn
    field: price_delta_pct
    warn_at: -20.0
    deny_at: -35.0
    message: "累计降价超过阈值"

  # ② 序列型：发高折扣券 → 立刻用券下单（spec §3.4②）。
  # 券阈值 50 起步：50~80 区间的券能发出去（语法层卡 80），序列在此触发。
  - id: coupon_self_purchase
    type: sequence
    severity: deny
    steps:
      - {tool: create_coupon, where: "args.discount_pct >= 50"}
      - {tool: create_order, where: "args.coupon_id != null"}
    max_gap: 3
    message: "发高折扣券后随即用券下单"

  # ③ 污点型：读订单（含手机号/地址）→ 外发邮件（spec §3.4③）。
  # sink_condition 用注册的辅助函数 email_domain（受限表达式沙箱内）。
  - id: customer_pii_exfiltration
    type: taint
    severity: deny
    sources: [get_order]
    source_taint: pii
    sinks: [send_email]
    sink_condition: "email_domain(args.to) != 'internal.corp'"
    message: "向外部域名发送含客户信息的邮件"

  # ④ 跨 Agent 型：降价 × 折扣叠加击穿成本线（spec §3.4④）。
  # 数值修正：spec 原稿的 -25% 价会被 M2 单次阈值先拦，改用 -8% + 20% 券，
  # multiplicative = (0.92)(0.80)-1 = -26.4% ≤ -25 触发。两步各自合规。
  - id: price_and_coupon_stack
    type: cross_agent
    severity: deny
    contributors: [pricing_agent, marketing_agent]
    combine:
      - {a: price_delta_pct, b: coupon_rate_delta, op: multiplicative}
    max_combined: -25.0
    message: "降价与折扣叠加击穿成本线"
```

- [x] **Step 4: 写 `policy/combined.py` 的模型部分**——`BudgetConfig` / `CostConfig` / `ThresholdConfig` / `CumulativeRule` / `SequenceStep` / `SequenceRule` / `TaintRule` / `CrossAgentCombine` / `CrossAgentRule`（`Rule = Annotated[Union[...], Field(discriminator="type")]`）/ `CombinedPolicy`。求值器函数留到 Task 5-7。
- [x] **Step 5: 扩展 `policy/lint.py`**——`lint_combined_policy(policy, tools)`：id 唯一；每条规则的 tool 引用存在；cumulative.field ∈ EntityDelta 字段且 warn_at/deny_at 至少一个；sequence.steps 非空、where 可解析且 args 引用 ⊆ 工具 schema、max_gap ≥ 0；taint.sources/sinks 存在、sink_condition 可解析；cross_agent.combine 的 a/b ∈ EntityDelta 字段、op ∈ {add, multiplicative, max}（`custom` → 报「custom 算子 M3 未实现，用 add/multiplicative/max」）、contributors 非空；成本分类恰覆盖全部工具；阶梯阈值单调。
- [x] **Step 6: 扩展 `policy/loader.py` + `config.py`**——`load_combined_policy(path)`；`Settings.combined_policy_path`（env `GUARDRAIL_COMBINED_POLICY_PATH`，默认仓库根 policies/combined_risk.yaml）。
- [x] **Step 7: 绿确认** + `uv run ruff check .`
- [x] **Step 8: Commit** `feat: 组合风险策略文件、模型与启动期自检`

---

### Task 2: 会话状态模型与工具风险声明

**Files:** Modify `models.py`（`EntityDelta` / `ActionRecord` / `SessionState` / `RiskDeltaDecl`；`ToolSpec` 加字段）；Modify `tools/registry.py`（9 个工具的 risk_deltas/taint_categories + 自检扩展）；Modify `policy/expr.py`（注册 `email_domain`）；Test `tests/test_session_state.py`

关键设计（写进代码注释）：
- `SessionState` 只持有 `(Δ, T, A)` + `risk_budget` + `flagged`。agent/task/expires 留在 `SessionRecord`（身份行）——状态 JSON 里重复身份字段是两处真相
- `RiskDeltaDecl(entity_type, entity_arg, field, value_expr)`：value_expr 用**现有受限表达式求值器**算（沙箱免费复用），如 `create_coupon` 的 `"-args.discount_pct"`；实体 key = `f"{entity_type}:{args[entity_arg]}"`
- `taint_categories: list[str]` 只配在 taint_source 工具上（`get_order` → `["pii"]`），自检强制

- [x] **Step 1: 写失败测试**——模型默认值/序列化 roundtrip；`RiskDeltaDecl` 非法 field 拒绝；registry 自检：risk_deltas 引用未声明参数报错、value_expr 引用未知 args 报错、taint_categories 配在非 source 工具报错、9 工具的声明齐全性（update_price/update_stock/create_coupon 有 risk_deltas，get_order 有 taint_categories，其余为空）；`email_domain("a@b.corp")` 等求值用例；`email_domain` 不可被用于逃逸（`email_domain(args.__class__)` 拒绝）。约 16 条。
- [x] **Step 2: 红确认**
- [x] **Step 3: 实现**（models.py 加模型与 ToolSpec 字段；registry.py：

```python
"update_price": ToolSpec(..., risk_deltas=[
    RiskDeltaDecl(entity_type="product", entity_arg="product_id",
                  field="price_delta_pct", value_expr="args.delta_pct")]),
"update_stock": ToolSpec(..., risk_deltas=[
    RiskDeltaDecl(entity_type="product", entity_arg="product_id",
                  field="stock_delta", value_expr="args.delta")]),
"create_coupon": ToolSpec(..., risk_deltas=[
    RiskDeltaDecl(entity_type="coupon", entity_arg="code",
                  field="coupon_rate_delta", value_expr="-args.discount_pct")]),
"get_order": ToolSpec(..., taint_categories=["pii"]),
```

自检新增：`_check_risk_deltas`（entity_arg ∈ schema properties、field 合法、value_expr 可解析且 args ⊆ schema）、`_check_taint`（categories 非空 ⇒ taint_source=True）。expr.py：`_ALLOWED_FUNCS["email_domain"] = lambda s: str(s).rpartition("@")[2].lower()`，注释写明「策略辅助函数是显式注册的纯函数，加一个就要过一次安全审视」。
- [x] **Step 4: 绿确认** + 全量回归 + lint
- [x] **Step 5: Commit** `feat: 会话状态三元组模型与工具风险声明`

---

### Task 3: 状态持久化与乐观锁存储

**Files:** Modify `protocols.py`（SessionStore 扩展）、`stores/sqlite.py`（表结构 + 实现）；Test `tests/test_session_store.py`

```python
class SessionStore(Protocol):
    async def load(self, session_id) -> SessionRecord | None
    async def save(self, record) -> None
    async def sweep_expired(self, now_iso) -> int
    # M3 新增：
    async def load_state(self, session_id) -> tuple[SessionState, int] | None:
        """返回 (状态, 版本)。state_json 为 NULL（新会话）时返回 (全新状态, 0)。"""
    async def save_state(self, session_id, state, expected_version: int) -> bool:
        """CAS：UPDATE ... SET state_json=?, version=version+1 WHERE id=? AND version=?
        返回 False = 版本失配（并发冲突），调用方重载重算。"""
    async def find_by_task(self, task_id) -> list[tuple[SessionRecord, SessionState]]:
        """跨 Agent 合并的唯一入口（§19.1）：返回同一 task_id 下的身份与状态快照。"""
```

- 表变更：`sessions` 加 `state_json TEXT`、`version INTEGER NOT NULL DEFAULT 0`。
  当时计划用 `make clean` 重建；当前实现已接入 `schema_migrations`，升级路径
  以 `src/guardrail/stores/sqlite.py` 和 `deploy/README.md` 为准。
- [x] 测试覆盖：roundtrip；NULL state_json → (fresh, 0)；CAS 成功 version+1；CAS 失配返回 False 且不改数据；find_by_task 只返回同 task 会话、task_id None 的会话不参与；sweep 不受影响。约 10 条
- [x] Commit `feat: 会话状态持久化——单一 state_json + version 乐观锁 + find_by_task`

---

### Task 4: 合并算子

**Files:** Create `policy/merge.py`；Test `tests/test_merge_ops.py`

```python
def merge_entities(a: dict[str, EntityDelta], b: dict[str, EntityDelta]) -> dict[str, EntityDelta]
    # 按 entity_key 并集，字段逐项相加（spec §3.7：单会话内 Δ 可加，跨会话合并同为逐字段加）
def field_scalar(entities: dict[str, EntityDelta], field: str) -> float
    # combine 的输入：该字段在全部实体上的总和（demo 里单实体，精确；
    # 多实体会话是已知简化，记入 limitations）
def apply_combine(op: str, a: float, b: float) -> float
    # add: a+b
    # multiplicative: ((1+a/100)*(1+b/100)-1)*100   ← 百分比语义，-8 与 -20 → -26.4
    # max: max(a, b)（字面最大；负向越界字段请勿用它——lint 不拦，注释+测试说明）
def combine_values(entries, entities) -> float
    # 多条 combine 取最严重（最小值）
```

- [x] 测试：merge 并集相加；multiplicative 精确值（-8,-20 → -26.4；-25,-20 → -40.0 复算 spec 原例）；add(-25,-20) = -45 **误报**、max(-25,-20) = -20 **漏报**（spec §12.1 的两个「用错算子」反例，注释对照真值 -40）；field_scalar 空实体 = 0.0；combine_values 多条取最小。约 12 条
- [x] Commit `feat: 跨会话合并与 combine 算子——multiplicative 语义与用错算子的误报/漏报对照`

---

### Task 5: 累积型与序列型求值器

**Files:** Extend `policy/combined.py`（求值器）；Test `tests/test_combined_risk.py`（本任务先覆盖前两类）

```python
@dataclass 已由 RuleHit/CombinedVerdict 承载：
class RuleHit(BaseModel): rule_id: str; severity: str; message: str
class CombinedVerdict(BaseModel):
    hits: list[RuleHit]; deny: bool; warn_count: int; reasons: list[str]

def evaluate_cumulative(rules, pending: dict[str, EntityDelta], touched: set[str]) -> list[RuleHit]
    # 只对**本次调用声明过增量的实体**（touched keys）检查，且检查的是
    # 已计入当前调用贡献后的值（第 7 次越过线的调用自己要被抓住）：
    # 严格超过才触发（warn_at/deny_at 都是 > 语义，spec 文本「超过」）。
    # 限制 touched 是必须的，不是优化：否则「会话里商品 A 已累计 -25，
    # 此时读一次商品列表」也会对 A 报 warn 并给这次读加 0.20 成本——
    # 一次零成本的读被扣预算，语义完全错误。
def evaluate_sequence(rules, actions, tool, args) -> list[RuleHit]
    # A 尾部 + 当前调用作为候选序列；steps 依序匹配，相邻两步之间隔 ≤ max_gap 个动作；
    # where 用受限表达式对 args 求值，求值异常 = 不匹配（fail-closed 指向 deny 与否？
    # → 不匹配。因为 where 是匹配条件不是安全条件，求值失败按「此步不匹配」处理，
    # 真正的安全兜底在别层。注释写明这个取舍）
def evaluate_combined(policy, state, pending_entities, tool, args, cross_states) -> CombinedVerdict
    # 总入口：四类求值 → hits；deny = 任一 deny 级命中；warn_count = warn 命中数
```

- [x] 测试（cumulative ≥3 正 3 反：-21 warn、-35.5 deny、多实体各自独立、恰在阈值不触发、无规则不触发、stock_delta 不受 price 规则影响；sequence ≥3 正 3 反：gap 内命中、gap 外不命中、where 不满足断链、无券订单不命中、第三步无关动作不打断、当前调用即第二步）。约 14 条
- [x] Commit `feat: 累积型与序列型组合风险求值器`

---

### Task 6: 污点型求值器

**Files:** Extend `policy/combined.py`；Test `tests/test_combined_risk.py`（追加）

- 语义：`state.taint ∩ 规则的 source_taint ≠ ∅` 且当前调用 ∈ sinks 且 sink_condition 对 args 求值为真 → deny。source 的登记发生在**执行后**（commit 段写 `taint |= spec.taint_categories`），所以「读订单 → 下一封外发邮件」命中；同一封调用既非源也非汇不误伤
- [x] 测试 ≥3 正 3 反：污染后外发命中；污染后发内部域不命中；未污染外发不命中；sink_condition 求值异常 → **视为条件成立**（fail-closed：条件守不住就当要外发，注释写明这里与 sequence 的取舍相反——那是匹配条件、这是安全条件）；多污源任一命中；send_email 非污汇场景。约 8 条
- [x] Commit `feat: 污点型组合风险——源登记、汇条件与 fail-closed 求值`

---

### Task 7: 跨 Agent 求值器

**Files:** Extend `policy/combined.py`；Test `tests/test_combined_risk.py`（追加）

- 语义：`task_id` 非空时，`find_by_task` 快照 + 当前调用的 pending Δ 合并（`merge_entities` 逐个折叠）→ `combine_values` → `≤ max_combined` 触发（**含等**：成本线就是底线，达到即击穿，注释对照场景 4 的 -26.4）。contributors 只过滤 agent_id；无 task_id 跳过
- [x] 测试 ≥3 正 3 反：-8+20 券 → -26.4 命中；单边 -8 → 不命中；-5+20 → -24 不命中（恰好线外）；无 task_id 跳过；contributors 外的 agent 不参与合并；快照含自己会话（不重复计自己的已提交 Δ——pending 只算一次）。约 8 条
- [x] **Step 末：跑四类合计** `uv run pytest tests/test_combined_risk.py`，每类 ≥3 正 3 反齐备（spec §12.1 验收）
- [x] Commit `feat: 跨 Agent 组合风险——快照合并与 multiplicative 击穿判定`

---

### Task 8: 预算成本与决策阶梯

**Files:** Create `policy/budget.py`；Test `tests/test_budget.py`

```python
def classify_cost(policy: CombinedPolicy, tool: str) -> float
    # 查 cost_classes；未分类工具 → RuntimeError（lint 已保证，双保险）
def synth_decision(budget: float, cost: float, warn_count: int, cfg) -> BudgetDecision
    # 决策读**扣减前**预算（本计划拍板）；cost = base + warn_count × surcharge
    # 阶梯（自上而下首次命中）：budget < deny_below → DENY
    #                          budget ≤ ask → ASK
    #                          budget ≤ flag → ALLOW_WITH_FLAG
    #                          else → ALLOW
    # 返回 (decision, cost)——cost 由调用方在生效提交时扣
```

- [x] 测试：阶梯四段边界（-0.01/-0.0/0.10/0.1001/0.30/0.31）；warn 附加计算；未分类工具炸；cost 与决策解耦（ASK 时也返回 cost，供批准后扣）。约 12 条
- [x] Commit `feat: 风险预算——成本分类与决策阶梯（扣减前判定）`

---

### Task 9: 会话编排——授权段、生效提交、乐观锁

**Files:** Create `session.py`；Test `tests/test_concurrency.py`（spec §12.1 点名的本项目特有测试）

```python
class Authorization(BaseModel):
    decision: Literal["allow", "allow_with_flag", "ask", "deny"]
    state: SessionState | None      # allow/flag 时携带（供提交段续用）
    version: int
    cost: float
    hits: list[RuleHit]
    reasons: list[str]

async def authorize(store, combined, record, tool, args) -> Authorization:
    # 版本双检循环（最多 2 次）：
    #   state, version = load_state
    #   pending, touched = apply_risk_deltas(spec.risk_deltas, args, state.entities 的内存副本)
    #     （无 risk_deltas 的调用——如读操作——touched 为空集）
    #   verdict = evaluate_combined(..., touched, ...)
    #   deny 命中 → deny（不扣、不改状态）
    #   cost = classify + surcharge
    #   decision = synth(state.risk_budget, ...)   ← 扣减前预算
    #   ask → 返回（状态不动，审批段负责）
    #   allow/flag → **提交前再读一次 version**；变了 → 重载重算（重试一次）
    #                二次失配 → 409 语义（抛 VersionConflict）
    # 理由：只记生效 ⇒ 授权段无状态可写，CAS 落在「决策所依据的快照仍然新鲜」上
async def commit_effect(store, session_id, state, version, spec, args, cost, tool) -> bool
    # 生效提交：Δ 并入（含 last_updated_at）、taint |= categories、A 追加（截断 200）、
    # budget -= cost、flag |= (decision == allow_with_flag)
    # CAS save；失配 → 重载→重放本调用增量→再存（重试一次）；二次失配 → False
    # 调用方（已执行成功）收到 False 时：照常返回结果 + 审计记一条「状态同步降级」
    # —— 商城已写，返回失败是说谎；少扣预算的方向是更宽松，记入 limitations
async def rollback_effect(store, ...) -> None
    # M3 里执行失败发生在 commit 之前 ⇒ 无需退还；保留此桩并为 M4 注释预留
```

- `VersionConflict(Exception)`：授权段二次失配 → API 层映射 409
- [x] 测试（test_concurrency.py，spec 点名）：CAS 底层失配返回 False；并发 `asyncio.gather` 两个 authorize+commit 恰好一升一降、最终预算 = 两次扣减之和且 version +2；授权段版本失配触发重算（monkeypatch 让首次 save 前插入他人写入）；二次失配抛 VersionConflict；A 截断 200；Δ 的 last_updated_at 更新。约 12 条
- [x] Commit `feat: 会话编排——授权段版本双检、生效提交 CAS 与并发重算`

---

### Task 10: 待审批项存储与最小审批 API

**Files:** Modify `protocols.py`（ApprovalStore）、`stores/sqlite.py`（pending_approvals 表）、Create `stores/approvals.py`、`api/approvals.py`；Modify `main.py`、`config.py`；Test `tests/test_approvals.py`

```python
class PendingApproval(BaseModel):
    id: str; session_id: str; agent_id: str; tool: str; args: dict
    reasons: list[str]; created_at: str
    resolved_at: str | None = None; resolution: str | None = None   # approve|reject
    decided_by: str | None = None; comment: str | None = None

class ApprovalStore(Protocol):
    async def create(self, pa) -> PendingApproval
    async def load(self, id) -> PendingApproval | None
    async def resolve(self, id, resolution, decided_by, comment) -> PendingApproval | None
        # UPDATE ... WHERE id=? AND resolved_at IS NULL —— 原子认领，重复 resolve 返回 None
    async def list_open(self, limit=50) -> list[PendingApproval]
```

API：
- `GET /v1/approvals` → 开放中的待审批列表（M5 控制台的数据源，先有接口）
- `POST /v1/approvals/{id}/resolve` `{resolution: "approve"|"reject", decided_by, comment?}`：
  - **approve**：原子认领 → 按存档的 (tool, args) 直接执行（该调用已过全部判定；M4 的 plan_token 机制再加「批准后重校验」）→ `commit_effect`（此时才扣预算）→ 幂等 complete → 审计 `allow`（reasons 含 `审批人:<decided_by> 批准后执行`）→ 返回执行结果。执行失败 → 幂等 release → 审计失败 → 透传商城状态码
  - **reject**：原子认领 → 幂等 release → 审计 `deny`（reasons 含审批人）→ 200。预算不动（只记生效）
  - 已 resolved 再 resolve → 409

- [x] 测试：create/load roundtrip；resolve 原子性（并发 resolve 恰一成功）；approve 后执行并扣预算；reject 后幂等键释放可重试；重复 resolve 409；列表只含未决。约 12 条
- [x] Commit `feat: 待审批项——原子认领、批准后执行与拒绝释放`

---

### Task 11: call_tool 接线

**Files:** Modify `api/tools.py`、`main.py`；Test `tests/test_gateway_api.py`（追加/修正）

`main.py` 的改动：`create_app` 在加载 single_call 策略的同一处加载 combined 策略（`load_combined_policy(resolved.combined_policy_path)`，失败同样 `RuntimeError 拒绝启动`——fail-closed 对两份策略一视同仁），lifespan 里装配 `app.state.combined_policy` 与 `app.state.approvals`。

call_tool 的完整顺序（在 M2 基础上插入第 6′ 段，docstring 同步重写）：

```
1. 会话加载（404）
2. 未知工具（404 + 审计）
3. 幂等快速路径：done → replay（**不扣预算**）
4. M2 单次判定链 → deny → 审计 + 400/403（未到预算段，不扣）
5. 幂等占位 begin() → 409
6′. authorize()：
    - deny（组合规则）→ 审计 deny → release(key) → 403（不扣）
    - ask → create approval → 审计 ask → **202** {decision:"ask", pending_approval_id, reasons}
      （幂等键保持 in_progress：批准后执行走 complete，拒绝走 release）
    - allow/allow_with_flag → 继续（state/version/cost 随行）
7. 审计 allow（先于执行，M2 语义不变）
8. 执行；失败 → release(key) + 审计失败 + 透传（未扣过，无需退还）
9. commit_effect(...)：CAS 生效提交（Δ/T/A/扣减/flag）；False → 审计追加「状态同步降级」
10. provenance 登记、幂等 complete、审计「执行完成」、bus.publish
11. 响应：{"decision": "allow"|"allow_with_flag", "flagged": bool, ...}
```

- [x] 测试：普通写多次 ALLOW 后预算递减（响应体带 reasons）；连续改价到阈值触发 ALLOW_WITH_FLAG（decision/flagged 字段）；再继续触发 ASK → 202 + pending_approval_id，且**未执行**（商城未变）；组合 deny（sequence：发 60% 券→带券下单，中间隔 ≤3 个动作）→ 403 + 规则 id；taint（get_order 后外发）→ 403；ask 后 resolve approve → 商城生效 + 预算扣减；resolve reject → 幂等键释放可重新发起；透支后读操作也被拒（spec 字面：budget<0 → DENY）。约 14 条
- [x] 全量回归（当前测试集必须保持全绿；响应只增键，不删除既有字段）
- [x] Commit `feat: 调用路径接入组合风险与预算——ASK 202、allow_with_flag、生效提交`

---

### Task 12: 端到端验收、场景复现与 spec 同步

**Files:** Modify spec、README

- [x] **Step 1: 全量 + lint**——`make test && uv run ruff check .`
- [x] **Step 2: 场景 2 复现**（`make shop` / `make gateway`，agent=pricing_agent）——连续 10 次 `update_price -5`：
  预期 trace（写入 README）：c1-c4 ALLOW（预算 1.00→0.40）、c5 ALLOW+warn（→0.05）、**c6 ASK(202)**、批准执行后 c7 DENY(透支 -0.30)。断言：第 6 次返回 202；批准后价格累计 -30%；商城只被改 6 次
- [x] **Step 3: 场景 4 复现**（同 task_id 双会话）——pricing 会话 `update_price -8`（放行）；marketing 会话 `create_coupon 20%` → **403 `price_and_coupon_stack`**，理由含「-26.4%」。验证 multiplicative 算出的合计值出现在 reasons
- [x] **Step 4: 并发验收**——同 task 双会话并发各发一次写，`make test` 的 test_concurrency 已覆盖；手工 curl 并发两次观察 version 递增
- [x] **Step 5: spec 同步**（5 处，各写明理由）：
  1. §3.2：SessionState 精简为 (Δ,T,A)+budget+flag，身份字段留在记录行；plan_id 留 M4
  2. §3.3：决策读扣减前预算；「只记生效调用」语义；场景 2 实际第 6 次（warn 附加所致），M7 校准
  3. §3.4④/§13：场景 4 数值修正（-8%/20%/-25），原 -25% 会被 M2 语法层先拦
  4. §3.7：max 算子字面语义（-20 漏报而非 -25）；授权段「版本双检」+ 生效提交 CAS 的两层结构；`custom` 算子 M3 未实现（lint 拒绝）
  5. §15：sessions 表 state_json/version；pending_approvals 增加 agent_id/decided_by/comment
  校验：`grep -c "M3 实现回写" spec` ≥ 5
- [x] **Step 6: README 更新**——能力表加 M3 行；场景 2/4 复现命令；「已知局限」加两条：跨 Agent 快照读的极小概率漏判（§3.7.2）、生效提交二次冲突的状态新鲜度降级
- [x] **Step 7: Commit** `docs: 场景 2/4 验收与 spec 五处回写`
- [x] **Step 8: 收尾**——`feat/m3-session-risk-layer` --no-ff 合入 main

---

## 完成标志

- [x] `make test` 全绿（v1.2.0 交付时为 `640 passed, 1 skipped`）
- [x] `uv run ruff check .` 无错误
- [x] **场景 2 可复现**：连续降价在第 6 次触发 ASK(202)，批准后执行并扣减，透支后全拒
- [x] **场景 4 可复现**：跨 Agent 的降价×折扣被 multiplicative 算出 -26.4% 并 deny，两步各自合规
- [x] 四类规则各 ≥3 正例 + 3 反例（spec §12.1）
- [x] `test_concurrency.py`：并发下版本恰有一次失配并重算（spec §12.1 点名）
- [x] 用错算子的误报/漏报对照测试存在（spec §12.1）
- [x] 全部系数来自 `policies/combined_risk.yaml`（grep 无硬编码 0.15/0.05/0.20/0.10/0.30 于 src/）
- [x] spec 五处回写完成

## 本计划明确不做的

- **计划级审批**（plan_token / 计划生命周期 / 批准后重校验）→ M4。M3 的 approvals 是「单次调用的扣下」，M4 是「整份计划的批准」——表结构与 API 都为后者预留
- **控制台**（预算条 / 计划卡片 / 审计时间线）→ M5。`GET /v1/approvals` 已是它的数据源
- **`custom` 合并算子** → 需要时再加（注册命名函数 = 新的逃逸面，lint 现在显式拒绝）
- **demo Agent 与录屏**（spec §13 的自动化）→ M6。本计划用 curl/pytest 复现场景
- **预算系数校准** → M7（§18.3：当前值是工程直觉，全部可配置）
- **Δ 的多实体聚合精化**：`field_scalar` 按字段求和是已知简化（同字段跨多实体时语义近似），记入 limitations，M7 评测时复核

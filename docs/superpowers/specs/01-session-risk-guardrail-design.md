# 会话级风险护栏 · 系统设计

> 版本：v1.2.0
> 前置调研：[AI Agent 工具调用护栏 · 同类方案调研](../../research/01-agent-guardrail-competitive-analysis.md)
> 状态：核心设计已实现；单节点生产基线已交付，边界见 `limitations.md`

---

## 0. 设计目标

系统关注的是跨调用、跨 Agent 的风险是否能在同一会话里被看见。
单次调用是否合法仍然是前置条件，但不是唯一判据。

### 设计原则

系统不试图成为通用策略平台。它回答一个具体问题：当 Agent 的动作按
顺序发生、彼此又互相影响时，护栏应该在哪里判断。

由此产生四条约束：

1. 判定单位是会话与计划，不只看单次工具调用。
2. 安全判定可复现，LLM 不进入决策路径。
3. 状态变更、审批和失败都留下可验证的证据。
4. 接入不侵入被保护系统，投影由网关侧维护。

---

## 1. 背景与定位

### 1.1 调研后的设计判断

调研显示，多数同类方案在单次调用粒度做判定。OpenAPPA 已覆盖跨调用
数据流，Squidbrake 已覆盖跨步序列，但跨 Agent 合并仍然少见。系统选择
把会话状态和计划生命周期做进判定层，补足这两处缺口。

| 设计缺口 | 现状 | 系统选择 |
|---|---|---|
| 组合风险 | 多数方案不覆盖；少数覆盖数据流或序列，但少见跨 Agent 合并 | 机制一：会话状态与组合风险 |
| 审批单位 | 多数方案按单次调用审批，审批人看不到整件事的终态 | 机制二：计划级审批与终态投影 |

业务语义护栏不单独建设。它作为计划投影的副产品出现：投影执行会自然
产出毛利、价格和库存等业务指标。

### 1.2 项目定位

面向电商场景的**会话级风险护栏网关**，外加一个迷你商城作为受保护对象。

这项设计面向可复现的工程展示。它说明组合风险如何被建模和验证，同时
明确保留单进程 demo 与生产系统之间的边界。

设计取舍优先看三件事：判定是否可复现，状态是否可审计，接入是否不侵入
被保护系统。

### 1.3 目标 / 非目标

**目标**

- 演示并论证「跨调用组合风险检测」可行，且有量化指标
- 演示并论证「计划级审批」可用，且不会退化成审批疲劳
- 可复现：一条命令起环境，一条命令跑全部场景
- 可评测：README 给出拦截率与误伤率的实测数字
- 说明工程边界：接入形态（MCP / LangChain）、可观测性、交付验证。范围见 §19

**非目标（YAGNI）**

- 真实支付、真实电商平台对接：无真实商户通道，只能做适配器骨架；
  硬接是对接方商务问题
- 通用策略语言 / 通用策略引擎：expr 沙箱 + YAML 策略已满足两层域；
  全量 DSL 是独立项目
- 学习型风险模型：LLM 一律不放在决策路径上（§10.2）——确定性护栏的
  立身之本，属设计主张
- 分布式会话存储与多副本：当前只交付单节点、单 worker 的 SQLite 基线；
  外置存储、全局限流和跨副本一致性属于后续路线

与目标的关系说明：控制台鉴权、结构化访问日志、并发安全验证、性能基准
（`make bench`）与 K8s 部署清单（deploy/k8s/）属于单节点生产基线，见 §19.7；
多租户与 RBAC 是后续路线（tenant 维度贯穿 session/策略/预算，当前权限
表即 agent→tool RBAC 的雏形）。

工程范围集中在接入形态、可观测性和交付验证三块，每一项都服务于组合
风险这条主线。排除项见 §19.7。局限说明单独放在 `limitations.md`，避免
把已知边界留到最后才出现。

### 1.4 能力分层：设计重点与基础能力

能力分两层：设计重点解决组合风险和计划审批，基础能力保证系统能进入
真实工作流。基础能力不因为“常见”就不做，缺一项，设计重点也很难被信任。

| 层 | 能力 | 章节 | 同类方案情况 |
|---|---|---|---|
| 设计重点 | 跨调用 / 跨 Agent 组合风险 | §3 | 少数覆盖数据流或序列，跨 Agent 较少 |
| | 计划级审批 | §4 | 多数按单次调用审批 |
| | 策略语义可测（拦截率 / 误伤率） | §12.3 | Rampart 等已开始做回归与 bench |
| 基础能力 | Opaque token / Provenance 门 | §8 | Anthropic 官方蓝图已做 |
| | 哈希链审计 | §7 | Preloop / Squidbrake / TelsonBase / AEGIS 都有 |
| | allow / deny / ask 三态 | §10.1 | 所有人都有 |
| | 幂等 | §6.2 | 所有人都有 |
| | 上限（对结果态计算） | §6.3 | Anthropic 的 caps-against-resulting-state |

设计重点决定系统解决什么问题，基础能力决定它能否被接入和验证。
因此 §3、§4 讲建模与审批，§6 到 §8 保持克制，只实现接入工作流所需
的基线。

---

## 2. 系统组成

网关位于接入层与被保护系统之间。接入层有三种形态，共享同一条判定链：

```
  接入层
  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐
  │ HTTP 客户端  │  │ MCP 客户端   │  │ LangChain    │
  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘
         └─────────────────┼─────────────────┘
                           ▼
                ┌───────────────────────┐
                │ Guardrail Gateway     │
                │ 会话状态 / 风险预算   │
                │ 计划审批 / 投影       │
                │ 策略 / 审计           │
                └───────────┬───────────┘
                            │
                            ▼
                     ┌──────────────┐
                     │ 迷你商城      │
                     │ 或 domain 实现│
                     └──────────────┘
```

信任边界很明确。Agent 不可信，可能被提示注入，也可能只是实现有 bug。
商城或其他受保护系统只接受来自网关的请求，不向 Agent 暴露直接凭证。
网关是唯一有权限改动受保护系统的组件。

### 2.1 组件职责

| 组件 | 职责 | 对外接口 |
|---|---|---|
| 迷你商城 (`shop/`) | 电商域的商品、订单、优惠券状态；提供原子变更接口 | HTTP，仅内网 |
| 护栏网关 (`src/guardrail/`) | 策略判定、会话状态、计划生命周期、审计 | HTTP，对外 |
| MCP 适配器 (`mcp_server.py`) | 把网关工具暴露为 MCP basic server | stdio，见 §19.3 |
| LangChain 工具 (`langchain_tools.py`) | 把网关工具包装为 duck-typed BaseTool | Python 对象，见 §19.3 |
| corp 域 (`domains/corp.py`) | 文件、邮件、论坛、工单、电汇的内存域 | 经网关工具调用 |
| 可观测性 | 决策指标与就绪检查 | `/metrics`、`/readyz` |
| 控制台 (`templates/`) | 人在环审批界面 | HTTP，浏览器 |

接入层可以替换。任何 MCP 客户端、LangChain Agent 或普通 HTTP 客户端
都能走同一条判定链；当前仓库不包含独立的 LLM Agent 实现。

---

## 3. 机制一：会话状态与组合风险

### 3.1 会话

**会话（Session）** 是组合风险的载体。Agent 开始一个任务时向网关申请 `session_id`，之后所有工具调用必须携带它。会话有 TTL（默认 30 分钟）。

跨 Agent 组合风险需要多个会话之间可关联，因此会话额外携带 `agent_id`，并且共享一个可选的 `task_id`——同一业务任务下的多个 Agent 共享它。

### 3.2 会话状态快照

```python
class EntityDelta(BaseModel):
    """对某个实体的累积影响。"""
    entity_key: str                 # 如 "product:123"
    price_delta_pct: float = 0.0    # 累计价格变动百分比
    stock_delta: int = 0            # 累计库存变动
    coupon_rate_delta: float = 0.0
    last_updated_at: datetime

class SessionState(BaseModel):
    session_id: str
    agent_id: str
    task_id: str | None
    started_at: datetime
    expires_at: datetime
    actions: list[ActionRecord]        # 已执行动作序列（截断至最近 200 条）
    entities: dict[str, EntityDelta]   # 按实体聚合的累积影响
    taint: set[str]                    # 污点标记，如 "secret_read", "pii_read"
    risk_budget: float = 1.0           # 剩余风险预算 [0, 1]
    plan_id: str | None = None         # 当前绑定的计划
```

**实现要点**：状态精简为 `(Δ, T, A) + risk_budget + flagged`，持久化为
sessions 表的**单一 `state_json` 列 + `version` 乐观锁**（偏离 §15 的 risk_budget 独立列——
预算在 JSON 内单一事实来源，避免同事务双写两处真相）。agent_id / task_id / expires_at
留在身份行（SessionRecord），不在状态 JSON 里重复；plan_id 属于 M4。

### 3.3 风险预算

这里需要明确一个取舍。

**问题**：组合风险天然要求「跨调用有状态」。如果每个可疑动作都扣住等人批，系统很快会退化成审批疲劳。人点到第 10 次就开始闭眼点了，护栏形同虚设。已有方案在动作粒度审批上普遍遇到这个问题。

**方案**：会话持有**风险预算**，每次调用消耗预算，**预算耗尽才升级为审批**。

```
risk_budget 初始 = 1.0，下限可至负数。

每次调用计算 cost：
  纯只读、无敏感数据            → 0.00
  普通写操作（改库存、建订单）  → 0.05
  敏感写操作（改价、发券）      → 0.15
  触发任一组合风险 warn 规则    → 额外 +0.20

决策（自上而下，首次命中即返回；顺序不可调换）：
  1. budget < 0      → DENY（预算透支）
  2. budget ≤ 0.10   → ASK（扣下等人审批）
  3. budget ≤ 0.30   → ALLOW_WITH_FLAG（放行，会话标记告警）
  4. 其他            → ALLOW（静默放行）
```

效果：正常运营操作消耗接近 0，零摩擦；只有在一个会话里持续做高风险动作时才会刹车。**预算本身就是「这一串做完会怎样」的量化形式。**

**实现要点**：

- **决策读扣减前预算**：阶梯看「这次调用到来时」的预算；扣减发生在生效提交段。
- **只记生效调用**（用户拍板）：执行成功才扣预算、才更新 Δ/T/A；执行失败不记。
  组合 deny 拦下的调用也不扣（deny 规则是独立后盾）。依据：§0「这一串做完会怎样」
  ——预算量化生效的后果，不是尝试。
- **warn 附加成本的触发会提前 ASK**：若序列中 warn 已触发（+0.20/次），ASK 提前
  一到两次出现。场景 2 用逐次微调参数（无 warn）复现时，ASK 恰在第 7 次，与 §13
  一致；含 warn 的序列第 6 次即 ASK。M7 参数扫描统一校准。
- **幂等与连续操作的交互**（§6.2 的推论，场景演示必须知道）：幂等键含 args，
  完全相同的重复调用被重放保护挡下——「拆成 10 次各降 5%」若逐字重复，
  实际是 1 次执行 + 9 次重放。连续降价必须用不同参数（逐次微调幅度/不同商品）
  或新会话表达。这是重试保护与蓄意重复的正确分界，但写 demo 时容易踩。

预算初值、消耗系数全部写在 `policies/combined_risk.yaml` 里，可调可测。

### 3.4 四类组合风险规则

策略即数据（policy-as-data），YAML 定义，网关启动时加载。四类规则对应四种真实的组合风险形态：

**① 累积型（cumulative）** —— 同一实体的同类字段被反复修改，单次合规但累计越界。

```yaml
- id: cumulative_price_cut
  type: cumulative
  severity: warn
  scope: entity
  field: price_delta_pct
  window: session
  warn_at: -20.0     # 累计降价超过 20% 告警
  deny_at: -35.0     # 超过 35% 拒绝
```

对应真实事故：Agent 分 10 次把商品打五折，因为单次降价都在 10% 的政策红线内。

**② 序列型（sequence）** —— 有序动作模式，每步单独看都合法。

```yaml
- id: coupon_self_purchase
  type: sequence
  severity: deny
  scope: session
  steps:
    - {tool: create_coupon, where: "args.discount_pct >= 50"}
    - {tool: create_order,  where: "args.coupon_id != null"}
  max_gap: 3          # 两步之间最多间隔 3 个动作
```

对应真实事故：内部人员先用运营权限发一张 5 折券，再立刻用它给自己下单核销——两步各自都在权限内。序列型规则要演示的正是这类「单看每步都合规」的组合。通用形态（如「关闭备份 → 删除记录」）同理，Squidbrake 的重放集里就有。

**③ 数据流型（taint）** —— 污点从源传播到汇。

```yaml
- id: customer_pii_exfiltration
  type: taint
  severity: deny
  scope: session
  sources: [get_order]                 # 返回值中含客户手机号、收货地址
  sinks: [send_email]
  sink_condition: "args.to_domain != 'internal.corp'"
  # source 一旦出现，其后任何满足 sink_condition 的 sink 都触发
```

对应真实事故：客服 Agent 读订单拿到客户手机号，再用邮件工具把名单发到外部邮箱——两步各自合规，合起来是数据外泄。通用形态（读密钥 → 写临时文件 → 外发请求）同理。

**④ 跨 Agent 型（cross_agent）** —— 多个 Agent 各自合规的动作，合起来击穿底线。

```yaml
- id: price_and_coupon_stack
  type: cross_agent
  severity: deny
  scope: cross_agent        # 按 task_id 关联
  contributors: [pricing_agent, marketing_agent]
  combine:
    - {a: price_delta_pct, b: coupon_rate_delta, op: multiplicative}   # 算子语义见 §3.7
  max_combined: -25.0       # 降价与折扣叠加越过 25% 即击穿成本线
```

对应场景：定价 Agent 降价 8%，营销 Agent 发 20% 券，各自都在单次阈值内，
合起来 `0.92 × 0.80 - 1 = -26.4%`，越过 -25% 成本线。

### 3.5 求值时机

```
单次策略求值（语法层：工具名、参数、权限）
        ↓ 通过
组合风险求值（语义层：当前调用 + 会话状态）
        ↓
预算消耗，决策合成
        ↓
执行
```

顺序不可颠倒：语法层不通过的直接拒掉，不必进入语义层。

求值函数签名：

```python
def evaluate_combined_risk(
    call: ToolCall,
    state: SessionState,
    policy: CombinedRiskPolicy,
    cross_state: list[SessionState] | None,   # 仅跨 Agent 规则需要
) -> CombinedRiskVerdict:
    """返回触发的规则、严重级别、预算消耗、建议决策。"""
```

### 3.6 组合风险的建模：会话 = 三元组

四类规则不是四个特例。它们读的是**同一个会话模型的不同投影**。

**会话被建模为三元组 `(Δ, T, A)`：**

| 分量 | 含义 | 谁来维护 |
|---|---|---|
| `Δ` | 累积增量状态——按实体的**可加量**（价格变动率、库存变动、券折扣率） | 每次调用后由效果声明更新（§4.3） |
| `T` | 污点集合——会话中已接触过的敏感数据类别 | 工具清单声明的 source 命中后写入 |
| `A` | 有序动作序列 | 每次调用追加 |

四类规则各读一个投影：

| 规则类型 | 读取 | 判定形式 |
|---|---|---|
| 累积型 | `Δ` 的单实体分量 | `predicate(Δ[e], call)` |
| 序列型 | `A` 的有序子序列 | `match(A, pattern, max_gap)` |
| 污点型 | `T` × `A` | `T ∩ sources ≠ ∅ ∧ call ∈ sinks ∧ sink_condition(call)` |
| 跨 Agent 型 | `⨁{Δ_i}`，同一 `task_id` 下的合并 | `predicate(combine(Δ₁..Δₙ), call)` |

**为什么这个建模是必要的**：它把「跨调用有状态」从一句口号变成可实现的机制。规则不遍历历史，只读一个**已经物化**的状态；新增一类风险，加的是**一个投影**，不是一套新的历史扫描逻辑。这也解释了 §3.2 的 `SessionState` 为什么恰好是那三个字段——它们就是 `(Δ, T, A)` 的落地。

### 3.7 跨 Agent：合并算子与并发语义

这一节处理跨 Agent 合并的语义与并发边界。

#### 问题一：跨会话的 `Δ` 怎么合并？

单会话内 `Δ` 是可加的——两次降价 5% 就是 -10%。但**跨 Agent 合并不是加法**。设：

- 定价 Agent 降价 8% → `Δ₁.price_delta_pct = -8`
- 营销 Agent 发 20% 券 → `Δ₂.coupon_rate_delta = -20`

实际到手价是 `0.92 × 0.80 = 0.736`，即 -26.4%。它既不是
`-8 + -20 = -28`，也不是取最大值 `-8`。

所以每条跨 Agent 规则必须**显式声明 combine 算子**，不能默认相加：

```yaml
- id: price_and_coupon_stack
  type: cross_agent
  severity: deny
  scope: cross_agent          # 按 task_id 关联
  contributors: [pricing_agent, marketing_agent]
  combine:
    - {a: price_delta_pct, b: coupon_rate_delta, op: multiplicative}
  max_combined: -25.0
```

可用算子：

| 算子 | 语义 | 适用 |
|---|---|---|
| `add` | 直接相加 | 同类量叠加，如两次库存扣减 |
| `multiplicative` | 折扣/比率相乘 | 价格 × 优惠券 |
| `max` | 取最严重的 | 同一维度的多次约束 |
| `custom` | 注册的命名函数 | 领域特有的合成规则 |

算子必须显式声明。用错算子会同时产生漏报和误报：加法把 `-8 / -20`
算成 `-28`，取最大值算成 `-8`，后者会漏掉真实的成本线击穿。

**这一条是「跨 Agent 组合风险」从直觉变成工程的分界线。** 跨 Agent
合并不能只做字段相加，必须把算子语义写进策略，并让并发写入有明确的
冲突处理。这是系统最有辨识度的工程约束之一。

**实现要点**：

- 场景使用定价 -8% 与营销 20% 券，`(0.92)(0.80)-1 = -26.4% ≤ -25`
  触发组合规则。两步各自合规，组合层才看到成本线被击穿。
- **`max` 算子按字面实现** `max(a, b)`：`max(-25, -20) = -20`（不是本节原稿说的
  -25）。对「负值越糟」的字段用它就是漏报——这正是 §12.1 用错算子反例要演示的，
  不替策略作者兜底。
- **单贡献者守卫**：至少 2 个贡献者有非零增量才评估 combine。没有它，一张 60%
  的券会单独把 multiplicative 算成 -60 而误触发——跨 Agent 规则管「叠加」，
  单方越界归累积规则。

#### 问题二：并发怎么办？

场景 4 里两个 Agent 可能**同时**在写。所以：

1. **求值与扣减必须原子**：`加载会话状态 → 求值 → 扣预算 → 落盘` 这一段用乐观锁保护（`sessions.version` 列，见 §15）
2. **跨 Agent 求值读快照**：读同一 `task_id` 下所有会话的 `Δ` 时，允许读「某一时刻的快照」，不要求全局一致。代价是极小概率的漏判，收益是不引入分布式事务。**这个取舍要明确写进 README**
3. **冲突时重试一次**：版本号失配则重新加载并重算；二次失配返回 409，
   让 Agent 重试。这里不采用“尽力而为地放行”，否则会破坏 §10.2 的 fail-closed。
4. **单进程下同样需要**：即使网关是单进程，`asyncio` 并发下同一会话的两个请求仍可能交错。乐观锁不是为多进程预留的，是**现在就需要**

**实现要点**：乐观锁落成两层：

- **授权段**（load → 组合求值 → 预算决策，不写状态）：用**版本双检**——决策前重读
  版本，变了就重载重算；二次失配抛 409。
- **生效提交段**（Δ/T/A/扣减落盘）：标准 CAS；冲突时重载最新状态、重放本调用增量
  （Δ 声明是 args 的纯函数，重放不重复计）、再存。二次失配返回 False：此时商城已写
  成功，向调用方返回失败是说谎——照常响应，降级事实进审计与响应。少记预算的方向
  是更宽松（漏判），不是更严（误伤），记入 limitations。
- `custom` 算子 M3 未实现（lint 显式拒绝）：注册命名函数是一个新的逃逸面，需要时再开。

第 4 点值得强调：很多人以为「单进程 = 没有并发问题」。在这个设计里不成立，因为跨 Agent 场景本身就是并发的，与进程数无关。

---

## 4. 机制二：计划级审批

### 4.1 问题

Agent 说「把夏季款清仓」，背后是 30 个商品改价加 3 张优惠券。按单次调用
逐条审批会把 30 个动作全推给人，审批质量很快下降。系统把这一串动作
聚合为计划，让审批单位跟意图对齐。

**人类看到的是一棵树，批准的是一片叶子。**

### 4.2 计划结构与提交

Agent 在执行任何写操作前，先提交计划：

```python
class PlannedAction(BaseModel):
    step: int
    tool: str
    args: dict[str, Any]

class Plan(BaseModel):
    plan_id: str
    session_id: str
    agent_id: str
    intent: str                          # Agent 自述意图，仅供人参考，不参与任何判定
    actions: list[PlannedAction]

class PlanDecision(BaseModel):
    risk_level: Literal["low", "medium", "high", "rejected"]
    triggered_rules: list[str]
    projected_state: ProjectedState
    reasons: list[str]
```

`intent` 字段只用于展示，不参与判定。它由 Agent 自行填写，可能被注入污染，
因此不能进入策略表达式。

**实现要点**：计划提交走 `POST /v1/plans`（session_id + intent + actions）。
分级四级判定：rejected（任一动作语法层不合规，直接 422 不进审批）/ high（投影触发 deny
或 warn 命中数 ≥ `high_warn_count`）/ medium（warn ≥ 1 或预算低于 `low_budget_floor`）/
low（无规则且预算充足 → 自动签发 plan_token）。全部系数在 `policies/plan_policy.yaml`。

### 4.3 投影执行（Projection）

网关在**影子状态**上按序执行计划的每个动作，得到终态。

```python
class ProjectedState(BaseModel):
    entities: dict[str, EntitySnapshot]   # 受影响的实体终态
    metrics: BusinessMetrics              # 派生业务指标
    triggered_rules: list[str]            # 投影过程中触发的组合风险规则

class BusinessMetrics(BaseModel):
    gross_margin_pct_before: float
    gross_margin_pct_after: float         # ← demo 里最醒目的那个数字
    avg_price_before: float
    avg_price_after: float
    affected_product_count: int
    cash_impact: float
```

**实现约束：投影逻辑全部在网关侧，工具不承担任何配合义务。**

早期设计曾要求每个工具实现一个纯函数 `project()`。该契约已否决——它把护栏与被保护系统耦合在一起：每接入一个新工具都要改工具代码，工具越多契约越重，最终必然劣化。

改为网关侧投影：

- 网关维护一份**影子领域模型**（`projection.py`），覆盖它关心的实体与字段（商品价格、库存、优惠券、订单状态）
- 每种工具在**网关自己的工具清单**里声明一条**效果声明**（effect spec），描述该工具对影子模型做了什么
- 投影 = 按序把效果声明应用到影子模型上

效果声明是网关侧的数据，不是工具的代码：

```yaml
- tool: update_price
  effect:
    - target: "product:{args.product_id}"
      field: price_delta_pct
      op: add
      value: "{args.delta_pct}"
```

代价是网关必须知道自己保护的是什么领域——这对本项目完全成立（就是电商）。收益是**接入成本为零**：工具、乃至第三方的 MCP server，都不需要为护栏做任何改动。这正是 §19.3 的 MCP 适配器得以成立的前提。

投影与真实执行必须一致。测试方式：同一组动作，先投影、后真实执行，断言终态相等（property test，见 §12.2）。

**实现要点**：计划级组合风险用**终态语义**——把计划各步增量叠到
会话状态副本（不改库），对四类规则求值「计划执行完之后是否越界」。途中逐步告警会造成
N 次审批，正是 §4.1 要消灭的东西。逐步预检只做语法层（白名单/schema/权限/单次阈值；
provenance 对 `preview-` 占位 id 跳过、单步 caps 被终态投影覆盖）。

### 4.4 风险分级与审批路径

```
提交计划
   ↓
① 逐动作单次策略预检 —— 任一动作语法层不合规 → rejected（直接拒绝，不进审批）
   ↓
② 影子执行 → ProjectedState
   ↓
③ 组合风险对投影求值 —— 包含"计划执行完之后是否越界"这类只有投影才能发现的规则
   ↓
④ 分级：
   low      —— 无规则触发，且会话预算充足   → 自动批准，直接签发 plan_token
   medium   —— 触发 warn 级规则              → 需审批，控制台黄色
   high     —— 触发 deny 级规则或多条 warn   → 需审批，控制台红色
   rejected —— 单次策略不合规                 → 直接拒绝
```

### 4.5 批准后的执行约束

批准之后**不能盲信**。`plan_token` 里存的是动作哈希，不是「任意授权」。

```python
class PlanToken(BaseModel):
    plan_id: str
    session_id: str
    action_hashes: list[str]   # sha256(tool + canonical_json(args))，按序
    exp: datetime              # 默认 TTL 15 分钟
```

Agent 执行每一步时带 `plan_token`，网关校验三件事：

1. **token 有效**：签名正确、未过期、plan 状态为 approved
2. **动作在计划内**：当前 `(tool, args)` 的哈希必须命中 `action_hashes` 中某个尚未消费的条目
3. **重新求值**：**再跑一次单次策略与组合风险**——因为批准之后会话状态可能已经变了（比如别的 Agent 也改了同一个商品）

三项全过才执行。任何一个参数被篡改，哈希失配，拒绝。

这里的分界是：批准的是「这一个计划」，不是「这个 Agent」。如果执行时
只校验身份，不校验动作，审批机制就失去意义。

**实现要点**：

- **哈希算法改为 `sha256(canonical_json({tool, args}))`**：原式 `tool ‖ json` 拼接有
  分隔符歧义（与 §6.2 幂等键同一修正）。
- **消费记账**：token 无状态、消费有状态——plans 行存 `executed_hashes_json`，
  CAS 消费（乐观锁同 §3.7）。消费放在**执行成功之后**：失败步不占坑、可重试；
  并发同参步骤由幂等键挡下，不会双执行。
- **预算不豁免**：计划步骤执行照常走 authorize + commit——批准的是这一个计划，
  不是预算通行证；会话状态在批准后可能已变。
- **重新求值天然成立**：计划步骤走的就是同一条判定链（单次策略 + authorize），
  用执行时刻的会话状态。

### 4.6 计划生命周期

```
pending ──approve──▶ approved ──全部执行──▶ completed
   │                    │
   │                    ├──部分执行失败──▶ partially_applied（不回滚，标红）
   │                    └──超 TTL 未完成──▶ expired
   └──reject──────────▶ rejected
```

- **不自动回滚**：真实电商系统也不会因为第 17 步失败就把前 16 步回滚。网关记录已执行列表，控制台标红，把处置权交给人。
- **超时失效**：`approved` 后 TTL 到期仍有未执行动作 → `expired`，后续调用被拒。

**实现要点**：

- **执行入口复用 `POST /v1/tools/{name}`**：body 带可选 `plan_id`，不带走单调用路径。
  网关校验三件事（token 有效 / 哈希在计划内且未消费 / 重新求值），响应形状与单调用一致。
- **过期惰性判定**：不引入定时器——执行时发现 exp 已过即置 expired 并拒绝。
- **与幂等的交互**：同一计划内逐字相同的步骤（同 tool+args）会被幂等重放挡下，
  实际只执行一次。连续同类操作必须在计划里用不同参数表达（同 §6.2 回写）。

---

## 5. 三条请求路径

### 路径 1：单次调用（无计划）

```
Agent → POST /v1/tools/{tool_name}   {session_id, args}

  1. 加载 SessionState（不存在或过期 → 拒绝）
  2. Provenance 校验：写操作的实体 ID 必须由本会话的工具返回过
  3. 单次策略求值            → allow / deny / ask
  4. 组合风险求值            → 更新预算，产出 reasons
  5. 决策合成
  6. 若已绑定计划，校验动作哈希是否命中且尚未消费
  7. 写审计链（**先于执行**；写入失败则取消执行，见 §7）
  8. 执行 / 扣下 / 拒绝
  9. 更新 SessionState
 10. 返回 {decision, result, risk_budget_remaining, reasons}
```

### 路径 2：提交计划

```
Agent → POST /v1/plans   {session_id, intent, actions[]}

  1. 加载 SessionState
  2. 逐动作单次策略预检
  3. 影子执行 → ProjectedState
  4. 组合风险对投影求值
  5. 分级
  6. low  → 直接签发 plan_token，返回 {plan_id, status: approved, token}
     其他 → 存为 pending，返回 {plan_id, status: pending, decision: PlanDecision}
```

### 路径 3：人工审批

```
人 → POST /v1/plans/{plan_id}/approve    （控制台 htmx 触发）
人 → POST /v1/plans/{plan_id}/reject     {comment}

  1. 校验计划仍为 pending 且未过期
  2. 记录审批人、时间、意见 → 审计链
  3. approve → 签发 plan_token
```

---

## 6. 单次策略层（基础能力）

这一层不是本设计的重点，但它是组合风险层能够工作的前提。

### 6.1 判定顺序

```
1. 会话有效性（存在、未过期）
2. 工具白名单
3. Provenance：写操作的实体 ID 是否为本会话工具返回过的（§8）
4. 参数 JSON Schema 校验
5. 权限规则（agent_id 是否有权调该工具）
6. 单次阈值 —— 语法层，读参数即可，如「单次降价 ≤ 10%」
7. 结果态上限 —— 结果层，需投影，见 §6.3
```

幂等不在这条链上——见下方说明。

**与初稿的三处偏离**：

1. **Provenance 与工具白名单互换**。初稿把 Provenance 放在第 2 步、白名单第 3 步，但 Provenance 校验要读工具自己声明的 `requires`（§8）——白名单都还没过就没有 `requires` 可读。实现改为先过白名单。
2. **「未绑定其他计划」暂不检查**。它需要 `plan_id`，属于计划生命周期（M4）。M2 阶段该项为空，实现里没有对应代码。
3. **幂等被拆到执行前后两步**。初稿把它列为第 8 步，但它必须横跨「审计 → 执行 → 记账」，塞不进一条纯前置的链。实现拆成：
   - **快速路径**（执行前）：幂等键已完成 → 原样返回首次响应，不再判定、不再执行；
   - **占位登记**（执行前，判定通过后）：用一次条件写把并发重放挡在 409。

   顺序上，快速路径在判定链**之前**——为一个已知会重放的调用白跑一遍投影（含商城往返）是不必要的。代价是：一次重放不会重新走单次策略。这是可接受的，因为重放的前提就是「首次已经通过判定」，而策略在会话生命周期内不变。

   **接 M3 风险预算时的硬约束**：预算扣减必须放在占位登记**之后**、重放分支**之前**。放错位置会导致一次重放扣两次预算。

策略格式：

```yaml
- id: max_single_price_cut
  match: {tool: update_price}
  deny_if: "abs(args.delta_pct) > 10"
  message: "单次降价不得超过 10%"
```

表达式用受限的 Python `eval`（白名单 AST 节点，禁用 `__` 与属性访问），**不引入 CEL 依赖**——这是 demo，不需要通用表达式引擎。

### 6.2 幂等

幂等键 = `sha256(session_id ‖ tool ‖ canonical_json(args))`。

**为什么必须做，不只是工程健壮性**：Agent 会在超时、重试、网络抖动时重复发出同一个调用。没有幂等，一次「降价 5%」的重试就变成「降价 10%」——而这**恰好绕过单次阈值**，因为策略看到的是两个各自合规的 5%。所以幂等是单次阈值能成立的前提，不是可选项。

行为：

| 命中情况 | 响应 |
|---|---|
| 幂等键已完成 | 返回**首次的响应体原样**；不重新执行、不重复扣预算；审计链记一条 `replay: true` 的记录 |
| 幂等键进行中 | 返回 409，调用方等待后重试 |
| 未命中 | 正常执行，完成后登记 |

幂等窗口 = 会话 TTL（与 §3.1 一致，不做独立的窗口配置）。

**两处实现细节**：

1. **`decision: replay` 改为独立的 `replay: true` 字段**。初稿写的是「审计链记一条 `decision: replay`」，但 `decision` 会被 `/metrics` 按值计数（§19.4），掺进一个非判定值会污染那个指标。所以 `decision` 仍为 `allow`，重放标记单独成列。
2. **幂等键算法改为 `sha256(canonical_json({session_id, tool, args}))`**。初稿写的是 `sha256(session_id ‖ tool ‖ canonical_json(args))`——字段直接拼接会碰撞：`("ab", "c")` 与 `("a", "bc")` 拼出同一个字符串，于是两个不同的调用共用一个幂等键，后者的响应会被原样返回给前者。语义完全一致，消掉了分隔符歧义。

   键必须用 **Agent 提交的原始参数**，不能用服务端派生之后的版本：派生结果依赖当时的商城状态，同一个意图在两次调用里会算出不同的 `absolute_delta_cents`，键就永远对不上。

### 6.3 上限：对结果态计算，不信模型宣称值

结果态上限参考 Anthropic 公开蓝图中的 `caps validated against resulting state`。

```yaml
- id: max_order_amount
  match: {tool: create_order}
  cap_field: total_amount
  max: 50000
  compute_from: resulting_state     # 而非 args
```

**错误做法**：读模型传来的 `args.total_amount` 做比较——模型（或被注入的模型）可以谎报。
**正确做法**：复用 §4.3 的同一套影子模型，先投影出这笔订单的结果态，从结果态里读金额。

代价是这类上限校验比读参数贵，收益是它不采用模型自报值。实现上复用了
机制二的投影能力，也是网关侧投影的第二个收益。

**哪些约束需要投影**：只有「金额 / 总量 / 终态」这类**结果层**约束需要。诸如「单次降价 ≤ 10%」是**语法层**约束，读参数即可，不必投影。不要把所有规则都升级成投影求值，那会让 §6.1 的判定链无谓变重。

**实现要点**：

- `cap_field` 定为 `total_amount_cents`（不是 `total_amount`），`max: 50000` 的单位是**整数分** = 500.00 元，与 §15 的金额约束一致。
- **本项目只实现这一条结果层规则**（`policies/single_call.yaml` 的 `cap_order_amount`）。这正是上面那句告诫的直接结果——每个结果层规则都要付一次「装载影子状态 + 投影」的代价。其余上限（`create_coupon` 的折扣率等）留在语法层。
- 可读字段是一个**闭集**（`policy/lint.py` 的 `CAP_FIELDS`），新增字段必须同时给出「从哪个快照、怎么算」。刻意不让策略文件自己写表达式——那等于把 §6.1 的表达式沙箱开一个口子在配置层。

---

## 7. 审计链（基础能力）

哈希链，每条记录包含前一条的哈希：

```python
class AuditEntry(BaseModel):
    seq: int
    prev_hash: str
    entry_hash: str      # sha256(prev_hash + canonical_json(payload))
    session_id: str
    plan_id: str | None
    tool: str
    args: dict
    decision: Literal["allow", "allow_with_flag", "ask", "deny"]
    reasons: list[str]
    timestamp: datetime
```

**顺序保证**：审计写入**先于**工具执行。审计写失败则执行取消。这样不存在「执行了但没记上」的窗口。

**一次放行会写两条记录**——一条「批准并即将执行」，一条「执行完成」或「执行失败：<状态码>」。这不是重复，而是上面那条保证的必然结果：审计必须先于执行（这样不存在「执行了但没记上」的窗口），而执行结果只有执行后才知道。链是只追加的，所以补第二条而不是改第一条。拒绝的调用只写一条（没有「即将执行」可言）。

**`replay` 单独成列**，不在 `decision` 里。理由见 §6.2。

**验收测试**：篡改任意**历史**条目的 payload，链校验必须失败。

**已知边界（M2 实测确认）**：删掉或伪造**最后一条**记录，`verify_chain()` 会返回 `ok` —— 剩下的前缀本身自洽。要发现尾部截断需要一个本设计刻意不引入的东西：把链头哈希写到别处（WORM 存储、外部日志，或定期打快照）。这正是上面那句「只证明『没被改』，不证明『写的是真的』」的一个具体实例。`tests/test_audit_chain.py::test_truncating_the_tail_is_not_detectable_without_anchor` 把这条边界钉死。中间条目的删除与篡改都能检出（靠 `seq` 连续性检查 + `prev_hash` 悬空）。

不做 Ed25519 签名——AEGIS 在这一层已做得更好，且对 demo 无增益。但要在 README 里明确说明：这一层只证明「没被改」，不证明「写的是真的」。这是从调研里学到的诚实态度。

---

## 8. Provenance 门（基础能力）

写操作只接受**本会话的工具返回过的实体 ID**。

对抗的是间接提示注入：被污染的模型无法凭空编造一个真实系统中存在的商品 ID 去改价，因为它必须先从本会话读到过。

实现：工具在**网关侧的清单**（`tools/registry.py`）里声明两条信息，登记与校验都按声明走：

- `emits`：执行成功后从返回值里登记哪些实体。路径语法 `coupon.id` / `products[].id` / `order_id`。
- `requires`：执行前必须已被本会话读到过哪些实体。`coupon_id` 这类可选依赖标 `optional: true`。

**实现要点**：初稿写的是「工具返回值里出现的实体 ID **自动登记**」，实现改为**声明式路径**。理由：递归扫描返回值会把商品名、邮箱域名、订单备注里恰好长得像 ID 的字符串也登记成实体，那道门就形同虚设了。声明式路径把「什么算一个实体」变成网关侧的可审计数据，与效果声明（§4.3）是同一个立场。

代价是接入新工具要手写 `emits` / `requires`。这项成本换来了可读的实体
边界，也让它能在 `assert_specs_valid()` 中静态检查。

参数缺失或类型不对时 `requires` 校验一律跳过：那些情况由 §6.1 第 4 步的参数契约报错，那条消息会点名工具与参数，对 Agent 更可读。「参数畸形」与「实体没读过」本来就是两件不同的事，不该混成一条消息。

---

## 9. 控制台设计

Tailwind CDN + htmx，服务端渲染，无构建步骤。

### 9.1 页面

| 路径 | 内容 |
|---|---|
| `/console` | 会话列表（含实时风险预算条） |
| `/console/sessions/{id}` | 会话详情：预算条 + 计划卡片 + 审计时间线 |
| `/console/plans/{id}` | 计划详情 + 批准/拒绝 |

### 9.2 控制台组件

**① 风险预算条** —— 会话页顶部常驻，每次调用消耗一格，绿→黄→红渐变。
这是「组合风险」最直观的界面表达，也让审批人能看到会话是在变安全还是
在接近风险线。

**② 计划卡片** —— 把多步动作折叠成一张卡。左侧可展开动作列表，右侧显示终态对比：

```
                    当前          计划执行后
毛利率              32.4%    →     -12.1%    ← 红色高亮
平均售价            ¥299     →     ¥179
受影响商品数          0      →      30
现金影响              —      →  -¥48,200

触发规则：cumulative_price_cut（累计降价 -40%，超 deny 阈值 -35%）
```

**③ 审计时间线** —— 垂直时间线，每步显示工具名、决策、哈希前 8 位和时间戳。点击展开完整 payload。

### 9.3 demo 模式

`python -m guardrail demo` 启动控制台后，页面提供场景运行入口，顺序播放 §13 的 4 个场景并展示结果。

---

## 10. 决策语义与失败语义

### 10.1 决策三态

| 决策 | 语义 | 行为 |
|---|---|---|
| `allow` | 合规 | 直接执行 |
| `ask` | 需人工 | 扣下，生成待审批项，返回 `pending_approval_id` |
| `deny` | 违规 | 拒绝，返回原因 |

`allow_with_flag` 是 `allow` 的子状态，仅用于会话标记，不改变行为。

### 10.2 失败语义：fail-closed

| 情况 | 决策 |
|---|---|
| 未匹配任何策略规则 | **拒绝** |
| 策略文件缺失或解析失败 | **拒绝**（进程启动即失败，不进入服务状态） |
| 组合风险求值抛异常 | **拒绝** |
| 商城服务不可达 | **拒绝** |
| 审计链写入失败 | **拒绝** |
| 投影执行超时 | **拒绝** |

系统选择 fail-closed，理由有三条：

1. 漏配规则时继续放行，会把配置错误直接变成安全洞。
2. 当前实现面向演示与验证，不追求高可用。
3. 失败语义需要明确，含糊处理比选择更危险。

LLM 不进入决策路径。Rampart 在调研中明确解释过这一点：

> "deterministic enforcement outside the model that doesn't care if the LLM is being prompt-injected"

用 LLM 判断 LLM 是否被注入会形成循环。当前系统的判定全部是确定性的。

---

## 11. 技术栈

| 层 | 选型 | 理由 |
|---|---|---|
| 语言 | Python 3.11+ | — |
| Web | FastAPI + Pydantic v2 | 明确的请求模型与 OpenAPI 支持 |
| 存储 | SQLite + aiosqlite（经存储协议，§19.1） | 单文件，易于演示；协议层保留替换空间 |
| 模板 | Jinja2 | FastAPI 原生支持 |
| 前端 | Tailwind CDN + htmx | 无构建步骤，适合单页控制台 |
| HTTP 调用 | httpx | 网关与商城、适配器共用 |
| 策略 | PyYAML + jsonschema + 受限表达式求值 | 策略即数据，避免引入通用表达式引擎 |
| CLI | argparse | 当前命令量小，标准库足够 |
| 接入 | MCP stdio + LangChain duck-typed 适配器 | 不引入硬依赖 |
| 可观测 | `/healthz` + `/metrics` + `/readyz` + JSON 日志 | 决策计数、依赖就绪和容器日志解析；OTel 尚未接入 |
| 交付 | Docker Compose + K8s + GitHub Actions | 本地复现、单节点生产部署与 CI 门禁 |
| 依赖管理 | uv | 锁定依赖与构建环境 |
| 测试 | pytest + pytest-asyncio + hypothesis | 覆盖并发、投影一致性和语料门槛 |
| lint | ruff | 全仓静态检查 |

当前不引入 Redis、Postgres、通用策略引擎、前端框架或 LLM SDK。K8s 清单
用于单节点生产基线；Redis / Postgres 只在多副本扩展路径中作为可能实现出现。

---

## 12. 测试与评测

测试同时覆盖策略回归、策略语义和失败路径。只测“规则改了会不会误伤”
还不够，系统还需要说明这些规则覆盖了哪些威胁。

### 12.1 单元测试

- `test_single_policy.py`：单次规则正反例
- `test_combined_risk.py`：四类组合风险
- `test_merge_ops.py`：合并算子与用错算子的反例
- `test_projection.py`、`test_projection_consistency.py`：投影正确性与一致性
- `test_plan_api.py`、`test_plan_token.py`：计划生命周期、审批和 token 校验
- `test_idempotency.py`：已完成、进行中、未命中
- `test_concurrency.py`：乐观锁与并发写入
- `test_audit_chain.py`：哈希链与篡改检测
- `test_auth.py`：身份绑定与审批权限
- `test_bench_corp_tools.py`：第二领域工具、策略和 Bench-Corp 门槛

`test_merge_ops.py` 和 `test_concurrency.py` 覆盖跨 Agent 合并与并发语义，
它们直接检验系统最核心的建模假设。

### 12.2 投影一致性（property test）

同一组动作序列，分别在影子状态和真实商城上执行，断言终态完全相等。用 hypothesis 生成随机动作序列。

投影错误会让审批人看到错误终态，因此它是计划审批的根基。

### 12.3 对抗语料与基线对照

#### 语料来源与标注

当前语料由两部分组成：电商场景的合成语料，以及从 Bench-Corp 场景语义
转译的 corp 语料。每条 case 都标注来源，避免把自编用例当成外部验证。

| 语料集 | 来源 | 规模 | 期望 |
|---|---|---|---|
| 电商对抗集 | 自编，标注 `synthetic: true` | 20 组 | 拦截率 ≥ 90% |
| 电商正常集 | 自编常见运营序列 | 20 组以上 | 误伤率 ≤ 5% |
| corp 语料 | 改写自 Bench-Corp 场景语义 | 10 组 | 第二领域对照 |
| Bench-Corp 转译 | 14 个完整转译、1 个仅 attack、5 个 skipped | 29 case | primary 拦截率 ≥ 90%、误伤率 ≤ 5% |

真实事故重放仍是后续工作。报告会把“自编”“转译”“跳过”分开写，
不把不同来源的语料混成一个数字。

#### 三方基线对比

同一套语料跑三个系统，用于观察组合风险层是否真的改变结果：

| 系统 | 说明 |
|---|---|
| **无护栏** | 全部放行，作为下界参照 |
| **单次判定** | 只做单次策略，不做会话状态 |
| **本项目** | 单次策略 + 组合风险 + 计划级审批 |

实测结果（`make eval`，语料见 `eval/`）：

| 系统 | 对抗集拦截率 | 正常集误伤率 |
|---|---|---|
| 无护栏 | 10% | 14%* |
| 单次判定（基线） | 25% | 14%* |
| 本项目 | 100% | 0% |

`*` 无护栏的误伤来自商城自身业务校验，不是护栏。

第二领域（文件/邮件，语料改写自 Bench-Corp）中，无护栏与单次判定均为
0%，本项目为 100%/0%（`make eval-corp`）。两域语料构成不同，拦截率
不应跨域直接比较。

Bench-Corp 全量处置结果见 `make eval-bench`：primary 28 case 为 100%/0%，
3 个已知误伤单独列出，5 个 skipped 场景记录机制缺口。

#### 误伤率同样重要

只统计漏拦会把系统推向过度拦截。正常语料的误伤率需要和拦截率一起看，
否则“更安全”的代价会被隐藏起来。

README 同时列出两个指标和语料路径，任何读者都可以复现或替换语料重跑。

### 12.4 一条命令

```
make demo       # 起商城 + 网关 + 控制台，打开浏览器
make scenarios  # 顺序跑完 4 个录屏场景
make eval       # 跑三方基线对比（无护栏 / 单次判定 / 本项目），打印拦截率与误伤率表
make test       # 全部测试
make lint       # ruff + 策略 lint（引用不存在的工具之类的静态错误）
make verify     # 校验审计链完整性
make up         # docker compose up，容器化版本
```

**实现要点**：

- 上表全部兑现。补充：`make sweep`（系数网格扫描，§18.3）与
  `python -m guardrail policy-lint`（策略 lint 独立成 CLI，CI 可用）。
- **make lint 的策略半边**：三份策略可加载 + 权限表/规则/cost_classes 引用的
  工具名全部存在于注册表。语义正确性不属于 lint——那是评测（§12.3）的事。
- **§12.3 语料落地规模**：对抗 20 组 + 正常 22 组（spec 期望各 ≥20），
  全部标注 `synthetic: true`；实测三方对比：无护栏 10%/14%、单次判定 25%/14%、
  本项目 100%/0%（数字随语料变化，复现见 `make eval`）。
- **§9.3 demo 选择器**：`/console?demo=1` 顺序播放 4 场景并渲染通过/失败结果。
  偏离：播放为同步执行 + 结果渲染，没有「逐步骤实时高亮」——录屏用
  `make scenarios` 的终端逐步输出替代，够用且省一套前端状态机。
- **§19.3 MCP 范围**：stdio JSON-RPC 的 initialize / tools/list / tools/call
  基础子集；resources / prompts / 采样未实现（见 limitations #14）。

---

## 13. 演示场景

| # | 场景 | 演示的能力 | 预期结果 |
|---|---|---|---|
| 1 | Agent 试图把 iPhone 降价 90% | 单次策略 | 返回 403，商城未被修改 |
| 2 | 同一会话连续降价，第 7 次越过预算线 | 风险预算与决策阶梯 | 前六次 allow / flag，第 7 次返回 202 并进入审批 |
| 3 | 运营 Agent 提交 5 步调价计划 | 计划级审批 | 终态越线进入 high，审批后凭 token 逐步执行 |
| 4 | 定价 Agent 降价 8%，营销 Agent 发 20% 券 | 跨 Agent 组合风险 | 营销侧被 `price_and_coupon_stack` 拦截，合计 -26.4% |

四个场景都走真实网关路径。命令 `make scenarios` 的输出就是验收结果。

---

## 14. 目录结构

```
agent-guardrail/
├── README.md
├── pyproject.toml
├── Makefile
├── Dockerfile
├── docker-compose.yml
├── .github/workflows/ci.yml
├── docs/
│   ├── limitations.md
│   ├── owasp-mapping.md
│   ├── research/
│   │   └── 01-agent-guardrail-competitive-analysis.md
│   └── superpowers/specs/
│       └── 01-session-risk-guardrail-design.md
├── policies/
│   ├── single_call.yaml
│   ├── combined_risk.yaml
│   ├── corp_single_call.yaml
│   └── corp_combined_risk.yaml
├── eval/
│   ├── cases_adversarial.yaml
│   ├── cases_normal.yaml
│   ├── cases_corp.yaml
│   └── cases_bench_corp.yaml
├── src/guardrail/
│   ├── main.py
│   ├── __main__.py
│   ├── demo.py
│   ├── evaluation.py
│   ├── config.py
│   ├── models.py
│   ├── protocols.py
│   ├── plans.py
│   ├── projection.py
│   ├── policy/
│   │   ├── engine.py
│   │   ├── single.py
│   │   ├── combined.py
│   │   ├── merge.py
│   │   └── lint.py
│   ├── tools/
│   │   ├── registry.py
│   │   └── handlers.py
│   ├── domains/
│   │   └── corp.py
│   ├── stores/
│   │   ├── sqlite.py
│   │   ├── audit.py
│   │   ├── approvals.py
│   │   ├── idempotency.py
│   │   ├── plans.py
│   │   └── provenance.py
│   ├── api/
│   │   ├── tools.py
│   │   ├── plans_api.py
│   │   ├── sessions.py
│   │   ├── approvals.py
│   │   ├── auth.py
│   │   ├── console.py
│   │   ├── audit.py
│   │   └── obs.py
│   ├── mcp_server.py
│   ├── langchain_tools.py
│   └── templates/
│       ├── base.html
│       ├── sessions.html
│       ├── session_detail.html
│       └── plan_detail.html
├── shop/
│   ├── main.py
│   └── store.py
└── tests/
```

---

## 15. 数据模型

```sql
CREATE TABLE products (
  id TEXT PRIMARY KEY, name TEXT, category TEXT,
  cost_price_cents INTEGER, list_price_cents INTEGER, stock INTEGER
);

CREATE TABLE orders (
  id TEXT PRIMARY KEY, product_id TEXT, qty INTEGER,
  unit_price_cents INTEGER, coupon_id TEXT, status TEXT, created_at TEXT
);

CREATE TABLE coupons (
  id TEXT PRIMARY KEY, code TEXT UNIQUE, discount_pct REAL,
  max_uses INTEGER, used INTEGER, expires_at TEXT
);

CREATE TABLE sessions (
  id TEXT PRIMARY KEY, agent_id TEXT, task_id TEXT,
  state_json TEXT, risk_budget REAL,
  version INTEGER NOT NULL DEFAULT 0,   -- 乐观锁，见 §3.7
  created_at TEXT NOT NULL, expires_at TEXT NOT NULL   -- expires_at 判定链第 1 步读它
);
CREATE INDEX idx_sessions_task ON sessions(task_id);   -- 跨 Agent 合并的查询入口
CREATE INDEX idx_sessions_expires ON sessions(expires_at);  -- 会话清理的扫描入口
-- 实现说明：risk_budget 不设独立列（在 state_json 内，单一事实来源）；
-- find_by_task 返回 (身份行, 状态) 对——contributors 过滤需要 agent_id。

CREATE TABLE pending_approvals (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL, agent_id TEXT NOT NULL,
  tool TEXT NOT NULL, args_json TEXT NOT NULL,
  reasons_json TEXT NOT NULL,
  cost REAL NOT NULL,                    -- authorize 时算好存档，批准后按此扣
  created_at TEXT NOT NULL,
  resolved_at TEXT, resolution TEXT,     -- approve | reject；原子认领靠 resolved_at IS NULL
  decided_by TEXT, comment TEXT
);
CREATE INDEX idx_approvals_open ON pending_approvals(resolved_at);

CREATE TABLE idempotency (
  key TEXT PRIMARY KEY,                 -- sha256(session_id‖tool‖canonical_json(args))，见 §6.2
  session_id TEXT, status TEXT,         -- status: in_progress | done
  response_json TEXT, created_at TEXT
);

CREATE TABLE plans (
  id TEXT PRIMARY KEY, session_id TEXT, agent_id TEXT,
  intent TEXT, actions_json TEXT, projected_state_json TEXT,
  risk_level TEXT, risk_reasons_json TEXT, status TEXT,
  created_at TEXT, decided_at TEXT, decided_by TEXT, comment TEXT
);

CREATE TABLE audit_log (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  prev_hash TEXT NOT NULL, entry_hash TEXT NOT NULL,
  session_id TEXT NOT NULL, plan_id TEXT, tool TEXT NOT NULL, args_json TEXT NOT NULL,
  -- decision 只有四个判定值，不含 replay：重放单独成列，见 §6.2
  decision TEXT NOT NULL,
  replay INTEGER NOT NULL DEFAULT 0,
  reasons_json TEXT NOT NULL, timestamp TEXT NOT NULL
);

CREATE TABLE provenance (
  session_id TEXT, entity_type TEXT, entity_id TEXT, issued_at TEXT,
  PRIMARY KEY (session_id, entity_type, entity_id)
);

CREATE TABLE pending_approvals (
  id TEXT PRIMARY KEY, session_id TEXT, tool TEXT, args_json TEXT,
  reasons_json TEXT, created_at TEXT, resolved_at TEXT, resolution TEXT
);
```

金额一律以**整数分**存储，禁止用浮点。理由：§6.3 的金额上限在边界上依赖精确比较，
浮点误差会让上限时灵时不灵。

时间戳统一为 ISO 8601 文本，如 `2026-10-03T14:22:05+08:00`。

---

## 16. 工具清单（16 个）

当前注册 16 个工具：电商域 9 个，corp 域 7 个。`send_email` 注册在
电商域，但 corp 策略也会用它作为外发汇点。效果声明集中在
`tools/registry.py` 和 `domains/corp.py`，handler 本身不写护栏逻辑。

| 域 | 工具 | 类型 | 风险角色 |
|---|---|---|---|
| 电商 | `list_products` | 读 | — |
| 电商 | `get_product` | 读 | — |
| 电商 | `get_order` | 读 | 污点源：订单包含客户联系方式与地址 |
| 电商 | `update_price` | 写 | 累积型价格变化 |
| 电商 | `update_stock` | 写 | 库存变化 |
| 电商 | `create_coupon` | 写 | 序列风险、跨 Agent 叠加 |
| 电商 | `create_order` | 写 | 序列风险 |
| 电商 | `refund_order` | 写 | 订单状态变化 |
| 电商 | `send_email` | 写 | 污点汇，电商与 corp 共用 |
| corp | `list_files` | 读 | 文件枚举 |
| corp | `read_file` | 读 | 污点源：文件内容 |
| corp | `write_file` | 写 | 文件写入 |
| corp | `read_forum` | 读 | 污点源：论坛内容可能包含注入指令 |
| corp | `post_forum` | 写 | 公开出口 |
| corp | `create_ticket` | 写 | 跨系统外传汇点 |
| corp | `execute_wire` | 写 | 大额电汇进入审批门槛 |

组合风险与工具的关系如下：

| 规则类型 | 触发组合 |
|---|---|
| 累积型 | `update_price` 连续调价；corp 外发计数 |
| 序列型 | `create_coupon` 后紧跟带券 `create_order` |
| 污点型 | `get_order` / `read_file` / `read_forum` 后的外发或跨系统写入 |
| 跨 Agent 型 | `pricing_agent` 调价与 `marketing_agent` 发券叠加 |
| 权限门槛 | `execute_wire` 大额请求进入审批路径 |

`send_email` 没有领域效果声明，因为它不改变商城状态。这个边界同时验证了
效果声明机制对无副作用工具也成立。

---

## 17. 实现阶段与验收

| 阶段 | 范围 | 验收 |
|---|---|---|
| 领域与基础能力 | 商城、工具层、单次策略、审计、Provenance、幂等 | `make test`、`make lint` 通过 |
| 会话状态与组合风险 | 会话状态、风险预算、四类规则、合并算子、乐观锁 | 场景 2、4 可复现 |
| 计划审批与交付 | 投影、计划生命周期、审批、控制台、评测、MCP / LangChain | 场景 3 可复现，三套评测和演示命令通过 |
| 调研复核与通用域 | 威胁编号、OWASP 映射、确定性测试、corp 域、Bench-Corp 处置 | `eval`、`eval-corp`、`eval-bench` 通过 |
| 单节点生产基线 | API key、CSRF、限流、日志、WAL、PVC、脱敏、锚点、备份与 TLS | 生产环境启动校验、全量测试、lint、bench 与部署清单通过 |

阶段一建立可运行基线，阶段二把“同一会话内的连续影响”变成状态，
阶段三把状态用于计划审批和接入交付，阶段四验证第二领域和评测边界。
阶段五把同一引擎收束到可备份、可审计、可直接部署的单节点形态。

当前 CI 依次执行 Ruff、pytest、`guardrail eval`、`guardrail eval-corp`
和 `guardrail eval-bench`。单节点部署、向前迁移、备份恢复和性能基线已经
纳入交付验收；多副本容量与故障转移不在当前范围内。

---

## 18. 设计决策记录

这里记录三项已经确定的设计取舍。

### 18.1 投影不做工具侧契约

**决策**：不在工具层引入 `project()` 契约。投影改为**网关侧**实现——网关维护影子领域模型，工具只需在网关的清单里声明效果（§4.3）。

**理由**：工具侧契约会把护栏与被保护系统耦合，接入新工具就要改工具代码。
网关侧投影不要求工具侧配合，也是 §19.3 的 MCP 适配器能成立的前提。

**代价**：网关需要知道自己保护的是什么领域。区域扩展到 corp 后，系统
新增了一份领域适配，也验证了这项成本是显式且可控的。

### 18.2 单进程运行，不把扩展点伪装成多副本能力

**决策**：当前**单进程、单 worker、单写者**。会话状态、审计、待审批项全部经
`SessionStore` / `AuditSink` 协议访问（§19.1），但只提供 SQLite 实现。

**理由**：多副本本身会引入与组合风险主线无关的复杂度，而 SQLite 也不适合
多写者。把状态访问收口到协议，是为后续替换外置存储留下接缝，不是声称当前
已经支持多副本。

跨 Agent 场景在同一进程内使用两个身份（`pricing_agent` / `marketing_agent`）
共享 `task_id`，不启两个进程。

合并语义与并发语义由 §3.7 定义，与部署形态无关。因此单进程下演示的
跨 Agent 组合风险不需要等真实多进程部署才成立。

多机分布式一致性不在当前范围内。生产清单固定单副本、单 worker，并在文档
和探针中保持这一事实。

### 18.3 风险预算系数由评测验证

**决策**：§3.3 的数值（1.0 初始，0.05 / 0.15 / 0.20 消耗）全部可配置；
`make sweep` 小网格扫描（warn_surcharge 0.10–0.30 × ask 线 0.05–0.20）在
demo 语料下均满足双达标约束。语料扩大后需重扫。

所有系数从 `policies/combined_risk.yaml` 读取，方便做参数扫描。

---

## 19. 工程边界与交付

系统保留了几处扩展接缝，但没有为“可能的未来”提前抽象完整基础设施。
当前实现优先保证可复现、可验证、可替换。

### 19.1 架构扩展点

| 接缝 | 协议 | 当前实现 | 可替换方向 |
|---|---|---|---|
| 会话存储 | `SessionStore` | SQLite | Redis / Postgres |
| 审计后端 | `AuditSink` | SQLite 哈希链 | WORM 存储 / 外部日志 |
| 决策事件 | `DecisionEventBus` | 进程内同步调用 | Redis Pub/Sub |

当前调用方已经走协议，替换实现不需要改业务层。

### 19.2 多进程扩展条件（当前未交付）

当前实现是单进程。若未来要扩展到多进程，至少需要保持四个约束：

1. 会话状态、预算和待审批项全部外置。
2. 进程内不缓存会话数据，只缓存只读配置与策略。
3. 跨 Agent 关联通过 `task_id` 查询存储，不依赖进程内注册表。
4. 状态写入保留 CAS 语义。

多机分布式一致性不在范围内。这里只描述未来改造条件，不把当前 SQLite
版本表述成已经验证多进程。

### 19.3 接入形态

网关提供三种接入方式，共用同一套工具注册、策略和审计：

| 形态 | 当前实现 |
|---|---|
| HTTP | FastAPI 原生接口 |
| MCP | stdio JSON-RPC 的 initialize / tools/list / tools/call |
| LangChain | duck-typed 工具对象，不硬依赖 LangChain |

MCP 和 LangChain 适配器都支持透传 agent API key。未实现的 MCP resources、
prompts 和采样能力记录在 limitations 中。

### 19.4 可观测性

当前可观测性保持最小闭环：

| 入口 | 内容 |
|---|---|
| `/metrics` | 审计条目按决策计数、拒绝原因 TOP 10 |
| `/healthz` | 进程存活，不检查外部依赖 |
| `/readyz` | 网关数据库、三份策略和商城依赖状态 |
| stdout | 生产环境逐行 JSON 访问日志，包含 request_id / status / latency |

`/metrics` 与 `/readyz` 在启用 API key 后遵循对应读端鉴权；探针不经过
控制台 cookie。OTel trace、延迟直方图和投影耗时指标尚未实现，见 limitations。

### 19.5 开发者工具与交付

| 入口 | 当前内容 |
|---|---|
| CLI | `scenarios` / `demo` / `eval` / `eval-corp` / `eval-bench` / `sweep` / `verify` / `audit-anchor` / `maintenance` / `bench` / `policy-lint` |
| 本地运行 | `make demo`、`make scenarios`、`make up` |
| 生产部署 | `deploy/k8s/` + `deploy/README.md`；TLS Ingress、PVC、密钥、备份恢复和回滚 |
| CI | Ruff、pytest、`eval`、`eval-corp`、`eval-bench` |
| 契约 | FastAPI 自动生成 OpenAPI |
| 策略检查 | 启动时 lint，CLI 可单独运行 |

CI 的评测门槛把拦截率和误伤率变成回归约束。策略改动导致指标劣化时，
流水线会失败。

### 19.6 局限说明

`docs/limitations.md` 是交付物的一部分。它记录当前系统已知的语义边界、
工程边界和转译缺口，避免把演示结果表述成安全保证。

当前需要重点保留的局限包括：

- Agent 完全绕开网关时，系统无法保护目标。
- 哈希链只能证明记录未被改写，不能证明写入内容为真。
- 跨 Agent 求值读取快照，不保证全局一致。
- 风险预算系数只在当前语料上做过小网格验证。
- 单节点 SQLite 不具备多实例一致性；限流也是进程内状态。
- 计划与审批表保留执行所需原始参数，业务卷必须加密并限制 API 访问。
- 审计链尾部截断依赖外部锚点；锚点必须写到独立存储。
- corp 域的受众粒度与前置条件断言仍缺失。

可选交付 `docs/threat-model.md` 尚未创建。

### 19.7 范围边界

- **交付范围**：单节点生产基线、TLS Ingress、PVC、API key 与控制台鉴权、
  CSRF、安全响应头、限流与请求体限额、JSON 日志、WAL、审计脱敏、外部锚点、
  保留期清理、备份恢复和性能基准。
- **不做**：前端框架（控制台已满足审批交互）、通用策略语言、K8s 之上的
  高可用编排（HPA/多区域）、外置会话存储、跨副本全局限流。
- **后续路线**：外置存储后端、全局限流、策略版本化发布、多租户与会话
  控制面。
- 第二领域已经落在 corp 域，但它用于验证域适配成本，不代表系统要扩张成
  通用多领域平台。

---

## 20. 与同类方案的设计对照

| 维度 | 同类方案常见形态 | 本项目 |
|---|---|---|
| 判定单位 | 单次调用为主，少数覆盖跨调用序列 | 会话与计划 |
| 组合风险 | 少数覆盖数据流或序列，跨 Agent 较少 | 四类规则，有实测拦截率 |
| 跨 Agent | 较少覆盖 | 支持 |
| 审批单位 | 多数按单次调用审批 | 整份计划与终态投影 |
| 批准后校验 | 多数不提供动作哈希 | 每步重校验与动作哈希 |
| 失败语义 | 部分方案允许 fail-open | 明确 fail-closed |
| 策略可测性 | 已有多家做回归和 bench | 回归、语义与误伤率 |

---

## 21. 调研复核与后续路线

> 依据：对 25+ 个开源项目的两轮同类方案调研（详见调研文档 §9/§10）。本节是
> 「调研发现 → 设计动作」的核对清单，分三类：**A 已吸收进本设计的认知**、
> **B 改进项路线（按优先级）**、**C 明确不做（记录理由）**。

### A. 已吸收的认知修正

1. **设计重点收窄为四项可验证主张**：数值组合风险、风险预算阶梯、
   跨 Agent 合并算子、计划级投影审批与 token 校验。
   调研后的表述不再声称“所有方案都不做组合风险”，因为 OpenAPPA 已做
   跨调用数据流，cordum 已做 job 级审批。
2. **威胁模型更新**：microsoft/agent-governance-toolkit 是最接近的同类
   项目。其 ACS 引擎采用逐次求值，跨调用状态由宿主承担。这说明本项目
   把状态做进引擎是有效选择，也说明设计重点需要继续落在业务语义深度，
   而不是停在“有会话状态”本身。
3. **范围边界声明**（回应「读者无法判断是有意排除还是没看到」）：本项目
   判定层在**工具调用序列**；LLM I/O 护栏（NeMo Guardrails、guardrails-ai、
   litellm 等）与 MCP 网关基础设施（IBM/mcp-context-forge、plano）是不同层，
   可叠加使用，不构成直接同层方案。

### B. 改进项（按优先级）

| # | 改进项 | 来源 | 优先级 | 工作量 | 说明 |
|---|---|---|---|---|---|
| B1 | **对抗语料加攻击框架编号映射**（OWASP Agentic Top 10 / MITRE ATLAS） | Rampart bench 做法 + 微软 OWASP 映射 | 高 | 中（纯文档+YAML 字段） | 每个 adversarial case 标注覆盖的框架条目；拦截率表可按条目聚合，补强 §12.3 的验证能力 |
| B2 | **OWASP Agentic Top 10 合规映射文档** | 微软 7F+3P 映射表 | 高 | 小（一张表） | 「本项目组合风险与基础能力 × OWASP 条目」覆盖矩阵，成本低且边界明确 |
| B3 | **求值可复现性测试**：同一审计日志重放得到同一决策序列 | OpenAPPA「same log always gets the same decision」 | 中 | 小 | 本项目投影+求值本就确定性，缺一条显式 property test 固化该性质 |
| B4 | **语料向标准化 bench 形态靠拢 / 交叉验证** | OpenAPPA Bench-Corp + AgentThreatBench | 中 | 中 | 回应 limitations #10（自编语料）。可行性已确认：Bench-Corp 为 MIT 许可，但场景绑定其自带的 file/email/GitHub CLI 域（黑盒按可观测副作用评分，仿 AgentDojo）——直接接入需先实现文件/邮件域的影子模型与效果声明，是一个完整项目而非配置项。**结论：不做直接接入；对标其方法论（副作用评分 + 双轴）已完成**——本评测按网关响应判定（副作用等价物）、拦截率/误伤率双轴与之一致。**已交付（两阶段）**：corp 域适配层（文件/邮件/论坛/工单/电汇 7 工具 + 独立策略）+ 语料 `eval/cases_corp.yaml`（10 组）与 `eval/cases_bench_corp.yaml`（Bench-Corp 20 场景全量处置 29 case：14 完整转译 + 1 仅 attack 路径 + 5 skipped；primary 28 + 已知误伤 3，`make eval-bench` primary 实测 100%/0%）。转译发现两个机制缺口并如实记录：受众粒度（内部/外部二值谓词不可分）与前置条件断言（正向序列模式无法表达缺失前置），见 limitations |
| B5 | **Claude Code hook 接入形态**（cordum Edge 模式） | cordum | 低 | 中 | **暂缓**：本地 hook 防火墙是第四接入面；MCP、LangChain 和 HTTP 已覆盖当前接入需求，等真实场景出现再评估 |
| B6 | **通用 egress 出口控制** | pipelock | 低 | — | send_email 外部域拦截已覆盖 demo 领域；通用化与领域内聚冲突，明确不做通用层 |

### C. 明确不做（记录理由）

| 项 | 理由 |
|---|---|
| 多框架适配（CrewAI 等 governed 示例） | MCP、LangChain 和 HTTP 已覆盖当前接入需求；继续增加适配器不会提升核心设计的信息量 |
| OWASP 全量合规认证 | 只做映射表（B2），不做认证流程 |
| IFC 标签流模型重构 | 微软 ACS 的 stateless 标签流与本项目持久化 T 是两种架构；已有论证（§3.2），不为追随其他项目而改 |
| OTel 导出 / 多进程 | OTel 与多副本一致性仍是边界；控制台登录态与 CSRF 已交付，不再列入未做项 |

### D. 执行状态

- B1/B2/B3 已交付——语料威胁编号、OWASP 映射、可复现性测试，均不动
  核心引擎，直接补强 §12 的「策略可测试性」
- B4 的 corp 域适配层已交付（`make eval-corp` + `make eval-bench` 全量
  转译）；直接跑 OpenAPPA bench runner 仍需 file/email/GitHub 域适配
  （后续候选）
- B5 暂缓，等真实接入需求
- 每月一次同类方案调研复核，发现新威胁先更新本节再动代码

---

## 22. 能力完备性检查

这一节不以生产级治理平台为标尺，而是检查一个“可运行的会话级护栏网关”
是否形成了完整闭环。结论是：**基础能力闭环完整；生产运维和规模化能力
仍有缺口。**

### 22.1 已具备

| 能力 | 当前实现 |
|---|---|
| 身份与权限 | API key 映射到 `agent_id` / `approver_id`；默认 demo 可关闭 |
| 单次策略 | YAML 策略、权限表、参数契约、语法阈值、结果态上限 |
| 会话状态 | `(Δ, T, A)`、风险预算、TTL、乐观锁、截断动作历史 |
| 组合风险 | 累积、序列、污点、跨 Agent 四类规则 |
| 计划审批 | 计划提交、终态投影、风险分级、`plan_token`、生命周期 |
| 失败语义 | 策略/投影/审计失败均 fail-closed；HTTP 错误映射明确 |
| 幂等与审计 | 幂等键、执行前后审计、哈希链校验 |
| 接入 | HTTP、MCP stdio、LangChain duck-typed 工具 |
| 可观测性 | `/healthz`、`/metrics`、`/readyz`、JSON 访问日志、审计链校验 |
| 隐私与保留 | 审计参数字段级脱敏、operational retention、外部锚点 |
| 交付 | Docker Compose、CI、CLI、策略 lint、评测门禁、单节点 K8s/PVC/TLS 清单 |

### 22.2 常见但尚未实现或只做了部分

| 能力 | 状态 | 影响 |
|---|---|---|
| **请求级限流** | 单节点已实现 | 进程内滑动窗口；多副本前需要全局限流后端 |
| **会话撤销 / kill switch** | 未实现 | 只能等 TTL 到期；事故中缺少立即冻结某会话或某 agent 的入口 |
| **审批通知与升级** | 未实现 | 待审批项需要控制台轮询，没有 webhook、邮件、超时升级或值班路由 |
| **策略热加载 / 版本回滚** | 未实现 | 策略随镜像发布；变更需要重启，缺少在线 diff 与回滚入口 |
| **审计脱敏与保留策略** | 已实现 | 审计写库前字段级脱敏；计划/审批表为执行保留原始参数，依赖加密卷 |
| **OTel trace / 延迟指标** | 部分实现 | 有决策计数和访问日志，没有投影耗时与跨服务 trace |
| **后台清理与恢复** | 已实现 | `maintenance` 清理过期会话和终态业务记录，审计链保持追加 |
| **多进程 / 分布式状态** | 未实现 | 已有协议接缝，但只有 SQLite 实现；多实例一致性仍是边界 |
| **压力测试与容量基线** | 已实现 | `make bench` 提供单节点 p50/p95/p99 与并发吞吐，不作为多副本容量结论 |

### 22.3 结论

核心链路已经完整：Agent 接入、单次判定、组合风险、计划审批、审计和
评测都能形成闭环，足以支撑“会话级风险护栏”的设计展示。

单节点生产试点的必要闭环已经成立：入口鉴权、控制台身份、CSRF、限流、
持久化、日志、探针、备份恢复和审计锚点都有对应实现与测试。下一步若要
进入多租户或高可用场景，优先级是 **外置存储与全局限流**、**会话撤销**、
**策略版本化与回滚**、**审批通知与升级**。这些是运维控制面能力，不改变
当前组合风险的判定模型。
